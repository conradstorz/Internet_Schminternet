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

    // The address itself is not in /api/status — metrics rows have no message
    // column — so read it from the state the ip monitor keeps.
    const ipRes = await fetch("/api/ip");
    const { ip } = await ipRes.json();
    const ipVal = document.getElementById("val-ip");
    if (ipVal && ip) ipVal.textContent = ip;

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

/**
 * Downsample a time series into evenly-spaced time buckets so a wide window
 * of dense samples (e.g. 24 h of ping data) can be plotted legibly. Pure —
 * no DOM.
 *
 * Buckets span the points' own [min x, max x] range, divided into `buckets`
 * equal-width intervals. Each non-empty bucket becomes one output point at
 * the bucket's midpoint, carrying the bucket's mean (`y`) and its max value
 * (`max`) plus the timestamp that max occurred at (`maxX`) — so a spike can
 * still be flagged and its true time reported even after aggregation.
 * Empty buckets produce no point, leaving an honest gap in the line.
 *
 *   bucketSeries(points, { buckets }) -> [{ x, y, min, max, maxX, count }]
 */
function bucketSeries(points, { buckets = 180 } = {}) {
  if (!points.length) return [];

  const toMs = (x) => (x instanceof Date ? x.getTime() : x);
  const xs = points.map((p) => toMs(p.x));
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const span = maxX - minX || 1;
  const width = span / buckets;

  const bins = new Array(buckets);
  for (let i = 0; i < points.length; i++) {
    const t = xs[i];
    const y = points[i].y;
    let idx = Math.floor((t - minX) / width);
    if (idx >= buckets) idx = buckets - 1;
    if (idx < 0) idx = 0;

    let bin = bins[idx];
    if (!bin) {
      bin = { sum: 0, count: 0, min: Infinity, max: -Infinity, maxT: t };
      bins[idx] = bin;
    }
    bin.sum += y;
    bin.count += 1;
    if (y < bin.min) bin.min = y;
    if (y > bin.max) {
      bin.max = y;
      bin.maxT = t;
    }
  }

  const result = [];
  for (let i = 0; i < buckets; i++) {
    const bin = bins[i];
    if (!bin) continue; // empty bucket -> honest gap, no point emitted
    const center = minX + (i + 0.5) * width;
    result.push({
      x: new Date(center),
      y: bin.sum / bin.count,
      min: bin.min,
      max: bin.max,
      maxX: new Date(bin.maxT),
      count: bin.count,
    });
  }
  return result;
}

/**
 * Build a density-heatmap grid from raw (unbucketed) samples across all
 * targets combined. Time is divided into `cols` equal-width columns
 * (matching the series' own [min x, max x] range); value is divided into
 * `rows` equal-height bands from 0 to the data's max value. Each occupied
 * cell becomes one entry carrying its sample count; `maxCount` is the
 * busiest cell's count, for callers to derive colour intensity from. Pure —
 * no DOM, no Chart.js.
 *
 *   heatmapGrid(points, { cols, rows })
 *   -> { cells: [{ x, y, x0, x1, y0, y1, count }], maxCount, xMin, xMax, yMax }
 */
function heatmapGrid(points, { cols = 60, rows = 12 } = {}) {
  if (!points.length) return { cells: [], maxCount: 0, xMin: 0, xMax: 0, yMax: 0 };

  const toMs = (x) => (x instanceof Date ? x.getTime() : x);
  const xs = points.map((p) => toMs(p.x));
  const ys = points.map((p) => p.y);
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMax = Math.max(...ys) || 1;
  const xSpan = xMax - xMin || 1;
  const colWidth = xSpan / cols;
  const rowHeight = yMax / rows;

  const grid = new Map(); // "col,row" -> count
  for (let i = 0; i < points.length; i++) {
    let col = Math.floor((xs[i] - xMin) / colWidth);
    let row = Math.floor(ys[i] / rowHeight);
    if (col >= cols) col = cols - 1;
    if (col < 0) col = 0;
    if (row >= rows) row = rows - 1;
    if (row < 0) row = 0;
    const key = `${col},${row}`;
    grid.set(key, (grid.get(key) || 0) + 1);
  }

  let maxCount = 0;
  const cells = [];
  for (const [key, count] of grid) {
    const [col, row] = key.split(",").map(Number);
    if (count > maxCount) maxCount = count;
    cells.push({
      x: new Date(xMin + (col + 0.5) * colWidth),
      y: (row + 0.5) * rowHeight,
      x0: new Date(xMin + col * colWidth),
      x1: new Date(xMin + (col + 1) * colWidth),
      y0: row * rowHeight,
      y1: (row + 1) * rowHeight,
      count,
    });
  }
  return { cells, maxCount, xMin, xMax, yMax };
}

