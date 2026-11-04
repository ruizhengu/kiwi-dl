"""The chunk requests a download has on the wire, so a stop can cut them off.

A `requests` timeout bounds each socket read, not the whole response. A worker
blocked on a slow proxy noticed a stop only when its read returned, which can
take up to the read timeout (15 s through a pool proxy) after Ctrl-C. A live
run took 8.6 s to stop. Two things now end that wait at once:

* A response that has arrived has its socket shut down. A read blocked in it
  gets end-of-file and `requests` raises. `close()` would not do: called from
  another thread, it leaves the blocked read to wait out its timeout (seen on
  macOS).
* A request still connecting, or waiting for the response, runs on a helper
  thread, and the worker stops waiting for it. Nothing in that phase is
  reachable from outside: the socket only exists inside `requests.get`. The
  helper closes the response when it arrives, or simply ends at its timeout.

Neither is a failure. The worker sees the stop event and counts the attempt as
cancelled, so no proxy is benched and nothing is logged as failed.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator
from typing import Any

import requests


class _Pending:
    """One `requests.get` running on a helper thread."""

    __slots__ = ("abandoned", "done", "error", "response")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: requests.Response | None = None
        self.error: BaseException | None = None
        self.abandoned = False


def sever(response: Any) -> None:
    """Shut down the socket under `response`, ending any read blocked in it.

    The socket is urllib3's connection's. It is gone (None) once the response
    has been read to the end and released, and then nothing is waiting on it.
    """
    connection = getattr(getattr(response, "raw", None), "connection", None)
    sock = getattr(connection, "sock", None)
    if sock is not None:
        # Already closed by the worker leaving its `with`: nothing to wake.
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


class InFlightRequests:
    """One download's chunk requests, for `cut_off` to end when it stops."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop = stop_event
        self._lock = threading.Lock()
        self._waiting: set[_Pending] = set()
        self._open: set[requests.Response] = set()

    @contextlib.contextmanager
    def get(self, url: str, **kwargs: Any) -> Iterator[requests.Response | None]:
        """`requests.get(url, **kwargs)` that a stop cuts off.

        Yields None when the download is stopped before the response arrives.
        Once it has arrived, a stop shuts its socket down, so a read blocked
        in it raises. The response is closed on the way out.
        """
        pending = _Pending()
        with self._lock:
            stopped = self._stop.is_set()
            if not stopped:
                self._waiting.add(pending)
        if stopped:
            yield None
            return
        try:
            threading.Thread(
                target=self._fetch,
                args=(pending, url, kwargs),
                name="kiwi-dl-chunk-request",
                daemon=True,  # an abandoned one must not hold up the exit
            ).start()
            pending.done.wait()
        finally:
            with self._lock:
                self._waiting.discard(pending)
                response, error = pending.response, pending.error
                # Woken by `cut_off` before the helper delivered: whatever it
                # gets later is closed by the helper itself.
                pending.abandoned = response is None and error is None
        if error is not None:
            raise error
        if response is None:
            yield None
            return
        try:
            yield response
        finally:
            with self._lock:
                self._open.discard(response)
            response.close()

    def _fetch(self, pending: _Pending, url: str, kwargs: dict[str, Any]) -> None:
        """Helper thread: make the request, then hand it over or close it."""
        response: requests.Response | None = None
        error: BaseException | None = None
        try:
            response = requests.get(url, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the worker
            error = exc
        with self._lock:
            # The stop event is set before `cut_off` takes this lock, so a
            # response that lands after it is never handed to a worker that
            # was told to stop.
            delivered = not pending.abandoned and not self._stop.is_set()
            if delivered:
                pending.response, pending.error = response, error
                if response is not None:
                    self._open.add(response)
        pending.done.set()
        if not delivered and response is not None:
            response.close()

    def cut_off(self) -> None:
        """Wake every worker waiting for a response and sever every open one.

        Call it after setting the stop event.
        """
        with self._lock:
            waiting, opened = list(self._waiting), list(self._open)
        for pending in waiting:
            pending.done.set()
        for response in opened:
            sever(response)
