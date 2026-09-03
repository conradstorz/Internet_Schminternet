"""External IP change tracker.

Queries a public IP-echo service and compares the result against the last known
IP stored in the ``state`` table.  An IP change is most often a sign that the
ISP dropped and re-established the PPPoE/DHCP session.

Primary API: api.ipify.org  (returns bare IP text)
Fallback:    icanhazip.com  (returns bare IP text, Cloudflare-backed)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

import storage.db as db
from monitors.base import MonitorResult

logger = logging.getLogger(__name__)

_IP_PRIMARY = "https://api.ipify.org"
_IP_FALLBACK = "https://icanhazip.com"
_STATE_KEY = "external_ip"


async def run(config: dict) -> list[MonitorResult]:
    ts = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            current_ip: str
            try:
                resp = await client.get(_IP_PRIMARY)
                resp.raise_for_status()
                current_ip = resp.text.strip()
            except Exception:
                resp = await client.get(_IP_FALLBACK)
                resp.raise_for_status()
                current_ip = resp.text.strip()

        previous_ip = await db.get_state(_STATE_KEY)
        ip_changed = previous_ip is not None and previous_ip != current_ip

        if ip_changed or previous_ip is None:
            await db.set_state(_STATE_KEY, current_ip)

        if ip_changed:
            logger.warning("External IP changed: %s -> %s", previous_ip, current_ip)
            await db.insert_event(
                {
                    "timestamp": ts,
                    "event_type": "ip_change",
                    "monitor": "ip",
                    "description": f"External IP changed: {previous_ip} → {current_ip}",
                    "previous_status": previous_ip or "",
                    "new_status": current_ip,
                }
            )

        return [
            MonitorResult(
                monitor="ip",
                target="external",
                timestamp=ts,
                metric="ip_changed",
                value=1.0 if ip_changed else 0.0,
                status="degraded" if ip_changed else "ok",
                message=current_ip,
            )
        ]

    except Exception as exc:
        return [
            MonitorResult(
                monitor="ip",
                target="external",
                timestamp=ts,
                metric="ip_changed",
                value=-1.0,
                status="down",
                message=str(exc),
            )
        ]
