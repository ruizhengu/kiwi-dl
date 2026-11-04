"""MEGA, faked at the wire: public-folder listings and a storage server.

These stub the SERVER, not the client: a listing is built exactly as MEGA
returns it - `{"h","p","u","t","s","ts","a","k"}`, attributes AES-encrypted,
keys wrapped with the share key - and `FakeCdn` serves a really encrypted file
through `requests.get`, so the real parsing, key unwrapping, chunk transfer and
MAC code all run against them.

The encryption is written from the MEGA format description (`a32`/key-wrap/
attribute prefix) rather than with the helpers under test where it can be, so
a fixture cannot just agree with a buggy reader.

Not a mock framework. Add only what a test needs to observe.
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import requests

from kiwi_dl.core.crypto import (
    aes_key_wrap_encrypt,
    b64_url_encode,
    bytes_to_a32,
    encrypt_attributes,
    pack_file_key,
)

# Node type codes, straight from the protocol.
FILE, FOLDER, ROOT, INBOX, TRASH = 0, 1, 2, 3, 4

MASTER_KEY = bytes(range(16))
OWNER = "self0000"


def bytes_from_a32_pair(key_a32: list[int]) -> bytes:
    """The AES key of a 32-byte file key: the first half XOR the second.

    Written out from the format rather than by calling `unpack_file_key`, so a
    change in that function cannot silently keep this fixture agreeing with it.
    """
    return bytes(
        b
        for word in (key_a32[i] ^ key_a32[i + 4] for i in range(4))
        for b in word.to_bytes(4, "big")
    )


def make_file_key() -> bytes:
    """A well-formed 32-byte file key (AES key + nonce + MAC IV)."""
    return bytes(
        b
        for word in pack_file_key(os.urandom(16), os.urandom(8), [0, 0])
        for b in word.to_bytes(4, "big")
    )


def share_listing(
    folder_key: bytes,
    entries: list[tuple[str, str, str, int]],
    owner_item: str | None = None,
) -> list[dict]:
    """Build a public-folder listing: entries are (handle, parent, name, type).

    Keys are wrapped with the SHARE key rather than a master key, which is the
    distinction `import_public_share` has to get right when it re-wraps them.

    `owner_item` ("first" or "last") builds `k` the way a live owner-shared
    folder sends it: two `handle:key` items joined by "/" - the owner's, wrapped
    with THEIR master key, and the share's, wrapped with the link key under the
    shared folder's handle. Observed on a live public folder in Nov 2026:
    `<owner>:<22ch>/<root>:<22ch>`, owner first. Without it, the single item is
    labelled with the owner handle, the shape the readers were written for.
    """
    listed = {handle for handle, *_ in entries}
    root = next((handle for handle, parent, *_ in entries if parent not in listed), "")
    listing = []
    for handle, parent, name, node_type in entries:
        node_key = os.urandom(32) if node_type == FILE else os.urandom(16)
        aes_key = (
            bytes_from_a32_pair(bytes_to_a32(node_key)) if node_type == FILE else node_key[:16]
        )
        share_wrapped = b64_url_encode(aes_key_wrap_encrypt(node_key, folder_key))
        if owner_item is None:
            raw_k = f"{OWNER}:{share_wrapped}"
        else:
            owner = f"{OWNER}:{b64_url_encode(aes_key_wrap_encrypt(node_key, MASTER_KEY))}"
            share = f"{root}:{share_wrapped}"
            raw_k = f"{owner}/{share}" if owner_item == "first" else f"{share}/{owner}"
        listing.append(
            {
                "h": handle,
                "p": parent,
                "u": OWNER,
                "t": node_type,
                "s": 0,
                "ts": 1_700_000_000,
                "a": b64_url_encode(encrypt_attributes({"n": name}, aes_key)),
                "k": raw_k,
            }
        )
    return listing


def attrs_of(blob_b64: str, key: bytes) -> dict | None:
    """Decrypt an attribute blob the way MEGA's own reader would."""
    from kiwi_dl.core.crypto import b64_url_decode, decrypt_attributes

    return decrypt_attributes(b64_url_decode(blob_b64), key)


# ---------------------------------------------------------------------------
# The storage side: one public file served over `requests.get`
# ---------------------------------------------------------------------------


class _Socket:
    """The socket under a response, as far as a stop can reach it."""

    def __init__(self) -> None:
        self.shut = threading.Event()

    def shutdown(self, how) -> None:
        self.shut.set()


class _CdnResponse:
    """The slice of `requests.Response` that `download_transport` reads.

    A slow body waits for each block the way a socket read does. `close()`
    from another thread does not end that wait, as it does not on a real
    socket; shutting the socket down (`raw.connection.sock`) does, and the read
    then fails like a dropped connection. `record` gets "reading" once the body
    is being read and "closed" once the response is.
    """

    def __init__(
        self,
        status_code: int,
        headers: dict,
        body: bytes = b"",
        delay: float = 0.0,
        record: dict | None = None,
    ):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self._delay = delay
        self._record = record if record is not None else {}
        self.raw = SimpleNamespace(connection=SimpleNamespace(sock=_Socket()))

    def iter_content(self, chunk_size: int = 65536):
        shut = self.raw.connection.sock.shut
        self._record["reading"] = True
        for i in range(0, len(self._body), chunk_size):
            if self._delay:
                shut.wait(self._delay)
            if shut.is_set():
                raise requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")
            yield self._body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._record["closed"] = True


