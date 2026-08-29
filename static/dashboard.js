const cards = new Map();
const waterfalls = document.querySelector("#waterfalls");
const template = document.querySelector("#waterfallTemplate");
const receiverText = document.querySelector("#receiverText");
const gainText = document.querySelector("#gainText");
const statsText = document.querySelector("#statsText");
const peakLog = document.querySelector("#peakLog");
const processLog = document.querySelector("#processLog");
const sourceText = document.querySelector("#sourceText");
const frequencyText = document.querySelector("#frequencyText");
const hardwareFreqText = document.querySelector("#hardwareFreqText");
const inputFreqText = document.querySelector("#inputFreqText");
const offsetText = document.querySelector("#offsetText");
const displayState = new Map();
const waterfallState = new Map();
const lastSeq = new Map();
const maxWaterfallRows = 300;

function makeCard(band) {
  const fragment = template.content.cloneNode(true);
  const card = fragment.querySelector(".waterfall-card");
  card.dataset.key = band.key;
  card.querySelector('[data-field="low"]').textContent = `${band.low_mhz} MHz`;
  card.querySelector('[data-field="high"]').textContent = `${band.high_mhz} MHz`;
  card.querySelector('[data-field="range"]').textContent = band.range;
  card.querySelector('[data-field="markers"]').textContent =
    `Markers: ${band.markers_mhz.map((f) => `${f} MHz`).join(" / ")}`;
  waterfalls.appendChild(card);
  cards.set(band.key, card);
  return card;
}

function colorForPower(value) {
  const v = Math.max(0, Math.min(1, value));
  const r = v > 0.68 ? Math.round(255 * v) : Math.round(22 + 20 * v);
  const g = v > 0.52 ? Math.round(215 * v) : Math.round(42 + 70 * v);
  const b = v > 0.75 ? Math.round(30 * (1 - v)) : Math.round(120 + 125 * (1 - v));
  return `rgb(${r}, ${g}, ${b})`;
}

function drawWaterfall(canvas, rows) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);

  const rowHeight = height / rows.length;
  const colWidth = width / rows[0].length;

  rows.forEach((row, y) => {
    row.forEach((value, x) => {
      ctx.fillStyle = colorForPower(value);
      ctx.fillRect(x * colWidth, y * rowHeight, Math.ceil(colWidth), Math.ceil(rowHeight));
    });
  });

  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i += 1) {
    const x = (width / 4) * i;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }
}

function updateWaterfallRows(band) {
  let rows = waterfallState.get(band.key);
  if (!rows) {
    const width = (band.waterfall_row || band.waveform || []).length || 1024;
    rows = Array.from({ length: maxWaterfallRows }, () => Array(width).fill(0));
    waterfallState.set(band.key, rows);
  }

  if (lastSeq.get(band.key) !== band.row_seq) {
    const row = band.waterfall_row || band.waveform || [];
    if (row.length) {
      rows.push(row);
      while (rows.length > maxWaterfallRows) rows.shift();
    }
    lastSeq.set(band.key, band.row_seq);
  }
  return rows;
}

function smoothRow(key, row) {
  if (!row || !row.length) return [];
  const previous = displayState.get(key);
  if (!previous || previous.length !== row.length) {
    displayState.set(key, row.slice());
    return row;
  }
  const alpha = 0.28;
  const next = row.map((value, index) => previous[index] * (1 - alpha) + value * alpha);
  displayState.set(key, next);
  return next;
}

