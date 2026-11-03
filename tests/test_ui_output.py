"""The output frame: status symbols, the prompt shape, the header and the summary."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from rich.console import Console

from kiwi_dl.commands import download_cmd
from kiwi_dl.commands.download_support import RunTally
from kiwi_dl.ui import prompts, report
from kiwi_dl.ui.progress import MultiFileProgressView
from kiwi_dl.ui.theme import SYMBOL, THEME, styled_prompt


def _console(color: bool = False) -> Console:
    return Console(
        theme=THEME, width=100, force_terminal=color, color_system="truecolor" if color else None
    )


def _render(renderable, color: bool = False) -> str:
    console = _console(color)
    with console.capture() as captured:
        console.print(renderable, end="")
    return captured.get()


# --------------------------------------------------------------------------
# Status lines
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("helper", "symbol"),
    [
        (prompts.print_success, SYMBOL["ok"]),
        (prompts.print_error, SYMBOL["fail"]),
        (prompts.print_warn, SYMBOL["warn"]),
        (prompts.print_info, SYMBOL["info"]),
        (prompts.print_skip, SYMBOL["skip"]),
    ],
)
def test_each_status_line_leads_with_its_own_symbol(monkeypatch, helper, symbol):
    console = _console()
    monkeypatch.setattr(prompts, "_console", console)
    with console.capture() as captured:
        helper("a [bold]name[/bold] from MEGA")
    out = captured.get()
    assert out.startswith(f"{symbol} ")
    # Server-supplied text is shown as typed, never parsed as markup.
    assert "a [bold]name[/bold] from MEGA" in out


def test_the_symbols_are_all_different():
    assert len(set(SYMBOL.values())) == len(SYMBOL)


# --------------------------------------------------------------------------
# Prompt shape
# --------------------------------------------------------------------------


def test_a_prompt_without_a_default_is_label_colon_space():
    assert styled_prompt("MEGA link").plain == "MEGA link: "


def test_only_the_default_value_is_coloured_not_its_brackets():
    out = _render(styled_prompt("Save to", "~/Downloads"), color=True)
    plain = styled_prompt("Save to", "~/Downloads").plain
    assert plain == "Save to [~/Downloads]: "
    # The colour opens AFTER "[" and closes BEFORE "]".
    assert "[\x1b[" in out
    assert "\x1b[0m]" in out


def test_confirm_reasks_on_gibberish_and_takes_the_default_on_enter(monkeypatch):
    answers = iter(["maybe", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(prompts, "_console", _console())
    assert prompts.confirm("Go on?", default=False) is False


# --------------------------------------------------------------------------
# Header and summary
# --------------------------------------------------------------------------


def test_short_path_uses_a_tilde_for_home():
    home = Path.home()
    assert report.short_path(home / "Downloads") == "~/Downloads"
    assert report.short_path(home) == "~"
    assert report.short_path("/tmp/x") == "/tmp/x"


def test_free_space_answers_for_a_folder_that_does_not_exist_yet(tmp_path):
    assert report.free_space(tmp_path / "not" / "made" / "yet") is not None


def test_the_context_line_names_the_machine_free_space_and_destination(tmp_path):
    line = report.context_line(tmp_path)
    assert report.machine_name() in line
    assert " free · " in line
    assert line.endswith(report.short_path(tmp_path))


def test_the_header_is_a_title_then_the_context_line():
    console = _console()
    with console.capture() as captured:
        # A short real path: pytest's tmp_path is long enough to wrap the line.
        report.header(console, "Download", Path("/tmp"))
    title, context, blank = captured.get().split("\n")[:3]
    assert title == "Download"
    assert context.startswith(f"{SYMBOL['context']} ")
    assert blank == ""


def test_the_summary_is_framed_and_its_labels_line_up():
    console = _console()
    with console.capture() as captured:
        report.summary(console, "Download complete", [("Saved to", "~/x"), ("Time", "00:03")])
    lines = captured.get().strip("\n").split("\n")
    assert lines[0] == lines[-1] == "=" * report.RULE_WIDTH
    assert lines[1] == f"{SYMBOL['ok']} Download complete"
    assert lines[2].index("~/x") == lines[3].index("00:03")


def test_a_failed_summary_says_so_with_the_failure_symbol():
    console = _console()
    with console.capture() as captured:
        report.summary(console, "Download finished with errors", [], ok=False)
    assert f"{SYMBOL['fail']} Download finished with errors" in captured.get()


def test_the_download_summary_lists_failures_and_the_quota_only_when_there_are_some(
    monkeypatch, tmp_path
):
    console = _console()
    monkeypatch.setattr(download_cmd, "_console", console)
    tally = RunTally()
    tally.add(2048, files=2)

    with console.capture() as clean:
        download_cmd._print_summary(
            tally, failures=0, quota_stops=0, output_dir=tmp_path, seconds=3
        )
    assert "Download complete" in clean.get()
    assert "2.00 KB · 2 file(s)" in clean.get()
    assert "Failed" not in clean.get() and "Quota" not in clean.get()

    with console.capture() as failed:
        download_cmd._print_summary(
            tally, failures=1, quota_stops=1, output_dir=tmp_path, seconds=3
        )
    assert "Download finished with errors" in failed.get()
    assert "Failed" in failed.get() and "Quota" in failed.get()


def test_the_run_tally_is_safe_to_share_between_parallel_slots():
    tally = RunTally()

    def worker():
        for _ in range(1000):
            tally.add(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert (tally.files, tally.total_bytes) == (8000, 8000)


# --------------------------------------------------------------------------
# Progress rows
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "symbol"),
    [
        ("active", SYMBOL["active"]),
        ("queued", SYMBOL["queued"]),
        ("complete", SYMBOL["ok"]),
        ("failed", SYMBOL["fail"]),
        ("skipped", SYMBOL["skip"]),
        ("canceled", SYMBOL["skip"]),
    ],
)
def test_a_file_row_is_marked_with_the_symbol_for_its_state(status, symbol):
    assert MultiFileProgressView._state_mark(status)[0] == symbol
