"""A finished download leaves only its file; an unfinished one keeps its sidecars.

Seen live (2026-11-04): after a single-file and a folder download completed
with the MAC verified, `NAME.mbclaim` and `NAME.mbstate.lock` (both empty)
stayed beside every file. Both were kept on purpose, because unlinking a lock
file after `release()` lets two processes own one name: one locks the orphaned
inode, another creates and locks a fresh file.

They are now deleted once the download has finished, while still locked, and
`FileLock.acquire` only trusts a lock whose path still names the file it
locked. The race tests below drive that interleaving step by step.

Review of PR #10 added two rules: only a lock file kiwi-dl created (stamped
with `LOCK_FILE_MARKER`) is ever deleted, since a real file can carry a
sidecar's name; and retrying after a moved name respects the timeout.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

from kiwi_dl.core.api import MegaAPIClient
from kiwi_dl.core.downloader import MegaDownloader
from kiwi_dl.core.errors import IntegrityError, TransferQuotaError
from kiwi_dl.core.folder_downloader import MegaFolderDownloader
from kiwi_dl.core.state import TransferState, clear_state, load_state, save_state
from kiwi_dl.utils import filelock as filelock_module
from kiwi_dl.utils.filelock import LOCK_FILE_MARKER, FileLock, FileLockError
from kiwi_dl.utils.helpers import CLAIM_SUFFIX, claim_destination, release_destination
from tests.fake_mega import FakeCdn

KiB = 1024


def _tree(root: Path) -> list[str]:
    """Every file under `root`, relative and sorted."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture()
def cdn(monkeypatch):
    cdn = FakeCdn(size=3 * 1024 * KiB)  # 7 chunks
    monkeypatch.setattr("requests.get", cdn.get)
    return cdn


# ---------------------------------------------------------------------------
# What a download leaves behind
# ---------------------------------------------------------------------------


def test_a_finished_download_leaves_only_its_file(cdn, tmp_path):
    destination = tmp_path / "movie.mkv"

    result = cdn.download_with(MegaDownloader(api=None, max_workers=2), destination)

    assert result.integrity_ok
    assert destination.read_bytes() == cdn.plaintext
    assert _tree(tmp_path) == ["movie.mkv"]