const Dashboard = { percentile, clampSeries, clusterMarkers, bucketSeries, heatmapGrid };
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

/**
 * Colours for a multi-target chart. Targets are user-configurable, so
 * colours are drawn from a small fixed palette by index rather than
 * hard-coded per known host — chosen to stay distinguishable on the dark
 * dashboard background.
 */
const SERIES_PALETTE = [
  "#38bdf8", // sky
  "#f472b6", // pink
  "#34d399", // green
  "#fbbf24", // amber
  "#a78bfa", // violet
  "#22d3ee", // cyan
  "#f97316", // orange
];

/** Roughly one plotted point per 2-3 px, clamped to a sane range. */
function chartBucketCount(pixelWidth) {
  return Math.max(50, Math.min(300, Math.round(pixelWidth / 2.5)));
}

async function buildChart(canvasId, monitor, metric, color) {
  try {
    const res  = await fetch(`/api/metrics/${monitor}?hours=24`);
    const rows = await res.json();

    const filtered = rows.filter((r) => r.metric === metric && r.value >= 0);
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const raw = filtered.map((r) => ({ x: new Date(r.timestamp), y: r.value }));
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
  } catch (err) {
    console.warn(`Chart ${canvasId} failed to load:`, err);
  }
}

// ---------------------------------------------------------------------------
// Ping / DNS multi-target charts — switchable rendering styles
//
// Data is fetched and bucketed once per chart and cached in `chartCaches`;
// the per-chart style control re-renders from that cache without refetching.
// ---------------------------------------------------------------------------

const CLIP_COLOR = "#ef4444"; // var(--down)

const CHART_STYLES = [
  { id: "clamped", label: "Clamped" },
  { id: "log",     label: "Log" },
  { id: "band",    label: "Min–Max" },
  { id: "heatmap", label: "Heatmap" },
  { id: "scatter", label: "Scatter" },
];
const DEFAULT_CHART_STYLE = "clamped";
const CHART_STYLE_STORAGE_PREFIX = "schminternet.chartStyle.";

/** Cached fetched/bucketed data per canvas id, so switching styles never re-fetches. */
const chartCaches = {};

function isKnownChartStyle(style) {
  return CHART_STYLES.some((s) => s.id === style);
}

/** Reads the saved style for a chart; falls back to the default if storage is
 * unavailable or holds a value that no longer names a real style. */
function getStoredChartStyle(canvasId) {
  try {
    const stored = window.localStorage.getItem(CHART_STYLE_STORAGE_PREFIX + canvasId);
    if (isKnownChartStyle(stored)) return stored;
  } catch {
    // Storage blocked (private mode, disabled cookies, etc.) — use default.
  }
  return DEFAULT_CHART_STYLE;
}

function setStoredChartStyle(canvasId, style) {
  try {
    window.localStorage.setItem(CHART_STYLE_STORAGE_PREFIX + canvasId, style);
  } catch {
    // Persistence is a nicety; losing it shouldn't affect rendering.
  }
}

function clippedTooltipLabel(ctx) {
  const p = ctx.raw;
  const name = ctx.dataset.label;
  const time = new Date(p.trueTime).toLocaleTimeString([], { hour12: false });
  return p.clipped
    ? `${name}: ${Math.round(p.trueValue)} ms — ${time}`
    : `${name}: ${Math.round(p.trueValue)} ms`;
}

