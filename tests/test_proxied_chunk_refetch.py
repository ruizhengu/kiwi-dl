"""A MAC failure re-fetches only the chunks that came through proxies.

MEGA's CDN URLs are plain http, so a proxy can hand back altered bytes. Each
chunk is decrypted and MAC'd as it arrives, but the only reference to check
against is the whole-file MAC in the link key - which says the file is wrong,
not which chunk. Before this, a failed check left the user one option: delete
everything and download it again.

Now every committed chunk records which proxy served it (a fingerprint of the
proxy's address, never its URL), and a failed check bans those proxies and
fetches just those chunks again - once. Direct chunks are trusted.
"""

from __future__ import annotations

import json

import pytest
from tenacity import wait_none

from megabasterd_cli.core import downloader as dl_mod
from megabasterd_cli.core.chunks import iter_chunks
from megabasterd_cli.core.downloader import MegaDownloader
from megabasterd_cli.core.errors import IntegrityError
from megabasterd_cli.core.state import (
    StateCorruptionError,
    TransferState,
    load_state,
    save_state,
    state_path_for,
    validate_state_dict,
)
from megabasterd_cli.proxy.quota_router import SmartProxyPolicy, proxy_fingerprint
from megabasterd_cli.proxy.smart_proxy import SmartProxyPool
from tests.fake_mega import FakeCdn

KiB = 1024
A = "http://proxy-a.invalid:8080"
B = "http://proxy-b.invalid:8080"
SIZE = 3 * 1024 * KiB
OFFSETS = [c.offset for c in iter_chunks(SIZE)]  # 7 chunks


@pytest.fixture(autouse=True)
def _quiet_retries(monkeypatch):
    monkeypatch.setattr(MegaDownloader._download_chunk.retry, "wait", wait_none())
    monkeypatch.setattr(dl_mod, "_local_route_fingerprint", lambda: "10.0.0.5")
    monkeypatch.setattr(
        "megabasterd_cli.proxy.smart_proxy.random.choices", lambda pop, weights, k: [pop[0]]
    )


@pytest.fixture()
def cdn(monkeypatch):
    cdn = FakeCdn(size=SIZE)
    monkeypatch.setattr("requests.get", cdn.get)
    return cdn


def _downloader(pool, mode="always"):
    return MegaDownloader(api=None, max_workers=1, smart_proxy=SmartProxyPolicy(pool, mode=mode))


def test_a_corrupting_proxy_is_banned_and_its_chunks_fetched_again(cdn, tmp_path):
    cdn.corrupt_routes.add(A)
    pool = SmartProxyPool([A, B])
    destination = tmp_path / "f.bin"

    result = cdn.download_with(_downloader(pool), destination)

    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    assert not pool.is_usable(A)
    first, second = cdn.requests[:7], cdn.requests[7:]
    assert {r["route"] for r in first} == {A}
    assert sorted(r["start"] for r in second) == OFFSETS
    assert {r["route"] for r in second} == {B}


def test_only_the_proxied_chunks_are_fetched_again(cdn, tmp_path):
    cdn.quota["direct"] = (128 + 256) * KiB  # chunks 0 and 1 direct, then 509
    cdn.corrupt_routes.add(A)
    destination = tmp_path / "f.bin"

    result = cdn.download_with(_downloader(SmartProxyPool([A, B]), mode="on-509"), destination)

    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    refetched = [r for r in cdn.requests if r["route"] == B]
    assert sorted(r["start"] for r in refetched) == OFFSETS[2:]


def test_without_proxied_chunks_a_mac_failure_is_final(cdn, tmp_path):
    cdn.corrupt_routes.add("direct")
    with pytest.raises(IntegrityError):
        cdn.download_with(_downloader(None, mode="on-509"), tmp_path / "f.bin")
    assert len(cdn.requests) == 7, "nothing was fetched again"


def test_there_is_only_one_second_round(cdn, tmp_path):
    cdn.corrupt_routes.update({A, B})
    with pytest.raises(IntegrityError):
        cdn.download_with(_downloader(SmartProxyPool([A, B])), tmp_path / "f.bin")
    assert len(cdn.requests) == 14


# ---------------------------------------------------------------------------
# What the resume state records
# ---------------------------------------------------------------------------


def test_the_fingerprint_ignores_credentials():
    assert proxy_fingerprint("http://user:secret@host.invalid:3128") == proxy_fingerprint(
        "http://host.invalid:3128"
    )
    assert proxy_fingerprint("http://host.invalid:3128") != proxy_fingerprint(
        "socks5://host.invalid:3128"
    )
    assert len(proxy_fingerprint("http://host.invalid:3128")) == 16


def test_the_state_file_never_holds_a_proxy_credential(cdn, tmp_path):
    secret_proxy = "http://alice:hunter2@proxy-a.invalid:8080"
    cdn.quota["direct"] = 128 * KiB
    cdn.quota[secret_proxy] = 256 * KiB  # serves chunk 1, then 509: the download stops
    destination = tmp_path / "f.bin"
    policy = SmartProxyPolicy(SmartProxyPool([secret_proxy]))

    with pytest.raises(Exception):  # noqa: B017 - only the state left behind matters
        cdn.download_with(MegaDownloader(api=None, max_workers=1, smart_proxy=policy), destination)

    raw = state_path_for(destination).read_text()
    assert "hunter2" not in raw and "alice" not in raw
    state = load_state(destination)
    assert state.proxied_chunks() == {1: proxy_fingerprint(secret_proxy)}


def test_a_chunk_fetched_direct_later_is_no_longer_proxied(tmp_path):
    state = TransferState("download", "src", str(tmp_path / "f"), 10)
    state.mark_chunk_done(0, b"\0" * 16)
    state.mark_chunk_proxied(0, "ab" * 8)
    state.mark_chunk_proxied(0, None)
    assert state.proxied_chunks() == {}


def test_forget_chunks_undoes_completion_and_the_proxy_record(tmp_path):
    state = TransferState("download", "src", str(tmp_path / "f"), 10)
    for i in range(3):
        state.mark_chunk_done(i, bytes([i]) * 16)
        state.mark_chunk_proxied(i, "cd" * 8)
    state.forget_chunks([0, 2])
    assert state.completed_chunks == [1]
    assert set(state.chunk_macs) == {1}
    assert state.proxied_chunks() == {1: "cd" * 8}


def test_proxied_chunks_round_trip_through_the_state_file(tmp_path):
    destination = tmp_path / "f.bin"
    destination.write_bytes(b"\0" * 10)  # a state only records chunks of a file that exists
    state = TransferState("download", "src", str(destination), 10)
    state.mark_chunk_done(4, b"\1" * 16)
    state.mark_chunk_proxied(4, "ef" * 8)
    save_state(state)
    assert load_state(destination).proxied_chunks() == {4: "ef" * 8}


@pytest.mark.parametrize(
    "bad",
    [["not", "an", "object"], {"x": "ab" * 8}, {"1": "zz" * 8}, {"1": "ab"}, {"1": 7}],
    ids=["list", "non-index-key", "non-hex", "short", "number"],
)
def test_a_tampered_proxied_record_is_refused(tmp_path, bad):
    state = TransferState("download", "src", str(tmp_path / "f"), 10)
    data = json.loads(json.dumps(state.__dict__))
    data["metadata"] = {"proxied_chunks": bad}
    with pytest.raises(StateCorruptionError):
        validate_state_dict(data)
