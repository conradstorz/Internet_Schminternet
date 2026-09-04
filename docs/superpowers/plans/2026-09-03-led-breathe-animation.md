# LED Breathe Animation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the WS2812B quality strip breathe gently between polls, so that visible motion means "the monitoring service is alive" and stillness means something is wrong.

**Architecture:** A new pure module `leds/animation.py` computes a raised-cosine brightness factor. `leds/controller.py` gains a daemon thread that, at ~25 fps, calls `setBrightness(base * factor)` + `show()` on the strip. `render_quality()` is otherwise unchanged — it still paints the pixels via `run_in_executor` once per poll. A single lock serialises all strip access between the two threads. The dashboard's strip preview gets a matching CSS-only breathe.

**Tech Stack:** Python 3.13, asyncio, `threading`, `rpi_ws281x` (Pi-only, absent on dev machines), pytest with `asyncio_mode = auto`, Jinja2 templates with inline CSS.

**Design spec:** `docs/superpowers/specs/2026-09-03-led-breathe-animation-design.md`

## Global Constraints

- Run all Python through `uv`: `uv run pytest`, `uv run python main.py`. Never `pip install`, never activate a venv.
- Never chain shell commands with `&&` or `;` — one logical command per tool call. The `git add` / `git commit` pairs below are written as separate blocks for that reason.
- `rpi_ws281x` is NOT installed on the development machine. Every test must pass without it. `LEDController` already falls back to a silent no-op when the import fails; preserve that.
- Existing tests must keep passing unchanged: `tests/test_led_controller.py`, `tests/test_led_wiring.py`, `tests/test_quality.py`. In particular `TestRenderQuality` wires a fake strip post-construction and asserts pixel colours **synchronously** after `await ctl.render_quality(...)` — so `render_quality` must keep painting pixels itself.
- `leds/animation.py` is pure: no hardware, no I/O, no threads, no imports beyond the standard library `math`. This mirrors `leds/quality.py`.
- Config defaults live in `DEFAULT_CONFIG` in `config.py` and are mirrored, with comments, in `config.example.yaml`. Config is deep-merged, so user config holds overrides only.
- Exact default values, verbatim from the spec: `enabled: true`, `period_seconds: 4.0`, `min_brightness: 0.7`, `fps: 25`.
- `min_brightness` is a **fraction of** `leds.brightness`, not an absolute 0-255 value.
- Timing in the animator uses `time.monotonic()`, never `time.time()`.

---

### Task 1: `breathe_factor` — the pure brightness curve

**Files:**
- Create: `leds/animation.py`
- Test: `tests/test_animation.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `leds.animation.breathe_factor(elapsed: float, period: float, floor: float) -> float`. Returns a multiplier in `[floor, 1.0]`. `elapsed=0` → `1.0`; `elapsed=period/2` → `floor`; `elapsed=period` → `1.0`. Task 3 imports this.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_animation.py`:

```python
"""Tests for leds/animation.py — the pure breathe brightness curve.

No hardware, no threads: breathe_factor is a plain function of elapsed
time, so it is fully testable on any machine.
"""

from __future__ import annotations

import pytest

from leds.animation import breathe_factor


class TestBreatheFactorKeyPoints:
    def test_starts_at_full_brightness(self):
        assert breathe_factor(0.0, period=4.0, floor=0.7) == pytest.approx(1.0)

    def test_dips_to_floor_at_half_period(self):
        assert breathe_factor(2.0, period=4.0, floor=0.7) == pytest.approx(0.7)

    def test_returns_to_full_at_end_of_period(self):
        assert breathe_factor(4.0, period=4.0, floor=0.7) == pytest.approx(1.0)

    def test_quarter_period_sits_midway(self):
        # Raised cosine: cos(pi/2) == 0 -> exactly halfway between floor and 1.
        assert breathe_factor(1.0, period=4.0, floor=0.7) == pytest.approx(0.85)


class TestBreatheFactorIsPeriodic:
    def test_value_repeats_every_period(self):
        for t in (0.0, 0.37, 1.1, 2.5, 3.9):
            assert breathe_factor(t, 4.0, 0.7) == pytest.approx(
                breathe_factor(t + 4.0, 4.0, 0.7)
            )

    def test_never_leaves_the_floor_to_one_band(self):
        # Sweep several periods at fine resolution.
        for i in range(1000):
            t = i * 0.013
            value = breathe_factor(t, 4.0, 0.7)
            assert 0.7 - 1e-9 <= value <= 1.0 + 1e-9


class TestBreatheFactorGuards:
    def test_zero_period_degenerates_to_static(self):
        assert breathe_factor(1.23, period=0.0, floor=0.7) == pytest.approx(1.0)

    def test_negative_period_degenerates_to_static(self):
        assert breathe_factor(1.23, period=-4.0, floor=0.7) == pytest.approx(1.0)

    def test_floor_of_one_is_constant(self):
        for t in (0.0, 1.0, 2.0, 3.0):
            assert breathe_factor(t, 4.0, 1.0) == pytest.approx(1.0)

    def test_floor_above_one_is_clamped(self):
        assert breathe_factor(2.0, 4.0, 1.8) == pytest.approx(1.0)

    def test_negative_floor_is_clamped_to_zero(self):
        assert breathe_factor(2.0, 4.0, -0.5) == pytest.approx(0.0)

    def test_negative_elapsed_is_still_in_band(self):
        # A clock that has not been reset yet must not produce a wild value.
        assert 0.7 - 1e-9 <= breathe_factor(-1.0, 4.0, 0.7) <= 1.0 + 1e-9
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_animation.py -v`

