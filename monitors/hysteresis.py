"""Recovery hysteresis for a monitor's effective status — pure, no I/O.

A monitor's raw (worst-wins) status can flip every poll when the underlying
measurement is a coin toss (e.g. 5-probe ping loss at a true ~40% loss rate
lands on 40% or 60-80% by chance). `step()` damps that at the source:

* Worsening is immediate. A raw status worse than or equal to the effective
  status becomes the effective status at once, so a first ``down`` alert is
  never delayed.
* Improving needs confirmation. A raw status better than the effective status
  starts (or continues) a count of consecutive better polls; the effective
  status changes only once that count reaches ``recover_polls``, and then to
  the **worst** raw status seen across those confirming polls.
* Any poll that is not better resets the count.
* An ``unknown`` effective status (no history, e.g. after a restart) is
  replaced by the first raw status immediately.
* ``recover_polls`` of 0, 1, None, or anything unusable means no damping.

Raw ``unknown`` while the effective status is known has the lowest severity,
so it counts as a "better" poll and is damped like any improvement (no
monitor emits it today).

See docs/superpowers/specs/2026-10-13-status-flap-damping-design.md.
"""

from __future__ import annotations

from typing import Any, Optional

SEVERITY: dict[str, int] = {"unknown": 0, "ok": 1, "degraded": 2, "down": 3}

# Pending state with no confirmation in progress: (count, worst raw seen).
IDLE: tuple[int, Optional[str]] = (0, None)


def _severity(status: Optional[str]) -> int:
    return SEVERITY.get(status or "unknown", 0)


def _threshold(recover_polls: Any) -> int:
    """Coerce recover_polls to an int; anything unusable means 0 (no damping)."""
    try:
        return int(recover_polls or 0)
    except (TypeError, ValueError):
        return 0


def step(
    effective: str,
    pending_count: int,
    pending_worst: Optional[str],
    raw: str,
    recover_polls: Any,
) -> tuple[str, int, Optional[str]]:
    """Advance the hysteresis by one poll.

    Returns the new ``(effective, pending_count, pending_worst)``.
    """
    n = _threshold(recover_polls)
    if n <= 1 or effective == "unknown" or _severity(raw) >= _severity(effective):
        return raw, *IDLE

    count = pending_count + 1
    worst = raw if pending_worst is None or _severity(raw) > _severity(pending_worst) else pending_worst
    if count >= n:
        return worst, *IDLE
    return effective, count, worst
