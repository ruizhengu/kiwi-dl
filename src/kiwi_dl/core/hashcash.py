"""MEGA's hashcash proof-of-work.

When MEGA wants a client to slow down, it answers an API request with HTTP 402
and an ``X-Hashcash`` challenge: a version, an *easiness* and a 48-byte token.
The client has to find a 4-byte prefix such that

    SHA-256(prefix || token repeated 262,144 times)

starts with a big-endian 32-bit number no greater than a threshold derived from
the easiness, then repeat the request with that prefix attached.

The search runs on threads (``hashlib`` releases the GIL while it hashes, so
they really do run in parallel). An external solver can be plugged in through
``KIWI_DL_HASHCASH_SOLVER``; nothing ships one.

Written for kiwi-dl from the protocol description above. It contains no
MegaBasterd code (see ATTRIBUTION.md).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

log = logging.getLogger(__name__)

# --- Protocol constants ------------------------------------------------------

TOKEN_BYTES = 48
PREFIX_BYTES = 4
REPEAT = 262_144

SUPPORTED_VERSIONS = (1,)
MAX_EASINESS = 255  # Easiness travels as a single byte.

# A 48-byte token is 64 base64 characters. Anything much longer is refused
# before it is decoded, so a hostile header cannot make us decode megabytes.
MAX_TOKEN_B64_CHARS = 128

# --- Time and parallelism ----------------------------------------------------

# Per-solve cap for direct callers. api.py hands each solve only what is left
# of its own per-request budget, so this is an upper bound, not a promise.
DEFAULT_TIMEOUT_S = 30.0

# Extra time an external solver gets past its own deadline to report back
# before it is killed.
NATIVE_TIMEOUT_MARGIN_S = 5.0

DEFAULT_WORKERS = 8
MAX_WORKERS = 32

# How often the main thread wakes while the workers search, so a deadline or a
# Ctrl-C is noticed promptly.
_POLL_INTERVAL_S = 0.5

# --- External solver contract ------------------------------------------------
#
# KIWI_DL_HASHCASH_SOLVER names an executable. It is run as
#     <solver> <easiness> <token as 96 hex chars> <workers> <timeout in ms>
# and must print the 4-byte prefix as 8 hex digits on a line of its own.
# Exit code 2 means "not solved in time"; any other failure falls back to the
# built-in search. KIWI_DL_HASHCASH_NATIVE=0 (or false/no/off) ignores it.

_SOLVER_ENV = "KIWI_DL_HASHCASH_SOLVER"
_SOLVER_SWITCH_ENV = "KIWI_DL_HASHCASH_NATIVE"
_SWITCH_OFF = frozenset({"0", "false", "n", "no", "off"})
_SOLVER_GAVE_UP = 2
_HEX_PREFIX = re.compile(r"[0-9a-fA-F]{8}")  # PREFIX_BYTES as hex


class HashcashError(ValueError):
    """The X-Hashcash challenge is malformed or asks for something unsupported."""


@dataclass(frozen=True)
class HashcashChallenge:
    """One decoded X-Hashcash challenge.

    Validated on construction, so a challenge built by hand gets the same
    bounds as one parsed off the wire.
    """

    version: int
    easiness: int
    token: bytes

    def __post_init__(self) -> None:
        if self.version not in SUPPORTED_VERSIONS:
            raise HashcashError(f"Unsupported hashcash version {self.version}")
        if not 0 <= self.easiness <= MAX_EASINESS:
            raise HashcashError(
                f"hashcash easiness {self.easiness} is out of range (0-{MAX_EASINESS})"
            )

    @property
    def threshold(self) -> int:
        """The largest digest head that counts as a solution.

        The easiness byte is a tiny float: its low 6 bits give an odd mantissa
        and its top 2 bits pick a power of two, 7 bits apart. Easiness 0 gives
        8 (about one hit in 500 million hashes); 255 gives 127 << 24 (about one
        hit in two).
        """
        mantissa = 2 * (self.easiness % 64) + 1
        exponent = 7 * (self.easiness // 64) + 3
        return mantissa << exponent


# --- Wire format -------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_token(text: str) -> bytes:
    if len(text) > MAX_TOKEN_B64_CHARS:
        raise HashcashError(
            f"hashcash token is too long ({len(text)} characters, "
            f"at most {MAX_TOKEN_B64_CHARS})"
        )
    padded = text + "=" * (-len(text) % 4)
    try:
        token = base64.b64decode(padded, altchars=b"-_", validate=True)
    except ValueError as exc:  # binascii.Error is a ValueError
        raise HashcashError("hashcash token is not valid base64") from exc
    if len(token) != TOKEN_BYTES:
        raise HashcashError(f"hashcash token must be {TOKEN_BYTES} bytes, got {len(token)}")
    return token


def parse_challenge(header: str) -> HashcashChallenge:
    """Decode an ``X-Hashcash`` challenge of the form ``version:easiness:token``.

    The token is unpadded base64url. Raises HashcashError (a ValueError) for
    anything malformed or out of range.
    """
    fields = header.strip().split(":")
    if len(fields) != 3:
        raise HashcashError(f"Malformed hashcash challenge: expected 3 fields, got {len(fields)}")
    version_text, easiness_text, token_text = fields
    try:
        version = int(version_text)
        easiness = int(easiness_text)
    except ValueError as exc:
        raise HashcashError("Bad numeric field in hashcash challenge") from exc
    token = _decode_token(token_text)
    return HashcashChallenge(version=version, easiness=easiness, token=token)


def build_solution_header(challenge_header: str, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Solve a raw challenge header and return the value to send back.

    The answer is ``version:easiness:token:prefix``, with the token and prefix
    in unpadded base64url. Raises TimeoutError if no prefix is found in time.
    """
    challenge = parse_challenge(challenge_header)
    prefix = solve(challenge, timeout=timeout)
    return ":".join(
        (
            str(challenge.version),
            str(challenge.easiness),
            _b64url_encode(challenge.token),
            _b64url_encode(prefix),
        )
    )


