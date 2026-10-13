# Fault localisation: LAN gateway ping + path trace on episode onset

## Motivation

On 2026-10-13 from ~18:30 UTC the link lost ~40% of packets. Every ping target,
both DNS servers and the speedtest failed, but nothing in the tool said *where*
the loss started. A manual check from hpz440 answered it in two commands:

```
ping 192.168.86.1   -> 0% loss, 0.4 ms        (LAN is fine)
mtr -n -r 1.1.1.1   -> loss begins at hop 2, 142.254.146.181 (first ISP hop)
```

```
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
```

This feature makes the tool do both checks itself.

## 1. LAN gateway ping

### Config

`monitors.ping.gateway`: `"auto"` (default), an explicit IP/hostname, or `null`/`false` to disable.

### Auto-detection (`monitors/gateway.py`)

The kernel default route is **wrong inside Docker**: in the `schminternet`
container it is the bridge (`172.25.0.1`), always 0% loss. Instead, probe with
TTL-limited pings toward a public address (first configured ping target that is
an IP, else `1.1.1.1`): `ping -c 1 -W 1 -t N <target>` for N = 1..4, parse
`From <addr> ... Time to live exceeded`. The gateway is the **last private hop
before the first public hop**. Measured live on hpz440 in the container:
TTL 1 -> `172.25.0.1`, TTL 2 -> `192.168.86.1` (the answer), TTL 3 -> public.
On a Pi on the LAN, TTL 1 is the answer directly.

- "Private" = RFC1918 (10/8, 172.16/12, 192.168/16). `100.64.0.0/10` (CGNAT) and
  everything else count as public — they belong to the ISP.
- A hop that doesn't answer stops the walk; use the last private hop seen.
- If the destination itself replies at some TTL (so no hop beyond), stop.
- Pure parse function `_parse_ttl_reply(output) -> str | None` and pure
  selection `_choose_gateway(hops: list[str | None]) -> str | None`, both unit-tested.
- Linux only. On Windows (dev) auto mode returns `None` (feature off), no subprocess.
- Detect **once** (on first ping poll), cache in the module. If detection
  finds nothing, log a WARNING once and the gateway leg is off for the process
  lifetime. If detection finds an address but a confirming normal ping gets
  zero replies (router ignores ICMP), log a WARNING and disable likewise —
  otherwise ping would read `down` forever.
- Log the detected gateway at INFO once.

### Ping monitor changes (`monitors/ping.py`)

- The gateway is pinged in the **same `asyncio.gather`** as the other targets
  (no added wall-clock). Gateway rows use `target=f"gateway {addr}"`
  (e.g. `gateway 192.168.86.1`) — `message` is reserved for the fault-location
  text below. Rows carry the same metrics (`latency_ms`, `packet_loss_pct`) and statuses
  via `_determine_status`, so a dead router makes ping `down` by worst-wins.
- **Fault location message.** Add a pure function
  `_fault_location(gateway_status, internet_statuses) -> str` and attach its
  text as the `message` on every **non-ok** internet-target row (so it appears
  in down-alert emails and status_change event descriptions, which today read
  "No detail"):
  - gateway ok, any internet target non-ok -> `"LAN gateway <addr> ok (<loss>% loss) — fault is beyond your router (modem/ISP)"`
  - gateway non-ok -> `"LAN gateway <addr> <loss>% loss — fault is on the local network or router"`
  - no gateway leg -> internet rows still get a short message:
    `"<loss>% loss"` (so "No detail" goes away regardless).
  Keep messages one line.

### LED scoring (`leds/quality.py`)

`_score_ping` must **skip targets whose name starts with `"gateway"`** — a
perfect 0.4 ms LAN hop would otherwise dilute the ping score. (A dead gateway
still drives the status to down and the internet targets will score 0 anyway.)

## 2. Path trace on episode onset

### Module `monitors/path_trace.py`

- `run_trace(target, cycles) -> str | None` (sync, for executor): run
  `mtr -n -r -c <cycles> <target>` (timeout `cycles*2 + 20` s). If `mtr` is not
  on PATH (`shutil.which`), fall back to `traceroute -n -q 3 -w 2 <target>`; if
  neither, return `None`. Non-Linux: `None`.
- Pure `analyse_mtr(report: str) -> dict` returning
  `{"hops": [(n, host, loss_pct), ...], "culprit": (n, host, loss) | None, "clean": bool}`.
  **Anchoring rule (ICMP rate-limit safe):** routers often drop probes
  addressed to themselves while forwarding fine, so a lossy middle hop alone
  means nothing.
  - If the final hop's loss < 5% -> `clean=True`, no culprit.
  - Otherwise the culprit is the first hop `k` such that **every** hop from
    `k` to the end (ignoring `???` rows) has loss >= half the final hop's loss.
  Tests: the real report above -> culprit hop 2 `142.254.146.181`; a
  counter-fixture with a `???`/100% middle hop and 0% at the destination ->
  clean, no culprit; a fixture where loss starts at hop 1 -> culprit hop 1.
- `summarise(analysis) -> str`: one line, e.g.
  `"Loss begins at hop 2 (142.254.146.181, 40%); destination 40% loss"` or
  `"Path clean: destination 0% loss"`.

### Trigger (in `main.py`, separate helper `_maybe_trace(monitor_name, previous, new_status)`)

Called from `run_monitor` **outside** the `if _alerter` block, for
`monitor_name == "ping"` only:

- Fire only on **episode onset**: `previous in ("ok", "unknown")` and
  `new_status in ("degraded", "down")`. Not on degraded<->down flips.
- Cooldown `monitors.ping.trace.cooldown_minutes` (default 15), measured from
  the last trace **start**, in a module-level variable.
- At most one trace in flight (module-level task reference; also keeps the
  task from being garbage-collected).
- Runs as `asyncio.create_task(...)`; the trace itself goes through
  `loop.run_in_executor` with `loop = asyncio.get_running_loop()` inside the coroutine.
- Target: `monitors.ping.trace.target`, default the first ping target.
- On completion insert an event: `event_type="path_trace"`, `monitor="ping"`,
  `description = summary + "\n" + compact hop list` (e.g.
  `"1 192.168.86.1 0% · 2 142.254.146.181 40% · ..."` on one line — keep the
  whole description to two lines), `previous_status`/`new_status` = the
  transition that triggered it. Also log the summary at WARNING.
- `monitors.ping.trace.enabled` (default True), `cycles` (default 10).
- Any exception inside the task is logged, never raised.

### Packaging

- Dockerfile: add `mtr-tiny` to the apt line (container runs as root, so mtr's
  raw sockets work).
- README Pi install line: add `mtr-tiny`.

### Dashboard

Check how `web/templates` (events partial) and `web/static` JS render
`event_type` and descriptions; make sure a `path_trace` event displays
sensibly (label/colour like other non-status events, newline rendered or
replaced). No schema change.

## Docs

Update `DEFAULT_CONFIG`, `config.example.yaml`, README monitor section, and
CLAUDE.md (ping conventions: gateway leg + fault message; event types: `path_trace`; LED scoring excludes gateway).

## Out of scope

Damping degraded<->down flapping and the duplicate degraded->down event row (separate PR).
