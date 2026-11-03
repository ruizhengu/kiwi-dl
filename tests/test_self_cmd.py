"""`kiwi-dl update`, `remove` and `completion`.

No package manager is ever run: `subprocess.call` is replaced, and the install
kind is read from a temporary prefix the test lays out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from kiwi_dl.cli import cli
from kiwi_dl.commands import self_cmd
from kiwi_dl.commands.self_cmd import Install, detect_install

GIT = {
    "url": "https://github.com/ruizhengu/kiwi-dl",
    "vcs_info": {"vcs": "git", "requested_revision": "main", "commit_id": "abc"},
}


@pytest.fixture
def calls(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(self_cmd.subprocess, "call", lambda cmd: seen.append(cmd) or 0)
    return seen


def _as(monkeypatch, install: Install) -> None:
    monkeypatch.setattr(self_cmd, "detect_install", lambda: install)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "manager"), [("uv-receipt.toml", "uv"), ("pipx_metadata.json", "pipx")]
)
def test_the_tool_that_installed_kiwi_dl_is_read_from_its_marker(tmp_path, marker, manager):
    (tmp_path / marker).write_text("")
    install = detect_install(prefix=tmp_path, direct=GIT)
    assert install.manager == manager
    assert install.source == "git+https://github.com/ruizhengu/kiwi-dl@main"


def test_a_local_directory_install_reinstalls_from_that_directory(tmp_path):
    (tmp_path / "uv-receipt.toml").write_text("")
    install = detect_install(prefix=tmp_path, direct={"url": "file:///src/kiwi-dl", "dir_info": {}})
    assert install.source == "/src/kiwi-dl"


def test_an_editable_checkout_and_a_plain_pip_install_are_told_apart(tmp_path):
    editable = {"url": "file:///src/kiwi-dl", "dir_info": {"editable": True}}
    assert detect_install(prefix=tmp_path, direct=editable).manager == "editable"
    assert detect_install(prefix=tmp_path, direct={}).manager == "pip"
    assert detect_install(prefix=tmp_path, direct={}).source == self_cmd.DEFAULT_SOURCE


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------


def test_update_reinstalls_from_the_same_source_with_the_same_tool(monkeypatch, calls):
    _as(monkeypatch, Install("uv", "git+https://x/kiwi-dl@main", Path("/p")))
    result = CliRunner().invoke(cli, ["update"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ["uv", "tool", "install", "--force", "--reinstall", "git+https://x/kiwi-dl@main"]
    ]


def test_update_dry_run_changes_nothing(monkeypatch, calls):
    _as(monkeypatch, Install("pipx", "git+https://x/kiwi-dl@main", Path("/p")))
    result = CliRunner().invoke(cli, ["update", "--dry-run"])
    assert result.exit_code == 0
    assert calls == []
    assert "pipx install --force" in result.output


def test_a_source_checkout_is_told_to_use_git_instead(monkeypatch, calls):
    _as(monkeypatch, Install("editable", "/src/kiwi-dl", Path("/p")))
    result = CliRunner().invoke(cli, ["update"])
    assert calls == []
    assert "git pull" in result.output


def test_a_failed_update_exits_with_the_tools_status(monkeypatch):
    _as(monkeypatch, Install("uv", "git+https://x/kiwi-dl@main", Path("/p")))
    monkeypatch.setattr(self_cmd.subprocess, "call", lambda cmd: 7)
    assert CliRunner().invoke(cli, ["update"]).exit_code == 7


# --------------------------------------------------------------------------
# remove
# --------------------------------------------------------------------------


def _with_data(home: Path) -> None:
    for name in ("Config", "Data", "Logs"):
        (home / name).mkdir(parents=True, exist_ok=True)
        (home / name / "file").write_text("x")


def test_remove_without_purge_keeps_settings_and_uninstalls(monkeypatch, calls, tmp_path):
    _as(monkeypatch, Install("uv", "git+https://x/kiwi-dl@main", Path("/p")))
    home = Path(self_cmd.config_dir()).parent
    _with_data(home)
    result = CliRunner().invoke(cli, ["remove", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "uninstall", "kiwi-dl"]]
    assert (home / "Config" / "file").exists()


def test_remove_purge_deletes_only_kiwi_dls_own_folders(monkeypatch, calls):
    _as(monkeypatch, Install("pipx", "git+https://x/kiwi-dl@main", Path("/p")))
    home = Path(self_cmd.config_dir()).parent  # KIWI_DL_HOME, set per test by conftest
    _with_data(home)
    (home / "my-downloads").mkdir()
    result = CliRunner().invoke(cli, ["remove", "--purge", "--yes"])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in home.iterdir()) == ["my-downloads"]
    assert calls == [["pipx", "uninstall", "kiwi-dl"]]


def test_remove_dry_run_lists_and_keeps_everything(monkeypatch, calls):
    _as(monkeypatch, Install("uv", "git+https://x/kiwi-dl@main", Path("/p")))
    home = Path(self_cmd.config_dir()).parent
    _with_data(home)
    result = CliRunner().invoke(cli, ["remove", "--purge", "--dry-run"])
    assert result.exit_code == 0
    assert calls == []
    assert (home / "Config" / "file").exists()
    assert "Config" in result.output


def test_remove_refuses_without_a_terminal_or_yes(monkeypatch, calls):
    _as(monkeypatch, Install("uv", "git+https://x/kiwi-dl@main", Path("/p")))
    result = CliRunner().invoke(cli, ["remove"])
    assert result.exit_code == 1
    assert calls == []
    assert "--yes" in result.output


def test_purge_never_targets_the_home_directory_or_a_foreign_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("KIWI_DL_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    foreign = tmp_path / "Documents"
    foreign.mkdir()
    monkeypatch.setattr(self_cmd, "config_dir", lambda: tmp_path)  # $HOME itself
    monkeypatch.setattr(self_cmd, "data_dir", lambda: foreign)  # not kiwi-dl's
    monkeypatch.setattr(self_cmd, "log_dir", lambda: Path("/"))
    assert self_cmd.purge_targets() == []


@pytest.mark.parametrize("pointed_at", ["home", "a parent of home", "root"])
def test_even_a_kiwi_dl_home_override_cannot_aim_purge_at_home_or_above(
    monkeypatch, tmp_path, pointed_at
):
    """KIWI_DL_HOME lifts the "must be kiwi-dl's folder" rule, so these three
    checks are the only thing between a typo in it and deleting $HOME."""
    home = tmp_path / "home" / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    target = {"home": home, "a parent of home": home.parent, "root": Path("/")}[pointed_at]
    for name in ("config_dir", "data_dir", "log_dir"):
        monkeypatch.setattr(self_cmd, name, lambda target=target: target)
    assert self_cmd.purge_targets() == []


# --------------------------------------------------------------------------
# completion
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "marker"),
    [("zsh", "#compdef kiwi-dl"), ("bash", "_KIWI_DL_COMPLETE"), ("fish", "_KIWI_DL_COMPLETE")],
)
def test_completion_prints_a_script_for_each_shell(shell, marker):
    result = CliRunner().invoke(cli, ["completion", shell])
    assert result.exit_code == 0
    assert marker in result.output


def test_completion_defaults_to_the_login_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert "#compdef kiwi-dl" in CliRunner().invoke(cli, ["completion"]).output


def test_completion_actually_completes_subcommands(monkeypatch):
    monkeypatch.setenv("_KIWI_DL_COMPLETE", "bash_complete")
    monkeypatch.setenv("COMP_WORDS", "kiwi-dl up")
    monkeypatch.setenv("COMP_CWORD", "1")
    result = CliRunner().invoke(cli, [], prog_name="kiwi-dl", standalone_mode=True)
    assert "update" in result.output
