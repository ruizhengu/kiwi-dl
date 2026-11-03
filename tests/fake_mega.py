"""An in-memory MEGA account that speaks the real wire shapes.

WHY THIS EXISTS. Every bug that has actually hurt this project was found by a
human doing a live run, never by the unit suite - the double-base64'd upload
token, the MPI prefix fed into the RSA exponentiation, `logout()` in six
teardowns. The reason is structural: the suite stubs `MegaClient` methods, so
nothing ever exercises the layer where those bugs live, which is the assembly
between a decrypted node and an API payload.

So this harness does NOT stub the client. It stubs the SERVER: it stores nodes
exactly as MEGA returns them - `{"h","p","u","t","s","ts","a","k"}`, attributes
AES-encrypted, keys wrapped with the master key - and lets a real `MegaClient`
run against it. `decrypt_node`, `find_node`, `unpack_file_key` and the key
re-wrapping in `rename`/`export_link` all execute for real.

That cuts both ways and is the point: a fixture built with the same helper the
code under test uses can only prove self-consistency. Compare
`tests/test_login_rsa_session_id.py`, whose csid fixture omitted the MPI prefix
because it was written to match the buggy reader. So the encryption here is
written from the MEGA format description - `a32`/key-wrap/attribute prefix -
and `test_fake_mega_harness.py` pins the wire shapes independently.

Not a mock framework. Add only what a test needs to observe.
"""

from __future__ import annotations

import os
import threading
import time

from megabasterd_cli.core.auth import MegaSession
from megabasterd_cli.core.client import MegaClient
from megabasterd_cli.core.crypto import (
    aes_key_wrap_encrypt,
    b64_url_encode,
    bytes_to_a32,
    encrypt_attributes,
    pack_file_key,
)
from megabasterd_cli.core.errors import MegaError

# Node type codes, straight from the protocol.
FILE, FOLDER, ROOT, INBOX, TRASH = 0, 1, 2, 3, 4

MASTER_KEY = bytes(range(16))
OWNER = "self0000"