Expected: collection error — `ModuleNotFoundError: No module named 'leds.animation'`.

- [ ] **Step 3: Write the implementation**

Create `leds/animation.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_animation.py -v`

Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add leds/animation.py tests/test_animation.py
```

```bash
git commit -m "feat: add pure breathe_factor brightness curve for the LED strip"
```

---

### Task 2: Animation config defaults

**Files:**
- Modify: `config.py` (the `"leds"` block of `DEFAULT_CONFIG`, around lines 61-74)
- Modify: `config.example.yaml` (the `leds:` block, around lines 90-112)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `config["leds"]["animation"]` — a dict with keys `enabled` (bool), `period_seconds` (float), `min_brightness` (float), `fps` (int). Task 3 reads this dict.

**Before writing the test:** run `grep -n "^def \|^class " config.py` and `grep -n "import\|def test" tests/test_config.py | head -20` to confirm the loader's exact name and how existing tests call it. Adapt the call below to match; keep the assertions identical.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
class TestLedAnimationDefaults:
    def test_animation_defaults_are_present(self):
        from config import DEFAULT_CONFIG

        animation = DEFAULT_CONFIG["leds"]["animation"]
        assert animation["enabled"] is True
        assert animation["period_seconds"] == 4.0
        assert animation["min_brightness"] == 0.7
        assert animation["fps"] == 25

    def test_user_config_can_override_one_animation_key(self, tmp_path):
        # Deep merge: overriding period_seconds must leave the other
        # animation keys at their defaults rather than replacing the dict.
        from config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            "leds:\n"
            "  animation:\n"
            "    period_seconds: 10.0\n",
            encoding="utf-8",
        )
        config = load_config(str(path))
        animation = config["leds"]["animation"]
        assert animation["period_seconds"] == 10.0
        assert animation["min_brightness"] == 0.7
        assert animation["fps"] == 25
        assert animation["enabled"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k animation -v`

Expected: FAIL with `KeyError: 'animation'`.

- [ ] **Step 3: Add the defaults**

In `config.py`, inside `DEFAULT_CONFIG["leds"]`, add after the `"overall": True,` line:

```python
        # Gentle "breathe" so the strip visibly moves between polls — a
        # still strip is indistinguishable from a crashed service. It
        # encodes liveness only, never severity: colour already carries
        # quality. See leds/animation.py.
        "animation": {
            "enabled": True,
            "period_seconds": 4.0,   # one full swell-and-dip
            "min_brightness": 0.7,   # dimmest point, as a fraction of `brightness`
            "fps": 25,
        },
```

In `config.example.yaml`, add at the end of the `leds:` block (after the `speedtest: 0.25` weight line, at the same two-space indent as `weights:`):

```yaml
  animation:
    # A gentle brightness "breathe" so the strip visibly moves between
    # polls. Between a ping poll (30 s) and a speedtest (30 min) the strip
    # would otherwise sit perfectly still, which looks exactly like a
    # crashed service or a powered-off Pi. The animation is a liveness
    # signal only — it is identical on every slot regardless of score,
    # because colour already tells you the quality.
    enabled: true
    period_seconds: 4.0      # seconds for one full swell-and-dip cycle
    min_brightness: 0.7      # dimmest point as a FRACTION of `brightness` above,
                             # not an absolute value — 0.7 means 70% of 0.4
    fps: 25                  # animation frames per second
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`

Expected: PASS, including the two new tests.

- [ ] **Step 5: Run the full suite to confirm nothing regressed**

Run: `uv run pytest tests/ -q`

Expected: all tests pass. The new `leds.animation` key is inert until Task 3 reads it.

