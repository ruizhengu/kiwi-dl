"""Entry points: `kiwi-dl` (download, info, proxy, config) and `mg-dl` (download)."""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import signal
import sys
import threading
from pathlib import Path

import click
from rich.text import Text

from . import __version__
from .commands.config_cmd import config_cmd
from .commands.download_cmd import download
from .commands.info_cmd import info_cmd
from .commands.proxy_cmd import proxy_cmd
from .config import ConfigStore, config_file, data_dir, log_dir, user_dir
from .core.downloader import MegaDownloader
from .ui.theme import make_console
from .utils.logger import setup_logging

console = make_console()


LINK_MARKERS = ("mega.nz/", "mega.co.nz/", "mega://")


def _redacted_argv(argv: list[str]) -> list[str]:
    """Hide share keys, passwords, and token-like values in debug logs."""
    redacted: list[str] = []
    sensitive_next = False
    sensitive_options = {"-p", "--password"}
    for arg in argv:
        if sensitive_next:
            redacted.append("<redacted>")
            sensitive_next = False
            continue
        if arg in sensitive_options:
            redacted.append(arg)
            sensitive_next = True
            continue
        # Also redact inline `--option=value` forms of sensitive options.
        if "=" in arg and arg.split("=", 1)[0] in sensitive_options:
            redacted.append(arg.split("=", 1)[0] + "=<redacted>")
            continue
        if any(prefix in arg for prefix in LINK_MARKERS):
            redacted.append("<redacted-link>")
            continue
        redacted.append(arg)
    return redacted


def _startup_args_for_log() -> list[str]:
    """Return process args only when this process looks like the CLI itself."""
    argv0 = str(sys.argv[0]).replace("\\", "/").rsplit("/", 1)[-1].lower()
    cli_names = {"kiwi-dl", "kiwi-dl.exe", "mg-dl", "mg-dl.exe", "__main__.py"}
    if argv0 in cli_names:
        return _redacted_argv(sys.argv[1:])
    return []


def _command_name_for_log(args: list[str]) -> str:
    """Return the first non-global-option token as the command name."""
    if not args:
        return "menu/help"
    for arg in args:
        if arg.startswith("-"):
            continue
        return arg
    return "global"


def _config_summary_for_log(store: ConfigStore) -> dict[str, object]:
    """Return non-secret config values that help debug user runs."""
    cfg = store.config
    return {
        "config_file": str(store.path),
        "user_dir": str(user_dir()),
        "data_dir": str(data_dir()),
        "log_dir": str(log_dir()),
        "download_path": cfg.download_path,
        "max_workers": cfg.max_workers,
        "max_parallel_downloads": cfg.max_parallel_downloads,
        "speed_limit_kbps": cfg.speed_limit_kbps,
        "verify_integrity": cfg.verify_integrity,
        "auto_resume": cfg.auto_resume,
        "keep_state_files_on_error": cfg.keep_state_files_on_error,
        "smart_proxy_enabled": cfg.smart_proxy_enabled,
        "force_smart_proxy": cfg.force_smart_proxy,
        "timeout_seconds": cfg.timeout_seconds,
        "quota_wait_seconds": cfg.quota_wait_seconds,
        "quota_max_wait_loops": cfg.quota_max_wait_loops,
        "log_level": cfg.log_level,
        "log_to_file": cfg.log_to_file,
        "log_max_bytes": cfg.log_max_bytes,
        "log_backups": cfg.log_backups,
    }


# The ONLY command group allowed to run against a corrupt config: it is the
# surface that inspects and repairs one. Its own mutating subcommands still
# refuse individually (ConfigCorruptionError), so nothing here can overwrite
# the preserved file except the explicit `config recover --reset`.
_CORRUPT_CONFIG_ALLOWLIST = frozenset({"config"})


_CONTEXT = {"help_option_names": ["-h", "--help"]}


def _global_options(fn):
    """`-v`, `-q`, `--log-file` and `--version`: the same on both entry points."""
    for option in reversed(
        [
            click.option(
                "-v",
                "--verbose",
                count=True,
                help="Increase verbosity (-v for INFO, -vv for DEBUG).",
            ),
            click.option(
                "-q",
                "--quiet",
                is_flag=True,
                help="Suppress console output (errors still shown).",
            ),
            click.option(
                "--log-file/--no-log-file",
                default=None,
                help="Write a debug log file in the user log directory.",
            ),
            click.version_option(version=__version__, prog_name="kiwi-dl"),
        ]
    ):
        fn = option(fn)
    return fn


@click.group(
    context_settings=_CONTEXT,
    help="kiwi-dl - download public MEGA links. Not affiliated with Mega Limited.",
)
@_global_options
@click.pass_context
def cli(ctx: click.Context, verbose: int, quiet: bool, log_file: bool | None) -> None:
    """Top-level CLI group."""
    _bootstrap(ctx, verbose, quiet, log_file, ctx.invoked_subcommand)


