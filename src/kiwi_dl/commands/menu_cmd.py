"""`kiwi-dl` with no arguments: the interactive menu.

Every action asks for what it needs, then runs the real command - the same one
you could type - as a child process. That keeps one implementation of each
command, and a Ctrl-C during a download ends that download and comes back
here instead of ending the menu.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from rich.console import Console
from rich.text import Text

from .. import __version__
from ..config import ConfigStore
from ..core.links import LinkType, parse_link
from ..ui import prompts, report
from ..ui.menu import QUIT_HINT, Choice, read_terminal_key, select
from ..ui.theme import SYMBOL, make_console
from ..utils.helpers import format_bytes

Runner = Callable[[Sequence[str], dict[str, str] | None], int]

_FOLDER_TYPES = {LinkType.FOLDER, LinkType.FOLDER_IN_FOLDER}

MAIN = [
    Choice("download", "Download", "Save a file or folder from a MEGA link"),
    Choice("inspect", "Inspect", "See what a link holds, without downloading it"),
    Choice("proxies", "Proxies", "Your proxy pool: list, add, test, remove"),
    Choice("settings", "Settings", "Download folder, parallel files, speed limit"),
    Choice("help", "Help", "Every command and option"),
    Choice("quit", "Quit"),
]


def run_child(args: Sequence[str], env: dict[str, str] | None = None) -> int:
    """`python -m kiwi_dl <args>` on this terminal; returns its exit status."""
    child_env = {**os.environ, **env} if env else None
    try:
        return subprocess.call([sys.executable, "-m", "kiwi_dl", *args], env=child_env)
    except KeyboardInterrupt:
        # The child got the same Ctrl-C and stopped its transfer; we just go back.
        return 130


def default_destination(cfg) -> Path:
    """Where the menu offers to save: the configured folder, else ~/Downloads.

    The command line keeps `curl -O` semantics (the current directory), but a
    menu is usually started from the home folder, where that is not a choice
    anyone means to make.
    """
    if cfg.download_path:
        return Path(cfg.download_path).expanduser()
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.cwd()


class Menu:
    """The menu's screens. Every side effect goes through `console`, `read_key`,
    `run`, and `ask`/`ask_secret`/`confirm`, so a test can drive all of it."""

    def __init__(
        self,
        console: Console | None = None,
        read_key: Callable[[], str] = read_terminal_key,
        run: Runner = run_child,
        ask: Callable[..., str] = prompts.ask,
        ask_secret: Callable[[str], str] = prompts.ask_secret,
        confirm: Callable[..., bool] = prompts.confirm,
        pause: Callable[[], None] | None = None,
    ) -> None:
        self.console = console or make_console()
        self.read_key = read_key
        self.run = run
        self.ask = ask
        self.ask_secret = ask_secret
        self.confirm = confirm
        self.pause = pause or self._pause

    # -- frame ---------------------------------------------------------------

    def _config(self):
        return ConfigStore().config

    def _header(self, section: str | None = None) -> None:
        self.console.clear()
        title = Text("kiwi-dl", style="kiwi.title")
        title.append(f" {__version__}", style="kiwi.muted")
        title.append(f" · {section}" if section else " · download public MEGA links")
        self.console.print(title)
        destination = default_destination(self._config())
        context = f"{SYMBOL['context']} {report.context_line(destination)}"
        self.console.print(Text(context, style="kiwi.muted"))
        self.console.print()

    def _pause(self) -> None:
        self.console.print()
        self.console.print(Text("Press Enter to go back", style="kiwi.muted"), end="")
        with contextlib.suppress(EOFError):
            input("")

    # -- main loop -----------------------------------------------------------

    def loop(self) -> int:
        actions = {
            "download": self.download,
            "inspect": self.inspect,
            "proxies": self.proxies,
            "settings": self.settings,
            "help": self.help,
        }
        position = 0
        while True:
            self._header()
            choice = select(MAIN, self.console, self.read_key, start=position, hint=QUIT_HINT)
            if choice is None or choice == "quit":
                self.console.print()
                return 0
            position = next(i for i, c in enumerate(MAIN) if c.value == choice)
            actions[choice]()

    # -- actions -------------------------------------------------------------

    def _ask_link(self) -> tuple[str, dict[str, str] | None, LinkType] | None:
        """A link, the password env for a protected one, and its type; None = back."""
        url = self.ask("MEGA link (blank to go back)")
        if not url:
            return None
        try:
            parsed = parse_link(url)
        except ValueError as exc:
            prompts.print_error(str(exc))
            self.pause()
            return None
        env = None
        if parsed.type == LinkType.PASSWORD_PROTECTED:
            password = self.ask_secret("Link password")
            if not password:
                prompts.print_skip("No password given; nothing was downloaded.")
                self.pause()
                return None
            # Through the environment, never argv: argv is visible in `ps`.
            env = {"KIWI_DL_PASSWORD": password}
        return url, env, parsed.type

    def download(self) -> None:
        self._header("Download")
        link = self._ask_link()
        if link is None:
            return
        url, env, link_type = link
        default = report.short_path(default_destination(self._config()))
        destination = Path(self.ask("Save to", default)).expanduser()
        args = ["download", url, "-o", str(destination)]
        if link_type in _FOLDER_TYPES and self.confirm(
            "Pick which files to download?", default=False
        ):
            args.append("--select")
        self.console.print()
        self.run(args, env)
        self.pause()

    def inspect(self) -> None:
        self._header("Inspect")
        link = self._ask_link()
        if link is None:
            return
        url, env, _ = link
        self.console.print()
        self.run(["info", url], env)
        self.pause()

    def help(self) -> None:
        self._header("Help")
        self.run(["--help"], None)
        self.pause()

    def proxies(self) -> None:
        choices = [
            Choice("list", "List", "Every proxy in the pool"),
            Choice("add", "Add", "http://, https://, socks4:// or socks5:// URLs"),
            Choice("test", "Test", "Which proxies answer, and how fast"),
            Choice("remove", "Remove", "Take one proxy out of the pool"),
            Choice("clear", "Clear", "Empty the pool"),
            Choice("back", "Back"),
        ]
        position = 0
        while True:
            self._header("Proxies")
            choice = select(choices, self.console, self.read_key, start=position)
            if choice is None or choice == "back":
                return
            position = next(i for i, c in enumerate(choices) if c.value == choice)
            self.console.print()
            if choice in ("list", "test", "clear"):
                self.run(["proxy", choice], None)
            else:
                url = self.ask("Proxy URL (blank to go back)")
                if not url:
                    continue
                self.run(["proxy", choice, url], None)
            self.pause()

    def settings(self) -> None:
        position = 0
        while True:
            cfg = self._config()
            limit = cfg.speed_limit_kbps
            choices = [
                Choice("show", "Show all", "Every setting and its value"),
                Choice(
                    "folder",
                    "Download folder",
                    report.short_path(cfg.download_path) if cfg.download_path else "not set",
                ),
                Choice("parallel", "Parallel files", f"{cfg.max_parallel_downloads} at once"),
                Choice(
                    "limit",
                    "Speed limit",
                    f"{format_bytes(int(limit * 1024))}/s" if limit else "unlimited",
                ),
                Choice("reset", "Reset", "Every setting back to its default"),
                Choice("back", "Back"),
            ]
            self._header("Settings")
            choice = select(choices, self.console, self.read_key, start=position)
            if choice is None or choice == "back":
                return
            position = next(i for i, c in enumerate(choices) if c.value == choice)
            self.console.print()
            if choice == "show":
                self.run(["config", "show"], None)
            elif choice == "reset":
                self.run(["config", "reset"], None)
            else:
                key, question, current = {
                    "folder": ("download_path", "Download folder", cfg.download_path),
                    "parallel": (
                        "max_parallel_downloads",
                        "Files at once (1-32)",
                        str(cfg.max_parallel_downloads),
                    ),
                    "limit": (
                        "speed_limit_kbps",
                        "Speed limit in KB/s (0 = unlimited)",
                        f"{cfg.speed_limit_kbps:g}",
                    ),
                }[choice]
                value = self.ask(question, current or None)
                if not value:
                    continue
                if key == "download_path":
                    value = str(Path(value).expanduser())
                self.run(["config", "set", key, value], None)
            self.pause()


def run_menu() -> int:
    return Menu().loop()
