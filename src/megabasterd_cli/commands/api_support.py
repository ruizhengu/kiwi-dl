"""One MegaAPIClient factory for every command module.

Command sites differ only in whether a static ``--proxy`` dict, a pre-built
pool, or a custom user agent applies.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..core.api import MegaAPIClient
from ..proxy.runtime import effective_pool

# Sentinel, because `None` is itself a meaningful pool value: an explicit
# `--proxy` deliberately suppresses the rotating pool, and that must not be
# confused with "the caller didn't say, derive it from cfg".
_FROM_CONFIG: Any = object()


def api_for(
    cfg: Config,
    *,
    proxies: dict[str, str] | None = None,
    proxy_pool: Any = _FROM_CONFIG,
    user_agent: str | None = None,
) -> MegaAPIClient:
    """Build a MegaAPIClient that honours the user's smart-proxy settings.

    Left to the config, the pool carries API calls only under
    `force_smart_proxy`: an anonymous API call keeps working while the
    transfer quota is spent, so routing it through proxies only hands the link
    to more third parties (the chunks are what the pool is for). Pass
    ``proxy_pool`` explicitly when the caller already holds a pool, or when a
    static ``--proxy`` means there should be no pool at all.
    """
    if proxy_pool is _FROM_CONFIG:
        proxy_pool = effective_pool(cfg) if cfg.force_smart_proxy else None
    return MegaAPIClient(
        timeout=cfg.timeout_seconds,
        proxies=proxies,
        proxy_pool=proxy_pool,
        force_proxy=cfg.force_smart_proxy,
        user_agent=user_agent,
    )
