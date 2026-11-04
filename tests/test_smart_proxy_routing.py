"""SmartProxy for chunks: direct first, the pool after a 509, bans and a quota budget.

The behaviour is MegaBasterd's SmartProxy (read from v8.61, reimplemented): a
chunk 509 opens a command-wide proxy window, each worker keeps its proxy until
it fails, an HTTP error bans a proxy for everyone while a timeout or 429 only
sets it aside for one worker, and an exhausted pool goes direct. What it adds:
the window follows MEGA's `X-MEGA-Time-Left`, the API stays direct, and how
long quota blocks are waited out can be capped (`quota_budget_seconds`; the
default waits for as long as it takes, as MegaBasterd does).

Before this, a configured pool carried every request - API and chunks - from
the first byte through a proxy re-picked at random per request, and a chunk
509 was treated as an expired URL.

End-to-end tests run the real chunk path against `FakeCdn`, whose per-route
quota answers like the live capture (509, `X-MEGA-Time-Left`, burned URL).
"""

from __future__ import annotations

import logging
import math
import threading

import pytest
from click.testing import CliRunner
from tenacity import wait_none

from kiwi_dl.cli import cli
from kiwi_dl.config import Config, validate_config_value
from kiwi_dl.core import downloader as dl_mod
from kiwi_dl.core.downloader import MegaDownloader
from kiwi_dl.core.errors import TransferCancelled, TransferError, TransferQuotaError
from kiwi_dl.core.folder_downloader import FolderNode, MegaFolderDownloader
from kiwi_dl.core.state import load_state
from kiwi_dl.proxy.quota_router import (
    ROUTE_CHECK_SECONDS,
    WAIT_NOTICE_SECONDS,
    ChunkRouter,
    Route,
    SmartProxyPolicy,
)
from kiwi_dl.proxy.selector import ProxyRequiredError
from kiwi_dl.proxy.smart_proxy import SmartProxyPool
from tests.fake_mega import FakeCdn

KiB = 1024
A = "http://proxy-a.invalid:8080"
B = "http://proxy-b.invalid:8080"
MANUAL = {"http": "http://manual.invalid:3128", "https": "http://manual.invalid:3128"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Exercise routing, not tenacity's wall-clock backoff."""
    monkeypatch.setattr(MegaDownloader._download_chunk.retry, "wait", wait_none())


@pytest.fixture(autouse=True)
def steady_route(monkeypatch):
    monkeypatch.setattr(dl_mod, "_local_route_fingerprint", lambda: "10.0.0.5")


@pytest.fixture()
def first_pick(monkeypatch):
    """Make the pool pick the first available entry, so a test knows who goes first."""
    monkeypatch.setattr(
        "kiwi_dl.proxy.smart_proxy.random.choices", lambda pop, weights, k: [pop[0]]
    )


@pytest.fixture()
def cdn(monkeypatch):
    cdn = FakeCdn(size=3 * 1024 * KiB)  # 7 chunks: 128K, 256K, ... , 1M
    monkeypatch.setattr("requests.get", cdn.get)
    return cdn


def _download(cdn, tmp_path, policy, name="f.bin", workers=1):
    dl = MegaDownloader(api=None, max_workers=workers, smart_proxy=policy)
    destination = tmp_path / name
    result = cdn.download_with(dl, destination)
    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    return result


def _in_thread(fn):
    out: list = []
    thread = threading.Thread(target=lambda: out.append(fn()))
    thread.start()
    thread.join()
    return out[0]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="on-509"):
        SmartProxyPolicy(mode="sometimes")


@pytest.mark.parametrize(("given", "used"), [(5, 60), (3600, 3600), (10**6, 86400)])
def test_the_window_is_clamped_like_megabasterd(given, used):
    assert SmartProxyPolicy(window_seconds=given).window_seconds == used


def test_off_ignores_a_configured_pool():
    assert SmartProxyPolicy(SmartProxyPool([A]), mode="off").pool is None


