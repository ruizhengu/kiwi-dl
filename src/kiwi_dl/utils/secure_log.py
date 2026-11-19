r"""The one secure-open helper for log files.

The invariant: a log file is owner-only *before* its first byte is written, or
file logging does not happen. There is no ordinary-open fallback anywhere on
this path, because a fallback is exactly how the guarantee was once lost: an
append created the file with the ambient umask (normally 0644).

Two rules follow from "before the first byte":

* The file is created explicitly, not as a side effect of the first write:
  `O_CREAT|O_EXCL` with mode 0600 (POSIX). Creating permissively and fixing
  it afterwards is not equivalent - it leaves a window in which anyone can
  read the log.
* The result is *verified* rather than assumed, with `fstat` on the open
  descriptor. A hardening call that silently failed used to leave a
  world-readable log behind a comment claiming otherwise.

A pre-existing path is tightened in place and re-verified, never unlinked and
replaced: the path is arbitrary and may be a file the caller cares about. If it
cannot be made owner-only, `InsecureLogFileError` is raised and nothing is
written.

Windows has no POSIX mode bits, so "owner-only" there means: a plain regular
file, never a reparse point (symlink, junction), inside the per-user profile
whose ACLs already exclude other users (`%LOCALAPPDATA%\kiwi-dl\logs`). The
same fail-closed shape applies - anything that cannot be verified raises
`InsecureLogFileError` and nothing is written.
"""

from __future__ import annotations

import os
import stat

OWNER_ONLY = 0o600

# Windows `os.lstat` reports symlinks and junctions through this attribute;
# POSIX stat results simply do not carry it.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class InsecureLogFileError(OSError):
    """The log file could not be established as owner-only, so it must not be written.

    Subclasses `OSError` so callers that already treat log writing as
    non-fatal (`except OSError: pass`) keep degrading gracefully instead of
    crashing the CLI over a log file.
    """


def _is_reparse(info: os.stat_result) -> bool:
    """True for a symlink or any other reparse point, on either platform."""
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _verify_posix(fd: int, path: str) -> None:
    """Prove, on the open descriptor, that this is our own owner-only regular file.

    On the descriptor and not the path: anything checked by name can be swapped
    between the check and the write.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise InsecureLogFileError(f"log path is not a regular file: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode != OWNER_ONLY:
        raise InsecureLogFileError(f"log file is {oct(mode)}, not {oct(OWNER_ONLY)}: {path}")
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and info.st_uid != geteuid():
        raise InsecureLogFileError(f"log file is owned by uid {info.st_uid}: {path}")


def _open_posix(path: str) -> int:
    # O_NOFOLLOW on every open: a symlink planted at the log path must never
    # redirect the write, and must never be "handled" by falling back. It is
    # looked up defensively, so a faked platform cannot crash the open.
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, OWNER_ONLY)
    except FileExistsError:
        pass
    else:
        try:
            _verify_posix(fd, path)
        except BaseException:
            os.close(fd)
            raise
        return fd

    # Pre-existing: classify by lstat BEFORE opening, so a symlink or a special
    # file is rejected rather than opened and then reasoned about.
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise InsecureLogFileError(f"log path is a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise InsecureLogFileError(f"log path is not a regular file: {path}")

    fd = os.open(path, flags)
    try:
        os.fchmod(fd, OWNER_ONLY)
        _verify_posix(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _verify_windows(fd: int, path: str) -> None:
    """Prove, on the open descriptor, that this is a plain regular file.

    There are no mode bits to check: owner-only is the per-user profile
    directory's ACL doing its job. The descriptor check refuses anything
    that could redirect or replace the write - a reparse point or a
    non-regular file - even if the file was swapped in after the lstat.
    """
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise InsecureLogFileError(f"log path is not a regular file: {path}")
    if _is_reparse(info):
        raise InsecureLogFileError(f"log path is a reparse point: {path}")


def _open_windows(path: str) -> int:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        pass
    except OSError:
        # A junction to a directory, or another target the create cannot
        # open, refuses outright; classify it by lstat rather than trust it.
        info = os.lstat(path)
        if _is_reparse(info):
            raise InsecureLogFileError(f"log path is a symlink: {path}") from None
        raise
    else:
        try:
            _verify_windows(fd, path)
        except BaseException:
            os.close(fd)
            raise
        return fd

    # Pre-existing: classify by lstat BEFORE opening, so a symlink or a
    # junction is rejected rather than followed - there is no O_NOFOLLOW to
    # lean on, and the descriptor check below is the second gate.
    info = os.lstat(path)
    if _is_reparse(info):
        raise InsecureLogFileError(f"log path is a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise InsecureLogFileError(f"log path is not a regular file: {path}")

    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        _verify_windows(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def open_secure(path: str | os.PathLike[str]) -> int:
    """Return an append-mode descriptor on a *verified* owner-only file.

    The single secure-open implementation, shared by `append_line` and by the
    CLI's rotating file handler. Raises `InsecureLogFileError` (an `OSError`)
    rather than returning a descriptor on a file it could not secure.

    Dispatched on `os.name`, not `sys.platform`: the platform string is
    monkeypatchable by tests (pathlib itself keys on `os.name`, so nothing
    ever fakes that), and the two arms are defensive about which `os` flags
    exist.
    """
    target = os.fspath(path)
    if os.name == "nt":
        return _open_windows(target)
    return _open_posix(target)


def reject_if_unsafe_target(path: str | os.PathLike[str]) -> None:
    """Refuse a rotation destination that is a symlink or any non-regular file.

    Rollover renames onto these paths; a planted symlink there would otherwise
    make the next rollover hand the log to somebody else's file.
    """
    target = os.fspath(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise InsecureLogFileError(f"rotation target is a symlink: {target}")
    if not stat.S_ISREG(info.st_mode):
        raise InsecureLogFileError(f"rotation target is not a regular file: {target}")
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise InsecureLogFileError(f"rotation target is a reparse point: {target}")


def append_line(path: str | os.PathLike[str], line: str) -> None:
    """Append one already-redacted line, creating the file owner-only if needed."""
    fd = open_secure(path)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line if line.endswith("\n") else line + "\n")
