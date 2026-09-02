"""WS2812B LED strip controller.

Wraps rpi_ws281x.PixelStrip to provide a monitor-oriented API:
  - Each monitor owns a named segment (range of LED indices).
  - Status maps to a colour (green / amber / red / blue / dim-white).
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

logger = logging.getLogger(__name__)

# RGB colour values for each status
_COLORS: dict[str, tuple[int, int, int]] = {
    "ok":        (0,   150, 0),
    "degraded":  (200, 100, 0),
    "down":      (180, 0,   0),
    "measuring": (0,   0,   180),
    "unknown":   (20,  20,  20),
}
_OFF = (0, 0, 0)


class LEDController:
    def __init__(self, config: dict) -> None:
        self._enabled: bool = config.get("enabled", False)
        self._count: int = config.get("count", 16)
        self._segments: dict[str, list[int]] = config.get("segments", {})
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

    def _update_segment_sync(self, monitor: str, status: str) -> None:
        if self._strip is None:
            return
        segment = self._segments.get(monitor)
        if segment is None:
            return
        rgb = _COLORS.get(status, _COLORS["unknown"])
        c = self._Color(*rgb)
        start, end = segment[0], segment[1]
        for i in range(start, end + 1):
            self._strip.setPixelColor(i, c)
        self._strip.show()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def update_segment(self, monitor: str, status: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._update_segment_sync, monitor, status)

    async def set_all(self, color: tuple[int, int, int]) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_all_sync, color)

    async def blackout(self) -> None:
        await self.set_all(_OFF)
