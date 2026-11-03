"""A public-folder node's `k` can hold several `handle:key` items.

The defect, found on a live owner-shared folder: `k` came back as
`<owner>:<key>/<root>:<key>` - the owner's item (master-key wrapped) first, the
share's item (link-key wrapped, labelled with the shared folder's handle)
second. Every reader took the text after the FIRST ":", handed
`"<key>/<root>:<key>"` to the base64 decoder and skipped the node, so the
folder listed as empty and `mb download` failed with "No nodes returned".

Each reader is driven here with the live shape, in both orders, and with the
single-item shape the code was written for.
"""

from __future__ import annotations

import os

import pytest

from megabasterd_cli.core import download_source as ds
from megabasterd_cli.core.crypto import (
    aes_key_wrap_decrypt,
    b64_url_decode,
    b64_url_encode,
    bytes_to_a32,
    unpack_file_key,
)
from megabasterd_cli.core.folder_downloader import MegaFolderDownloader
from megabasterd_cli.core.links import parse_link
from megabasterd_cli.core.share_keys import (
    share_root_handles,
    unwrap_share_node_key,
    wrapped_key_candidates,
)
from tests.fake_mega import (
    FILE,
    FOLDER,
    MASTER_KEY,
    attrs_of,
    bytes_from_a32_pair,
    logged_in_client,
    share_listing,
)

FOLDER_KEY = os.urandom(16)
SHARE_ID = "SHAREID1"
ENTRIES = [
    ("root0001", "outside1", "album", FOLDER),
    ("file0001", "root0001", "one.mp4", FILE),
    ("sub00001", "root0001", "raw", FOLDER),
    ("file0002", "sub00001", "two.cr2", FILE),
]
SHAPES = [None, "first", "last"]
SHAPE_IDS = ["single-item", "owner-first", "owner-last"]


def test_candidates_put_the_preferred_handle_first():
    assert wrapped_key_candidates("own:AAA/root:BBB", {"root"}) == ["BBB", "AAA"]
    assert wrapped_key_candidates("root:BBB/own:AAA", {"root"}) == ["BBB", "AAA"]


def test_a_bare_or_single_item_key_is_its_own_candidate():
    assert wrapped_key_candidates("AAA") == ["AAA"]
    assert wrapped_key_candidates("own:AAA") == ["AAA"]
    assert wrapped_key_candidates("") == []


def test_the_share_root_is_the_node_whose_parent_is_not_listed():
    listing = share_listing(FOLDER_KEY, ENTRIES)
    assert share_root_handles(listing) == {"root0001"}


def test_the_live_shape_is_what_the_fixture_builds():
    """Pin the fixture to the observed wire shape, so the tests below test it."""
    k = share_listing(FOLDER_KEY, ENTRIES, owner_item="first")[1]["k"]
    owner, share = k.split("/")
    assert owner.split(":")[0] != "root0001"
    assert share.split(":")[0] == "root0001"
    assert len(share.split(":")[1]) == 43  # 32-byte file key, unpadded base64


@pytest.mark.parametrize("owner_item", SHAPES, ids=SHAPE_IDS)
def test_unwrap_returns_the_key_that_decrypts_the_attributes(owner_item):
    listing = share_listing(FOLDER_KEY, ENTRIES, owner_item=owner_item)
    key, attrs = unwrap_share_node_key(listing[1], FOLDER_KEY, share_root_handles(listing))
    assert attrs == {"n": "one.mp4"}
    assert len(key) == 32


def test_a_node_no_item_unwraps_for_is_reported_as_none():
    node = share_listing(FOLDER_KEY, ENTRIES)[1]
    assert unwrap_share_node_key(dict(node, k="own:%%%"), FOLDER_KEY) is None


def test_without_provable_attributes_the_preferred_item_is_still_used():
    """No attributes to prove a candidate: fall back to the share's own item,
    as the single-item readers did, rather than dropping the node."""
    node = share_listing(FOLDER_KEY, ENTRIES, owner_item="first")[1]
    key, attrs = unwrap_share_node_key(dict(node, a=""), FOLDER_KEY, {"root0001"})
    assert attrs is None
    share_wrapped = node["k"].split("/")[1].split(":")[1]
    assert key == aes_key_wrap_decrypt(b64_url_decode(share_wrapped), FOLDER_KEY)


@pytest.mark.parametrize("owner_item", SHAPES, ids=SHAPE_IDS)
def test_the_folder_downloader_lists_every_node(owner_item):
    listing = share_listing(FOLDER_KEY, ENTRIES, owner_item=owner_item)
    nodes = MegaFolderDownloader._decrypt_folder_nodes(listing, FOLDER_KEY)
    assert sorted(n.name for n in nodes) == ["album", "one.mp4", "raw", "two.cr2"]


@pytest.mark.parametrize("owner_item", SHAPES, ids=SHAPE_IDS)
def test_info_json_lists_the_files(owner_item):
    from megabasterd_cli.commands.info_cmd import _folder_files

    listing = share_listing(FOLDER_KEY, ENTRIES, owner_item=owner_item)
    files = _folder_files(listing, b64_url_encode(FOLDER_KEY), "root0001")
    assert sorted(f["path"] for f in files) == ["album/one.mp4", "album/raw/two.cr2"]


class _Api:
    def __init__(self, listing):
        self.listing = listing

    def get_public_folder_listing(self, public_id):
        return {"f": self.listing}

    def request(self, payload, extra_params=None):
        return {"g": "http://cdn.invalid/x", "s": 1024}


class _Downloader:
    def __init__(self, api):
        self.api = api

    def _get_with_quota_wait(self, fn):
        return fn()


@pytest.mark.parametrize("owner_item", SHAPES, ids=SHAPE_IDS)
def test_a_file_in_folder_link_resolves_the_right_key(owner_item):
    listing = share_listing(FOLDER_KEY, ENTRIES, owner_item=owner_item)
    parsed = parse_link(
        f"https://mega.nz/folder/{SHARE_ID}#{b64_url_encode(FOLDER_KEY)}/file/file0001"
    )
    _, key_a32, encrypted_attrs = ds._resolve_folder_file(_Downloader(_Api(listing)), parsed)
    aes_key, _, _ = unpack_file_key(key_a32)
    assert attrs_of(b64_url_encode(encrypted_attrs), aes_key) == {"n": "one.mp4"}


@pytest.mark.parametrize("owner_item", SHAPES, ids=SHAPE_IDS)
def test_importing_the_share_rewraps_the_share_key_item(owner_item):
    client, api = logged_in_client()
    api.shares[SHARE_ID] = share_listing(FOLDER_KEY, ENTRIES, owner_item=owner_item)

    handles = client.import_public_share(
        f"https://mega.nz/folder/{SHARE_ID}#{b64_url_encode(FOLDER_KEY)}"
    )

    assert len(handles) == len(ENTRIES)
    names = []
    for name, call in api.calls:
        if name != "import_node_from_share":
            continue
        node_key = aes_key_wrap_decrypt(b64_url_decode(call["wrapped_key"]), MASTER_KEY)
        is_file = call["node_type"] == FILE
        attr_key = bytes_from_a32_pair(bytes_to_a32(node_key)) if is_file else node_key[:16]
        names.append(attrs_of(call["encrypted_attrs"], attr_key)["n"])
    assert sorted(names) == ["album", "one.mp4", "raw", "two.cr2"]
