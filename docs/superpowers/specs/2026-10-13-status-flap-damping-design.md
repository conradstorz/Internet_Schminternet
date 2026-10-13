# Status flap damping + single degraded→down event

## Motivation

During the 2026-10-13 Spectrum packet-loss episode (~40% loss from 18:30 UTC),
the event log gained 242 rows in a day. Ping swung `degraded ↔ down` every
30–60 s and DNS swung `ok ↔ down` every minute or two.

Mechanism: ping sends `count: 5` probes per target, so per-poll loss comes in
20% steps. A true ~40% loss rate lands at 40% (degraded) or 60–80% (down,
`loss_down_pct: 50`) by chance each poll, and worst-wins across three targets
amplifies it. DNS is the same story: each UDP query is lost or not. The
monitor's state is a coin flip each poll, and each flip is a transition
(event row, potential alert, SSE push).

With email enabled, the per-monitor cooldown already suppresses repeat `down`
emails, but the event log is flooded and each `down` re-entry is a fresh
transition.

## 1. Recovery hysteresis (monitor-level, all monitors)

Damp at the source of the transition: the monitor's **effective** status.

- **Worsening is immediate.** If the raw (worst-wins) status of this poll is
  worse than or equal to the current effective status, the effective status
  becomes the raw status at once. The first `down` alert is never delayed.
- **Improving needs confirmation.** If the raw status is better than the
  effective status, count consecutive "better" polls. The effective status
  changes only once that count reaches `recover_polls` for this monitor. It
  moves to the **worst** raw status seen across those confirming polls, so
  `down, degraded, ok, ok` with `recover_polls: 3` lands on `degraded` at the
  third better poll, not `ok`. Any poll whose raw status is not better than
  the effective status resets the count.
- `unknown` (no previous state, e.g. at startup) is replaced by the first raw
  status immediately.
- Config: `monitors.<name>.recover_polls`, defaulting to 2. Set ping and DNS to
  3 in `DEFAULT_CONFIG`. A value of 1 (or 0, or missing) means no damping, the
  old behaviour.
- Implement the decision as a pure function in a new module
  `monitors/hysteresis.py` (no I/O), e.g.
  `step(effective, pending_count, pending_worst, raw, recover_polls) -> (effective, pending_count, pending_worst)`,
  with exhaustive unit tests. `main.py` keeps the in-memory pending state per
  monitor next to `_monitor_status`; it is not persisted (a restart starts
  from `unknown`, as today).
- Everything downstream of the status in `run_monitor` uses the **effective**
  status: transition logging (`EVENT ...`), events, alerts, degraded
  accumulation, the trace trigger, and the SSE `status`. Metric rows keep their
  own raw per-row statuses (unchanged). LED scores are computed from raw
  values, as today.
- The per-poll summary log line shows the raw status, and appends
  ` (holding <effective>)` when the effective status differs.

Rough effect at 40% loss with three ping targets: P(one poll is all ≤40%) ≈
0.32, so ping escapes `down` with probability ≈ 0.03 per poll at
`recover_polls: 3` versus ≈ 0.32 today.

## 2. One event row per degraded → down transition

`run_monitor` writes two `status_change` rows for degraded → down: a bare
`"Status changed: degraded → down"` (main.py, inside `if previous == "degraded"`)
and the detailed `"Status changed degraded → down. <desc>"` that every
→ down transition writes. Remove the bare one. The detailed row already
records `previous_status = "degraded"`, so the degraded episode's end is still
visible. `tests/test_degraded_alerting.py` asserts the bare string. Change that
assertion on purpose, to check that exactly one degraded → down row is written
and that it carries the detail, and say so in the PR.

## Docs

Update `DEFAULT_CONFIG`, `config.example.yaml`, README, and CLAUDE.md: the
per-poll data flow now goes raw status → hysteresis → effective status, and
the event-log semantics change.
