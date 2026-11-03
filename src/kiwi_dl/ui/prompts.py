"""Status lines and prompts: one symbol per meaning, one prompt shape."""

from __future__ import annotations

from rich.text import Text

from .theme import SYMBOL, literal, make_console, styled_prompt

_console = make_console()


def _line(symbol: str, style: str, msg: str | Text) -> Text:
    """A coloured symbol, then the message in plain text, rendered verbatim.

    Only the symbol carries the colour: a whole line in green or red is louder
    than the news it brings. Callers routinely build `msg` as an f-string around
    server-supplied text, so it is escaped by default; pass `markup(...)` to opt
    a message into styling.
    """
    return Text().append(f"{symbol} ", style=style).append_text(literal(msg))


def print_success(msg: str | Text) -> None:
    _console.print(_line(SYMBOL["ok"], "kiwi.success", msg))


def print_error(msg: str | Text) -> None:
    _console.print(_line(SYMBOL["fail"], "kiwi.error", msg))


def print_warn(msg: str | Text) -> None:
    _console.print(_line(SYMBOL["warn"], "kiwi.warning", msg))


def print_info(msg: str | Text) -> None:
    _console.print(_line(SYMBOL["info"], "kiwi.info", msg))


def print_skip(msg: str | Text) -> None:
    """Something deliberately not done - a skipped folder, a declined prompt."""
    _console.print(_line(SYMBOL["skip"], "kiwi.muted", msg))


def ask(question: str, default: str | None = None) -> str:
    """`question [default]: `; Enter or end of input takes the default."""
    _console.print(styled_prompt(question, default), end="")
    try:
        answer = input("")
    except EOFError:
        return default or ""
    return answer.strip() or (default or "")


def ask_secret(question: str) -> str:
    """Like `ask`, but the answer is not echoed (a link password)."""
    from getpass import getpass

    _console.print(styled_prompt(question), end="")
    try:
        return getpass("").strip()
    except EOFError:
        return ""


_YES = ("y", "yes")
_NO = ("n", "no")


def confirm(question: str, default: bool = True) -> bool:
    """`question (y/n) [Y]: `.

    An unrecognised answer is RE-ASKED, not silently taken as "no": a character
    typed in the wrong keyboard layout must not quietly decline. Enter means the
    shown default, and so does end of input, so a piped caller cannot hang here.
    """
    while True:
        _console.print(styled_prompt(f"{question} (y/n)", "Y" if default else "N"), end="")
        try:
            answer = input("")
        except EOFError:
            return default
        trimmed = answer.strip().lower()
        if not trimmed:
            return default
        if trimmed in _YES:
            return True
        if trimmed in _NO:
            return False
        print_warn(f"Please answer y or n (or press Enter for {'yes' if default else 'no'}).")


def confirmed(question: str, default: bool = False) -> bool:
    """`confirm`, but a "no" says so: a declined action must not look like a done one."""
    if confirm(question, default=default):
        return True
    print_skip("Cancelled; nothing was changed.")
    return False