- [ ] **Step 6: Commit**

```bash
git add config.py config.example.yaml tests/test_config.py
```

```bash
git commit -m "feat: add leds.animation config defaults"
```

---

### Task 3: The animation render thread

**Files:**
- Modify: `leds/controller.py`
- Test: `tests/test_led_controller.py`

**Interfaces:**
- Consumes: `leds.animation.breathe_factor(elapsed, period, floor) -> float` from Task 1; the `config["leds"]["animation"]` dict from Task 2.
- Produces: no new public API. `LEDController.render_quality`, `set_all` and `blackout` keep their existing signatures. Internally it gains `self._base_brightness: int`, `self._strip_lock: threading.Lock`, `self._anim_stop: threading.Event`, `self._anim_thread: Optional[threading.Thread]`, `self._start_animation() -> None` and `self._stop_animation() -> None`.

**Background the implementer needs:**
- `rpi_ws281x.PixelStrip.setBrightness(value)` takes a 0-255 int and applies on the next `show()`. Re-showing an unchanged pixel buffer at a new brightness is therefore the whole frame — no pixel writes needed.
- `render_quality` paints on an executor thread; the animator runs on its own thread. Both touch the same `PixelStrip`, so every strip access must hold `self._strip_lock`.
- The existing tests substitute a `_FakeStrip` **after** construction, so the animator must read `self._strip` at call time rather than capturing it at construction.

- [ ] **Step 1: Extend the fake strip in the existing tests**

In `tests/test_led_controller.py`, replace the whole `_FakeStrip` class (around line 105) with:

```python
class _FakeStrip:
    def __init__(self, count: int) -> None:
        self.pixels: list[tuple[int, int, int]] = [(0, 0, 0)] * count
        self.shown = False
        self.show_count = 0
        self.brightness_values: list[int] = []

    def setPixelColor(self, index: int, color: tuple[int, int, int]) -> None:
        self.pixels[index] = color

    def setBrightness(self, value: int) -> None:
        self.brightness_values.append(value)

    def show(self) -> None:
        self.shown = True
        self.show_count += 1
```

- [ ] **Step 2: Write the failing tests**

Append to the end of `tests/test_led_controller.py`:

```python
# ---------------------------------------------------------------------------
# Breathe animation — the daemon thread that modulates brightness between
# polls. render_quality still paints the pixels itself; the thread only
# calls setBrightness + show.
#
# Test configs use a deliberately short period and high fps so a handful of
# frames land inside a fraction of a second of wall clock.
# ---------------------------------------------------------------------------

import time

_ANIM_ON = {"enabled": True, "period_seconds": 0.2, "min_brightness": 0.5, "fps": 50}
_ANIM_OFF = {"enabled": False, "period_seconds": 4.0, "min_brightness": 0.7, "fps": 25}


class TestAnimationDisabled:
    async def test_no_thread_is_started(self):
        ctl = LEDController({"enabled": False, "count": 12, "animation": _ANIM_OFF})
        _wire_fake_strip(ctl)
        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        assert ctl._anim_thread is None
        await ctl.blackout()

    async def test_render_shows_exactly_once(self):
        ctl = LEDController({"enabled": False, "count": 12, "animation": _ANIM_OFF})
        strip = _wire_fake_strip(ctl)
        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        assert strip.show_count == 1
        assert strip.brightness_values == []
        await ctl.blackout()

    async def test_config_without_an_animation_key_behaves_as_before(self):
        # The pre-existing tests pass no "animation" key at all; that must
        # keep meaning "no animation", not "default on".
        ctl = LEDController({"enabled": False, "count": 12})
        strip = _wire_fake_strip(ctl)
        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        assert ctl._anim_thread is None
        assert strip.show_count == 1


class TestAnimationEnabled:
    async def test_thread_starts_lazily_on_first_render(self):
        ctl = LEDController({"enabled": False, "count": 12, "animation": _ANIM_ON})
        _wire_fake_strip(ctl)
        assert ctl._anim_thread is None  # nothing spins before the first poll

        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        assert ctl._anim_thread is not None
        assert ctl._anim_thread.is_alive()
        await ctl.blackout()

    async def test_second_render_does_not_start_a_second_thread(self):
        ctl = LEDController({"enabled": False, "count": 12, "animation": _ANIM_ON})
        _wire_fake_strip(ctl)
        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        first = ctl._anim_thread
        await ctl.render_quality({"ping": 0.5}, overall=0.5)
        assert ctl._anim_thread is first
        await ctl.blackout()

    async def test_brightness_is_modulated_within_the_configured_band(self):
        ctl = LEDController({"enabled": False, "count": 12, "animation": _ANIM_ON})
        ctl._base_brightness = 200
        strip = _wire_fake_strip(ctl)

        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        time.sleep(0.35)  # ~1.75 cycles at a 0.2 s period
        await ctl.blackout()

        values = strip.brightness_values
        assert len(values) > 5, f"expected many frames, got {values}"
        assert min(values) >= round(200 * 0.5)
        assert max(values) <= 200
        # It must actually move, not sit at one level.
        assert len(set(values)) > 1

    async def test_ranking_colours_are_still_painted_with_the_animator_running(self):
        ctl = LEDController({
            "enabled": False, "count": 12, "orientation": "top_down",
            "overall": False, "animation": _ANIM_ON,
        })
        strip = _wire_fake_strip(ctl)

        await ctl.render_quality({"ping": 0.9, "dns": 0.1, "http": 0.5}, overall=0.5)
        assert strip.pixels[0] == quality_color(0.9)
        assert strip.pixels[4] == quality_color(0.5)
        assert strip.pixels[8] == quality_color(0.1)
        await ctl.blackout()


class TestBlackoutStopsTheAnimator:
    async def test_blackout_joins_the_thread_and_leaves_pixels_off(self):
        ctl = LEDController({"enabled": False, "count": 12, "animation": _ANIM_ON})
        strip = _wire_fake_strip(ctl)
        await ctl.render_quality({"ping": 0.9}, overall=0.9)
        time.sleep(0.05)

        await ctl.blackout()

        assert not ctl._anim_thread.is_alive()
        assert all(p == (0, 0, 0) for p in strip.pixels)

        # No further writes after blackout returns — the animator must not
        # repaint over the shutdown blackout.
        settled = strip.show_count
        time.sleep(0.1)
        assert strip.show_count == settled
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_led_controller.py -v`

