"""`kiwi-dl info` — inspect a MEGA link without downloading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from ..core.crypto import (
    a32_to_bytes,
    b64_url_decode,
    decrypt_attributes,
    str_to_a32,
    unpack_file_key,
)
from ..core.errors import MegaError
from ..core.links import LinkType, parse_link, require_link_key, resolve_password_link
from ..core.share_keys import share_root_handles, unwrap_share_node_key
from ..ui.prompts import print_error
from ..ui.theme import SafeTable, make_console
from ..utils.helpers import format_bytes
from .api_support import api_for

_console = make_console()


def _subtree(raw_nodes: list[Any], root: str) -> list[Any]:
    """The nodes under `root` (root included), by parent -> children walk.

    A folder share lists the whole tree flat, so narrowing to one subfolder
    means following the `p` (parent) links down from its handle.
    """
    children: dict[str, list[str]] = {}
    for n in raw_nodes:
        children.setdefault(n.get("p", ""), []).append(n.get("h", ""))
    keep = {root}
    stack = [root]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child and child not in keep:
                keep.add(child)
                stack.append(child)
    return [n for n in raw_nodes if n.get("h") in keep]


def _share_root_handle(raw_nodes: list[Any]) -> str:
    """The share's own root: the one node whose parent is not in the listing."""
    handles = {n.get("h") for n in raw_nodes}
    for node in raw_nodes:
        if node.get("p") not in handles:
            return str(node.get("h") or "")
    return str(raw_nodes[0].get("h") or "") if raw_nodes else ""


def _folder_files(raw_nodes: list[Any], link_key: str, root_handle: str) -> list[dict[str, Any]]:
    """The files a download of this share would write, as data.

    Goes through the downloader's own `plan_file_jobs`, so `path` is the exact
    string `--include` matches against and the exact one a download writes -
    a second implementation here would drift, and silently: the listing would
    look right while a pattern taken from it matched nothing. `plan_file_jobs`
    is the disk-free half, so nothing is created by asking.

    The root is neutral (`.`) because only the RELATIVE path is reported, and
    that part does not depend on where the download would eventually go.
    """
    from ..core.folder_downloader import MegaFolderDownloader

    folder_key = a32_to_bytes(str_to_a32(link_key))
    nodes = MegaFolderDownloader._decrypt_folder_nodes(raw_nodes, folder_key)
    root = Path(".")
    return [
        {
            "path": destination.relative_to(root).as_posix(),
            "size": int(node.size or 0),
            "handle": node.handle,
        }
        for node, destination in MegaFolderDownloader.plan_file_jobs(nodes, root, root_handle)
    ]


class _Fields:
    """One record of the answer, rendered either as a table or as JSON.

    Every branch below used to append straight to a `SafeTable`, so adding a
    machine-readable mode meant either a second set of `add_row`-shaped calls
    or reading values back out of Rich renderables. Both drift. Values are
    recorded once here and each renderer reads the same list.

    `raw` is the machine value where it differs from the displayed one: sizes
    go out as an integer number of bytes, never the "103.03 MB" a consumer
    would have to parse back.
    """

    def __init__(self, title: str) -> None:
        self.title = title
        self.rows: list[tuple[str, str, Any]] = []
        self.files: list[dict[str, Any]] | None = None

    def add_row(self, key: str, value: str, raw: Any = None) -> None:
        self.rows.append((key, value, value if raw is None else raw))

    # The "Files" row is a COUNT and the listing below is an ARRAY. Both would
    # land on `files`, and the array is assigned second, so the count vanished
    # silently - a consumer reading it got a list where it expected a number.
    # The count takes the distinct name; the array keeps `files`, which is what
    # a caller reaches for.
    _JSON_KEYS = {"files": "file_count"}

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, _display, raw in self.rows:
            name = key.lower().replace(" ", "_")
            payload[self._JSON_KEYS.get(name, name)] = raw
        if self.files is not None:
            payload["files"] = self.files
        return payload


def _emit(fields: _Fields, json_mode: bool) -> None:
    """Print the collected answer in the caller's chosen shape.

    JSON goes to stdout through `click.echo` and nothing else is printed on
    that stream, so a consumer can parse it whole. The Rich table is the
    interactive rendering and never appears in JSON mode.
    """
    if json_mode:
        click.echo(json.dumps(fields.as_json(), ensure_ascii=False))
        return
    table = SafeTable(show_header=False, title=fields.title, border_style="kiwi.table.border")
    table.add_column("Field", style="kiwi.info")
    table.add_column("Value", style="kiwi.value")
    for key, display, _raw in fields.rows:
        table.add_row(key, display)
    _console.print(table)
    if fields.files:
        listing = SafeTable(show_header=True, border_style="kiwi.table.border")
        listing.add_column("Size", justify="right", style="kiwi.value")
        listing.add_column("Path")
        for item in fields.files:
            listing.add_row(format_bytes(int(item["size"])), item["path"])
        _console.print(listing)


