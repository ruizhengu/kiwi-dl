"""Bounded retry policy for MEGA API requests."""

from __future__ import annotations

import json as _json

import pytest
import requests

from kiwi_dl.core.api import MegaAPIClient
from kiwi_dl.core.errors import RateLimitError


@pytest.fixture(autouse=True)
def _no_backoff_sleeps(monkeypatch):
    """Exercise the retry policy, not wall-clock backoff."""
    from kiwi_dl.core import api as api_module

    monkeypatch.setattr(api_module, "_retry_delay", lambda attempt: 0.0)


class _Recorder:
    """Stands in for requests.Session, counting what actually goes out."""

    def __init__(self, behavior):
        self.sent: list = []
        self._behavior = behavior
        self.headers = {}
        self.proxies = {}

    def post(self, url, json=None, timeout=None, headers=None, proxies=None, stream=False):
        self.sent.append(json)
        return self._behavior(len(self.sent))

    def close(self):
        return None


class _Response:
    status_code = 200
    headers: dict = {}
    encoding = "utf-8"

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=None):
        yield _json.dumps(self._payload).encode()

    def close(self):
        return None


def _client(behavior) -> MegaAPIClient:
    client = MegaAPIClient(timeout=1)
    client._session = _Recorder(behavior)
    return client


READS = {
    "download_url": {"a": "g", "g": 1, "p": "id"},
    "listing": {"a": "f", "c": 1},
}


@pytest.mark.parametrize("command", READS.values(), ids=list(READS))
def test_requests_retry_on_timeout(command):
    def behavior(attempt):
        if attempt < 3:
            raise requests.ReadTimeout("flaky network")
        return _Response([{"ok": True}])

    client = _client(behavior)
    assert client.request(command) == {"ok": True}
    assert len(client._session.sent) == 3


@pytest.mark.parametrize("command", READS.values(), ids=list(READS))
def test_requests_retry_on_connection_error(command):
    def behavior(attempt):
        if attempt < 2:
            raise requests.ConnectionError("dropped")
        return _Response([{"ok": True}])

    client = _client(behavior)
    assert client.request(command) == {"ok": True}
    assert len(client._session.sent) == 2


def test_rate_limit_is_retried_and_bounded():
    def behavior(attempt):
        return _Response(-4)

    client = _client(behavior)
    with pytest.raises(RateLimitError):
        client.request({"a": "g", "g": 1, "p": "id"})
    assert len(client._session.sent) == 5


def test_retries_remain_bounded():
    def behavior(attempt):
        raise requests.ReadTimeout("always down")

    client = _client(behavior)
    with pytest.raises(requests.ReadTimeout):
        client.request({"a": "g", "g": 1, "p": "id"})
    assert len(client._session.sent) == 5
