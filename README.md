<div align="center">
  <img src="assets/icon.svg" alt="kiwi-dl icon" width="112">
  <h1>kiwi-dl</h1>
  <p><b>Fast, resumable MEGA CLI downloader for macOS, Windows, and Linux.</b></p>
  <p>
    <a href="https://github.com/ruizhengu/kiwi-dl/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/ruizhengu/kiwi-dl/tests.yml?branch=main&label=tests&style=flat-square" alt="Tests"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT license"></a>
    <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey?style=flat-square" alt="macOS, Windows and Linux">
    <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
  </p>
  <img src="assets/demo.svg" alt="kiwi-dl downloading a MEGA folder" width="760">
</div>

No MEGA account required. Downloads public files and folders, resumes cleanly, verifies every file's MAC, and handles MEGA's transfer quota automatically. Not affiliated with Mega Limited — see [CHANGELOG.md](CHANGELOG.md) for releases.

## Install

**Homebrew (macOS / Linux)**

```bash
brew install ruizhengu/tap/kiwi-dl
```

**macOS / Linux** (uses `uv` or `pipx`; append `-s -- v0.2.0` to pin a release)

```bash
curl -fsSL https://raw.githubusercontent.com/ruizhengu/kiwi-dl/main/install.sh | bash
```

**Windows (PowerShell)** (set `KIWI_DL_REF=v0.2.0` to pin a release)

```powershell
irm https://raw.githubusercontent.com/ruizhengu/kiwi-dl/main/install.ps1 | iex
```

**Manual**

```bash
uv tool install git+https://github.com/ruizhengu/kiwi-dl
```

## Usage

Run `kiwi-dl` with no arguments for the interactive menu, or use `kdl` directly:

```bash
kiwi-dl                                                  # interactive menu
kdl 'https://mega.nz/file/ID#KEY'                        # download a file
kdl 'https://mega.nz/folder/ID#KEY' -o ~/Downloads       # download a folder
kdl 'https://mega.nz/folder/ID#KEY' --select             # pick files interactively
kdl -i links.txt -P 3                                    # batch download, 3 files at once
kiwi-dl info 'https://mega.nz/folder/ID#KEY' --json      # inspect without downloading
```

> Wrap links in single quotes so the shell does not interpret `#` or `!`.

**Common options:**

- `-o DIR` — output directory (default: current directory or `download_path`)
- `--select` / `-I '*.mp4'` / `-X '*.txt'` — pick or glob-filter files in a folder
- `-P N` / `-w N` / `-l KBPS` — parallel files, connections per file, total speed cap
- `-p PASS` (or `KIWI_DL_PASSWORD`) — password-protected `#P!` links
- `--overwrite` — replace existing files (by default clashes are saved as `name (1).ext`)
- `--json` — emit one JSON record per file on stdout for scripts

Re-run the same command to resume an unfinished download. `Ctrl-C` stops promptly and keeps completed chunks.

## Quota & Proxies

When MEGA returns HTTP 509 (transfer quota exceeded):

- **Without proxies**: kiwi-dl waits for the quota reset and resumes automatically (retrying every minute, or immediately if your network/VPN changes). Cap or disable the wait with `kiwi-dl config set quota_budget_seconds 0`.
- **With proxies**: downloads run direct until a 509, then switch to your proxy pool automatically (HTTP, HTTPS, SOCKS4, SOCKS5). Link lookups stay direct, bad proxies are cooled down, and corrupted proxied chunks are re-fetched if MAC verification fails.

```bash
kiwi-dl proxy add 'http://user:pass@203.0.113.7:8080' 'socks5://198.51.100.2:1080'
kiwi-dl proxy import proxies.txt
kiwi-dl proxy add-list 'https://example.com/socks5.txt' --scheme socks5
kiwi-dl proxy test --keep-working
```

*Note: kiwi-dl ships no proxy lists. Using proxies to bypass MEGA's quota violates MEGA's Terms of Service; use at your own discretion.*

## Configuration

```bash
kiwi-dl config show
kiwi-dl config set max_parallel_downloads 3
kiwi-dl config path
```

| Key | Default | Description |
| --- | --- | --- |
| `download_path` | *(cwd)* | Default output directory when `-o` is omitted |
| `max_workers` | `8` | Connections per file |
| `max_parallel_downloads` | `6` | Files downloaded concurrently (`-P`) |
| `speed_limit_kbps` | `0` | Aggregate speed cap in KB/s (`0` = unlimited) |
| `smart_proxy_mode` | `on-509` | `on-509`, `always`, or `off` |
| `smart_proxy_window_seconds` | `3600` | Seconds a 509 keeps downloads on proxies (60–86400) |
| `smart_proxy_ban_seconds` | `300` | Cooldown for failing proxies (`0` = rest of run) |
| `smart_proxy_timeout_seconds` | `5` | Connect timeout per proxy before trying another (3–120) |
| `quota_budget_seconds` | *(unlimited)* | Max seconds to wait on quota (`0` = exit immediately with code `3`) |
| `force_smart_proxy` | `false` | Refuse direct connections (including link lookups) |
| `smart_proxy_url` | *(none)* | Extra comma-separated proxy URLs |
| `run_command` | *(none)* | Hook run after each file with its path |
| `all_finished_command` | *(none)* | Hook run at the end with `download <ok> <failed>` |

Config and logs live in the platform's standard app directories (`~/Library/...` on macOS, XDG on Linux, `%APPDATA%` / `%LOCALAPPDATA%` on Windows), or set `KIWI_DL_HOME` to keep everything in one directory.

## Manage & Scripting

```bash
kiwi-dl update                      # update uv/pipx install (Homebrew: brew upgrade kiwi-dl)
kiwi-dl remove                      # uninstall (add --purge to also delete config/logs)
eval "$(kiwi-dl completion zsh)"    # shell completion: bash, zsh, fish
```

**Exit codes:** `0` ok · `1` failure · `2` bad usage · `3` stopped by quota budget · `130` Ctrl-C.

**Safety:** Never asks for a MEGA account; never overwrites existing files without `--overwrite`; redacts link keys and proxy credentials from logs and `--json` output; `--purge` never touches your downloads (preview with `--dry-run`).

## Development

```bash
git clone https://github.com/ruizhengu/kiwi-dl && cd kiwi-dl
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check src tests && .venv/bin/black --check src tests && .venv/bin/mypy src
```

## Credits & License

Built on [MegaBasterd-CLI](https://github.com/KiaroSama/MegaBasterd-CLI) by Kiaro Sama (MIT). Proxy behaviour follows [MegaBasterd](https://github.com/tonikelope/megabasterd); CLI design takes cues from [Mole](https://github.com/tw93/mole).

MIT licensed — see [LICENSE](LICENSE) and [ATTRIBUTION.md](ATTRIBUTION.md).
