/**
 * Internet Schminternet — dashboard client script
 *
 * Responsibilities:
 *  1. Poll /api/status every 10 s and update status cards
 *  2. Connect to /stream (SSE) for immediate card refreshes
 *  3. Render three Chart.js line charts from /api/metrics/{monitor}
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Which metric drives the primary display value for each monitor card. */
const CARD_METRIC = {
  ping:      { metric: "latency_ms",    format: v => Math.round(v) },
  dns:       { metric: "resolution_ms", format: v => Math.round(v) },
  http:      { metric: "response_ms",   format: v => Math.round(v) },
  speedtest: { metric: "download_mbps", format: v => v.toFixed(1) },
  ip:        { metric: "ip_changed",    format: (_v, msg) => msg || "—" },
};

const ALL_STATUSES = ["ok", "degraded", "down", "measuring", "unknown"];

// ---------------------------------------------------------------------------
// Status card helpers
// ---------------------------------------------------------------------------

function setCard(monitor, status, displayValue) {
  const card  = document.getElementById(`card-${monitor}`);
  const badge = document.getElementById(`badge-${monitor}`);
  const val   = document.getElementById(`val-${monitor}`);

  if (!card) return;

  card.className  = `status-card ${status}`;
  badge.className = `badge ${status}`;
  badge.textContent = status;

  if (val && displayValue !== undefined) val.textContent = displayValue;
}

/**
 * Update all status cards from the flat array returned by /api/status.
 * The API returns one row per (monitor, target, metric) combination; we pick
 * the most representative metric for each monitor card.
 */
function applyStatusRows(rows) {
  // Index into { "monitor:metric": row } keeping the latest timestamp per key
  const latest = {};
  for (const row of rows) {
    const key = `${row.monitor}:${row.metric}`;
    if (!latest[key] || row.timestamp > latest[key].timestamp) {
      latest[key] = row;
    }
  }

  for (const [monitor, cfg] of Object.entries(CARD_METRIC)) {
    const row = latest[`${monitor}:${cfg.metric}`];
    if (!row) continue;

    const display =
      monitor === "ip"
        ? row.message || "—"
        : row.value >= 0
        ? cfg.format(row.value, row.message)
        : "err";

    setCard(monitor, row.status, display);
  }
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

async function fetchStatus() {
  try {
    const res  = await fetch("/api/status");
    const rows = await res.json();
    applyStatusRows(rows);

    document.getElementById("last-update").textContent =
      `Last updated ${new Date().toLocaleTimeString()}`;
    document.getElementById("live-dot").style.background = "var(--ok)";
  } catch {
    document.getElementById("live-dot").style.background = "var(--down)";
  }
}

// ---------------------------------------------------------------------------
// Server-Sent Events
// ---------------------------------------------------------------------------

function connectSSE() {
  const es = new EventSource("/stream");

  es.onmessage = (event) => {
    const data = JSON.parse(event.data);
    const { monitor, status, results = [] } = data;

    const cfg = CARD_METRIC[monitor];
    if (!cfg) return;

    const relevant = results.find((r) => r.metric === cfg.metric);
    const display = relevant
      ? monitor === "ip"
        ? relevant.message || "—"
        : relevant.value >= 0
        ? cfg.format(relevant.value, relevant.message)
        : "err"
      : undefined;

    setCard(monitor, status, display);
    document.getElementById("last-update").textContent =
      `Live update ${new Date().toLocaleTimeString()}`;
  };

  es.onerror = () => {
    document.getElementById("live-dot").style.background = "var(--degraded)";
    es.close();
    setTimeout(connectSSE, 5000);   // auto-reconnect after 5 s
  };
}

// ---------------------------------------------------------------------------
// Chart.js helpers
// ---------------------------------------------------------------------------

const CHART_BASE_OPTIONS = {
  responsive: true,
  maintainAspectRatio: false,
  animation: false,
  scales: {
    x: {
      type: "time",
      time: { unit: "hour", displayFormats: { hour: "HH:mm" } },
      ticks: { color: "#94a3b8", maxTicksLimit: 8 },
      grid: { color: "#1e293b" },
    },
    y: {
      beginAtZero: true,
      ticks: { color: "#94a3b8" },
      grid: { color: "#334155" },
    },
  },
  plugins: { legend: { display: false } },
};

async function buildChart(canvasId, monitor, metric, color) {
  try {
    const res  = await fetch(`/api/metrics/${monitor}?hours=24`);
    const rows = await res.json();

    const points = rows
      .filter((r) => r.metric === metric && r.value >= 0)
      .map((r) => ({ x: new Date(r.timestamp), y: r.value }));

    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    new Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            data: points,
            borderColor: color,
            backgroundColor: color + "25",
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          },
        ],
      },
      options: CHART_BASE_OPTIONS,
    });
  } catch (err) {
    console.warn(`Chart ${canvasId} failed to load:`, err);
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function init() {
  await fetchStatus();
  setInterval(fetchStatus, 10_000);
  connectSSE();

  // Charts load in parallel — failures are non-fatal
  await Promise.allSettled([
    buildChart("chart-ping",  "ping",      "latency_ms",    "#38bdf8"),
    buildChart("chart-dns",   "dns",       "resolution_ms", "#a78bfa"),
    buildChart("chart-speed", "speedtest", "download_mbps", "#34d399"),
  ]);
}

document.addEventListener("DOMContentLoaded", init);
