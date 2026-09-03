"""Monitor-level tests for the DNS resolution timing monitor.

Covers query randomization (to defeat resolver caching) and the handling of
NXDOMAIN / NoAnswer as successful, timed round-trips rather than failures.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import dns.exception
import dns.resolver

import monitors.dns as dns_monitor


def _dns_cfg(**overrides) -> dict:
    cfg = {
        "servers": ["8.8.8.8"],
        "test_domain": "example.com",
        "thresholds": {},
    }
    cfg.update(overrides)
    return {"monitors": {"dns": cfg}}


def _patch_resolver(side_effect=None, return_value=None):
    """Patch dns.asyncresolver.Resolver, capturing the queried names."""
    queried_names = []

    async def fake_resolve(qname, *_a, **_kw):
        queried_names.append(str(qname))
        if side_effect is not None:
            raise side_effect
        return return_value if return_value is not None else MagicMock()

    patcher = patch("dns.asyncresolver.Resolver")
    MockResolver = patcher.start()
    inst = MagicMock()
    inst.resolve = fake_resolve
    MockResolver.return_value = inst
    return patcher, queried_names


async def test_randomize_query_true_uses_different_names_each_poll():
    """Two consecutive polls query different names, both suffixed with the domain."""
    patcher, queried_names = _patch_resolver()
    try:
        await dns_monitor.run(_dns_cfg(randomize_query=True))
        await dns_monitor.run(_dns_cfg(randomize_query=True))
    finally:
        patcher.stop()

    assert len(queried_names) == 2
    first, second = queried_names
    assert first != second
    assert first.rstrip(".").endswith("example.com")
    assert second.rstrip(".").endswith("example.com")


async def test_randomize_query_false_queries_domain_verbatim():
    """With randomize_query: false, the configured domain is queried as-is."""
    patcher, queried_names = _patch_resolver()
    try:
        await dns_monitor.run(_dns_cfg(randomize_query=False))
    finally:
        patcher.stop()

    assert len(queried_names) == 1
    assert queried_names[0].rstrip(".") == "example.com"


async def test_randomize_query_defaults_to_true():
    """randomize_query defaults to true when not specified."""
    patcher, queried_names = _patch_resolver()
    try:
        await dns_monitor.run(_dns_cfg())
    finally:
        patcher.stop()

    assert queried_names[0].rstrip(".") != "example.com"
    assert queried_names[0].rstrip(".").endswith("example.com")


async def test_nxdomain_is_a_successful_timed_result():
    """NXDOMAIN means the resolver answered — treat it as a normal result."""
    patcher, _ = _patch_resolver(side_effect=dns.resolver.NXDOMAIN())
    try:
        results = await dns_monitor.run(_dns_cfg(randomize_query=True))
    finally:
        patcher.stop()

    assert len(results) == 1
    result = results[0]
    assert result.status != "down"
    assert result.value >= 0.0


async def test_noanswer_is_a_successful_timed_result():
    """NoAnswer means the resolver answered — treat it as a normal result."""
    patcher, _ = _patch_resolver(side_effect=dns.resolver.NoAnswer())
    try:
        results = await dns_monitor.run(_dns_cfg(randomize_query=False))
    finally:
        patcher.stop()

    assert len(results) == 1
    result = results[0]
    assert result.status != "down"
    assert result.value >= 0.0


async def test_nxdomain_for_verbatim_query_is_still_a_success():
    """randomize_query: false + NXDOMAIN still counts as a successful round trip."""
    patcher, _ = _patch_resolver(side_effect=dns.resolver.NXDOMAIN())
    try:
        results = await dns_monitor.run(_dns_cfg(randomize_query=False))
    finally:
        patcher.stop()

    assert results[0].status != "down"


async def test_timeout_is_still_down():
    """A genuine timeout must still mark the monitor down, with the error in message."""
    patcher, _ = _patch_resolver(side_effect=dns.exception.Timeout())
    try:
        results = await dns_monitor.run(_dns_cfg())
    finally:
        patcher.stop()

    assert len(results) == 1
    assert results[0].status == "down"
    assert results[0].value == -1.0
    assert results[0].message != ""


async def test_no_nameservers_is_still_down():
    """A 'no nameservers reachable' error must still mark the monitor down."""
    patcher, _ = _patch_resolver(side_effect=dns.resolver.NoNameservers())
    try:
        results = await dns_monitor.run(_dns_cfg())
    finally:
        patcher.stop()

    assert len(results) == 1
    assert results[0].status == "down"
    assert results[0].value == -1.0
    assert results[0].message != ""


def test_generated_label_is_dns_legal():
    """The random label is short (<=63 chars) and made of lowercase letters/digits."""
    for _ in range(50):
        label = dns_monitor._random_label()
        assert 1 <= len(label) <= 63
        assert re.fullmatch(r"[a-z0-9]+", label), f"illegal label: {label!r}"
