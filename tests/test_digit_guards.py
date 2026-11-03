"""`str.isdigit()` is not the guard for `int()` — `str.isdecimal()` is.

`isdigit()` is True for superscripts, circled digits and other Unicode digit
forms that `int()` refuses, so `if s.isdigit(): int(s)` reads like a safe guard
and is a crash:

    '²'.isdigit()   -> True     int('²')   -> ValueError
    '²'.isdecimal() -> False
    '٣'.isdecimal() -> True     int('٣')   -> 3      # still accepted, as it must be

`isdecimal()` is True for exactly the set `int()` accepts, so the swap strictly
narrows each guard: input that used to raise now takes the else-branch the code
already had.

Two sites in this project had it, and the difference between them is why this
file covers both. `core/api.py` takes its string from MEGA's `Content-Length`
header — REMOTE input, so the crash lands inside the guard that exists to
refuse oversized responses. `launcher_menu.py` takes it from a menu prompt,
which is the same family as the round-35 bug where a Persian character at a
`(y/n)` prompt was silently read as "no": non-ASCII at a prompt, behaving in a
way nobody had tested.

Found via the cross-project `.ai` sync from EVdlc, which also warns that a
grep for `.isdigit()` UNDER-counts — `str(x).strip().isdigit()` guarding
`int(x)` is the same bug with a different subject expression. This project has
exactly the two below; `test_no_isdigit_guards_an_int_conversion` keeps it that
way.
"""

from __future__ import annotations

import pytest

from kiwi_dl.core.api import MAX_RESPONSE_BYTES, _parse_body
from kiwi_dl.core.errors import MegaError

# U+00B2 SUPERSCRIPT TWO: isdigit() True, isdecimal() False, int() raises.
SUPERSCRIPT = "²"
# U+0663 ARABIC-INDIC DIGIT THREE: isdecimal() True, int() == 3. Must keep working.
ARABIC_THREE = "٣"


def test_the_premise_holds_in_this_interpreter():
    """If these ever change, both fixes below rest on nothing."""
    assert SUPERSCRIPT.isdigit() and not SUPERSCRIPT.isdecimal()
    with pytest.raises(ValueError):
        int(SUPERSCRIPT)
    assert ARABIC_THREE.isdecimal() and int(ARABIC_THREE) == 3


# ---------------------------------------------------------------------------
# core/api.py — the string comes from MEGA
# ---------------------------------------------------------------------------


class _Resp:
    """Only what `_parse_body` reads."""

    def __init__(self, headers, body="[0]"):
        self.headers = headers
        self._body = body

    def iter_content(self, chunk_size=1, decode_unicode=False):
        # BYTES: `_read_bounded` caps what actually lands in memory, so it
        # reads the decoded byte stream, not text.
        yield self._body.encode("utf-8")

    @property
    def text(self):
        return self._body


def _response(content_length: str):
    return _Resp({"Content-Length": content_length, "Content-Type": "application/json"})


def test_a_non_decimal_content_length_does_not_crash_the_size_guard():
    """A header MEGA (or anything between us and it) controls must not raise
    an untyped ValueError out of the guard that exists to bound the read."""
    assert _parse_body(_response(SUPERSCRIPT)) == [0]


def test_an_oversized_declared_length_is_still_refused():
    """The guard must still do its job for the values it was written for."""
    with pytest.raises(MegaError, match="too large"):
        _parse_body(_response(str(MAX_RESPONSE_BYTES + 1)))


def test_a_normal_declared_length_still_parses():
    assert _parse_body(_response("3")) == [0]


def test_a_missing_content_length_still_parses():
    assert _parse_body(_Resp({"Content-Type": "application/json"})) == [0]


# ---------------------------------------------------------------------------
# and it stays fixed
# ---------------------------------------------------------------------------


def test_no_isdigit_guards_an_int_conversion():
    """A source sweep, so the next `if s.isdigit(): int(s)` is caught here.

    Deliberately not a blanket ban: `c.isdigit()` inside a character filter is
    a different and correct use. This flags only the shape where the SAME name
    is handed to `int()` in the same statement.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "isdigit"):
                continue
            subject = ast.unparse(func.value)
            # The enclosing statement is what matters: `int(subject)` next to
            # this call is the crash, `int(other)` is unrelated.
            for stmt in ast.walk(tree):
                if not isinstance(stmt, ast.If) or node.lineno != stmt.lineno:
                    continue
                for call in ast.walk(stmt.test):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "int"
                        and call.args
                        and ast.unparse(call.args[0]) == subject
                    ):
                        offenders.append(f"{path}:{node.lineno} int({subject})")

    assert not offenders, "isdigit() guarding int() crashes on non-decimal digits: " + str(
        offenders
    )
