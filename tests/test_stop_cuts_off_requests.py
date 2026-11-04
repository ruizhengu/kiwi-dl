"""A stop cuts off the chunk requests on the wire, well within a second.

Live, through slow public proxies, Ctrl-C took 8.6 s to stop. Every worker sat
in a socket read, which a `requests` timeout bounds per read (15 s through a
pool proxy), and the stop event was only checked between reads. `stop` now
shuts down the socket under every response being read, and stops waiting for
responses that have not arrived yet. Neither is held against the proxy: it is
not banned or set aside, and nothing is logged as failed.

`FakeCdn`'s slow body waits like a socket read: `close()` does not end it, a
shutdown does. The last tests check the same mechanism against real sockets.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import pytest
import requests

from kiwi_dl.core.downloader import MegaDownloader
from kiwi_dl.core.errors import TransferCancelled
from kiwi_dl.core.inflight import InFlightRequests
from kiwi_dl.core.state import load_state
from kiwi_dl.proxy.quota_router import SmartProxyPolicy
from kiwi_dl.proxy.smart_proxy import SmartProxyPool
from tests.fake_mega import FakeCdn

KiB = 1024
MiB = 1024 * KiB
PROXY = "http://proxy.invalid:8080"
FIRST_CHUNK = 128 * KiB  # MEGA's first chunk size; the tests slow down what follows

# "Well within a second": a cut-off download returns in tens of milliseconds.
PROMPT = 0.5


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


def _start(cdn, tmp_path):
    """A two-worker download through one pool proxy, on a thread of its own."""
    pool = SmartProxyPool([PROXY])
    dl = MegaDownloader(api=None, max_workers=2, smart_proxy=SmartProxyPolicy(pool, mode="always"))
    destination = tmp_path / "file.bin"
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = cdn.download_with(dl, destination)
        except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
            outcome["exc"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return dl, pool, destination, thread, outcome


def _seconds_to_stop(dl, thread) -> float:
    stopped_at = time.monotonic()
    dl.stop()  # what Ctrl-C does, through `stop_all`
    thread.join(5)
    assert not thread.is_alive()
    return time.monotonic() - stopped_at


def _slow(cdn) -> list[dict]:
    return [r for r in cdn.requests if r["start"] >= FIRST_CHUNK]


def _assert_held_nothing_against(pool, caplog):
    (entry,) = pool.list()
    assert pool.is_usable(PROXY)
    assert entry.failures == 0
    blamed = [
        r.getMessage()
        for r in caplog.records
        if any(word in r.getMessage() for word in ("banned", "set aside", "failed"))
    ]
    assert blamed == []
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_stop_mid_body_returns_at_once_and_keeps_the_resume_state(tmp_path, monkeypatch, caplog):
    cdn = FakeCdn(size=3 * MiB, block_delay=60)  # a proxy that has stopped sending
    cdn.slow_from = FIRST_CHUNK
    monkeypatch.setattr("requests.get", cdn.get)
    caplog.set_level(logging.DEBUG, logger="kiwi_dl")

    dl, pool, destination, thread, outcome = _start(cdn, tmp_path)
    # The first chunk lands; the next two are stuck in their bodies.
    _wait_for(lambda: dl._chunks_done == 1 and sum(bool(r.get("reading")) for r in _slow(cdn)) == 2)

    assert _seconds_to_stop(dl, thread) < PROMPT
    assert isinstance(outcome.get("exc"), TransferCancelled)
    assert sorted(load_state(destination).completed_chunks) == [0]
    assert all(r.get("closed") for r in _slow(cdn))
    _assert_held_nothing_against(pool, caplog)

    # And the same command picks up where it stopped.
    cdn.block_delay = 0
    before = len(cdn.requests)
    result = cdn.download_with(MegaDownloader(api=None, max_workers=2), destination)
    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    assert all(r["start"] >= FIRST_CHUNK for r in cdn.requests[before:])


def test_a_stop_while_waiting_for_the_response_returns_at_once(tmp_path, monkeypatch, caplog):
    cdn = FakeCdn(size=3 * MiB)
    answer = threading.Event()

    def no_answer_after_the_first_chunk(record):
        if record["start"] >= FIRST_CHUNK:
            answer.wait(30)  # a proxy that took the request and says nothing

    cdn.fail_with = no_answer_after_the_first_chunk
    monkeypatch.setattr("requests.get", cdn.get)
    caplog.set_level(logging.DEBUG, logger="kiwi_dl")

    dl, pool, destination, thread, outcome = _start(cdn, tmp_path)
    try:
        _wait_for(lambda: dl._chunks_done == 1 and len(_slow(cdn)) == 2)

        assert _seconds_to_stop(dl, thread) < PROMPT
        assert isinstance(outcome.get("exc"), TransferCancelled)
        assert sorted(load_state(destination).completed_chunks) == [0]
        _assert_held_nothing_against(pool, caplog)
    finally:
        answer.set()
    # A response that turns up after the stop is closed, not left open.
    _wait_for(lambda: all(r.get("closed") for r in _slow(cdn)))
    assert len(_slow(cdn)) == 2, "a request was made after the stop"


# ---------------------------------------------------------------------------
# Real sockets: the fake's model of a blocked read, checked
# ---------------------------------------------------------------------------


class _Stalling:
    """A loopback HTTP server, or proxy, that stops sending partway.

    With `headers`, it answers 206 and sends the first 64 KiB block of a 1 MiB
    body and a little more; without, it reads the request and says nothing.
    """

    def __init__(self, *, headers: bool):
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.accepted = threading.Event()
        self._release = threading.Event()
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._serve, args=(headers,), daemon=True).start()

    def _serve(self, headers: bool) -> None:
        conn, _ = self._listener.accept()
        self._conns.append(conn)
        conn.recv(65536)
        self.accepted.set()
        if headers:
            head = b"HTTP/1.1 206 Partial Content\r\nContent-Length: 1048576\r\n\r\n"
            conn.sendall(head + bytes(64 * KiB + KiB))
        self._release.wait(30)

    def close(self) -> None:
        self._release.set()
        for conn in self._conns:
            conn.close()
        self._listener.close()


@pytest.fixture()
def stalling():
    servers: list[_Stalling] = []

    def make(*, headers: bool) -> _Stalling:
        servers.append(_Stalling(headers=headers))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _target(server: _Stalling, via_proxy: bool) -> tuple[str, dict | None]:
    if via_proxy:
        # A CDN URL is plain http, so a pool proxy relays it as an absolute URI.
        return "http://cdn.invalid/dl/x", {"http": f"http://127.0.0.1:{server.port}"}
    return f"http://127.0.0.1:{server.port}/dl/x", None


@pytest.mark.parametrize("via_proxy", [False, True], ids=["direct", "via-proxy"])
def test_a_real_read_blocked_mid_body_ends_at_once(stalling, via_proxy):
    server = stalling(headers=True)
    url, proxies = _target(server, via_proxy)
    stop = threading.Event()
    inflight = InFlightRequests(stop)
    reading = threading.Event()
    outcome: dict = {}

    def worker():
        try:
            with inflight.get(url, stream=True, timeout=(5, 15), proxies=proxies) as resp:
                assert resp is not None
                for _block in resp.iter_content(64 * KiB):
                    reading.set()
        except requests.RequestException as exc:
            outcome["exc"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert reading.wait(5)
    time.sleep(0.1)  # into the read for the second block, which never comes

    stopped_at = time.monotonic()
    stop.set()
    inflight.cut_off()
    thread.join(5)

    assert time.monotonic() - stopped_at < PROMPT
    assert isinstance(outcome.get("exc"), requests.exceptions.ChunkedEncodingError)


@pytest.mark.parametrize("via_proxy", [False, True], ids=["direct", "via-proxy"])
def test_a_real_request_with_no_answer_yet_is_let_go_at_once(stalling, via_proxy):
    server = stalling(headers=False)
    url, proxies = _target(server, via_proxy)
    stop = threading.Event()
    inflight = InFlightRequests(stop)
    outcome: dict = {}

    def worker():
        with inflight.get(url, stream=True, timeout=(5, 15), proxies=proxies) as resp:
            outcome["resp"] = resp

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert server.accepted.wait(5)
    time.sleep(0.1)  # waiting for a status line that never comes

    stopped_at = time.monotonic()
    stop.set()
    inflight.cut_off()
    thread.join(5)

    assert time.monotonic() - stopped_at < PROMPT
    assert outcome == {"resp": None}
