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
import threading
import time
from typing import Optional

from leds.animation import breathe_factor
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

        anim = config.get("animation", {})
        self._anim_enabled: bool = anim.get("enabled", False)
        self._anim_period: float = float(anim.get("period_seconds", 4.0))
        self._anim_floor: float = float(anim.get("min_brightness", 0.7))
        self._anim_fps: float = float(anim.get("fps", 25))
        self._base_brightness: int = int(config.get("brightness", 0.4) * 255)

        # Serialises every strip access: render_quality paints from an
        # executor thread while the animator shows from its own.
        self._strip_lock = threading.Lock()
        self._anim_stop = threading.Event()
        self._anim_thread: Optional[threading.Thread] = None

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
            brightness = self._base_brightness
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
        with self._strip_lock:
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

        with self._strip_lock:
            for i, (_monitor, color) in enumerate(ranking):
                start, end = ranges[i]
                self._paint_range(start, end, color)

            if overall_color is not None:
                start, end = ranges[len(ranking)]
                self._paint_range(start, end, overall_color)

            self._strip.show()

    # ------------------------------------------------------------------
    # Breathe animation
    # ------------------------------------------------------------------

    def _animation_loop(self) -> None:
        """Modulate strip brightness on a raised-cosine curve until stopped.

        Runs on its own daemon thread rather than through
        `loop.run_in_executor` (the convention for the per-poll render):
        at 25 fps the executor route would mean ~25 task submissions per
        second forever on a Pi 3B that is also serving uvicorn and SSE, and
        would make frame timing hostage to a 30-minute speedtest holding the
        executor. The pixel buffer is untouched — only brightness changes —
        so a frame is one `setBrightness` plus one `show()`.
        """
        interval = 1.0 / self._anim_fps if self._anim_fps > 0 else 0.04
        start = time.monotonic()
        frame = 0
        try:
            while not self._anim_stop.is_set():
                factor = breathe_factor(
                    time.monotonic() - start, self._anim_period, self._anim_floor
                )
                with self._strip_lock:
                    if self._strip is None:
                        return
                    self._strip.setBrightness(round(self._base_brightness * factor))
                    self._strip.show()

                # Sleep to an absolute deadline derived from `start` so the
                # frame rate does not drift with per-frame work.
                frame += 1
                delay = (start + frame * interval) - time.monotonic()
                if delay > 0:
                    self._anim_stop.wait(delay)
                else:
                    # Fell behind (slow show, scheduler hiccup): resync to
                    # now rather than sprinting to catch up.
                    frame = int((time.monotonic() - start) / interval) + 1
        except Exception as exc:  # noqa: BLE001
            # Log once and exit; the service keeps running without the
            # animation rather than spinning on a repeating error.
            logger.warning("LED animation stopped after an error: %s", exc)

    def _start_animation(self) -> None:
        """Start the breathe thread if it is wanted and not already running.

        Called lazily from the first `render_quality`, so nothing spins
        before the first poll and the strip never breathes black.
        """
        if not self._anim_enabled or self._strip is None:
            return
        if self._anim_thread is not None and self._anim_thread.is_alive():
            return
        self._anim_stop.clear()
        self._anim_thread = threading.Thread(
            target=self._animation_loop, name="led-breathe", daemon=True
        )
        self._anim_thread.start()
        logger.info(
            "LED breathe animation started: %.1f s period, %.0f%%-100%% at %.0f fps",
            self._anim_period, self._anim_floor * 100, self._anim_fps,
        )

    def _stop_animation(self) -> None:
        """Signal the breathe thread and wait briefly for it to exit.

        Must complete before the shutdown blackout, or the animator would
        `show()` over it. The join timeout keeps a wedged thread from
        blocking service shutdown.
        """
        self._anim_stop.set()
        thread = self._anim_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                logger.warning("LED animation thread did not stop within 1 s")

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
        self._start_animation()

    async def set_all(self, color: tuple[int, int, int]) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._set_all_sync, color)

    async def blackout(self) -> None:
        """Stop the animation and turn every LED off.

        Order matters: the animator must be joined first, or its next frame
        would `show()` the strip back on after the blackout.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._stop_animation)
        await self.set_all(_OFF)
