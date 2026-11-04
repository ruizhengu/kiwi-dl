"""`kiwi-dl proxy` - manage the Smart Proxy pool."""

from __future__ import annotations

import random
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import click
import requests

from ..proxy.lists import PROXY_SCHEMES, ProxyList, validate_list
from ..proxy.runtime import _load_persisted_pool, _pool_path, pool_payload
from ..ui.prompts import confirmed, print_error, print_info, print_success
from ..ui.theme import SafeTable, make_console
from ..utils.atomic import atomic_write_text
from ..utils.filelock import FileLock
from ..utils.redaction import REDACTED, redact_text

_console = make_console()

# Matches the other persisted stores (config, state): long enough for a
# concurrent `kiwi-dl proxy add` to finish, short enough to fail with a clear error.
_POOL_LOCK_TIMEOUT_SECONDS = 10.0


@contextmanager
def pool_transaction():
    """Hold the lock across the WHOLE read-modify-write, then save.

    Locking only the write made the write atomic and the transaction not.
    Every command did `_load_pool()` outside the lock, mutated that snapshot,
    and saved it - so two concurrent `proxy add` processes both read the same
    pool and the second save silently discarded the first one's entry, with
    the lock dutifully held the entire time.

    Yield the pool, mutate it, and the save happens here on a clean exit. The
    lock and the write are inline because this is their only caller - a named
    `_write_pool_locked` whose docstring had to say "the caller MUST already
    hold the lock" is a precondition nobody can now get wrong.
    """
    path = _pool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        path.parent / (path.name + ".lock"),
        message=(
            f"Could not lock the proxy pool within {_POOL_LOCK_TIMEOUT_SECONDS:.0f}s; "
            "another proxy command is holding it. Retry after it finishes."
        ),
    )
    lock.acquire(timeout=_POOL_LOCK_TIMEOUT_SECONDS)
    try:
        # Re-read INSIDE the lock: this is what makes the loser of a race
        # observe the winner's write instead of overwriting it.
        pool = _load_persisted_pool()
        yield pool
        atomic_write_text(path, pool_payload(pool))
    finally:
        lock.release()


@click.group("proxy", short_help="Manage the Smart Proxy pool.")
def proxy_cmd() -> None:
    """Add, remove, list proxies in the Smart Proxy pool, and the proxy lists
    it is filled from."""


@proxy_cmd.command("list", short_help="List all proxies in the pool.")
@click.option(
    "--config-urls/--no-config-urls",
    default=True,
    help="Also show URLs from the `smart_proxy_url` config field (default on).",
)
@click.pass_context
def proxy_list(ctx: click.Context, config_urls: bool) -> None:
    cfg = ctx.obj["config"]
    pool = _load_persisted_pool()
    persisted = {e.url for e in pool.list()}
    if config_urls:
        from ..proxy.runtime import _urls_from_config

        for url in _urls_from_config(cfg):
            pool.add(url)
    entries = pool.list()
    _print_lists(pool.list_sources)
    if not entries:
        if not pool.list_sources:
            _console.print("[kiwi.muted]No proxies stored.[/kiwi.muted]")
        return
    # SafeTable, not Table: these URLs come from `proxy import` (an arbitrary
    # file) or a pasted list, so a cell is untrusted. One poisoned line with an
    # unbalanced tag used to brick `proxy list` for good.
    table = SafeTable(
        title="Smart Proxy Pool",
        show_header=True,
        header_style="kiwi.table.header",
        border_style="kiwi.table.border",
    )
    table.add_column("URL", style="kiwi.path")
    table.add_column("Successes", justify="right", style="kiwi.success")
    table.add_column("Failures", justify="right", style="kiwi.error")
    table.add_column("Status")
    table.add_column("Source", style="kiwi.muted")
    for e in entries:
        status = "OK" if e.is_available else "Cooldown"
        source = "persisted" if e.url in persisted else "config"
        # A proxy URL routinely carries `user:pass@`; the table is printed to a
        # terminal that gets screenshotted and pasted into bug reports.
        # `_safe_source`, not bare `redact_text`: `proxy add`/`import` store the
        # string exactly as typed, and `redact_text` only matches credentials
        # AFTER a `scheme://`, so `alice:hunter2@1.2.3.4:8080` printed in full.
        table.add_row(_safe_source(e.url), str(e.successes), str(e.failures), status, source)
    _console.print(table)


