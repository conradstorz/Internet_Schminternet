"""LAN gateway detection for the ping monitor's local leg.

The kernel default route is the wrong answer inside Docker: in the
``schminternet`` container it is the bridge (e.g. ``172.25.0.1``), which is
always 0% loss. Instead we walk TTL-limited pings toward a public address and
take the **last private (RFC1918) hop before the first public hop** — on
hpz440's container TTL 1 is the bridge, TTL 2 the LAN router (the answer),
TTL 3 public; on a Pi on the LAN, TTL 1 is the answer directly.

Detection runs once per process (first ping poll) and is cached here.
Linux only: on any other OS ``"auto"`` resolves to ``None`` without spawning a
subprocess. See docs/superpowers/specs/2026-10-13-fault-localisation-design.md.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import platform
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# RFC1918 only. 100.64.0.0/10 (CGNAT) and everything else belong to the ISP.
_PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

_FALLBACK_PROBE = "1.1.1.1"
_MAX_TTL = 4

# "From 192.168.86.1 icmp_seq=1 Time to live exceeded"
# "From _gateway (172.25.0.1) icmp_seq=1 Time to live exceeded"
_TTL_EXCEEDED_RE = re.compile(
    r"^From\s+(\S+)(?:\s+\(([^)]+)\))?.*Time to live exceeded", re.IGNORECASE | re.MULTILINE
)
# "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=25.6 ms"
# "64 bytes from one.one.one.one (1.1.1.1): icmp_seq=1 ..."
_ECHO_REPLY_RE = re.compile(
    r"bytes from\s+([^\s:]+)(?:\s+\(([^)]+)\))?:", re.IGNORECASE
)

# Module-level cache: detection runs at most once per process.
_resolved = False
_gateway: Optional[str] = None


def reset() -> None:
    """Forget the cached detection result (tests)."""
    global _resolved, _gateway
    _resolved = False
    _gateway = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _parse_ttl_reply(output: str) -> Optional[str]:
    """Return the address that answered a single TTL-limited ping, or None.

    The answerer is either an intermediate router ("Time to live exceeded")
    or the destination itself (an echo reply). A bracketed numeric address is
    preferred over a resolved name.
    """
    if not output:
        return None
    m = _TTL_EXCEEDED_RE.search(output) or _ECHO_REPLY_RE.search(output)
    if not m:
        return None
    return m.group(2) or m.group(1)


def _is_private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETS)


def _choose_gateway(hops: list[Optional[str]]) -> Optional[str]:
    """Last private hop before the first public (or silent / unparseable) hop."""
    gateway: Optional[str] = None
    for hop in hops:
        if hop is None or not _is_private(hop):
            break
        gateway = hop
    return gateway


def _probe_target(targets: list[str]) -> str:
    """First configured ping target that is a literal IP, else 1.1.1.1."""
    for target in targets or []:
        try:
            ipaddress.ip_address(target)
        except ValueError:
            continue
        return target
    return _FALLBACK_PROBE


# ---------------------------------------------------------------------------
# Subprocess-backed helpers (Linux, run in an executor)
# ---------------------------------------------------------------------------

def _ping_once(args: list[str]) -> str:
    try:
        return subprocess.run(
            ["ping", "-n", *args], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _walk_sync(target: str, max_ttl: int = _MAX_TTL) -> list[Optional[str]]:
    """TTL-limited pings toward *target* for TTL 1..max_ttl.

    Stops at the first silent hop (recorded as None) or when the destination
    itself answers.
    """
    hops: list[Optional[str]] = []
    for ttl in range(1, max_ttl + 1):
        hop = _parse_ttl_reply(_ping_once(["-c", "1", "-W", "1", "-t", str(ttl), target]))
        hops.append(hop)
        if hop is None or hop == target:
            break
    return hops


def _confirm_sync(addr: str) -> bool:
    """True if *addr* answers at least one ordinary echo request."""
    return _ECHO_REPLY_RE.search(_ping_once(["-c", "3", "-W", "1", addr])) is not None


def _detect_sync(probe: str) -> Optional[str]:
    hops = _walk_sync(probe)
    found = _choose_gateway(hops)
    if found is None:
        logger.warning(
            "LAN gateway auto-detect found no private hop toward %s (hops: %s); "
            "gateway ping leg disabled",
            probe, hops,
        )
        return None
    if not _confirm_sync(found):
        logger.warning(
            "LAN gateway %s does not answer ping (ICMP ignored?); gateway ping leg disabled",
            found,
        )
        return None
    logger.info("LAN gateway detected: %s", found)
    return found


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def resolve(config: dict) -> Optional[str]:
    """Return the gateway address to ping, or None when the leg is off.

    ``monitors.ping.gateway``: ``"auto"`` (default), an explicit address, or
    ``None``/``False`` to disable. Auto-detection runs once and is cached for
    the process lifetime, including a negative result.
    """
    global _resolved, _gateway

    setting = config.get("monitors", {}).get("ping", {}).get("gateway", "auto")
    if not setting:
        return None
    if str(setting).lower() != "auto":
        return str(setting)
    if _resolved:
        return _gateway

    if platform.system() != "Linux":
        logger.debug("LAN gateway auto-detect is Linux-only; gateway ping leg off")
        found = None
    else:
        targets = config.get("monitors", {}).get("ping", {}).get("targets", [])
        loop = asyncio.get_running_loop()
        found = await loop.run_in_executor(None, _detect_sync, _probe_target(targets))

    _gateway = found
    _resolved = True
    return found
