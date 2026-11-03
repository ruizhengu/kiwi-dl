"""HTTP 509 on a chunk is the exit IP's transfer quota, not an expired URL.

Captured live on an anonymous download (Nov 2026): `509 Bandwidth Limit
Exceeded`, empty body, `X-MEGA-Time-Left: 20226`, `Connection: close`. A fresh
URL from the API came back refused the same way, even for a 1-byte range, and
the URL that got the 509 answered 403 from then on.

The engine put 509 in `URL_EXPIRY_STATUS`: it refreshed the URL seven times
with backoff, gave up after 128 s with a generic `transfer_failed`, and never
said when the quota would reset.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from kiwi_dl.cli import cli
from kiwi_dl.core.download_transport import URL_EXPIRY_STATUS, quota_time_left
from kiwi_dl.core.downloader import MegaDownloader
from kiwi_dl.core.errors import QuotaError, TransferQuotaError, format_time_left
from kiwi_dl.core.state import load_state
from kiwi_dl.ui.machine_output import error_code_for
from tests.fake_mega import FakeCdn

KiB = 1024


@pytest.fixture()
def cdn(monkeypatch):
    cdn = FakeCdn(size=3 * 1024 * KiB)  # 7 chunks: 128K, 256K, ... , 1M
    monkeypatch.setattr("requests.get", cdn.get)
    return cdn


def test_509_is_no_longer_treated_as_url_expiry():
    assert 509 not in URL_EXPIRY_STATUS
    assert {403, 410} <= URL_EXPIRY_STATUS


def test_a_509_raises_a_quota_error_carrying_the_reset_time(cdn, tmp_path):
    cdn.quota["direct"] = 0

    with pytest.raises(TransferQuotaError) as caught:
        cdn.download_with(MegaDownloader(api=None, max_workers=1), tmp_path / "f.bin")

    assert caught.value.time_left == 20226
    assert "resets in about 5h 37m" in str(caught.value)
    assert isinstance(caught.value, QuotaError)
    assert error_code_for(caught.value) == "quota_exceeded"


def test_a_509_is_neither_retried_nor_answered_with_a_url_refresh(cdn, tmp_path):
    cdn.quota["direct"] = 0

    with pytest.raises(TransferQuotaError):
        cdn.download_with(MegaDownloader(api=None, max_workers=1), tmp_path / "f.bin")

    assert [r["status"] for r in cdn.requests] == [509]
    assert cdn._urls == 1, "the URL was refreshed"


def test_the_chunks_before_the_509_are_kept_for_a_resume(cdn, tmp_path):
    destination = tmp_path / "f.bin"
    cdn.quota["direct"] = (128 + 256) * KiB  # exactly the first two chunks

    with pytest.raises(TransferQuotaError):
        cdn.download_with(MegaDownloader(api=None, max_workers=1), destination)

    assert sorted(load_state(destination).completed_chunks) == [0, 1]

    del cdn.quota["direct"]  # the quota came back
    before = len(cdn.requests)
    result = cdn.download_with(MegaDownloader(api=None, max_workers=2), destination)

    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    assert len(cdn.requests) - before == 5


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-MEGA-Time-Left": "20226"}, 20226),
        ({"X-MEGA-Time-Left": " 60 "}, 60),
        ({"X-MEGA-Time-Left": "0"}, 0),
        ({}, None),
        ({"X-MEGA-Time-Left": ""}, None),
        ({"X-MEGA-Time-Left": "soon"}, None),
        ({"X-MEGA-Time-Left": "-5"}, None),
        ({"X-MEGA-Time-Left": "²"}, None),  # isdigit() is True for it; int() is not
    ],
)
def test_the_reset_time_header_is_read_strictly(headers, expected):
    assert quota_time_left(headers) == expected


def test_without_a_reset_time_the_message_does_not_invent_one():
    assert str(TransferQuotaError()) == "MEGA transfer quota exceeded (HTTP 509)"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(20226, "5h 37m"), (3600, "1h 0m"), (125, "2m 5s"), (120, "2m"), (59, "59s"), (-3, "0s")],
)
def test_format_time_left(seconds, text):
    assert format_time_left(seconds) == text


def test_the_json_record_names_the_quota_and_its_reset_time(tmp_path, monkeypatch):
    monkeypatch.setenv("KIWI_DL_HOME", str(tmp_path / "user"))
    monkeypatch.setenv("KIWI_DL_LOG_DIR", str(tmp_path / "logs"))

    def over_quota(self, url, output_dir, **kwargs):
        raise TransferQuotaError(time_left=1800)

    monkeypatch.setattr(MegaDownloader, "download_link", over_quota)
    result = CliRunner().invoke(
        cli,
        ["download", "https://mega.nz/file/abc123#xyz", "-o", str(tmp_path / "out"), "--json"],
    )

    record = json.loads(result.stdout.splitlines()[0])
    assert record["error_code"] == "quota_exceeded"
    assert record["quota_resets_in_seconds"] == 1800
    assert "30m" in record["error"]


def test_a_folder_file_record_names_its_reset_time_too(tmp_path, monkeypatch):
    """Each file of a folder download gets its own record; a quota stop on one
    carries the reset time like the single-file record does."""
    from kiwi_dl.commands.download_support import _folder_progress_callbacks
    from kiwi_dl.ui.machine_output import MachineOutput

    class _Progress:
        def add_item(self, name, *args):
            return name

        def finish_item(self, *args):
            pass

    records: list[dict] = []
    machine = MachineOutput(True)
    monkeypatch.setattr(machine, "emit", lambda **record: records.append(record))
    _, _, on_file_failed = _folder_progress_callbacks(_Progress(), {}, machine, lambda p: p.name)

    on_file_failed(tmp_path / "a.mp4", TransferQuotaError(time_left=300))

    assert records[0]["error_code"] == "quota_exceeded"
    assert records[0]["quota_resets_in_seconds"] == 300