@click.command("info", short_help="Show public MEGA link metadata; no account or MFA needed.")
@click.argument("url")
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    help="Emit the answer as one JSON object on stdout (folder shares include a file list).",
)
@click.option(
    "--password",
    default=None,
    envvar="KIWI_DL_PASSWORD",
    help="Password for protected links (or set KIWI_DL_PASSWORD).",
)
@click.pass_context
def info_cmd(
    ctx: click.Context,
    url: str,
    json_mode: bool,
    password: str | None,
) -> None:
    """Resolve a public URL and print metadata. No account or MFA needed."""
    cfg = ctx.obj["config"]
    try:
        parsed = parse_link(url)
    except ValueError as exc:
        print_error(str(exc))
        return

    if parsed.type == LinkType.PASSWORD_PROTECTED:
        if not password:
            print_error("Link is password-protected; supply --password.")
            return
        try:
            parsed = resolve_password_link(parsed, password)
        except ValueError as exc:
            print_error(str(exc))
            return

    api = api_for(cfg)
    fields = _Fields("Link info")

    try:
        if parsed.type in (LinkType.FOLDER, LinkType.FOLDER_IN_FOLDER):
            listing = api.get_public_folder_listing(parsed.public_id)
            raw_nodes = listing.get("f", [])
            if parsed.type == LinkType.FOLDER_IN_FOLDER and parsed.subpath:
                # Built before the narrowing below: it answers whether the
                # subfolder exists in the *whole* share.
                by_handle = {n.get("h"): n for n in raw_nodes}
                raw_nodes = _subtree(raw_nodes, parsed.subpath)
                if parsed.subpath not in by_handle:
                    print_error(
                        f"Subfolder {parsed.subpath!r} not found in folder {parsed.public_id!r}"
                    )
                    return
                fields.add_row("Type", "Folder (inside folder share)")
                fields.add_row("Subfolder handle", parsed.subpath)
            else:
                fields.add_row("Type", "Folder share")
            fields.add_row("Public ID", parsed.public_id)
            fields.add_row("Node count", str(len(raw_nodes)), raw=len(raw_nodes))
            file_count = sum(1 for n in raw_nodes if n.get("t") == 0)
            total_size = sum(int(n.get("s", 0) or 0) for n in raw_nodes if n.get("t") == 0)
            fields.add_row("Files", str(file_count), raw=file_count)
            fields.add_row("Total size", format_bytes(total_size), raw=total_size)
            fields.files = _folder_files(
                raw_nodes,
                require_link_key(parsed, "link info"),
                root_handle=(
                    parsed.subpath
                    if parsed.type == LinkType.FOLDER_IN_FOLDER and parsed.subpath
                    else _share_root_handle(raw_nodes)
                ),
            )
        elif parsed.type == LinkType.FILE_IN_FOLDER:
            # Look up the file inside the parent folder listing, using the
            # folder share key to unwrap the file's key.
            folder_id = parsed.public_id
            file_handle = parsed.subpath
            folder_key = a32_to_bytes(str_to_a32(require_link_key(parsed, "link info")))
            listing = api.get_public_folder_listing(folder_id)
            raw_nodes = listing.get("f", [])
            file_raw = next(
                (n for n in raw_nodes if n.get("h") == file_handle and n.get("t") == 0), None
            )
            if file_raw is None:
                folder_raw = next(
                    (n for n in raw_nodes if n.get("h") == file_handle and n.get("t") == 1),
                    None,
                )
                if folder_raw is None:
                    print_error(f"Node {file_handle!r} not found in folder {folder_id!r}")
                    return
                # Reached only via `folder_raw`, which was found BY this
                # handle, so it cannot be None here.
                assert file_handle is not None
                subtree = _subtree(raw_nodes, file_handle)
                fields.add_row("Type", "Folder (inside folder share)")
                fields.add_row("Folder ID", folder_id)
                fields.add_row("Subfolder handle", file_handle or "?")
                fields.add_row("Node count", str(len(subtree)), raw=len(subtree))
                file_count = sum(1 for n in subtree if n.get("t") == 0)
                total_size = sum(int(n.get("s", 0) or 0) for n in subtree if n.get("t") == 0)
                fields.add_row("Files", str(file_count), raw=file_count)
                fields.add_row("Total size", format_bytes(total_size), raw=total_size)
                _emit(fields, json_mode)
                return
            # `k` may hold several `handle:key` items (see core/share_keys.py).
            unwrapped = unwrap_share_node_key(file_raw, folder_key, share_root_handles(raw_nodes))
            if unwrapped is None:
                print_error(f"No key on {file_handle!r} unwraps with the link key")
                return
            attrs = unwrapped[1] or {}
            fields.add_row("Type", "File (in folder share)")
            fields.add_row("Name", attrs.get("n", "?"))
            fields.add_row("Folder ID", folder_id)
            fields.add_row("File handle", file_handle or "?")
            fields.add_row(
                "Size",
                format_bytes(int(file_raw.get("s", 0) or 0)),
                raw=int(file_raw.get("s", 0) or 0),
            )
        else:
            info = api.get_public_file_info(parsed.public_id)
            if not parsed.key:
                fields.add_row("Type", "File")
                fields.add_row("Public ID", parsed.public_id)
                fields.add_row(
                    "Size", format_bytes(int(info.get("s", 0))), raw=int(info.get("s", 0))
                )
                _emit(fields, json_mode)
                return
            aes_key, _nonce, _mac = unpack_file_key(
                str_to_a32(require_link_key(parsed, "link info"))
            )
            attrs = decrypt_attributes(b64_url_decode(info.get("at", "") or ""), aes_key) or {}
            fields.add_row("Type", "File")
            fields.add_row("Name", attrs.get("n", "?"))
            fields.add_row("Public ID", parsed.public_id)
            fields.add_row("Size", format_bytes(int(info.get("s", 0))), raw=int(info.get("s", 0)))
            if "fa" in info:
                fields.add_row("File attributes", info["fa"])
    except MegaError as exc:
        print_error(f"Lookup failed: {exc}")
        return
    finally:
        # Every branch above returns early; the client is only ever used
        # inside this block, so release its sockets on all of them.
        api.close()

    _emit(fields, json_mode)
