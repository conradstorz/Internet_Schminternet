"""Unit tests for monitors.hysteresis.step — the pure recovery-hysteresis rule.

step(effective, pending_count, pending_worst, raw, recover_polls)
    -> (effective, pending_count, pending_worst)

Worsening (or equal) is immediate; improving needs `recover_polls`
consecutive better polls and lands on the worst raw status seen across them.
See docs/superpowers/specs/2026-10-13-status-flap-damping-design.md.
"""

from __future__ import annotations

import itertools

import pytest

from monitors.hysteresis import IDLE, step

STATUSES = ("unknown", "ok", "degraded", "down")
SEVERITY = {"unknown": 0, "ok": 1, "degraded": 2, "down": 3}


def _run(effective, raws, recover_polls):
    """Feed a sequence of raw statuses; return the effective status after each."""
    count, worst = IDLE
    out = []
    for raw in raws:
        effective, count, worst = step(effective, count, worst, raw, recover_polls)
        out.append(effective)
    return out


# ---------------------------------------------------------------------------
# Idle pending state
# ---------------------------------------------------------------------------

def test_idle_is_zero_count_no_worst():
    assert IDLE == (0, None)


# ---------------------------------------------------------------------------
# Worsening (and equal) is immediate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recover_polls", [2, 3, 5])
@pytest.mark.parametrize(
    "effective,raw",
    [(e, r) for e, r in itertools.product(("ok", "degraded", "down"), repeat=2)
     if SEVERITY[r] >= SEVERITY[e]],
)
def test_worse_or_equal_raw_is_adopted_immediately(effective, raw, recover_polls):
    assert step(effective, 0, None, raw, recover_polls) == (raw, 0, None)


def test_ok_to_down_is_immediate_first_poll():
    assert _run("ok", ["down"], 3) == ["down"]


def test_worsening_mid_confirmation_is_immediate_and_clears_pending():
    # effective degraded, one ok poll pending, then down: down at once.
    assert step("degraded", 1, "ok", "down", 3) == ("down", 0, None)


# ---------------------------------------------------------------------------
# Improving needs recover_polls consecutive better polls
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recover_polls", [2, 3, 4, 7])
def test_down_to_ok_needs_exactly_recover_polls(recover_polls):
    out = _run("down", ["ok"] * recover_polls, recover_polls)
    assert out[:-1] == ["down"] * (recover_polls - 1)
    assert out[-1] == "ok"


@pytest.mark.parametrize("recover_polls", [2, 3])
def test_degraded_to_ok_needs_exactly_recover_polls(recover_polls):
    out = _run("degraded", ["ok"] * recover_polls, recover_polls)
    assert out == ["degraded"] * (recover_polls - 1) + ["ok"]


def test_down_to_degraded_needs_confirmation_too():
    assert _run("down", ["degraded", "degraded", "degraded"], 3) == ["down", "down", "degraded"]


def test_holding_returns_pending_count_and_worst():
    assert step("down", 0, None, "ok", 3) == ("down", 1, "ok")
    assert step("down", 1, "ok", "degraded", 3) == ("down", 2, "degraded")


def test_confirmation_clears_pending():
    assert step("down", 2, "ok", "ok", 3) == ("ok", 0, None)


def test_stays_put_after_recovery():
    out = _run("down", ["ok"] * 6, 3)
    assert out == ["down", "down", "ok", "ok", "ok", "ok"]


# ---------------------------------------------------------------------------
# Effective lands on the WORST raw status seen during confirmation
# ---------------------------------------------------------------------------

def test_spec_example_down_degraded_ok_ok_lands_on_degraded():
    # Spec: "down, degraded, ok, ok with recover_polls: 3 lands on degraded
    # at the third better poll, not ok."
    out = _run("ok", ["down", "degraded", "ok", "ok"], 3)
    assert out == ["down", "down", "down", "degraded"]


def test_worst_tracked_regardless_of_order():
    assert _run("down", ["ok", "degraded", "ok"], 3)[-1] == "degraded"
    assert _run("down", ["ok", "ok", "degraded"], 3)[-1] == "degraded"
    assert _run("down", ["degraded", "ok", "ok"], 3)[-1] == "degraded"


def test_all_ok_confirmation_lands_on_ok():
    assert _run("down", ["ok", "ok", "ok"], 3)[-1] == "ok"


def test_after_landing_on_degraded_further_ok_needs_fresh_confirmation():
    out = _run("down", ["degraded", "ok", "ok", "ok", "ok"], 3)
    # 3rd better poll -> degraded (worst of degraded, ok, ok); then two more
    # ok polls are not yet enough to leave degraded; the third one is.
    assert out == ["down", "down", "degraded", "degraded", "degraded"]
    assert _run("down", ["degraded", "ok", "ok", "ok", "ok", "ok"], 3)[-1] == "ok"


