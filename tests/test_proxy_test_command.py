"""`kiwi-dl proxy test` must find the proxies that reach MEGA, without leaking them.

The pool was write-only in practice: `proxy list` shows the entries and their
success/failure counters, but a freshly imported list has all-zero counters, so
the only way to learn that an entry was dead was to start a transfer and watch
it fail.

Three properties are load-bearing and each has a test here:

* the verdict - a proxy works only when MEGA itself answered through it (an
  open port that is not a proxy does not count, nor does a proxy answering
  for itself), and the exit code is non-zero only when NOTHING worked (that
  is what a `kiwi-dl proxy test && kdl ...` script keys on);
* redaction - a pooled URL routinely carries `user:pass@`, and `proxy add` /
  `proxy import` store it exactly as typed, so the schemeless form that
  bypassed `redact_text` must go through `_safe_source`;
* rendering - the URLs come from a remote list or an arbitrary file, so the
  table must treat a cell as literal text and survive an unbalanced tag.

Bounds are tested too: the probes run concurrently on a fixed set of threads,
and the per-proxy timeout is `smart_proxy_timeout_seconds`.

No probe here leaves the loopback interface: the working proxy is an HTTP
server on 127.0.0.1 answering the way MEGA does, and the dead one is a port
that was just released.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from click.testing import CliRunner

from kiwi_dl.commands import proxy_cmd as proxy_module
from kiwi_dl.commands.proxy_cmd import proxy_cmd
from kiwi_dl.ui.theme import make_console

PASSWORD = "SENTINEL_PASSWORD"
TOKEN = "SENTINEL_TOKEN"


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    """Point the proxy store at an isolated directory (same as the other suites)."""
    monkeypatch.setattr("kiwi_dl.proxy.runtime.data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """The auto-detected width (80) would wrap a cell and hide the evidence."""
    monkeypatch.setattr(proxy_module, "_console", make_console(width=400))


@pytest.fixture(autouse=True)
def _capture_logs(caplog):
    caplog.set_level(logging.DEBUG)


def _serve(headers: dict[str, str], status: int):
    """A loopback HTTP 'proxy' that answers every request with `status` and `headers`."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def live_port():
    """A proxy that relays to MEGA: MEGA's own 301 and CORS header come back."""
    httpd = _serve(
        {
            "Location": "https://mega.nz",
            "Access-Control-Allow-Headers": "Content-Type, X-Hashcash, MEGA-Chrome-Antileak",
        },
        301,
    )
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def not_mega_port():
    """Something that speaks HTTP but is not MEGA: a captive portal, say."""
    httpd = _serve({"Content-Type": "text/html"}, 200)
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def silent_port():
    """A listening socket that never answers: an open port is not a proxy."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def dead_port():
    """A port that was bound and released, so a connect is refused, not hung."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _run(args, **cfg_kwargs):
    from kiwi_dl.config import Config

    return CliRunner().invoke(proxy_cmd, args, obj={"config": Config(**cfg_kwargs)})


def _blob(result, caplog) -> str:
    """Everything a human or a machine could read after the command ran."""
    parts = [result.output, repr(result.exception), caplog.text]
    # click<8.2 mixes stderr into output and raises on .stderr
    with contextlib.suppress(ValueError):
        parts.append(result.stderr)
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


def test_a_proxy_that_relays_to_mega_works(pool_dir, live_port):
    assert _run(["add", f"http://127.0.0.1:{live_port}"]).exit_code == 0

    result = _run(["test"])

    assert result.exit_code == 0, result.output
    assert "yes" in result.output, result.output
    assert "ms" in result.output, f"no latency reported: {result.output}"


def test_an_open_port_that_is_not_a_proxy_does_not_count(pool_dir, silent_port):
    assert _run(["add", f"http://127.0.0.1:{silent_port}"]).exit_code == 0

    result = _run(["test"], smart_proxy_timeout_seconds=1)

    assert result.exit_code == 1, result.output
    assert "Timeout" in result.output, result.output


