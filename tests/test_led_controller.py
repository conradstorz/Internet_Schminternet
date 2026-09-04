"""Tests for leds/controller.py — rank-sorted slot allocation and rendering.

No rpi_ws281x on this machine: LEDController must construct and operate as a
no-op regardless (enabled defaults False, and even enabled=True falls back
to no-op when the hardware import fails), so none of this touches hardware.
"""

from __future__ import annotations

from leds.controller import LEDController, _slot_ranges
from leds.quality import quality_color


# ---------------------------------------------------------------------------
# Pure slot-allocation maths
# ---------------------------------------------------------------------------

class TestSlotRanges:
    def test_evenly_divisible(self):
        # 12 LEDs, 3 slots -> 4 each.
        assert _slot_ranges(12, 3, "top_down") == [(0, 3), (4, 7), (8, 11)]

    def test_remainder_distributed_not_left_dark(self):
        # 16 LEDs, 5 slots (4 monitors + overall) -> 16 // 5 = 3 r1.
        ranges = _slot_ranges(16, 5, "top_down")
        assert len(ranges) == 5
        # Every LED covered exactly once.
        covered = []
        for start, end in ranges:
            covered.extend(range(start, end + 1))
        assert sorted(covered) == list(range(16))

    def test_remainder_goes_to_earliest_slots(self):
        # 10 LEDs, 3 slots -> base 3, remainder 1 -> sizes [4, 3, 3].
        assert _slot_ranges(10, 3, "top_down") == [(0, 3), (4, 6), (7, 9)]

    def test_bottom_up_mirrors_top_down(self):
        top_down = _slot_ranges(12, 3, "top_down")
        bottom_up = _slot_ranges(12, 3, "bottom_up")
        # Same slot sizes, same logical order, but the physical index sets
        # are mirrored end-for-end.
        assert [e - s for s, e in top_down] == [e - s for s, e in bottom_up]
        mirrored = [(11 - e, 11 - s) for s, e in top_down]
        assert bottom_up == mirrored

    def test_bottom_up_first_slot_sits_at_the_high_end(self):
        ranges = _slot_ranges(12, 3, "bottom_up")
        # Slot 0 (the best-ranked monitor / topmost logical position) should
        # occupy the highest physical indices when "top" = last LED.
        assert ranges[0][1] == 11

    def test_single_slot_covers_everything(self):
        assert _slot_ranges(8, 1, "top_down") == [(0, 7)]

    def test_odd_led_count(self):
        ranges = _slot_ranges(7, 2, "top_down")
        covered = []
        for start, end in ranges:
            covered.extend(range(start, end + 1))
        assert sorted(covered) == list(range(7))


# ---------------------------------------------------------------------------
# LEDController construction / no-op behaviour
# ---------------------------------------------------------------------------

class TestLEDControllerNoOp:
    def test_disabled_controller_is_inert(self):
        ctl = LEDController({"enabled": False, "count": 16})
        assert ctl._strip is None

    def test_enabled_without_hardware_falls_back_to_noop(self):
        # rpi_ws281x is not installed on this machine — enabling must not raise.
        ctl = LEDController({"enabled": True, "count": 16})
        assert ctl._strip is None

    async def test_render_quality_is_a_no_op_without_hardware(self):
        ctl = LEDController({"enabled": False, "count": 16})
        # Must not raise even with no strip attached.
        await ctl.render_quality({"ping": 0.9, "dns": 0.5}, overall=0.7)

    async def test_set_all_and_blackout_still_work(self):
        ctl = LEDController({"enabled": False, "count": 16})
        await ctl.set_all((0, 0, 0))
        await ctl.blackout()


# ---------------------------------------------------------------------------
# render_quality's ranking/colour logic, verified against a fake strip.
#
# LEDController only talks to the strip through self._strip / self._Color,
# both set in __init__ when hardware init succeeds. We substitute fakes
# post-construction so the ranking/colour/orientation logic is exercised
# without needing rpi_ws281x.
# ---------------------------------------------------------------------------

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


def _fake_color(r: int, g: int, b: int) -> tuple[int, int, int]:
    return (r, g, b)


def _wire_fake_strip(ctl: LEDController) -> _FakeStrip:
    strip = _FakeStrip(ctl._count)
    ctl._strip = strip
    ctl._Color = _fake_color
    return strip


class TestRenderQuality:
    async def test_ranks_best_score_at_the_top_slot(self):
        ctl = LEDController({"enabled": False, "count": 12, "orientation": "top_down", "overall": False})
        strip = _wire_fake_strip(ctl)

        await ctl.render_quality({"ping": 0.9, "dns": 0.1, "http": 0.5}, overall=0.5)

        # 12 LEDs / 3 monitors -> 4 each: ping (best) at [0-3], http at
        # [4-7], dns (worst) at [8-11].
        assert strip.pixels[0] == quality_color(0.9)
        assert strip.pixels[4] == quality_color(0.5)
        assert strip.pixels[8] == quality_color(0.1)
        assert strip.shown is True

    async def test_overall_slot_sits_at_the_bottom_and_is_not_sorted(self):
        ctl = LEDController({"enabled": False, "count": 12, "orientation": "top_down", "overall": True})
        strip = _wire_fake_strip(ctl)

        await ctl.render_quality({"ping": 0.1, "dns": 0.9}, overall=0.5)

        # 12 LEDs / 3 slots (2 monitors + overall) -> 4 each.
        # dns (best) at top [0-3], ping (worst) next [4-7], overall last [8-11]
        # regardless of overall's own score relative to the monitors.
        assert strip.pixels[0] == quality_color(0.9)
        assert strip.pixels[4] == quality_color(0.1)
        assert strip.pixels[8] == quality_color(0.5)

    async def test_overall_disabled_is_excluded_entirely(self):
        ctl = LEDController({"enabled": False, "count": 12, "orientation": "top_down", "overall": False})
        strip = _wire_fake_strip(ctl)

        await ctl.render_quality({"ping": 0.1, "dns": 0.9}, overall=0.99)

        # Only 2 slots now -> 6 each, and overall's colour appears nowhere.
        assert strip.pixels[0] == quality_color(0.9)
        assert strip.pixels[6] == quality_color(0.1)
        assert quality_color(0.99) not in strip.pixels

    async def test_bottom_up_orientation_flips_physical_layout(self):
        ctl = LEDController({"enabled": False, "count": 12, "orientation": "bottom_up", "overall": False})
        strip = _wire_fake_strip(ctl)

        await ctl.render_quality({"ping": 0.9, "dns": 0.1}, overall=0.5)

        # "top" is now the highest index: best score (ping) should occupy
        # the high end of the strip instead of index 0.
        assert strip.pixels[11] == quality_color(0.9)
        assert strip.pixels[0] == quality_color(0.1)

    async def test_ties_sort_stably(self):
        ctl = LEDController({"enabled": False, "count": 8, "orientation": "top_down", "overall": False})
        strip = _wire_fake_strip(ctl)

        # dns and http tie; insertion order (ping, dns, http) must be
        # preserved for the tied pair rather than reordering between polls.
        await ctl.render_quality({"ping": 0.9, "dns": 0.5, "http": 0.5, "speedtest": 0.1}, overall=0.5)
        first_pass = list(strip.pixels)

        await ctl.render_quality({"ping": 0.9, "dns": 0.5, "http": 0.5, "speedtest": 0.1}, overall=0.5)
        second_pass = list(strip.pixels)

        assert first_pass == second_pass


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
