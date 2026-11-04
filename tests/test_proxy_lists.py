"""Proxy lists: URLs the user adds, fetched into the pool, MegaBasterd-style.

MegaBasterd's SmartProxy runs on free public lists (its developer points users
at TheSpeedX/PROXY-List): bare `ip:port` lines, one list per proxy kind, most
entries dead at any moment, refreshed every hour and 30 s after the pool runs
dry. kiwi-dl does the same with lists the user adds and bundles none.

A list is untrusted third-party input, so the parser is strict and capped, and
an entry naming a non-public address is dropped. Fetches go to a real HTTP
server on loopback.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from kiwi_dl.config import Config
from kiwi_dl.core.bounded_read import PayloadTooLargeError
from kiwi_dl.proxy import lists as lists_module
from kiwi_dl.proxy.lists import (
    DRY_RETRY_SECONDS,
    MAX_LIST_ENTRIES,
    REFRESH_SECONDS,
    ListRefresher,
    ProxyList,
    fetch_proxy_list,
    parse_entry,
    parse_proxy_list,
    validate_list,
)
from kiwi_dl.proxy.quota_router import ChunkRouter, SmartProxyPolicy
from kiwi_dl.proxy.runtime import (
    ProxyPoolCorruptionError,
    _load_persisted_pool,
    effective_pool,
    pool_payload,
)
from kiwi_dl.proxy.smart_proxy import SmartProxyPool

LIST = "https://lists.invalid/socks5.txt"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "scheme", "entry"),
    [
        ("8.8.8.8:1080", "socks5", "socks5://8.8.8.8:1080"),
        ("  8.8.8.8:3128  ", "http", "http://8.8.8.8:3128"),
        ("socks4://8.8.4.4:4145", "http", "socks4://8.8.4.4:4145"),
        ("http://user:pw@8.8.8.8:80", "socks5", "http://user:pw@8.8.8.8:80"),
        ("proxy.example.com:8080", "http", "http://proxy.example.com:8080"),
        ("HTTP://8.8.8.8:80/", "socks5", "http://8.8.8.8:80"),
    ],
)
def test_a_usable_line_becomes_an_entry(line, scheme, entry):
    assert parse_entry(line, scheme) == entry


@pytest.mark.parametrize(
    "line",
    [
        "",
        "# a comment",
        "8.8.8.8",
        "8.8.8.8:0",
        "8.8.8.8:70000",
        "8.8.8.8:80x",
        "8.8.8.8 :80",
        "ftp://8.8.8.8:21",
        "http://8.8.8.8:80/path",
        "http://8.8.8.8:80?q=1",
        "user:pw@8.8.8.8:80",  # credentials only with an explicit scheme
        "[2001:4860::8888]:80",
        "1.2.3.999:80",
        "-bad-.example:80",
        "x" * 400 + ":80",
    ],
)
def test_an_unusable_line_is_dropped(line):
    assert parse_entry(line, "http") is None


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.8",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
    ],
)
def test_an_entry_naming_a_non_public_address_is_dropped(host):
    """Every chunk request may go through a list entry: a hostile list must not
    be able to aim them at the user's own network."""
    assert parse_entry(f"{host}:8080", "http") is None
    assert parse_entry(f"socks5://{host}:1080", "http") is None


def test_a_list_is_deduplicated_and_capped():
    text = "\n".join(["8.8.8.8:80", "8.8.8.8:80", "garbage"])
    assert parse_proxy_list(text, "http") == ["http://8.8.8.8:80"]

    many = "\n".join(f"8.8.{i // 250}.{i % 250 + 1}:80" for i in range(MAX_LIST_ENTRIES + 50))
    assert len(parse_proxy_list(many, "http")) == MAX_LIST_ENTRIES


@pytest.mark.parametrize(
    ("url", "scheme", "ok"),
    [
        ("https://raw.example/http.txt", "http", True),
        ("http://raw.example/socks5.txt", "socks5", True),
        ("ftp://raw.example/list.txt", "http", False),
        ("https://raw.example/list.txt", "gopher", False),
        ("raw.example/list.txt", "http", False),
    ],
)
def test_a_list_url_is_validated_when_added(url, scheme, ok):
    if ok:
        assert validate_list(url, scheme) == ProxyList(url, scheme)
    else:
        with pytest.raises(ValueError):
            validate_list(url, scheme)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@pytest.fixture()
