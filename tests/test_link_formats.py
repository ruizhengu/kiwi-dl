"""Tests for every MEGA URL flavour the parser must accept."""

import pytest

from kiwi_dl.core.links import LinkType, parse_link


@pytest.mark.parametrize(
    "url, expected_type, expected_id, expected_key, expected_sub",
    [
        # Modern
        ("https://mega.nz/file/ABC#KEY", LinkType.FILE, "ABC", "KEY", None),
        ("https://mega.nz/folder/ABC#KEY", LinkType.FOLDER, "ABC", "KEY", None),
        ("https://mega.nz/folder/ABC#KEY/file/XYZ", LinkType.FILE_IN_FOLDER, "ABC", "KEY", "XYZ"),
        (
            "https://mega.nz/folder/ABC#KEY/folder/SUB",
            LinkType.FOLDER_IN_FOLDER,
            "ABC",
            "KEY",
            "SUB",
        ),
        # Legacy file & folder
        ("https://mega.nz/#!ABC!KEY", LinkType.FILE, "ABC", "KEY", None),
        ("https://mega.nz/#F!ABC!KEY", LinkType.FOLDER, "ABC", "KEY", None),
        # Legacy: folder + subfolder
        ("https://mega.nz/#F!ABC@SUB!KEY", LinkType.FOLDER_IN_FOLDER, "ABC", "KEY", "SUB"),
        # Legacy: folder + file trailer
        ("https://mega.nz/#F!ABC!KEY!FILE", LinkType.FILE_IN_FOLDER, "ABC", "KEY", "FILE"),
        # Legacy compact file-in-folder (#F*)
        ("https://mega.nz/#F*FILE!FOLDER!KEY", LinkType.FILE_IN_FOLDER, "FOLDER", "KEY", "FILE"),
        # Alternate node form (#N!)
        ("https://mega.nz/#N!FILE!FOLDER!KEY", LinkType.FILE_IN_FOLDER, "FOLDER", "KEY", "FILE"),
    ],
)
def test_parse_link_types(url, expected_type, expected_id, expected_key, expected_sub):
    p = parse_link(url)
    assert p.type == expected_type, f"{url} -> {p.type}"
    assert p.public_id == expected_id
    assert p.key == expected_key
    assert p.subpath == expected_sub


@pytest.mark.parametrize(
    "url",
    ["mc://server.example/token", "mega://enc?xxx", "mega://fenc2?xxx", "mega://elc?xxx"],
    ids=["megacrypter", "enc", "fenc2", "elc"],
)
def test_third_party_link_wrappers_are_not_supported(url):
    """kiwi-dl reads MEGA's own link formats only."""
    with pytest.raises(ValueError, match="Not a recognizable MEGA link"):
        parse_link(url)


def test_parse_link_rejects_garbage():
    with pytest.raises(ValueError):
        parse_link("not a url")
    with pytest.raises(ValueError):
        parse_link("https://example.com/file/ABC#KEY")
