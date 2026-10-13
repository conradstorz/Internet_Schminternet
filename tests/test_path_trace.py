"""Tests for monitors/path_trace.py — mtr report analysis (pure)."""

from __future__ import annotations

from unittest.mock import patch

from monitors import path_trace
from monitors.path_trace import analyse_mtr, compact_hops, summarise

# The real report from hpz440 on 2026-10-13, ~18:45 UTC (see the design spec).
REAL_REPORT = """\
Start: 2026-10-13T18:45:02+0000
HOST: hpz440                      Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.86.1               0.0%    25    0.5   0.5   0.4   0.6   0.1
  2.|-- 142.254.146.181           40.0%    25   15.1  17.4   9.0  26.9   6.5
  3.|-- 74.128.4.21               36.0%    25  199.1  73.9  25.6 199.1  66.1
  4.|-- 24.29.0.48                36.0%    25   13.7  16.1   9.7  26.3   5.7
  5.|-- 65.29.28.54               56.0%    25   13.3  15.5  10.6  25.4   5.1
  6.|-- 65.189.140.166            44.0%    25   22.1  28.8  21.3  55.2   8.7
  7.|-- 65.29.39.110              36.0%    25   36.1  27.0  22.4  37.6   4.3
  8.|-- 65.29.33.239              36.0%    25   25.4  30.2  24.1  42.4   5.4
  9.|-- 172.69.174.2              28.0%    25   24.2  32.7  23.1  64.3  10.5
 10.|-- 1.1.1.1                   40.0%    25   25.6  28.8  22.7  44.0   6.2
"""

# ICMP rate-limiting counter-fixture: a silent middle hop and a router that
# drops probes addressed to itself, but the destination is perfect.
RATE_LIMITED_REPORT = """\
Start: 2026-10-13T19:00:00+0000
HOST: hpz440                      Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.86.1               0.0%    10    0.5   0.5   0.4   0.6   0.1
  2.|-- ???                       100.0    10    0.0   0.0   0.0   0.0   0.0
  3.|-- 74.128.4.21               60.0%    10   12.1  13.9  11.6  19.1   2.1
  4.|-- 1.1.1.1                    0.0%    10   25.6  28.8  22.7  44.0   6.2
"""

# Loss from the very first hop: the LAN itself is the problem.
LAN_LOSS_REPORT = """\
HOST: pi                          Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.86.1              50.0%    10    0.5   0.5   0.4   0.6   0.1
  2.|-- 142.254.146.181           50.0%    10   15.1  17.4   9.0  26.9   6.5
  3.|-- 1.1.1.1                   40.0%    10   25.6  28.8  22.7  44.0   6.2
"""

# Destination never answers: trailing ??? rows. The final row's loss (100%)
# is the destination loss, and loss "begins" at the first silent hop after
# the last router that answered.
DEAD_PATH_REPORT = """\
HOST: hpz440                      Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.86.1               0.0%    10    0.5   0.5   0.4   0.6   0.1
  2.|-- 142.254.146.181            0.0%    10   15.1  17.4   9.0  26.9   6.5
  3.|-- ???                       100.0    10    0.0   0.0   0.0   0.0   0.0
  4.|-- ???                       100.0    10    0.0   0.0   0.0   0.0   0.0
"""

TRACEROUTE_REPORT = """\
traceroute to 1.1.1.1 (1.1.1.1), 30 hops max, 60 byte packets
 1  192.168.86.1  0.456 ms  0.401 ms  0.512 ms
 2  142.254.146.181  15.1 ms * 17.0 ms
 3  * * *
 4  1.1.1.1  25.6 ms *  28.8 ms
"""


