"""SmartProxy for chunk transfers: direct first, the proxy pool once MEGA says 509.

The behaviour follows MegaBasterd's SmartProxy (reimplemented, not ported):

* **Direct first.** A chunk answered with 509 - the exit IP's transfer quota -
  opens a proxy WINDOW for the whole command: until it closes, every download's
  chunks go through the pool. Then direct is tried again, and a fresh 509 opens
  a new window. The window lasts `smart_proxy_window_seconds`, cut to MEGA's
  `X-MEGA-Time-Left` when the quota resets sooner, and ends early when the
  local route changes (VPN reconnect, interface swap).
* **Sticky proxies.** Each worker keeps the proxy it was given until it fails.
* **Ban or exclude.** A proxy that answers 509 or any other HTTP error is
  banned for everyone for `smart_proxy_ban_seconds` (0: the rest of the run).
  A timeout, a dropped connection or a 429 only excludes it for the worker
  that saw it - it may be fine for the others.
* **Exhausted pool.** When a worker has no proxy left it goes direct (never
  under strict mode) and its exclusions are cleared for the next round.
* `always` keeps the window open from the start; `off` never uses the pool.
  Strict (`force_smart_proxy`) never goes direct at all.

A direct 509 that no proxy can take over is a quota BLOCK. Like MegaBasterd,
the command waits blocks out: each blocked worker re-tries direct every minute,
and at once when the local route changes. `quota_budget_seconds` bounds the
wait for the command - wall-clock time with at least one worker blocked: None
(the config default) waits for as long as it takes, and 0 stops at once with
the reset time. Every round of an exhausted pool ends in a direct attempt, and
a refused one waits or ends the download.

API calls are not routed here. An anonymous `a:g` keeps answering normally
while the quota is spent, so the API stays direct unless strict.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..core.errors import format_time_left
from ..utils.redaction import redact_text
from .selector import ProxyRequiredError

log = logging.getLogger(__name__)

SMART_PROXY_MODES = ("off", "on-509", "always")

# MegaBasterd's bounds for the 509 recheck window.
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 86400

# How often an open window re-reads the local route (one local syscall).
ROUTE_CHECK_SECONDS = 10.0

# While the command waits for the quota, it says so at most this often. Every
# blocked worker re-tries each minute; telling the user each time was one
# warning per worker per minute.
WAIT_NOTICE_SECONDS = 900.0


def _local_route() -> str | None:
    # Imported late: the downloader imports this module.
    from ..core.downloader import _local_route_fingerprint

    return _local_route_fingerprint()


class RouteAgain(Exception):  # noqa: N818 - control flow, not an error
    """This attempt's route is spent; pick a route again and retry at once.

    Raised for a proxy the router has just banned or excluded, and for a direct
    509 that a proxy can now take over. Caught by the chunk worker's own loop,
    never by the retry policy: changing route is not a transient fault and must
    not spend its attempts or its backoff.
    """


class SmartProxyPolicy:
    """The settings and shared state of SmartProxy for one command.

    One instance is shared by every download of the command, because the quota
    belongs to the exit IP: once one file hits it, all of them have.

    `budget_seconds` is how long quota blocks may be waited out; None is no
    limit. Built directly it defaults to 0, so the engine never waits unless
    asked; the commands pass the config's value, which defaults to None.
    """

    def __init__(
        self,
        pool=None,  # SmartProxyPool | None
        *,
        mode: str = "on-509",
        window_seconds: int = 3600,
        ban_seconds: int = 300,
        proxy_timeout_seconds: int = 5,
        strict: bool = False,
        budget_seconds: int | None = 0,
        clock: Callable[[], float] = time.monotonic,
        route_fingerprint: Callable[[], str | None] = _local_route,
    ):
        if mode not in SMART_PROXY_MODES:
            raise ValueError(f"smart proxy mode must be one of {SMART_PROXY_MODES}, got {mode!r}")
        self.pool = pool if mode != "off" else None
        self.mode = mode
        self.window_seconds = max(MIN_WINDOW_SECONDS, min(MAX_WINDOW_SECONDS, window_seconds))
        self.ban_seconds = max(0, ban_seconds)
        self.proxy_timeout_seconds = max(1, proxy_timeout_seconds)
        self.strict = strict
        self.budget_seconds = None if budget_seconds is None else max(0, budget_seconds)
        self._clock = clock
        self._route_fingerprint = route_fingerprint
        self._lock = threading.Lock()
        self._window_until = 0.0
        self._window_route: str | None = None
        self._route_checked_at = 0.0
        self._blocked = 0
        self._blocked_since = 0.0
        self._budget_used = 0.0
        self._wait_noticed_at: float | None = None

    @classmethod
    def from_config(cls, cfg, pool) -> SmartProxyPolicy:
        return cls(
            pool,
            mode=cfg.smart_proxy_mode,
            window_seconds=cfg.smart_proxy_window_seconds,
            ban_seconds=cfg.smart_proxy_ban_seconds,
            proxy_timeout_seconds=cfg.smart_proxy_timeout_seconds,
            strict=cfg.force_smart_proxy,
            budget_seconds=cfg.quota_budget_seconds,
        )

    def proxy_timeout(self, direct_timeout: float) -> tuple[float, float]:
        """`(connect, read)` timeouts for a chunk request through a pool proxy.

        A dead proxy is the normal case with a public list, and the general
        timeout (60 s) let one hold a worker for a minute. The connect timeout
        is `proxy_timeout_seconds`; the read timeout - the longest silence
        between bytes, not the whole chunk - is three times that, never more
        than the direct one, so a slow but working proxy is not cut off.
        """
        connect = float(self.proxy_timeout_seconds)
        return connect, min(float(direct_timeout), max(connect, 3 * connect))

    # -- the proxy window ---------------------------------------------------

    def proxies_wanted(self) -> bool:
        """Whether chunks should go through the pool right now."""
        if self.strict or self.mode == "always":
            return True
        return self.pool is not None and self._window_is_open()

    def _window_is_open(self) -> bool:
        with self._lock:
            now = self._clock()
            if now >= self._window_until:
                return False
            if now - self._route_checked_at < ROUTE_CHECK_SECONDS:
                return True
            self._route_checked_at = now
            baseline = self._window_route
        current = self._route_fingerprint()
        if baseline is not None and current is not None and current != baseline:
            log.info("Local route changed; trying direct again before the proxy window ends")
            self.close_window()
            return False
        return True

    def open_window(self, time_left: int | None) -> None:
        """A direct chunk got 509: send chunks through the pool for a while."""
        length = self.window_seconds
        if time_left is not None:
            length = max(MIN_WINDOW_SECONDS, min(length, time_left))
        route = self._route_fingerprint()
        with self._lock:
            now = self._clock()
            if now < self._window_until:
                return  # another worker opened it first
            self._window_until = now + length
            self._window_route = route
            self._route_checked_at = now
        log.warning(
            "MEGA transfer quota exceeded on the direct route; chunks go through the proxy "
            "pool for the next %s",
            format_time_left(length),
        )

    def close_window(self) -> None:
        with self._lock:
            self._window_until = 0.0

    # -- the quota budget ---------------------------------------------------

    def budget_left(self) -> float:
        """Seconds of quota blocks the command may still wait out (inf: no limit)."""
        if self.budget_seconds is None:
            return math.inf
        with self._lock:
            used = self._budget_used
            if self._blocked:
                used += self._clock() - self._blocked_since
            return self.budget_seconds - used

    def wait_notice_due(self) -> bool:
        """Whether this wait should tell the user; the rest only go to the log file.

        The first wait of the command says so, and then one every
        `WAIT_NOTICE_SECONDS` however many workers are waiting.
        """
        with self._lock:
            now = self._clock()
            last = self._wait_noticed_at
            if last is not None and now - last < WAIT_NOTICE_SECONDS:
                return False
            self._wait_noticed_at = now
            return True

    @contextmanager
    def blocked(self) -> Iterator[None]:
        """Account a worker's wait on a quota block against the budget.

        Wall-clock, not per worker: eight workers blocked for a minute spend
        one minute, not eight.
        """
        with self._lock:
            if self._blocked == 0:
                self._blocked_since = self._clock()
            self._blocked += 1
        try:
            yield
        finally:
            with self._lock:
                self._blocked -= 1
                if self._blocked == 0:
                    self._budget_used += self._clock() - self._blocked_since


@dataclass(frozen=True)
class Route:
    """Where one chunk request goes."""

    proxies: dict[str, str] | None  # the `proxies=` for requests; None = direct
    proxy: str | None = None  # the pool entry it goes through, if any
    fallback: bool = False  # direct only because the pool had nothing left


def _proxies_for(url: str) -> dict[str, str]:
    return {"http": url, "https": url}


def proxy_fingerprint(url: str) -> str:
    """A short, credential-free identity for a proxy: a hash of scheme://host:port.

    What a resume state stores to remember which proxy served a chunk. The
    user and password are left out before hashing, so the state file next to
    a download can neither reveal nor help guess them.
    """
    parts = urlsplit(url)
    endpoint = f"{parts.scheme}://{parts.hostname}:{parts.port}"
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


class ChunkRouter:
    """Per-download routing: which proxy each worker thread is on, and what it gave up on."""

    def __init__(self, policy: SmartProxyPolicy, static: dict[str, str] | None = None):
        self.policy = policy
        self.static = static or None  # a manual --proxy: the "direct" route goes through it
        self._lock = threading.Lock()
        self._sticky: dict[int, str] = {}
        self._excluded: dict[int, set[str]] = {}

    def route(self) -> Route:
        policy = self.policy
        pool = policy.pool
        worker = threading.get_ident()
        if pool is None or not policy.proxies_wanted():
            with self._lock:
                self._sticky.pop(worker, None)
            if policy.strict and not self.static:
                raise ProxyRequiredError(
                    message="force_smart_proxy is on but no proxy is available "
                    "(pool empty or exhausted, and no --proxy was given); "
                    "refusing to connect directly"
                )
            return Route(self.static)

        # Proxies are wanted: let the proxy lists refresh when they are due.
        if pool.refresher is not None:
            pool.refresher.tick()
        with self._lock:
            excluded = self._excluded.setdefault(worker, set())
            sticky = self._sticky.get(worker)
            if sticky is not None and sticky not in excluded and pool.is_usable(sticky):
                return Route(_proxies_for(sticky), sticky)
            entry = pool.pick(exclude=excluded)
            if entry is not None:
                self._sticky[worker] = entry.url
                return Route(_proxies_for(entry.url), entry.url)
            self._sticky.pop(worker, None)
            excluded.clear()
        if pool.refresher is not None:
            pool.refresher.ran_dry()
        if policy.strict:
            raise ProxyRequiredError(
                message="force_smart_proxy is on and every proxy in the pool is banned or "
                "failing; refusing to connect directly"
            )
        return Route(None, fallback=True)

    def ban_fingerprints(self, fingerprints: set[str], *, why: str) -> None:
        """Ban every pool proxy whose `proxy_fingerprint` is in `fingerprints`."""
        pool = self.policy.pool
        if pool is None:
            return
        for entry in pool.list():
            if proxy_fingerprint(entry.url) in fingerprints:
                self.proxy_failed(Route(_proxies_for(entry.url), entry.url), ban=True, why=why)

    def proxy_succeeded(self, route: Route) -> None:
        if route.proxy is not None and self.policy.pool is not None:
            self.policy.pool.report_success(route.proxy)

    def proxy_failed(self, route: Route, *, ban: bool, why: str) -> None:
        """Take `route.proxy` off this worker; with `ban`, off every worker."""
        if route.proxy is None or self.policy.pool is None:
            return
        worker = threading.get_ident()
        with self._lock:
            if self._sticky.get(worker) == route.proxy:
                del self._sticky[worker]
            if not ban:
                self._excluded.setdefault(worker, set()).add(route.proxy)
        shown, why = redact_text(route.proxy), redact_text(why)
        if ban:
            self.policy.pool.ban(route.proxy, self.policy.ban_seconds)
            length = self.policy.ban_seconds
            log.info(
                "Proxy %s banned %s: %s",
                shown,
                f"for {format_time_left(length)}" if length else "for the rest of this run",
                why,
            )
        else:
            log.debug("Proxy %s set aside by one worker: %s", shown, why)
