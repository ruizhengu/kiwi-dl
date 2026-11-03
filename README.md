<div align="center">
  <h1>kiwi-dl</h1>
  <p><b>Download MEGA links from your Mac or Linux terminal: resumable, verified, quota-aware.</b></p>
  <p>
    <a href="https://github.com/ruizhengu/kiwi-dl/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/ruizhengu/kiwi-dl/tests.yml?branch=main&label=tests&style=flat-square" alt="Tests"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT license"></a>
    <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey?style=flat-square" alt="macOS and Linux">
    <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  </p>
  <img src="assets/demo.svg" alt="kiwi-dl downloading a MEGA folder: a header, one row per file, and a summary" width="760">
</div>

> **Pre-release.** There is no tagged version yet, so the installer installs the
> `main` branch. kiwi-dl is not affiliated with Mega Limited.

## Features

- **A menu, nothing to memorise.** Run `kiwi-dl` and choose with the arrow keys.
  Every menu action is also a plain command for scripts.
- **Files and whole folders.** Folders keep their layout; pick files from a list
  or filter them with globs.
- **Resumable.** Run the same command again and it carries on. Ctrl-C stops
  within a second and keeps what has finished.
- **Verified.** Every file is checked against the MAC in its link key before it
  counts as done.
- **Quota-aware.** When MEGA's free quota runs out, kiwi-dl says when it resets,
  or carries on through proxies you add.
- **Tidy.** One-line install, `kiwi-dl update`, and `kiwi-dl remove` to leave
  nothing behind.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/ruizhengu/kiwi-dl/main/install.sh | bash
```

Then:

```bash
kiwi-dl                                  # the menu
mg-dl 'https://mega.nz/file/ID#KEY'      # or download straight away
```

kiwi-dl runs on macOS (Apple Silicon or Intel) and Linux. The installer sets it
up as an isolated tool with [uv](https://docs.astral.sh/uv/), or pipx if you
have that instead, and tells you how to get uv if you have neither. To do it by
hand: `uv tool install git+https://github.com/ruizhengu/kiwi-dl`.

## Safety

- **No account.** kiwi-dl downloads public links and never asks for or uses a
  MEGA account.
- **Nothing is overwritten.** If a file of the same name is already there and is
  not an unfinished copy of this download, the new one is saved as `name (1).ext`.
  Pass `--overwrite` to replace it.
- **Keys stay private.** Logs and `--json` output never contain a link's key (the
  part after `#`), and proxy passwords show as `<redacted>`. The `.mbstate` file
  beside an unfinished download holds the full link because resuming needs it;
  it is deleted when the download completes.
- **Removal is scoped.** `kiwi-dl remove --purge` deletes kiwi-dl's own settings
  and logs, never your downloads. Add `--dry-run` to see the list first.

## Usage

### The menu

```text
kiwi-dl 0.1.0 · download public MEGA links
⚙ Apple Silicon · macOS 26.6 · 412.80 GB free · ~/Downloads

➤ Download   Save a file or folder from a MEGA link
  Inspect    See what a link holds, without downloading it
  Proxies    Your proxy pool: list, add, test, remove
  Settings   Download folder, parallel files, speed limit
  Help       Every command and option
  Quit

↑↓ move · ⏎ select · 1-9 jump · q quit
```

`↑`/`↓` or `j`/`k` move, Enter or a number chooses, `q` goes back. Download asks
for the link and offers `~/Downloads`; for a folder it offers to let you pick
the files. A Ctrl-C during a download stops it and brings you back here.

### Download

```bash
mg-dl 'https://mega.nz/file/ID#KEY'
mg-dl 'https://mega.nz/folder/ID#KEY' -o ~/Downloads --select
mg-dl -i links.txt -P 3
```

Put links in single quotes: the shell treats `#` and `!` specially, and old-style
`#!` and `#F!` links are common.

- Files land in the current folder unless you pass `-o` or set `download_path`.
- Folder links: `--select` to pick files, `-I '*.mp4'` / `-X '*.txt'` to filter.
- `-P 3` downloads three files at once, `-w 8` sets the connections per file, and
  `-l 2000` caps the speed at 2000 KB/s.
- Protected links: `-p PASSWORD`, or set `KIWI_DL_PASSWORD` so it stays out of
  your shell history.
- `--json` prints one JSON record per file on stdout, for scripts.

### When the quota runs out

MEGA limits how much one IP may download anonymously. Once it is spent, MEGA
answers HTTP 509 along with the time until it resets.

**Without proxies**, kiwi-dl stops straight away and tells you when to try again:

```text
✗ Download failed: MEGA transfer quota exceeded (HTTP 509); it resets in about 4h 38m
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
- Each connection keeps its proxy until that proxy fails. A proxy that returns
  an error is skipped by everyone for 5 minutes; a timeout only moves that one
  connection. If every proxy has failed, kiwi-dl goes back to a direct connection.
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

### Inspect a link

```bash
kiwi-dl info 'https://mega.nz/folder/ID#KEY'          # name, size, file count
kiwi-dl info 'https://mega.nz/folder/ID#KEY' --json   # plus every file's path
```

### Settings

```bash
kiwi-dl config show
kiwi-dl config set max_parallel_downloads 3
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

On macOS, settings and the proxy pool live in `~/Library/Application Support/kiwi-dl`
and the log in `~/Library/Logs/kiwi-dl`. On Linux they live in `~/.config/kiwi-dl`,
`~/.local/share/kiwi-dl` and `~/.local/state/kiwi-dl` (or your `XDG_*` folders).
Set `KIWI_DL_HOME` to keep everything in one folder instead.

### Update, remove, completion

```bash
kiwi-dl update                      # reinstall the latest, with the tool that installed it
kiwi-dl remove                      # uninstall; settings and logs stay
kiwi-dl remove --purge --dry-run    # see what --purge would delete
eval "$(kiwi-dl completion zsh)"    # tab completion; also bash and fish
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything downloaded. |
| `1` | Something failed. |
| `2` | Bad usage, such as no link given. |
| `3` | Only MEGA's quota stopped it; try again after the reset. |
| `130` | Stopped with Ctrl-C. |

## Development

```bash
git clone https://github.com/ruizhengu/kiwi-dl && cd kiwi-dl
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest                                   # parallel; no network, no MEGA account
.venv/bin/ruff check src tests && .venv/bin/black --check src tests && .venv/bin/mypy src
```

The tests talk to a fake MEGA (`tests/fake_mega.py`) that serves really encrypted
files and answers like the live service, 509s included. CI runs them on macOS
and Ubuntu with Python 3.10 to 3.14, and installs with `install.sh` on both.

## Credits

Built on [MegaBasterd-CLI](https://github.com/KiaroSama/MegaBasterd-CLI) by Kiaro
Sama (MIT). The proxy behaviour follows [MegaBasterd](https://github.com/tonikelope/megabasterd)
by tonikelope, and the interface takes its cues from [Mole](https://github.com/tw93/mole)
by tw93. kiwi-dl is not affiliated with Mega Limited.

MIT licensed. See [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md).