const LEGEND_LABEL_OPTS = { color: "#94a3b8", boxWidth: 12, font: { size: 11 } };

/** Style 1 (default): bucketed per-target lines, clamped to a shared p95
 * ceiling, over-ceiling buckets pinned at the ceiling as red markers. */
function buildClampedChart(canvas, cache) {
  const datasets = cache.targets.map((target, i) => {
    const seriesColor = SERIES_PALETTE[i % SERIES_PALETTE.length];
    const buckets = Dashboard.bucketSeries(cache.byTarget.get(target), { buckets: cache.bucketCount });
    const points = buckets.map((b) => {
      const clipped = b.max > cache.ceiling;
      return {
        x: b.x,
        y: clipped ? cache.ceiling : b.y,
        clipped,
        trueValue: clipped ? b.max : b.y,
        trueTime: clipped ? b.maxX : b.x,
      };
    });
    return {
      label: target,
      data: points,
      borderColor: seriesColor,
      backgroundColor: seriesColor + "25",
      borderWidth: 1.5,
      pointRadius: points.map((p) => (p.clipped ? 4 : 0)),
      pointBackgroundColor: points.map((p) => (p.clipped ? CLIP_COLOR : seriesColor)),
      pointBorderColor: points.map((p) => (p.clipped ? CLIP_COLOR : seriesColor)),
      fill: false,
      tension: 0.3,
    };
  });

  const options = {
    ...CHART_BASE_OPTIONS,
    scales: {
      ...CHART_BASE_OPTIONS.scales,
      y: { beginAtZero: true, max: cache.ceiling, ticks: { color: "#94a3b8" }, grid: { color: "#334155" } },
    },
    plugins: {
      legend: { display: true, labels: LEGEND_LABEL_OPTS },
      tooltip: { callbacks: { label: clippedTooltipLabel } },
    },
  };

  new Chart(canvas, { type: "line", data: { datasets }, options });
}

/** Style 2: true bucket means on a log y-axis, no ceiling, no clipping.
 * Non-positive values are dropped — a log axis cannot plot them. */
function buildLogChart(canvas, cache) {
  const datasets = cache.targets.map((target, i) => {
    const seriesColor = SERIES_PALETTE[i % SERIES_PALETTE.length];
    const buckets = Dashboard.bucketSeries(cache.byTarget.get(target), { buckets: cache.bucketCount });
    const points = buckets.filter((b) => b.y > 0).map((b) => ({ x: b.x, y: b.y }));
    return {
      label: target,
      data: points,
      borderColor: seriesColor,
      backgroundColor: seriesColor + "25",
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      tension: 0.3,
    };
  });

  const options = {
    ...CHART_BASE_OPTIONS,
    scales: {
      ...CHART_BASE_OPTIONS.scales,
      y: { type: "logarithmic", ticks: { color: "#94a3b8" }, grid: { color: "#334155" } },
    },
    plugins: {
      legend: { display: true, labels: LEGEND_LABEL_OPTS },
      tooltip: {
        callbacks: {
          label: (ctx) => `${ctx.dataset.label}: ${Math.round(ctx.raw.y)} ms`,
        },
      },
    },
  };

  new Chart(canvas, { type: "line", data: { datasets }, options });
}

/** Style 3: per-bucket mean line inside a shaded min–max band. Each target
 * contributes a hidden min line, a fill-to-min max line (the band), and the
 * visible mean line; the legend filter keeps only the mean line's entry so
 * the legend still reads one item per target. Uses the clamped ceiling. */
