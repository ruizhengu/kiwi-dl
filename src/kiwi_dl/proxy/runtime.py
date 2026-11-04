"""Runtime helpers for assembling the active SmartProxyPool.

Every CLI command that needs to honour the user's smart-proxy settings goes
through `effective_pool(cfg)` (or its `effective_pool_for_cmd(cfg, explicit_proxy)`
sibling for the download/stream paths that also accept a manual `--proxy`).

The active pool is the union of:

1. The persisted pool in `<data_dir>/proxies.json` (managed by `kiwi-dl proxy
   add / import`).
2. Comma- or whitespace-separated URLs from the `smart_proxy_url` config key.
3. The proxy lists in the same file (`kiwi-dl proxy add-list`), fetched when the
   pool is built and kept fresh by a `ListRefresher` for the rest of the command.

Adding proxies or lists is what turns them on. If there are none, or
`smart_proxy_mode` is "off", the helper returns None - callers should treat
that as "no smart-proxy routing". A
malformed `proxies.json` raises ProxyPoolCorruptionError instead: "no routing"
must be something the user chose, never something a broken file decided.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ..config import Config, data_dir
from ..utils.corruption import preserve_corrupt_file
from .lists import ListRefresher, validate_list
from .smart_proxy import SmartProxyPool

log = logging.getLogger(__name__)

_SPLIT_RE = re.compile(r"[\s,;]+")


class ProxyPoolCorruptionError(Exception):
    """The persisted proxy pool file is malformed and was preserved untouched.

    Every command that honours smart-proxy settings loads this file, so an
    unvalidated payload used to escape as an AttributeError/TypeError from the
    middle of the loader (`'list' object has no attribute 'get'`). Failing with
    one typed, actionable error keeps every command reporting the real
    problem, and mutations stay blocked so a corrupt pool is never silently
    replaced by an empty one.
    """


def _pool_path() -> Path:
    return data_dir() / "proxies.json"


def _corrupt(path: Path, reason: str, raw: bytes | None) -> ProxyPoolCorruptionError:
    """Preserve the unusable bytes (when we have them) and describe the fix.

    Same contract as ConfigStore/QueueManager: the original file is never
    touched, and the message only claims a backup that was really written.
    """
    backup = preserve_corrupt_file(path, raw) if raw is not None else None
    if backup is not None:
        hint = f"A backup was saved as {backup.name}; "
    else:
        hint = "A backup could NOT be written; "
    return ProxyPoolCorruptionError(
        f"The proxy pool file {path.name} is corrupt and was left untouched: {reason}. "
        f"{hint}move the file aside, then re-add proxies with `kiwi-dl proxy add`."
    )


def _load_persisted_pool() -> SmartProxyPool:
    """Load `<data_dir>/proxies.json`, validating its shape.

    Raises ProxyPoolCorruptionError rather than guessing at a payload that is
    not `{"proxies": [<url str>, ...], "lists": [{"url": ..., "scheme": ...}]}`
    ("lists" optional): an unreadable or malformed pool is
    silently the same as "no proxies", which would send traffic direct that the
    user meant to route through a proxy.
    """
    path = _pool_path()
    if not path.exists():
        return SmartProxyPool()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _corrupt(path, f"unreadable ({type(exc).__name__})", None) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _corrupt(path, f"not valid UTF-8 JSON ({type(exc).__name__})", raw) from exc
    if not isinstance(data, dict):
        raise _corrupt(path, f"root is {type(data).__name__}, expected an object", raw)
    proxies = data.get("proxies", [])
    if not isinstance(proxies, list) or not all(isinstance(url, str) for url in proxies):
        raise _corrupt(path, '"proxies" must be a list of URL strings', raw)
    lists = data.get("lists", [])
    if not isinstance(lists, list):
        raise _corrupt(path, '"lists" must be a list', raw)
    pool = SmartProxyPool(proxies)
    for item in lists:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise _corrupt(path, 'each of "lists" must be {"url": ..., "scheme": ...}', raw)
        try:
            pool.list_sources.append(validate_list(item["url"], item.get("scheme", "http")))
        except ValueError as exc:
            raise _corrupt(path, f"a proxy list entry is unusable ({exc})", raw) from exc
    return pool


def pool_payload(pool: SmartProxyPool) -> str:
    """What `proxies.json` holds: the user's own entries and the list URLs,
    never the entries fetched from a list."""
    return json.dumps(
        {
            "proxies": [e.url for e in pool.list() if not e.source],
            "lists": [{"url": s.url, "scheme": s.scheme} for s in pool.list_sources],
        },
        indent=2,
    )


def _urls_from_config(cfg: Config) -> list[str]:
    """Split smart_proxy_url into individual URLs.

    Accepts comma, semicolon, or whitespace separators so users can paste a
    flat proxy list straight into the config.
    """
    raw = cfg.smart_proxy_url
    if not raw:
        return []
    return [u for u in (s.strip() for s in _SPLIT_RE.split(raw)) if u]


def effective_pool(cfg: Config) -> SmartProxyPool | None:
    """Return the SmartProxyPool the command should use, or None.

    The pool merges the persisted on-disk pool with anything listed in
    `smart_proxy_url`, and with the entries of every proxy list, fetched here.
    Returns None if `smart_proxy_mode` is "off" OR there are no proxies and no
    lists. A pool with lists is returned even when the first fetch found
    nothing: its refresher tries again once a download wants a proxy.
    """
    if cfg.smart_proxy_mode == "off":
        return None
    pool = _load_persisted_pool()
    for url in _urls_from_config(cfg):
        pool.add(url)
    if pool.list_sources:
        attach_list_refresher(pool, strict=cfg.force_smart_proxy).refresh_now()
    if not pool.list() and not pool.list_sources:
        return None
    return pool


def attach_list_refresher(pool: SmartProxyPool, *, strict: bool) -> ListRefresher:
    """Give `pool` a refresher for its lists, fetching them the way policy allows.

    Lists are fetched direct: it is not MEGA traffic. Under `force_smart_proxy`
    nothing may go direct, so the fetch goes through one of the user's own
    proxies, and is skipped (with a warning) when they have none.
    """
    proxies: dict[str, str] | None = None
    if strict:
        own = next((e for e in pool.list() if not e.source), None)
        proxies = {"http": own.url, "https": own.url} if own else None
    refresher = ListRefresher(pool, pool.list_sources, proxies=proxies)
    if strict and proxies is None:
        log.warning(
            "force_smart_proxy is on and there is no proxy of your own to fetch the "
            "proxy lists through; they are not fetched"
        )
        refresher.sources = []
    pool.refresher = refresher
    return refresher


def effective_pool_for_cmd(
    cfg: Config,
    explicit_proxy: str | None,
) -> SmartProxyPool | None:
    """Same as `effective_pool` but skipped when the user passed `--proxy`.

    Use this in commands where a manual --proxy flag should take precedence
    over the auto-rotating pool (`kiwi-dl download --proxy ...`).
    """
    if explicit_proxy:
        return None
    return effective_pool(cfg)
