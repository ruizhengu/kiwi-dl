"""Memory must never be sized by something an attacker chose.

Three separate places sized an allocation from untrusted input:

* `decode_elc_payload` gunzipped a blob taken straight from the PASTED LINK,
  before credentials were looked up and before any network call. Deflate
  reaches ~1029:1, so a 544 KB link expanded to 400 MiB and a 5 MB link to
  ~8 GB.
* Service responses were parsed with `response.json()` / `.text`, which buffer
  AND gzip-inflate the whole body first. The DLC cap tried to catch this with
  `len(response.text) > MAX` - a check that runs only after the damage is done.
  The chunk downloader had no ceiling at all on its `iter_content` accumulator.
* The chunk plan was built from a SERVER-DECLARED file size. A hostile
  MegaCrypter host declaring 1 PiB yields ~1.07 billion Chunk objects
  (~172 GiB) in a loop that runs for hours, before the first HTTP request.

Every test here asserts the guard FIRES; none of them allocates the sizes that
made the original reports interesting.
"""

from __future__ import annotations

import pytest

from kiwi_dl.core.chunks import MAX_CHUNKS, MAX_FILE_SIZE, Chunk, chunk_count, iter_chunks
from kiwi_dl.core.downloader import (
    DeclaredSizeError,
    InsufficientDiskSpaceError,
    MegaDownloader,
)
from kiwi_dl.core.errors import NonRetryableTransferError

# ---------------------------------------------------------------------------
# B7 - decompression bombs in the link payload
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B7 - unbounded response bodies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B7 - unbounded chunk accumulation in the downloader
# ---------------------------------------------------------------------------

TOTAL = 4 * 1024 * 1024


class _OverlongChunkResponse:
    """A valid-looking 206 with no Content-Length and an endless body.

    `range_validation` only checks Content-Length IF PRESENT, so this response
    passes range validation and then streams forever.
    """

    status_code = 206

    def __init__(self, offset: int, size: int) -> None:
        self.headers = {"Content-Range": f"bytes {offset}-{offset + size - 1}/{TOTAL}"}
        self.yielded = 0

    def iter_content(self, chunk_size=65536):
        while True:
            self.yielded += chunk_size
            yield b"y" * chunk_size

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self) -> None:
        return None


def test_chunk_body_cannot_exceed_the_requested_range(monkeypatch):
    from kiwi_dl.core.state import TransferState

    offset, size = 1024, 1024
    response = _OverlongChunkResponse(offset, size)
    downloader = MegaDownloader(api=None, verify_integrity=False, max_workers=1)
    downloader._cdn_url = "https://cdn.invalid/file"
    monkeypatch.setattr("kiwi_dl.core.downloader.requests.get", lambda *a, **kw: response)
    state = TransferState(
        transfer_type="download",
        source="https://mega.nz/file/ID#key",
        destination="unused",
        total_size=TOTAL,
    )
    with pytest.raises(NonRetryableTransferError, match="more than the requested range"):
        downloader._download_chunk(
            Chunk(index=1, offset=offset, size=size), b"\x00" * 16, b"\x00" * 8, "unused", state
        )
    assert response.yielded <= size + 65536, "the read must stop at the first over-long block"


# ---------------------------------------------------------------------------
# B8 - the server's declared size is a claim, not a fact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared",
    [
        1 << 50,  # 1 PiB, as a hostile MegaCrypter host would declare
        MAX_FILE_SIZE + 1,
        -1,
        True,
        "12",
        None,
    ],
)
def test_absurd_declared_sizes_are_refused_before_any_allocation(monkeypatch, tmp_path, declared):
    exploded = False

    def _tripwire(size):
        nonlocal exploded
        exploded = True
        return iter(())

    monkeypatch.setattr("kiwi_dl.core.downloader.iter_chunks", _tripwire)
    downloader = MegaDownloader(api=None, verify_integrity=False, max_workers=1)
    with pytest.raises(DeclaredSizeError):
        downloader._run_download(
            cdn_url="https://cdn.invalid/file",
            file_size=declared,
            aes_key=b"\x00" * 16,
            nonce=b"\x00" * 8,
            mac_iv_a32=[0, 0],
            destination=tmp_path / "out.bin",
            source="mc://mc.example/token",
            on_progress=None,
        )
    assert not exploded, "the chunk plan must never be built for a rejected size"


def test_chunk_count_is_computed_without_building_the_chunks():
    """The guard cannot depend on the loop it exists to prevent."""
    assert chunk_count(1 << 50) == 1_073_741_828  # 1 PiB, never enumerated
    # Agreement with the real generator on sizes small enough to enumerate.
    for size in (0, 1, 128 * 1024, 1024 * 1024, 5 * 1024 * 1024, 12 * 1024 * 1024):
        assert chunk_count(size) == len(list(iter_chunks(size)))
    assert chunk_count(MAX_FILE_SIZE) == MAX_CHUNKS


def test_preallocation_checks_free_disk_space(monkeypatch, tmp_path):
    monkeypatch.setattr("kiwi_dl.core.downloader.available_disk_space", lambda p: 1024)
    downloader = MegaDownloader(api=None, verify_integrity=False, max_workers=1)
    destination = tmp_path / "big.bin"
    with pytest.raises(InsufficientDiskSpaceError, match="Not enough free space"):
        downloader._run_claimed_download(
            file_size=10 * 1024 * 1024,
            aes_key=b"\x00" * 16,
            nonce=b"\x00" * 8,
            mac_iv_a32=[0, 0],
            destination=destination,
            source="https://mega.nz/file/ID#key",
            on_progress=None,
            all_chunks=list(iter_chunks(10 * 1024 * 1024)),
        )
    assert not destination.exists(), "nothing may be preallocated when it cannot fit"