def server():
    """A loopback HTTP server; tests set `routes[path] = (status, body, headers)`."""
    routes: dict[str, tuple[int, bytes, dict]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            status, body, headers = routes.get(self.path, (404, b"", {}))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, routes
    httpd.shutdown()
    httpd.server_close()


def test_a_list_is_fetched_and_parsed(server):
    base, routes = server
    routes["/socks5.txt"] = (200, b"8.8.8.8:1080\n1.1.1.1:1080\n10.0.0.1:1080\n", {})
    assert fetch_proxy_list(ProxyList(f"{base}/socks5.txt", "socks5")) == [
        "socks5://8.8.8.8:1080",
        "socks5://1.1.1.1:1080",
    ]


def test_an_oversized_list_is_refused_while_reading(server, monkeypatch):
    base, routes = server
    monkeypatch.setattr(lists_module, "MAX_LIST_BYTES", 64)
    routes["/big.txt"] = (200, b"8.8.8.8:80\n" * 100, {})
    with pytest.raises(PayloadTooLargeError):
        fetch_proxy_list(ProxyList(f"{base}/big.txt"))


def test_an_http_error_is_raised(server):
    base, _ = server
    with pytest.raises(Exception, match="404"):
        fetch_proxy_list(ProxyList(f"{base}/missing.txt"))


def test_a_redirect_loop_is_cut_off(server):
    base, routes = server
    routes["/loop"] = (302, b"", {"Location": "/loop"})
    with pytest.raises(Exception, match="redirects"):
        fetch_proxy_list(ProxyList(f"{base}/loop"))


# ---------------------------------------------------------------------------
# Refreshing
# ---------------------------------------------------------------------------


def _refresher(pool, answers, clock=None):
    """A refresher whose fetches return `answers[url]` (a list, or an exception)."""
    calls: list[str] = []

    def fetch(source, proxies=None):
        calls.append(source.url)
        answer = answers[source.url]
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    refresher = ListRefresher(pool, pool.list_sources, fetch=fetch, clock=clock or Clock())
    pool.refresher = refresher
    return refresher, calls


def _pool_with_list(*urls):
    pool = SmartProxyPool(urls)
    pool.list_sources.append(ProxyList(LIST, "socks5"))
    return pool


def test_a_refresh_replaces_the_list_entries_and_keeps_the_users_own():
    pool = _pool_with_list("http://own.invalid:8080")
    answers = {LIST: ["socks5://8.8.8.8:1080", "socks5://1.1.1.1:1080"]}
    refresher, _ = _refresher(pool, answers)
    refresher.refresh_now()

    answers[LIST] = ["socks5://1.1.1.1:1080", "socks5://9.9.9.9:1080"]
    refresher.refresh_now()

    assert sorted(e.url for e in pool.list()) == [
        "http://own.invalid:8080",
        "socks5://1.1.1.1:1080",
        "socks5://9.9.9.9:1080",
    ]


def test_a_refresh_does_not_unban_a_proxy_that_stays_on_the_list():
    pool = _pool_with_list()
    refresher, _ = _refresher(pool, {LIST: ["socks5://8.8.8.8:1080"]})
    refresher.refresh_now()
    pool.ban("socks5://8.8.8.8:1080", 300)

    refresher.refresh_now()

    assert not pool.is_usable("socks5://8.8.8.8:1080")


@pytest.mark.parametrize(
    "answer", [ConnectionError("down"), []], ids=["fetch-fails", "list-is-empty"]
)
def test_a_failed_or_empty_refresh_keeps_the_previous_list(answer):
    pool = _pool_with_list()
    answers: dict = {LIST: ["socks5://8.8.8.8:1080"]}
    refresher, _ = _refresher(pool, answers)
    refresher.refresh_now()

    answers[LIST] = answer
    results = refresher.refresh_now()

    assert [e.url for e in pool.list()] == ["socks5://8.8.8.8:1080"]
    assert isinstance(results[0][1], str)


def _run_tick(refresher):
    """tick() refreshes on a background thread; wait for that one."""
    before = refresher._thread
    refresher.tick()
    if refresher._thread is not None and refresher._thread is not before:
        refresher._thread.join(timeout=5)


def test_lists_refresh_every_hour_while_proxies_are_wanted():
    clock = Clock()
    pool = _pool_with_list()
    refresher, calls = _refresher(pool, {LIST: ["socks5://8.8.8.8:1080"]}, clock)
    refresher.refresh_now()

    clock.now += REFRESH_SECONDS - 1
    _run_tick(refresher)
    assert len(calls) == 1

    clock.now += 2
    _run_tick(refresher)
    assert len(calls) == 2


def test_a_dry_pool_refetches_its_lists_30_seconds_later():
    clock = Clock()
    pool = _pool_with_list()
    refresher, calls = _refresher(pool, {LIST: ["socks5://8.8.8.8:1080"]}, clock)
    refresher.refresh_now()

    refresher.ran_dry()
    clock.now += DRY_RETRY_SECONDS - 1
    _run_tick(refresher)
    assert len(calls) == 1

    clock.now += 2
    _run_tick(refresher)
    assert len(calls) == 2


def test_the_router_asks_for_a_refresh_and_reports_a_dry_pool(monkeypatch):
    pool = _pool_with_list()
    events: list[str] = []

    class Spy:
        def tick(self):
            events.append("tick")

        def ran_dry(self):
            events.append("dry")

    pool.refresher = Spy()  # type: ignore[assignment]
    route = ChunkRouter(SmartProxyPolicy(pool, mode="always")).route()

    assert route.fallback, "an empty pool goes direct"
    assert events == ["tick", "dry"]


# ---------------------------------------------------------------------------
# Storage and the pool a command builds
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiwi_dl.proxy.runtime.data_dir", lambda: tmp_path)
    return tmp_path


def test_lists_are_stored_and_fetched_entries_are_not(data_dir):
    pool = _pool_with_list("http://own.invalid:8080")
    pool.replace_source(LIST, ["socks5://8.8.8.8:1080"])

    payload = json.loads(pool_payload(pool))

    assert payload == {
        "proxies": ["http://own.invalid:8080"],
        "lists": [{"url": LIST, "scheme": "socks5"}],
    }
    (data_dir / "proxies.json").write_text(json.dumps(payload), encoding="utf-8")
    loaded = _load_persisted_pool()
    assert loaded.list_sources == [ProxyList(LIST, "socks5")]
    assert [e.url for e in loaded.list()] == ["http://own.invalid:8080"]


@pytest.mark.parametrize(
    "lists",
    [
        '"x"',
        "[1]",
        '[{"scheme": "http"}]',
        '[{"url": "ftp://x/y", "scheme": "http"}]',
        '[{"url": "https://x/y", "scheme": "nope"}]',
    ],
)
def test_a_malformed_list_entry_marks_the_store_corrupt(data_dir, lists):
    (data_dir / "proxies.json").write_text(f'{{"proxies": [], "lists": {lists}}}', encoding="utf-8")
    with pytest.raises(ProxyPoolCorruptionError):
        _load_persisted_pool()


def test_a_command_pool_is_filled_from_its_lists(data_dir, monkeypatch):
    (data_dir / "proxies.json").write_text(
        json.dumps({"proxies": [], "lists": [{"url": LIST, "scheme": "socks5"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ListRefresher,
        "__init__",
        _init_with_fetch(lambda source, proxies=None: ["socks5://8.8.8.8:1080"]),
    )

    pool = effective_pool(Config())

    assert pool is not None and pool.refresher is not None
    assert [(e.url, e.source) for e in pool.list()] == [("socks5://8.8.8.8:1080", LIST)]


def test_a_pool_with_lists_survives_a_failed_first_fetch(data_dir, monkeypatch):
    """The refresher retries once a download wants a proxy, so the pool must exist."""
    (data_dir / "proxies.json").write_text(
        json.dumps({"proxies": [], "lists": [{"url": LIST, "scheme": "socks5"}]}),
        encoding="utf-8",
    )

    def down(source, proxies=None):
        raise ConnectionError("offline")

    monkeypatch.setattr(ListRefresher, "__init__", _init_with_fetch(down))
    pool = effective_pool(Config())

    assert pool is not None and pool.list() == [] and pool.refresher is not None


def _init_with_fetch(fetch):
    real = ListRefresher.__init__

    def init(self, pool, sources, **kwargs):
        kwargs["fetch"] = fetch
        real(self, pool, sources, **kwargs)

    return init


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------


def _cli(*args):
    from kiwi_dl.commands.proxy_cmd import proxy_cmd

    return CliRunner().invoke(proxy_cmd, list(args), obj={"config": Config()})


def test_add_list_remove_list_and_show_them(data_dir):
    secret = "https://raw.example/list.txt?token=SENTINEL_TOKEN_123"
    assert _cli("add-list", secret, "--scheme", "socks5").exit_code == 0
    assert _cli("add-list", "https://raw.example/http.txt").exit_code == 0

    shown = _cli("list")

    assert "raw.example/http.txt" in shown.output and "(socks5)" in shown.output
    assert "SENTINEL_TOKEN_123" not in shown.output
    assert _cli("remove-list", "https://raw.example/http.txt").exit_code == 0
    assert [s.url for s in _load_persisted_pool().list_sources] == [secret]


def test_add_list_refuses_a_bad_url(data_dir):
    result = _cli("add-list", "ftp://raw.example/list.txt")
    assert result.exit_code == 2
    assert _load_persisted_pool().list_sources == []