function buildBandChart(canvas, cache) {
  const datasets = [];
  cache.targets.forEach((target, i) => {
    const seriesColor = SERIES_PALETTE[i % SERIES_PALETTE.length];
    const buckets = Dashboard.bucketSeries(cache.byTarget.get(target), { buckets: cache.bucketCount });
    const clampVal = (v) => Math.min(v, cache.ceiling);
    const minPoints  = buckets.map((b) => ({ x: b.x, y: clampVal(b.min) }));
    const maxPoints  = buckets.map((b) => ({ x: b.x, y: clampVal(b.max) }));
    const meanPoints = buckets.map((b) => ({ x: b.x, y: clampVal(b.y) }));

    const minIndex = datasets.length;
    datasets.push({
      label: target,
      data: minPoints,
      borderColor: "transparent",
      backgroundColor: "transparent",
      borderWidth: 0,
      pointRadius: 0,
      fill: false,
      tension: 0.3,
      isBand: true,
    });
    datasets.push({
      label: target,
      data: maxPoints,
      borderColor: "transparent",
      backgroundColor: seriesColor + "30",
      borderWidth: 0,
      pointRadius: 0,
      fill: minIndex,
      tension: 0.3,
      isBand: true,
    });
    datasets.push({
      label: target,
      data: meanPoints,
      borderColor: seriesColor,
      backgroundColor: seriesColor + "25",
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      tension: 0.3,
    });
  });

  const options = {
    ...CHART_BASE_OPTIONS,
    scales: {
      ...CHART_BASE_OPTIONS.scales,
      y: { beginAtZero: true, max: cache.ceiling, ticks: { color: "#94a3b8" }, grid: { color: "#334155" } },
    },
    plugins: {
      legend: {
        display: true,
        labels: {
          ...LEGEND_LABEL_OPTS,
          filter: (item, data) => !data.datasets[item.datasetIndex].isBand,
        },
      },
      tooltip: {
        filter: (item) => !item.dataset.isBand,
        callbacks: {
          label: (ctx) => `${ctx.dataset.label}: ${Math.round(ctx.raw.y)} ms (mean)`,
        },
      },
    },
  };

  new Chart(canvas, { type: "line", data: { datasets }, options });
}

/** Cell/point sizing for the heatmap: enough columns/rows to read as a
 * texture at this card's size, without so many cells the squares vanish. */
function heatmapDimensions(pixelWidth, pixelHeight) {
  const cols = Math.max(20, Math.min(90, Math.round(pixelWidth / 10)));
  const rows = Math.max(8, Math.min(16, Math.round(pixelHeight / 14)));
  return { cols, rows };
}

/** Style 4: density heatmap built on Chart.js's scatter type — one square
 * point per occupied (time, latency-band) cell, sized from the cell's pixel
 * geometry, alpha from the cell's share of the busiest cell. All targets are
 * combined into a single grid (see the card note this style turns on). */
function buildHeatmapChart(canvas, cache) {
  const wrapperWidth  = canvas.parentElement?.clientWidth  || cache.wrapperWidth || 400;
  const wrapperHeight = canvas.parentElement?.clientHeight || 190;
  const { cols, rows } = heatmapDimensions(wrapperWidth, wrapperHeight);
  const { cells, maxCount, yMax } = Dashboard.heatmapGrid(cache.allPoints, { cols, rows });

  // Leave room for axis labels when converting cell geometry to point radius.
  const plotWidth  = Math.max(1, wrapperWidth - 50);
  const plotHeight = Math.max(1, wrapperHeight - 40);
  const pointRadius = Math.max(2, Math.min(plotWidth / cols, plotHeight / rows) / 2 - 1);

  const HEAT_RGB = "56, 189, 248"; // sky, blended by alpha below

  const data = cells.map((c) => {
    const share = maxCount ? c.count / maxCount : 0;
    const alpha = 0.15 + 0.85 * share;
    return { x: c.x, y: c.y, x0: c.x0, x1: c.x1, y0: c.y0, y1: c.y1, count: c.count, alpha };
  });

  const dataset = {
    label: "All targets",
    data,
    showLine: false,
    pointStyle: "rect",
    pointRadius,
    pointHoverRadius: pointRadius + 1,
    backgroundColor: data.map((d) => `rgba(${HEAT_RGB}, ${d.alpha.toFixed(3)})`),
    borderWidth: 0,
  };

  const options = {
    ...CHART_BASE_OPTIONS,
    scales: {
      ...CHART_BASE_OPTIONS.scales,
      y: { beginAtZero: true, max: yMax, ticks: { color: "#94a3b8" }, grid: { color: "#334155" } },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label(ctx) {
            const p = ctx.raw;
            const fmt = (t) => new Date(t).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" });
            return `${p.count} samples, ${Math.round(p.y0)}–${Math.round(p.y1)} ms, ${fmt(p.x0)}–${fmt(p.x1)}`;
          },
        },
      },
    },
  };

  new Chart(canvas, { type: "scatter", data: { datasets: [dataset] }, options });
}

