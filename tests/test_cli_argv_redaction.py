"""Startup-args redaction in CLI logs: passwords and link keys never reach the log."""

from kiwi_dl.cli import _redacted_argv


def test_link_password_value_is_redacted():
    out = _redacted_argv(["download", "-p", "linkpw", "--password", "linkpw2"])
    assert "linkpw" not in " ".join(out)
    assert out.count("<redacted>") == 2


def test_inline_password_form_is_redacted():
    out = _redacted_argv(["download", "--password=s3cret"])
    assert "s3cret" not in " ".join(out)
    assert "--password=<redacted>" in out


def test_mega_link_redacted_and_plain_args_preserved():
    out = _redacted_argv(["download", "https://mega.nz/folder/ID#KEY", "-o", "out"])
    assert "<redacted-link>" in out
    assert "ID#KEY" not in " ".join(out)
    assert out[-2:] == ["-o", "out"]
