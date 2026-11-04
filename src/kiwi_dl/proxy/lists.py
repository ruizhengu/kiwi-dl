"""Proxy lists: URLs you add, fetched and parsed into pool entries.

MegaBasterd's SmartProxy runs on free public proxy lists that its user points
it at (`#https://...` lines in its custom proxy list), refreshed every hour and
again shortly after the pool runs dry. kiwi-dl does the same with the lists
you add with `kiwi-dl proxy add-list`. It ships none.

A list is untrusted input from a third party, so it is read with a size cap,
parsed line by line with strict rules, and capped in entries. Every chunk
request may go through one of these addresses, so an entry naming a private,
loopback or otherwise non-public IP is dropped: a hostile list must not be able
to aim requests at the user's own network. Free lists are mostly
`ip:port` with no scheme - one list is all HTTP proxies, another all SOCKS5 -
so each list carries the scheme to give its bare lines.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from ..core.bounded_read import read_bounded_text
from ..utils.redaction import redact_text

log = logging.getLogger(__name__)

PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")

# A real list is tens of thousands of short lines at most (TheSpeedX's SOCKS5
# list was ~3,650 lines, ~70 KB, in Nov 2026).
MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_LIST_ENTRIES = 20_000
MAX_LINE_CHARS = 300
FETCH_TIMEOUT = (10.0, 20.0)
MAX_REDIRECTS = 5

# MegaBasterd: PROXY_AUTO_REFRESH_TIME = 60 (minutes), and
# PROXY_AUTO_REFRESH_SLEEP_TIME = 30 (seconds) before refetching a dry pool.
REFRESH_SECONDS = 3600.0
DRY_RETRY_SECONDS = 30.0

_HOSTNAME = re.compile(
    r"\A(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


@dataclass(frozen=True)
class ProxyList:
    """A list URL and the scheme its bare `host:port` lines get."""

    url: str
    scheme: str = "http"


def validate_list(url: str, scheme: str) -> ProxyList:
    """A ProxyList, or ValueError with a reason fit to show the user."""
    if scheme not in PROXY_SCHEMES:
        raise ValueError(f"scheme must be one of {', '.join(PROXY_SCHEMES)}")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("a proxy list URL must be an http:// or https:// address")
    return ProxyList(url=url, scheme=scheme)


def _public_host(host: str) -> bool:
    """A hostname, or an IP address that is reachable on the public internet."""
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return bool(_HOSTNAME.match(host))


def parse_entry(line: str, scheme: str) -> str | None:
    """One list line as `scheme://host:port` (credentials kept), or None to skip it."""
    line = line.strip()
    if not line or line.startswith("#") or len(line) > MAX_LINE_CHARS:
        return None
    if any(ch.isspace() for ch in line):
        return None
    try:
        if "://" in line:
            parts = urlsplit(line)
            scheme = parts.scheme.lower()
            if scheme not in PROXY_SCHEMES or parts.path not in ("", "/"):
                return None
            if parts.query or parts.fragment:
                return None
            host, port = parts.hostname, parts.port
            userinfo = parts.netloc.rpartition("@")[0]
        else:
            host, sep, port_text = line.rpartition(":")
            if not sep or not port_text.isdecimal() or "@" in host or ":" in host:
                return None
            port, userinfo = int(port_text), ""
    except ValueError:  # an out-of-range port or a malformed netloc
        return None
    if not host or port is None or not 1 <= port <= 65535 or not _public_host(host):
        return None
    credentials = f"{userinfo}@" if userinfo else ""
    return f"{scheme}://{credentials}{host}:{port}"


def parse_proxy_list(text: str, scheme: str) -> list[str]:
    """Every usable entry in a fetched list, de-duplicated, at most MAX_LIST_ENTRIES."""
    found: dict[str, None] = {}
    for line in text.splitlines():
        entry = parse_entry(line, scheme)
        if entry is not None:
            found[entry] = None
            if len(found) >= MAX_LIST_ENTRIES:
                break
    return list(found)


def fetch_proxy_list(
    source: ProxyList,
    *,
    proxies: dict[str, str] | None = None,
    timeout: tuple[float, float] = FETCH_TIMEOUT,
) -> list[str]:
    """Download and parse one list. Raises on a transport error, an HTTP error
    or a body past MAX_LIST_BYTES."""
    with requests.Session() as session:
        session.max_redirects = MAX_REDIRECTS
        with session.get(source.url, timeout=timeout, stream=True, proxies=proxies) as resp:
            resp.raise_for_status()
            text = read_bounded_text(resp, MAX_LIST_BYTES, what="Proxy list")
    return parse_proxy_list(text, source.scheme)


def shown(url: str) -> str:
    """A list URL as it may be logged: host and path kept, credentials gone."""
    return redact_text(url)


class ListRefresher:
    """Keeps a pool's list-sourced entries fresh for the life of one command.

    `refresh_now` fetches every list; `tick` (called whenever a chunk wants a
    proxy) starts a background refresh once an hour; `ran_dry` (called when a
    worker finds no usable proxy) brings the next one forward to 30 seconds
    later. A list that fails or comes back empty keeps its previous entries.
    """

    def __init__(
        self,
        pool,  # SmartProxyPool
        sources: list[ProxyList],
        *,
        fetch: Callable[..., list[str]] = fetch_proxy_list,
        proxies: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.pool = pool
        self.sources = list(sources)
        self._fetch = fetch
        self._proxies = proxies
        self._clock = clock
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last: float | None = None
        self._dry_since: float | None = None

    def refresh_now(self) -> list[tuple[ProxyList, int | str]]:
        """Fetch every list; return `(list, entries or reason)` for each."""
        results: list[tuple[ProxyList, int | str]] = []
        for source in self.sources:
            try:
                entries = self._fetch(source, proxies=self._proxies)
            except Exception as exc:  # noqa: BLE001 - one bad list must not stop the others
                reason = f"{type(exc).__name__}: {redact_text(str(exc))}"
                log.warning(
                    "Could not fetch the proxy list %s (%s); keeping the previous one",
                    shown(source.url),
                    reason,
                )
                results.append((source, reason))
                continue
            if not entries:
                log.warning(
                    "The proxy list %s had no usable entries; keeping the previous one",
                    shown(source.url),
                )
                results.append((source, "no usable entries"))
                continue
            self.pool.replace_source(source.url, entries)
            log.info("Proxy list %s: %d proxies", shown(source.url), len(entries))
            results.append((source, len(entries)))
        with self._lock:
            self._last = self._clock()
            self._dry_since = None
        return results

    def tick(self) -> None:
        """Start a background refresh if one is due and none is running."""
        with self._lock:
            if self._running:
                return
            now = self._clock()
            due = self._last is None or now - self._last >= REFRESH_SECONDS
            if self._dry_since is not None and now - self._dry_since >= DRY_RETRY_SECONDS:
                due = True
            if not due:
                return
            self._running = True
            self._thread = threading.Thread(target=self._refresh_in_background, daemon=True)
        self._thread.start()

    def ran_dry(self) -> None:
        """No usable proxy left: refetch the lists 30 seconds from now."""
        with self._lock:
            if self._dry_since is None:
                self._dry_since = self._clock()
        self.tick()

    def _refresh_in_background(self) -> None:
        try:
            self.refresh_now()
        finally:
            with self._lock:
                self._running = False