Expected: the new tests FAIL with `AttributeError: 'LEDController' object has no attribute '_anim_thread'`. The pre-existing `TestSlotRanges`, `TestLEDControllerNoOp` and `TestRenderQuality` classes must still PASS.

- [ ] **Step 4: Implement the animator**

In `leds/controller.py`:

**(a)** Extend the imports at the top of the module:

```python
import asyncio
import logging
import threading
import time
from typing import Optional

from leds.animation import breathe_factor
from leds.quality import quality_color
```

**(b)** In `__init__`, after `self._overall: bool = config.get("overall", True)`, add:

```python
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
```

The `_anim_enabled` default here is `False`, not `True`, on purpose: a config dict with no `animation` key (as in the pre-existing tests) must behave exactly as it does today. `DEFAULT_CONFIG` supplies the real `True` default for the running service.

**(c)** In `_init_hardware`, replace:

```python
            brightness = int(config.get("brightness", 0.4) * 255)
```

with:

```python
            brightness = self._base_brightness
```

**(d)** Put the lock around the two existing sync helpers' strip access. Replace `_set_all_sync` with:

```python
    def _set_all_sync(self, color: tuple[int, int, int]) -> None:
        if self._strip is None:
            return
        c = self._Color(*color)
        with self._strip_lock:
            for i in range(self._count):
                self._strip.setPixelColor(i, c)
            self._strip.show()
```

and in `_render_quality_sync`, replace everything after the `ranges = _slot_ranges(self._count, n_slots, self._orientation)` line with:

```python
        with self._strip_lock:
            for i, (_monitor, color) in enumerate(ranking):
                start, end = ranges[i]
                self._paint_range(start, end, color)

            if overall_color is not None:
                start, end = ranges[len(ranking)]
                self._paint_range(start, end, overall_color)

            self._strip.show()
```

`_paint_range` itself stays as-is — it is only ever called with the lock already held.

**(e)** Add the animator immediately after `_render_quality_sync`:

```python
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
```

**(f)** In `render_quality`, start the animator after the executor render. Replace the method's final two lines with:

```python
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._render_quality_sync, colored, overall_color)
        self._start_animation()
```

**(g)** Replace `blackout` with:

```python
    async def blackout(self) -> None:
        """Stop the animation and turn every LED off.

        Order matters: the animator must be joined first, or its next frame
        would `show()` the strip back on after the blackout.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._stop_animation)
        await self.set_all(_OFF)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_led_controller.py -v`

