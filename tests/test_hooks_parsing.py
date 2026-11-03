"""Cross-platform post-transfer hook parsing and clipboard fallbacks."""

from __future__ import annotations

from pathlib import Path

import megabasterd_cli.utils.hooks as hooks_module
from megabasterd_cli.utils.hooks import parse_hook_command, run_post_transfer_command


def test_parse_hook_command_windows_keeps_backslashes(monkeypatch):
    monkeypatch.setattr(hooks_module.os, "name", "nt")
    argv = parse_hook_command(r'C:\Tools\notify.exe --path "C:\My Files"')
    assert argv[0] == r"C:\Tools\notify.exe"
    assert argv[-1] == r'"C:\My Files"'  # non-POSIX split keeps quotes intact


def test_parse_hook_command_posix_uses_posix_rules(monkeypatch):
    monkeypatch.setattr(hooks_module.os, "name", "posix")
    argv = parse_hook_command("/usr/bin/notify --msg 'hello world'")
    assert argv == ["/usr/bin/notify", "--msg", "hello world"]


def test_transferred_path_is_exactly_one_argv_item(monkeypatch):
    captured: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        captured.append(argv)
        return None

    monkeypatch.setattr(hooks_module.subprocess, "Popen", fake_popen)
    path = Path("C:/dir with space/file name.bin")
    run_post_transfer_command("tool --flag", path)
    assert captured[0][-1] == str(path)
    assert captured[0][:-1] == parse_hook_command("tool --flag")


def test_hook_arguments_are_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(hooks_module.subprocess, "Popen", lambda argv, **kw: None)
    with caplog.at_level("INFO", logger="megabasterd_cli.utils.hooks"):
        run_post_transfer_command("tool --token SUPERSECRET", Path("f.bin"))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "SUPERSECRET" not in joined


# ---------------------------------------------------------------------------
# Clipboard fallback (mb watch)
# ---------------------------------------------------------------------------


def _warm_platform_cache() -> None:
    # platform.system() lazily shells out via subprocess on first use; warm
    # its cache before check_output is monkeypatched.
    import platform

    platform.system()
