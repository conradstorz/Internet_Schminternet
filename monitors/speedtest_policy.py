"""Adaptive-intensity policy for the speedtest monitor.

Pure functions, no I/O. The speedtest job in main.py calls these after every
run to decide whether the last result was good or poor and which rung of the
intensity ladder to use next. See
docs/superpowers/specs/2026-10-09-cloudflare-adaptive-speedtest-design.md.
"""

from __future__ import annotations

Verdict = str  # "good" | "poor"


def verdict(
    download_mbps: float,
    expected_mbps: float,
    degraded_ratio: float,
    history: list[float],
    avg_ratio: float,
    min_samples: int,
) -> Verdict:
    """Classify one run.

    poor when any of:
      - the run failed (download_mbps < 0)
      - download is below the configured degraded threshold
        (expected_mbps * degraded_ratio; skipped when expected_mbps <= 0)
      - download is below avg_ratio * mean(history), but only once history
        holds at least min_samples readings
    otherwise good.
    """
    if download_mbps < 0:
        return "poor"
    if expected_mbps > 0 and download_mbps < expected_mbps * degraded_ratio:
        return "poor"
    if len(history) >= min_samples:
        mean = sum(history) / len(history)
        if download_mbps < avg_ratio * mean:
            return "poor"
    return "good"


def next_level(
    level: int,
    verdict: Verdict,
    good_streak: int,
    calm_after: int,
    max_level: int,
) -> tuple[int, int]:
    """Return (new_level, new_good_streak).

    poor -> one rung up (capped), streak reset.
    good -> streak + 1; once it reaches calm_after, one rung down (floored
            at 0) and the streak resets.
    """
    if verdict == "poor":
        return min(level + 1, max_level), 0
    good_streak += 1
    if good_streak >= calm_after:
        return max(level - 1, 0), 0
    return level, good_streak