@proxy_cmd.command("add", short_help="Add one or more proxies.")
@click.argument("urls", nargs=-1, required=True)
def proxy_add(urls: tuple[str, ...]) -> None:
    with pool_transaction() as pool:
        for u in urls:
            pool.add(u)
    print_success(f"Added {len(urls)} proxy/proxies.")


@proxy_cmd.command("remove", short_help="Remove a proxy by URL.")
@click.argument("url")
def proxy_remove(url: str) -> None:
    # Matching uses the RAW url; only what reaches the terminal is redacted.
    # A proxy URL routinely carries `user:pass@`, and both branches used to
    # print it verbatim.
    shown = _safe_source(url)
    removed = False
    with pool_transaction() as pool:
        removed = pool.remove(url)
    if removed:
        print_success(f"Removed {shown}")
    else:
        print_error(f"Not found: {shown}")


@proxy_cmd.command("clear", short_help="Remove all proxies.")
def proxy_clear() -> None:
    if not confirmed("Clear the entire proxy pool?"):
        return
    with pool_transaction() as pool:
        for entry in list(pool.list()):
            pool.remove(entry.url)
        pool.list_sources = []
    print_success("Proxy pool cleared (proxies and proxy lists).")


@proxy_cmd.command("import", short_help="Import proxies from a text file (one per line).")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def proxy_import(path: Path) -> None:
    added = 0
    with pool_transaction() as pool, open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pool.add(line)
            added += 1
    print_success(f"Imported {added} proxy/proxies.")


@proxy_cmd.command("add-list", short_help="Add a proxy list URL, fetched when a download starts.")
@click.argument("url")
@click.option(
    "--scheme",
    type=click.Choice(PROXY_SCHEMES),
    default="http",
    show_default=True,
    help="Scheme for the list's bare host:port lines; free lists are usually one kind.",
)
@click.pass_context
def proxy_add_list(ctx: click.Context, url: str, scheme: str) -> None:
    """Fill the pool from a proxy list, MegaBasterd-style.

    The list is fetched each time a download starts and again every hour while
    proxies are in use, or 30 s after none is left. kiwi-dl ships no lists.
    """
    try:
        source = validate_list(url, scheme)
    except ValueError as exc:
        print_error(str(exc))
        ctx.exit(2)
    with pool_transaction() as pool:
        pool.list_sources = [s for s in pool.list_sources if s.url != url] + [source]
    print_success(f"Added proxy list {_safe_source(url)} ({scheme}).")


@proxy_cmd.command("remove-list", short_help="Remove a proxy list URL.")
@click.argument("url")
def proxy_remove_list(url: str) -> None:
    with pool_transaction() as pool:
        before = len(pool.list_sources)
        pool.list_sources = [s for s in pool.list_sources if s.url != url]
        removed = len(pool.list_sources) != before
    if removed:
        print_success(f"Removed proxy list {_safe_source(url)}")
    else:
        print_error(f"Not found: {_safe_source(url)}")


def _print_lists(sources: list[ProxyList]) -> None:
    if not sources:
        return
    _console.print("Proxy lists (fetched when a download starts):", markup=False)
    for source in sources:
        _console.print(f"  {_safe_source(source.url)}  ({source.scheme})", markup=False)


_SECRETISH_PARAM = re.compile(r"(?i)(token|key|sig|hmac|secret|auth|pass|cred|session|sid|nonce)")


def _url_secrets(url: str) -> list[str]:
    """The literal secret substrings of a source URL, longest first.

    `redact_text` recognises secrets by SHAPE (`user:pass@host`, `token=x`).
    An exception message routinely echoes the same value stripped of that
    shape - urllib3 reports a bare `user:hunter2@host` with no scheme, and a
    signed query value can sit under any parameter name - and then no pattern
    matches. Here the raw values are known, so they are also removed by
    identity, after the shape pass.

    Short values are skipped: substituting a 4-character string everywhere
    would mangle ordinary words in the message for no real protection.

    The scheme is synthesised when absent: `kiwi-dl proxy add` and `kiwi-dl proxy import`
    store the string exactly as typed, and `urlsplit` reads a schemeless
    `alice:hunter2@host:8080` as `scheme='alice'` - so the password ended up in
    neither `parsed.password` nor `redact_text`'s `scheme://` pattern, and was
    printed verbatim.
    """
    try:
        parsed = urlsplit(url if "://" in url else f"//{url}")
    except ValueError:
        # Malformed input (a stray `[` parses as an unterminated IPv6 literal)
        # has no extractable credentials, and a display helper must not raise;
        # the shape-based pass in `_scrub` still applies.
        return []
    values = [parsed.password or "", parsed.username or ""]
    values += [v for k, v in parse_qsl(parsed.query) if _SECRETISH_PARAM.search(k)]
    return sorted({v for v in values if len(v) >= 5}, key=len, reverse=True)