@pytest.mark.parametrize(
    ("key", "value", "ok"),
    [
        ("smart_proxy_mode", "on-509", True),
        ("smart_proxy_mode", "always", True),
        ("smart_proxy_mode", "on509", False),
        ("smart_proxy_window_seconds", 59, False),
        ("smart_proxy_window_seconds", 86400, True),
        ("smart_proxy_ban_seconds", 0, True),
        ("smart_proxy_ban_seconds", -1, False),
        ("quota_budget_seconds", 0, True),
        ("quota_budget_seconds", None, True),
        ("quota_budget_seconds", -5, False),
        ("quota_budget_seconds", True, False),
        ("quota_budget_seconds", "60", False),
    ],
)
def test_config_validation(key, value, ok):
    if ok:
        validate_config_value(key, value)
    else:
        with pytest.raises(ValueError):
            validate_config_value(key, value)


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def test_the_window_follows_the_quota_reset_time():
    clock = Clock()
    policy = SmartProxyPolicy(SmartProxyPool([A]), window_seconds=3600, clock=clock)
    assert not policy.proxies_wanted()

    policy.open_window(time_left=20226)  # resets later than the window: window wins
    clock.advance(3599)
    assert policy.proxies_wanted()
    clock.advance(2)
    assert not policy.proxies_wanted()

    policy.open_window(time_left=120)  # resets sooner: go direct again then
    clock.advance(119)
    assert policy.proxies_wanted()
    clock.advance(2)
    assert not policy.proxies_wanted()

    policy.open_window(time_left=5)  # but never sooner than a minute
    clock.advance(59)
    assert policy.proxies_wanted()


def test_a_route_change_closes_the_window_early():
    clock = Clock()
    route = ["10.0.0.5"]
    policy = SmartProxyPolicy(SmartProxyPool([A]), clock=clock, route_fingerprint=lambda: route[0])
    policy.open_window(time_left=None)
    route[0] = "10.8.0.2"  # VPN came up
    clock.advance(ROUTE_CHECK_SECONDS)
    assert not policy.proxies_wanted()


def test_a_509_moves_the_rest_of_the_download_to_one_proxy(cdn, tmp_path):
    cdn.quota["direct"] = (128 + 256) * KiB  # chunks 0 and 1, then 509
    policy = SmartProxyPolicy(SmartProxyPool([A, B]))

    _download(cdn, tmp_path, policy)

    routes = [r["route"] for r in cdn.requests]
    hit = [r["status"] for r in cdn.requests].index(509)
    assert routes[: hit + 1] == ["direct"] * (hit + 1)
    proxied = routes[hit + 1 :]
    assert proxied and "direct" not in proxied
    assert len(set(proxied)) == 1, "one worker kept one proxy"
    assert 403 not in [r["status"] for r in cdn.requests], "the burned URL was reused"
    assert policy.proxies_wanted()


def test_the_window_is_shared_by_every_download_of_the_command(cdn, tmp_path):
    cdn.quota["direct"] = 128 * KiB
    policy = SmartProxyPolicy(SmartProxyPool([A, B]))
    _download(cdn, tmp_path, policy, "first.bin")

    before = len(cdn.requests)
    _download(cdn, tmp_path, policy, "second.bin")

    assert all(r["route"] != "direct" for r in cdn.requests[before:])


def test_after_the_window_direct_is_tried_again(cdn, tmp_path):
    clock = Clock()
    cdn.quota["direct"] = 128 * KiB
    policy = SmartProxyPolicy(SmartProxyPool([A]), clock=clock)
    _download(cdn, tmp_path, policy, "first.bin")

    clock.advance(3601)
    del cdn.quota["direct"]  # quota is back
    before = len(cdn.requests)
    _download(cdn, tmp_path, policy, "second.bin")

    assert all(r["route"] == "direct" for r in cdn.requests[before:])


def test_always_mode_never_goes_direct_while_a_proxy_works(cdn, tmp_path):
    _download(cdn, tmp_path, SmartProxyPolicy(SmartProxyPool([A, B]), mode="always"))
    routes = {r["route"] for r in cdn.requests}
    assert len(routes) == 1 and "direct" not in routes


