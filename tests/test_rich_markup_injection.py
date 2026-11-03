"""Regression tests: untrusted remote text must never be parsed as Rich markup.

MEGA filenames, proxy URLs and server-supplied error strings all reach a Rich
console with markup enabled. Rich would either apply attacker-chosen
styling or raise `MarkupError` on an unbalanced tag and abort the whole listing.
"""

from __future__ import annotations

import pytest

from kiwi_dl.ui import prompts
from kiwi_dl.ui.theme import make_console


@pytest.fixture
def wide_console(monkeypatch):
    """Give the module consoles a wide, non-wrapping capture surface."""
    consoles = {}

    def patch(module):
        console = make_console(width=200)
        monkeypatch.setattr(module, "_console", console)
        consoles[module] = console
        return console

    patch(prompts)
    return consoles


def _capture(console, fn, *args, **kwargs) -> str:
    with console.capture() as cap:
        fn(*args, **kwargs)
    return cap.get()


# --------------------------------------------------------------------------
# Untrusted values must be shown literally
# --------------------------------------------------------------------------


def test_server_error_text_with_markup_is_neutralised(wide_console):
    exc = RuntimeError("boom [bold red]pwned[/bold red] [/]")
    out = _capture(prompts._console, prompts.print_error, f"Login failed: {exc}")
    assert "[bold red]pwned[/bold red]" in out


def test_error_text_with_lone_bracket_does_not_raise(wide_console):
    out = _capture(prompts._console, prompts.print_error, "Lookup failed: bad token [oops")
    assert "[oops" in out


def test_untrusted_table_cell_via_safe_table(wide_console):
    from kiwi_dl.ui.theme import SafeTable

    table = SafeTable(show_header=False)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Name", "[bold red]evil[/bold red]")
    table.add_row("Handle", "abc[def")
    out = _capture(prompts._console, prompts._console.print, table)
    assert "[bold red]evil[/bold red]" in out
    assert "abc[def" in out


# --------------------------------------------------------------------------
# The app's own deliberate styling must keep working
# --------------------------------------------------------------------------


def test_print_helper_prefix_still_renders(wide_console):
    out = _capture(prompts._console, prompts.print_success, "done")
    assert "OK" in out and "done" in out
    assert "[mb.success]" not in out