def _scrub(text: str, secrets: list[str]) -> str:
    """Shape-based redaction first, then removal of known literal values."""
    text = redact_text(text)
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return text


def _safe_source(url: str) -> str:
    """The source URL as it may be shown: host and path kept, secrets gone."""
    return _scrub(url, _url_secrets(url))


def _safe_exc(exc: BaseException, secrets: list[str]) -> str:
    """One exception, reduced to a display-safe `Class: message`.

    Never bare `str(exc)`: `requests`/`urllib3`/`socket` faithfully repeat the
    address they were handed, so the message carries whatever credential the
    entry holds. The class name is kept because "it failed" is not diagnosable.
    """
    return f"{type(exc).__name__}: {_scrub(str(exc), secrets)}"


MAX_PROBE_THREADS = 256

# What a probe asks for through each proxy: plain HTTP, like a chunk download,
# to a MEGA host that answers without a link or any quota - with a 301 that
# carries MEGA's own CORS header. A dead proxy fails, and a captive portal or a
# proxy that answers by itself does not produce that header.
MEGA_PROBE_URL = "http://g.api.mega.co.nz/"
MEGA_SIGNATURE = "mega-chrome-antileak"

# More rows than this and the table is noise: the working ones are listed
# (fastest first) and the failures are counted by reason.
TABLE_ROWS = 50


def _probe(url: str, timeout: float) -> tuple[bool, str]:
    """Ask MEGA for one tiny answer through this proxy.

    Returns `(works, latency-or-reason)`. The reason is scrubbed HERE, not at
    print time, so a future consumer of the result cannot reintroduce the leak.
    An open port proves nothing about a free proxy: plenty answer and then
    refuse, or relay to their own login page, so the verdict is whether MEGA
    itself answered.
    """
    start = time.monotonic()
    # Schemeless is normal here: `proxy add`/`proxy import` store the line
    # exactly as typed, and `requests` would read `user:pass@host` as a scheme.
    proxy = url if "://" in url else f"http://{url}"
    try:
        with requests.get(
            MEGA_PROBE_URL,
            proxies={"http": proxy, "https": proxy},
            timeout=(timeout, timeout),
            allow_redirects=False,
            stream=True,
        ) as resp:
            signature = resp.headers.get("Access-Control-Allow-Headers", "").lower()
            status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        return False, _safe_exc(exc, _url_secrets(url))
    if MEGA_SIGNATURE not in signature:
        return False, f"answered HTTP {status}, but not as MEGA"
    return True, f"{(time.monotonic() - start) * 1000:.0f} ms"


_NOT_STARTED = object()  # still waiting for a worker when the run ended
_NO_ANSWER = object()  # probing, but no answer before the run ended


