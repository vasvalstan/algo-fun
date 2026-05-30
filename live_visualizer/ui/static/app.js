const fmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qtyFmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 6, maximumFractionDigits: 8 });

const canvas = document.getElementById("chart");
const ctx = canvas.getContext("2d");

// ── State ──────────────────────────────────────────────────────────────────
let candles = [];       // [{time,open,high,low,close,volume,closed}]
let book = { bid: 0, ask: 0 };
let feedConnected = false;
let botState = {};
let stateSource = {};
let symbol = "BTCUSDC";
let interval = "1m";
let binanceWsBase = "wss://stream.binance.com/stream";
const CANDLE_LIMIT = 500;

// ── Canvas ─────────────────────────────────────────────────────────────────
function resize() {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  drawChart();
}
window.addEventListener("resize", resize);

// ── Binance REST seed ──────────────────────────────────────────────────────
async function seedCandles() {
  try {
    const url = `https://data-api.binance.vision/api/v3/klines?symbol=${symbol}&interval=${interval}&limit=${CANDLE_LIMIT}`;
    const resp = await fetch(url);
    const rows = await resp.json();
    candles = rows.map(r => ({
      time: Math.floor(r[0] / 1000),
      open: parseFloat(r[1]),
      high: parseFloat(r[2]),
      low: parseFloat(r[3]),
      close: parseFloat(r[4]),
      volume: parseFloat(r[5]),
      closed: true,
    }));
    drawChart();
  } catch (e) {
    console.warn("Candle seed failed:", e);
  }
}

// ── Binance WebSocket (browser → Binance directly) ─────────────────────────
function connectBinance() {
  const sym = symbol.toLowerCase();
  const streams = `${sym}@kline_${interval}/${sym}@bookTicker`;
  const url = `${binanceWsBase}?streams=${encodeURIComponent(streams)}`;
  const ws = new WebSocket(url);

  ws.onopen = () => { feedConnected = true; renderFeedStatus(); };
  ws.onclose = () => { feedConnected = false; renderFeedStatus(); setTimeout(connectBinance, 2000); };
  ws.onerror = () => ws.close();

  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    const data = msg.data || msg;
    const event = data.e;

    if (event === "kline" || data.k) {
      const k = data.k;
      const candle = {
        time: Math.floor(k.t / 1000),
        open: parseFloat(k.o),
        high: parseFloat(k.h),
        low: parseFloat(k.l),
        close: parseFloat(k.c),
        volume: parseFloat(k.v),
        closed: k.x,
      };
      updateCandle(candle);
      drawChart();
      renderPrices();
    } else if (event === "bookTicker" || (data.b && data.a)) {
      book = { bid: parseFloat(data.b), ask: parseFloat(data.a) };
      renderPrices();
    }
  };
}

function updateCandle(candle) {
  const last = candles[candles.length - 1];
  if (last && last.time === candle.time) {
    candles[candles.length - 1] = candle;
  } else {
    candles.push(candle);
    if (candles.length > CANDLE_LIMIT) candles.shift();
  }
}

// ── Server WebSocket (bot state) ───────────────────────────────────────────
function connectServer() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = e => {
    const payload = JSON.parse(e.data);
    botState = payload.bot_state || {};
    stateSource = payload.state_source || {};
    renderState();
    renderSource();
    drawChart();
  };
  ws.onclose = () => setTimeout(connectServer, 1000);
  ws.onerror = () => ws.close();
}

// ── Render helpers ─────────────────────────────────────────────────────────
function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function money(value) {
  const n = Number(value || 0);
  return n ? `$${fmt.format(n)}` : "--";
}

function pnlClass(value) {
  const n = Number(value || 0);
  return n > 0 ? "pos" : n < 0 ? "neg" : "";
}

function renderFeedStatus() {
  setText("feed", feedConnected ? "LIVE" : "RECONNECTING");
  document.getElementById("feed").className = feedConnected ? "pos" : "warn";
}

function renderPrices() {
  const lastCandle = candles[candles.length - 1];
  setText("last-price", lastCandle ? money(lastCandle.close) : "--");
  setText("bid", book.bid ? money(book.bid) : "--");
  setText("ask", book.ask ? money(book.ask) : "--");
}

function renderState() {
  const regime = botState.regime || "UNKNOWN";
  const regimeEl = document.getElementById("regime");
  regimeEl.textContent = regime;
  regimeEl.style.background = regime === "BULL" ? "#0ecb81" : regime === "BEAR" ? "#f6465d" : "#64748b";

  setText("mode", botState.current_mode || "--");
  setText("gear", botState.gear || "--");
  setText("cash", money(botState.cash));
  setText("inventory", botState.position_qty ? `${qtyFmt.format(botState.position_qty)} BTC` : "--");
  setText("realized", money(botState.pnl_realized));
  setText("unrealized", money(botState.pnl_unrealized));
  document.getElementById("realized").className = pnlClass(botState.pnl_realized);
  document.getElementById("unrealized").className = pnlClass(botState.pnl_unrealized);

  renderOrders(botState.active_orders || []);
  renderBags(botState.open_bags || []);
}

function renderOrders(orders) {
  const el = document.getElementById("orders");
  if (!orders.length) { el.className = "list empty"; el.textContent = "No active orders"; return; }
  el.className = "list";
  el.innerHTML = orders.map(o => `
    <div class="item">
      <div class="item-title">
        <span class="${o.side === "BUY" ? "pos" : "warn"}">${o.side}</span>
        <span>${money(o.price)}</span>
      </div>
      <div class="item-meta">${o.label || "order"} · ${qtyFmt.format(o.qty || 0)} BTC</div>
    </div>
  `).join("");
}

