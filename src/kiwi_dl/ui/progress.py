"""The live transfer view: one overall row, then one row per file.

➤ mega.nz/folder/AbCdEf
Overall
██████████░░░░░░░░░░  58.1% · 1.20 GB / 2.10 GB · 12.40 MB/s · ETA 01:12 · Elapsed 00:41
✓ part1.mkv
  ████████████████████ 100.0% · 1.00 GB / 1.00 GB · Done
↓ part2.mkv
  ████████░░░░░░░░░░░░  40.0% · 400.00 MB / 1.00 GB · 8.10 MB/s · ETA 01:12
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from rich.console import Group
from rich.live import Live
from rich.text import Text

from ..utils.helpers import format_eta
from ..utils.speed import RollingSpeedMeter
from .theme import SYMBOL, make_console

SEPARATOR = " · "

# Huge folders: cap the per-file rows painted each frame (totals still cover
# every file); active rows win, then queued, then finished ones.
MAX_VISIBLE_FILE_ROWS = 12


@dataclass
class ProgressFileState:
    """One file row in a shared multi-file progress view."""

    key: str
    name: str
    completed: int = 0
    total: int | None = None
    speed: float | None = 0.0
    status: str = "queued"


class MultiFileProgressView:
    """Live progress with one overall row and one row per file."""

    def __init__(
        self,
        title: str,
        details: list[str] | None = None,
        item_label: str = "files",
        min_interval: float = 0.5,
    ) -> None:
        self.title = title
        self.details = list(details or [])
        self.item_label = item_label
        self.min_interval = min_interval
        self.started_at = time.perf_counter()
        # Set exactly once at terminal state; freezes the Elapsed display.
        self.finished_at: float | None = None
        self.last_render = 0.0
        self.overall_completed = 0
        self.overall_total: int | None = None
        self.overall_speed = 0.0
        self.completed_items = 0
        self.total_items = 0
        self.failed_items = 0
        self.status = "Starting"
        self.file_states: list[ProgressFileState] = []
        self._lock = threading.RLock()
        # View-owned speed measurement: one rolling meter per file row. Speeds
        # and ETA are computed at RENDER time from these meters, so they decay
        # to 0 during a stall instead of freezing at the last producer-supplied
        # value. The overall speed is the sum of the row meters (or a fresh
        # producer-supplied hint), so there is no second meter to drift.
        self._row_meters: dict[str, RollingSpeedMeter] = {}
        self._backend_speed: float | None = None
        self._backend_speed_at = 0.0
        self._console = make_console(color_system="truecolor")
        # Pass SELF as the renderable: Rich re-invokes __rich_console__ on
        # every auto-refresh (4/s), so Elapsed / ETA / speed keep moving even
        # while the producer is silent (Rich's Live runs its own refresh
        # thread). A pre-built static Group would freeze between updates.
        self._live = Live(
            self,
            console=self._console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()

    def __rich_console__(self, console, options):  # pragma: no cover - Rich hook
        with self._lock:
            yield self._render()

    def update(
        self,
        file_states: list[ProgressFileState],
        overall_completed: int,
        overall_total: int | None,
        completed_items: int,
        total_items: int,
        failed_items: int = 0,
        status: str = "Downloading",
        overall_speed: float | None = None,
        force: bool = False,
    ) -> None:
        with self._lock:
            now = time.perf_counter()
            self.file_states = list(file_states)
            self.overall_completed = max(0, int(overall_completed or 0))
            self.overall_total = int(overall_total) if overall_total is not None else None
            if self.overall_total is not None and self.overall_completed > self.overall_total:
                self.overall_total = self.overall_completed
            self.completed_items = max(0, int(completed_items or 0))
            self.total_items = max(0, int(total_items or 0))
            self.failed_items = max(0, int(failed_items or 0))
            self.status = status
            # Feed the view-owned meters with this frame's cumulative bytes.
            # Queued rows are skipped: they carry no transfer data yet, and a
            # 0-byte sample would turn a resumed file's FIRST report (which
            # includes every previously-downloaded byte) into a huge fake
            # speed spike. By feeding only reporting rows, the meter's first
            # sample is the resume baseline and is excluded from the rate.
            live_keys = {state.key for state in self.file_states}
            for stale_key in [key for key in self._row_meters if key not in live_keys]:
                self._row_meters.pop(stale_key, None)
            for state in self.file_states:
                if state.status == "queued":
                    continue
                meter = self._row_meters.setdefault(state.key, RollingSpeedMeter(window=5.0))
                meter.update(max(0, int(state.completed or 0)), now)
            # A producer-reported overall speed is authoritative while fresh;
            # render falls back to the summed row meters when it goes stale.
            if overall_speed is not None and overall_speed >= 0:
                self._backend_speed = float(overall_speed)
                self._backend_speed_at = now
            should_refresh = force or now - self.last_render >= self.min_interval
            if should_refresh:
                self.last_render = now
        # Refresh OUTSIDE self._lock. Rich's auto-refresh thread renders while
        # holding Live's internal lock and re-enters __rich_console__, which
        # needs self._lock; calling refresh() (which takes Live's lock) while
        # holding self._lock is an ABBA inversion that deadlocks the UI.
        if should_refresh:
            self._live.refresh()

    def close(self, success: bool = True) -> None:
        with self._lock:
            if self.finished_at is None:
                self.finished_at = time.perf_counter()
            self.status = "Complete" if success else "Failed"
            if success and self.overall_total is not None:
                self.overall_completed = max(self.overall_completed, self.overall_total)
        # Outside self._lock for the same lock-ordering reason as update().
        self._live.refresh()
        self._live.stop()
        print()

    def _render(self) -> Group:
        # Speeds are measured HERE, at render time, by the view's own meters:
        # Rich's auto-refresh calls this ~4x/s, so between producer updates the
        # elapsed clock ticks, speeds decay during stalls, and ETA follows.
        now = time.perf_counter()
        for state in self.file_states:
            if state.status == "active":
                meter = self._row_meters.get(state.key)
                state.speed = meter.current(now) if meter is not None else 0.0
        if self._backend_speed is not None and now - self._backend_speed_at <= 2.5:
            self.overall_speed = self._backend_speed
        else:
            self.overall_speed = sum(meter.current(now) for meter in self._row_meters.values())
        width = max(70, min(140, int(getattr(self._console, "width", 100) or 100)))
        lines: list[Text] = []
        if self.title:
            title_style = "kiwi.error" if self.status == "Failed" else "kiwi.title"
            title = _shorten_middle(self.title, width - 2)
            lines.append(Text(f"{SYMBOL['group']} {title}", style=title_style))
        for detail in self.details:
            if detail:
                lines.append(Text(f"  {_shorten_middle(detail, width - 2)}", style="kiwi.muted"))
        if lines:
            lines.append(Text(""))
        lines.append(Text("Overall", style="bold"))
        lines.append(
            self._stats_text(
                self.overall_completed,
                self.overall_total,
                self.overall_speed,
                width,
                include_elapsed=True,
            )
        )
        visible, hidden = self._visible_rows()
        if visible:
            lines.append(Text(""))
        for _row_number, state in visible:
            symbol, symbol_style = self._state_mark(state.status)
            name = _shorten_middle(state.name, max(10, width - 2))
            lines.append(Text().append(f"{symbol} ", style=symbol_style).append(name))
            stats = self._stats_text(
                state.completed,
                state.total,
                state.speed,
                width - 2,
                include_elapsed=False,
                status=state.status,
            )
            lines.append(Text("  ").append_text(stats))
        if hidden:
            lines.append(Text(f"  … and {hidden} more {self.item_label}", style="kiwi.muted"))
        return Group(*lines)

    def _visible_rows(self) -> tuple[list[tuple[int, ProgressFileState]], int]:
        """Bound the painted rows for huge folders without losing totals."""
        numbered = list(enumerate(self.file_states, 1))
        if len(numbered) <= MAX_VISIBLE_FILE_ROWS:
            return numbered, 0
        rank = {"active": 0, "queued": 1}
        ordered = sorted(numbered, key=lambda pair: (rank.get(pair[1].status, 2), pair[0]))
        visible = sorted(ordered[:MAX_VISIBLE_FILE_ROWS], key=lambda pair: pair[0])
        return visible, len(numbered) - MAX_VISIBLE_FILE_ROWS

    def _stats_text(
        self,
        completed: int,
        total: int | None,
        speed: float | None,
        width: int,
        include_elapsed: bool,
        status: str = "active",
    ) -> Text:
        percent = (completed / total * 100.0) if total else 0.0
        percent = max(0.0, min(100.0, percent))
        # Elapsed is never hidden; on narrow terminals the bar shrinks instead.
        # 76 is the rest of the overall line at its longest (bytes, speed, ETA,
        # Elapsed), so the line fits the terminal instead of wrapping.
        bar_width = max(10, min(30, width - 76))
        remaining = (total - completed) if total else 0
        done = status in {"complete", "downloaded", "resumed"} or bool(total and completed >= total)
        failed = status in {"failed", "error"}
        # A queued row has no speed or countdown yet, only a state to name.
        stopped = status in {"canceled", "skipped", "queued"}
        if done:
            speed_label = "Done"
        elif failed:
            speed_label = "Failed"
        elif stopped:
            speed_label = status.capitalize()
        else:
            speed_label = _format_speed(speed)
        # ETA answers "how much longer", so a row that has finished, failed or
        # been cancelled has no countdown left to show. It used to be assigned
        # the state label and printed anyway, which put the same word in two
        # adjacent columns - "Done | ETA Done" - a heading with nothing behind
        # it, paid for in terminal width. None here means the whole section is
        # skipped below, separator included.
        eta = (
            None
            if done or failed or stopped
            else format_eta(remaining / speed) if total and speed and speed > 1 else "--:--"
        )
        speed_style = (
            "kiwi.success"
            if done
            else "kiwi.error" if failed else "kiwi.muted" if stopped else "kiwi.value"
        )
        text = Text()
        text.append_text(_rich_bar(percent, bar_width, status))
        text.append(f" {percent:5.1f}%", style="bold")
        text.append(SEPARATOR, style="kiwi.muted")
        text.append(f"{_format_bytes(completed)} / {_format_bytes(total) if total else '?'}")
        text.append(SEPARATOR, style="kiwi.muted")
        text.append(speed_label, style=speed_style)
        if eta is not None:
            # The separator belongs to this section: leaving it behind renders
            # "Done ·  · Elapsed".
            text.append(SEPARATOR, style="kiwi.muted")
            text.append(f"ETA {eta}", style="kiwi.warning")
        if include_elapsed:
            # Wall-clock operation time from one monotonic clock owned by the
            # view; frozen exactly at terminal state by close().
            elapsed_ref = self.finished_at if self.finished_at is not None else time.perf_counter()
            text.append(SEPARATOR, style="kiwi.muted")
            text.append(f"Elapsed {format_eta(elapsed_ref - self.started_at)}", style="kiwi.muted")
            if width >= 110:
                item_text = f"{self.completed_items}/{self.total_items} {self.item_label}"
                if self.failed_items:
                    item_text += f", {self.failed_items} failed"
                text.append(SEPARATOR, style="kiwi.muted")
                text.append(item_text, style="kiwi.warning" if self.failed_items else "kiwi.muted")
        return text

    @staticmethod
    def _state_mark(status: str) -> tuple[str, str]:
        """The symbol and style a file row's name carries for its state."""
        if status in {"complete", "downloaded", "resumed"}:
            return SYMBOL["ok"], "kiwi.success"
        if status in {"failed", "error"}:
            return SYMBOL["fail"], "kiwi.error"
        if status in {"skipped", "canceled"}:
            return SYMBOL["skip"], "kiwi.muted"
        if status == "queued":
            return SYMBOL["queued"], "kiwi.muted"
        return SYMBOL["active"], "kiwi.accent"


def _format_bytes(size: int | float | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _format_speed(speed: float | None) -> str:
    if speed is None:
        return "--"
    return f"{_format_bytes(speed)}/s"


def _shorten_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    left = max(1, limit // 2 - 2)
    right = max(1, limit - left - 3)
    return text[:left] + "..." + text[-right:]


def _bar_parts(percent: float, width: int) -> tuple[int, int]:
    width = max(1, int(width))
    filled = int(max(0.0, min(100.0, percent)) / 100.0 * width)
    return filled, max(0, width - filled)


def _rich_bar(percent: float, width: int, status: str = "active") -> Text:
    filled, empty = _bar_parts(percent, width)
    failed = status in {"failed", "error"}
    done = percent >= 100.0
    fill_style = "kiwi.error" if failed else "kiwi.success" if done else "kiwi.accent"
    text = Text()
    text.append("█" * filled, style=fill_style)
    text.append("░" * empty, style="kiwi.muted")
    return text
