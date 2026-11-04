"""One chunk over the wire: HTTP Range request, guards, decrypt, write, MAC.

This is the per-attempt body only. The retry POLICY that decides whether a
failure here is worth replaying lives with the downloader
(`_is_transient_chunk_failure`), because the distinction between a transient
fault and a deterministic refusal is what that predicate exists to encode.

Every failure on a pool proxy is reported to the download's `ChunkRouter`,
which bans the proxy (an HTTP error, a mangled range) or sets it aside for this
worker (a timeout, a dropped connection, a 429), and the attempt is retried at
once on a new route. CDN URL expiry is the exception - that is the URL ageing
out, not the proxy misbehaving. A direct 509 goes to the downloader, which
opens the proxy window, waits from the quota budget, or stops the download.
"""

from __future__ import annotations

import logging

import requests

from ..proxy.quota_router import Route, RouteAgain, proxy_fingerprint
from .chunks import Chunk, chunk_mac
from .crypto import ctr_offset_to_counter, make_ctr_cipher
from .errors import NonRetryableTransferError, RetryableTransferError, TransferError
from .range_validation import RangeNotHonoredError, validate_range_response
from .state import TransferState, save_state, snapshot_state

log = logging.getLogger(__name__)


# HTTP status codes that indicate the CDN URL has expired or is unusable
# and should be re-fetched rather than retried as-is. 509 used to be here: it is
# the exit IP's transfer quota, and a fresh URL is refused just the same.
URL_EXPIRY_STATUS = {403, 410}

# MEGA's storage answer when the exit IP's transfer quota is spent.
QUOTA_EXCEEDED_STATUS = 509


def quota_time_left(headers) -> int | None:
    """Seconds until the quota resets, from `X-MEGA-Time-Left`, or None."""
    raw = str(headers.get("X-MEGA-Time-Left") or "").strip()
    return int(raw) if raw.isdecimal() else None


class CdnUrlExpired(TransferError):  # noqa: N818 - internal retry sentinel name
    """Raised when the CDN URL has expired and needs to be refreshed."""


def _proxy_fault(dl, route: Route, *, ban: bool, why: str) -> None:
    """Bench the proxy this attempt went through and retry on a new route."""
    dl._router.proxy_failed(route, ban=ban, why=why)
    raise RouteAgain(why)


