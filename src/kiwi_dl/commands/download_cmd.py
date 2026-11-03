"""`kiwi-dl download` - download files/folders from MEGA public links."""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from pathlib import Path

import click

from ..config import default_download_dir
from ..core.api import MegaAPIClient
from ..core.downloader import MegaDownloader
from ..core.links import LinkType, parse_link, resolve_password_link
from ..ui import report
from ..ui.machine_output import MachineOutput
from ..ui.prompts import print_error
from ..ui.theme import make_console
from ..ui.transfer_progress import TransferProgress
from ..utils.helpers import format_bytes, format_eta
from ..utils.hooks import run_all_finished_command
from ..utils.selection import (
    build_folder_file_filter,
    compose_file_filters,
)
from ..utils.speed import make_limiter
from .api_support import api_for
from .download_support import (
    EXIT_QUOTA_EXCEEDED,
    RunTally,
    _download_file,
    _download_folder,
    _download_folder_file,
    _interactive_file_picker,
)

log = logging.getLogger(__name__)
_console = make_console()


def _print_summary(
    tally: RunTally, failures: int, quota_stops: int, output_dir: Path, seconds: float
) -> None:
    """The closing block: what was saved, where, and what is left on the disk."""
    rows = [("Downloaded", f"{format_bytes(tally.total_bytes)} · {tally.files} file(s)")]
    if failures:
        rows.append(("Failed", f"{failures} item(s), listed above"))
    if quota_stops:
        rows.append(("Quota", "MEGA's transfer quota stopped some items; retry once it resets"))
    rows.append(("Saved to", report.short_path(output_dir)))
    free = report.free_space(output_dir)
    if free is not None:
        rows.append(("Free space", format_bytes(free)))
    rows.append(("Time", format_eta(seconds)))
    title = "Download complete" if not failures else "Download finished with errors"
    report.summary(_console, title, rows, ok=not failures)


def _read_links_file(path: Path) -> list[str]:
    """Read a list of MEGA URLs from a text file (one per line, # for comments)."""
    return [
        line
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]


