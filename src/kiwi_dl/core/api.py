"""Low-level MEGA API client.

This module handles raw JSON-RPC requests to MEGA's API endpoints. Higher-level
operations (file info, folder listings, downloads) build on top of this.

MEGA uses a simple request/response JSON protocol:
- POST https://g.api.mega.co.nz/cs
- Body is a JSON array of command objects
- Response is a JSON array of result objects (or a single negative int on error)

A sequence number (`id`) and app key (`ak`) are passed as query params.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import time
from typing import Any

import requests

from ..proxy.smart_proxy import ProxyRequiredError
from .bounded_read import PayloadTooLargeError, read_bounded_text
from .errors import MegaError, RateLimitError, raise_for_code
from .inflight import SeverableResponses

# Bounded exponential backoff for transient API errors (2s, 4s, 8s, 16s, 30s).
# `_retry_delay` is read at call time so tests can zero out sleeps cleanly.
RETRY_ATTEMPTS = 5


def _retry_delay(attempt: int) -> float:
    return min(30.0, float(2 * (2**attempt)))


def _expect_mapping(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise MegaError(
            message=f"Unexpected MEGA response for {what}: "
            f"expected an object, got {type(value).__name__}"
        )
    return value


class HashcashBudgetExceededError(MegaError):
    """One request spent its whole hashcash allowance without getting through.

    The 402 retry loop solves a FRESH challenge per attempt, so a per-solve
    timeout was multiplied by the attempt count: an attacker-chosen easiness
    could hold a single API call for minutes of full-CPU work. The budget is
    now computed once per request and shared by every attempt.
    """


log = logging.getLogger(__name__)


API_BASE_URL = "https://g.api.mega.co.nz"
DEFAULT_TIMEOUT = 30
DEFAULT_APP_KEY = "BdARkQSQ"

# A MEGA `f` listing for a huge account is the largest legitimate response and
# stays well under this. Anything bigger is a proxy error page, a redirect
# chain or a hostile body, and must not be handed to the JSON parser.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# Total wall-clock one request may spend on hashcash, ACROSS all 402 retries.
# hashcash.DEFAULT_TIMEOUT_S is the per-solve cap for direct callers; this is
# the cap for the whole phase, so extra attempts cost patience, not minutes.
HASHCASH_TOTAL_BUDGET_S = 30.0

# HTTP statuses that are the PROXY's own refusal rather than MEGA's answer.
# Every other 4xx/5xx came from the origin and says nothing about proxy health:
# blaming the proxy for those is how three MEGA-side 503s used to put a
# blameless proxy on a 60s cooldown (and, under force_smart_proxy, turned the
# next call into a ProxyRequiredError).
PROXY_FAULT_STATUSES = frozenset({407})


def _parse_body(response) -> Any:
    """Return the parsed JSON body, or raise MegaError if it is not usable.

    Guards the boundary in the order that costs least: declared size, then
    content type, then the actual parse. A captive portal answering HTTP 200
    with an HTML login page fails here with a typed, actionable error instead
    of surfacing later as `Error: 'p'` from the CLI catch-all.
    """
    headers = getattr(response, "headers", None) or {}
    declared = str(headers.get("Content-Length") or "")
    # `isdecimal()`, not `isdigit()`: the latter is True for superscripts and
    # other Unicode digit forms that `int()` refuses, so it would let a header
    # through and then raise ValueError out of the very guard meant to bound
    # the read. This header comes from the network. A value that is not a plain
    # decimal now simply fails the check, and `_read_bounded` still caps the
    # body that actually lands in memory - the declared size was only ever the
    # cheap first look.
    if declared.isdecimal() and int(declared) > MAX_RESPONSE_BYTES:
        raise MegaError(
            message=f"MEGA API response too large ({declared} bytes); refusing to parse it"
        )
    content_type = str(headers.get("Content-Type") or "")
    if content_type and "json" not in content_type.lower():
        raise MegaError(
            message=(
                f"MEGA API returned {content_type!r} instead of JSON "
                "(a proxy or captive portal may have replaced the response)"
            )
        )
    try:
        return json.loads(_read_bounded(response))
    except ValueError as exc:
        raise MegaError(message="MEGA API returned a body that is not valid JSON") from exc


def _read_bounded(response) -> str:
    """Return the body as text, refusing to buffer more than MAX_RESPONSE_BYTES.

    Requires `stream=True`. Reading `response.content`/`.json()` first and
    checking the length afterwards is no defence at all: by then `requests` has
    downloaded the whole body AND transparently inflated any
    `Content-Encoding: gzip`, so a bomb is fully materialised before the cap is
    consulted. `iter_content` yields the DECODED bytes, so the cap applies to
    what actually lands in memory and an oversized body is dropped mid-read.

    The cap is read from the module global on every call, so tests (and any
    future runtime override) can lower it by patching `MAX_RESPONSE_BYTES`.
    """
    try:
        return read_bounded_text(response, MAX_RESPONSE_BYTES)
    except PayloadTooLargeError as exc:
        raise MegaError(message="MEGA API response too large; refusing to parse it") from exc


def default_user_agent() -> str:
    """Default User-Agent derived from the package version (no drift)."""
    from .. import __version__

    return f"kiwi-dl/{__version__}"


# One registry for every API session in the process: a stop severs the
# responses still being read, whatever download or command they belong to.
_inflight_api = SeverableResponses()


def sever_inflight_api() -> None:
    """Shut down the socket of every API request still being read.

    A stop must not wait out the API timeout: a worker refreshing a CDN URL
    sits in a blocked read that only severing ends - the same primitive the
    chunk-request cut-off uses. Safe to call when nothing is in flight.
    """
    _inflight_api.sever_all()


class MegaAPIClient:
    """Stateful client for the MEGA JSON-RPC API.

    Maintains a sequence counter and automatically retries transient errors.
    """

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        proxies: dict[str, str] | None = None,
        api_base: str = API_BASE_URL,
        proxy_pool=None,  # SmartProxyPool | None
        force_proxy: bool = False,
        user_agent: str | None = None,
    ):
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")
        self.user_agent = user_agent or default_user_agent()
        self._seq = random.randint(0, 0xFFFFFFFF)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})
        if proxies:
            self._session.proxies.update(proxies)
        # Smart proxy pool: if set, each API request also picks a proxy and
        # reports back to the pool. `force_proxy` disallows direct connections.
        self.proxy_pool = proxy_pool
        self.force_proxy = force_proxy
        self._static_proxies = dict(proxies) if proxies else None
        # Non-secret identity instrumentation: lets tests and -vv runs prove
        # that independent parallel transfers use distinct client/session
        # objects.
        log.debug("MegaAPIClient created client_id=%s session_id=%s", id(self), id(self._session))

    def clone(self) -> MegaAPIClient:
        """Return an independent client sharing configuration.

        The clone owns its own `requests.Session` and sequence counter, so
        parallel transfers never share mutable HTTP/sequence state. The proxy
        pool object is intentionally shared (it is explicitly thread-safe).
        """
        return MegaAPIClient(
            timeout=self.timeout,
            proxies=self._static_proxies,
            api_base=self.api_base,
            proxy_pool=self.proxy_pool,
            force_proxy=self.force_proxy,
            user_agent=self.user_agent,
        )

    def _request_proxies(self) -> tuple[dict[str, str] | None, str | None]:
        """Per-request proxy decision: pool pick > static --proxy > direct (or refuse)."""
        if self.proxy_pool is not None:
            entry = self.proxy_pool.pick()
            if entry is not None:
                return {"http": entry.url, "https": entry.url}, entry.url
        if self._static_proxies:
            return self._static_proxies, None
        if self.force_proxy:
            raise ProxyRequiredError(
                message="force_smart_proxy is on but no proxy is available "
                "(pool empty or exhausted, and no --proxy was given); "
                "refusing to connect directly"
            )
        return None, None

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> MegaAPIClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    def _build_url(self, extra_params: dict[str, str] | None = None) -> str:
        self._seq += 1
        params = [f"id={self._seq}", f"ak={DEFAULT_APP_KEY}"]
        if extra_params:
            for k, v in extra_params.items():
                params.append(f"{k}={v}")
        return f"{self.api_base}/cs?" + "&".join(params)

    def request(
        self,
        commands: list[dict[str, Any]] | dict[str, Any],
        extra_params: dict[str, str] | None = None,
    ) -> Any:
        """Send an API request with bounded retries on transient errors."""
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return self._send(commands, extra_params)
            except (RateLimitError, requests.ConnectionError, requests.Timeout):
                if attempt + 1 >= RETRY_ATTEMPTS:
                    raise
                delay = _retry_delay(attempt)
                if delay > 0:
                    time.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _send(
        self,
        commands: list[dict[str, Any]] | dict[str, Any],
        extra_params: dict[str, str] | None = None,
    ) -> Any:
        """Send a JSON-RPC request and return the parsed response.

        If a single command is sent, the single result is returned.
        If a list of commands is sent, a list of results is returned.
        Negative API codes are converted into MegaError exceptions.

        If MEGA replies HTTP 402 with an `X-Hashcash` challenge, the request is
        transparently retried with a solved nonce.
        """
        single = not isinstance(commands, list)
        payload = [commands] if single else commands

        url = self._build_url(extra_params)
        log.debug("MEGA API request: %s -> %s", url, payload)

        extra_headers: dict[str, str] = {}
        request_proxies, picked_proxy = self._request_proxies()
        # ONE deadline for the whole hashcash phase. Computed here, not per
        # attempt, so three challenges cost one budget between them.
        hashcash_deadline = time.monotonic() + HASHCASH_TOTAL_BUDGET_S
        try:
            for _attempt in range(3):  # Initial request plus up to two hashcash retries
                response = self._session.post(
                    url,
                    json=payload,
                    timeout=self.timeout,
                    headers=extra_headers,
                    proxies=request_proxies,
                    stream=True,
                )
                _inflight_api.add(response)
                if response.status_code == 402 and "X-Hashcash" in response.headers:
                    from .hashcash import build_solution_header

                    challenge = response.headers["X-Hashcash"]
                    _inflight_api.discard(response)
                    response.close()  # nothing in this body is read; free the connection
                    remaining = hashcash_deadline - time.monotonic()
                    if remaining <= 0:
                        raise HashcashBudgetExceededError(
                            message=(
                                "MEGA kept demanding hashcash proof-of-work and the "
                                f"{HASHCASH_TOTAL_BUDGET_S:g}s budget for this request ran out"
                            )
                        )
                    log.info("Solving MEGA hashcash challenge: %s", challenge.split(":", 2)[:2])
                    try:
                        extra_headers["X-Hashcash"] = build_solution_header(
                            challenge, timeout=remaining
                        )
                    except TimeoutError as exc:
                        raise HashcashBudgetExceededError(
                            message=(
                                "The MEGA hashcash challenge was not solved within the "
                                f"{HASHCASH_TOTAL_BUDGET_S:g}s budget for this request"
                            )
                        ) from exc
                    continue
                break
        except (requests.ConnectionError, requests.Timeout):
            self._blame_proxy(picked_proxy)
            raise

        # The proxy is only credited once the body is proven usable: a captive
        # portal answering 200 with HTML was otherwise rewarded on every
        # request, and SmartProxyPool.pick weights by success ratio - so the
        # broken proxy became progressively PREFERRED. The response is streamed,
        # so it must be closed on the rejection paths too or urllib3 never gets
        # the connection back.
        try:
            response.raise_for_status()
            data = _parse_body(response)
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in PROXY_FAULT_STATUSES:
                self._blame_proxy(picked_proxy)
            raise
        except MegaError:
            self._blame_proxy(picked_proxy)
            raise
        finally:
            with contextlib.suppress(Exception):
                response.close()
            _inflight_api.discard(response)
        if picked_proxy and self.proxy_pool is not None:
            self.proxy_pool.report_success(picked_proxy)

        log.debug("MEGA API response: %s", data)

        # Top-level negative integer = global error
        if isinstance(data, int) and not isinstance(data, bool):
            raise_for_code(data)
            raise MegaError(data)

        if not isinstance(data, list):
            raise MegaError(
                message=(
                    "MEGA API returned a "
                    f"{type(data).__name__} where an array of results was expected"
                )
            )

        # Each element may be a negative integer error.
        results = []
        for item in data:
            if isinstance(item, int) and not isinstance(item, bool) and item < 0:
                raise_for_code(item)
            results.append(item)

        if single and not results:
            raise MegaError(message="MEGA API returned an empty result array")
        return results[0] if single else results

    def _blame_proxy(self, picked_proxy: str | None) -> None:
        """Count a failure against the proxy that actually caused it."""
        if picked_proxy and self.proxy_pool is not None:
            self.proxy_pool.report_failure(picked_proxy)

    # ------------------------------------------------------------------
    # Convenience methods for common API actions
    # ------------------------------------------------------------------
    def get_public_file_info(self, public_id: str) -> dict:
        """Get metadata for a public file link (returns size, encrypted attrs, download URL)."""
        return _expect_mapping(
            self.request({"a": "g", "g": 1, "p": public_id}), "get_public_file_info"
        )

    def get_public_folder_listing(self, public_id: str) -> dict:
        """Get the node listing inside a public folder."""
        return _expect_mapping(
            self.request({"a": "f", "c": 1, "r": 1, "ca": 1}, extra_params={"n": public_id}),
            "get_public_folder_listing",
        )
