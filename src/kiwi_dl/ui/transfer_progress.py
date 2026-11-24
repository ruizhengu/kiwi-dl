"""Shared transfer-progress controller used by every download mode.

One architecture for all download UIs (single download, parallel downloads,
folder download, file-in-folder):

- producers (downloader callbacks) push structured byte snapshots
  into a `TransferProgress` controller;
- the controller owns aggregation, the monotonic operation clock, and the
  item lifecycle (every item ends in exactly one terminal state);
- the renderer (`MultiFileProgressView`) only paints controller state and is
  repainted by Rich's own refresh ticker, so Elapsed keeps advancing even
  while producers are silent (stalls, retries, quota waits, finalization).

Command modules must not duplicate layout logic; they talk to this API only.
"""

from __future__ import annotations

import threading
import uuid

from .progress import MultiFileProgressView, ProgressFileState

__all__ = ["TransferProgress", "TERMINAL_STATUSES"]

TERMINAL_STATUSES = {"complete", "failed", "canceled", "skipped"}


class TransferProgress:
    """Controller/model for one command's transfer UI.

    Thread-safe: producer callbacks may arrive from any worker thread. When
    `quiet` is true no live view is created and all calls are cheap no-ops
    apart from state tracking (final summaries stay meaningful).
    """

    def __init__(
        self,
        title: str,
        quiet: bool = False,
        view_factory=None,
    ) -> None:
        self.title = title
        self.quiet = quiet
        self._items: dict[str, ProgressFileState] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._closed = False
        self._final_success: bool | None = None  # Set exactly once by close()
        self._view: MultiFileProgressView | None = None
        self._view_factory = view_factory or (lambda: MultiFileProgressView(title=self.title))

    # ------------------------------------------------------------------
    # Item lifecycle
    # ------------------------------------------------------------------
    def add_item(
        self,
        name: str,
        total: int | None = None,
        status: str = "queued",
    ) -> str:
        """Register one transfer item; returns its key."""
        with self._lock:
            key = uuid.uuid4().hex[:16]
            self._items[key] = ProgressFileState(
                key=key,
                name=name,
                completed=0,
                total=int(total) if total is not None else None,
                speed=0.0,
                status=status,
            )
            self._order.append(key)
        self._push()
        return key

    def set_item_name(self, key: str, name: str) -> None:
        with self._lock:
            state = self._items.get(key)
            if state is not None:
                state.name = name
        self._push()

    def update_item(self, key: str, completed: int, total: int | None = None) -> None:
        """Producer snapshot: cumulative bytes for one item."""
        with self._lock:
            state = self._items.get(key)
            if state is None or state.status in TERMINAL_STATUSES:
                return
            state.completed = max(0, int(completed or 0))
            if total is not None:
                state.total = int(total)
            state.status = "active"
        self._push()

    def finish_item(self, key: str, status: str = "complete") -> None:
        """Move an item into exactly one terminal state."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Not a terminal status: {status}")
        with self._lock:
            state = self._items.get(key)
            if state is None or state.status in TERMINAL_STATUSES:
                return
            if status == "complete" and state.total is not None:
                state.completed = max(state.completed, state.total)
            state.speed = 0.0
            state.status = status
        self._push(force=True)

    # ------------------------------------------------------------------
    # Aggregation / rendering
    # ------------------------------------------------------------------
    def _snapshot(self) -> tuple[list[ProgressFileState], int, int | None, int, int, int]:
        states = [self._items[k] for k in self._order]
        completed = sum(max(0, s.completed) for s in states)
        known_totals = [s.total for s in states if s.total is not None]
        total = sum(known_totals) if known_totals else None
        done_items = sum(
            1
            for s in states
            if s.status in {"complete", "downloaded", "resumed"}
            or bool(s.total and s.completed >= s.total)
        )
        failed_items = sum(1 for s in states if s.status in {"failed", "error"})
        return states, completed, total, done_items, len(states), failed_items

    def _push(self, force: bool = False, status: str | None = None) -> None:
        if self.quiet:
            return
        with self._lock:
            if self._closed:
                return
            view = self._view
            if view is None:
                view = self._view = self._view_factory()
            states, completed, total, done_items, total_items, failed_items = self._snapshot()
        view.update(
            [ProgressFileState(**vars(s)) for s in states],
            overall_completed=completed,
            overall_total=total,
            completed_items=done_items,
            total_items=total_items,
            failed_items=failed_items,
            status=status or "Downloading",
            force=force,
        )

    # ------------------------------------------------------------------
    # Finalization (single shared path for every outcome)
    # ------------------------------------------------------------------
    def close(self, success: bool = True) -> None:
        """Finalize the whole view; no item stays visually active.

        The final overall state combines the caller's outcome (an uncaught
        exception maps to ``success=False``) with the ITEM states: any failed
        item, and any item left unfinished (auto-canceled here), makes the
        overall state Failed even when the surrounding context exited
        cleanly. Items explicitly finished as "skipped" (user-requested
        skips) never fail the overall state. Idempotent: repeated calls are
        no-ops and can never convert a failed overall back to complete.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            auto_canceled = False
            for state in self._items.values():
                if state.status in TERMINAL_STATUSES:
                    continue
                if state.total is not None and state.completed >= state.total and state.total > 0:
                    state.status = "complete"
                else:
                    # An item that was still queued/active at close time did
                    # not finish; that is never a successful outcome.
                    state.status = "canceled"
                    auto_canceled = True
                state.speed = 0.0
            failed = any(s.status == "failed" for s in self._items.values())
            effective_success = success and not failed and not auto_canceled
            self._final_success = effective_success
            view = self._view
            self._view = None
        if view is None or self.quiet:
            return
        states, completed, total, done_items, total_items, failed_items = self._snapshot()
        view.update(
            states,
            overall_completed=completed,
            overall_total=total,
            completed_items=done_items,
            total_items=total_items,
            failed_items=failed_items,
            status="Complete" if effective_success else "Failed",
            force=True,
        )
        view.close(success=effective_success)

    def __enter__(self) -> TransferProgress:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Any exception (including KeyboardInterrupt) finalizes as failure.
        self.close(success=exc_type is None)

    # Introspection used by tests and summaries.
    def statuses(self) -> dict[str, str]:
        with self._lock:
            return {k: self._items[k].status for k in self._order}

    def final_success(self) -> bool | None:
        """Overall outcome computed by close(); None while still open."""
        with self._lock:
            return self._final_success