class FakeCdn:
    """A MEGA storage server for one public file, reached through `requests.get`.

    Install with `monkeypatch.setattr("requests.get", cdn.get)`: that is the
    call behind every chunk request (made in `inflight`, on a helper thread),
    so the real chunk path - range guards, AES-CTR, chunk MACs, state commits
    and the on-disk MAC check - runs unmodified against it.

    The ciphertext is produced from the format (AES-128-CTR, 64-bit nonce then
    a 64-bit block counter from zero), not with the client's cipher helper.
    The expected MAC does use the client's chunk-MAC functions: the MAC code is
    proven against MEGA by live runs, and what these tests exercise is
    transfer behaviour around it, not the MAC itself.

    Every request is appended to `self.requests` as a dict, so a test can
    assert on which URL, range and route each chunk actually used.

    Quota is per ROUTE - the exit IP MEGA sees: "direct", or the proxy URL the
    request went through. `quota[route]` is how many bytes that route may still
    be served; past it, the answer is what a live anonymous quota hit returned
    (Nov 2026): 509 with an empty body, `X-MEGA-Time-Left` and
    `Connection: close`. The URL that got the 509 is burned and answers 403
    from then on, as the live one did when asked again.

    `block_delay` is the wait before each 64 KiB block of a body, for the
    ranges that start at or past `slow_from` (all of them by default): a slow
    proxy, or with a long delay one that has stopped sending.
    """

    def __init__(self, size: int = 3 * 1024 * 1024, block_delay: float = 0.0):
        from Crypto.Cipher import AES

        from kiwi_dl.core.chunks import (
            chunk_mac,
            combine_chunk_macs,
            condense_mac,
            iter_chunks,
        )

        self.size = size
        self.plaintext = os.urandom(size)
        self.aes_key = os.urandom(16)
        self.nonce = os.urandom(8)
        self.ciphertext = AES.new(
            self.aes_key, AES.MODE_CTR, nonce=self.nonce, initial_value=0
        ).encrypt(self.plaintext)
        macs = [
            chunk_mac(self.plaintext[c.offset : c.offset + c.size], self.aes_key, self.nonce)
            for c in iter_chunks(size)
        ]
        self.mac_iv_a32 = condense_mac(combine_chunk_macs(macs, self.aes_key))
        self.block_delay = block_delay
        self.slow_from = 0
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._urls = 0
        # Scripted failures: called with the request record before anything is
        # served; returning an exception makes `get` raise it.
        self.fail_with = None
        self.quota: dict[str, int] = {}  # route -> bytes it may still be served
        self.served: dict[str, int] = {}
        self.time_left = 20226
        self.burned: set[str] = set()
        self.proxy_status: dict[str, int] = {}  # route -> the HTTP status that proxy answers
        # Routes that alter what they relay. CDN URLs are plain http, so a proxy
        # can, and nothing but the whole-file MAC would notice.
        self.corrupt_routes: set[str] = set()

    def issue_url(self) -> str:
        """A fresh CDN URL, as the `a:g` resolver would hand out."""
        with self._lock:
            self._urls += 1
            return f"http://cdn.invalid/dl/url{self._urls}"

    def get(self, url, headers=None, timeout=None, stream=False, proxies=None):
        start, end = (int(x) for x in headers["Range"].split("=", 1)[1].split("-"))
        route = (proxies or {}).get("http") or "direct"
        record = {
            "url": url,
            "start": start,
            "end": end,
            "proxies": proxies,
            "route": route,
            "timeout": timeout,
        }
        with self._lock:
            self.requests.append(record)
        if self.fail_with is not None:
            exc = self.fail_with(record)
            if exc is not None:
                raise exc
        body = self.ciphertext[start : end + 1]
        closing = {"Content-Length": "0", "Connection": "close"}
        if route in self.proxy_status:
            record["status"] = self.proxy_status[route]
            return _CdnResponse(self.proxy_status[route], closing)
        with self._lock:
            if url in self.burned:
                record["status"] = 403
                return _CdnResponse(403, closing)
            allowed = self.quota.get(route)
            if allowed is not None and self.served.get(route, 0) + len(body) > allowed:
                self.burned.add(url)
                record["status"] = 509
                return _CdnResponse(509, {"X-MEGA-Time-Left": str(self.time_left), **closing})
            self.served[route] = self.served.get(route, 0) + len(body)
        if route in self.corrupt_routes:
            body = bytes([body[0] ^ 0xFF]) + body[1:]
        record["status"] = 206
        return _CdnResponse(
            206,
            {"Content-Range": f"bytes {start}-{end}/{self.size}", "Content-Length": str(len(body))},
            body,
            self.block_delay if start >= self.slow_from else 0.0,
            record,
        )

    def download_with(self, downloader, destination):
        """Run `downloader` over this file the way `download_link` would."""
        return downloader._run_download(
            cdn_url=self.issue_url(),
            file_size=self.size,
            aes_key=self.aes_key,
            nonce=self.nonce,
            mac_iv_a32=self.mac_iv_a32,
            destination=destination,
            source="https://mega.nz/file/FAKEFILE#<key>",
            on_progress=None,
            url_resolver=self.issue_url,
        )