def test_a_finished_folder_download_leaves_only_its_files(cdn, tmp_path, monkeypatch):
    """Folder files run on their own worker downloaders; each cleans up."""

    def owned_file(self, folder_public_id, node, destination, on_progress):
        return cdn.download_with(self.downloader, destination)

    monkeypatch.setattr(MegaFolderDownloader, "_download_owned_file", owned_file)
    root = tmp_path / "share"
    jobs = []
    for relative in ("a.bin", "sub/b.bin", "sub/deeper/c.bin"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        jobs.append((object(), destination))

    folder = MegaFolderDownloader(MegaDownloader(api=MegaAPIClient(), max_workers=2))
    results = folder._download_file_jobs("PUBID", jobs, parallel_files=2)

    assert all(r.integrity_ok for r in results)
    assert _tree(root) == ["a.bin", "sub/b.bin", "sub/deeper/c.bin"]


def test_a_stopped_download_keeps_its_sidecars_until_the_resume_finishes(cdn, tmp_path):
    """The claim and the state lock are what a resume contends on."""
    destination = tmp_path / "f.bin"
    cdn.quota["direct"] = (128 + 256) * KiB  # the first two chunks, then 509

    with pytest.raises(TransferQuotaError):
        cdn.download_with(MegaDownloader(api=None, max_workers=1), destination)

    assert _tree(tmp_path) == ["f.bin", "f.bin.mbclaim", "f.bin.mbstate", "f.bin.mbstate.lock"]
    assert sorted(load_state(destination).completed_chunks) == [0, 1]

    del cdn.quota["direct"]
    result = cdn.download_with(MegaDownloader(api=None, max_workers=2), destination)

    assert result.integrity_ok
    assert _tree(tmp_path) == ["f.bin"]


def test_a_download_that_fails_its_mac_check_is_not_finished(cdn, tmp_path, monkeypatch):
    monkeypatch.setattr(MegaDownloader, "_verify_integrity", lambda self, *a: False)
    destination = tmp_path / "f.bin"

    with pytest.raises(IntegrityError):
        cdn.download_with(MegaDownloader(api=None, max_workers=2), destination)

    assert "f.bin.mbclaim" in _tree(tmp_path)
    assert "f.bin.mbstate.lock" in _tree(tmp_path)


@pytest.mark.parametrize("sidecar", ["movie.mkv.mbclaim", "movie.mkv.mbstate.lock"])
@pytest.mark.parametrize("content", [b"someone's data", b""])
def test_a_finished_download_keeps_a_real_file_named_like_its_sidecar(
    cdn, tmp_path, sidecar, content
):
    """A folder share can hold both `movie.mkv` and `movie.mkv.mbclaim`. The
    lock uses such a file as it is, and finishing must not delete it - even
    with `overwrite` off, which is the default here."""
    real = tmp_path / sidecar
    real.write_bytes(content)

    result = cdn.download_with(MegaDownloader(api=None, max_workers=2), tmp_path / "movie.mkv")

    assert result.integrity_ok
    assert real.read_bytes() == content
    assert _tree(tmp_path) == sorted(["movie.mkv", sidecar])


# ---------------------------------------------------------------------------
# Removing a lock file is safe only while holding it
# ---------------------------------------------------------------------------


def test_release_with_remove_deletes_the_lock_file(tmp_path):
    lock = FileLock(tmp_path / "x.lock")
    lock.acquire(timeout=0)
    assert (tmp_path / "x.lock").read_bytes() == LOCK_FILE_MARKER
    lock.release(remove=True)
    assert not (tmp_path / "x.lock").exists()


@pytest.mark.parametrize("content", [b"precious", b"", LOCK_FILE_MARKER + b"and more"])
def test_a_lock_never_writes_into_or_removes_a_file_it_did_not_create(tmp_path, content):
    path = tmp_path / "x.lock"
    path.write_bytes(content)
    lock = FileLock(path)
    lock.acquire(timeout=0)
    lock.release(remove=True)
    assert path.read_bytes() == content


def test_a_waiter_on_a_removed_lock_file_moves_to_the_current_one(tmp_path, monkeypatch):
    """A holds -> B opens the same file and waits -> A removes it and lets go
    -> B gets the lock on the orphan. C then locks the name. B must have
    noticed the name moved and be holding the CURRENT file, so C is refused.
    """
    path = tmp_path / "shared.lock"
    a = FileLock(path)
    a.acquire(timeout=0)

    b_opened = threading.Event()
    real_open = os.open

    def open_and_signal(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if threading.current_thread().name == "waiter-b":
            b_opened.set()
        return fd

    monkeypatch.setattr(filelock_module.os, "open", open_and_signal)
    b = FileLock(path)
    waiter = threading.Thread(target=b.acquire, args=(5,), name="waiter-b")
    waiter.start()
    assert b_opened.wait(5), "B never opened the lock file"

    a.release(remove=True)  # B holds an fd to the file just unlinked
    waiter.join(5)
    assert not waiter.is_alive()
    try:
        assert b._fd is not None
        held, on_disk = os.fstat(b._fd), os.stat(path)
        b_holds_the_current_file = (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)
        assert b_holds_the_current_file, "B holds the unlinked file: anyone may lock the name"
        c = FileLock(path)
        with pytest.raises(FileLockError):
            c.acquire(timeout=0)
    finally:
        b.release()


def test_remove_leaves_a_file_that_is_not_ours_alone(tmp_path):
    """If the name was replaced behind the holder, the new file is someone
    else's and must survive the holder's cleanup."""
    path = tmp_path / "x.lock"
    lock = FileLock(path)
    lock.acquire(timeout=0)
    path.unlink()
    path.write_bytes(b"")
    lock.release(remove=True)
    assert path.exists()


def _lose_the_name_after_every_open(monkeypatch, path: Path) -> list[int]:
    """Rival holders that remove `path` between our open() and our flock()."""
    real_open = os.open
    opens: list[int] = []
    give_up_at = time.monotonic() + 5.0  # fail instead of hanging

    def open_then_lose_the_name(file, flags, *args, **kwargs):
        fd = real_open(file, flags, *args, **kwargs)
        if str(file) == str(path):
            opens.append(fd)
            assert time.monotonic() < give_up_at, "acquire kept retrying past its timeout"
            os.unlink(file)
        return fd

    monkeypatch.setattr(filelock_module.os, "open", open_then_lose_the_name)
    return opens


@pytest.mark.parametrize("timeout", [0, 0.2])
def test_a_name_that_keeps_moving_cannot_hold_acquire_past_its_timeout(
    tmp_path, monkeypatch, timeout
):
    path = tmp_path / "x.lock"
    opens = _lose_the_name_after_every_open(monkeypatch, path)
    started = time.monotonic()

    with pytest.raises(FileLockError):
        FileLock(path).acquire(timeout=timeout)

    assert time.monotonic() - started < timeout + 1.0
    if timeout == 0:
        assert len(opens) == 1, "a try-lock must make exactly one attempt"


def test_a_claim_moves_on_when_a_name_keeps_moving(tmp_path, monkeypatch):
    """`claim_destination` holds the process-wide claim lock while it tries
    each name, so a name that never settles must cost one attempt, not a hang."""
    _lose_the_name_after_every_open(monkeypatch, tmp_path / ("movie.mkv" + CLAIM_SUFFIX))

    claimed = claim_destination(tmp_path / "movie.mkv")
    try:
        assert claimed == tmp_path / "movie (1).mkv"
    finally:
        release_destination(claimed)


def test_clear_state_with_remove_lock_leaves_nothing(tmp_path):
    destination = tmp_path / "out.bin"
    save_state(
        TransferState(
            transfer_type="download", source="u", destination=str(destination), total_size=1
        )
    )
    clear_state(destination, remove_lock=True)
    assert _tree(tmp_path) == []


def test_a_finished_claim_does_not_block_the_next_one(tmp_path):
    target = tmp_path / "movie.mkv"
    first = claim_destination(target)
    release_destination(first, remove_claim=True)
    assert not (tmp_path / ("movie.mkv" + CLAIM_SUFFIX)).exists()

    again = claim_destination(target)
    try:
        assert again == first
    finally:
        release_destination(again)


def _claim_and_finish_repeatedly(args):
    """Worker PROCESS: claim, prove sole ownership, finish (removing the claim)."""
    directory, ready_dir, worker_count, rounds = args
    import time
    from pathlib import Path

    from kiwi_dl.utils.helpers import claim_destination, release_destination

    # Start together, or a spawned worker can finish before the next exists.
    Path(ready_dir, f"{os.getpid()}.ready").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 30
    while len(list(Path(ready_dir).glob("*.ready"))) < worker_count:
        if time.monotonic() > deadline:
            break
        time.sleep(0.01)

    clashes = 0
    for _ in range(rounds):
        claimed = claim_destination(Path(directory) / "shared.bin")
        marker = Path(directory) / (claimed.name + ".owner")
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            clashes += 1  # another process holds this destination too
        else:
            os.close(fd)
            os.unlink(marker)
        release_destination(claimed, remove_claim=True)
    return clashes


def test_processes_finishing_and_claiming_one_name_never_share_it(tmp_path):
    """The cross-process version: claims that are removed on finish still
    give every destination exactly one owner at a time."""
    out = tmp_path / "out"
    ready = tmp_path / "ready"
    out.mkdir()
    ready.mkdir()
    workers = 4
    ctx = mp.get_context("spawn")
    args = [(str(out), str(ready), workers, 500)] * workers
    with ctx.Pool(workers) as pool:
        clashes = pool.map(_claim_and_finish_repeatedly, args)

    assert sum(clashes) == 0, f"two processes held one destination {sum(clashes)} times"
    # A rival can finish on a sidecar in the instant between its creation and
    # its stamp; unstamped, it is kept rather than deleted. Any such leftover
    # is stamped by now, and the next finished claim of that name removes it.
    for sidecar in out.glob("*" + CLAIM_SUFFIX):
        assert sidecar.read_bytes() == LOCK_FILE_MARKER, f"{sidecar.name} is not a kiwi-dl lock"
        claimed = claim_destination(out / sidecar.name.removesuffix(CLAIM_SUFFIX))
        release_destination(claimed, remove_claim=True)
    assert not list(out.glob("*" + CLAIM_SUFFIX)), "a finished claim left its sidecar"
