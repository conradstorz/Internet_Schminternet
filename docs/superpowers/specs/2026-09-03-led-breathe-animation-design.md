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

The split of responsibility is deliberately narrow: **`render_quality` keeps
painting the pixels exactly as it does today**, and the animation thread
only modulates brightness. The thread therefore never needs to know the slot
colours, and there is no shared target state, no generation counter and no
repaint logic — just a lock around strip access.

**`render_quality(scores, overall)`** is unchanged in behaviour. It still
computes the ranking and slot ranges, paints every pixel and calls `show()`,
still via a single `run_in_executor` per poll — once every 30 s, which is
nothing. Its only addition is starting the animation thread lazily on the
first call.

**The animation thread**, a daemon started lazily, at `fps` frames per
second against `time.monotonic()`:

1. `factor = breathe_factor(now - start, period, floor)`
2. `strip.setBrightness(round(base_brightness * factor))`
3. `strip.show()`
4. Sleep to the next frame deadline, computed from a fixed start time so the
   frame rate does not drift.

`setBrightness` takes effect on the next `show()`, so re-showing the
unchanged pixel buffer at a new brightness is the entire frame. Per-frame
cost is one brightness write plus one `show()` — never 16 pixel writes.

**Locking.** A single `threading.Lock` guards every strip access: the
paint-and-show in `_render_quality_sync`, the animator's
set-brightness-and-show, and `_set_all_sync`. `render_quality` runs on an
executor thread and the animator on its own, so without it the two could
interleave inside `rpi_ws281x`. If a poll repaints while the animator is
mid-cycle, the next frame corrects the brightness within one frame period.

`base_brightness` — currently a local in `_init_hardware` — is stored on the
instance as `self._base_brightness` so the animator can scale it.

**Lifecycle:**
- Started lazily on the first `render_quality()` call, so nothing spins
  before the first poll and the strip does not breathe black.
- If `animation.enabled` is false, no thread is started; behaviour is
  byte-for-byte today's.
- If `_strip is None` — dev machine, or `rpi_ws281x` missing — no thread is
  started and everything no-ops exactly as today.
- `blackout()` sets the stop event and joins the thread with a short timeout
  **before** painting off. Without the join, the animator would repaint over
  the shutdown blackout that `main.py` performs on SIGINT/SIGTERM.

### `main.py` (unchanged)

No call-site changes at all. `render_quality` keeps its signature, its await
and its executor round-trip; `blackout()` keeps its call site in the
shutdown path.

### Dashboard (`web/templates/index.html`)

One CSS keyframe animation on `.quality-swatch`: `opacity` 1 → 0.7 → 1 over
4 s, `ease-in-out`, infinite. Matches the hardware defaults so the compact
strip preview looks like the real strip.

Wrapped in `@media (prefers-reduced-motion: reduce)` to disable it for users
who have asked their OS for less motion.

No JavaScript, no timer, and no change to `/api/quality` — the preview stays
a plain readout of the API's colours.

## Architectural deviation

`CLAUDE.md` states that blocking LED work goes through
`loop.run_in_executor`. The per-poll render still does. The 25 fps animation
loop does not — it runs on its own daemon thread.

Rationale: via the executor, the animation would mean ~25 task submissions
per second, forever, on a Pi 3B that is simultaneously serving uvicorn and
SSE clients. It would also make frame timing hostage to executor contention
— a 30-minute speedtest occupying the default executor would visibly stutter
the breathe. A dedicated thread costs one lock and an explicit join.

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
- Animation enabled: the thread starts on first render, and the fake strip
  records multiple `setBrightness` values over a short wall-clock window,
  all within `[round(base * floor), base]`.
- Animation enabled: `render_quality` still paints the correct slot colours,
  so every existing ranking assertion holds with the animator running.
- `blackout()` joins the thread and leaves every pixel at `(0, 0, 0)`, with
  no further `show()` calls after it returns.

Existing `tests/test_led_controller.py`, `tests/test_led_wiring.py` and
`tests/test_quality.py` must continue to pass unchanged.
