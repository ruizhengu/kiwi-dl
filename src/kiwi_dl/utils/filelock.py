"""Advisory file lock shared by the config, proxy-pool and state stores.

The lock serializes users across processes AND across handles in one
process: `fcntl.flock` on macOS and Linux, a `msvcrt.locking` byte-range
lock on Windows (one byte just past the marker - see `_LOCK_OFFSET` - since
Windows locks are mandatory and must never block the stamp below).
Acquisition is bounded by a timeout and never silently skipped.

A lock file may be removed, but only by its holder and only while it still
holds it (`release(remove=True)`). That is safe because `acquire` does not
trust a lock until the path still names the file it locked: the underlying
lock guards a file, not a name, so a waiter that opened the file before it
was removed would otherwise win the lock on an orphan while a newcomer
created and locked a fresh file under the same name - two owners. The
waiter sees the name moved, drops the orphan and contends on the current
file instead.

Windows cannot unlink an open file, so there the holder removes the file
*after* unlocking it, re-verifying both the identity it held and the
marker on a fresh descriptor first. The gap between unlock and unlink is
covered by the same re-check that covers every other surprise: a lock won
on a file whose name has since moved is dropped, never trusted.

Only a file this module created is ever removed. A lock path can collide with
a real file (a folder share may hold both `a.bin` and `a.bin.mbclaim`), so a
lock file is created exclusively and stamped with `LOCK_FILE_MARKER`, and
removal requires the marker - which is why the stamp is verified before the
descriptor is handed out (`_stamp`): a fresh file that cannot be stamped is
removed again and the acquisition fails, never left behind unstamped. An
existing file is opened as it is and never written to.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

LOCK_FILE_MARKER = b"kiwi-dl lock file; safe to delete when no kiwi-dl transfer is running\n"

# Without O_BINARY the Windows CRT opens the descriptor in text mode and the
# marker's "\n" lands as "\r\n" - byte comparisons with the marker then fail.
_BINARY = getattr(os, "O_BINARY", 0)

if sys.platform == "win32":
    import msvcrt

    # Windows byte-range locks are MANDATORY: while a handle holds the lock,
    # every other handle's write AND read of that range fails. The lock must
    # therefore sit beyond the marker bytes, or the creator's stamp can lose
    # a race against a rival that opened the fresh file and locked first -
    # leaving a sidecar that can neither be stamped nor ever removed.
    _LOCK_OFFSET = 1000

    def _try_lock(fd: int) -> None:
        """One non-blocking exclusive-lock attempt on the lock byte."""
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLockError(Exception):  # noqa: N818 - a timeout, not a generic error
    """Raised when the advisory file lock cannot be acquired in time."""


class FileLock:
    """A bounded exclusive lock backed by a sidecar file."""

    def __init__(self, path: Path, message: str | None = None):
        self.path = path
        self._message = message
        self._fd: int | None = None

    def acquire(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            fd = self._open()
            try:
                self._lock(fd, deadline, timeout)
                current = self._path_names(fd)
            except BaseException:
                os.close(fd)
                raise
            if current:
                self._fd = fd
                return
            # The holder we waited on removed the file before letting go, so
            # this lock guards a name nobody uses any more. Closing drops it;
            # the next open finds the current file (or creates it) - but only
            # within the deadline, however often the name keeps moving.
            os.close(fd)
            if time.monotonic() >= deadline:
                raise self._timeout_error(timeout)

    def _open(self) -> int:
        """Open the lock file, creating and stamping it if it does not exist."""
        path = str(self.path)
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | _BINARY)
            except FileExistsError:
                try:
                    return os.open(path, os.O_RDWR | _BINARY)
                except FileNotFoundError:
                    continue  # removed between the two opens: create it
            self._stamp(fd, path)
            return fd

    def _stamp(self, fd: int, path: str) -> None:
        """Write the marker onto a lock file this call just created, and prove
        it landed.

        The stamp is what makes later removal safe, so "the write may have
        failed" is not good enough to leave behind: under Windows churn a
        write can fail or land unseen while rivals create and delete the name
        around ours, and a sidecar without its marker can never be cleaned
        up. The fd is read back after writing; if the marker is not there
        after a bounded number of rounds, the fresh file is this process's
        own and is removed before raising - the caller's claim moves to the
        next candidate name.
        """
        try:
            for _ in range(5):
                stamped = False
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, LOCK_FILE_MARKER)
                    os.lseek(fd, 0, os.SEEK_SET)
                    stamped = os.read(fd, len(LOCK_FILE_MARKER) + 1) == LOCK_FILE_MARKER
                except OSError:
                    stamped = False  # e.g. a rival's lock made this range unreadable
                if stamped:
                    return
                time.sleep(0.05)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(path)  # ours, freshly created, unstamped: do not leave it
        raise OSError(f"could not stamp the lock file {path}; the claim moves on")

    def _lock(self, fd: int, deadline: float, timeout: float) -> None:
        while True:
            try:
                _try_lock(fd)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise self._timeout_error(timeout) from None
                time.sleep(0.05)

    def _timeout_error(self, timeout: float) -> FileLockError:
        return FileLockError(
            self._message
            or (
                f"Could not acquire the lock {self.path.name} within "
                f"{timeout:.0f}s; another operation is holding it. "
                "Retry after it finishes."
            )
        )

    def _path_names(self, fd: int) -> bool:
        """True when `self.path` is still the file open on `fd`."""
        try:
            on_disk = os.stat(self.path)
        except FileNotFoundError:
            return False
        held = os.fstat(fd)
        return (on_disk.st_dev, on_disk.st_ino) == (held.st_dev, held.st_ino)

    def _is_removable(self, fd: int) -> bool:
        """True when `self.path` is the lock file this module created."""
        if not self._path_names(fd):
            return False
        return os.pread(fd, len(LOCK_FILE_MARKER) + 1, 0) == LOCK_FILE_MARKER

    def release(self, *, remove: bool = False) -> None:
        """Unlock; with `remove`, delete the lock file.

        On macOS/Linux it is deleted while still locked: anyone who opened
        the old file and locks it after this sees the name gone in `acquire`
        and starts over. Windows cannot unlink an open file, so there the
        holder unlocks, closes, and only then deletes - re-verified first,
        as the module docstring describes. Only a file stamped by `_open` is
        deleted; a name that points elsewhere, or at a file this module did
        not create, is left alone.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if sys.platform == "win32":
            self._release_windows(fd, remove=remove)
            return
        try:
            if remove:
                with contextlib.suppress(OSError):
                    if self._is_removable(fd):
                        os.unlink(self.path)
            with contextlib.suppress(OSError):
                _unlock(fd)
        finally:
            os.close(fd)

    def _release_windows(self, fd: int, *, remove: bool) -> None:
        """Windows release: unlock and close before any removal is possible."""
        identity: tuple[int, int] | None = None
        if remove and self._path_names(fd):
            held = os.fstat(fd)
            identity = (held.st_dev, held.st_ino)
        try:
            with contextlib.suppress(OSError):
                _unlock(fd)
        finally:
            os.close(fd)
        if identity is not None:
            self._unlink_marked(identity)

    def _unlink_marked(self, identity: tuple[int, int]) -> None:
        """Delete `self.path` when it is still the marked file we held."""
        try:
            fd = os.open(self.path, os.O_RDWR | _BINARY)
        except OSError:
            return
        try:
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != identity:
                return  # the name moved on after we let go: not ours to remove
            if os.read(fd, len(LOCK_FILE_MARKER) + 1) != LOCK_FILE_MARKER:
                return
        except OSError:
            return
        finally:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(self.path)
