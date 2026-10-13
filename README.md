# Internet Schminternet

A Raspberry Pi service that continuously monitors the health of your internet
connection and displays the status on a WS2812B LED strip and a local web dashboard.

---

## What it monitors

| Monitor | What it measures | Default interval |
|---|---|---|
| **Ping** | Latency (ms) + packet loss % to configurable hosts, plus your LAN gateway to tell a router fault from an ISP fault | 30 s |
| **DNS** | Resolution time against specific nameservers | 60 s |
| **HTTP** | Response time + reachability of configurable URLs | 120 s |
| **Speedtest** | Download / upload Mbps via speed.cloudflare.com | adaptive, 1–30 min |
| **IP Tracker** | External IP address change detection | 5 min |

Results are stored in a local SQLite database and shown on:
- A dark-themed web dashboard at `http://pi-address:8080`
- A WS2812B addressable LED strip (rank-sorted, green-to-red by quality score)
- SMTP email alerts on state transitions

---

## Hardware requirements

- Raspberry Pi 3B / 3B+ (or any Pi running Raspberry Pi OS Bullseye/Bookworm)
- WS2812B / NeoPixel addressable LED strip
- Data wire connected to **GPIO18** (PWM0)

### GPIO18 / audio conflict

GPIO18 shares the PWM0 output with the onboard 3.5 mm audio jack.
If you need the audio jack, choose GPIO12, GPIO13, or GPIO21 instead (update
`leds.pin` in `config.yaml` accordingly) **and** remove the `dtparam=audio=off`
line from `/boot/config.txt`.

If you do **not** need the audio jack, add this line to `/boot/config.txt`:

```
dtparam=audio=off
```

---

## Installation

### 1. Clone the repo

```bash
git clone git@github.com:conradstorz/Internet_Schminternet.git
cd Internet_Schminternet
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install the LED library on the Pi (requires system build tools):

```bash
sudo apt update && sudo apt install -y python3-dev swig gcc mtr-tiny
pip install rpi-ws281x
```

### 3. Configure

```bash
cp config.example.yaml config.yaml
nano config.yaml   # fill in your targets, SMTP credentials, LED settings
```

Key settings to review:

- `monitors.ping.targets` — internet hosts to ping (your router is pinged separately — see `monitors.ping.gateway`)
- `monitors.ping.gateway` — `auto` (default, Linux only), an explicit router address, or `null` to disable
- `monitors.ping.trace` — onset path trace (`enabled`, `target`, `cycles` — default 20 probes per hop, `cooldown_minutes`)
- `monitors.speedtest.expected_download_mbps` — your expected plan speed
- `leds.enabled` — set `true` when LED strip is connected
- `leds.count` — total number of LEDs on your strip
- `leds.orientation` — `top_down` (index 0 = top) or `bottom_up`, to match how the strip is physically mounted
- `leds.weights` — per-monitor weight used to combine scores into the `overall` slot's colour
- `alerts.email` — SMTP credentials (use a Gmail **App Password**, not your account password)

### 4. Test manually

```bash
sudo .venv/bin/python main.py
```

Open `http://localhost:8080` in a browser on the same network.

### 5. Install as a systemd service

```bash
# Adjust paths in the unit file first
sudo nano systemd/internet-schminternet.service

sudo cp systemd/internet-schminternet.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable internet-schminternet
sudo systemctl start internet-schminternet
```

Check logs:
```bash
sudo journalctl -u schminternet -f
```

---

## Development (non-Pi)

On a Windows/macOS/Linux dev machine the LED controller silently no-ops
(it logs a warning and continues). Set `leds.enabled: false` in `config.yaml`
to suppress the warning.

Run the test suite:

```bash
pip install -r requirements.txt
pytest tests/
```

---

## Project layout

```
.
├── config.example.yaml   # Configuration template — copy to config.yaml
├── config.py             # Config loader (deep-merges user config onto defaults)
├── main.py               # Entry point
├── monitors/             # One module per health check
├── storage/              # SQLite async data layer
├── leds/                 # WS2812B controller
├── web/                  # FastAPI app + Jinja2 templates + JS
├── alerts/               # SMTP email alerter
├── tests/                # pytest test suite
└── systemd/              # Systemd unit file
```