def test_a_proxy_answering_for_itself_does_not_count(pool_dir, not_mega_port):
    assert _run(["add", f"http://127.0.0.1:{not_mega_port}"]).exit_code == 0

    result = _run(["test"])

    assert result.exit_code == 1, result.output
    assert "answered HTTP 200, but not as MEGA" in result.output, result.output


def test_a_closed_port_is_unreachable_and_exits_non_zero(pool_dir, dead_port):
    assert _run(["add", f"http://127.0.0.1:{dead_port}"]).exit_code == 0

    result = _run(["test"])

    assert result.exit_code == 1, result.output
    assert "no" in result.output, result.output
    # The reason has to survive: "unreachable" alone is not diagnosable.
    assert "Error" in result.output, f"no failure reason reported: {result.output}"


def test_one_working_proxy_is_enough_for_a_zero_exit(pool_dir, live_port, dead_port):
    assert _run(["add", f"http://127.0.0.1:{live_port}"]).exit_code == 0
    assert _run(["add", f"http://127.0.0.1:{dead_port}"]).exit_code == 0

    result = _run(["test"])

    assert result.exit_code == 0, result.output
    assert "yes" in result.output and "no" in result.output, result.output


def test_an_empty_pool_exits_non_zero(pool_dir):
    result = _run(["test"])

    assert result.exit_code == 1, result.output


def test_an_unparseable_entry_is_reported_not_raised(pool_dir):
    assert _run(["add", "not-a-proxy"]).exit_code == 0

    result = _run(["test"])

    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_a_schemeless_entrys_password_never_reaches_the_output(pool_dir, dead_port, caplog):
    """The known trap: `redact_text` only matches AFTER a `scheme://`."""
    assert _run(["add", f"alice:{PASSWORD}@127.0.0.1:{dead_port}"]).exit_code == 0

    result = _run(["test"])

    assert PASSWORD not in _blob(result, caplog), _blob(result, caplog)[:600]
    assert "127.0.0.1" in result.output, "the redacted proxy lost its host"


def test_a_scheme_qualified_entrys_password_never_reaches_the_output(pool_dir, dead_port, caplog):
    assert _run(["add", f"http://bob:{PASSWORD}@127.0.0.1:{dead_port}"]).exit_code == 0

    result = _run(["test"])

    # Assert the verdict too: without it this passes while `test` does not exist.
    assert result.exit_code == 1, result.output
    assert PASSWORD not in _blob(result, caplog), _blob(result, caplog)[:600]


def test_a_credential_bearing_query_value_never_reaches_the_output(pool_dir, dead_port, caplog):
    assert _run(["add", f"http://127.0.0.1:{dead_port}/?token={TOKEN}"]).exit_code == 0

    result = _run(["test"])

    assert result.exit_code == 1, result.output
    assert TOKEN not in _blob(result, caplog), _blob(result, caplog)[:600]


def test_the_raw_url_is_still_probed(pool_dir, live_port, monkeypatch):
    """Redaction is a DISPLAY concern; the socket must see the real host."""
    raw = f"http://alice:{PASSWORD}@127.0.0.1:{live_port}"
    assert _run(["add", raw]).exit_code == 0
    seen: list[str] = []
    real_probe = proxy_module._probe
    monkeypatch.setattr(
        proxy_module, "_probe", lambda url, timeout: (seen.append(url), real_probe(url, timeout))[1]
    )

    result = _run(["test"])

    assert result.exit_code == 0, result.output
    assert seen == [raw], f"redaction reached the probe: {seen}"


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_unbalanced_markup_in_a_stored_url_does_not_brick_the_command(pool_dir):
    """`proxy fetch`/`import` accept any line; a raw Table dies on this forever."""
    assert _run(["add", "http://127.0.0.1[/bold]x:1"]).exit_code == 0

    result = _run(["test"])

    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "[/bold]" in result.output, f"the stored URL was parsed as markup: {result.output}"


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------


