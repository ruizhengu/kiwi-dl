"""The Windows console key path of the menu, unit-tested without a console.

`_normalize_windows` is a pure function and the reader takes its key source
as a callable, so both run anywhere; the real `getwch` is only wired up in
`read_terminal_key`, behind the win32 import.
"""

from __future__ import annotations

from kiwi_dl.ui.menu import (
    DOWN,
    END,
    ENTER,
    HOME,
    QUIT,
    UP,
    _normalize_windows,
    _read_terminal_key_windows,
)


def test_arrow_keys_arrive_as_a_prefix_and_a_code():
    assert _normalize_windows("\x00H") == UP
    assert _normalize_windows("\xe0P") == DOWN
    assert _normalize_windows("\x00G") == HOME
    assert _normalize_windows("\xe0O") == END


def test_enter_and_vim_keys_mean_what_the_posix_menu_expects():
    assert _normalize_windows("\r") == ENTER
    assert _normalize_windows("k") == UP
    assert _normalize_windows("j") == DOWN
    assert _normalize_windows("g") == HOME
    assert _normalize_windows("G") == END


def test_quit_keys():
    assert _normalize_windows("q") == QUIT
    assert _normalize_windows("Q") == QUIT
    assert _normalize_windows("\x1b") == QUIT
    assert _normalize_windows("\x03") == QUIT  # Ctrl-C with processed input off


def test_a_printable_char_passes_through_for_the_number_jump():
    assert _normalize_windows("3") == "3"


def test_the_reader_joins_a_two_call_extended_key():
    calls = iter(["\xe0", "P"])
    assert _read_terminal_key_windows(lambda: next(calls)) == DOWN


def test_the_reader_returns_a_plain_key_in_one_call():
    calls = iter(["q"])
    assert _read_terminal_key_windows(lambda: next(calls)) == QUIT


def test_the_reader_pairs_the_other_prefix_code_as_well():
    calls = iter(["\x00", "G"])
    assert _read_terminal_key_windows(lambda: next(calls)) == HOME
