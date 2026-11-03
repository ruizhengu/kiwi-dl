"""Tests for the proxy/runtime helper that merges persisted pool + config."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiwi_dl.config import Config
from kiwi_dl.proxy import runtime
from kiwi_dl.proxy.smart_proxy import SmartProxyPool


def _set_data_dir(tmp_path: Path):
    """Return a context manager that redirects proxy/runtime's data dir."""
    fake = tmp_path / "data"
    fake.mkdir()
    return patch.object(runtime, "data_dir", lambda: fake)


def test_effective_pool_returns_none_when_mode_is_off(tmp_path: Path) -> None:
    cfg = Config(smart_proxy_mode="off", smart_proxy_url="http://1.2.3.4:8080")
    with _set_data_dir(tmp_path):
        assert runtime.effective_pool(cfg) is None


def test_effective_pool_returns_none_when_empty(tmp_path: Path) -> None:
    cfg = Config(smart_proxy_url=None)
    with _set_data_dir(tmp_path):
        assert runtime.effective_pool(cfg) is None


def test_effective_pool_merges_config_urls(tmp_path: Path) -> None:
    cfg = Config(
        smart_proxy_url="http://1.2.3.4:8080, socks5://5.6.7.8:1080",
    )
    with _set_data_dir(tmp_path):
        pool = runtime.effective_pool(cfg)
        assert isinstance(pool, SmartProxyPool)
        urls = {e.url for e in pool.list()}
        assert urls == {"http://1.2.3.4:8080", "socks5://5.6.7.8:1080"}


def test_effective_pool_merges_persisted_and_config(tmp_path: Path) -> None:
    cfg = Config(
        smart_proxy_url="http://config-proxy:1234",
    )
    with _set_data_dir(tmp_path):
        (tmp_path / "data" / "proxies.json").write_text(
            json.dumps({"proxies": ["http://disk-proxy:9999"]})
        )
        pool = runtime.effective_pool(cfg)
        assert pool is not None
        urls = {e.url for e in pool.list()}
        assert urls == {"http://disk-proxy:9999", "http://config-proxy:1234"}


def test_urls_from_config_separators() -> None:
    cfg = Config(smart_proxy_url="a, b;c\nd  e")
    assert runtime._urls_from_config(cfg) == ["a", "b", "c", "d", "e"]


def test_effective_pool_for_cmd_skipped_when_explicit_proxy(tmp_path: Path) -> None:
    cfg = Config(
        smart_proxy_url="http://config-proxy:1234",
    )
    with _set_data_dir(tmp_path):
        # When --proxy was passed, the smart pool MUST be ignored
        assert runtime.effective_pool_for_cmd(cfg, "http://manual:8080") is None
        # When --proxy is None, behave like effective_pool
        pool = runtime.effective_pool_for_cmd(cfg, None)
        assert pool is not None
        assert {e.url for e in pool.list()} == {"http://config-proxy:1234"}


def test_effective_pool_deduplicates(tmp_path: Path) -> None:
    """A URL present in both the persisted pool and the config should only
    appear once in the merged pool."""
    cfg = Config(
        smart_proxy_url="http://duplicate:1111",
    )
    with _set_data_dir(tmp_path):
        (tmp_path / "data" / "proxies.json").write_text(
            json.dumps({"proxies": ["http://duplicate:1111"]})
        )
        pool = runtime.effective_pool(cfg)
        assert pool is not None
        urls = [e.url for e in pool.list()]
        assert urls == ["http://duplicate:1111"]


def test_adding_proxies_is_what_turns_them_on(tmp_path: Path) -> None:
    """No separate on-switch: a configured proxy is a proxy the user wants used."""
    cfg = Config(smart_proxy_url="http://1.2.3.4:8080")
    with _set_data_dir(tmp_path):
        pool = runtime.effective_pool(cfg)
    assert pool is not None and [e.url for e in pool.list()] == ["http://1.2.3.4:8080"]


def test_a_config_from_the_old_switch_loads_with_a_warning(tmp_path: Path, caplog) -> None:
    from kiwi_dl.config import ConfigStore

    path = tmp_path / "config.json"
    path.write_text('{"smart_proxy_enabled": false, "max_workers": 4}', encoding="utf-8")
    with caplog.at_level("WARNING"):
        cfg = ConfigStore(path).load()
    assert cfg.max_workers == 4
    assert not hasattr(cfg, "smart_proxy_enabled")
    assert "smart_proxy_enabled" in caplog.text


@pytest.mark.parametrize("scheme", ["socks5", "socks5h", "socks4"])
def test_socks_proxies_are_usable(scheme) -> None:
    """Without PySocks, `requests` raises InvalidSchema for every socks:// URL
    before a byte is sent, and the chunk router can only ban such a proxy."""
    from requests.adapters import HTTPAdapter

    assert HTTPAdapter().proxy_manager_for(f"{scheme}://127.0.0.1:9") is not None
