"""Parse MEGA share links (offline).

Everything here is pure: regex parsing and key checks, with no network access.

Supported URL families:

Modern (since 2020):
    https://mega.nz/file/<id>#<key>
    https://mega.nz/folder/<id>#<key>
    https://mega.nz/folder/<id>#<key>/file/<id>
    https://mega.nz/folder/<id>#<key>/folder/<sub>

Legacy (still common in older posts):
    https://mega.nz/#!<id>!<key>
    https://mega.nz/#F!<id>!<key>
    https://mega.nz/#F!<id>@<sub>!<key>           (folder inside a folder share)
    https://mega.nz/#F*<file>!<folder>!<key>      (file inside a folder share, compact)

Password-protected:
    https://mega.nz/#P!<encoded-blob>
"""

from __future__ import annotations

import binascii
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from .crypto import b64_url_decode
from .errors import MegaError


class LinkType(str, Enum):
    FILE = "file"
    FOLDER = "folder"
    FILE_IN_FOLDER = "file_in_folder"
    FOLDER_IN_FOLDER = "folder_in_folder"
    PASSWORD_PROTECTED = "password_protected"


@dataclass
class ParsedLink:
    """A parsed MEGA share link."""

    type: LinkType
    public_id: str
    key: str | None = None
    subpath: str | None = None  # File/folder handle inside a folder share
    encrypted_blob: str | None = None  # For password-protected links


# Modern format patterns
_RE_MODERN_FILE = re.compile(
    r"^https?://mega(?:\.co)?\.nz/file/([^#?/]+)(?:#([^!?/]+))?(?:!(.+))?$"
)
_RE_MODERN_FOLDER = re.compile(
    r"^https?://mega(?:\.co)?\.nz/folder/([^#?/]+)(?:#([^!?/]+))?"
    r"(?:/(?P<kind>file|folder)/(?P<inner>[^#?/]+))?$"
)
_RE_PASSWORD = re.compile(r"^https?://mega(?:\.co)?\.nz/#P!(.+)$")

# Legacy format patterns
_RE_LEGACY_FILE = re.compile(r"^https?://mega(?:\.co)?\.nz/#!([^!]+)!(.+)$")
# Folder share, optionally with an explicit @subfolder (#F!<id>@<sub>!<key>) or
# an ambiguous trailer (#F!<id>!<key>!<handle>). Legacy MEGA links used the
# trailer for both files and folders; parse it as FILE_IN_FOLDER and let callers
# that have the folder listing resolve the handle's real node type at runtime.
_RE_LEGACY_FOLDER = re.compile(
    r"^https?://mega(?:\.co)?\.nz/#F!(?P<id>[^!@]+)(?:@(?P<sub>[^!]+))?!"
    r"(?P<key>[^!]+)(?:!(?P<trailer>.+))?$"
)
# Compact file-in-folder form: #F*<file_id>!<folder_id>!<key>
_RE_LEGACY_FOLDER_FILE_COMPACT = re.compile(
    r"^https?://mega(?:\.co)?\.nz/#F\*(?P<file>[^!]+)!(?P<folder>[^!]+)!(?P<key>.+)$"
)
# "Node" link form used by alternate MEGA clients: #N!<file>!<folder>!<key>.
# Semantically identical to #F*<file>!<folder>!<key>.
_RE_LEGACY_NODE = re.compile(
    r"^https?://mega(?:\.co)?\.nz/#N!(?P<file>[^!]+)!(?P<folder>[^!]+)!(?P<key>.+)$"
)


