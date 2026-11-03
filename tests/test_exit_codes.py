"""Non-zero exit codes for failed transfers.

Historical bug: transfer helpers caught errors, printed a message, and
returned normally, so automation received exit code 0 after failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import kiwi_dl.commands.download_cmd as download_cmd_module
from kiwi_dl.cli import cli
from kiwi_dl.core.downloader import DownloadResult, MegaDownloader
from kiwi_dl.core.errors import TransferError

FILE_URL = "https://mega.nz/file/abc123#xyz"
FILE_URL_2 = "https://mega.nz/file/def456#uvw"


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIWI_DL_HOME", str(tmp_path / "user"))
    monkeypatch.setenv("KIWI_DL_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def test_single_failed_download_exits_nonzero(cli_env, monkeypatch):
    def boom(self, url, output_dir, **kwargs):
        raise TransferError(message="simulated failure")

    monkeypatch.setattr(MegaDownloader, "download_link", boom)
    runner = CliRunner()
    result = runner.invoke(cli, ["-q", "download", FILE_URL, "-o", str(cli_env / "out")])
    assert result.exit_code == 1


def test_mixed_batch_completes_then_exits_nonzero(cli_env, monkeypatch):
    calls: list[str] = []

    def flaky(self, url, output_dir, **kwargs):
        calls.append(url)
        if "abc123" in url:
            raise TransferError(message="simulated failure")
        path = Path(output_dir) / "ok.bin"
        path.write_bytes(b"x")
        return DownloadResult(path=path, size=1, elapsed_seconds=0.1, integrity_ok=True)

    monkeypatch.setattr(MegaDownloader, "download_link", flaky)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["-q", "download", FILE_URL, FILE_URL_2, "-P", "1", "-o", str(cli_env / "out")]
    )
    assert len(calls) == 2, "--batch must continue past the failed item"
    assert result.exit_code == 1


def test_fully_successful_download_exits_zero(cli_env, monkeypatch):
    def ok(self, url, output_dir, **kwargs):
        path = Path(output_dir) / "ok.bin"
        path.write_bytes(b"x")
        return DownloadResult(path=path, size=1, elapsed_seconds=0.1, integrity_ok=True)

    monkeypatch.setattr(MegaDownloader, "download_link", ok)
    runner = CliRunner()
    result = runner.invoke(cli, ["-q", "download", FILE_URL, "-o", str(cli_env / "out")])
    assert result.exit_code == 0


def test_invalid_link_counts_as_failure(cli_env):
    runner = CliRunner()
    result = runner.invoke(cli, ["-q", "download", "not-a-mega-link", "-o", str(cli_env / "o")])
    assert result.exit_code == 1


def test_selection_cancellation_is_a_skip_not_a_failure(cli_env, monkeypatch):
    from kiwi_dl.core.folder_downloader import MegaFolderDownloader
    from kiwi_dl.utils.selection import SelectionCancelled

    def cancelled(self, url, output_dir, **kwargs):
        raise SelectionCancelled()

    monkeypatch.setattr(MegaFolderDownloader, "download_folder", cancelled)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["-q", "download", "https://mega.nz/folder/abc123#xyz", "-o", str(cli_env / "out")],
    )
    assert result.exit_code == 0, result.output


def test_download_file_reports_failure_to_controller(cli_env, monkeypatch):
    """The shared controller marks the failed row, not just the exit code."""
    from kiwi_dl.ui.transfer_progress import TransferProgress

    def boom(self, url, output_dir, **kwargs):
        raise TransferError(message="nope")

    monkeypatch.setattr(MegaDownloader, "download_link", boom)
    tp = TransferProgress(title="t", quiet=True)
    ok = download_cmd_module._download_file(MegaDownloader(api=None), FILE_URL, cli_env, tp)
    assert ok is False
    assert list(tp.statuses().values()) == ["failed"]