function renderBags(bags) {
  const el = document.getElementById("bags");
  if (!bags.length) { el.className = "list empty"; el.textContent = "No open bags"; return; }
  el.className = "list";
  el.innerHTML = bags.map(b => `
    <div class="item">
      <div class="item-title">
        <span>Bag ${b.id || ""}</span>
        <span>${money(b.entry_price)}</span>
      </div>
      <div class="item-meta">
        ${qtyFmt.format(b.qty || 0)} BTC · TP ${money(b.tp_price)}
        · <span class="${pnlClass(b.unrealized_pnl)}">${money(b.unrealized_pnl)}</span>
      </div>
    </div>
  `).join("");
}

function renderSource() {
  const source = stateSource.source || "No state source configured";
  const lastOk = stateSource.last_ok_at ? new Date(stateSource.last_ok_at * 1000).toLocaleTimeString() : "never";
  const err = stateSource.last_error ? ` · ${stateSource.last_error}` : "";
  document.getElementById("source").textContent = `${source} · last ok ${lastOk}${err}`;
}

// ── Chart drawing ──────────────────────────────────────────────────────────
function drawChart() {
  if (!ctx) return;
  const rect = canvas.getBoundingClientRect();
  const width = rect.width;
  const height = rect.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#0b0e11";
  ctx.fillRect(0, 0, width, height);

  if (!candles.length) {
    ctx.fillStyle = "#8b949e";
    ctx.font = "13px system-ui";
    ctx.fillText("Waiting for BTCUSDC candles...", 24, 32);
    return;
  }

  const orders = botState.active_orders || [];
  const bags = botState.open_bags || [];
  const support = botState.support_level || 0;
  const resistance = botState.resistance_level || 0;
  const avgEntry = botState.avg_entry_price || 0;

  const pad = { left: 12, right: 76, top: 18, bottom: 28 };
  const chartW = width - pad.left - pad.right;
  const chartH = height - pad.top - pad.bottom;
  const visible = candles.slice(-180);

  const overlayPrices = [
    ...orders.map(o => o.price),
    ...bags.flatMap(b => [b.entry_price, b.tp_price || 0]),
    support, resistance, avgEntry,
  ].filter(Boolean);

  const max = Math.max(...visible.map(c => c.high), ...overlayPrices);
  const min = Math.min(...visible.map(c => c.low), ...overlayPrices);
  const span = Math.max(1, max - min);
  const y = price => pad.top + (max - price) / span * chartH;
  const x = i => pad.left + i / Math.max(1, visible.length - 1) * chartW;

  drawGrid(width, height, pad, chartH, min, max, y);

  const step = chartW / Math.max(visible.length, 1);
  const bodyW = Math.max(2, Math.min(9, step * 0.62));
  visible.forEach((c, i) => {
    const cx = x(i);
    const up = c.close >= c.open;
    const color = up ? "#0ecb81" : "#f6465d";
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx, y(c.high));
    ctx.lineTo(cx, y(c.low));
    ctx.stroke();
    const top = Math.min(y(c.open), y(c.close));
    const bottom = Math.max(y(c.open), y(c.close));
    ctx.fillRect(cx - bodyW / 2, top, bodyW, Math.max(1, bottom - top));
  });

  if (support) drawPriceLine("Support", support, "#22c55e", y, pad, width);
  if (resistance) drawPriceLine("Resistance", resistance, "#f6465d", y, pad, width);
  if (avgEntry) drawPriceLine("Avg Entry", avgEntry, "#f0b90b", y, pad, width);

  for (const o of orders) {
    drawPriceLine(`${o.side} ${o.label || ""}`.trim(), o.price, o.side === "BUY" ? "#22c55e" : "#f97316", y, pad, width, true);
  }
  for (const b of bags) {
    drawMarker(`Bag ${b.id || ""}`, b.entry_price, "#f0b90b", y, pad, width);
    if (b.tp_price) drawMarker("TP", b.tp_price, "#f97316", y, pad, width);
  }
}

function drawGrid(width, height, pad, chartH, min, max, y) {
  ctx.strokeStyle = "#151b23";
  ctx.fillStyle = "#8b949e";
  ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) {
    const price = min + (max - min) * (i / 5);
    const yy = y(price);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    ctx.fillText(fmt.format(price), width - pad.right + 8, yy + 4);
  }
  ctx.strokeStyle = "#29313d";
  ctx.strokeRect(pad.left, pad.top, width - pad.left - pad.right, chartH);
}

function drawPriceLine(label, price, color, y, pad, width, dashed = false) {
  const yy = y(price);
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 1.5;
  ctx.setLineDash(dashed ? [5, 4] : []);
  ctx.beginPath();
  ctx.moveTo(pad.left, yy);
  ctx.lineTo(width - pad.right, yy);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.font = "11px system-ui";
  ctx.fillText(label, pad.left + 8, yy - 5);
  ctx.fillText(fmt.format(price), width - pad.right + 8, yy - 5);
  ctx.restore();
}

function drawMarker(label, price, color, y, pad, width) {
  const yy = y(price);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(width - pad.right - 8, yy, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.font = "11px system-ui";
  ctx.fillStyle = color;
  ctx.fillText(label, width - pad.right - 58, yy + 4);
}

// ── Boot ───────────────────────────────────────────────────────────────────
async function boot() {
  resize();
  renderFeedStatus();

  // Fetch config from server (symbol, interval, ws base)
  try {
    const cfg = await fetch("/api/config").then(r => r.json());
    symbol = cfg.symbol || symbol;
    interval = cfg.interval || interval;
    if (cfg.binance_ws_base) binanceWsBase = cfg.binance_ws_base;
    setText("symbol", symbol);
  } catch (e) {}

  await seedCandles();
  connectBinance();
  connectServer();
}

boot();