# ---------------------------------------------------------------------------
# Ban, exclude, exhaust
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "banned"),
    [
        ("http-500", True),
        ("http-407", True),
        ("quota-509", True),
        ("http-429", False),
        ("connection-error", False),
        ("timeout", False),
        ("broken-stream", False),
        ("unusable-scheme", True),  # socks5:// without PySocks
    ],
)
def test_how_each_proxy_failure_is_treated(cdn, tmp_path, first_pick, fault, banned):
    import requests

    if fault.startswith("http-"):
        cdn.proxy_status[A] = int(fault.split("-")[1])
    elif fault == "quota-509":
        cdn.quota[A] = 0
    else:
        exc = {
            "connection-error": requests.ConnectionError("dead"),
            "timeout": requests.Timeout(),
            "broken-stream": requests.exceptions.ChunkedEncodingError(),
            "unusable-scheme": requests.exceptions.InvalidSchema("Missing dependencies for SOCKS"),
        }[fault]
        cdn.fail_with = lambda record: exc if record["route"] == A else None
    pool = SmartProxyPool([A, B])

    _download(cdn, tmp_path, SmartProxyPolicy(pool, mode="always"))

    assert cdn.requests[0]["route"] == A
    assert all(r["route"] == B for r in cdn.requests[1:]), "the chunk moved to the next proxy"
    assert pool.is_usable(A) is not banned


def test_a_ban_is_for_everyone_and_an_exclusion_for_one_worker(first_pick):
    pool = SmartProxyPool([A, B])
    router = ChunkRouter(SmartProxyPolicy(pool, mode="always"))

    mine = router.route()
    assert mine.proxy == A
    router.proxy_failed(mine, ban=False, why="timeout")
    assert router.route().proxy == B
    assert _in_thread(router.route).proxy == A, "another worker may still use it"

    router.proxy_failed(Route(None, A), ban=True, why="HTTP 500")
    assert _in_thread(router.route).proxy == B


def test_an_exhausted_pool_goes_direct_and_starts_over(first_pick):
    router = ChunkRouter(SmartProxyPolicy(SmartProxyPool([A]), mode="always"))
    router.proxy_failed(router.route(), ban=False, why="timeout")

    assert router.route() == Route(None, fallback=True)
    assert router.route().proxy == A, "the exclusions were cleared"


def test_a_banned_proxy_comes_back_when_its_ban_ends():
    pool = SmartProxyPool([A])
    pool.ban(A, 0.01)
    assert not pool.is_usable(A)
    threading.Event().wait(0.02)
    assert pool.is_usable(A)


def test_a_zero_ban_lasts_for_the_run():
    pool = SmartProxyPool([A])
    pool.ban(A, 0)
    assert not pool.is_usable(A)


def test_strict_mode_never_goes_direct(first_pick):
    router = ChunkRouter(SmartProxyPolicy(SmartProxyPool([A]), strict=True))
    router.proxy_failed(router.route(), ban=True, why="HTTP 500")
    with pytest.raises(ProxyRequiredError):
        router.route()
    with pytest.raises(ProxyRequiredError):
        ChunkRouter(SmartProxyPolicy(None, strict=True)).route()
    assert ChunkRouter(SmartProxyPolicy(None, strict=True), MANUAL).route().proxies == MANUAL


def test_a_manual_proxy_is_the_direct_route():
    assert ChunkRouter(SmartProxyPolicy(None), MANUAL).route() == Route(MANUAL)


# ---------------------------------------------------------------------------
# Quota blocks and the budget
# ---------------------------------------------------------------------------


def test_without_a_pool_a_509_stops_the_download_at_once(cdn, tmp_path):
    cdn.quota["direct"] = 0
    with pytest.raises(TransferQuotaError, match="no proxy pool") as caught:
        _download(cdn, tmp_path, SmartProxyPolicy(None))
    assert caught.value.time_left == 20226
    assert len(cdn.requests) == 1


def test_when_every_proxy_fails_the_direct_fallback_ends_it(cdn, tmp_path):
    import requests

    cdn.quota["direct"] = 0
    cdn.fail_with = lambda r: requests.ConnectionError("dead") if r["route"] != "direct" else None
    with pytest.raises(TransferQuotaError, match="every proxy in the pool"):
        _download(cdn, tmp_path, SmartProxyPolicy(SmartProxyPool([A, B])))


