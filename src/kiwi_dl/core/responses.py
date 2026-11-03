"""Response shape guards for everything below the MEGA API boundary.

Everything below the API boundary used to index server-supplied JSON
blind: `nodes[0]["h"]`, `raw_node["t"]`, `result["privk"]`. A response
whose shape differs - a hostile server, a captive portal, a protocol
change - produced a KeyError/TypeError that the CLI catch-all rendered
as an uninterpretable `Error: 'p'`, and `{"s": "1234"}` propagated a
STRING into format_bytes and the chunk maths instead of failing here.
`export_link` already did this correctly; these make it universal.
"""

from __future__ import annotations

from typing import Any

from .errors import MegaError

_REQUIRED = object()


def _expect_mapping(value: Any, what: str) -> dict:
    if not isinstance(value, dict):
        raise MegaError(
            message=f"Unexpected MEGA response for {what}: "
            f"expected an object, got {type(value).__name__}"
        )
    return value


def _expect_field(mapping: dict, key: str, kind: type, what: str, default: Any = _REQUIRED) -> Any:
    """Return `mapping[key]`, proving its type before any caller can use it."""
    if key not in mapping:
        if default is _REQUIRED:
            raise MegaError(message=f"Malformed MEGA response for {what}: missing {key!r}")
        return default
    value = mapping[key]
    # bool is a subclass of int, and MEGA never sends one where a number or a
    # size belongs; accepting it would let `True` become a node type or a size.
    if (kind is int and isinstance(value, bool)) or not isinstance(value, kind):
        raise MegaError(
            message=f"Malformed MEGA response for {what}: {key!r} is "
            f"{type(value).__name__}, expected {kind.__name__}"
        )
    return value