@click.command("download", short_help="Download a file or folder from a MEGA link.")
@click.argument("urls", nargs=-1, required=False)
@click.option(
    "-o",
    "--output",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Destination directory (default: config download_path, else the current directory).",
)
@click.option(
    "-w",
    "--workers",
    type=int,
    default=None,
    help="Number of parallel chunk workers per file.",
)
@click.option(
    "-l",
    "--limit",
    "speed_limit_kbps",
    type=float,
    default=None,
    help="Aggregate download speed limit for this command in KB/s (0 = unlimited).",
)
@click.option(
    "-p",
    "--password",
    default=None,
    envvar="KIWI_DL_PASSWORD",
    help="Password for password-protected links (or set KIWI_DL_PASSWORD).",
)
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip file integrity check (faster but unsafe).",
)
@click.option(
    "--overwrite",
    "--force",
    "overwrite",
    is_flag=True,
    help="Overwrite an existing destination file. By default an unrelated "
    "existing file is preserved and a unique name is used instead.",
)
@click.option(
    "--rename",
    default=None,
    help="Override the filename (only for single-file links).",
)
@click.option(
    "--proxy",
    default=None,
    help="HTTP/SOCKS proxy URL (e.g. http://127.0.0.1:8080).",
)
@click.option(
    "-i",
    "--input-file",
    "input_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read links from a text file, one per line (# starts a comment).",
)
@click.option(
    "-P",
    "--parallel",
    "parallel_transfers",
    type=int,
    default=None,
    help="Number of files to download simultaneously (global limit).",
)
@click.option(
    "-I",
    "--include",
    "include_patterns",
    multiple=True,
    help="Folder links: only download files matching this glob (repeatable; "
    "matches the folder-relative path or filename, case-insensitive).",
)
@click.option(
    "-X",
    "--exclude",
    "exclude_patterns",
    multiple=True,
    help="Folder links: skip files matching this glob (repeatable; wins over --include).",
)
@click.option(
    "--select",
    "select_files",
    is_flag=True,
    help="Folder links: list the files and interactively choose which to download "
    "(e.g. 1,3-5 | all | none).",
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Machine mode: emit one JSON result record per line on stdout; "
    "human output and progress go to stderr.",
)
@click.pass_context
def download(
    ctx: click.Context,
    urls: tuple[str, ...],
    output_dir: Path | None,
    workers: int | None,
    speed_limit_kbps: float | None,
    password: str | None,
    no_verify: bool,
    overwrite: bool,
    rename: str | None,
    proxy: str | None,
    input_file: Path | None,
    parallel_transfers: int | None,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    select_files: bool,
    json_output: bool,
) -> None:
    """Download one or more MEGA links.

    Exit status is non-zero when any link fails; an interactive selection
    answered with "none" is a user skip, not a failure.
    """
    cfg = ctx.obj["config"]
    quiet = bool(ctx.obj.get("quiet"))
    # Machine mode: JSONL records own stdout; humans read stderr; the live
    # progress view is disabled. The emitter grabs the real stdout BEFORE the
    # redirect below.
    machine = MachineOutput(json_output)
    if json_output:
        quiet = True
        redirect = contextlib.redirect_stdout(sys.stderr)
        redirect.__enter__()
        ctx.call_on_close(lambda: redirect.__exit__(None, None, None))
    proxies = {"http": proxy, "https": proxy} if proxy else None
    failures = 0

    # Collect URLs from args and/or file
    url_list: list[str] = list(urls)
    if input_file:
        url_list.extend(_read_links_file(input_file))
    if not url_list:
        print_error("No URLs given. Pass URLs as arguments or use -i <file>.")
        ctx.exit(2)

    output_dir = output_dir or (
        Path(cfg.download_path).expanduser() if cfg.download_path else default_download_dir()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    tally = RunTally()
    if not quiet:
        report.header(_console, "Download", output_dir)
    # Folder-link file selection (parity with MegaBasterd's FolderLinkDialog):
    # glob filters first, then the interactive picker on whatever remains.
    folder_file_filter = compose_file_filters(
        build_folder_file_filter(include_patterns, exclude_patterns, output_dir),
        _interactive_file_picker(output_dir) if select_files else None,
    )
    workers = workers if workers is not None else cfg.max_workers
    speed_limit_kbps = speed_limit_kbps if speed_limit_kbps is not None else cfg.speed_limit_kbps
    verify = not no_verify and cfg.verify_integrity
    parallel = parallel_transfers if parallel_transfers is not None else cfg.max_parallel_downloads
    parallel = max(1, parallel)

    # Smart proxy: if enabled and no explicit --proxy was given, load the
    # persistent pool + any URLs from `smart_proxy_url` config. One policy for
    # the whole command, because MEGA's quota belongs to the exit IP: a 509 on
    # one file opens the proxy window for every file (proxy/quota_router.py).
    from ..proxy.quota_router import SmartProxyPolicy
    from ..proxy.runtime import effective_pool_for_cmd

    proxy_pool = effective_pool_for_cmd(cfg, proxy)
    smart_proxy = SmartProxyPolicy.from_config(cfg, proxy_pool)
    quota_stops: list[str] = []

    def _new_api() -> MegaAPIClient:
        """One isolated API client (own Session + sequence) per transfer.

        `MegaAPIClient` owns a mutable `requests.Session`, sequence counter,
        and SID; independent parallel transfers must never share one. The
        proxy pool below IS shared — it is explicitly thread-safe.
        """
        # The pool is for chunks. API calls stay direct unless strict: an
        # anonymous `a:g` keeps answering while the transfer quota is spent, so
        # proxying it only hands the link to more third parties.
        return api_for(
            cfg,
            proxies=proxies,
            proxy_pool=proxy_pool if cfg.force_smart_proxy else None,
            user_agent=cfg.user_agent,
        )

    # ONE limiter per command: `speed_limit_kbps` (or --limit) is an aggregate
    # cap shared by every parallel transfer, not a per-file value.
    shared_limiter = make_limiter(speed_limit_kbps)

    # Each parallel slot gets its own MegaDownloader because the downloader
    # holds per-transfer state (CDN URL, generation counter) — and its own
    # API client (see `_new_api`). The helpers below close that API client
    # on every path.
    def _new_downloader() -> MegaDownloader:
        return MegaDownloader(
            api=_new_api(),
            max_workers=workers,
            speed_limit_kbps=speed_limit_kbps,
            verify_integrity=verify,
            timeout=cfg.timeout_seconds,
            proxies=proxies,
            proxy_pool=proxy_pool,
            force_proxy=cfg.force_smart_proxy,
            quota_wait_seconds=cfg.quota_wait_seconds,
            quota_max_wait_loops=cfg.quota_max_wait_loops,
            keep_state_files_on_error=cfg.keep_state_files_on_error,
            overwrite=overwrite,
            limiter=shared_limiter,
            auto_resume=cfg.auto_resume,
            user_agent=cfg.user_agent,
            smart_proxy=smart_proxy,
        )

    # First pass: parse + filter URLs, collect actionable jobs
    file_jobs: list[tuple[str, str | None]] = []  # (url, rename_to)
    folder_file_jobs: list[str] = []
    folder_jobs: list[str] = []
    for url in url_list:
        try:
            parsed = parse_link(url)
        except ValueError as e:
            print_error(str(e))
            failures += 1
            continue

        if parsed.type == LinkType.PASSWORD_PROTECTED:
            if not password:
                print_error(f"Link is password-protected; supply -p PASSWORD: {url}")
                failures += 1
                continue
            try:
                parsed = resolve_password_link(parsed, password)
            except ValueError as exc:
                print_error(f"Wrong password: {exc}")
                failures += 1
                continue

        # `url` here may still be the original password or legacy URL - the
        # downstream URL is built from the parsed (resolved) form, so the
        # downloader never re-parses the wrapper, and a legacy and a modern
        # link to the same file share one resume state.
        if parsed.type in (LinkType.FOLDER, LinkType.FOLDER_IN_FOLDER):
            resolved_url = (
                f"https://mega.nz/folder/{parsed.public_id}#{parsed.key}"
                if parsed.public_id and parsed.key
                else url
            )
            if parsed.type == LinkType.FOLDER_IN_FOLDER and parsed.subpath:
                resolved_url = (
                    f"https://mega.nz/folder/{parsed.public_id}#{parsed.key}"
                    f"/folder/{parsed.subpath}"
                )
            folder_jobs.append(resolved_url)
        elif parsed.type == LinkType.FILE_IN_FOLDER:
            if not parsed.subpath:
                print_error("File-in-folder link is missing the file handle.")
                failures += 1
                continue
            resolved_url = (
                f"https://mega.nz/folder/{parsed.public_id}#{parsed.key}" f"/file/{parsed.subpath}"
            )
            folder_file_jobs.append(resolved_url)
        elif parsed.type == LinkType.FILE:
            resolved_url = f"https://mega.nz/file/{parsed.public_id}#{parsed.key}"
            file_jobs.append((resolved_url, rename if len(url_list) == 1 else None))
        else:
            print_error(f"Unsupported link type: {parsed.type}")
            failures += 1

    # Links that never became a job (parse/resolve/unsupported). They are still
    # items this run attempted, so the batch summary below matches the exit code.
    unusable_links = failures

    # One shared progress system for every download mode: single files,
    # parallel files, and file-in-folder links share this controller; each
    # full folder link gets its own controller (its manifest defines it).
    if file_jobs or folder_file_jobs:
        progress = TransferProgress(title="", direction="download", quiet=quiet)
        with progress:
            # Process file jobs in parallel slots
            if file_jobs:
                if parallel == 1 or len(file_jobs) == 1:
                    for url, rename_to in file_jobs:
                        failures += (
                            0
                            if _download_file(
                                _new_downloader(),
                                url,
                                output_dir,
                                progress,
                                password=password,
                                rename_to=rename_to,
                                machine=machine,
                                run_command=cfg.run_command,
                                quota_stops=quota_stops,
                                tally=tally,
                            )
                            else 1
                        )
                else:
                    from concurrent.futures import ThreadPoolExecutor, as_completed

                    def _run(job):
                        url, rename_to = job
                        return _download_file(
                            _new_downloader(),
                            url,
                            output_dir,
                            progress,
                            password=password,
                            rename_to=rename_to,
                            machine=machine,
                            run_command=cfg.run_command,
                            quota_stops=quota_stops,
                            tally=tally,
                        )

                    with ThreadPoolExecutor(max_workers=parallel) as pool:
                        futs = [pool.submit(_run, job) for job in file_jobs]
                        for f in as_completed(futs):
                            try:
                                if not f.result():
                                    failures += 1
                            except Exception as e:  # noqa: BLE001
                                log.error("Parallel download failed: %s", e)
                                failures += 1

            if folder_file_jobs:
                # Interactive selection must prompt from the main thread, so
                # --select forces these jobs to run sequentially.
                if parallel == 1 or select_files or len(folder_file_jobs) == 1:
                    for url in folder_file_jobs:
                        outcome = _download_folder_file(
                            _new_downloader(),
                            url,
                            output_dir,
                            progress,
                            file_filter=folder_file_filter,
                            machine=machine,
                            run_command=cfg.run_command,
                            quota_stops=quota_stops,
                            tally=tally,
                        )
                        failures += 0 if outcome else 1
                else:
                    from concurrent.futures import ThreadPoolExecutor, as_completed

                    def _run_folder_file(url):
                        return _download_folder_file(
                            _new_downloader(),
                            url,
                            output_dir,
                            progress,
                            file_filter=folder_file_filter,
                            machine=machine,
                            run_command=cfg.run_command,
                            quota_stops=quota_stops,
                            tally=tally,
                        )

                    with ThreadPoolExecutor(max_workers=parallel) as pool:
                        futs = [pool.submit(_run_folder_file, url) for url in folder_file_jobs]
                        for f in as_completed(futs):
                            try:
                                if not f.result():
                                    failures += 1
                            except Exception as e:  # noqa: BLE001
                                log.error("Parallel folder-file download failed: %s", e)
                                failures += 1

    # Folder jobs get one controller each (their manifests define the rows).
    for url in folder_jobs:
        if not _download_folder(
            _new_downloader(),
            url,
            output_dir,
            parallel_files=parallel,
            file_filter=folder_file_filter,
            quiet=quiet,
            machine=machine,
            run_command=cfg.run_command,
            quota_stops=quota_stops,
            tally=tally,
        ):
            failures += 1

    # Exactly once per invocation, on this thread, after every item has been
    # attempted - failures included. `ctx.exit` below raises, so this must come
    # first. An empty batch is filtered inside the hook.
    attempted = len(file_jobs) + len(folder_file_jobs) + len(folder_jobs) + unusable_links
    run_all_finished_command(
        cfg.all_finished_command,
        kind="download",
        succeeded=attempted - failures,
        failed=failures,
    )

    if not quiet:
        _print_summary(tally, failures, len(quota_stops), output_dir, time.monotonic() - started)
    elif failures:
        # Quiet and --json runs print no summary, but a failed batch still says so.
        print_error(f"{failures} download item(s) failed.")
    if failures:
        if len(quota_stops) == failures:
            ctx.exit(EXIT_QUOTA_EXCEEDED)
        ctx.exit(1)
