"""Smoke tests: every CLI module can be imported and the top-level group works."""

import sys

from click.testing import CliRunner

from megabasterd_cli.cli import _redacted_argv, _startup_args_for_log, cli


def test_cli_help_runs():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "MegaBasterd CLI" in result.output


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
            r"C:\project\src\megabasterd_cli\__main__.py",
            "download",
            "https://mega.nz/file/ABC#SECRET",
        ],
    )

    assert _startup_args_for_log() == ["download", "<redacted-link>"]
