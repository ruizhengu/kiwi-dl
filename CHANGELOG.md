# Changelog

## 0.1.0 - unreleased

kiwi-dl starts from MegaBasterd-CLI 1.2.0 (KiaroSama/MegaBasterd-CLI at
`c4559ec`), cut down to downloading public MEGA links. The history before that
point is in the [upstream changelog](https://github.com/KiaroSama/MegaBasterd-CLI/blob/main/CHANGELOG.md).

### Added

- `mg-dl URL [-o DIR]`: the download command at the top level. `kiwi-dl`
  holds `download`, `info`, `proxy` and `config`.
- SmartProxy for downloads, after MegaBasterd's: direct first; a chunk answered
  with HTTP 509 sends every download of the run through your proxies for
  `smart_proxy_window_seconds` (cut short to MEGA's own reset time and on a
  local network change), then direct again. Each connection keeps its proxy
  until it fails; a proxy that errors is skipped by everyone for
  `smart_proxy_ban_seconds`, a timeout or 429 only moves that one connection,
  and an exhausted pool falls back to direct. `smart_proxy_mode` picks
  `on-509` (default), `always` or `off`.
- `quota_budget_seconds`: how long a run may wait for the quota when no proxy
  can take over (default 0: stop at once with the reset time). Exit code 3
  when only the quota stopped the run; `--json` records carry
  `quota_resets_in_seconds`.
- A file that fails its final MAC check re-downloads only the chunks that came
  through proxies, once, and skips those proxies. The resume state records a
  hash of each proxy's address, never its URL or password.
- SOCKS proxies (`requests[socks]`).

### Changed

- Config, proxies and logs live in `~/Library/Application Support/kiwi-dl` and
  `~/Library/Logs/kiwi-dl` on macOS (XDG directories on Linux), not in a
  `User/` folder wherever the command ran. Downloads default to the current
  folder.
- Proxies you add are used; there is no separate `smart_proxy_enabled` switch.
- MEGA API calls stay direct unless `force_smart_proxy` is on.

### Fixed

- Folders shared by their owner listed as empty and could not be downloaded:
  a node's key field can hold several `handle:key` items, and only the first
  was read.
- Ctrl-C kept downloading the rest of the file for minutes, then exited 1. It
  now stops within a second, keeps every finished chunk, and exits 130.
- HTTP 509 was treated as an expired link: seven pointless retries, then a
  generic failure two minutes later with no reset time.

### Removed

- Uploads, accounts and the credential vault, cloud file commands, streaming,
  the queue, watch, split/merge/thumbnail.
- MegaCrypter, DLC, ELC and `mega://enc` links.
- `proxy fetch` and its bundled public proxy lists, and `proxy serve`.
- The PowerShell launcher, `Run.ps1` and the `mb` commands.
