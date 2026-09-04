# Timeline Cluster Colour — Design

**Date:** 2026-09-04
**Status:** Approved, pending implementation

## Problem

The events timeline (`web/static/main.js`, the 12 h pannable strip) draws one
dot per event, coloured by that event's `new_status` — green for `ok`, amber
for `degraded`, red for `down`, grey for events with no health status.

When several events fall close enough together to overlap on screen,
`clusterMarkers` collapses them into one larger dot carrying a count. That
cluster dot is painted flat `var(--muted)` grey regardless of what it
contains (`main.js:986-988`).

The result is that the timeline loses colour exactly where the most happened.
A burst of five events during a bad night — the densest, most interesting
part of the strip — renders as an anonymous grey dot with a `5` in it, while
a single isolated `ok` event elsewhere shows a confident green. The viewer
has to hover every cluster to find out whether anything was wrong.

## Goal

Colour cluster dots by the worst status among the events they contain, so
the timeline's colour survives clustering and a bad period is visible without
hovering.

## Behaviour

- A cluster takes the worst status among its events, using the project's
  existing precedence: `down` > `degraded` > `ok`.
- The count digit stays. Clusters remain distinguishable from single events
  by size (18 px vs 10 px) and by carrying a number — they never relied on
  grey for that.
- Single-event dots are unchanged in appearance. They already take their own
  status, and a one-event cluster's worst status is that same status, so both
  cases can go through one code path.
- Events with no health status (`startup`, `ip_change`) contribute nothing to
  the ranking. A cluster containing only such events stays grey, matching
  what `timelineStatusColor` already does for them individually. A cluster
  mixing an `ip_change` with a `down` is red.

Explicit non-goal: the dot does **not** show a continuous quality score from
the metrics table. "Worst status in the cluster" is derived entirely from the
events already on screen — no new API endpoint, no per-cluster metric lookup,
no schema change.

## Components

### `web/static/main.js`

One new pure function, placed beside `clusterMarkers` in the pure-helpers
section (around `main.js:246`):

```js
function worstStatus(events)  // -> "down" | "degraded" | "ok" | "unknown"
```

It scans each event's `new_status` and returns the highest-precedence one
found, or `"unknown"` when no event carries a ranked status. It is added to
the `Dashboard` export object at `main.js:381`, alongside `clusterMarkers`
and the other pure helpers, following the file's established convention.

`renderTimelineMarkers` then drops its `isCluster` colour branch. Where it
currently reads:

```js
el.style.background = isCluster
  ? "var(--muted)"
  : timelineStatusColor(cluster.events[0].new_status);
```

it becomes a single unconditional call:

```js
el.style.background = timelineStatusColor(Dashboard.worstStatus(cluster.events));
```

`isCluster` is still needed for the `cluster` class name and the count text,
so it stays.

### CSS

No change. `.timeline-marker.cluster` (`web/templates/index.html:227-231`)
already sizes the dot and centres the digit, and the digit's
`color: var(--bg)` (#0f172a) has adequate contrast against all three status
colours: roughly 7.9:1 on `--ok` (#22c55e) and `--degraded` (#f59e0b), and
4.8:1 on `--down` (#ef4444), for bold 0.6 rem text.

### Server / API

No change. `GET /api/events` already returns `new_status` on every event,
which is the only field this feature reads.

## Error handling

`worstStatus` handles an empty array and events with a missing or unexpected
`new_status` by returning `"unknown"`, which `timelineStatusColor` already
maps to grey — the current cluster appearance. A malformed event therefore
degrades to today's behaviour rather than throwing inside the render loop.

## Testing

This is the weak point, and it is a deliberate choice rather than an
oversight.

The project has no JavaScript test runner. The pure helpers on `Dashboard`
(`percentile`, `clampSeries`, `clusterMarkers`, `bucketSeries`,
`heatmapGrid`, `renderQualityStrip`) are exported for testability but nothing
executes them; `tests/test_web_app.py` only greps the served asset. Rather
than introduce a node toolchain into a uv/pytest project for a five-line
display change, this feature matches the existing pattern.

Added to `tests/test_web_app.py`: a test asserting the served `main.js`
contains `worstStatus` and no longer contains the `var(--muted)` cluster
branch. This proves the code shipped, not that the ranking is correct.

The correctness argument rests on the function being a short precedence
lookup with no branching subtlety, and on the change being immediately
visible on the dashboard. Adding a node test runner for the `Dashboard`
helpers remains worthwhile on its own merits and is deliberately left out of
this change's scope.
