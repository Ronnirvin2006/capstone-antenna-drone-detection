const cards = new Map();
const waterfalls = document.querySelector("#waterfalls");
const template = document.querySelector("#waterfallTemplate");
const alertPanel = document.querySelector("#alertPanel");
const alertText = document.querySelector("#alertText");
const countText = document.querySelector("#countText");
const eventLog = document.querySelector("#eventLog");

function makeCard(band) {
  const fragment = template.content.cloneNode(true);
  const card = fragment.querySelector(".waterfall-card");
  card.dataset.key = band.key;
  card.querySelector('[data-field="label"]').textContent = band.label;
  card.querySelector('[data-field="antenna"]').textContent = band.antenna;
  card.querySelector('[data-field="low"]').textContent = `${band.low_mhz} MHz`;
  card.querySelector('[data-field="high"]').textContent = `${band.high_mhz} MHz`;
  card.querySelector('[data-field="range"]').textContent = band.range;
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

function updateCard(band, activeKey) {
  const card = cards.get(band.key) || makeCard(band);
  card.classList.toggle("active", band.key === activeKey);
  card.classList.toggle("suspicious", band.suspicious);
  card.querySelector('[data-field="status"]').textContent =
    band.key === activeKey ? "live scan" : band.status;
  card.querySelector('[data-field="mux"]').textContent = `MUX ${band.mux_port}`;
  card.querySelector('[data-field="peaks"]').textContent =
    band.peaks.length ? `${band.peaks.length} peaks` : "no peaks";
  drawWaterfall(card.querySelector("canvas"), band.rows);
}

function updateAlert(state) {
  alertPanel.classList.toggle("danger", state.alert);
  alertPanel.classList.toggle("quiet", !state.alert);
  alertText.textContent = state.alert ? "Drone-like RF activity" : "Scanning";
  countText.textContent = `${state.detected_count} suspected drone${state.detected_count === 1 ? "" : "s"}`;

  if (!state.detections.length) {
    eventLog.innerHTML = '<div class="event-item"><span>No suspicious RF pattern currently detected.</span><span>live</span></div>';
    return;
  }

  eventLog.innerHTML = "";
  state.detections.forEach((detection) => {
    const item = document.createElement("div");
    item.className = "event-item";
    const peaks = detection.peaks.map((peak) => `${peak.freq_mhz} MHz`).join(", ");
    item.innerHTML = `<span>${detection.antenna}: ${peaks}</span><span>${detection.estimated_drones} estimated</span>`;
    eventLog.appendChild(item);
  });
}

async function pollState() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const state = await response.json();
    state.bands.forEach((band) => updateCard(band, state.active_antenna));
    updateAlert(state);
  } catch (error) {
    alertText.textContent = "Dashboard disconnected";
    countText.textContent = "waiting for backend";
  }
}

pollState();
setInterval(pollState, 650);
