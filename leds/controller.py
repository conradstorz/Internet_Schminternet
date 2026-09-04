"""WS2812B LED strip controller.

Wraps rpi_ws281x.PixelStrip to render internet-quality as a rank-sorted
colour strip:
  - Every scored monitor (see leds/quality.py) gets a contiguous slot, sized
    as evenly as the LED count allows.
  - Slots are re-sorted on every update, greenest (best score) nearest the
    top of the strip.
  - An optional "overall" slot sits at the bottom end, showing the combined
    score's colour, and never participates in sorting.
  - If rpi_ws281x is not available (e.g. dev machine) the controller silently
    no-ops — the rest of the service still runs normally.

Hardware note (Pi 3B):
  GPIO18 (PWM0) is the default data pin.  The onboard 3.5 mm audio uses PWM,
  so if you need the audio jack add ``dtparam=audio=off`` to /boot/config.txt
  and use a USB audio adapter instead.  GPIO12 / GPIO13 / GPIO21 are alternatives
  that avoid the audio PWM conflict.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from leds.quality import quality_color

logger = logging.getLogger(__name__)

_OFF = (0, 0, 0)


def _partition_sizes(total: int, n_slots: int) -> list[int]:
    """Split `total` into `n_slots` near-equal, non-negative sizes summing to
    `total`, distributing any remainder one-at-a-time to the earliest slots
    so every LED is covered rather than left dark."""
    if n_slots <= 0:
        return []
    base, remainder = divmod(total, n_slots)
    return [base + 1 if i < remainder else base for i in range(n_slots)]


def _slot_ranges(count: int, n_slots: int, orientation: str) -> list[tuple[int, int]]:
    """Contiguous, inclusive (start, end) LED index ranges for `n_slots`
    logical slots ordered top-to-bottom (slot 0 = topmost), on a strip of
    `count` LEDs.

    `orientation` ("top_down" or "bottom_up") decides which physical end is
    "top" without changing slot order or sizes — it mirrors the whole
    strip end-for-end so the owner never has to rewire for mounting
    direction.
    """
    sizes = _partition_sizes(count, n_slots)
    ranges: list[tuple[int, int]] = []
    pos = 0
    for size in sizes:
        if size <= 0:
            ranges.append((pos, pos - 1))  # empty slot: more slots than LEDs
            continue
        ranges.append((pos, pos + size - 1))
        pos += size

    if orientation == "bottom_up":
        ranges = [(count - 1 - end, count - 1 - start) for start, end in ranges]

    return ranges


class LEDController:
    def __init__(self, config: dict) -> None:
        self._enabled: bool = config.get("enabled", False)
        self._count: int = config.get("count", 16)
        self._orientation: str = config.get("orientation", "top_down")
        self._overall: bool = config.get("overall", True)
        self._strip = None
        self._Color = None

        if self._enabled:
            self._init_hardware(config)

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_hardware(self, config: dict) -> None:
        try:
            from rpi_ws281x import Color, PixelStrip  # type: ignore[import]

            self._Color = Color
            pin = config.get("pin", 18)
            brightness = int(config.get("brightness", 0.4) * 255)
            self._strip = PixelStrip(self._count, pin, brightness=brightness)
            self._strip.begin()
            self._set_all_sync(_OFF)
            logger.info(
                "LED strip ready: %d LEDs on GPIO%d (brightness %d/255)",
                self._count, pin, brightness,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LED strip unavailable (hardware not present or rpi_ws281x not "
                "installed) — running without LEDs. Detail: %s",
                exc,
            )
            self._strip = None

    # ------------------------------------------------------------------
    # Synchronous helpers (called from executor threads)
    # ------------------------------------------------------------------

    def _set_all_sync(self, color: tuple[int, int, int]) -> None:
        if self._strip is None:
            return
        c = self._Color(*color)
        for i in range(self._count):
            self._strip.setPixelColor(i, c)
        self._strip.show()

    def _paint_range(self, start: int, end: int, color: tuple[int, int, int]) -> None:
        c = self._Color(*color)
        for i in range(start, end + 1):
            self._strip.setPixelColor(i, c)

    def _render_quality_sync(
        self,
        ranking: list[tuple[str, tuple[int, int, int]]],
        overall_color: Optional[tuple[int, int, int]],
    ) -> None:
        if self._strip is None:
            return

        n_slots = len(ranking) + (1 if overall_color is not None else 0)
        if n_slots == 0:
            return
        ranges = _slot_ranges(self._count, n_slots, self._orientation)

        for i, (_monitor, color) in enumerate(ranking):
            start, end = ranges[i]
            self._paint_range(start, end, color)

        if overall_color is not None:
            start, end = ranges[len(ranking)]
            self._paint_range(start, end, overall_color)

        self._strip.show()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def render_quality(self, scores: dict[str, float], overall: float) -> None:
        """Score every monitor's colour, sort best-first (stable on ties so
        equal scores don't flicker order between polls), and paint the
        strip — best nearest the top, worst nearest the bottom, with the
        optional overall slot fixed at the bottom end."""
        ranking = sorted(scores.items(), key=lambda kv: -kv[1])
        colored = [(name, quality_color(score)) for name, score in ranking]
        overall_color = quality_color(overall) if self._overall else None

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._render_quality_sync, colored, overall_color)

    async def set_all(self, color: tuple[int, int, int]) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_all_sync, color)

    async def blackout(self) -> None:
        await self.set_all(_OFF)
