import os
import sys
from pathlib import Path

import pytest

from kiwi_dl.config import (
    ConfigStore,
    config_file,
    data_dir,
    default_download_dir,
    log_dir,
    user_dir,
)


@pytest.fixture()
def no_home_override(monkeypatch, tmp_path):
    """Undo the suite-wide KIWI_DL_HOME so the platform rules apply, with a
    throwaway $HOME and no XDG variables."""
    monkeypatch.delenv("KIWI_DL_HOME", raising=False)
    monkeypatch.delenv("KIWI_DL_LOG_DIR", raising=False)
    for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(variable, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_macos_keeps_state_in_library(monkeypatch, no_home_override):
    monkeypatch.setattr(sys, "platform", "darwin")
    support = no_home_override / "Library" / "Application Support" / "kiwi-dl"

    assert config_file() == support / "config.json"
    assert data_dir() == support
    assert log_dir() == no_home_override / "Library" / "Logs" / "kiwi-dl"


@pytest.mark.skipif(os.name == "nt", reason="XDG applies to POSIX")
def test_linux_follows_the_xdg_defaults(monkeypatch, no_home_override):
    monkeypatch.setattr(sys, "platform", "linux")

    assert config_file() == no_home_override / ".config" / "kiwi-dl" / "config.json"
    assert data_dir() == no_home_override / ".local" / "share" / "kiwi-dl"
    assert log_dir() == no_home_override / ".local" / "state" / "kiwi-dl" / "logs"


@pytest.mark.skipif(os.name == "nt", reason="XDG applies to POSIX")
def test_linux_honours_xdg_variables_but_not_relative_ones(monkeypatch, no_home_override, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data")  # the spec says: ignore

    assert config_file() == tmp_path / "cfg" / "kiwi-dl" / "config.json"
    assert data_dir() == no_home_override / ".local" / "share" / "kiwi-dl"


def test_kiwi_dl_home_keeps_everything_in_one_place(monkeypatch, tmp_path: Path):
    home = tmp_path / "portable"
    monkeypatch.setenv("KIWI_DL_HOME", str(home))
    monkeypatch.delenv("KIWI_DL_LOG_DIR", raising=False)

    assert user_dir() == home
    assert config_file() == home / "Config" / "config.json"
    assert data_dir() == home / "Data"
    assert log_dir() == home / "Logs"


def test_nothing_is_written_to_the_directory_it_runs_from(monkeypatch, no_home_override, tmp_path):
    """The bug: an installed CLI fell back to the current directory, so every
    folder it ran in grew a `User/` (config, proxies, logs) and an `Output/`."""
    from click.testing import CliRunner

    from kiwi_dl.cli import cli

    monkeypatch.setattr(sys, "platform", "darwin")
    workdir = tmp_path / "somewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    runner = CliRunner()
    assert runner.invoke(cli, ["config", "set", "max_workers", "4"]).exit_code == 0
    assert runner.invoke(cli, ["proxy", "add", "http://proxy.invalid:8080"]).exit_code == 0

    assert list(workdir.iterdir()) == []
    support = no_home_override / "Library" / "Application Support" / "kiwi-dl"
    assert (support / "config.json").is_file()
    assert (support / "proxies.json").is_file()


def test_downloads_default_to_the_current_directory(monkeypatch, tmp_path: Path):
    from click.testing import CliRunner

    from kiwi_dl.cli import cli
    from kiwi_dl.core.downloader import DownloadResult, MegaDownloader

    seen: list[Path] = []

    def fake(self, url, output_dir, **kwargs):
        seen.append(Path(output_dir))
        target = Path(output_dir) / "f.bin"
        target.write_bytes(b"x")
        return DownloadResult(path=target, size=1, elapsed_seconds=0.1, integrity_ok=True)

    monkeypatch.setattr(MegaDownloader, "download_link", fake)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["download", "https://mega.nz/file/abc123#xyz"])

    assert result.exit_code == 0, result.output
    assert seen == [tmp_path]
    assert default_download_dir() == tmp_path


def test_a_saved_config_does_not_pin_the_directory_it_was_saved_from(tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    store.set("max_workers", "4")

    assert ConfigStore(tmp_path / "config.json").load().download_path == ""


def test_logging_defaults_are_enabled(tmp_path: Path):
    cfg = ConfigStore(tmp_path / "missing.json").load()

    assert cfg.max_workers == 8
    assert cfg.max_parallel_downloads == 6
    assert cfg.log_level == "WARNING"
    assert cfg.log_to_file is True
    assert cfg.log_max_bytes > 0
    assert cfg.log_backups >= 1


def test_bool_config_values_are_strict(tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")

    store.set("verify_integrity", "no")
    assert store.config.verify_integrity is False

    with pytest.raises(ValueError):
        store.set("verify_integrity", "yse")