class FakeMegaAPI:
    """The subset of `MegaAPIClient` the node/cloud/share layers actually call.

    Every mutating call is recorded in `self.calls` as `(action, kwargs)` so a
    test can assert on the PAYLOAD - which is where the encoding bugs live -
    and not merely on the fact that something was called.
    """

    def __init__(self, master_key: bytes = MASTER_KEY):
        self.master_key = master_key
        self.nodes: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.exported: dict[str, str] = {}  # node handle -> public handle
        self.shares: dict[str, list[dict]] = {}  # public id -> raw listing
        self._next = 0
        self.closed = False
        self.sid: str | None = None

    # -- building an account -------------------------------------------------

    def _handle(self, prefix: str = "h") -> str:
        self._next += 1
        return f"{prefix}{self._next:07d}"

    def add_node(
        self,
        name: str | None,
        node_type: int = FOLDER,
        parent: str = "",
        size: int = 0,
        key: bytes | None = None,
    ) -> str:
        """Append one node, encrypted the way MEGA stores it. Returns its handle.

        A file gets the full 32-byte packed key (AES key, nonce, MAC IV); a
        folder gets 16 bytes. That difference is not cosmetic - it is the branch
        `decrypt_node`, `rename` and `export_link` each take separately, and the
        one a 16-byte-only fixture would hide.
        """
        handle = self._handle()
        raw_key = ""
        attrs = b""
        if node_type in (FILE, FOLDER):
            if key is None:
                key = os.urandom(32) if node_type == FILE else os.urandom(16)
            aes_key = bytes_from_a32_pair(bytes_to_a32(key)) if node_type == FILE else key[:16]
            attrs = encrypt_attributes({"n": name}, aes_key)
            raw_key = f"{OWNER}:{b64_url_encode(aes_key_wrap_encrypt(key, self.master_key))}"
        self.nodes.append(
            {
                "h": handle,
                "p": parent,
                "u": OWNER,
                "t": node_type,
                "s": size,
                "ts": 1_700_000_000,
                "a": b64_url_encode(attrs) if attrs else "",
                "k": raw_key,
            }
        )
        return handle

    def add_file(self, name: str, parent: str, size: int = 1024) -> str:
        return self.add_node(name, FILE, parent, size)

    def add_folder(self, name: str, parent: str) -> str:
        return self.add_node(name, FOLDER, parent)

    def with_default_tree(self) -> FakeMegaAPI:
        """root + trash + inbox, the three system nodes every real account has."""
        self.root = self.add_node(None, ROOT)
        self.trash = self.add_node(None, TRASH)
        self.inbox = self.add_node(None, INBOX)
        return self

    # -- the transport -------------------------------------------------------

    def set_session(self, sid):
        self.sid = sid

    def clear_session(self):
        self.sid = None

    def close(self):
        self.closed = True

    def request(self, payload, extra_params=None):
        self.calls.append(("request", {"payload": payload}))
        action = payload.get("a") if isinstance(payload, dict) else None
        if action == "f":
            return {"f": list(self.nodes)}
        if action == "ug":
            return {"u": OWNER}
        raise AssertionError(f"FakeMegaAPI got an unscripted action: {action!r}")

    # -- cloud mutations -----------------------------------------------------

    def create_folder(self, parent_handle, encrypted_attrs, wrapped_key):
        self.calls.append(
            (
                "create_folder",
                {
                    "parent_handle": parent_handle,
                    "encrypted_attrs": encrypted_attrs,
                    "wrapped_key": wrapped_key,
                },
            )
        )
        handle = self._handle()
        self.nodes.append(
            {
                "h": handle,
                "p": parent_handle,
                "u": OWNER,
                "t": FOLDER,
                "s": 0,
                "ts": 1_700_000_000,
                "a": encrypted_attrs,
                "k": f"{OWNER}:{wrapped_key}",
            }
        )
        return {"f": [{"h": handle}]}

    def delete_node(self, handle):
        self.calls.append(("delete_node", {"handle": handle}))
        before = len(self.nodes)
        self.nodes = [n for n in self.nodes if n["h"] != handle]
        if len(self.nodes) == before:
            raise MegaError(code=-9)
        return 0

    def move_node(self, handle, new_parent):
        self.calls.append(("move_node", {"handle": handle, "new_parent": new_parent}))
        for node in self.nodes:
            if node["h"] == handle:
                node["p"] = new_parent
                return 0
        raise MegaError(code=-9)

    def rename_node(self, handle, encrypted_attrs, wrapped_key):
        self.calls.append(
            (
                "rename_node",
                {
                    "handle": handle,
                    "encrypted_attrs": encrypted_attrs,
                    "wrapped_key": wrapped_key,
                },
            )
        )
        for node in self.nodes:
            if node["h"] == handle:
                node["a"] = encrypted_attrs
                node["k"] = f"{OWNER}:{wrapped_key}"
                return 0
        raise MegaError(code=-9)

    # -- shares --------------------------------------------------------------

    def export_node(self, handle):
        self.calls.append(("export_node", {"handle": handle}))
        public = self.exported.get(handle) or b64_url_encode(os.urandom(6))
        self.exported[handle] = public
        return public

    def remove_export(self, handle):
        self.calls.append(("remove_export", {"handle": handle}))
        self.exported.pop(handle, None)
        return 0

    def get_public_folder_listing(self, public_id):
        self.calls.append(("get_public_folder_listing", {"public_id": public_id}))
        if public_id not in self.shares:
            raise MegaError(code=-9)
        return {"f": list(self.shares[public_id])}

    def import_node_from_share(
        self, target_parent, source_handle, encrypted_attrs, wrapped_key, share_handle, node_type
    ):
        self.calls.append(
            (
                "import_node_from_share",
                {
                    "target_parent": target_parent,
                    "source_handle": source_handle,
                    "encrypted_attrs": encrypted_attrs,
                    "wrapped_key": wrapped_key,
                    "share_handle": share_handle,
                    "node_type": node_type,
                },
            )
        )
        handle = self._handle("i")
        self.nodes.append(
            {
                "h": handle,
                "p": target_parent,
                "u": OWNER,
                "t": node_type,
                "s": 0,
                "ts": 1_700_000_000,
                "a": encrypted_attrs,
                "k": f"{OWNER}:{wrapped_key}",
            }
        )
        return {"f": [{"h": handle}]}

    # -- assertions helpers --------------------------------------------------

    def actions(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last(self, action: str) -> dict:
        for name, kwargs in reversed(self.calls):
            if name == action:
                return kwargs
        raise AssertionError(f"{action} was never called; saw {self.actions()}")


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


def logged_in_client(api: FakeMegaAPI | None = None) -> tuple[MegaClient, FakeMegaAPI]:
    """A real `MegaClient` wired to a fake account with the standard tree."""
    api = api or FakeMegaAPI().with_default_tree()
    client = MegaClient(api=api)
    client.session = MegaSession(
        sid="fake-sid",
        master_key=api.master_key,
        rsa_private_key=None,
        user_handle=OWNER,
        email="someone@example.invalid",
    )
    return client, api


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
    from megabasterd_cli.core.crypto import b64_url_decode, decrypt_attributes

    return decrypt_attributes(b64_url_decode(blob_b64), key)


# ---------------------------------------------------------------------------
# The storage side: one public file served over `requests.get`
# ---------------------------------------------------------------------------


class _CdnResponse:
    """The slice of `requests.Response` that `download_transport` reads."""

    def __init__(self, status_code: int, headers: dict, body: bytes = b"", delay: float = 0.0):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self._delay = delay

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            if self._delay:
                time.sleep(self._delay)
            yield self._body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None


class FakeCdn:
    """A MEGA storage server for one public file, reached through `requests.get`.

    Install with `monkeypatch.setattr("requests.get", cdn.get)`: that is the
    call `download_transport` makes per chunk, so the real chunk path - range
    guards, AES-CTR, chunk MACs, state commits and the on-disk MAC check - runs
    unmodified against it.

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
    """

    def __init__(self, size: int = 3 * 1024 * 1024, block_delay: float = 0.0):
        from Crypto.Cipher import AES

        from megabasterd_cli.core.chunks import (
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

    def issue_url(self) -> str:
        """A fresh CDN URL, as the `a:g` resolver would hand out."""
        with self._lock:
            self._urls += 1
            return f"http://cdn.invalid/dl/url{self._urls}"

    def get(self, url, headers=None, timeout=None, stream=False, proxies=None):
        start, end = (int(x) for x in headers["Range"].split("=", 1)[1].split("-"))
        route = (proxies or {}).get("http") or "direct"
        record = {"url": url, "start": start, "end": end, "proxies": proxies, "route": route}
        with self._lock:
            self.requests.append(record)
        if self.fail_with is not None:
            exc = self.fail_with(record)
            if exc is not None:
                raise exc
        body = self.ciphertext[start : end + 1]
        closing = {"Content-Length": "0", "Connection": "close"}
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
        record["status"] = 206
        return _CdnResponse(
            206,
            {"Content-Range": f"bytes {start}-{end}/{self.size}", "Content-Length": str(len(body))},
            body,
            self.block_delay,
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