Expected: PASS — the pre-existing `TestSlotRanges`, `TestLEDControllerNoOp` and `TestRenderQuality` classes plus all the new animation tests.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q`

Expected: all tests pass, including `tests/test_led_wiring.py` and `tests/test_quality.py`.

- [ ] **Step 7: Commit**

```bash
git add leds/controller.py tests/test_led_controller.py
```

```bash
git commit -m "feat: breathe the LED strip between polls on a dedicated render thread"
```

---

### Task 4: Dashboard strip preview breathes to match

**Files:**
- Modify: `web/templates/index.html` (the `.quality-swatch` rule, around lines 109-112)
- Test: `tests/test_web_app.py`

**Interfaces:**
- Consumes: nothing — CSS only, no API change, no JavaScript.
- Produces: nothing consumed by later tasks.

**Before writing the test:** run `grep -n "fixture\|TestClient\|def client\|\.get(\"/\")" tests/test_web_app.py | head -20` to find the existing test client fixture's name and how the dashboard page is requested. Use that exact fixture and call style below; keep the assertions identical.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_app.py` (substituting the real client fixture name found above):

```python
class TestQualityStripPreviewBreathes:
    def test_swatches_carry_the_breathe_animation(self, client):
        html = client.get("/").text
        assert "@keyframes quality-breathe" in html
        assert "animation: quality-breathe 4s ease-in-out infinite" in html

    def test_breathe_respects_reduced_motion(self, client):
        html = client.get("/").text
        assert "prefers-reduced-motion: reduce" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_app.py -k breathe -v`

Expected: FAIL — `assert "@keyframes quality-breathe" in html`.

- [ ] **Step 3: Add the CSS**

In `web/templates/index.html`, replace the `.quality-swatch` rule:

```css
    .quality-swatch {
      width: 34px; height: 16px; border-radius: 0.3rem;
      border: 1px solid rgba(255,255,255,0.15);
    }
```

with:

```css
    .quality-swatch {
      width: 34px; height: 16px; border-radius: 0.3rem;
      border: 1px solid rgba(255,255,255,0.15);
      /* Matches the hardware strip's breathe (leds/animation.py): the same
         70%-100% swing over the same 4 s period, so the preview looks like
         the thing it previews. Liveness only — never severity. */
      animation: quality-breathe 4s ease-in-out infinite;
    }
    @keyframes quality-breathe {
      0%, 100% { opacity: 1; }
      50%      { opacity: 0.7; }
    }
    @media (prefers-reduced-motion: reduce) {
      .quality-swatch { animation: none; }
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_app.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/templates/index.html tests/test_web_app.py
```

```bash
git commit -m "feat: breathe the dashboard's LED strip preview to match the hardware"
```

---

### Task 5: Documentation

**Files:**
- Modify: `CLAUDE.md` (two bullets in the "Conventions that matter" list)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Update the executor convention bullet**

In `CLAUDE.md`, find the bullet beginning "Blocking work (ping subprocess, `speedtest-cli`, LED `show()`) goes through `loop.run_in_executor`…" and append this sentence to it:

> The one exception is the LED breathe animation, which runs on its own daemon thread (`_animation_loop` in `leds/controller.py`): at 25 fps the executor route would mean ~25 task submissions per second forever, competing with uvicorn and SSE on a Pi 3B, and would let a 30-minute speedtest holding the executor stutter the frame timing. The per-poll render still goes through the executor.

- [ ] **Step 2: Document the animation in the LED bullet**

In the same list, append to the end of the bullet that starts "LEDs are a rank-sorted quality strip, not fixed per-monitor segments":

> `leds/animation.py` adds a gentle brightness breathe (`leds.animation`, default on: 70%-100% over 4 s at 25 fps) so a still strip reads as "the service died" rather than "nothing has changed". It is a liveness signal only and is identical on every slot — colour already carries quality. A daemon thread calls `setBrightness` + `show()` on the unchanged pixel buffer; `render_quality()` still paints the pixels itself, and a `threading.Lock` in `LEDController` serialises the two. `blackout()` joins the thread before painting off, or the animator would show the strip back on after shutdown. Design spec: `docs/superpowers/specs/2026-09-03-led-breathe-animation-design.md`.

- [ ] **Step 3: Verify the full suite still passes**

Run: `uv run pytest tests/ -q`

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
```

```bash
git commit -m "docs: record the LED breathe animation and its threading exception"
```

---

## Manual verification (on the Pi, not the dev machine)

The dev machine has no `rpi_ws281x`, so the hardware animation is only observable after deploying. On the Pi:

```bash
sudo systemctl restart internet-schminternet
```

```bash
sudo journalctl -u internet-schminternet -n 50
```

Expect a `LED breathe animation started: 4.0 s period, 70%-100% at 25 fps` line shortly after startup (it appears after the first ping poll, not at boot), and a strip that visibly swells and dips roughly every 4 seconds without changing colour. `systemctl stop` must leave the strip fully dark and keep it dark.
