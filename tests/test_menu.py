"""The interactive menu: key handling, rendering, and the command each action runs.

Nothing here needs a terminal: keys come from a list, the screen is a captured
console, and the command an action would run is recorded instead of spawned.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from kiwi_dl import cli as cli_module
from kiwi_dl.commands import menu_cmd
from kiwi_dl.commands.menu_cmd import Menu
from kiwi_dl.ui import menu
from kiwi_dl.ui.menu import DOWN, ENTER, QUIT, UP, Choice, select
from kiwi_dl.ui.theme import SYMBOL, THEME

FILE_LINK = "https://mega.nz/file/ABCDEFGH#" + "k" * 43
FOLDER_LINK = "https://mega.nz/folder/ABCDEFGH#" + "k" * 22
PASSWORD_LINK = "https://mega.nz/#P!AwADabcdefghijklmnopqrstuvwxyz0123456789"

CHOICES = [Choice("a", "Alpha", "first"), Choice("b", "Beta"), Choice("c", "Gamma", "third")]


def _console() -> Console:
    return Console(theme=THEME, file=io.StringIO(), width=100, force_terminal=False)


def _keys(*keys: str):
    pending = iter(keys)
    return lambda: next(pending)


# --------------------------------------------------------------------------
# The widget
# --------------------------------------------------------------------------


def test_the_cursor_marks_exactly_one_row_and_the_hint_closes_the_list():
    lines = menu.render(CHOICES, 1).plain.split("\n")
    assert lines[1].startswith(f"{SYMBOL['group']} Beta")
    assert not lines[0].startswith(SYMBOL["group"]) and not lines[2].startswith(SYMBOL["group"])
    assert lines[-1] == menu.HINT
    # Labels are padded to one width so the hints line up.
    assert lines[0].index("first") == lines[2].index("third")


@pytest.mark.parametrize(
    ("keys", "chosen"),
    [
        ((ENTER,), "a"),
        ((DOWN, ENTER), "b"),
        ((DOWN, DOWN, DOWN, ENTER), "a"),  # wraps past the end
        ((UP, ENTER), "c"),  # and past the start
        (("3",), "c"),  # a number jumps straight to its row
        (("x", "?", "²", "9", DOWN, ENTER), "b"),  # anything else is ignored
        ((QUIT,), None),
    ],
)
def test_keys_move_the_cursor_and_choose(keys, chosen):
    assert select(CHOICES, _console(), _keys(*keys)) == chosen


def test_ctrl_c_inside_the_menu_backs_out_instead_of_crashing():
    def interrupted():
        raise KeyboardInterrupt

    assert select(CHOICES, _console(), interrupted) is None


def test_the_cursor_is_hidden_while_choosing_and_always_shown_again():
    console = _console()
    select(CHOICES, console, _keys(DOWN, QUIT))
    out = console.file.getvalue()
    assert out.startswith("\x1b[?25l")
    assert out.rstrip().endswith("\x1b[?25h")
    # A redraw climbs back over exactly the rows it drew: 3 choices, a blank, the hint.
    assert "\x1b[5F\x1b[J" in out


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        (b"\x1b[A", UP),
        (b"k", UP),
        (b"\x1b[B", DOWN),
        (b"j", DOWN),
        (b"\r", ENTER),
        (b"\n", ENTER),
        (b"q", QUIT),
        (b"\x1b", QUIT),
        (b"\x03", QUIT),
        (b"4", "4"),
    ],
)
def test_raw_keypresses_become_key_names(raw, key):
    assert menu._normalize(raw) == key


# --------------------------------------------------------------------------
# The actions
# --------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, args, env=None) -> int:
        self.calls.append((list(args), env))
        return 0


def _menu(*answers: str, keys=(), confirm: bool = False, secret: str = ""):
    pending = iter(answers)
    runner = _Recorder()
    app = Menu(
        console=_console(),
        read_key=_keys(*keys),
        run=runner,
        ask=lambda question, default=None: next(pending, "") or (default or ""),
        ask_secret=lambda question: secret,
        confirm=lambda question, default=False: confirm,
        pause=lambda: None,
    )
    return app, runner


def test_download_runs_the_download_command_with_the_chosen_folder(tmp_path):
    app, runner = _menu(FILE_LINK, str(tmp_path))
    app.download()
    assert runner.calls == [(["download", FILE_LINK, "-o", str(tmp_path)], None)]


def test_a_folder_link_offers_the_file_picker(tmp_path):
    app, runner = _menu(FOLDER_LINK, str(tmp_path), confirm=True)
    app.download()
    assert runner.calls[0][0][-1] == "--select"


def test_a_link_password_goes_through_the_environment_never_argv(tmp_path):
    app, runner = _menu(PASSWORD_LINK, str(tmp_path), secret="hunter2")
    app.download()
    (args, env) = runner.calls[0]
    assert env == {"KIWI_DL_PASSWORD": "hunter2"}
    assert "hunter2" not in " ".join(args)


def test_a_protected_link_without_a_password_downloads_nothing():
    app, runner = _menu(PASSWORD_LINK, secret="")
    app.download()
    assert runner.calls == []


@pytest.mark.parametrize("answer", ["", "not a mega link"])
def test_a_blank_or_bad_link_goes_back_without_running_anything(answer):
    app, runner = _menu(answer)
    app.download()
    app.inspect()
    assert runner.calls == []


def test_inspect_runs_info():
    app, runner = _menu(FILE_LINK)
    app.inspect()
    assert runner.calls == [(["info", FILE_LINK], None)]


def test_proxies_add_then_back():
    app, runner = _menu("socks5://127.0.0.1:1080", keys=("2", "6"))
    app.proxies()
    assert runner.calls == [(["proxy", "add", "socks5://127.0.0.1:1080"], None)]


def test_settings_writes_the_value_it_asked_for():
    app, runner = _menu("4", keys=("3", QUIT))
    app.settings()
    assert runner.calls == [(["config", "set", "max_parallel_downloads", "4"], None)]


def test_the_download_folder_setting_is_stored_expanded():
    app, runner = _menu("~/Movies", keys=("2", QUIT))
    app.settings()
    assert runner.calls == [(["config", "set", "download_path", str(Path.home() / "Movies")], None)]


def test_the_main_loop_runs_an_action_and_quits(tmp_path):
    app, runner = _menu(FILE_LINK, str(tmp_path), keys=("1", QUIT))
    assert app.loop() == 0
    assert runner.calls[0][0][0] == "download"


def test_the_menu_suggests_downloads_not_the_current_directory(monkeypatch, tmp_path):
    # HOME, not a patched Path.home: `expanduser` reads the variable directly.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()

    class Cfg:
        download_path = ""

    assert menu_cmd.default_destination(Cfg()) == tmp_path / "Downloads"
    Cfg.download_path = "~/elsewhere"
    assert menu_cmd.default_destination(Cfg()) == tmp_path / "elsewhere"


class _FakeChild:
    """A child process whose wait() first sees the user's Ctrl-C, then exits."""

    def __init__(self, argv, env=None, interrupts: int = 0, status: int = 0):
        self.argv, self.env = argv, env
        self.interrupts, self.status = interrupts, status
        self.killed = False

    def wait(self):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return self.status

    def kill(self):
        self.killed = True


