"""Ctrl-C stops a download within a second, keeps its progress, and exits 130.

The defect, found in a live run on macOS: SIGINT at ~100 MB of a 1.34 GB file,
and the process kept downloading for another 115 s, printed "Aborted!" and
exited 1. Every chunk is submitted to the pool up front, KeyboardInterrupt
reaches only the main thread, and leaving the `with ThreadPoolExecutor` joins
the pool - which runs every queued chunk first. The state written every 16
chunks also missed the last few, so a resume re-fetched them.

These drive the real chunk path against `FakeCdn` and deliver a real SIGINT.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import click
import pytest
import requests

from megabasterd_cli.core.downloader import MegaDownloader
from megabasterd_cli.core.errors import TransferCancelled
from megabasterd_cli.core.state import load_state
from tests.fake_mega import FakeCdn

KiB = 1024
MiB = 1024 * KiB


@pytest.fixture()
def sigint_raises():
    """Python's own SIGINT behaviour, whatever the test runner installed."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, previous)


def _run_in_thread(downloader, cdn, destination):
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = cdn.download_with(downloader, destination)
        except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
            outcome["exc"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for the download to start"
        time.sleep(0.01)


def test_ctrl_c_stops_the_download_within_a_second(tmp_path, monkeypatch, sigint_raises):
    cdn = FakeCdn(size=8 * MiB, block_delay=0.05)  # 12 chunks, ~3 s with two workers
    monkeypatch.setattr("requests.get", cdn.get)
    downloader = MegaDownloader(api=None, max_workers=2)
    fired: list[float] = []

    def interrupt_after_first_commit():
        _wait_for(lambda: downloader._chunks_done > 0)
        fired.append(time.monotonic())
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_after_first_commit, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        cdn.download_with(downloader, tmp_path / "file.bin")

    assert time.monotonic() - fired[0] < 1.0
    assert len(cdn.requests) < 12, "the queued chunks were fetched anyway"


def test_an_interrupted_download_resumes_from_every_committed_chunk(
    tmp_path, monkeypatch, sigint_raises
):
    cdn = FakeCdn(size=8 * MiB, block_delay=0.05)
    monkeypatch.setattr("requests.get", cdn.get)
    destination = tmp_path / "file.bin"
    downloader = MegaDownloader(api=None, max_workers=2)

    def interrupt_after_two_commits():
        _wait_for(lambda: downloader._chunks_done >= 2)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_after_two_commits, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        cdn.download_with(downloader, destination)

    # Fewer than 16 chunks committed, so the periodic save never ran: only
    # the save on the way out can have written this.
    state = load_state(destination)
    assert state is not None
    done = len(state.completed_chunks)
    assert 2 <= done < 12

    cdn.block_delay = 0
    before = len(cdn.requests)
    result = cdn.download_with(MegaDownloader(api=None, max_workers=2), destination)

    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    assert len(cdn.requests) - before == 12 - done, "a committed chunk was fetched again"


def test_a_stopped_download_does_not_sit_out_its_retry_backoff(tmp_path, monkeypatch):
    """The first retry waits 2 s and they grow to 30 s; stop must cut that short."""
    cdn = FakeCdn(size=256 * KiB)
    cdn.fail_with = lambda record: requests.ConnectionError("network down")
    monkeypatch.setattr("requests.get", cdn.get)
    downloader = MegaDownloader(api=None, max_workers=1)

    thread, outcome = _run_in_thread(downloader, cdn, tmp_path / "file.bin")
    _wait_for(lambda: cdn.requests)
    time.sleep(0.2)  # now inside the backoff after the first failure
    stopped_at = time.monotonic()
    downloader.stop()
    thread.join(5)

    assert not thread.is_alive()
    assert time.monotonic() - stopped_at < 1.0
    assert isinstance(outcome.get("exc"), TransferCancelled)
    assert len(cdn.requests) == 1, "an attempt was made after the stop"


def test_stop_all_stops_every_download_in_the_process(tmp_path, monkeypatch):
    """What a parallel `-P` or folder download needs: the files run on worker
    threads the interrupt never reaches."""
    cdn = FakeCdn(size=4 * MiB, block_delay=0.05)
    monkeypatch.setattr("requests.get", cdn.get)
    first = MegaDownloader(api=None, max_workers=2)
    second = MegaDownloader(api=None, max_workers=2)
    runs = [_run_in_thread(dl, cdn, tmp_path / f"{i}.bin") for i, dl in enumerate((first, second))]
    _wait_for(lambda: first._chunks_done > 0 and second._chunks_done > 0)

    stopped_at = time.monotonic()
    MegaDownloader.stop_all()
    for thread, _ in runs:
        thread.join(5)

    assert time.monotonic() - stopped_at < 1.0
    assert all(isinstance(outcome.get("exc"), TransferCancelled) for _, outcome in runs)


def test_ctrl_c_exits_130_after_stopping_the_downloads(monkeypatch):
    """Click turns KeyboardInterrupt into "Aborted!" and exit 1 on its own;
    the documented code for Ctrl-C is 130."""
    from megabasterd_cli import cli as cli_module

    stopped: list[bool] = []
    monkeypatch.setattr(MegaDownloader, "stop_all", classmethod(lambda cls: stopped.append(True)))

    @click.command()
    def interrupted():
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)  # the handler interrupts this

    monkeypatch.setattr(cli_module, "cli", interrupted)
    monkeypatch.setattr(sys, "argv", ["mb"])
    handler_before = signal.getsignal(signal.SIGINT)

    assert cli_module.main() == 130
    assert stopped == [True]
    assert signal.getsignal(signal.SIGINT) is handler_before


def test_a_plain_failure_still_exits_1(monkeypatch):
    from megabasterd_cli import cli as cli_module

    @click.command()
    def failing():
        raise click.exceptions.Exit(1)

    monkeypatch.setattr(cli_module, "cli", failing)
    monkeypatch.setattr(sys, "argv", ["mb"])
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 1
