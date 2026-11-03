"""Pick the right key out of a public-folder node's `k` field.

A node's `k` is a "/"-separated list of `handle:wrapped_key` items, one per key
the node is wrapped under. A folder link's key unwraps exactly one of them: the
item whose handle is the shared folder's own node. The owner's item (wrapped
with their master key) usually comes first, and every reader used to take the
text after the FIRST ":" - so a share with both items handed
`"<key>/<handle>:<key>"` to the base64 decoder and skipped every node as
"key decrypt failed". Owner-shared folders look exactly like that.

Unwrapping with the wrong key is not an error: it yields random bytes without
complaint. So a candidate only counts once the node's attributes decrypt with
it, which is MEGA's own proof that the key is right.
"""

from __future__ import annotations

from collections.abc import Iterable

from .crypto import (
    aes_key_wrap_decrypt,
    b64_url_decode,
    bytes_to_a32,
    decrypt_attributes,
    unpack_file_key,
)

_FILE = 0


def share_root_handles(raw_nodes: Iterable[object]) -> set[str]:
    """Handles of the listing's top nodes: those whose parent is not listed.

    The folder a link shares is the one node whose parent the listing does not
    contain, and its handle is the one the share key's `k` item carries.
    """
    nodes = [n for n in raw_nodes if isinstance(n, dict)]
    listed = {n.get("h") for n in nodes}
    return {n["h"] for n in nodes if isinstance(n.get("h"), str) and n.get("p") not in listed}


def wrapped_key_candidates(raw_k: str, preferred: Iterable[str] = ()) -> list[str]:
    """The wrapped keys in `raw_k`, items whose handle is in `preferred` first.

    A bare key with no `handle:` prefix is its own single candidate. MEGA's
    base64 is the URL-safe alphabet, so "/" can only ever be the separator.
    """
    items: list[tuple[str, str]] = []
    for item in raw_k.split("/"):
        handle, sep, wrapped = item.partition(":")
        if not sep:
            handle, wrapped = "", item
        if wrapped:
            items.append((handle, wrapped))
    wanted = set(preferred)
    return [w for h, w in items if h in wanted] + [w for h, w in items if h not in wanted]


def unwrap_share_node_key(
    raw_node: dict, share_key: bytes, preferred: Iterable[str] = ()
) -> tuple[bytes, dict | None] | None:
    """Return `(node_key, attributes)` for the `k` item `share_key` really unwraps.

    The first candidate whose attributes decrypt wins. When none does - a node
    with no attributes, or attributes that are themselves damaged - the first
    candidate that unwrapped at all is returned with `None` attributes, which
    is what the single-item readers did before, so such a node is still listed
    under its handle rather than silently dropped. `None` means no item
    unwrapped at all.
    """
    is_file = raw_node.get("t", _FILE) == _FILE
    try:
        encrypted_attrs = b64_url_decode(raw_node.get("a") or "")
    except ValueError:
        encrypted_attrs = b""

    fallback: bytes | None = None
    for wrapped in wrapped_key_candidates(raw_node.get("k") or "", preferred):
        try:
            key = aes_key_wrap_decrypt(b64_url_decode(wrapped), share_key)
        except ValueError:
            continue
        if len(key) < (32 if is_file else 16):
            continue
        attr_key = unpack_file_key(bytes_to_a32(key[:32]))[0] if is_file else key[:16]
        try:
            attrs = decrypt_attributes(encrypted_attrs, attr_key)
        except (TypeError, ValueError):
            attrs = None
        if attrs is not None:
            return key, attrs
        if fallback is None:
            fallback = key
    return (fallback, None) if fallback is not None else None
