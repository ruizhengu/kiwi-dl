# kiwi-dl

Download public MEGA links from the terminal. When MEGA's download quota for
your IP runs out, kiwi-dl can carry on through proxies you provide, the way
MegaBasterd's SmartProxy does.

> Not affiliated with Mega Limited. kiwi-dl is built on
> [MegaBasterd-CLI](https://github.com/KiaroSama/MegaBasterd-CLI) by Kiaro Sama
> (MIT), and its proxy behaviour follows
> [MegaBasterd](https://github.com/tonikelope/megabasterd).

## Install

macOS (Apple Silicon or Intel) or Linux, Python 3.10 or newer:

```bash
uv tool install git+https://github.com/ruizhengu/kiwi-dl
```

or `pipx install git+https://github.com/ruizhengu/kiwi-dl`. Both put two
commands on your `PATH`: `mg-dl` to download and `kiwi-dl` for everything else.

## Download

```bash
mg-dl 'https://mega.nz/file/ID#KEY'
mg-dl 'https://mega.nz/folder/ID#KEY' -o ~/Downloads
mg-dl -i links.txt
```

Put links in single quotes: the shell treats `#` and `!` specially, and old-style
`#!` and `#F!` links are common.

- Files land in the current folder unless you pass `-o` or set `download_path`.
- Run the same command again to resume. Finished chunks are recorded in a
  `.mbstate` file next to the download, which is deleted once the file is complete.
- Ctrl-C stops within a second and keeps what has finished.
- Every file is checked against the MAC in its link key before it counts as done.
- Folder links: `--select` to pick files, `-I '*.mp4'` / `-X '*.txt'` to filter.
- `-P 3` downloads three files at once; `-w 8` sets the connections per file;
  `-l 2000` caps the speed at 2000 KB/s.
- `--json` prints one JSON record per file on stdout for scripts.

## When the quota runs out

MEGA limits how much one IP may download anonymously. Once it is spent, MEGA's
servers answer HTTP 509 along with the time until it resets.

**Without proxies**, kiwi-dl stops straight away and tells you when to try again:

```text
Download failed: MEGA transfer quota exceeded (HTTP 509); it resets in about 4h 38m
```

The exit code is 3. Run the same command after the reset and it resumes. To wait
instead of stopping, give it a budget: `kiwi-dl config set quota_budget_seconds 7200`.

**With your own proxies**, the download carries on:

```bash
kiwi-dl proxy add 'http://user:pass@203.0.113.7:8080' 'socks5://198.51.100.2:1080'
kiwi-dl proxy import proxies.txt     # one per line
kiwi-dl proxy test                   # which ones answer
```

- Downloads go direct until MEGA answers 509. Then every download in the run
  switches to the proxies for an hour, or less if the quota resets sooner, and
  goes direct again after that.
- Each connection keeps its proxy until that proxy fails.
- A proxy that returns an error is skipped by everyone for 5 minutes. A timeout
  only moves that one connection to another proxy.
- If every proxy has failed, kiwi-dl goes back to a direct connection.
- Link lookups always go direct; only file data goes through proxies.
- Proxies can alter what they relay. If a file fails its final check, only the
  parts that came through proxies are downloaded again, and those proxies are
  skipped.

Using proxies to get around MEGA's download quota is against MEGA's terms of
service. kiwi-dl ships no proxy lists and cannot promise how much you can
download; whether and how you use this is your decision.

`smart_proxy_mode` changes this behaviour: `always` uses proxies from the first
byte, and `off` ignores them. `force_smart_proxy true` never connects directly,
including link lookups.

## Link info

```bash
kiwi-dl info 'https://mega.nz/folder/ID#KEY'          # name, size, file count
kiwi-dl info 'https://mega.nz/folder/ID#KEY' --json   # plus every file's path
```

## Settings

```bash
kiwi-dl config show
kiwi-dl config set max_workers 12
kiwi-dl config path
```

| Key | Default | What it does |
| --- | --- | --- |
| `download_path` | *(current folder)* | Where downloads go without `-o`. |
| `max_workers` | `8` | Connections per file. |
| `max_parallel_downloads` | `6` | Files at once (`-P`). |
| `speed_limit_kbps` | `0` | Total speed cap in KB/s, `0` for none. |
| `smart_proxy_mode` | `on-509` | `on-509`, `always` or `off`. |
| `smart_proxy_window_seconds` | `3600` | How long a 509 keeps downloads on proxies (60–86400). |
| `smart_proxy_ban_seconds` | `300` | How long a failing proxy is skipped; `0` for the rest of the run. |
| `quota_budget_seconds` | `0` | How long to wait for the quota when no proxy can help. |
| `force_smart_proxy` | `false` | Never connect directly. |
| `smart_proxy_url` | *(none)* | Extra proxies, comma-separated. |
| `run_command` | *(none)* | Run after each finished file, with its path as the argument. |
| `all_finished_command` | *(none)* | Run once at the end, with `download <ok> <failed>`. |

kiwi-dl keeps its config and proxy list in `~/Library/Application Support/kiwi-dl`
and its log in `~/Library/Logs/kiwi-dl`. On Linux it uses `~/.config/kiwi-dl`,
`~/.local/share/kiwi-dl` and `~/.local/state/kiwi-dl` (or your `XDG_*`
directories). Set `KIWI_DL_HOME` to keep everything in one folder instead.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything downloaded. |
| `1` | Something failed. |
| `2` | Bad usage, such as no link given. |
| `3` | Only MEGA's quota stopped it; try again after the reset. |
| `130` | Stopped with Ctrl-C. |

## Privacy

Logs and `--json` output never contain a link's key (the part after `#`), and
proxy passwords are shown as `<redacted>`. The `.mbstate` file next to an
unfinished download holds the link, key included, because resuming needs it; it
is deleted when the download completes. kiwi-dl never uses a MEGA account.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # runs in parallel; no network, no MEGA account
.venv/bin/ruff check src tests && .venv/bin/mypy src
```

The tests talk to a fake MEGA (`tests/fake_mega.py`) that serves really encrypted
files and answers like the live service, 509s included.

## License

MIT. See [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md).
