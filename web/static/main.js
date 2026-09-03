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
// Pure helpers — chart y-axis clamping, timeline clustering.
//
// This file is a plain script (not a module), so these are exposed on
// `window.Dashboard` for the browser-driven verification pass to call
// directly (there is no JS test runner in this repo).
// ---------------------------------------------------------------------------

/** Linear-interpolation percentile (numpy's default method) over sorted values. */
function percentile(sortedValues, p) {
  const n = sortedValues.length;
  if (n === 0) return 0;
  if (n === 1) return sortedValues[0];
  const idx = (n - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sortedValues[lo];
  const frac = idx - lo;
  return sortedValues[lo] + (sortedValues[hi] - sortedValues[lo]) * frac;
}

/**
 * Clamp a series to a p95-based ceiling so one outage spike can't flatten
 * the rest of the chart. Points above the ceiling are clipped to it and
 * flagged `clipped: true`, keeping their real value in `trueValue`.
 *
 *   clampSeries(points, { floor = 50, factor = 1.5 })
 *   -> { ceiling, points: [{ x, y, clipped, trueValue }] }
 */
function clampSeries(points, { floor = 50, factor = 1.5 } = {}) {
  if (!points.length) return { ceiling: floor, points: [] };

  const sorted = points.map((p) => p.y).sort((a, b) => a - b);
  const p95 = percentile(sorted, 0.95);
  const ceiling = Math.max(floor, p95 * factor);

  return {
    ceiling,
    points: points.map((p) => {
      const clipped = p.y > ceiling;
      return { x: p.x, y: clipped ? ceiling : p.y, clipped, trueValue: p.y };
    }),
  };
}

/**
 * Group timeline events into on-screen clusters when their x pixel
 * positions land within `thresholdPx` of each other. Pure — no DOM.
 *   clusterMarkers([{ ...event, x }], thresholdPx) -> [{ x, events: [...] }]
 */
function clusterMarkers(events, thresholdPx = 10) {
  const sorted = [...events].sort((a, b) => a.x - b.x);
  const clusters = [];
  for (const ev of sorted) {
    const last = clusters[clusters.length - 1];
    if (last && ev.x - last.x <= thresholdPx) {
      last.events.push(ev);
      last.x = (last.x * (last.events.length - 1) + ev.x) / last.events.length;
    } else {
      clusters.push({ x: ev.x, events: [ev] });
    }
  }
  return clusters;
}

const Dashboard = { percentile, clampSeries, clusterMarkers };
if (typeof window !== "undefined") window.Dashboard = Dashboard;

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

async function buildChart(canvasId, monitor, metric, color, { clamp = false } = {}) {
  try {
    const res  = await fetch(`/api/metrics/${monitor}?hours=24`);
    const rows = await res.json();

    const raw = rows
      .filter((r) => r.metric === metric && r.value >= 0)
      .map((r) => ({ x: new Date(r.timestamp), y: r.value }));

    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    if (!clamp) {
      new Chart(canvas, {
        type: "line",
        data: {
          datasets: [
            {
              data: raw,
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
      return;
    }

    // Ping / DNS: an outage spike must not flatten the rest of the series —
    // clamp the y-axis to a p95-based ceiling and mark clipped points.
    const { ceiling, points } = Dashboard.clampSeries(raw);
    const CLIP_COLOR = "#ef4444"; // var(--down)

    const options = {
      ...CHART_BASE_OPTIONS,
      scales: {
        ...CHART_BASE_OPTIONS.scales,
        y: {
          beginAtZero: true,
          max: ceiling,
          ticks: { color: "#94a3b8" },
          grid: { color: "#334155" },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label(ctx) {
              const p = points[ctx.dataIndex];
              const time = new Date(p.x).toLocaleTimeString([], { hour12: false });
              return p.clipped
                ? `${Math.round(p.trueValue)} ms — ${time}`
                : `${Math.round(p.trueValue)} ms`;
            },
          },
        },
      },
    };

    new Chart(canvas, {
      type: "line",
      data: {
        datasets: [
          {
            data: points,
            borderColor: color,
            backgroundColor: color + "25",
            borderWidth: 1.5,
            pointRadius: points.map((p) => (p.clipped ? 4 : 0)),
            pointBackgroundColor: points.map((p) => (p.clipped ? CLIP_COLOR : color)),
            pointBorderColor: points.map((p) => (p.clipped ? CLIP_COLOR : color)),
            fill: true,
            tension: 0.3,
          },
        ],
      },
      options,
    });
  } catch (err) {
    console.warn(`Chart ${canvasId} failed to load:`, err);
  }
}

// ---------------------------------------------------------------------------
// Events timeline — 12 h pannable window
// ---------------------------------------------------------------------------

const TIMELINE_WINDOW_MS = 12 * 60 * 60 * 1000;
const TIMELINE_POLL_MS = 30_000;

const timelineState = {
  windowEnd: new Date(), // right edge of the visible window
  live: true,
};

let timelineCache = { events: [], start: null, end: null };

function timelineWindowStart() {
  return new Date(timelineState.windowEnd.getTime() - TIMELINE_WINDOW_MS);
}

function timelineStatusColor(status) {
  if (status === "ok" || status === "degraded" || status === "down") {
    return `var(--${status})`;
  }
  return "var(--unknown)"; // startup, ip_change, anything else
}

function fmtTimelineDate(d) {
  return d.toLocaleString([], {
    hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function updateTimelineRangeLabel() {
  const label = document.getElementById("tl-range");
  if (!label) return;
  const start = timelineWindowStart();
  const end = timelineState.windowEnd;
  label.textContent = `${fmtTimelineDate(start)} → ${fmtTimelineDate(end)}`;
  label.classList.toggle("live", timelineState.live);
  label.classList.toggle("historical", !timelineState.live);
}

function formatEventDetail(ev) {
  const ts = ev.timestamp ? ev.timestamp.replace("T", " ").slice(0, 19) : "—";
  const prev = ev.previous_status || "—";
  const next = ev.new_status || "—";
  const desc = ev.description || "";
  return (
    `<div class="tt-event">` +
    `<div class="tt-time">${ts} UTC</div>` +
    `<div><strong>${ev.monitor}</strong> · ${ev.event_type}</div>` +
    `<div class="tt-transition">${prev} → ${next}</div>` +
    `<div>${desc}</div>` +
    `</div>`
  );
}

function hideTimelineTooltip() {
  const tip = document.getElementById("timeline-tooltip");
  if (tip) tip.hidden = true;
}

function showTimelineTooltip(events, anchorEl) {
  const tip = document.getElementById("timeline-tooltip");
  if (!tip) return;

  tip.innerHTML = events.map(formatEventDetail).join("");
  tip.hidden = false;

  const anchorRect = anchorEl.getBoundingClientRect();
  const tipRect = tip.getBoundingClientRect();

  let left = anchorRect.left + anchorRect.width / 2 - tipRect.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - tipRect.width - 8));

  let top = anchorRect.top - tipRect.height - 10;
  if (top < 8) top = anchorRect.bottom + 10;

  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function renderTimelineMarkers(track, events, start, end) {
  track.innerHTML = "";
  if (!events.length) return;

  const width = track.clientWidth || 1;
  const spanMs = end.getTime() - start.getTime() || 1;

  const positioned = events.map((ev) => {
    const t = new Date(ev.timestamp).getTime();
    const frac = Math.min(1, Math.max(0, (t - start.getTime()) / spanMs));
    return { ...ev, x: frac * width };
  });

  const clusters = Dashboard.clusterMarkers(positioned);

  for (const cluster of clusters) {
    const isCluster = cluster.events.length > 1;
    const el = document.createElement("div");
    el.className = isCluster ? "timeline-marker cluster" : "timeline-marker";
    el.style.left = `${cluster.x}px`;
    el.style.background = isCluster
      ? "var(--muted)"
      : timelineStatusColor(cluster.events[0].new_status);
    el.tabIndex = 0;
    if (isCluster) el.textContent = String(cluster.events.length);

    el.addEventListener("mouseenter", () => showTimelineTooltip(cluster.events, el));
    el.addEventListener("mouseleave", hideTimelineTooltip);
    el.addEventListener("focus", () => showTimelineTooltip(cluster.events, el));
    el.addEventListener("blur", hideTimelineTooltip);

    track.appendChild(el);
  }
}

async function loadTimeline() {
  const track = document.getElementById("timeline-track");
  const empty = document.getElementById("timeline-empty");
  if (!track) return;

  updateTimelineRangeLabel();

  const start = timelineWindowStart();
  const end = timelineState.windowEnd;
  let events = [];

  try {
    const res = await fetch(
      `/api/events?start=${encodeURIComponent(start.toISOString())}` +
      `&end=${encodeURIComponent(end.toISOString())}`
    );
    events = await res.json();
  } catch (err) {
    console.warn("Timeline fetch failed:", err);
  }

  timelineCache = { events, start, end };
  renderTimelineMarkers(track, events, start, end);
  if (empty) empty.hidden = events.length > 0;
}

function panTimelineWindow(deltaMs) {
  const now = new Date();
  const proposed = new Date(timelineState.windowEnd.getTime() + deltaMs);
  if (proposed >= now) {
    timelineState.windowEnd = now;
    timelineState.live = true;
  } else {
    timelineState.windowEnd = proposed;
    timelineState.live = false;
  }
  loadTimeline();
}

function goLive() {
  timelineState.windowEnd = new Date();
  timelineState.live = true;
  loadTimeline();
}

function attachTimelineDrag(wrapper, track) {
  let dragging = false;
  let startX = 0;
  let startWindowEnd = null;

  function pxToMs(dxPx) {
    const width = wrapper.clientWidth || 1;
    return (dxPx / width) * TIMELINE_WINDOW_MS;
  }

  wrapper.addEventListener("pointerdown", (e) => {
    dragging = true;
    startX = e.clientX;
    startWindowEnd = timelineState.windowEnd;
    wrapper.classList.add("dragging");
    wrapper.setPointerCapture(e.pointerId);
  });

  wrapper.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    track.style.transform = `translateX(${dx}px)`;
    const now = new Date();
    const proposed = new Date(startWindowEnd.getTime() - pxToMs(dx));
    timelineState.windowEnd = proposed >= now ? now : proposed;
    timelineState.live = proposed >= now;
    updateTimelineRangeLabel();
  });

  function endDrag() {
    if (!dragging) return;
    dragging = false;
    wrapper.classList.remove("dragging");
    track.style.transform = "";
    loadTimeline();
  }

  wrapper.addEventListener("pointerup", endDrag);
  wrapper.addEventListener("pointercancel", endDrag);
}

function initTimeline() {
  const wrapper = document.getElementById("timeline-wrapper");
  const track = document.getElementById("timeline-track");
  if (!wrapper || !track) return;

  attachTimelineDrag(wrapper, track);

  document.getElementById("tl-back")?.addEventListener("click", () =>
    panTimelineWindow(-TIMELINE_WINDOW_MS)
  );
  document.getElementById("tl-forward")?.addEventListener("click", () =>
    panTimelineWindow(TIMELINE_WINDOW_MS)
  );
  document.getElementById("tl-now")?.addEventListener("click", goLive);

  window.addEventListener("resize", () => {
    if (timelineCache.start) {
      renderTimelineMarkers(track, timelineCache.events, timelineCache.start, timelineCache.end);
    }
  });

  loadTimeline();
  setInterval(() => {
    if (timelineState.live) {
      timelineState.windowEnd = new Date();
      loadTimeline();
    }
  }, TIMELINE_POLL_MS);
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
    buildChart("chart-ping",  "ping",      "latency_ms",    "#38bdf8", { clamp: true }),
    buildChart("chart-dns",   "dns",       "resolution_ms", "#a78bfa", { clamp: true }),
    buildChart("chart-speed", "speedtest", "download_mbps", "#34d399"),
  ]);

  initTimeline();
}

document.addEventListener("DOMContentLoaded", init);
