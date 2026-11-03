"""Small shared test helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX file modes are not a Windows concept"
)
windows_only = pytest.mark.skipif(os.name != "nt", reason="ACL check is Windows-specific")

OWNER_ONLY = 0o600


def mode(path: Path) -> int:
    """POSIX permission bits, via lstat so a symlink is not followed."""
    return stat.S_IMODE(path.lstat().st_mode)