def parse_link(url: str) -> ParsedLink:
    """Parse a MEGA share link into its components.

    Raises ValueError if the URL is not a recognizable MEGA link.
    """
    url = url.strip()
    try:
        parsed_url = urlparse(url)
        host = parsed_url.netloc.lower()
        if host.endswith("mega.nz") or host.endswith("mega.co.nz"):
            url = url.rstrip("/")
    except Exception:
        pass

    # Password-protected
    m = _RE_PASSWORD.match(url)
    if m:
        return ParsedLink(
            type=LinkType.PASSWORD_PROTECTED,
            public_id="",
            encrypted_blob=m.group(1),
        )

    # Modern folder (may include a /file/<id> or /folder/<sub> trailer)
    m = _RE_MODERN_FOLDER.match(url)
    if m:
        folder_id = m.group(1)
        key = m.group(2)
        kind = m.group("kind")
        inner = m.group("inner")
        if kind == "file" and inner:
            return ParsedLink(
                type=LinkType.FILE_IN_FOLDER,
                public_id=folder_id,
                key=key,
                subpath=inner,
            )
        if kind == "folder" and inner:
            return ParsedLink(
                type=LinkType.FOLDER_IN_FOLDER,
                public_id=folder_id,
                key=key,
                subpath=inner,
            )
        return ParsedLink(type=LinkType.FOLDER, public_id=folder_id, key=key)

    # Modern file
    m = _RE_MODERN_FILE.match(url)
    if m:
        return ParsedLink(
            type=LinkType.FILE,
            public_id=m.group(1),
            key=m.group(2),
            subpath=m.group(3),
        )

    # Legacy compact file-in-folder: #F*<file>!<folder>!<key>
    m = _RE_LEGACY_FOLDER_FILE_COMPACT.match(url)
    if m:
        return ParsedLink(
            type=LinkType.FILE_IN_FOLDER,
            public_id=m.group("folder"),
            key=m.group("key"),
            subpath=m.group("file"),
        )

    # Alternate "node" form: #N!<file>!<folder>!<key>
    m = _RE_LEGACY_NODE.match(url)
    if m:
        return ParsedLink(
            type=LinkType.FILE_IN_FOLDER,
            public_id=m.group("folder"),
            key=m.group("key"),
            subpath=m.group("file"),
        )

    # Legacy folder (with optional @subfolder and trailing file handle)
    m = _RE_LEGACY_FOLDER.match(url)
    if m:
        folder_id = m.group("id")
        sub = m.group("sub")
        key = m.group("key")
        trailer = m.group("trailer")
        if trailer:
            # `#F!<id>!<key>!<file_handle>` → file inside a folder share
            return ParsedLink(
                type=LinkType.FILE_IN_FOLDER,
                public_id=folder_id,
                key=key,
                subpath=trailer,
            )
        if sub:
            return ParsedLink(
                type=LinkType.FOLDER_IN_FOLDER,
                public_id=folder_id,
                key=key,
                subpath=sub,
            )
        return ParsedLink(type=LinkType.FOLDER, public_id=folder_id, key=key)

    # Legacy file
    m = _RE_LEGACY_FILE.match(url)
    if m:
        return ParsedLink(
            type=LinkType.FILE,
            public_id=m.group(1),
            key=m.group(2),
        )

    raise ValueError(f"Not a recognizable MEGA link: {url!r}")


