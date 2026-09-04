"""Brightness animation curves for the LED quality strip.

Pure functions only — no hardware, no I/O, no threads — so they run
identically whether or not `rpi_ws281x` is installed, and are unit-testable
on any machine. Same contract as `leds/quality.py`.

The strip renders once per poll cycle and is then still until the next one
(30 s for ping, 30 min for speedtest). A static strip looks exactly like a
crashed service, so `breathe_factor` provides a gentle brightness swell that
makes "alive" visible at a glance. It deliberately carries no quality
information — colour already does that.
"""

from __future__ import annotations

import math


def breathe_factor(elapsed: float, period: float, floor: float) -> float:
    """Brightness multiplier for a "breathe" cycle at time `elapsed`.

    A raised cosine between `floor` and 1.0:
      elapsed = 0          -> 1.0   (full brightness)
      elapsed = period / 2 -> floor (the dimmest point)
      elapsed = period     -> 1.0   (back to full)

    The curve is continuous and flat-sloped at both ends, so the cycle wraps
    without a visible snap and the clock can be restarted at any time.

    `floor` is clamped into [0.0, 1.0]; a floor of 1.0 yields a constant 1.0.
    A non-positive `period` degenerates to a constant 1.0 rather than
    dividing by zero, so a misconfigured period disables the animation
    instead of crashing the render thread.
    """
    floor = max(0.0, min(1.0, floor))
    if period <= 0:
        return 1.0
    phase = 2.0 * math.pi * elapsed / period
    swing = 0.5 * (1.0 + math.cos(phase))  # 1.0 at phase 0, 0.0 at phase pi
    return floor + (1.0 - floor) * swing