def test_the_per_proxy_timeout_comes_from_the_config(pool_dir, dead_port, monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(
        proxy_module, "_probe", lambda url, timeout: (seen.append(timeout), (False, "x"))[1]
    )
    assert _run(["add", f"http://127.0.0.1:{dead_port}"]).exit_code == 0

    _run(["test"], smart_proxy_timeout_seconds=7)

    assert seen == [7], f"the probe did not use smart_proxy_timeout_seconds: {seen}"


def test_the_probes_run_concurrently(pool_dir, monkeypatch):
    """Serially, eight 0.3s probes take 2.4s; concurrently they take ~0.3s."""
    monkeypatch.setattr(
        proxy_module, "_probe", lambda url, timeout: (time.sleep(0.3), (False, "slow"))[1]
    )
    for port in range(9001, 9009):
        assert _run(["add", f"http://127.0.0.1:{port}"]).exit_code == 0

    start = time.monotonic()
    _run(["test"])
    elapsed = time.monotonic() - start

    assert elapsed < 1.5, f"probes ran serially: {elapsed:.2f}s for 8 x 0.3s"


def test_a_probe_that_outlives_the_run_budget_does_not_hang_the_command(pool_dir, monkeypatch):
    """The whole run is capped, not just each socket: a stuck DNS lookup ignores
    the connect timeout, so the command must give up on its own."""
    monkeypatch.setattr(
        proxy_module, "_probe", lambda url, timeout: (time.sleep(5), (True, "never"))[1]
    )
    assert _run(["add", "http://127.0.0.1:9100"]).exit_code == 0

    start = time.monotonic()
    result = _run(["test"], smart_proxy_timeout_seconds=1)
    elapsed = time.monotonic() - start

    # One wave gets two probe timeouts (connect, then read): 2 s, not 5.
    assert elapsed < 3.5, f"the run was not capped: {elapsed:.2f}s"
    assert result.exit_code == 1, result.output


def test_a_pool_larger_than_the_thread_cap_still_reports(pool_dir, monkeypatch, caplog):
    """A queued probe is CANCELLED at shutdown, and a cancelled future is `done()`.

    So the `future.result() if future.done()` branch called `.result()` on it and
    got `CancelledError` instead of a verdict, destroying the whole report - the
    64 probes that DID answer included. A fetched list is routinely hundreds of
    entries against a 64-thread cap, so every entry past the cap is queued and
    this is the normal path for the command's main use, not a corner.
    """
    monkeypatch.setattr(proxy_module, "MAX_PROBE_THREADS", 1)
    monkeypatch.setattr(
        proxy_module, "_probe", lambda url, timeout: (time.sleep(5), (True, "never"))[1]
    )
    for port in (9201, 9202):
        assert _run(["add", f"http://127.0.0.1:{port}"]).exit_code == 0

    start = time.monotonic()
    result = _run(["test"], smart_proxy_timeout_seconds=1)
    elapsed = time.monotonic() - start

    assert "CancelledError" not in _blob(result, caplog)
    assert result.exit_code == 1, result.output
    assert "0/2 reach MEGA" in result.output, result.output
    # The run budget is two probe timeouts per WAVE, so two entries against a
    # one-thread cap get 4s - still bounded, and still far under the 10s the
    # probes themselves want.
    assert elapsed < 5, f"the run was not capped: {elapsed:.2f}s"
    # Distinct reasons on purpose: one probe was tried and did not answer, the
    # other never got a socket. Calling the second "no" would be a wrong
    # verdict about a proxy that may be perfectly healthy.
    assert "no answer within" in result.output, result.output
    assert "not probed within the run budget" in result.output, result.output


def test_a_stuck_probe_cannot_hold_the_process_open_after_the_report(monkeypatch):
    """`concurrent.futures` joins its workers at interpreter exit, so one probe
    stuck in a connect kept `kiwi-dl proxy test` alive for its whole timeout
    after printing the report, and Ctrl-C printed "Aborted!" then hung as long.
    Probe threads must be daemons: still running, but never awaited at exit."""
    import threading

    release = threading.Event()
    monkeypatch.setattr(proxy_module, "_probe", lambda url, timeout: release.wait() or (True, ""))
    before = set(threading.enumerate())
    try:
        start = time.monotonic()
        results = proxy_module._run_probes(["http://127.0.0.1:9301"], timeout=0.2, workers=1)
        assert time.monotonic() - start < 1.5
        assert results == [proxy_module._NO_ANSWER]
        stuck = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        assert stuck, "the probe should still be running"
        assert all(t.daemon for t in stuck), "a non-daemon probe thread blocks interpreter exit"
    finally:
        release.set()


# ---------------------------------------------------------------------------
# long lists and keeping the working ones
# ---------------------------------------------------------------------------


def _fake_probe(working: set[str], seen: list[str] | None = None, delay: float = 0.0):
    def probe(url, timeout):
        if seen is not None:
            seen.append(url)
        if delay:
            time.sleep(delay)
        return (True, "12 ms") if url in working else (False, "ProxyError: refused")

    return probe


def test_keep_working_keeps_exactly_the_proxies_that_worked(pool_dir, monkeypatch):
    """MegaBasterd's "save working proxies": the dead ones are gone afterwards."""
    good, bad = "http://8.8.8.8:3128", "http://9.9.9.9:3128"
    assert _run(["add", good, bad]).exit_code == 0
    monkeypatch.setattr(proxy_module, "_probe", _fake_probe({good}))

    result = _run(["test", "--keep-working"])

    assert result.exit_code == 0, result.output
    assert "Kept 1 working" in result.output
    from kiwi_dl.proxy.runtime import _load_persisted_pool

    assert [e.url for e in _load_persisted_pool().list()] == [good]


def test_limit_tests_a_random_sample(pool_dir, monkeypatch):
    for i in range(10):
        assert _run(["add", f"http://8.8.8.{i + 1}:80"]).exit_code == 0
    seen: list[str] = []
    monkeypatch.setattr(proxy_module, "_probe", _fake_probe(set(), seen))

    _run(["test", "--limit", "3"])

    assert len(seen) == 3


def test_a_long_list_is_summarised_and_probed_on_a_bounded_set_of_threads(pool_dir, monkeypatch):
    """Thousands of entries: one thread each was thousands of threads at once."""
    urls = [f"http://8.8.{i // 250}.{i % 250 + 1}:80" for i in range(300)]
    assert _run(["add", *urls]).exit_code == 0
    monkeypatch.setattr(proxy_module, "MAX_PROBE_THREADS", 16)
    peak = [0]
    baseline = threading.active_count()

    def probe(url, timeout):
        peak[0] = max(peak[0], threading.active_count() - baseline)
        return (True, "40 ms") if url.endswith(".1:80") else (False, "ConnectTimeout: x")

    monkeypatch.setattr(proxy_module, "_probe", probe)

    result = _run(["test"])

    assert result.exit_code == 0, result.output
    assert peak[0] <= 16, f"{peak[0]} probe threads at once"
    assert "2/300 reach MEGA" in result.output, result.output
    assert "(298 proxies)" in result.output and "ConnectTimeout" in result.output


def test_the_proxy_lists_entries_are_fetched_and_tested(pool_dir, monkeypatch):
    from kiwi_dl.proxy.lists import ListRefresher

    assert _run(["add-list", "https://lists.invalid/s5.txt", "--scheme", "socks5"]).exit_code == 0
    real_init = ListRefresher.__init__

    def init(self, pool, sources, **kwargs):
        kwargs["fetch"] = lambda source, proxies=None: ["socks5://8.8.8.8:1080"]
        real_init(self, pool, sources, **kwargs)

    monkeypatch.setattr(ListRefresher, "__init__", init)
    seen: list[str] = []
    monkeypatch.setattr(proxy_module, "_probe", _fake_probe({"socks5://8.8.8.8:1080"}, seen))

    result = _run(["test"])

    assert result.exit_code == 0, result.output
    assert seen == ["socks5://8.8.8.8:1080"]
    assert "lists.invalid/s5.txt: 1 proxies" in result.output
