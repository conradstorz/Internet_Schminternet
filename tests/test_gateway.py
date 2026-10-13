"""Tests for monitors/gateway.py — LAN gateway auto-detection.

The parse and selection functions are pure; ``resolve()`` is driven with the
subprocess-backed helpers (``_walk_sync``, ``_confirm_sync``) and the platform
check stubbed, so nothing here ever shells out.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from monitors import gateway
from monitors.gateway import _choose_gateway, _parse_ttl_reply, _probe_target

# ---------------------------------------------------------------------------
# _parse_ttl_reply
# ---------------------------------------------------------------------------

TTL_EXCEEDED_NUMERIC = """\
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
From 192.168.86.1 icmp_seq=1 Time to live exceeded

--- 1.1.1.1 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""

TTL_EXCEEDED_NAMED = """\
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
From _gateway (172.25.0.1) icmp_seq=1 Time to live exceeded

--- 1.1.1.1 ping statistics ---
1 packets transmitted, 0 received, +1 errors, 100% packet loss, time 0ms
"""

DESTINATION_REPLY = """\
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=25.6 ms

--- 1.1.1.1 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
"""

NO_REPLY = """\
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.

--- 1.1.1.1 ping statistics ---
1 packets transmitted, 0 received, 100% packet loss, time 0ms
"""


class TestParseTtlReply:
    def test_numeric_ttl_exceeded(self):
        assert _parse_ttl_reply(TTL_EXCEEDED_NUMERIC) == "192.168.86.1"

    def test_named_ttl_exceeded_uses_bracketed_address(self):
        assert _parse_ttl_reply(TTL_EXCEEDED_NAMED) == "172.25.0.1"

    def test_destination_reply_returns_destination(self):
        assert _parse_ttl_reply(DESTINATION_REPLY) == "1.1.1.1"

    def test_no_reply(self):
        assert _parse_ttl_reply(NO_REPLY) is None

    def test_empty(self):
        assert _parse_ttl_reply("") is None


# ---------------------------------------------------------------------------
# _choose_gateway
# ---------------------------------------------------------------------------

class TestChooseGateway:
    def test_docker_bridge_then_lan_router(self):
        # Measured live in the schminternet container on hpz440.
        hops = ["172.25.0.1", "192.168.86.1", "142.254.146.181"]
        assert _choose_gateway(hops) == "192.168.86.1"

    def test_pi_on_lan_first_hop_is_the_answer(self):
        assert _choose_gateway(["192.168.86.1", "142.254.146.181"]) == "192.168.86.1"

    def test_silent_first_hop(self):
        assert _choose_gateway([None, "192.168.86.1"]) is None

    def test_silent_hop_stops_walk_keeps_last_private(self):
        assert _choose_gateway(["192.168.1.1", None, "10.0.0.1"]) == "192.168.1.1"

    def test_cgnat_counts_as_public(self):
        assert _choose_gateway(["100.64.0.1"]) is None
        assert _choose_gateway(["10.0.0.1", "100.64.1.1"]) == "10.0.0.1"

    def test_all_rfc1918_ranges(self):
        assert _choose_gateway(["10.0.0.1", "172.16.5.1", "192.168.0.1", "8.8.8.8"]) == "192.168.0.1"

    def test_172_outside_slash12_is_public(self):
        assert _choose_gateway(["172.32.0.1"]) is None

    def test_public_first_hop(self):
        assert _choose_gateway(["1.1.1.1"]) is None

    def test_empty(self):
        assert _choose_gateway([]) is None

    def test_garbage_address_stops_walk(self):
        assert _choose_gateway(["192.168.1.1", "not-an-ip", "10.0.0.1"]) == "192.168.1.1"


# ---------------------------------------------------------------------------
# _probe_target
# ---------------------------------------------------------------------------

def test_probe_target_first_ip_target():
    assert _probe_target(["wikipedia.org", "9.9.9.9", "1.1.1.1"]) == "9.9.9.9"


