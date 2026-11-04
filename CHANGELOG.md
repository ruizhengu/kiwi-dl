# Changelog

## 0.1.0 - 2026-11-04

The first release of kiwi-dl: a macOS and Linux command line tool that downloads
public MEGA links, resumes them, verifies them, and keeps going when MEGA's
transfer quota runs out. It is built on
[MegaBasterd-CLI](https://github.com/KiaroSama/MegaBasterd-CLI) by Kiaro Sama
(MIT), cut down to downloading. kiwi-dl is not affiliated with Mega Limited.

### Downloading

- `kdl 'LINK'` (or `kiwi-dl download`) saves public files and whole folders,
  keeping the folder layout. Old `#!` and `#F!` links work, as do folder keys
  that carry several `handle:key` items.
- Folders: pick files with `--select`, or filter with `-I` / `-X` globs.
- `-P` downloads several files at once, `-w` sets connections per file, `-l`
  caps the total speed, and `-i` reads links from a file.
- Password-protected links: `-p`, or `KIWI_DL_PASSWORD` to keep the password
  out of your shell history.
- Running the same command again resumes an unfinished download.
- Every file is checked against the MAC in its link key before it counts as
  done.
- Nothing is overwritten unless you pass `--overwrite`: a clash is saved as
  `name (1).ext`.
- Ctrl-C stops within a second, through slow proxies too, and keeps what has
  finished.
- A finished download leaves only its file behind.
- `--json` prints one record per file, for scripts.

### When MEGA's quota runs out

- MEGA's HTTP 509 is read as the transfer quota, with the reset time from
  `X-MEGA-Time-Left`, not as a broken link.
- **Without proxies**, kiwi-dl waits for the quota to come back and carries on,
  as MegaBasterd does. It retries every minute, and at once if your network
  changes. `quota_budget_seconds` caps the wait; a run stopped only by the
  quota exits with code 3.
- **With proxies**, it behaves like MegaBasterd's SmartProxy, reimplemented
  rather than copied:
  - Downloads go direct until a 509, then through your proxies for an hour or
    until the quota resets.
  - Each connection keeps its proxy until that proxy fails.
  - A proxy that returns an error is skipped by everyone for 5 minutes. A
    timeout only moves that one connection.
  - If every proxy has failed, kiwi-dl goes back to a direct connection.
  - Link lookups never go through proxies.
- `kiwi-dl proxy add`, `import` and `add-list` fill the pool, with HTTP, HTTPS,
  SOCKS4 and SOCKS5 proxies. Lists are refreshed every hour, and 30 seconds
  after the pool runs dry. Entries naming private addresses are dropped.
- `kiwi-dl proxy test` asks MEGA for a tiny answer through each proxy, with no
  link and no quota used. `--keep-working` keeps the ones that answered.
- A proxy that does not connect within 5 seconds is passed over
  (`smart_proxy_timeout_seconds`).
- Free public proxies misbehave, and the damage is contained:
  - A proxy that ignores the requested byte range is banned before its data is
    used.
  - A proxy that refuses a fresh download URL as well as the old one is banned.
  - If a file fails its final check, only the parts that came through proxies
    are downloaded again.
- MEGA's hashcash challenge (HTTP 402) is solved. The solver was written from
  MEGA's protocol, so kiwi-dl stays MIT.

### Around the downloads

- `kiwi-dl` with no command opens an arrow-key menu. Every menu action is also a
  plain command.
- `kiwi-dl info LINK` shows a link's name, size and file count without
  downloading; `--json` adds every file's path.
- `kiwi-dl config` shows and changes settings, `update` reinstalls the latest
  version, `remove` uninstalls (`--purge` also deletes settings and logs, and
  `--dry-run` previews it), and `completion` prints bash, zsh or fish completion.
- A one-line installer sets kiwi-dl up as an isolated tool with uv, or pipx.
- Settings, the proxy pool and logs live in the platform's own folders
  (`~/Library/...` on macOS, XDG on Linux), or under `KIWI_DL_HOME`.
- Logs and `--json` output never contain a link's key; proxy passwords show as
  `<redacted>`.

### Compared with MegaBasterd-CLI

- Removed: MEGA accounts and login, uploads, streaming, and Windows support.
  kiwi-dl downloads public links anonymously and runs on macOS and Linux.
- Commands are `kiwi-dl` and `kdl` (the package is `kiwi_dl`).

### Known limits

- Free public proxies are slow and mostly dead at any moment. In live tests
  between 1 in 10 and 1 in 15 entries of a public list reached MEGA, and a
  download through them ran at about 1 MB/s, against about 19 MB/s direct.
- kiwi-dl ships no proxy list. Using proxies to get around MEGA's download quota
  is against MEGA's terms of service, and kiwi-dl cannot promise how much you
  can download.
- While a download is unfinished, the `.mbstate` file beside it holds the full
  link, key included, because resuming needs it. It is deleted when the
  download completes.
- The installer and `kiwi-dl update` follow the `main` branch. To install this
  release exactly, pass its tag:
  `curl -fsSL https://raw.githubusercontent.com/ruizhengu/kiwi-dl/main/install.sh | bash -s -- v0.1.0`.
  `kiwi-dl update` then reinstalls the same tag; install again without the tag
  to follow `main`.

Checked live, anonymously: a 1.34 GB download crossed a real 509 partway
through. It switched to public proxies and finished through them with its MAC
verified.
