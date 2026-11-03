"""Shared test setup.

The only thing here is a containment guard: without it any test that saves
config or a proxy pool writes into the OPERATOR'S kiwi-dl directories
(`~/Library/Application Support/kiwi-dl`, or the XDG ones), silently changing
their proxy policy or download settings.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_dir(tmp_path_factory, monkeypatch):
    """Point every path resolver at a per-test directory.

    Autouse and unconditional on purpose: an opt-in guard is one a new test can
    forget, which is exactly how this escaped. A test that wants its own
    location still just calls `monkeypatch.setenv` itself - it runs after this
    fixture and wins.
    """
    root = tmp_path_factory.mktemp("kiwi-dl-home")
    monkeypatch.setenv("KIWI_DL_HOME", str(root))
    monkeypatch.setenv("KIWI_DL_LOG_DIR", str(root / "Logs"))
    return root