def test_blocks_are_waited_out_from_the_budget_and_then_stop(cdn, tmp_path, monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr("kiwi_dl.core.downloader.time.sleep", clock.advance)
    cdn.quota["direct"] = 0

    with (
        caplog.at_level(logging.WARNING, logger="kiwi_dl"),
        pytest.raises(TransferQuotaError, match="quota budget of 2m 30s is spent"),
    ):
        _download(cdn, tmp_path, SmartProxyPolicy(None, budget_seconds=150, clock=clock))

    # 509, wait 60s, 509, wait 60s, 509, wait the last 30s, 509: budget spent.
    # And no other chunk asks after that: the quota stop stops the download.
    assert [r["status"] for r in cdn.requests] == [509, 509, 509, 509]
    assert clock.now == 1150
    assert "waiting up to 2m 30s more for it to come back" in _wait_notices(caplog)[0]


def _wait_notices(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "no proxy can take over; waiting" in r.getMessage()
    ]


def test_the_command_waits_for_the_quota_by_default():
    assert Config().quota_budget_seconds is None
    assert SmartProxyPolicy.from_config(Config(), None).budget_left() == math.inf


def test_with_no_limit_a_block_is_waited_out_until_the_quota_returns(
    cdn, tmp_path, monkeypatch, caplog
):
    clock = Clock()

    def sleep(seconds):
        clock.advance(seconds)
        if clock.now >= 1000 + 20 * 60:
            cdn.quota.pop("direct", None)

    monkeypatch.setattr("kiwi_dl.core.downloader.time.sleep", sleep)
    cdn.quota["direct"] = 0

    with caplog.at_level(logging.WARNING, logger="kiwi_dl"):
        _download(cdn, tmp_path, SmartProxyPolicy(None, budget_seconds=None, clock=clock))

    # A direct retry each minute for 20 minutes, then the rest of the file.
    assert sum(r["status"] == 509 for r in cdn.requests) == 20
    # One notice at the start and one 15 minutes in - not one per retry.
    notices = _wait_notices(caplog)
    assert len(notices) == 2
    assert "waiting for it to come back" in notices[0]
    assert "MEGA says it resets in about 5h 37m" in notices[0]


def test_one_wait_notice_per_quarter_hour_however_many_workers_wait():
    clock = Clock()
    policy = SmartProxyPolicy(None, budget_seconds=None, clock=clock)
    assert [policy.wait_notice_due() for _ in range(8)] == [True] + [False] * 7
    clock.advance(WAIT_NOTICE_SECONDS - 1)
    assert not policy.wait_notice_due()
    clock.advance(1)
    assert policy.wait_notice_due()


def test_stopping_during_the_wait_cancels_and_keeps_the_resume_state(cdn, tmp_path, monkeypatch):
    clock = Clock()
    dl = MegaDownloader(
        api=None,
        max_workers=1,
        smart_proxy=SmartProxyPolicy(None, budget_seconds=None, clock=clock),
    )
    destination = tmp_path / "f.bin"
    cdn.quota["direct"] = 128 * KiB  # the first chunk, then the quota is spent

    def sleep(seconds):
        clock.advance(seconds)
        if clock.now >= 1000 + 5 * 60:
            dl.stop()  # what Ctrl-C does

    monkeypatch.setattr("kiwi_dl.core.downloader.time.sleep", sleep)

    with pytest.raises(TransferCancelled):
        cdn.download_with(dl, destination)
    assert sorted(load_state(destination).completed_chunks) == [0]


def test_a_quota_that_comes_back_during_the_wait_resumes_the_download(cdn, tmp_path, monkeypatch):
    clock = Clock()

    def sleep_and_reset(seconds):
        clock.advance(seconds)
        cdn.quota.pop("direct", None)

    monkeypatch.setattr("kiwi_dl.core.downloader.time.sleep", sleep_and_reset)
    cdn.quota["direct"] = 0

    _download(cdn, tmp_path, SmartProxyPolicy(None, budget_seconds=3600, clock=clock))


def test_the_budget_is_wall_clock_not_per_worker():
    clock = Clock()
    policy = SmartProxyPolicy(None, budget_seconds=100, clock=clock)
    with policy.blocked(), policy.blocked():  # two workers waiting at once
        clock.advance(30)
    assert policy.budget_left() == 70


# ---------------------------------------------------------------------------
# The command: one policy, the API direct, exit code 3
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIWI_DL_HOME", str(tmp_path / "user"))
    monkeypatch.setenv("KIWI_DL_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


def _run(cli_env, *urls):
    return CliRunner().invoke(cli, ["-q", "download", *urls, "-P", "1", "-o", str(cli_env / "o")])


def test_a_batch_stopped_only_by_quota_exits_3(cli_env, monkeypatch):
    def over_quota(self, url, output_dir, **kwargs):
        raise TransferQuotaError(time_left=60)

    monkeypatch.setattr(MegaDownloader, "download_link", over_quota)
    result = _run(cli_env, "https://mega.nz/file/aaa111#k1", "https://mega.nz/file/bbb222#k2")
    assert result.exit_code == 3


def test_the_wait_limit_can_be_set_and_cleared_from_the_command_line(cli_env):
    def run(*args):
        result = CliRunner().invoke(cli, ["config", *args])
        assert result.exit_code == 0, result.output
        return result.output.strip()

    def budget():
        return run("get", "quota_budget_seconds")

    assert budget() == ""  # no limit
    run("set", "quota_budget_seconds", "0")
    assert budget() == "0"
    run("unset", "quota_budget_seconds")
    assert budget() == ""
    run("set", "quota_budget_seconds", "7200")
    run("set", "quota_budget_seconds", "none")
    assert budget() == ""


def test_any_other_failure_in_the_batch_still_exits_1(cli_env, monkeypatch):
    def mixed(self, url, output_dir, **kwargs):
        if "aaa111" in url:
            raise TransferQuotaError(time_left=60)
        raise TransferError(message="broken")

    monkeypatch.setattr(MegaDownloader, "download_link", mixed)
    result = _run(cli_env, "https://mega.nz/file/aaa111#k1", "https://mega.nz/file/bbb222#k2")
    assert result.exit_code == 1


@pytest.mark.parametrize("strict", [False, True])
def test_api_calls_use_the_pool_only_in_strict_mode(cli_env, monkeypatch, strict):
    import kiwi_dl.commands.download_cmd as download_cmd

    pool = SmartProxyPool([A])
    monkeypatch.setattr("kiwi_dl.proxy.runtime.effective_pool_for_cmd", lambda c, p: pool)
    seen: list = []
    real_api_for = download_cmd.api_for

    def spy(cfg, **kwargs):
        seen.append(kwargs["proxy_pool"])
        return real_api_for(cfg, **kwargs)

    monkeypatch.setattr(download_cmd, "api_for", spy)
    monkeypatch.setattr(
        MegaDownloader,
        "download_link",
        lambda self, url, output_dir, **kw: (_ for _ in ()).throw(TransferError(message="x")),
    )
    if strict:
        CliRunner().invoke(cli, ["config", "set", "force_smart_proxy", "true"])

    _run(cli_env, "https://mega.nz/file/aaa111#k1")

    assert seen == [pool if strict else None]


def test_a_folder_stopped_only_by_quota_reports_the_soonest_reset(tmp_path, monkeypatch):
    resets = iter([900, 300, None])

    def over_quota(self, folder_id, node, destination, progress):
        raise TransferQuotaError(time_left=next(resets))

    monkeypatch.setattr(MegaFolderDownloader, "_download_owned_file", over_quota)
    jobs = [
        (FolderNode(f"h{i}", "root", 0, 1, f"{i}.bin", b"\0" * 32), tmp_path / f"{i}.bin")
        for i in range(3)
    ]
    with pytest.raises(TransferQuotaError) as caught:
        MegaFolderDownloader(MegaDownloader(api=None))._download_file_jobs("FOLDER", jobs)
    assert caught.value.time_left == 300


def test_a_folder_with_another_failure_is_a_plain_transfer_error(tmp_path, monkeypatch):
    errors = iter([TransferQuotaError(time_left=60), TransferError(message="broken")])

    def failing(self, folder_id, node, destination, progress):
        raise next(errors)

    monkeypatch.setattr(MegaFolderDownloader, "_download_owned_file", failing)
    jobs = [
        (FolderNode(f"h{i}", "root", 0, 1, f"{i}.bin", b"\0" * 32), tmp_path / f"{i}.bin")
        for i in range(2)
    ]
    with pytest.raises(TransferError) as caught:
        MegaFolderDownloader(MegaDownloader(api=None))._download_file_jobs("FOLDER", jobs)
    assert not isinstance(caught.value, TransferQuotaError)