def test_probe_target_falls_back_to_cloudflare():
    assert _probe_target(["wikipedia.org", "kernel.org"]) == "1.1.1.1"
    assert _probe_target([]) == "1.1.1.1"


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------

def _cfg(gw="auto", targets=("1.1.1.1",)) -> dict:
    return {"monitors": {"ping": {"targets": list(targets), "gateway": gw}}}


def _no_shell(*_a, **_kw):
    raise AssertionError("gateway detection must not shell out here")


@pytest.mark.parametrize("setting", [None, False, ""])
async def test_resolve_disabled_never_shells_out(setting):
    with patch.object(gateway, "_walk_sync", _no_shell), \
         patch.object(gateway, "_confirm_sync", _no_shell):
        assert await gateway.resolve(_cfg(setting)) is None


async def test_resolve_auto_off_on_non_linux():
    with patch.object(gateway.platform, "system", return_value="Windows"), \
         patch.object(gateway, "_walk_sync", _no_shell), \
         patch.object(gateway, "_confirm_sync", _no_shell):
        assert await gateway.resolve(_cfg()) is None


async def test_resolve_explicit_address_used_as_is():
    with patch.object(gateway, "_walk_sync", _no_shell), \
         patch.object(gateway, "_confirm_sync", _no_shell):
        assert await gateway.resolve(_cfg("192.168.1.254")) == "192.168.1.254"


async def test_resolve_auto_detects_once_and_caches(caplog):
    calls = []

    def walk(target, max_ttl=4):
        calls.append(target)
        return ["172.25.0.1", "192.168.86.1", "142.254.146.181"]

    with patch.object(gateway.platform, "system", return_value="Linux"), \
         patch.object(gateway, "_walk_sync", walk), \
         patch.object(gateway, "_confirm_sync", return_value=True), \
         caplog.at_level(logging.INFO, logger="monitors.gateway"):
        first = await gateway.resolve(_cfg(targets=("wikipedia.org", "9.9.9.9")))
        second = await gateway.resolve(_cfg(targets=("wikipedia.org", "9.9.9.9")))

    assert first == second == "192.168.86.1"
    assert calls == ["9.9.9.9"]
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "192.168.86.1" in r.getMessage()]
    assert len(infos) == 1


async def test_resolve_nothing_found_warns_once_and_stays_off(caplog):
    calls = []

    def walk(target, max_ttl=4):
        calls.append(target)
        return [None]

    with patch.object(gateway.platform, "system", return_value="Linux"), \
         patch.object(gateway, "_walk_sync", walk), \
         patch.object(gateway, "_confirm_sync", _no_shell), \
         caplog.at_level(logging.WARNING, logger="monitors.gateway"):
        assert await gateway.resolve(_cfg()) is None
        assert await gateway.resolve(_cfg()) is None

    assert len(calls) == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_resolve_unconfirmed_gateway_is_disabled(caplog):
    # Router answers TTL-exceeded but ignores echo requests to itself: pinging
    # it would read "down" forever, so the leg is switched off instead.
    with patch.object(gateway.platform, "system", return_value="Linux"), \
         patch.object(gateway, "_walk_sync", return_value=["192.168.86.1", "8.8.8.8"]), \
         patch.object(gateway, "_confirm_sync", return_value=False), \
         caplog.at_level(logging.WARNING, logger="monitors.gateway"):
        assert await gateway.resolve(_cfg()) is None
        assert await gateway.resolve(_cfg()) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "192.168.86.1" in warnings[0].getMessage()


async def test_reset_clears_cache():
    with patch.object(gateway.platform, "system", return_value="Linux"), \
         patch.object(gateway, "_walk_sync", return_value=["192.168.86.1", "8.8.8.8"]), \
         patch.object(gateway, "_confirm_sync", return_value=True):
        assert await gateway.resolve(_cfg()) == "192.168.86.1"
    gateway.reset()
    with patch.object(gateway.platform, "system", return_value="Windows"):
        assert await gateway.resolve(_cfg()) is None
