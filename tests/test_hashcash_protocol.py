"""Protocol facts and edge cases for the hashcash solver.

These pin down what the older tests left implicit: the header formats MEGA's
own clients use, the exact threshold for each easiness band, that a solution
header carries a prefix that verifies,
that the external solver's time comes out of the same deadline as the built-in
search, and which external-solver outputs are taken as an answer.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import time

import pytest

import kiwi_dl.core.hashcash as hashcash
from kiwi_dl.core.hashcash import (
    REPEAT,
    TOKEN_BYTES,
    HashcashChallenge,
    HashcashError,
    parse_challenge,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _from_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.mark.parametrize(
    ("easiness", "threshold"),
    [
        (0, 8),
        (1, 3 << 3),
        (63, 127 << 3),
        (64, 1 << 10),
        (128, 1 << 17),
        (192, 1 << 24),
        (255, 127 << 24),
    ],
)
def test_threshold_matches_the_protocol_formula(easiness: int, threshold: int) -> None:
    challenge = HashcashChallenge(version=1, easiness=easiness, token=b"\0" * TOKEN_BYTES)
    assert challenge.threshold == threshold


# The sample header in MEGA's C++ SDK (src/posix/net.cpp), documented there as
# <version>:<easiness>:<timestamp>:<b64token>.
MEGA_SDK_EXAMPLE = (
    "1:100:1731410499:RUvIePV2PNO8ofg8xp1aT5ugBcKSEzwKoLBw9o4E6F_fmn44eC3oMpv388UtFl2K"
)


def test_parses_the_example_header_from_megas_sdk() -> None:
    challenge = parse_challenge(MEGA_SDK_EXAMPLE)
    assert challenge.version == 1
    assert challenge.easiness == 100
    assert challenge.token == _from_b64url(MEGA_SDK_EXAMPLE.rsplit(":", 1)[1])
    assert len(challenge.token) == TOKEN_BYTES


@pytest.mark.parametrize(
    "header",
    [
        "1:100:" + MEGA_SDK_EXAMPLE.rsplit(":", 1)[1],  # no timestamp
        MEGA_SDK_EXAMPLE + ":extra",
    ],
)
def test_challenge_without_exactly_four_fields_is_rejected(header: str) -> None:
    with pytest.raises(HashcashError, match="Malformed"):
        parse_challenge(header)


def test_solution_header_echoes_the_token_and_carries_a_prefix_that_verifies(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KIWI_DL_HASHCASH_NATIVE", "0")
    token = bytes(range(TOKEN_BYTES))
    token_b64 = _b64url(token)

    solution = hashcash.build_solution_header(f"1:255:1731410499:{token_b64}", timeout=30.0)

    version, token_text, prefix_text = solution.split(":")
    assert version == "1"
    assert token_text == token_b64
    assert len(prefix_text) == 6
    prefix = _from_b64url(prefix_text)
    assert len(prefix) == 4
    head = int.from_bytes(hashlib.sha256(prefix + token * REPEAT).digest()[:4], "big")
    assert head <= 127 << 24


@pytest.mark.parametrize("junk", ["!", " ", "*", "="])
def test_token_with_characters_outside_base64url_is_rejected(junk: str) -> None:
    text = _b64url(b"A" * TOKEN_BYTES)
    with pytest.raises(HashcashError, match="base64"):
        parse_challenge(f"1:192:1731410499:{text[:10]}{junk}{text[11:]}")


def test_external_solver_time_comes_out_of_the_same_deadline(monkeypatch, tmp_path) -> None:
    """A slow, failing external solver must not buy the built-in search a fresh timeout."""
    solver = tmp_path / "solver"
    solver.write_bytes(b"")
    monkeypatch.setenv("KIWI_DL_HASHCASH_SOLVER", str(solver))

    def slow_crash(args, **kwargs):
        time.sleep(1.0)
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="crashed")

    monkeypatch.setattr(hashcash.subprocess, "run", slow_crash)
    monkeypatch.setattr(hashcash, "_check_nonce", lambda *args: False)

    challenge = HashcashChallenge(version=1, easiness=0, token=b"\x05" * TOKEN_BYTES)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        hashcash.solve(challenge, timeout=1.2, workers=2)
    elapsed = time.monotonic() - started

    # One budget is ~1.2 s. Restarting it after the solver would take ~2.2 s.
    assert elapsed < 1.8, f"took {elapsed:.2f}s of a 1.2s budget"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("0000002a\n", bytes.fromhex("0000002a")),
        ("searching...\nDEADBEEF\n", bytes.fromhex("deadbeef")),
        ("0000002a\nnot an answer\n", bytes.fromhex("0000002a")),
        ("ab cd ef\n", None),  # 8 characters, but only 3 bytes
        ("0x00002a\n", None),
        ("0000002a0\n", None),
        ("", None),
    ],
)
def test_only_a_line_of_exactly_eight_hex_digits_is_an_answer(
    stdout: str, expected: bytes | None
) -> None:
    assert hashcash._prefix_from_output(stdout) == expected


def test_solver_exit_code_2_means_out_of_time(monkeypatch, tmp_path) -> None:
    solver = tmp_path / "solver"
    solver.write_bytes(b"")
    monkeypatch.setenv("KIWI_DL_HASHCASH_SOLVER", str(solver))
    monkeypatch.setattr(
        hashcash.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 2, stdout="", stderr=""),
    )
    challenge = HashcashChallenge(version=1, easiness=192, token=b"\x07" * TOKEN_BYTES)
    with pytest.raises(TimeoutError):
        hashcash._solve_with_native(challenge, timeout=1.0, workers=1)


@pytest.mark.parametrize("switch", ["0", "false", "No", "OFF", "n"])
def test_native_switch_off_ignores_a_configured_solver(monkeypatch, tmp_path, switch) -> None:
    solver = tmp_path / "solver"
    solver.write_bytes(b"")
    monkeypatch.setenv("KIWI_DL_HASHCASH_SOLVER", str(solver))
    monkeypatch.setenv("KIWI_DL_HASHCASH_NATIVE", switch)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the external solver ran although it was switched off")

    monkeypatch.setattr(hashcash.subprocess, "run", must_not_run)
    challenge = HashcashChallenge(version=1, easiness=192, token=b"\x08" * TOKEN_BYTES)
    assert hashcash._solve_with_native(challenge, timeout=1.0, workers=1) is None


def test_a_crashing_worker_is_reported_not_disguised_as_a_timeout(monkeypatch) -> None:
    monkeypatch.setenv("KIWI_DL_HASHCASH_NATIVE", "0")

    def broken(*args):
        raise RuntimeError("hash backend broke")

    monkeypatch.setattr(hashcash, "_check_nonce", broken)
    challenge = HashcashChallenge(version=1, easiness=0, token=b"\x06" * TOKEN_BYTES)
    with pytest.raises(RuntimeError, match="hash backend broke"):
        hashcash.solve(challenge, timeout=5.0, workers=2)