def _bootstrap(
    ctx: click.Context,
    verbose: int,
    quiet: bool,
    log_file: bool | None,
    command: str | None,
) -> None:
    """Load the config, refuse a corrupt one, and set up logging."""
    ctx.ensure_object(dict)
    store = ConfigStore()
    ctx.obj["config_store"] = store
    ctx.obj["config"] = store.config
    ctx.obj["console"] = console
    ctx.obj["quiet"] = quiet

    # Fail CLOSED on a corrupt config. `load()` returns defaults so the CLI can
    # still start and be repaired, but those defaults are NOT the user's
    # policy: a file that said `force_smart_proxy = true` would load as false
    # and the next command would open a direct socket. Everything except the
    # recovery/inspection surface therefore stops here, before any request,
    # destination reservation, or stored-state mutation.
    if store.is_corrupt and command not in _CORRUPT_CONFIG_ALLOWLIST:
        click.echo(store.corruption_reason, err=True)
        click.echo(
            f"Refusing to run `{command}` with an unusable configuration: "
            "your saved settings (proxy policy, integrity, paths, limits) cannot be "
            "applied. Inspect it with `kiwi-dl config show`, or start over with "
            "`kiwi-dl config recover --reset`.",
            err=True,
        )
        ctx.exit(1)

    if verbose >= 2:
        level = "DEBUG"
    elif verbose == 1:
        level = "INFO"
    elif quiet:
        level = "ERROR"
    else:
        level = store.config.log_level

    env_log_file = os.environ.get("KIWI_DL_LOG_FILE")
    if log_file is False:
        log_path = None
    elif env_log_file:
        log_path = Path(env_log_file)
    else:
        use_log_file = log_file if log_file is not None else store.config.log_to_file
        log_path = (log_dir() / "kiwi-dl.log") if use_log_file else None

    startup_args = _startup_args_for_log()
    command_name = _command_name_for_log(startup_args)
    setup_logging(
        level=level,
        log_file=log_path,
        quiet=quiet,
        max_bytes=store.config.log_max_bytes,
        backup_count=store.config.log_backups,
        command=command_name,
    )
    log = logging.getLogger(__name__)
    log.info(
        "CLI start version=%s cwd=%s executable=%s python=%s platform=%s args=%s "
        "log_file=%s config_file=%s effective_level=%s quiet=%s verbose=%s",
        __version__,
        os.getcwd(),
        sys.executable,
        sys.version.replace("\n", " "),
        platform.platform(),
        startup_args,
        log_path,
        config_file(),
        level,
        quiet,
        verbose,
    )
    log.debug(
        "Runtime paths user_dir=%s data_dir=%s log_dir=%s",
        user_dir(),
        data_dir(),
        log_dir(),
    )
    log.debug(
        "Config summary: %s",
        _config_summary_for_log(store),
    )


cli.add_command(download)
cli.add_command(info_cmd)
cli.add_command(proxy_cmd)
cli.add_command(config_cmd, name="config")


@click.command(
    "mg-dl",
    context_settings=_CONTEXT,
    help="Download public MEGA links: `mg-dl 'https://mega.nz/file/ID#KEY' [-o DIR]`. "
    "The same as `kiwi-dl download`. Not affiliated with Mega Limited.",
)
@_global_options
@click.pass_context
def mg_dl(ctx: click.Context, verbose: int, quiet: bool, log_file: bool | None, **options) -> None:
    _bootstrap(ctx, verbose, quiet, log_file, "download")
    ctx.invoke(download, **options)


# Every `download` option and argument, so `mg-dl --help` lists them as its own.
mg_dl.params.extend(download.params)


# Set by the SIGINT handler below; read by `main` to tell Ctrl-C apart from
# any other exit status 1.
_interrupted = threading.Event()


def _stop_transfers_then_interrupt(signum, frame) -> None:
    """SIGINT: stop every running download, then raise KeyboardInterrupt as usual.

    The interrupt is delivered to the main thread only. Chunk workers, and the
    per-file workers of a parallel or folder download, never see it, so a
    download carried on to the end of the file while the main thread waited to
    join them.
    """
    _interrupted.set()
    MegaDownloader.stop_all()
    signal.default_int_handler(signum, frame)


def main() -> int:
    """`kiwi-dl` console script."""
    return _run(cli, "kiwi-dl")


def mg_dl_main() -> int:
    """`mg-dl` console script."""
    return _run(mg_dl, "mg-dl")


def _run(command: click.Command, prog_name: str) -> int:
    # Make stdout/stderr Unicode-safe on legacy Windows terminals so messages
    # that contain characters outside cp1252 (e.g. arrows, em-dashes, non-Latin
    # filenames returned by MEGA) don't crash the process.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    _interrupted.clear()
    on_main_thread = threading.current_thread() is threading.main_thread()
    previous = (
        signal.signal(signal.SIGINT, _stop_transfers_then_interrupt) if on_main_thread else None
    )
    try:
        command(prog_name=prog_name)
        return 0
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        return 130
    except SystemExit as exc:
        # Click's standalone mode catches KeyboardInterrupt itself, prints
        # "Aborted!" and exits 1, so the documented 130 above was never reached.
        if _interrupted.is_set() and exc.code == 1:
            return 130
        raise
    except Exception as e:
        logging.getLogger(__name__).exception("Fatal error: %s", e)
        # `e` may carry server-supplied text; render it literally, not as markup.
        console.print(Text.assemble(("Error:", "bold red"), " ", str(e)))
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    sys.exit(main())
