"""Path trace (mtr / traceroute) run once at the onset of a ping episode.

``run_trace`` is blocking and meant for ``loop.run_in_executor``; the rest is
pure analysis. The analysis is anchored on the destination, because routers
often drop probes addressed to themselves while forwarding traffic fine — a
lossy middle hop on its own means nothing. See
docs/superpowers/specs/2026-10-13-fault-localisation-design.md.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from typing import Optional

Hop = tuple[int, str, float]

SILENT = "???"
CLEAN_BELOW_PCT = 5.0

#   "  2.|-- 142.254.146.181           40.0%    25   15.1 ..."
#   "  3.|-- ???                       100.0    10    0.0 ..."
_MTR_ROW_RE = re.compile(r"^\s*(\d+)\.\|--\s+(\S+)\s+([\d.]+)%?\s")
#   " 2  142.254.146.181  15.1 ms * 17.0 ms"   /   " 3  * * *"
_TRACEROUTE_ROW_RE = re.compile(r"^\s*(\d+)\s+(.*)$")


def run_trace(target: str, cycles: int) -> Optional[str]:
    """Run ``mtr -n -r -c <cycles> <target>`` (or traceroute) and return its text.

    Falls back to ``traceroute -n -q 3 -w 2`` when mtr is not installed.
    Returns None on non-Linux, when neither tool exists, or on failure.
    """
    return _run_trace_impl(target, cycles)


def _run_trace_impl(target: str, cycles: int) -> Optional[str]:
    if platform.system() != "Linux":
        return None
    mtr = shutil.which("mtr")
    if mtr:
        cmd = [mtr, "-n", "-r", "-c", str(cycles), target]
    else:
        traceroute = shutil.which("traceroute")
        if not traceroute:
            return None
        cmd = [traceroute, "-n", "-q", "3", "-w", "2", target]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=cycles * 2 + 20
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout or None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_mtr(report: str) -> list[Hop]:
    hops: list[Hop] = []
    for line in report.splitlines():
        m = _MTR_ROW_RE.match(line)
        if m:
            hops.append((int(m.group(1)), m.group(2), float(m.group(3))))
    return hops


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _parse_traceroute(report: str) -> list[Hop]:
    """traceroute -n -q 3: loss = unanswered probes ('*') out of probes sent."""
    hops: list[Hop] = []
    for line in report.splitlines():
        if line.lstrip().lower().startswith("traceroute"):
            continue
        m = _TRACEROUTE_ROW_RE.match(line)
        if not m:
            continue
        tokens = m.group(2).split()
        stars = tokens.count("*")
        answered = len(re.findall(r"[\d.]+\s*ms", m.group(2)))
        probes = stars + answered
        if probes == 0:
            continue
        host = next((t for t in tokens if t not in ("*", "ms") and not _is_number(t)), None)
        if host is None or answered == 0:
            host = SILENT
        hops.append((int(m.group(1)), host, round(100.0 * stars / probes, 1)))
    return hops


def _culprit(hops: list[Hop]) -> Optional[Hop]:
    """First hop k such that every answering hop from k to the end has loss
    >= half the final hop's loss. A silent (???) hop is only named when no
    later hop answers — otherwise the next answering hop speaks for it."""
    final_loss = hops[-1][2]
    floor = final_loss / 2
    for i, hop in enumerate(hops):
        rest = hops[i:]
        answering = [h for h in rest if h[1] != SILENT]
        if hop[1] == SILENT and answering:
            continue
        if all(h[2] >= floor for h in answering):
            return hop
    return None


def analyse_mtr(report: str) -> dict:
    """Locate where loss begins on the path.

    Returns ``{"hops": [(n, host, loss_pct), ...], "culprit": hop | None,
    "clean": bool}``. A destination (final hop) loss under 5% is clean.
    Accepts mtr report text, or traceroute output from the fallback path.
    """
    report = report or ""
    hops = _parse_mtr(report) if "Loss%" in report else _parse_traceroute(report)
    if not hops:
        return {"hops": [], "culprit": None, "clean": False}
    if hops[-1][2] < CLEAN_BELOW_PCT:
        return {"hops": hops, "culprit": None, "clean": True}
    return {"hops": hops, "culprit": _culprit(hops), "clean": False}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(value: float) -> str:
    return f"{int(value)}%" if value == int(value) else f"{value:.1f}%"


def summarise(analysis: dict) -> str:
    """One-line verdict, e.g. "Loss begins at hop 2 (142.254.146.181, 40%); destination 40% loss"."""
    hops = analysis.get("hops") or []
    if not hops:
        return "Trace inconclusive: no hops parsed"
    dest = _pct(hops[-1][2])
    if analysis.get("clean"):
        return f"Path clean: destination {dest} loss"
    culprit = analysis.get("culprit")
    if culprit:
        n, host, loss = culprit
        return f"Loss begins at hop {n} ({host}, {_pct(loss)}); destination {dest} loss"
    return f"Loss origin unclear; destination {dest} loss"


def compact_hops(analysis: dict) -> str:
    """All hops on one line: "1 192.168.86.1 0% · 2 142.254.146.181 40% · ..."."""
    return " · ".join(f"{n} {host} {_pct(loss)}" for n, host, loss in analysis.get("hops") or [])
