"""`kiwi-dl update`, `kiwi-dl remove` and `kiwi-dl completion`: kiwi-dl managing itself.

kiwi-dl is installed as a tool by uv or pipx (see install.sh). Each leaves a
marker in the tool's own environment - `uv-receipt.toml` or
`pipx_metadata.json` - and pip records where the package came from in
`direct_url.json`, so the same tool can update or remove it from the same
source. Anything else (a plain pip install, a source checkout) gets told what
to run instead of having its environment changed under it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import click
from rich.text import Text

from ..config import config_dir, data_dir, log_dir
from ..ui import report
from ..ui.prompts import confirm, print_error, print_info, print_skip, print_success
from ..ui.theme import SYMBOL, make_console

DIST = "kiwi-dl"
REPO = "https://github.com/ruizhengu/kiwi-dl"
DEFAULT_SOURCE = f"git+{REPO}@main"

_console = make_console()


@dataclass(frozen=True)
class Install:
    """How this copy of kiwi-dl was installed."""

    manager: str  # "uv" | "pipx" | "editable" | "pip"
    source: str  # what to reinstall from: a git URL@ref or a local directory
    prefix: Path


def _direct_url() -> dict:
    try:
        text = metadata.distribution(DIST).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return {}
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _source_from(direct: dict) -> str:
    """`git+URL@ref` for a git install, the directory for a local one."""
    url = str(direct.get("url", ""))
    vcs = direct.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and url:
        ref = vcs.get("requested_revision") or "main"
        return f"git+{url}@{ref}"
    if url.startswith("file://"):
        return url[len("file://") :]
    return DEFAULT_SOURCE


def detect_install(prefix: Path | None = None, direct: dict | None = None) -> Install:
    prefix = Path(sys.prefix) if prefix is None else prefix
    direct = _direct_url() if direct is None else direct
    source = _source_from(direct)
    if (prefix / "uv-receipt.toml").is_file():
        return Install("uv", source, prefix)
    if (prefix / "pipx_metadata.json").is_file():
        return Install("pipx", source, prefix)
    if (direct.get("dir_info") or {}).get("editable"):
        return Install("editable", source, prefix)
    return Install("pip", source, prefix)


def update_command(install: Install) -> list[str] | None:
    if install.manager == "uv":
        return ["uv", "tool", "install", "--force", "--reinstall", install.source]
    if install.manager == "pipx":
        return ["pipx", "install", "--force", install.source]
    return None


def remove_command(install: Install) -> list[str] | None:
    if install.manager == "uv":
        return ["uv", "tool", "uninstall", DIST]
    if install.manager == "pipx":
        return ["pipx", "uninstall", DIST]
    return None


def _manual_hint(install: Install, action: str) -> str:
    if install.manager == "editable":
        where = report.short_path(install.source)
        return f"This is a source checkout ({where}): {action} it with git, e.g. `git pull`."
    if action == "update":
        return f"Installed with pip: run `python -m pip install --upgrade '{install.source}'`."
    return "Installed with pip: run `python -m pip uninstall kiwi-dl`."


def _run(command: list[str]) -> int:
    print_info(" ".join(command))
    try:
        return subprocess.call(command)
    except FileNotFoundError:
        print_error(f"`{command[0]}` is not on PATH; it is what installed kiwi-dl.")
        return 1


def _go_ahead(question: str, yes: bool) -> bool:
    """Ask, unless --yes; refuse rather than hang when nobody can answer."""
    if yes:
        return True
    if not sys.stdin.isatty():
        print_error("Nothing to confirm with (no terminal); pass --yes to go ahead.")
        click.get_current_context().exit(1)
    if confirm(question, default=False):
        return True
    print_skip("Cancelled; nothing was changed.")
    return False


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@click.command("update", short_help="Update kiwi-dl to the latest version.")
@click.option("--dry-run", is_flag=True, help="Show what would run, change nothing.")
def update_cmd(dry_run: bool) -> None:
    """Reinstall kiwi-dl from where it came from, with the tool that installed it."""
    install = detect_install()
    command = update_command(install)
    report.header(_console, "Update")
    if command is None:
        print_info(_manual_hint(install, "update"))
        return
    if dry_run:
        print_skip(f"Dry run; would run: {' '.join(command)}")
        return
    status = _run(command)
    if status:
        click.get_current_context().exit(status)
    print_success("kiwi-dl is up to date.")


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def purge_targets() -> list[Path]:
    """The config, data and log directories this install uses, if they exist.

    Only directories that are kiwi-dl's own: a `kiwi-dl` platform directory,
    or the Config/Data/Logs folders inside a KIWI_DL_HOME. Never the home
    directory itself, never a parent of it, never `/` - whatever the
    environment says.
    """
    home = Path.home().resolve()
    found: list[Path] = []
    for path in (config_dir(), data_dir(), log_dir()):
        path = path.expanduser().resolve()
        if path in found or not path.is_dir():
            continue
        if path == home or path in home.parents or path == Path(path.anchor):
            continue
        if "kiwi-dl" not in path.parts and not os.environ.get("KIWI_DL_HOME"):
            continue
        found.append(path)
    return found


@click.command("remove", short_help="Uninstall kiwi-dl.")
@click.option("--purge", is_flag=True, help="Also delete settings, proxies and logs.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed, change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
def remove_cmd(purge: bool, dry_run: bool, yes: bool) -> None:
    """Uninstall kiwi-dl. Downloaded files are never touched."""
    install = detect_install()
    command = remove_command(install)
    targets = purge_targets() if purge else []
    report.header(_console, "Remove")
    _console.print(Text(f"{SYMBOL['group']} This will remove", style="kiwi.title"))
    if command is not None:
        where = report.short_path(install.prefix)
        _console.print(Text(f"  kiwi-dl ({install.manager} tool at {where})"))
    for target in targets:
        _console.print(Text(f"  {report.short_path(target)}"))
    if not purge:
        _console.print(Text("  (settings and logs stay; add --purge to delete them)", "kiwi.muted"))
    _console.print()
    if command is None and not targets:
        print_info(_manual_hint(install, "remove"))
        return
    if dry_run:
        print_skip("Dry run; nothing was removed.")
        return
    if not _go_ahead("Remove kiwi-dl?", yes):
        return
    # Data first: once the tool's environment is gone there is no code left
    # to delete it with.
    for target in targets:
        shutil.rmtree(target, ignore_errors=True)
        print_success(f"Deleted {report.short_path(target)}")
    if command is None:
        print_info(_manual_hint(install, "remove"))
        return
    status = _run(command)
    if status:
        click.get_current_context().exit(status)
    print_success("kiwi-dl is removed. Thanks for using it.")


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------

_SETUP = {
    "zsh": 'eval "$(kiwi-dl completion zsh)"   # in ~/.zshrc',
    "bash": 'eval "$(kiwi-dl completion bash)"  # in ~/.bashrc',
    "fish": "kiwi-dl completion fish | source   # in ~/.config/fish/config.fish",
}


@click.command("completion", short_help="Print the shell completion script.")
@click.argument("shell", required=False, type=click.Choice(sorted(_SETUP)))
@click.pass_context
def completion_cmd(ctx: click.Context, shell: str | None) -> None:
    """Print the completion script for SHELL (default: your login shell)."""
    from click.shell_completion import get_completion_class

    shell = shell or Path(os.environ.get("SHELL", "zsh")).name
    completion_class = get_completion_class(shell)
    if completion_class is None or shell not in _SETUP:
        print_error(f"No completion for {shell!r}; choose one of: {', '.join(sorted(_SETUP))}.")
        ctx.exit(2)
    root = ctx.find_root().command
    script = completion_class(root, {}, "kiwi-dl", "_KIWI_DL_COMPLETE").source()
    click.echo(script)
    if sys.stdout.isatty():
        click.echo(f"# To enable it: {_SETUP[shell]}", err=True)
