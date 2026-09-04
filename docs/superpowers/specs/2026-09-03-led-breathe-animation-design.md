# LED Breathe Animation — Design

**Date:** 2026-09-03
**Status:** Approved, pending implementation

## Problem

The WS2812B quality strip renders once per poll cycle and then sits perfectly
still until the next one. A ping poll is 30 s apart; a speedtest is 30 min.
A static strip is indistinguishable from a crashed service, a wedged
scheduler, or a Pi that has quietly powered off. There is no at-a-glance
signal that the monitor is still running.

## Goal

Add a gentle, uniform "breathe" to the strip so that motion means *the
service is alive and polling* — and its absence means something is wrong.

Explicit non-goal: the animation does **not** encode severity. Every slot
breathes identically regardless of its score. Colour already carries
quality (`leds/quality.py`); adding a second visual channel for the same
information would be redundant and would make a red slot harder to read.

## Behaviour

- All slots — the ranked monitor slots and the optional `overall` slot —
  breathe in unison, at one rate, with one depth.
- Default: brightness swings between 70% and 100% of the configured
  `leds.brightness`, over a 4-second period.
- The cycle starts at full brightness, dips to the floor at the half-period,
  and returns. It is continuous at the wrap point, so the clock can be
  restarted without a visible jump.
- Colour is unaffected: only brightness is modulated, so hue stays readable
  at every point in the cycle.
- Disabling the animation restores exactly the current behaviour — one
  `show()` per poll, then stillness.

## Configuration

Added to `DEFAULT_CONFIG["leds"]` in `config.py` and mirrored in
`config.example.yaml`:

```yaml
leds:
  animation:
    enabled: true
    period_seconds: 4.0   # full breathe cycle
    min_brightness: 0.7   # floor, as a fraction of leds.brightness
    fps: 25
```

`min_brightness` is a *fraction of* `leds.brightness`, not an absolute
value, so lowering the strip's overall brightness scales the breathe with it
rather than inverting the relationship.

## Components

### `leds/animation.py` (new)

Pure functions only — no hardware, no I/O, no threads — so they are unit
testable on any machine, matching the existing convention in
`leds/quality.py`.

```python
def breathe_factor(elapsed: float, period: float, floor: float) -> float:
```

Raised cosine:

```
floor + (1 - floor) * 0.5 * (1 + cos(2 * pi * elapsed / period))
```

- `elapsed = 0` → `1.0`
- `elapsed = period / 2` → `floor`
- `elapsed = period` → `1.0`

Continuous and differentiable at the wrap, so there is no visible snap.

Guards:
- `period <= 0` → return `1.0` (animation degenerates to static rather than
  dividing by zero).
- `floor` is clamped into `[0.0, 1.0]`; a `floor` of `1.0` yields a constant
  `1.0`.
- The return value is always within `[floor, 1.0]`.

### `leds/controller.py` (modified)

A daemon render thread owns the strip.

**State**, guarded by a `threading.Lock`:
- `_target: list[tuple[int, int, tuple[int, int, int]]]` — the painted slot
  ranges, `(start, end, rgb)`, as computed today by `_slot_ranges` and
  `quality_color`.
- A monotonically-increasing generation counter, so the thread can tell
  whether the pixels need repainting or only the brightness needs updating.

**`render_quality(scores, overall)`** stops touching hardware. It computes
the ranking, slot ranges and colours — all already pure — swaps `_target`
under the lock, bumps the generation, and returns. No `run_in_executor`
call. It keeps its `async def` signature so the call site in
`main.py` (the `await _led.render_quality(...)` in `run_monitor`) is
unchanged.

**The thread loop**, at `fps` frames per second against a monotonic clock:
1. Under the lock, read `_target` and the generation.
2. If the generation changed since the last frame, repaint every pixel.
   Otherwise skip — the colours only change once per poll.
3. `setBrightness(round(base_brightness * breathe_factor(...)))`.
4. `show()`.
5. Sleep to the next frame deadline (computed from a fixed start time, so
   the frame rate does not drift).

Per-frame cost in the steady state is one brightness write plus one
`show()`, not 16 pixel writes.

**Lifecycle:**
- The thread is started lazily on the first `render_quality()` call, so
  nothing spins before the first poll and the strip does not breathe black.
- If `animation.enabled` is false, no thread is started and
  `render_quality()` falls back to today's single `run_in_executor` render.
- If `_strip is None` — dev machine, or `rpi_ws281x` missing — no thread is
  started and everything no-ops exactly as today.
- `blackout()` sets the stop event and joins the thread with a short timeout
  **before** painting off. Without the join, the animator would repaint over
  the shutdown blackout that `main.py` performs on SIGINT/SIGTERM.

### `main.py` (unchanged)

No call-site changes. `render_quality` keeps its signature and its await;
the awaited work simply becomes a lock-guarded state swap instead of an
executor round-trip.

### Dashboard (`web/templates/index.html`)

One CSS keyframe animation on `.quality-swatch`: `opacity` 1 → 0.7 → 1 over
4 s, `ease-in-out`, infinite. Matches the hardware defaults so the compact
strip preview looks like the real strip.

Wrapped in `@media (prefers-reduced-motion: reduce)` to disable it for users
who have asked their OS for less motion.

No JavaScript, no timer, and no change to `/api/quality` — the preview stays
a plain readout of the API's colours.

## Architectural deviation

`CLAUDE.md` currently states that blocking LED work goes through
`loop.run_in_executor`. This design replaces that for the LED render path
with a thread that owns the strip.

Rationale: at 25 fps, the executor route means ~25 task submissions per
second, forever, on a Pi 3B that is simultaneously serving uvicorn and SSE
clients. It also makes frame timing hostage to executor contention — a
30-minute speedtest occupying the default executor would visibly stutter the
breathe. A dedicated thread costs one lock and an explicit join, and in
exchange the asyncio loop is untouched and the frame timing is steady.

The `CLAUDE.md` convention text is updated as part of this change so the
documented rule matches the code.

## Error handling

- A failure inside the render thread is logged once and the thread exits
  cleanly rather than spinning on a repeating exception; the service keeps
  running without LEDs, consistent with how `_init_hardware` already handles
  a missing strip.
- `blackout()`'s join uses a short timeout so a wedged render thread cannot
  block shutdown.

## Testing

**`tests/test_animation.py` (new)** — pure, no hardware:
- `breathe_factor` at `t = 0`, `period / 2`, and `period`.
- Floor clamping: `floor` outside `[0, 1]`, and `floor == 1.0` giving a
  constant.
- Degenerate `period <= 0` returns `1.0`.
- A sweep across several periods asserting the result never leaves
  `[floor, 1.0]` and that the value at `t` equals the value at `t + period`.

**`tests/test_led_controller.py` (extended)** — with a fake strip object:
- Animation disabled: `render_quality` still results in exactly one `show()`,
  and no thread is started.
- Animation enabled: the thread starts on first render and issues multiple
  `show()` calls over a short wall-clock window.
- A second `render_quality` with different scores repaints the pixels.
- `blackout()` joins the thread and leaves every pixel at `(0, 0, 0)`, with
  no further writes after it returns.

Existing `tests/test_led_controller.py`, `tests/test_led_wiring.py` and
`tests/test_quality.py` must continue to pass unchanged.