/** Style 5: one dot per bucket, no connecting line and no fill, so gaps in
 * the data stay visible gaps. Uses the clamped ceiling and clipped markers,
 * same as style 1. */
function buildScatterChart(canvas, cache) {
  const datasets = cache.targets.map((target, i) => {
    const seriesColor = SERIES_PALETTE[i % SERIES_PALETTE.length];
    const buckets = Dashboard.bucketSeries(cache.byTarget.get(target), { buckets: cache.bucketCount });
    const points = buckets.map((b) => {
      const clipped = b.max > cache.ceiling;
      return {
        x: b.x,
        y: clipped ? cache.ceiling : b.y,
        clipped,
        trueValue: clipped ? b.max : b.y,
        trueTime: clipped ? b.maxX : b.x,
      };
    });
    return {
      label: target,
      data: points,
      showLine: false,
      fill: false,
      pointBackgroundColor: points.map((p) => (p.clipped ? CLIP_COLOR : seriesColor)),
      pointBorderColor: points.map((p) => (p.clipped ? CLIP_COLOR : seriesColor)),
      pointRadius: points.map((p) => (p.clipped ? 5 : 3)),
    };
  });

  const options = {
    ...CHART_BASE_OPTIONS,
    scales: {
      ...CHART_BASE_OPTIONS.scales,
      y: { beginAtZero: true, max: cache.ceiling, ticks: { color: "#94a3b8" }, grid: { color: "#334155" } },
    },
    plugins: {
      legend: { display: true, labels: LEGEND_LABEL_OPTS },
      tooltip: { callbacks: { label: clippedTooltipLabel } },
    },
  };

  new Chart(canvas, { type: "scatter", data: { datasets }, options });
}

const CHART_STYLE_BUILDERS = {
  clamped: buildClampedChart,
  log:     buildLogChart,
  band:    buildBandChart,
  heatmap: buildHeatmapChart,
  scatter: buildScatterChart,
};

/** Only the heatmap combines all targets into one grid; every other style
 * keeps the per-target legend, so only the heatmap needs the card note. */
