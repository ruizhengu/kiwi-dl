"""Small shared test helpers."""

from __future__ import annotations

import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

OWNER_ONLY = 0o600


def mode(path: Path) -> int:
    """POSIX permission bits, via lstat so a symlink is not followed."""
    return stat.S_IMODE(path.lstat().st_mode)
