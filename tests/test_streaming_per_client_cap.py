"""Regression tests for the per-client half of the slowloris fix.

The global `max_connections` cap BOUNDS a slowloris but does not stop it: a
single hostile address can still take every slot in the pool by itself, so a
well-behaved second client is starved even though the server is "protected".
A per-remote-address cap is what keeps one peer from owning the whole pool.

Every network assertion is bounded by a hard client-side timeout, so a
regression FAILS instead of hanging CI.
"""

from __future__ import annotations

import http.client
import socket
import time

from megabasterd_cli.streaming.server import StreamingServer
from tests.test_streaming_hardening import HARD_TIMEOUT, _addr, _half_open, _status
from tests.test_streaming_hardening import _start as _start_server

# The address a second, different peer is reported under. It is a label, not a
# real source address: Linux routes the whole 127.0.0.0/8 to loopback, but macOS
# only configures 127.0.0.1, so binding 127.0.0.2 there fails with EADDRNOTAVAIL.
OTHER_IP = "127.0.0.2"

# Phase timings. The rule learned from a previous CI failure in this area: an
# assertion made "while X is held" must have a budget several times longer than
# the settle it waits out, or the timeout under test fires first and the test
# fails for the wrong reason. Both margins are asserted below, in the tests.
SETTLE = 0.5
HOLD_BUDGET = 5.0


def _start(**kwargs) -> StreamingServer:
    """`_start`, with a hook for reporting chosen connections under another address.

    The per-client cap keys on `client_address[0]` and nothing else, so changing
    that one value where the server accepts the connection is a different peer
    as far as the code under test can tell - over real sockets, on every OS.
    """
    server = _start_server(**kwargs)
    server.relabel = {}  # local port -> address to report it under
    accept = server.get_request

    def get_request():
        request, client_address = accept()
        ip = server.relabel.get(client_address[1])
        return request, (ip, client_address[1]) if ip else client_address

    server.get_request = get_request
    return server


def _status_from(server: StreamingServer, ip: str, method: str = "HEAD") -> int:
    """`_status`, but seen by the server as coming from `ip`."""
    host, port = _addr(server)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(HARD_TIMEOUT)
    # Bound before connecting, so the label is in place before the server can
    # accept the connection.
    sock.bind((host, 0))
    server.relabel[sock.getsockname()[1]] = ip
    sock.connect((host, port))
    conn = http.client.HTTPConnection(host, port, timeout=HARD_TIMEOUT)
    conn.sock = sock  # http.client only dials when it has no socket yet
    try:
        conn.request(method, "/")
        return conn.getresponse().status
    finally:
        conn.close()


def test_one_address_cannot_starve_another_client():
    """A hostile peer saturating its own cap must not deny a different peer."""
    assert HOLD_BUDGET > SETTLE * 3, "the hold assertion must not race the header timeout"
    assert HOLD_BUDGET < HARD_TIMEOUT, "the client must outlive the server-side budget"

    server = _start(max_connections=8, max_connections_per_client=2, header_timeout=HOLD_BUDGET)
    held = []
    try:
        held = [_half_open(server) for _ in range(2)]
        time.sleep(SETTLE)  # let the accept loop hand both to handler threads
        assert _status(server) == 503, "the per-client cap is not enforced"
        assert _status_from(server, OTHER_IP) == 200, "a hostile peer starved an innocent one"
    finally:
        for sock in held:
            sock.close()
        server.shutdown()
        server.server_close()


def test_the_global_cap_still_bounds_the_total_across_addresses():
    """The per-client cap is the inner bound; the global cap stays the outer one."""
    assert HOLD_BUDGET > SETTLE * 3, "the hold assertion must not race the header timeout"

    server = _start(max_connections=2, max_connections_per_client=2, header_timeout=HOLD_BUDGET)
    held = []
    try:
        held = [_half_open(server) for _ in range(2)]  # fills the global pool
        time.sleep(SETTLE)
        assert _status_from(server, OTHER_IP) == 503, "the global cap no longer applies"
    finally:
        for sock in held:
            sock.close()
        server.shutdown()
        server.server_close()


def test_per_client_counters_return_to_zero_so_a_client_is_never_locked_out():
    """A leaked counter permanently locks that address out - worse than the bug.

    Exercises every exit path that increments the counter: a served request, a
    refused one, and a connection reaped by the header timeout.
    """
    server = _start(max_connections=4, max_connections_per_client=1, header_timeout=3.0)
    try:
        assert _status(server) == 200  # served
        # Wait for the served request's counter to drain BEFORE opening the
        # stalled one. Without this the first socket's teardown can still hold
        # the single slot when _half_open connects, so the STALLED connection
        # takes the 503 and the measured request then finds the slot free - the
        # cap fires, just not on the connection being asserted about.
        deadline = time.monotonic() + HARD_TIMEOUT
        while time.monotonic() < deadline and server._peer_counts:
            time.sleep(0.02)
        assert server._peer_counts == {}, "the served request never released its slot"

        stalled = _half_open(server)  # will be reaped by the header timeout
        time.sleep(0.2)
        assert _status(server) == 503  # refused: must not leak a count either
        stalled.close()

        deadline = time.monotonic() + HARD_TIMEOUT
        while time.monotonic() < deadline and server._peer_counts:
            time.sleep(0.05)
        assert server._peer_counts == {}, "a per-client counter leaked"
        assert _status(server) == 200, "the client was locked out of its own cap"
    finally:
        server.shutdown()
        server.server_close()


def test_the_counter_map_does_not_grow_across_distinct_addresses():
    server = _start(max_connections=4, max_connections_per_client=2)
    try:
        for last in range(2, 8):
            assert _status_from(server, f"127.0.0.{last}") == 200
        deadline = time.monotonic() + HARD_TIMEOUT
        while time.monotonic() < deadline and server._peer_counts:
            time.sleep(0.05)
        assert server._peer_counts == {}, "the counter map grows with every distinct address"
    finally:
        server.shutdown()
        server.server_close()