---

## LED colour key

The strip is **rank-sorted**, not fixed per-monitor: every scored monitor
(ping, DNS, HTTP, speedtest — `ip` is excluded, since an address change is an
event, not a measure of quality) gets a slot sized as evenly as `leds.count`
allows, and the slots re-sort on every poll, best score nearest the top
(`leds.orientation: top_down`, or the highest index under `bottom_up`). An
optional `overall` slot (`leds.overall: true`) sits at the bottom end showing
the combined, weighted score and never participates in the sorting.

Each slot's colour is a continuous **green-to-red gradient**, not one of a
few fixed states — a monitor's 0.0 (unusable) to 1.0 (perfect) score maps to
hue 120° (green) through yellow and orange down to 0° (pure red):

| Score | Colour |
|---|---|
| 1.0 | Green |
| ~0.6 | Yellow |
| ~0.2 | Orange |
| 0.0 | Pure red — remote access is gone |

No hardware? `GET /api/quality` returns the same scores, ranking, and hex
colours the strip would show, and the dashboard renders a compact preview
row in the same ranked order.

---

## Fault localisation

When ping goes bad, two things tell you *where*:

- **LAN gateway leg.** Every ping poll also pings your router (target
  `gateway <addr>`, auto-detected once at startup by walking TTL-limited pings
  toward the internet and taking the last private hop — this finds the real
  LAN router even inside Docker, where the default route is only the bridge).
  Each failing internet target then carries a one-line hint, shown in alert
  emails and the event log: `LAN gateway 192.168.86.1 ok (0% loss) — fault is
  beyond your router (modem/ISP)`, or `... — fault is on the local network or
  router`. Gateway rows don't count toward the LED ping score.
- **Path trace on onset.** When ping first goes from ok to degraded/down, an
  `mtr` report (traceroute if mtr is missing) runs once in the background —
  at most once per `cooldown_minutes` — and lands in the event timeline as a
  `path_trace` event: `Loss begins at hop 2 (142.254.146.181, 40%);
  destination 40% loss`, followed by the hop list. Loss is anchored on the
  destination, so a router that merely rate-limits ICMP to itself is not
  blamed. Requires `mtr-tiny` (installed in the Docker image).

Both are Linux-only and silently off elsewhere.

---

## Status damping and the event log

Each poll produces a **raw** status per monitor (worst target wins). Under
moderate packet loss that raw status is a coin flip: 5 ping probes per target
give loss in 20% steps, so a true ~40% loss lands on degraded or down at
random each poll, and DNS queries are simply lost or not. Every flip used to be
a transition — an event row, an SSE push, and a fresh `down` entry.

So each monitor also has an **effective** status, which is what the event
log, alerts, degraded-alert accumulation, the path-trace trigger and the
dashboard's live status follow:

- **Getting worse is immediate.** The first `down` alert is never delayed.
- **Getting better needs `recover_polls` consecutive better polls**
  (`monitors.<name>.recover_polls`; 3 for ping and DNS, 2 for the rest). It
  then lands on the *worst* status seen while confirming, so `down`,
  `degraded`, `ok` with `recover_polls: 3` recovers to `degraded`, not `ok`.
  Any poll that isn't better restarts the count. Set 1 to turn damping off.

Metric rows (charts) and LED scores keep the raw per-poll values. The log's
per-poll summary line shows the raw status and appends `(holding down)` while
the effective status is being held. Pending confirmation is in memory only; a
restart starts from `unknown`, which the first poll replaces immediately.

A degraded → down transition writes a single `status_change` row (the one
carrying the failure detail); its previous status records the end of the
degraded episode.

---

## Notes

- The speedtest monitor is async (httpx against speed.cloudflare.com), so it
  needs no thread executor. Cadence is adaptive (see `monitors.speedtest.adaptive`
  in config.example.yaml): good results slow it to every 30 minutes, poor results
  speed it up to every minute with larger transfers until the link recovers.
- `config.yaml` is gitignored. Never commit credentials.
- The web dashboard depends on CDN links for Chart.js and Luxon. If you want
  the dashboard to function with the internet completely down, download these
  JS files to `web/static/` and update the `<script>` tags in `index.html`.
