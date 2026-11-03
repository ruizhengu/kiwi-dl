"""Read an HTTP response body with a hard size ceiling.

Moved out of the link-service helpers when those services were removed: the
MEGA API client still needs to bound what it parses.
"""

from __future__ import annotations


class PayloadTooLargeError(ValueError):
    """An untrusted payload or response exceeded the size we are willing to buffer."""


STREAM_CHUNK_BYTES = 65536


def read_bounded_bytes(response, limit: int, what: str = "Service") -> bytes:
    """Return the body, refusing to buffer more than `limit` bytes.

    Must be used with `stream=True`. Touching `response.text` or
    `response.json()` first is useless as a defence - by then `requests` has
    already read the whole body AND transparently inflated any
    `Content-Encoding: gzip`, which makes every response its own decompression
    bomb. `iter_content` yields the DECODED bytes, so the cap applies to what
    actually lands in memory.

    The chunk size is capped at `limit + 1` so a small cap cannot buffer a full
    64 KiB block before noticing: overshoot is always at most one byte past the
    cap or one block, whichever is smaller.

    `core.api` wraps this to translate `PayloadTooLargeError` into the
    exception type its own callers catch.
    """
    total = 0
    parts: list[bytes] = []
    for block in response.iter_content(chunk_size=min(STREAM_CHUNK_BYTES, limit + 1)):
        total += len(block)
        if total > limit:
            close = getattr(response, "close", None)
            if close is not None:
                close()
            raise PayloadTooLargeError(f"{what} response is unexpectedly large")
        parts.append(block)
    return b"".join(parts)


def read_bounded_text(response, limit: int, what: str = "Service") -> str:
    """`read_bounded_bytes` decoded with the response's own declared encoding."""
    raw = read_bounded_bytes(response, limit, what)
    encoding = getattr(response, "encoding", None) or "utf-8"
    return raw.decode(encoding, errors="replace")