def _run_probes(urls: list[str], timeout: float, workers: int) -> list:
    """Probe `urls` with at most `workers` threads, within one deadline.

    A fixed set of worker threads takes the next URL from a shared queue: a
    proxy list is thousands of entries, and one thread per entry was thousands
    of threads at once. Daemon threads rather than a ThreadPoolExecutor:
    `concurrent.futures` joins its workers at interpreter exit, so one probe
    stuck in a connect or a DNS lookup held the process open for its whole
    timeout after the report was printed - and Ctrl-C printed "Aborted!" and
    then hung for the same minute.

    The run gets two probe timeouts (connect, then read) per WAVE of probes: a
    pool bigger than the thread cap queues the rest, and a single-timeout
    budget would end with queued entries never given a socket. The deadline
    still matters on its own, because neither timeout covers the
    `getaddrinfo` in front of the connect.
    """
    results: list = [_NOT_STARTED] * len(urls)
    queue = iter(range(len(urls)))
    take = threading.Lock()

    def worker() -> None:
        while True:
            with take:
                index = next(queue, None)
            if index is None:
                return
            results[index] = _NO_ANSWER
            results[index] = _probe(urls[index], timeout)

    count = max(1, min(workers, len(urls)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(count)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2 * timeout * -(-len(urls) // count)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return list(results)


def _verdict(result, timeout: float) -> tuple[bool, str]:
    # An entry that never got a socket must not be called dead: "no" next to a
    # working proxy is how someone deletes a good one.
    if result is _NOT_STARTED:
        return False, "not probed within the run budget"
    if result is _NO_ANSWER:
        return False, f"no answer within {timeout}s"
    ok, detail = result
    return bool(ok), str(detail)


def _reason_kind(detail: str) -> str:
    """A failure reason without the address in it, for counting."""
    return detail.split(":", 1)[0] if ":" in detail else detail


@proxy_cmd.command("test", short_help="Check which proxies can reach MEGA.")
@click.option(
    "--lists/--no-lists",
    default=True,
    help="Also fetch and test the proxy lists' entries (default on).",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Test only this many entries, picked at random (for long lists).",
)
@click.option(
    "--keep-working",
    is_flag=True,
    help="Afterwards, keep exactly the proxies that worked as your own proxies.",
)
@click.pass_context
def proxy_test(ctx: click.Context, lists: bool, limit: int | None, keep_working: bool) -> None:
    """Ask MEGA for a tiny answer through every proxy and report which ones work.

    The same plain-HTTP path a chunk download takes, with no link and no quota
    used. Exits non-zero when none works, so `kiwi-dl proxy test && kdl ...`
    only downloads when the pool can carry it.
    """
    from ..proxy.runtime import _urls_from_config, attach_list_refresher

    cfg = ctx.obj["config"]
    # Same pool `proxy list` shows: persisted entries plus `smart_proxy_url`.
    # Not `effective_pool`, which returns None under `smart_proxy_mode off` -
    # testing the pool before switching it on is part of the command's point.
    pool = _load_persisted_pool()
    for url in _urls_from_config(cfg):
        pool.add(url)
    if lists and pool.list_sources:
        for source, outcome in attach_list_refresher(
            pool, strict=cfg.force_smart_proxy
        ).refresh_now():
            got = f"{outcome} proxies" if isinstance(outcome, int) else f"not fetched ({outcome})"
            print_info(f"Proxy list {_safe_source(source.url)}: {got}")
    entries = pool.list()
    if limit is not None and limit < len(entries):
        entries = random.sample(entries, limit)
    if not entries:
        print_error("No proxies stored; nothing to test.")
        ctx.exit(1)

    timeout = cfg.smart_proxy_timeout_seconds
    workers = min(len(entries), MAX_PROBE_THREADS)
    if len(entries) > TABLE_ROWS:
        print_info(f"Testing {len(entries)} proxies, {workers} at a time ...")
    results = [
        _verdict(result, timeout)
        for result in _run_probes([e.url for e in entries], timeout, workers)
    ]
    working = [(e, detail) for e, (ok, detail) in zip(entries, results, strict=True) if ok]

    # SafeTable, not Table: these URLs come from `proxy import` (an arbitrary
    # file) or a third-party list, so one poisoned line with an unbalanced tag
    # would kill the render. `_safe_source`, not the raw url: a pooled proxy
    # routinely carries `user:pass@` and this table gets screenshotted.
    table = SafeTable(
        title="Proxies that reach MEGA",
        show_header=True,
        header_style="kiwi.table.header",
        border_style="kiwi.table.border",
    )
    table.add_column("URL", style="kiwi.path")
    table.add_column("Works")
    table.add_column("Latency / reason", style="kiwi.muted")
    if len(entries) <= TABLE_ROWS:
        for entry, (ok, detail) in zip(entries, results, strict=True):
            table.add_row(_safe_source(entry.url), "yes" if ok else "no", detail)
    else:
        fastest = sorted(working, key=lambda pair: float(pair[1].split()[0]))
        for entry, detail in fastest[:TABLE_ROWS]:
            table.add_row(_safe_source(entry.url), "yes", detail)
        failures: dict[str, int] = {}
        for ok, detail in results:
            if not ok:
                kind = _reason_kind(detail)
                failures[kind] = failures.get(kind, 0) + 1
        for kind, count in sorted(failures.items(), key=lambda item: -item[1]):
            table.add_row(f"({count} proxies)", "no", kind)
    _console.print(table)
    print_info(f"{len(working)}/{len(entries)} reach MEGA.")

    if keep_working:
        with pool_transaction() as stored:
            for entry in list(stored.list()):
                stored.remove(entry.url)
            for entry, _detail in working:
                stored.add(entry.url)
        print_success(f"Kept {len(working)} working proxies as your own.")
        if pool.list_sources:
            print_info(
                "Your proxy lists are still fetched on every download; "
                "`kiwi-dl proxy remove-list URL` to use only the kept ones."
            )
    if not working:
        # A reported failure never exits 0.
        ctx.exit(1)
