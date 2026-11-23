"""MEGA's hashcash proof-of-work.

When MEGA wants a client to slow down, it answers an API request with HTTP 402
and a challenge header

    X-Hashcash: <version>:<easiness>:<timestamp>:<token>
    e.g.        1:100:1731410499:RUvIePV2PNO8ofg8xp1aT5ugBcKSEzwKoLBw9o4E6F_fmn44eC3oMpv388UtFl2K

where the token is 48 bytes of unpadded base64url (64 characters). The client
has to find a 4-byte prefix such that

    SHA-256(prefix || token repeated 262,144 times)

starts with a big-endian 32-bit number no greater than a threshold derived from
the easiness, then repeat the request with

    X-Hashcash: 1:<token, exactly as received>:<prefix in unpadded base64url>

This is the format MEGA's own clients use (its C++ SDK and its web client).

The search runs on threads (``hashlib`` releases the GIL while it hashes, so
they really do run in parallel).

Written for kiwi-dl from the protocol description above. It contains no
MegaBasterd code (see ATTRIBUTION.md).
"""

from __future__ import annotations

import base64
import hashlib
import logging
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

DEFAULT_WORKERS = 8
MAX_WORKERS = 32

# How often the main thread wakes while the workers search, so a deadline or a
# Ctrl-C is noticed promptly.
_POLL_INTERVAL_S = 0.5


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


def _challenge_fields(header: str) -> list[str]:
    fields = header.strip().split(":")
    if len(fields) != 4:
        raise HashcashError(
            "Malformed hashcash challenge: expected version:easiness:timestamp:token, "
            f"got {len(fields)} fields"
        )
    return fields


def parse_challenge(header: str) -> HashcashChallenge:
    """Decode an ``X-Hashcash`` challenge, ``version:easiness:timestamp:token``.

    The timestamp is not used (MEGA's own clients ignore it too). The token is
    unpadded base64url. Raises HashcashError (a ValueError) for anything
    malformed or out of range.
    """
    version_text, easiness_text, _timestamp, token_text = _challenge_fields(header)
    try:
        version = int(version_text)
        easiness = int(easiness_text)
    except ValueError as exc:
        raise HashcashError("Bad numeric field in hashcash challenge") from exc
    token = _decode_token(token_text)
    return HashcashChallenge(version=version, easiness=easiness, token=token)


def build_solution_header(challenge_header: str, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Solve a raw challenge header and return the value to send back.

    The answer is ``version:token:prefix``: the token exactly as MEGA sent it
    and the prefix in unpadded base64url. Raises TimeoutError if no prefix is
    found in time.
    """
    challenge = parse_challenge(challenge_header)
    prefix = solve(challenge, timeout=timeout)
    token_text = _challenge_fields(challenge_header)[3]
    return f"{challenge.version}:{token_text}:{_b64url_encode(prefix)}"


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

    Searches on ``workers`` threads. Raises TimeoutError if nothing is found
    in time.
    """
    deadline = time.monotonic() + timeout
    workers = _clamp_workers(workers)
    token_repeated = challenge.token * REPEAT
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