# ---------------------------------------------------------------------------
# A non-better poll resets the count
# ---------------------------------------------------------------------------

def test_equal_poll_resets_count():
    # down, then ok ok, then down (equal to effective): count resets, so
    # two more ok polls are not enough.
    out = _run("down", ["ok", "ok", "down", "ok", "ok"], 3)
    assert out == ["down"] * 5
    assert _run("down", ["ok", "ok", "down", "ok", "ok", "ok"], 3)[-1] == "ok"


def test_equal_poll_clears_pending_state():
    assert step("down", 2, "ok", "down", 3) == ("down", 0, None)
    assert step("degraded", 2, "ok", "degraded", 3) == ("degraded", 0, None)


def test_worst_from_aborted_confirmation_is_forgotten():
    # A degraded seen in an aborted window must not drag down a later,
    # clean confirmation window.
    out = _run("down", ["degraded", "down", "ok", "ok", "ok"], 3)
    assert out[-1] == "ok"


def test_alternating_down_degraded_never_recovers():
    raws = ["down", "degraded"] * 10
    out = _run("ok", raws, 3)
    assert out == ["down"] * len(raws)


def test_alternating_ok_down_never_recovers():
    raws = ["down", "ok"] * 10
    out = _run("ok", raws, 2)
    assert out == ["down"] * len(raws)


# ---------------------------------------------------------------------------
# unknown effective is replaced immediately
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["ok", "degraded", "down", "unknown"])
@pytest.mark.parametrize("recover_polls", [None, 1, 3])
def test_unknown_effective_is_replaced_immediately(raw, recover_polls):
    assert step("unknown", 0, None, raw, recover_polls) == (raw, 0, None)


def test_unknown_effective_replaced_even_with_stale_pending():
    assert step("unknown", 2, "degraded", "ok", 3) == ("ok", 0, None)


# ---------------------------------------------------------------------------
# recover_polls of 0, 1, None (or unusable) means no damping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("recover_polls", [None, 0, 1, -2, "nonsense", ""])
@pytest.mark.parametrize(
    "effective,raw", list(itertools.product(STATUSES, repeat=2))
)
def test_no_damping_passes_raw_through(effective, raw, recover_polls):
    assert step(effective, 0, None, raw, recover_polls) == (raw, 0, None)


@pytest.mark.parametrize("recover_polls", [None, 0, 1])
def test_no_damping_sequence_matches_raw(recover_polls):
    raws = ["down", "ok", "degraded", "ok", "down", "down", "ok"]
    assert _run("ok", raws, recover_polls) == raws


def test_numeric_string_recover_polls_is_accepted():
    # YAML may hand us "3"; it must behave like 3, not crash a poll.
    assert _run("down", ["ok", "ok", "ok"], "3") == ["down", "down", "ok"]


# ---------------------------------------------------------------------------
# raw "unknown" while effective is known: lowest severity, so it counts as
# "better" and is damped like any improvement (no monitor emits it today).
# ---------------------------------------------------------------------------

def test_raw_unknown_is_damped_like_an_improvement():
    assert step("down", 0, None, "unknown", 3) == ("down", 1, "unknown")
    assert _run("down", ["unknown", "ok", "unknown"], 3)[-1] == "ok"
    assert _run("down", ["unknown", "unknown", "unknown"], 3)[-1] == "unknown"


# ---------------------------------------------------------------------------
# Exhaustive property check over short sequences
# ---------------------------------------------------------------------------

def _reference(effective, raws, n):
    """Straightforward re-statement of the spec, used as an oracle."""
    out = []
    window: list[str] = []
    for raw in raws:
        if effective == "unknown" or SEVERITY[raw] >= SEVERITY[effective]:
            effective = raw
            window = []
        else:
            window.append(raw)
            if len(window) >= n:
                effective = max(window, key=SEVERITY.__getitem__)
                window = []
        out.append(effective)
    return out


@pytest.mark.parametrize("n", [2, 3])
@pytest.mark.parametrize("start", STATUSES)
def test_matches_reference_on_every_short_sequence(start, n):
    for length in range(1, 6):
        for raws in itertools.product(("ok", "degraded", "down"), repeat=length):
            assert _run(start, list(raws), n) == _reference(start, list(raws), n), raws


@pytest.mark.parametrize("n", [2, 3])
def test_never_more_optimistic_than_last_n_raws(n):
    """Once damped, effective is never better than the worst of the last n raws."""
    for raws in itertools.product(("ok", "degraded", "down"), repeat=6):
        out = _run("ok", list(raws), n)
        for i in range(n - 1, len(raws)):
            worst_recent = max(raws[i - n + 1:i + 1], key=SEVERITY.__getitem__)
            assert SEVERITY[out[i]] >= SEVERITY[worst_recent], (raws, out)