function drawWaveform(canvas, band) {
  const row = smoothRow(band.key, band.waveform || []);
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#05090c";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(255,255,255,0.14)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i += 1) {
    const y = (height / 4) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
    ctx.stroke();
  }

  if (!row.length) return;

  ctx.strokeStyle = "#e9edf0";
  ctx.lineWidth = 1;
  ctx.beginPath();
  row.forEach((value, index) => {
    const x = (index / Math.max(1, row.length - 1)) * width;
    const y = height - Math.max(0, Math.min(1, value)) * height;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = "rgba(255, 209, 102, 0.85)";
  band.peaks.slice(0, 4).forEach((peak) => {
    const x = ((peak.freq_mhz - band.low_mhz) / (band.high_mhz - band.low_mhz)) * width;
    ctx.fillRect(x - 2, 0, 4, height);
  });
}

function updateCard(band, activeKey) {
  const card = cards.get(band.key) || makeCard(band);
  card.classList.toggle("active", band.key === activeKey);
  card.classList.toggle("suspicious", band.peaks.length > 0);
  card.querySelector('[data-field="noise"]').textContent = `Noise: ${band.noise_floor_db} dB`;
  card.querySelector('[data-field="peak"]').textContent = `Peak: ${band.peak_power_db} dB`;
  const spectrumCanvas = card.querySelector(".spectrum-canvas");
  const waterfallCanvas = card.querySelector(".waterfall-canvas");
  const rows = updateWaterfallRows(band);
  drawWaveform(spectrumCanvas, band);
  drawMarkers(spectrumCanvas, band);
  drawWaterfall(waterfallCanvas, rows);
  drawMarkers(waterfallCanvas, band);
}

function drawMarkers(canvas, band) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.save();
  ctx.strokeStyle = "rgba(255, 209, 102, 0.72)";
  ctx.fillStyle = "rgba(255, 209, 102, 0.9)";
  ctx.font = "18px sans-serif";
  band.markers_mhz.forEach((freq) => {
    if (freq < band.low_mhz || freq > band.high_mhz) return;
    const x = ((freq - band.low_mhz) / (band.high_mhz - band.low_mhz)) * width;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.fillText(`${freq}`, x + 4, 22);
  });
  ctx.restore();
}

function updateHeader(state) {
  sourceText.textContent = state.source;
  receiverText.textContent = state.mode === "live" ? "HackRF Live" : "Offline";
  gainText.textContent = state.mode === "live" ? "Live spectrum monitor" : "Run with --live";
  const stats = state.stats || {};
  statsText.textContent = `${stats.fft_frames || stats.sweep_lines || 0} frames / ${stats.rows_published || 0} rows`;
  const band = state.bands[0];
  if (band) {
    const center = (band.low_mhz + band.high_mhz) / 2;
    frequencyText.textContent = formatLargeFrequency(center);
    hardwareFreqText.textContent = `${center.toFixed(6)} MHz`;
    inputFreqText.textContent = `${(center * 1000).toFixed(3)} kHz`;
    offsetText.textContent = "0.000 kHz";
  }
}

function formatLargeFrequency(freqMhz) {
  const hz = Math.round(freqMhz * 1_000_000).toString().padStart(10, "0");
  return `${hz.slice(0, 1)}.${hz.slice(1, 4)}.${hz.slice(4, 7)}.${hz.slice(7)}`;
}

function updatePeakLog(state) {
  const peaks = [];
  state.bands.forEach((band) => {
    band.peaks.forEach((peak) => peaks.push({ band: band.label, ...peak }));
  });

  if (!peaks.length) {
    peakLog.innerHTML = '<div class="event-item"><span>No strong peaks in current view.</span><span>monitoring</span></div>';
    return;
  }

  peakLog.innerHTML = "";
  peaks.slice(0, 10).forEach((peak) => {
    const item = document.createElement("div");
    item.className = "event-item";
    item.innerHTML = `<span>${peak.band}: ${peak.freq_mhz} MHz</span><span>${Math.round(peak.strength * 100)}%</span>`;
    peakLog.appendChild(item);
  });
}

function updateProcessLog(state) {
  const logs = state.logs || [];
  if (!logs.length) {
    processLog.innerHTML = '<div class="log-item"><span>waiting for receiver events</span></div>';
    return;
  }
  processLog.innerHTML = "";
  logs.slice(0, 18).forEach((entry) => {
    const item = document.createElement("div");
    item.className = `log-item ${entry.level}`;
    const fields = Object.entries(entry)
      .filter(([key]) => !["ts", "level", "message"].includes(key))
      .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : value}`)
      .join(" / ");
    item.innerHTML = `<span>${entry.ts} ${entry.level.toUpperCase()} ${entry.message}</span><small>${fields}</small>`;
    processLog.appendChild(item);
  });
}

let isPolling = false;

async function pollState() {
  if (isPolling) return;
  isPolling = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const state = await response.json();
    state.bands.forEach((band) => updateCard(band, state.active_band));
    updateHeader(state);
    updatePeakLog(state);
    updateProcessLog(state);
  } catch (error) {
    receiverText.textContent = "Disconnected";
    gainText.textContent = "waiting for backend";
  } finally {
    isPolling = false;
  }
}

function frameLoop() {
  pollState();
  requestAnimationFrame(frameLoop);
}

frameLoop();
