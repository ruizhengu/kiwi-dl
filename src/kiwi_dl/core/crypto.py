"""Cryptographic helpers for the MEGA protocol.

MEGA uses:
- AES-128-CBC with zero IV for unwrapping keys and decrypting attributes
- AES-128-CTR for file content decryption
- A custom chunked MAC for file integrity
- PBKDF2-HMAC-SHA512 for decrypting password-protected `#P!` links

References:
- https://mega.nz/SecurityWhitePaper.pdf
- https://github.com/meganz/sdk
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
from collections.abc import Iterable
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util import Counter

# ---------------------------------------------------------------------------
# Base64 (MEGA-flavoured, URL-safe without padding)
# ---------------------------------------------------------------------------


_STD_B64_RE = re.compile(r"\A[A-Za-z0-9+/]*\Z")


def b64_url_decode(data: str) -> bytes:
    """Decode MEGA's URL-safe base64 (no padding).

    Rejects characters outside the base64 alphabet instead of silently dropping
    them. `base64.b64decode(..., validate=False)` discards non-alphabet chars
    before checking padding, so a tampered key (a valid key plus junk) could
    decode to the right length and slip past a length-only guard. We gate on the
    alphabet *after* the URL-safe translation and the deliberate `,` strip, so
    valid input still decodes byte-identically.
    """
    data = data.replace("-", "+").replace("_", "/").replace(",", "")
    if not _STD_B64_RE.match(data):
        raise binascii.Error("non-base64 character in MEGA data")
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.b64decode(data + padding)


def b64_url_encode(data: bytes) -> str:
    """Encode bytes to MEGA's URL-safe base64 (no padding).

    `urlsafe_b64encode` IS the `+`->`-` / `/`->`_` substitution this used to
    hand-roll, so there is one fewer place to get the alphabet wrong. The
    decoder opposite is deliberately NOT symmetric: it translates first and
    then gates on the alphabet, which `urlsafe_b64decode` cannot express.
    """
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# int <-> a32 array conversions (MEGA represents keys as arrays of 32-bit ints)
# ---------------------------------------------------------------------------


def bytes_to_a32(data: bytes) -> list[int]:
    """Convert bytes to a list of unsigned 32-bit ints (big-endian)."""
    if len(data) % 4:
        data += b"\x00" * (4 - len(data) % 4)
    return list(struct.unpack(f">{len(data) // 4}I", data))


def a32_to_bytes(arr: Iterable[int]) -> bytes:
    """Convert a list of 32-bit ints (big-endian) to bytes."""
    arr = list(arr)
    return struct.pack(f">{len(arr)}I", *arr)


def str_to_a32(s: str) -> list[int]:
    """Decode MEGA's URL-safe base64 into an a32 array."""
    return bytes_to_a32(b64_url_decode(s))


# ---------------------------------------------------------------------------
# AES-CBC (key unwrapping & attribute decryption)
# ---------------------------------------------------------------------------