function updateChartNote(canvasId, style) {
  const suffix = canvasId.replace("chart-", "");
  const note = document.getElementById(`note-${suffix}`);
  if (!note) return;
  if (style === "heatmap") {
    note.textContent = "All targets combined into one grid — this view shows the distribution over time, not host-vs-host.";
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

function setActiveStyleButton(canvasId, style) {
  const suffix = canvasId.replace("chart-", "");
  const container = document.getElementById(`style-${suffix}`);
  if (!container) return;
  container.querySelectorAll("button[data-style]").forEach((btn) => {
    const active = btn.dataset.style === style;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

/** Destroys any existing Chart instance on the canvas and rebuilds it in the
 * requested style from the cached data — never re-fetches. */
function renderChartStyle(canvasId, style) {
  const cache = chartCaches[canvasId];
  const canvas = document.getElementById(canvasId);
  if (!cache || !canvas) return;

  const resolvedStyle = isKnownChartStyle(style) ? style : DEFAULT_CHART_STYLE;

  Chart.getChart(canvas)?.destroy();
  CHART_STYLE_BUILDERS[resolvedStyle](canvas, cache);

  updateChartNote(canvasId, resolvedStyle);
  setActiveStyleButton(canvasId, resolvedStyle);
  setStoredChartStyle(canvasId, resolvedStyle);
}

function initStyleControl(canvasId) {
  const suffix = canvasId.replace("chart-", "");
  const container = document.getElementById(`style-${suffix}`);
  if (!container) return;
  container.querySelectorAll("button[data-style]").forEach((btn) => {
    btn.addEventListener("click", () => renderChartStyle(canvasId, btn.dataset.style));
  });
  // Reflect the persisted (or default) choice immediately, even before the
  // data fetch that renderChartStyle needs has resolved.
  setActiveStyleButton(canvasId, getStoredChartStyle(canvasId));
}

/** Fetches and buckets a ping/DNS monitor's last 24 h once, caches it, and
 * renders the persisted (or default) style. Re-render on style change reads
 * from `chartCaches` — see `renderChartStyle`. */
async function initStyledChart(canvasId, monitor, metric) {
  try {
    const res  = await fetch(`/api/metrics/${monitor}?hours=24`);
    const rows = await res.json();

    const filtered = rows.filter((r) => r.metric === metric && r.value >= 0);
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    // One series per target (rows interleave targets, so a single merged
    // series would zig-zag between hosts).
    const byTarget = new Map();
    for (const r of filtered) {
      const list = byTarget.get(r.target) || [];
      list.push({ x: new Date(r.timestamp), y: r.value });
      byTarget.set(r.target, list);
    }
    const targets = [...byTarget.keys()].sort();

    // Ceiling computed across all targets together so every series shares
    // one comparable y-axis in the styles that use it.
    const allPoints = filtered.map((r) => ({ x: new Date(r.timestamp), y: r.value }));
    const { ceiling } = Dashboard.clampSeries(allPoints);

    const wrapperWidth = canvas.parentElement?.clientWidth || 400;
    const bucketCount = chartBucketCount(wrapperWidth);

    chartCaches[canvasId] = { targets, byTarget, allPoints, ceiling, bucketCount, wrapperWidth };

    renderChartStyle(canvasId, getStoredChartStyle(canvasId));
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

/** Hour tick marks + labels along the bottom of the timeline strip. */
function renderTimelineTicks(ticksEl, start, end) {
  if (!ticksEl) return;
  ticksEl.innerHTML = "";

  const width = ticksEl.clientWidth || 1;
  const spanMs = end.getTime() - start.getTime() || 1;
  const TICK_SPACING_MS = 2 * 60 * 60 * 1000; // every 2 h, so labels don't collide at this width

  // First tick at the earliest even hour >= start.
  const first = new Date(start);
  first.setMinutes(0, 0, 0);
  if (first < start) first.setHours(first.getHours() + 1);
  if (first.getHours() % 2 !== 0) first.setHours(first.getHours() + 1);

  for (let t = first.getTime(); t <= end.getTime(); t += TICK_SPACING_MS) {
    const frac = (t - start.getTime()) / spanMs;
    if (frac < 0 || frac > 1) continue;
    const x = frac * width;

    const tick = document.createElement("div");
    tick.className = "timeline-tick";
    tick.style.left = `${x}px`;
    ticksEl.appendChild(tick);

    const label = document.createElement("div");
    label.className = "timeline-tick-label";
    label.style.left = `${x}px`;
    label.textContent = new Date(t).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    ticksEl.appendChild(label);
  }
}

async function loadTimeline() {
  const track = document.getElementById("timeline-track");
  const ticks = document.getElementById("timeline-ticks");
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
  renderTimelineTicks(ticks, start, end);
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
      renderTimelineTicks(document.getElementById("timeline-ticks"), timelineCache.start, timelineCache.end);
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

  initStyleControl("chart-ping");
  initStyleControl("chart-dns");

  // Charts load in parallel — failures are non-fatal
  await Promise.allSettled([
    initStyledChart("chart-ping", "ping", "latency_ms"),
    initStyledChart("chart-dns",  "dns",  "resolution_ms"),
    buildChart("chart-speed", "speedtest", "download_mbps", "#34d399"),
  ]);

  initTimeline();
}

document.addEventListener("DOMContentLoaded", init);
