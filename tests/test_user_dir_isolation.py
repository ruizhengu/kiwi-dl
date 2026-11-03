"""Every test runs against its own kiwi-dl home, never the operator's.

`tests/conftest.py` points `KIWI_DL_HOME` at a per-test directory. Without it
a test that saves config or a proxy pool would write into the operator's real
`~/Library/Application Support/kiwi-dl` (or XDG directories) - silently
changing their proxy policy or download settings.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiwi_dl.config import config_file, data_dir, log_dir, user_dir


def _real_dirs() -> list[Path]:
    """The operator's actual kiwi-dl locations, from the real $HOME."""
    home = Path(os.path.expanduser("~"))
    return [
        home / "Library" / "Application Support" / "kiwi-dl",
        home / "Library" / "Logs" / "kiwi-dl",
        home / ".config" / "kiwi-dl",
        home / ".local" / "share" / "kiwi-dl",
        home / ".local" / "state" / "kiwi-dl",
    ]


def test_the_home_override_is_always_set_during_a_test():
    assert os.environ.get("KIWI_DL_HOME"), "conftest must isolate every test"


def test_every_resolved_path_stays_out_of_the_real_dirs():
    for resolver in (user_dir, data_dir, config_file, log_dir):
        resolved = resolver().resolve()
        for real in _real_dirs():
            assert resolved != real and real not in resolved.parents, resolver.__name__