class TestAnalyseMtr:
    def test_real_report_culprit_is_first_isp_hop(self):
        a = analyse_mtr(REAL_REPORT)
        assert a["clean"] is False
        assert a["culprit"] == (2, "142.254.146.181", 40.0)
        assert len(a["hops"]) == 10
        assert a["hops"][0] == (1, "192.168.86.1", 0.0)
        assert a["hops"][-1] == (10, "1.1.1.1", 40.0)

    def test_rate_limited_middle_hops_are_clean(self):
        a = analyse_mtr(RATE_LIMITED_REPORT)
        assert a["clean"] is True
        assert a["culprit"] is None
        assert a["hops"][1] == (2, "???", 100.0)

    def test_loss_from_hop_one(self):
        a = analyse_mtr(LAN_LOSS_REPORT)
        assert a["clean"] is False
        assert a["culprit"] == (1, "192.168.86.1", 50.0)

    def test_dead_path_culprit_is_first_silent_hop(self):
        a = analyse_mtr(DEAD_PATH_REPORT)
        assert a["clean"] is False
        assert a["culprit"] == (3, "???", 100.0)

    def test_silent_middle_hop_is_not_named_when_a_later_hop_answers(self):
        report = (
            "HOST: x  Loss%   Snt   Last   Avg  Best  Wrst StDev\n"
            "  1.|-- 192.168.86.1   0.0%  10  0.5 0.5 0.4 0.6 0.1\n"
            "  2.|-- ???          100.0   10  0.0 0.0 0.0 0.0 0.0\n"
            "  3.|-- 74.128.4.21   40.0%  10  1.0 1.0 1.0 1.0 0.0\n"
            "  4.|-- 1.1.1.1       40.0%  10  1.0 1.0 1.0 1.0 0.0\n"
        )
        assert analyse_mtr(report)["culprit"] == (3, "74.128.4.21", 40.0)

    def test_traceroute_fallback_output(self):
        a = analyse_mtr(TRACEROUTE_REPORT)
        hosts = [h[1] for h in a["hops"]]
        assert hosts == ["192.168.86.1", "142.254.146.181", "???", "1.1.1.1"]
        assert a["hops"][1][2] == round(100 / 3, 1)
        assert a["clean"] is False

    def test_garbage_does_not_raise(self):
        a = analyse_mtr("mtr: unable to get raw sockets\n")
        assert a == {"hops": [], "culprit": None, "clean": False}


class TestSummarise:
    def test_culprit(self):
        assert summarise(analyse_mtr(REAL_REPORT)) == (
            "Loss begins at hop 2 (142.254.146.181, 40%); destination 40% loss"
        )

    def test_clean(self):
        assert summarise(analyse_mtr(RATE_LIMITED_REPORT)) == "Path clean: destination 0% loss"

    def test_inconclusive(self):
        assert "\n" not in summarise(analyse_mtr(""))
        assert summarise(analyse_mtr("")).startswith("Trace inconclusive")


def test_compact_hops_one_line():
    line = compact_hops(analyse_mtr(REAL_REPORT))
    assert "\n" not in line
    assert line.startswith("1 192.168.86.1 0% · 2 142.254.146.181 40% · 3 74.128.4.21 36%")
    assert line.endswith("10 1.1.1.1 40%")


# ---------------------------------------------------------------------------
# run_trace — platform / binary selection only (never runs a real trace)
# ---------------------------------------------------------------------------

def test_run_trace_non_linux_returns_none():
    # conftest stubs path_trace.run_trace; exercise the real function.
    with patch.object(path_trace.platform, "system", return_value="Windows"), \
         patch.object(path_trace.subprocess, "run", side_effect=AssertionError):
        assert path_trace._run_trace_impl("1.1.1.1", 10) is None


def test_run_trace_no_binaries_returns_none():
    with patch.object(path_trace.platform, "system", return_value="Linux"), \
         patch.object(path_trace.shutil, "which", return_value=None), \
         patch.object(path_trace.subprocess, "run", side_effect=AssertionError):
        assert path_trace._run_trace_impl("1.1.1.1", 10) is None


def test_run_trace_prefers_mtr():
    class Done:
        stdout = REAL_REPORT
        returncode = 0

    with patch.object(path_trace.platform, "system", return_value="Linux"), \
         patch.object(path_trace.shutil, "which", side_effect=lambda b: f"/usr/bin/{b}"), \
         patch.object(path_trace.subprocess, "run", return_value=Done()) as run:
        assert path_trace._run_trace_impl("1.1.1.1", 10) == REAL_REPORT
    cmd = run.call_args.args[0]
    assert cmd == ["/usr/bin/mtr", "-n", "-r", "-c", "10", "1.1.1.1"]
    assert run.call_args.kwargs["timeout"] == 10 * 2 + 20


def test_run_trace_falls_back_to_traceroute():
    class Done:
        stdout = TRACEROUTE_REPORT
        returncode = 0

    with patch.object(path_trace.platform, "system", return_value="Linux"), \
         patch.object(path_trace.shutil, "which",
                      side_effect=lambda b: "/usr/sbin/traceroute" if b == "traceroute" else None), \
         patch.object(path_trace.subprocess, "run", return_value=Done()) as run:
        assert path_trace._run_trace_impl("1.1.1.1", 10) == TRACEROUTE_REPORT
    assert run.call_args.args[0] == ["/usr/sbin/traceroute", "-n", "-q", "3", "-w", "2", "1.1.1.1"]
