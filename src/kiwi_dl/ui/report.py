"""The frame around a command's output: a title with a context line, and a summary.

Download
⚙ Apple Silicon · macOS 26.6 · 219.0 GB free · ~/Downloads
...
==================================================
✓ Download complete
  Downloaded   2.1 GB · 12 files
  Saved to     ~/Downloads
  Free space   216.9 GB
==================================================
"""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ..utils.helpers import format_bytes
from .theme import SYMBOL, literal

RULE_WIDTH = 50


def short_path(path: Path | str) -> str:
    """`/Users/me/Downloads` as `~/Downloads`, the way a person would type it."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~" + text[len(home) :]
    return text


def machine_name() -> str:
    """`Apple Silicon`, `Intel Mac` or `Linux x86_64`."""
    machine = platform.machine()
    if platform.system() == "Darwin":
        return "Apple Silicon" if machine == "arm64" else "Intel Mac"
    return f"{platform.system()} {machine}".strip()


def os_name() -> str:
    """`macOS 26.6`, or the distribution's own name on Linux (`Ubuntu 24.04 LTS`)."""
    if platform.system() == "Darwin":
        version = platform.mac_ver()[0]
        return f"macOS {version}" if version else "macOS"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def free_space(path: Path) -> int | None:
    """Free bytes on the volume that holds `path`, or None if it cannot be read.

    Walks up to the nearest existing directory, so it answers for a download
    folder that has not been created yet.
    """
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None


def context_line(destination: Path | None = None) -> str:
    """`Apple Silicon · macOS 26.6 · 219.0 GB free · ~/Downloads`."""
    parts = [machine_name(), os_name()]
    if destination is not None:
        free = free_space(destination)
        if free is not None:
            parts.append(f"{format_bytes(free)} free")
        parts.append(short_path(destination))
    return " · ".join(parts)


def header(console: Console, title: str, destination: Path | None = None) -> None:
    """The bold title and the `⚙` line under it that says where things stand."""
    console.print(Text(title, style="kiwi.title"))
    console.print(Text(f"{SYMBOL['context']} {context_line(destination)}", style="kiwi.muted"))
    console.print()


def summary(console: Console, title: str, rows: list[tuple[str, str]], ok: bool = True) -> None:
    """A framed closing block: what happened, in a few label/value rows."""
    rule = Text("=" * RULE_WIDTH, style="kiwi.muted")
    label_width = max((len(label) for label, _ in rows), default=0)
    console.print()
    console.print(rule)
    symbol, style = (SYMBOL["ok"], "kiwi.success") if ok else (SYMBOL["fail"], "kiwi.error")
    console.print(Text(f"{symbol} {title}", style=style))
    for label, value in rows:
        line = Text(f"  {label.ljust(label_width)}   ", style="kiwi.muted")
        console.print(line.append_text(literal(value)))
    console.print(rule)