def is_mega_url(url: str) -> bool:
    """Quick check whether a URL looks like a MEGA link of any supported flavour."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = parsed.netloc.lower()
    return host.endswith("mega.nz") or host.endswith("mega.co.nz")


def resolve_password_link(parsed: ParsedLink, password: str) -> ParsedLink:
    """Convert a password-protected ParsedLink into a normal file/folder link."""
    from .crypto import b64_url_encode, decrypt_password_link

    if parsed.type != LinkType.PASSWORD_PROTECTED or not parsed.encrypted_blob:
        raise ValueError("Not a password-protected link")
    node_type, public_handle, raw_key = decrypt_password_link(parsed.encrypted_blob, password)
    public_id = b64_url_encode(public_handle)
    key_str = b64_url_encode(raw_key)
    return ParsedLink(
        type=LinkType.FILE if node_type == 0 else LinkType.FOLDER,
        public_id=public_id,
        key=key_str,
    )


# How many bytes a link's key must decode to, by link type. A file link
# carries the full 32-byte file key; every folder-shaped link carries the
# 16-byte folder share key - INCLUDING `FILE_IN_FOLDER`, whose fragment is the
# parent folder's key, the file's own key being unwrapped from the listing.
# A password-protected link carries an encrypted blob, not a raw MEGA key, so
# it is not length-checked.
_EXPECTED_KEY_BYTES = {
    LinkType.FILE: 32,
    LinkType.FILE_IN_FOLDER: 16,
    LinkType.FOLDER: 16,
    LinkType.FOLDER_IN_FOLDER: 16,
}

# A raw MEGA link key is unpadded URL-safe base64 and nothing else. The general
# `b64_url_decode` strips `,` (a MEGA artifact in other contexts) before
# decoding, so a key like `KEY,,,,` or `KE,Y` would otherwise reach the same
# 32/16 bytes and pass the length guard. `parse_link`'s pattern does capture a
# `,` into the key, so it has to be rejected HERE, on the raw string, not left
# to a decoder that is deliberately lenient about it.
_RAW_KEY_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


def require_link_key(parsed, what: str) -> str:
    """Return the link's decryption key, or fail with a message that says so.

    `ParsedLink.key` is genuinely optional - a MEGA link can be pasted without
    its `#fragment`. Callers that need to decrypt were passing it straight to
    `str_to_a32`, which raised a `TypeError` about NoneType from deep inside
    the crypto layer instead of telling the user their link is missing a key.

    The length check is here for the same reason the None check is. `str_to_a32`
    routes through `bytes_to_a32`, which zero-pads to a word boundary (MEGA's
    a32 convention) - so a key that lost one base64
    character still produced a full-length key array and passed every guard
    downstream. The user got a DIFFERENT AES key with no error, surfacing much
    later as "File MAC verification failed", or as silent garbage on disk when
    integrity verification was off. Every call site that decodes a link key
    comes through here first, so this is the one place the
    check cannot be forgotten.
    """
    if not parsed.key:
        raise MegaError(
            message=f"{what} needs a link that includes its decryption key (the part after '#')"
        )
    key = str(parsed.key)
    expected = _EXPECTED_KEY_BYTES.get(parsed.type)
    if expected is not None:
        if not _RAW_KEY_RE.match(key):
            raise MegaError(
                message=f"{what} needs a link whose key is URL-safe base64; this one is not"
            )
        try:
            raw = b64_url_decode(key)
        except (ValueError, binascii.Error) as exc:
            raise MegaError(
                message=f"{what} needs a link whose key is valid base64; this one is not"
            ) from exc
        if len(raw) != expected:
            raise MegaError(
                message=(
                    f"{what} got a link key that decodes to {len(raw)} bytes, "
                    f"expected {expected} - the key is truncated or corrupt"
                )
            )
    return key


def normalize_link(url: str) -> str:
    """Return the link in modern format if possible."""
    p = parse_link(url)
    if p.type == LinkType.FILE:
        base = f"https://mega.nz/file/{p.public_id}"
        return f"{base}#{p.key}" if p.key else base
    if p.type == LinkType.FOLDER:
        base = f"https://mega.nz/folder/{p.public_id}"
        return f"{base}#{p.key}" if p.key else base
    if p.type == LinkType.FILE_IN_FOLDER:
        return f"https://mega.nz/folder/{p.public_id}#{p.key}/file/{p.subpath}"
    if p.type == LinkType.FOLDER_IN_FOLDER:
        return f"https://mega.nz/folder/{p.public_id}#{p.key}/folder/{p.subpath}"
    return url


__all__ = [
    "LinkType",
    "ParsedLink",
    "is_mega_url",
    "normalize_link",
    "parse_link",
    "require_link_key",
    "resolve_password_link",
]