def aes_cbc_decrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-CBC decrypt with zero IV."""
    cipher = AES.new(key, AES.MODE_CBC, b"\x00" * 16)
    return cipher.decrypt(data)


def aes_key_wrap_decrypt(data: bytes, key: bytes) -> bytes:
    """ECB-style 16-byte-block decryption for MEGA wrapped node keys."""
    if len(data) % 16:
        raise ValueError(f"key-wrap input must be a multiple of 16 bytes, got {len(data)}")
    out = b""
    for i in range(0, len(data), 16):
        cipher = AES.new(key, AES.MODE_CBC, b"\x00" * 16)
        out += cipher.decrypt(data[i : i + 16])
    return out


# ---------------------------------------------------------------------------
# AES-CTR (file content)
# ---------------------------------------------------------------------------


def make_ctr_cipher(key: bytes, nonce: bytes, initial_value: int = 0) -> Any:
    """Create an AES-128-CTR cipher.

    MEGA uses a 64-bit nonce and a 64-bit counter that starts at byte_offset / 16.
    """
    if len(key) != 16:
        raise ValueError("AES-CTR key must be 16 bytes")
    if len(nonce) != 8:
        raise ValueError("MEGA nonce must be 8 bytes")
    ctr = Counter.new(64, prefix=nonce, initial_value=initial_value)
    return AES.new(key, AES.MODE_CTR, counter=ctr)


def ctr_offset_to_counter(byte_offset: int) -> int:
    """Convert a byte offset within a file to the AES-CTR block counter."""
    if byte_offset % 16 != 0:
        raise ValueError("CTR offset must be a multiple of 16 bytes")
    return byte_offset // 16


# ---------------------------------------------------------------------------
# File-key unpacking
# ---------------------------------------------------------------------------


def unpack_file_key(key_a32: list[int]) -> tuple[bytes, bytes, list[int]]:
    """Unpack an 8-int file key into (aes_key, nonce, mac_iv_a32).

    MEGA file keys are 256 bits (8 x uint32), structured as:
    - key_a32[0..3] XOR key_a32[4..7] = 128-bit AES key
    - key_a32[4..5] = 64-bit CTR nonce
    - key_a32[6..7] = 64-bit MAC IV
    """
    if len(key_a32) != 8:
        raise ValueError(f"File key must have 8 uint32 elements, got {len(key_a32)}")

    aes_key_a32 = [
        key_a32[0] ^ key_a32[4],
        key_a32[1] ^ key_a32[5],
        key_a32[2] ^ key_a32[6],
        key_a32[3] ^ key_a32[7],
    ]
    aes_key = a32_to_bytes(aes_key_a32)
    nonce = a32_to_bytes(key_a32[4:6])
    mac_iv = key_a32[6:8]
    return aes_key, nonce, mac_iv


# ---------------------------------------------------------------------------
# Attribute decryption (file metadata)
# ---------------------------------------------------------------------------

ATTR_PREFIX = b"MEGA"


def decrypt_attributes(data: bytes, key: bytes) -> dict | None:
    """Decrypt and parse the attribute blob for a file."""
    if not data or len(data) % 16 != 0:
        return None
    plaintext = aes_cbc_decrypt(data, key)
    if not plaintext.startswith(ATTR_PREFIX):
        return None
    body = plaintext[len(ATTR_PREFIX) :].rstrip(b"\x00").decode("utf-8", errors="replace")
    try:
        return dict(json.loads(body))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Password-protected link decoding
# ---------------------------------------------------------------------------


def derive_password_link_keys(password: str, salt: bytes, algo: int = 2) -> tuple[bytes, bytes]:
    """Derive (aes_xor_key, hmac_key) for a password-protected MEGA link.

    Algorithm 1: 64-byte PBKDF2-HMAC-SHA512 with 100k iterations (early variant).
    Algorithm 2: same but with the modern parameters used by current MEGA clients.

    Returns the AES XOR key and the HMAC verification key.
    """
    from Crypto.Hash import SHA512
    from Crypto.Protocol.KDF import PBKDF2

    if algo not in (1, 2):
        raise ValueError(f"Unsupported password-link algorithm {algo}")
    derived = PBKDF2(
        password.encode("utf-8"),  # type: ignore[arg-type]  # bytes ok at runtime
        salt,
        dkLen=64,
        count=100_000,
        hmac_hash_module=SHA512,
    )
    return derived[:32], derived[32:]


def decrypt_password_link(encoded_blob: str, password: str) -> tuple[int, bytes, bytes]:
    """Decrypt a MEGA password-protected link blob.

    Returns (node_type, public_handle, raw_key) where:
      - node_type: 0 = file, 1 = folder
      - public_handle: 6-byte handle for use in standard MEGA URLs
      - raw_key: 32-byte file key (files) or 16-byte folder key (folders)

    Raises ValueError if the password is wrong (HMAC mismatch) or the blob is
    malformed.
    """
    import hashlib
    import hmac as _hmac

    raw = b64_url_decode(encoded_blob)
    if len(raw) < 1 + 1 + 6 + 32 + 16 + 32:
        raise ValueError("Password link blob is too short")

    algo = raw[0]
    node_type = raw[1]
    public_handle = raw[2:8]
    salt = raw[8:40]
    key_len = 32 if node_type == 0 else 16
    encrypted_key = raw[40 : 40 + key_len]
    expected_hmac = raw[40 + key_len : 40 + key_len + 32]
    body = raw[: 40 + key_len]

    aes_xor_key, hmac_key = derive_password_link_keys(password, salt, algo=algo)

    # Verify HMAC-SHA256 over the body using the derived hmac key
    computed_hmac = _hmac.new(hmac_key, body, hashlib.sha256).digest()
    if not _hmac.compare_digest(computed_hmac, expected_hmac):
        raise ValueError("Wrong password (HMAC verification failed)")

    # strict: a truncated link would otherwise yield a SHORT key that is
    # silently wrong instead of an error (the HMAC above already covers
    # this case, so strict is the belt to that braces).
    plain_key = bytes(a ^ b for a, b in zip(encrypted_key, aes_xor_key[:key_len], strict=True))
    return node_type, public_handle, plain_key
