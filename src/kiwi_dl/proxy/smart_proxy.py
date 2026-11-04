"""Smart proxy: per-request proxy selection with health tracking.

The Smart Proxy concept from the original MegaBasterd: when the user's IP is
rate-limited or blocked by MEGA, route some chunk requests through a configured
proxy or proxy pool. Healthy proxies are preferred; unhealthy ones are cooled
down for a short period.

Entries come from the user (`proxy add`/`import`, `smart_proxy_url`) or from
the proxy lists they added, which can be thousands of entries; lookups by URL
go through an index, not a scan.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from ..utils.redaction import redact_text
from .lists import ListRefresher, ProxyList

log = logging.getLogger(__name__)


@dataclass
class ProxyEntry:
    url: str  # e.g. http://1.2.3.4:8080  or  socks5://user:pass@host:1080
    successes: int = 0
    failures: int = 0
    cooldown_until: float = 0
    source: str = ""  # "" for an entry the user added; else the list URL it came from

    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.cooldown_until


@dataclass
class SmartProxyPool:
    """Thread-safe pool of proxies with health tracking.

    `list_sources` are the proxy lists the user added; their entries are
    fetched per command (see `lists.ListRefresher`, set as `refresher`) and
    never saved, so only entries with an empty `source` are persisted.
    """

    COOLDOWN_SECONDS = 60
    MAX_FAILURES = 3

    proxies: Iterable[str] = ()
    list_sources: list[ProxyList] = field(default_factory=list)
    refresher: ListRefresher | None = None  # set when lists are in use

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, ProxyEntry] = {}
        for url in self.proxies:
            self._entries.setdefault(url, ProxyEntry(url=url))

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------
    def add(self, url: str, source: str = "") -> None:
        with self._lock:
            self._entries.setdefault(url, ProxyEntry(url=url, source=source))

    def remove(self, url: str) -> bool:
        with self._lock:
            return self._entries.pop(url, None) is not None

    def list(self) -> list[ProxyEntry]:
        with self._lock:
            return list(self._entries.values())

    def replace_source(self, source: str, urls: Iterable[str]) -> None:
        """Make the entries from list `source` exactly `urls`.

        Entries that stay keep their counters and any ban, so a refresh cannot
        un-ban a proxy early; an entry the user added themselves is never
        touched, even if a list also names it.
        """
        wanted = dict.fromkeys(urls)
        with self._lock:
            for url, entry in list(self._entries.items()):
                if entry.source == source and url not in wanted:
                    del self._entries[url]
            for url in wanted:
                self._entries.setdefault(url, ProxyEntry(url=url, source=source))

    # ------------------------------------------------------------------
    # Selection and feedback
    # ------------------------------------------------------------------
    def pick(self, exclude: Collection[str] = ()) -> ProxyEntry | None:
        """Pick a random available proxy (preferring healthier ones).

        `exclude` leaves out entries one caller has given up on without
        benching them for everyone (see `ChunkRouter`).
        """
        with self._lock:
            available = [
                e for e in self._entries.values() if e.is_available and e.url not in exclude
            ]
            if not available:
                return None
            # Weight by success ratio (Laplace smoothing avoids div-by-zero)
            weights = [(e.successes + 1) / (e.successes + e.failures + 2) for e in available]
            return random.choices(available, weights=weights, k=1)[0]

    def report_success(self, url: str) -> None:
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None:
                entry.successes += 1
                entry.failures = max(0, entry.failures - 1)

    def is_usable(self, url: str) -> bool:
        """True while `url` is in the pool and not banned or cooling down."""
        with self._lock:
            entry = self._entries.get(url)
            return entry is not None and entry.is_available

    def ban(self, url: str, seconds: float) -> None:
        """Bench `url` for every caller for `seconds`; 0 means for good.

        "For good" is the life of this pool object, i.e. the current command:
        the pool is rebuilt from disk on every run.
        """
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None:
                entry.failures += 1
                entry.cooldown_until = time.monotonic() + seconds if seconds > 0 else math.inf

    def report_failure(self, url: str) -> None:
        with self._lock:
            entry = self._entries.get(url)
            if entry is None:
                return
            entry.failures += 1
            if entry.failures >= self.MAX_FAILURES:
                entry.cooldown_until = time.monotonic() + self.COOLDOWN_SECONDS
                # Redacted: a pooled URL routinely carries `user:pass@`.
                log.info("Proxy %s on cooldown for %ds", redact_text(url), self.COOLDOWN_SECONDS)