# --- Solving -----------------------------------------------------------------


def _check_nonce(nonce_bytes: bytes, token_repeated: bytes, threshold: int) -> bool:
    """True if ``nonce_bytes`` (the prefix) solves the challenge."""
    hasher = hashlib.sha256(nonce_bytes)
    hasher.update(token_repeated)
    head = int.from_bytes(hasher.digest()[:4], "big")
    return head <= threshold


def _clamp_workers(workers: int) -> int:
    return max(1, min(MAX_WORKERS, int(workers)))


def solve(
    challenge: HashcashChallenge,
    timeout: float = DEFAULT_TIMEOUT_S,
    workers: int = DEFAULT_WORKERS,
) -> bytes:
    """Find a 4-byte prefix that solves ``challenge`` within ``timeout`` seconds.

    Tries the external solver first if one is configured, then searches on
    ``workers`` threads. The timeout covers both. Raises TimeoutError if
    nothing is found in time.
    """
    deadline = time.monotonic() + timeout
    workers = _clamp_workers(workers)
    token_repeated = challenge.token * REPEAT

    prefix = _solve_with_native(challenge, timeout=timeout, workers=workers)
    if prefix is not None:
        if _check_nonce(prefix, token_repeated, challenge.threshold):
            return prefix
        log.warning("The external hashcash solver returned a prefix that does not verify")

    if time.monotonic() >= deadline:
        raise TimeoutError("The hashcash challenge could not be solved in time")
    return _search(token_repeated, challenge.threshold, deadline, workers)


def _search(token_repeated: bytes, threshold: int, deadline: float, workers: int) -> bytes:
    """Split the 32-bit prefix space across threads, interleaved by worker index."""
    stop = threading.Event()
    winner: list[bytes] = []
    winner_lock = threading.Lock()

    def scan(offset: int) -> None:
        for candidate in range(offset, 1 << (8 * PREFIX_BYTES), workers):
            if stop.is_set() or time.monotonic() > deadline:
                return
            prefix = candidate.to_bytes(PREFIX_BYTES, "big")
            if _check_nonce(prefix, token_repeated, threshold):
                with winner_lock:
                    if not winner:
                        winner.append(prefix)
                stop.set()
                return

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kiwi-hashcash")
    futures = []
    try:
        for offset in range(workers):
            futures.append(pool.submit(scan, offset))
        while not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = wait(
                futures, timeout=min(_POLL_INTERVAL_S, remaining), return_when=FIRST_COMPLETED
            )
            if done:
                break
    finally:
        # Also runs on Ctrl-C: without it the workers would hash on until the
        # deadline and shutdown() below would wait for them.
        stop.set()
        pool.shutdown(wait=True)

    if winner:
        return winner[0]
    for future in futures:
        error = future.exception()
        if error is not None:
            raise error
    raise TimeoutError("The hashcash challenge could not be solved in time")


# --- External solver ---------------------------------------------------------


def _native_solver_path() -> str | None:
    if os.environ.get(_SOLVER_SWITCH_ENV, "").strip().lower() in _SWITCH_OFF:
        return None
    configured = os.environ.get(_SOLVER_ENV, "").strip()
    if not configured:
        return None
    path = os.path.expanduser(configured)
    return path if os.path.isfile(path) else None


def _prefix_from_output(stdout: str) -> bytes | None:
    """The last line of the solver's output that is exactly 8 hex digits."""
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if _HEX_PREFIX.fullmatch(text):
            return bytes.fromhex(text)
    return None


def _solve_with_native(challenge: HashcashChallenge, timeout: float, workers: int) -> bytes | None:
    """Run the external solver, if one is configured.

    Returns the prefix it printed (unverified), or None when there is no solver
    or it failed in a way the built-in search can recover from. Raises
    TimeoutError when the solver says, or shows by overrunning, that the time
    is gone.
    """
    solver = _native_solver_path()
    if solver is None:
        return None

    argv = [
        solver,
        str(challenge.easiness),
        challenge.token.hex(),
        str(_clamp_workers(workers)),
        str(max(1, int(timeout * 1000))),
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout + NATIVE_TIMEOUT_MARGIN_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("The external hashcash solver timed out") from exc
    except OSError as exc:
        log.debug("Could not start the external hashcash solver %s: %s", solver, exc)
        return None

    if result.returncode == _SOLVER_GAVE_UP:
        raise TimeoutError("The hashcash challenge could not be solved in time")
    if result.returncode != 0:
        log.debug(
            "The external hashcash solver exited with code %d: %s",
            result.returncode,
            result.stderr.strip(),
        )
        return None

    prefix = _prefix_from_output(result.stdout)
    if prefix is None:
        log.warning("The external hashcash solver printed no 8-hex-digit prefix")
    return prefix
