"""The Windows arm of the advisory file lock (`msvcrt.locking`).

These run only on Windows - the CI runner is the only place the byte-range
backend actually executes. The macOS/Linux behaviour of the same class is
covered by the state-exclusivity and config-safety suites, which exercise
the POSIX backend through the same public API.
"""

from __future__ import annotations

import sys
import time

import pytest

from kiwi_dl.utils.filelock import LOCK_FILE_MARKER, FileLock, FileLockError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="msvcrt backend")


def test_two_handles_in_one_process_are_serialized(tmp_path):
    """Byte-range locks refuse a second handle in one process, like `flock`."""
    lock_path = tmp_path / "serial.lock"
    first = FileLock(lock_path)
    first.acquire(timeout=5)
    try:
        second = FileLock(lock_path)
        with pytest.raises(FileLockError):
            second.acquire(timeout=0.2)
    finally:
        first.release()


def test_release_removes_a_stamped_lock_file(tmp_path):
    lock_path = tmp_path / "claimed.lock"
    lock = FileLock(lock_path)
    lock.acquire(timeout=5)
    lock.release(remove=True)
    assert not lock_path.exists()


def test_release_keeps_a_foreign_file(tmp_path):
    lock_path = tmp_path / "sidecar"
    lock_path.write_bytes(b"not a kiwi-dl lock")
    lock = FileLock(lock_path)
    lock.acquire(timeout=5)
    lock.release(remove=True)
    assert lock_path.exists()
    assert lock_path.read_bytes() == b"not a kiwi-dl lock"


def test_a_timeout_raises_with_the_message(tmp_path):
    lock_path = tmp_path / "busy.lock"
    holder = FileLock(lock_path)
    holder.acquire(timeout=5)
    try:
        waiter = FileLock(lock_path, message="state store is busy")
        start = time.monotonic()
        with pytest.raises(FileLockError, match="state store is busy"):
            waiter.acquire(timeout=0.2)
        assert time.monotonic() - start < 5
    finally:
        holder.release()


def test_the_marker_is_written_and_removal_needs_it(tmp_path):
    """Windows byte-range locks refuse reads of the locked range from another
    handle, so the stamp is checked once the lock is gone."""
    lock_path = tmp_path / "stamped.lock"
    lock = FileLock(lock_path)
    lock.acquire(timeout=5)
    lock.release()  # no remove: the sidecar stays, unlocked
    assert lock_path.read_bytes() == LOCK_FILE_MARKER

    second = FileLock(lock_path)
    second.acquire(timeout=5)
    second.release(remove=True)
    assert not lock_path.exists()
