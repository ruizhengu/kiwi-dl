"""Advisory file lock shared by the config, proxy-pool and state stores.

`fcntl.flock` blocks other processes AND other open descriptors in the same
process, so two store instances anywhere (threads, instances, or processes)
are serialized. Acquisition is bounded by a timeout and never silently skipped.

A lock file may be removed, but only by its holder and only while it still
holds it (`release(remove=True)`). That is safe because `acquire` does not
trust a lock until the path still names the file it locked: `flock` locks an
inode, not a name, so a waiter that opened the file before it was removed
would otherwise win the lock on an orphan while a newcomer created and locked
a fresh file under the same name - two owners. The waiter sees the name moved,
drops the orphan and contends on the current file instead.

Only a file this module created is ever removed. A lock path can collide with
a real file (a folder share may hold both `a.bin` and `a.bin.mbclaim`), so a
lock file is created exclusively and stamped with `LOCK_FILE_MARKER`, and
removal requires the marker. An existing file is opened as it is and never
written to.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from pathlib import Path

LOCK_FILE_MARKER = b"kiwi-dl lock file; safe to delete when no kiwi-dl transfer is running\n"


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
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                try:
                    return os.open(path, os.O_RDWR)
                except FileNotFoundError:
                    continue  # removed between the two opens: create it
            # A lock without its marker still locks; it is only never removed.
            # Another process can lock and release this file before the stamp
            # lands; it then leaves the file behind, which is untidy but safe.
            with contextlib.suppress(OSError):
                os.write(fd, LOCK_FILE_MARKER)
            return fd

    def _lock(self, fd: int, deadline: float, timeout: float) -> None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        """Unlock; with `remove`, delete the lock file first, still locked.

        Deleting before unlocking is what makes removal safe: anyone who
        opened the old file and locks it after this sees the name gone in
        `acquire` and starts over. Only a file stamped by `_open` is deleted;
        a name that points elsewhere, or at a file this module did not
        create, is left alone.
        """
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if remove:
                with contextlib.suppress(OSError):
                    if self._is_removable(fd):
                        os.unlink(self.path)
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
