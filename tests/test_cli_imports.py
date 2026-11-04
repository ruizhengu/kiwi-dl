"""Smoke tests: every CLI module can be imported and the top-level group works."""

import sys

from click.testing import CliRunner

from kiwi_dl.cli import _redacted_argv, _startup_args_for_log, cli


def test_cli_help_runs():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "kiwi-dl" in result.output
    assert "Not affiliated with Mega Limited" in result.output


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0


def test_cli_subcommands_registered():
    """Downloads, link info, the proxy pool and config - nothing else."""
    result = CliRunner().invoke(cli, ["--help"])
    listing = result.output.split("Commands:", 1)[1].splitlines()
    commands = [line.split()[0] for line in listing if line.strip()]
    for cmd in ("download", "info", "proxy", "config"):
        assert cmd in commands, f"Missing command: {cmd}"
    for gone in ("upload", "stream", "ls", "account", "queue", "crypter", "watch", "share"):
        assert gone not in commands, f"Removed command is back: {gone}"


def test_proxy_subcommands_bring_your_own_proxies():
    result = CliRunner().invoke(cli, ["proxy", "--help"])
    assert result.exit_code == 0
    for sub in ("add", "remove", "list", "clear", "import", "test"):
        assert sub in result.output
    assert "fetch" not in result.output, "kiwi-dl ships no public proxy lists"
    assert "serve" not in result.output


def test_download_has_parallel_option():
    runner = CliRunner()
    result = runner.invoke(cli, ["download", "--help"])
    assert result.exit_code == 0
    assert "--parallel" in result.output or "-P" in result.output


def test_download_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["download", "--help"])
    assert result.exit_code == 0
    assert "--password" in result.output
    assert "--input-file" in result.output or "-i" in result.output


def test_info_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["info", "--help"])
    assert result.exit_code == 0
    assert "no account or mfa needed" in result.output.lower()


def test_startup_log_redacts_sensitive_arguments():
    args = ["download", "https://mega.nz/file/ABC#SECRET", "--password", "secret"]
    assert _redacted_argv(args) == ["download", "<redacted-link>", "--password", "<redacted>"]


def test_startup_args_handles_python_module_entrypoint(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            r"C:\project\src\kiwi_dl\__main__.py",
            "download",
            "https://mega.nz/file/ABC#SECRET",
        ],
    )

    assert _startup_args_for_log() == ["download", "<redacted-link>"]


# ---------------------------------------------------------------------------
# `kdl URL` is `kiwi-dl download URL`
# ---------------------------------------------------------------------------


def _record_downloads(monkeypatch, tmp_path):
    from kiwi_dl.core.downloader import DownloadResult, MegaDownloader

    calls: list[tuple[str, str]] = []

    def fake(self, url, output_dir, **kwargs):
        calls.append((url, str(output_dir)))
        path = tmp_path / "file.bin"
        path.write_bytes(b"x")
        return DownloadResult(path=path, size=1, elapsed_seconds=0.1, integrity_ok=True)

    monkeypatch.setattr(MegaDownloader, "download_link", fake)
    return calls


def test_kdl_downloads_a_link_into_the_output_dir(monkeypatch, tmp_path):
    from kiwi_dl.cli import kdl

    calls = _record_downloads(monkeypatch, tmp_path)
    out = tmp_path / "out"
    result = CliRunner().invoke(kdl, ["https://mega.nz/file/abc123#xyz", "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert calls == [("https://mega.nz/file/abc123#xyz", str(out))]


def test_kdl_takes_the_global_flags_anywhere(monkeypatch, tmp_path):
    from kiwi_dl.cli import kdl

    calls = _record_downloads(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        kdl, ["https://mega.nz/file/abc123#xyz", "-q", "-o", str(tmp_path), "-v"]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_kdl_without_a_link_is_a_usage_error():
    from kiwi_dl.cli import kdl

    result = CliRunner().invoke(kdl, [])
    assert result.exit_code == 2
    assert "No URLs given" in result.output


def test_kdl_reports_a_quota_stop_with_exit_code_3(monkeypatch, tmp_path):
    from kiwi_dl.cli import kdl
    from kiwi_dl.core.downloader import MegaDownloader
    from kiwi_dl.core.errors import TransferQuotaError

    def over_quota(self, url, output_dir, **kwargs):
        raise TransferQuotaError(time_left=60)

    monkeypatch.setattr(MegaDownloader, "download_link", over_quota)
    result = CliRunner().invoke(kdl, ["https://mega.nz/file/abc123#xyz", "-o", str(tmp_path)])
    assert result.exit_code == 3


def test_kdl_help_and_version():
    from kiwi_dl.cli import kdl

    help_text = CliRunner().invoke(kdl, ["--help"]).output
    assert "Usage: kdl" in help_text and "--output" in help_text
    assert "Not affiliated with Mega Limited" in help_text
    assert "0.1.0" in CliRunner().invoke(kdl, ["--version"]).output
