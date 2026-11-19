"""An arrow-key menu for the terminal: ↑/↓ or j/k to move, Enter to choose.

Reading keys and drawing the list are separate so each can be tested without
a terminal: `select()` takes any `read_key` callable, and `render()` is a
pure function of the state. Keys come from termios on macOS and Linux, from
`msvcrt` on Windows; the drawing is the same ANSI on both.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from rich.console import Console
from rich.text import Text

from .theme import SYMBOL

if sys.platform != "win32":
    import select as _select
    import termios
    import tty

UP, DOWN, ENTER, QUIT, HOME, END = "up", "down", "enter", "quit", "home", "end"

HINT = "↑↓ move · ⏎ select · 1-9 jump · q back"
QUIT_HINT = "↑↓ move · ⏎ select · 1-9 jump · q quit"


@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    hint: str = ""


def render(choices: Sequence[Choice], index: int, hint: str = HINT) -> Text:
    """The list with the cursor on `index`, then the key hint."""
    label_width = max(len(choice.label) for choice in choices)
    text = Text()
    for position, choice in enumerate(choices):
        selected = position == index
        marker = f"{SYMBOL['group']} " if selected else "  "
        text.append(marker, style="kiwi.accent")
        text.append(choice.label.ljust(label_width), style="bold" if selected else "")
        if choice.hint:
            text.append(f"   {choice.hint}", style="kiwi.muted")
        text.append("\n")
    text.append("\n")
    text.append(hint, style="kiwi.muted")
    return text


def _normalize(raw: bytes) -> str:
    """One keypress, as `read_key` returns it, to a key name or a printable char."""
    if raw in (b"\r", b"\n"):
        return ENTER
    if raw in (b"\x1b[A", b"\x1bOA", b"k"):
        return UP
    if raw in (b"\x1b[B", b"\x1bOB", b"j"):
        return DOWN
    if raw in (b"\x1b[H", b"\x1bOH", b"\x1b[1~", b"g"):
        return HOME
    if raw in (b"\x1b[F", b"\x1bOF", b"\x1b[4~", b"G"):
        return END
    if raw in (b"q", b"Q", b"\x1b", b"\x03", b"\x04"):
        return QUIT
    return raw.decode("utf-8", "replace")


def _normalize_windows(raw: str) -> str:
    """One Windows console keypress, as `read_key` returns it, to a key name.

    `getwch` reports extended keys (arrows, Home, End) as a prefix-plus-code
    pair the reader has already joined; a lone prefix never reaches here.
    """
    if raw == "\r":
        return ENTER
    if raw in ("\x00H", "\xe0H", "k"):
        return UP
    if raw in ("\x00P", "\xe0P", "j"):
        return DOWN
    if raw in ("\x00G", "\xe0G", "g"):
        return HOME
    if raw in ("\x00O", "\xe0O", "G"):
        return END
    # `\x03` is only seen when processed input is off; normally the console
    # turns Ctrl-C into KeyboardInterrupt, which `select` treats as backing out.
    if raw in ("q", "Q", "\x1b", "\x03"):
        return QUIT
    return raw


@contextmanager
def _cbreak(fd: int) -> Iterator[None]:
    """Keys arrive one at a time, unechoed; the terminal is restored however we leave."""
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def read_terminal_key(fd: int | None = None) -> str:
    """Read one keypress from the terminal, folding escape sequences into one key."""
    if sys.platform == "win32":
        import msvcrt

        return _read_terminal_key_windows(msvcrt.getwch)
    fd = sys.stdin.fileno() if fd is None else fd
    with _cbreak(fd):
        raw = os.read(fd, 1)
        if raw == b"\x1b":
            # An arrow key is ESC [ X, sent at once; a lone ESC is nothing more.
            while _select.select([fd], [], [], 0.05)[0]:
                raw += os.read(fd, 1)
                if raw[-1:].isalpha() or raw[-1:] == b"~":
                    break
    return _normalize(raw)


def _read_terminal_key_windows(read_char: Callable[[], str]) -> str:
    """One keypress from the Windows console, folded into one key name.

    `getwch` needs no mode switch - it is already unechoed and unbuffered -
    and reports an extended key as two calls, prefix first.
    """
    first = read_char()
    if first in ("\x00", "\xe0"):
        return _normalize_windows(first + read_char())
    return _normalize_windows(first)


def _enable_windows_ansi() -> None:
    """Ask the Windows console to interpret the ANSI cursor codes the menu
    draws with.

    Best effort by design: Windows Terminal already accepts them, a console
    that refuses (or output that is not a console at all) just gets literal
    escape bytes, and nothing here is worth crashing the menu over.
    """
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def select(
    choices: Sequence[Choice],
    console: Console,
    read_key: Callable[[], str] = read_terminal_key,
    start: int = 0,
    hint: str = HINT,
) -> str | None:
    """Draw the menu and return the chosen value, or None when the user backs out.

    Ctrl-C backs out too: inside a menu it means "leave", not "crash".
    """
    index = max(0, min(start, len(choices) - 1))
    lines = len(choices) + 2  # the choices, a blank line, the hint
    _enable_windows_ansi()

    def draw() -> None:
        # The redraw climbs back exactly `lines` lines, so no line may wrap.
        console.print(render(choices, index, hint), no_wrap=True, overflow="ellipsis")

    console.file.write("\x1b[?25l")  # hide the cursor while the list is live
    try:
        draw()
        while True:
            try:
                key = read_key()
            except KeyboardInterrupt:
                return None
            if key == QUIT:
                return None
            if key == ENTER:
                return choices[index].value
            if key == UP:
                index = (index - 1) % len(choices)
            elif key == DOWN:
                index = (index + 1) % len(choices)
            elif key == HOME:
                index = 0
            elif key == END:
                index = len(choices) - 1
            elif key.isdecimal() and 1 <= int(key) <= len(choices):
                return choices[int(key) - 1].value
            else:
                continue
            # Back to the top of the list, clear it, draw it again.
            console.file.write(f"\x1b[{lines}F\x1b[J")
            draw()
    finally:
        console.file.write("\x1b[?25h")
        console.file.flush()
