"""The one look for everything kiwi-dl prints: a few calm colours, a few symbols."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

PALETTE = {
    "accent": "#5FD7FF",
    "success": "#5FD787",
    "warning": "#FFD75F",
    "error": "#FF5F5F",
    "muted": "#8A8A8A",
    "path": "#AFD7AF",
    "value": "#87D7D7",
}

THEME = Theme(
    {
        "kiwi.title": f"bold {PALETTE['accent']}",
        "kiwi.accent": PALETTE["accent"],
        "kiwi.success": f"bold {PALETTE['success']}",
        "kiwi.warning": f"bold {PALETTE['warning']}",
        "kiwi.error": f"bold {PALETTE['error']}",
        "kiwi.info": PALETTE["accent"],
        "kiwi.muted": PALETTE["muted"],
        "kiwi.path": PALETTE["path"],
        "kiwi.value": PALETTE["value"],
        "kiwi.option": PALETTE["warning"],
        "kiwi.default": PALETTE["success"],
        "kiwi.table.header": f"bold {PALETTE['accent']}",
        "kiwi.table.border": PALETTE["muted"],
    }
)

# One meaning per symbol, everywhere: a status line, a progress row and the
# menu all use the same glyph for the same state.
SYMBOL = {
    "ok": "✓",
    "fail": "✗",
    "warn": "!",
    "info": "➜",
    "skip": "◎",
    "group": "➤",
    "context": "⚙",
    "active": "↓",
    "queued": "·",
}


def make_console(**kwargs) -> Console:
    """Create a Console that uses the shared project theme."""
    return Console(theme=THEME, **kwargs)


# ---------------------------------------------------------------------------
# Trusted / untrusted boundary
#
# Rich parses plain `str` as markup, so any remote-controlled value (a MEGA
# filename, a server-supplied error string) could choose its own styling, or
# abort the whole render with `MarkupError` on an unbalanced tag. The rule
# enforced here: a plain `str` is UNTRUSTED and is shown literally; app-authored
# styling must be opted in via `markup()`.
# ---------------------------------------------------------------------------


def markup(text: str) -> Text:
    """Mark app-authored text as trusted Rich markup (`[kiwi.muted]...[/kiwi.muted]`)."""
    return Text.from_markup(text)


def literal(text: object) -> Text:
    """Wrap an untrusted value so Rich renders it verbatim."""
    return text if isinstance(text, Text) else Text(str(text))


class SafeTable(Table):
    """`rich.table.Table` that treats plain `str` cells as untrusted literal text.

    Use `markup("[kiwi.success]Y[/kiwi.success]")` for deliberate in-cell styling;
    column-level `style=` is unaffected and remains the normal way to colour a
    column.
    """

    def add_row(self, *renderables: Any, **kwargs: Any) -> None:
        super().add_row(
            *(Text(cell) if type(cell) is str else cell for cell in renderables),
            **kwargs,
        )


def styled_prompt(question: str, default: str | None = None) -> Text:
    """`question [default]: ` with only the default coloured.

    The brackets stay plain punctuation; the value inside them is the one thing
    the reader can accept by pressing Enter, so it alone carries the colour.
    """
    styled = Text()
    styled.append(question, style="bold")
    if default:
        styled.append(" [")
        styled.append(default, style="kiwi.default")
        styled.append("]")
    styled.append(": ")
    return styled