def test_run_child_passes_the_password_in_the_environment(monkeypatch):
    children = []
    monkeypatch.setattr(
        menu_cmd.subprocess,
        "Popen",
        lambda argv, env=None: children.append(_FakeChild(argv, env)) or children[-1],
    )
    menu_cmd.run_child(["download", FILE_LINK], {"KIWI_DL_PASSWORD": "pw"})
    (child,) = children
    assert child.argv[1:3] == ["-m", "kiwi_dl"]
    assert "pw" not in child.argv
    assert child.env["KIWI_DL_PASSWORD"] == "pw"
    assert "PATH" in child.env  # the rest of the environment is kept


def test_ctrl_c_waits_for_the_download_to_stop_instead_of_killing_it(monkeypatch):
    """The child gets the same Ctrl-C and stops cleanly; SIGKILL would cut it off."""
    children = []

    def popen(argv, env=None):
        children.append(_FakeChild(argv, env, interrupts=2, status=130))
        return children[-1]

    monkeypatch.setattr(menu_cmd.subprocess, "Popen", popen)
    assert menu_cmd.run_child(["download", FILE_LINK]) == 130
    assert children[0].killed is False


# --------------------------------------------------------------------------
# `kiwi-dl` with no command
# --------------------------------------------------------------------------


def test_without_a_terminal_bare_kiwi_dl_prints_help():
    result = CliRunner().invoke(cli_module.cli, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output and "interactive menu" in result.output


def test_on_a_terminal_bare_kiwi_dl_opens_the_menu(monkeypatch):
    opened = []
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(menu_cmd, "run_menu", lambda: opened.append(True) or 0)
    result = CliRunner().invoke(cli_module.cli, [])
    assert result.exit_code == 0
    assert opened == [True]
    assert "Usage:" not in result.output


def test_a_subcommand_never_opens_the_menu(monkeypatch):
    monkeypatch.setattr(cli_module, "_interactive", lambda: True)
    monkeypatch.setattr(menu_cmd, "run_menu", lambda: pytest.fail("menu opened"))
    assert CliRunner().invoke(cli_module.cli, ["config", "path"]).exit_code == 0
