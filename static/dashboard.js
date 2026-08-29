const cards = new Map();
const waterfalls = document.querySelector("#waterfalls");
const template = document.querySelector("#waterfallTemplate");
const receiverText = document.querySelector("#receiverText");
const gainText = document.querySelector("#gainText");
const statsText = document.querySelector("#statsText");
const peakLog = document.querySelector("#peakLog");
const processLog = document.querySelector("#processLog");
const sourceText = document.querySelector("#sourceText");

function makeCard(band) {
  const fragment = template.content.cloneNode(true);
  const card = fragment.querySelector(".waterfall-card");
  card.dataset.key = band.key;
  card.querySelector('[data-field="label"]').textContent = band.label;
  card.querySelector('[data-field="antenna"]').textContent = band.antenna;
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
  const r = v > 0.72 ? Math.round(255 * v) : Math.round(40 * v);
  const g = v > 0.45 ? Math.round(230 * v) : Math.round(95 + 120 * v);
  const b = v > 0.7 ? Math.round(60 * (1 - v)) : Math.round(120 + 110 * (1 - v));
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

function drawWaveform(canvas, band) {
  const row = band.waveform || [];
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

  ctx.strokeStyle = "#48cae4";
  ctx.lineWidth = 2;
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
  card.querySelector('[data-field="status"]').textContent =
    band.key === activeKey ? "updating" : band.status;
  card.querySelector('[data-field="noise"]').textContent = `Noise: ${band.noise_floor_db} dB`;
  card.querySelector('[data-field="peak"]').textContent = `Peak: ${band.peak_power_db} dB`;
  const spectrumCanvas = card.querySelector(".spectrum-canvas");
  const waterfallCanvas = card.querySelector(".waterfall-canvas");
  drawWaveform(spectrumCanvas, band);
  drawMarkers(spectrumCanvas, band);
  drawWaterfall(waterfallCanvas, band.rows);
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
  statsText.textContent = `${stats.sweep_lines || 0} sweep lines / ${stats.rows_published || 0} waterfall rows`;
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
