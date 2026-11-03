"""`kiwi-dl proxy` - manage the Smart Proxy pool."""

from __future__ import annotations

import json
import re
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import click

from ..proxy.runtime import _load_persisted_pool, _pool_path
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
        payload = json.dumps({"proxies": [e.url for e in pool.list()]}, indent=2)
        atomic_write_text(path, payload)
    finally:
        lock.release()


@click.group("proxy", short_help="Manage the Smart Proxy pool.")
def proxy_cmd() -> None:
    """Add, remove, list proxies in the Smart Proxy pool."""


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
    if not entries:
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
    print_success("Proxy pool cleared.")


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


MAX_PROBE_THREADS = 64


def _probe(url: str, timeout: float) -> tuple[bool, str]:
    """Open and immediately close a bounded TCP connection to one proxy.

    Returns `(reachable, latency-or-reason)`. The reason is scrubbed HERE, not
    at print time, so a future consumer of the result cannot reintroduce the
    leak.

    TCP reachability only. It proves the port answers, not that the peer
    speaks HTTP CONNECT or SOCKS5 - but a dead host is the commonest failure
    of a proxy list, and a real handshake
    needs a third-party host to tunnel to, i.e. this command would have to send
    someone else's server traffic to find out. Add the handshake if "answers
    but does not proxy" shows up in practice.
    """
    start = time.monotonic()
    try:
        # Schemeless is normal here: `proxy add`/`proxy import` store the line
        # exactly as typed, and `urlsplit` reads a bare `host:8080` as a scheme.
        parsed = urlsplit(url if "://" in url else f"//{url}")
        host, port = parsed.hostname, parsed.port
        if not host or not port:
            return False, "no host:port in the stored entry"
        socket.create_connection((host, port), timeout).close()
    except Exception as exc:  # noqa: BLE001
        return False, _safe_exc(exc, _url_secrets(url))
    return True, f"{(time.monotonic() - start) * 1000:.0f} ms"


@proxy_cmd.command("test", short_help="Probe every proxy in the pool for reachability.")
@click.pass_context
def proxy_test(ctx: click.Context) -> None:
    """Probe every pooled proxy concurrently and report which ones answer.

    Exits non-zero when nothing is reachable, so `kiwi-dl proxy test && kiwi-dl download
    ...` runs the transfer only when the pool can actually carry it.
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    from ..proxy.runtime import _urls_from_config

    cfg = ctx.obj["config"]
    # Same pool `proxy list` shows: persisted entries plus `smart_proxy_url`.
    # Not `effective_pool`, which returns None under `smart_proxy_mode off` -
    # testing the pool before switching it on is part of the command's point.
    pool = _load_persisted_pool()
    for url in _urls_from_config(cfg):
        pool.add(url)
    entries = pool.list()
    if not entries:
        print_error("No proxies stored; nothing to test.")
        ctx.exit(1)

    # The project's existing per-request timeout is each probe's connect
    # deadline, and the whole run gets one deadline per WAVE of probes. A pool
    # bigger than the thread cap queues the remainder, so a single-timeout run
    # budget would expire with entries that were never given a socket - and
    # report them as dead. The run deadline still earns its keep on its own:
    # `create_connection` applies its timeout to the connect and never to the
    # `getaddrinfo` in front of it, so one unresolvable host would otherwise
    # hang the command outright.
    timeout = cfg.timeout_seconds
    workers = min(len(entries), MAX_PROBE_THREADS)
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [executor.submit(_probe, e.url, timeout) for e in entries]
        wait(futures, timeout=timeout * -(-len(entries) // workers))
    finally:
        # Not `with`, whose __exit__ joins the workers: the entire point of the
        # deadline is to return the report while a probe is still stuck.
        executor.shutdown(wait=False, cancel_futures=True)

    # SafeTable, not Table, for the same reason as `proxy list`: these URLs come
    # from `proxy import` (an arbitrary file), so one poisoned line with an
    # unbalanced tag would kill the render.
    table = SafeTable(
        title="Proxy Reachability",
        show_header=True,
        header_style="kiwi.table.header",
        border_style="kiwi.table.border",
    )
    table.add_column("URL", style="kiwi.path")
    table.add_column("Reachable")
    table.add_column("Latency / reason", style="kiwi.muted")
    reachable = 0
    for entry, future in zip(entries, futures, strict=True):
        # `_safe_source`, not the raw url: a pooled proxy routinely carries
        # `user:pass@` and this table gets screenshotted into bug reports.
        # `done()` alone is a trap: shutdown cancels every still-queued probe,
        # and a CANCELLED future is `done()` - so `.result()` raised
        # CancelledError and destroyed the whole report, the probes that DID
        # answer included. A cancelled entry also must not be called dead: it
        # was never given a socket, and "no" next to a working proxy is how
        # someone deletes a good one.
        if future.cancelled():
            ok, detail = False, "not probed within the run budget"
        elif future.done():
            ok, detail = future.result()
        else:
            ok, detail = False, f"no answer within {timeout}s"
        reachable += ok
        table.add_row(_safe_source(entry.url), "yes" if ok else "no", detail)
    _console.print(table)
    print_info(f"{reachable}/{len(entries)} reachable.")
    if not reachable:
        # A reported failure never exits 0.
        ctx.exit(1)