def fetch_chunk(
    dl,  # MegaDownloader
    chunk: Chunk,
    aes_key: bytes,
    nonce: bytes,
    destination,
    state: TransferState,
) -> None:
    """Download one chunk, decrypt, write, MAC, update `dl`'s counters.

    Reads the current CDN URL fresh on every attempt so refreshes by other
    workers propagate to this one.
    """
    if dl._stop_event.is_set():
        return

    cdn_url, generation = dl._current_url()
    headers = {
        "Range": f"bytes={chunk.offset}-{chunk.offset + chunk.size - 1}",
        "User-Agent": dl.user_agent,
    }
    route = dl._router.route()
    proxied = route.proxy is not None
    encrypted = bytearray()
    try:
        with requests.get(
            cdn_url,
            headers=headers,
            timeout=dl.smart_proxy.proxy_timeout(dl.timeout) if proxied else dl.timeout,
            stream=True,
            proxies=route.proxies,
        ) as resp:
            if resp.status_code in URL_EXPIRY_STATUS:
                # Refresh the URL exactly once per generation, then retry.
                # These are not proxy faults, so don't penalise the proxy.
                dl._refresh_url(generation)
                raise CdnUrlExpired(
                    message=f"CDN URL expired (HTTP {resp.status_code}) for chunk {chunk.index}"
                )
            if resp.status_code == QUOTA_EXCEEDED_STATUS:
                # The quota belongs to the exit IP this request used.
                if proxied:
                    _proxy_fault(dl, route, ban=True, why="HTTP 509, its quota is spent")
                dl._on_direct_quota(route, quota_time_left(resp.headers), generation)
                raise RouteAgain("direct route over quota")
            if resp.status_code not in (200, 206):
                if proxied:
                    # A 429 is the proxy asking this client to slow down;
                    # anything else means it is broken or refusing us.
                    _proxy_fault(
                        dl, route, ban=resp.status_code != 429, why=f"HTTP {resp.status_code}"
                    )
                if resp.status_code >= 500:
                    raise RetryableTransferError(
                        message=f"HTTP {resp.status_code} for chunk {chunk.index}"
                    )
                raise NonRetryableTransferError(
                    message=f"HTTP {resp.status_code} for chunk {chunk.index}"
                )

            # These bytes are about to be decrypted with a CTR counter
            # derived from THIS chunk's offset and written at that offset.
            # If the CDN or a proxy ignored `Range` and sent the whole file
            # - or a different window of the same length - the result is
            # silent on-disk corruption that only a full MAC check would
            # ever notice, and only if integrity verification is enabled.
            # Same rule the streaming server enforces, from one module.
            try:
                validate_range_response(
                    resp.status_code,
                    resp.headers,
                    chunk.offset,
                    chunk.offset + chunk.size - 1,
                    state.total_size,
                )
            except RangeNotHonoredError as exc:
                if proxied:
                    _proxy_fault(dl, route, ban=True, why=f"ignored the range: {exc}")
                # A protocol violation, not a transient fault: retrying the
                # same request against the same server would repeat it.
                raise NonRetryableTransferError(
                    message=f"Upstream ignored the requested range for chunk {chunk.index}: {exc}"
                ) from exc

            # Read encrypted bytes, never more than this chunk is worth.
            # `validate_range_response` can only check Content-Length when
            # the server sends one, so a 206 with a plausible Content-Range
            # and chunked transfer-encoding streamed into this bytearray
            # without any ceiling at all.
            for block in resp.iter_content(chunk_size=65536):
                if dl._stop_event.is_set():
                    return
                if len(encrypted) + len(block) > chunk.size:
                    if proxied:
                        _proxy_fault(dl, route, ban=True, why="sent more than the range")
                    raise NonRetryableTransferError(
                        message=(
                            f"Upstream sent more than the requested range for chunk "
                            f"{chunk.index}: expected {chunk.size} bytes"
                        )
                    )
                encrypted.extend(block)
                dl.limiter.consume(len(block))
    except (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
    ) as exc:
        if proxied:
            _proxy_fault(dl, route, ban=False, why=type(exc).__name__)
        raise
    except requests.RequestException as exc:
        # The client cannot use this proxy at all - a socks5:// entry without
        # PySocks installed raises InvalidSchema before any byte is sent - so
        # it is benched like a broken proxy instead of failing the download.
        if proxied:
            _proxy_fault(dl, route, ban=True, why=f"{type(exc).__name__}: {exc}")
        raise

    if len(encrypted) != chunk.size:
        if proxied:
            _proxy_fault(dl, route, ban=False, why="connection dropped mid-chunk")
        raise NonRetryableTransferError(
            message=f"Chunk {chunk.index} short read: got {len(encrypted)}, expected {chunk.size}"
        )

    # Decrypt with AES-CTR starting at this chunk's offset
    cipher = make_ctr_cipher(
        aes_key,
        nonce,
        initial_value=ctr_offset_to_counter(chunk.offset),
    )
    plaintext = cipher.decrypt(bytes(encrypted))

    # Compute per-chunk MAC for later combining
    mac = chunk_mac(plaintext, aes_key, nonce)

    # Write plaintext to destination at the right offset
    with open(destination, "r+b") as f:
        f.seek(chunk.offset)
        f.write(plaintext)

    with dl._lock:
        state.mark_chunk_done(chunk.index, mac)
        state.mark_chunk_proxied(
            chunk.index, proxy_fingerprint(route.proxy) if proxied and route.proxy else None
        )
        dl._bytes_done += chunk.size
        dl._chunks_done += 1
        bytes_done_now = dl._bytes_done
        # Save state periodically (every ~16 chunks) to limit IO overhead
        should_save = dl._chunks_done % 16 == 0
        state_to_save = snapshot_state(state) if should_save else None
    # Feed the rolling meter outside the state lock (it has its own).
    dl._speed_meter.update(bytes_done_now)
    if state_to_save is not None:
        save_state(state_to_save)

    dl._router.proxy_succeeded(route)
