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

const BINANCE_REST = "https://data-api.binance.vision/api/v3";

// ── Binance REST seed ──────────────────────────────────────────────────────
async function seedCandles() {
  try {
    const url = `${BINANCE_REST}/klines?symbol=${symbol}&interval=${interval}&limit=${CANDLE_LIMIT}`;
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

// ── REST real-time polling (runs every second, always) ─────────────────────
async function pollBinance() {
  try {
    // Fetch current open candle + book ticker in parallel
    const [klRes, bkRes] = await Promise.all([
      fetch(`${BINANCE_REST}/klines?symbol=${symbol}&interval=${interval}&limit=1`),
      fetch(`${BINANCE_REST}/ticker/bookTicker?symbol=${symbol}`),
    ]);
    const klines = await klRes.json();
    const bk = await bkRes.json();

    // Update book
    if (bk.bidPrice) {
      book = { bid: parseFloat(bk.bidPrice), ask: parseFloat(bk.askPrice) };
    }

    // Update only the current open candle (limit=1 returns just the latest)
    if (Array.isArray(klines) && klines.length) {
      const k = klines[0];
      updateCandle({
        time: Math.floor(k[0] / 1000),
        open: parseFloat(k[1]),
        high: parseFloat(k[2]),
        low: parseFloat(k[3]),
        close: parseFloat(k[4]),
        volume: parseFloat(k[5]),
        closed: false,
      });
    }

    if (!feedConnected) {
      feedConnected = true;
      setText("feed", "LIVE");
      document.getElementById("feed").className = "pos";
    }

    renderPrices();
    drawChart();
  } catch (e) {}

  setTimeout(pollBinance, 1000);
}

// ── Binance WebSocket (browser → Binance directly) ─────────────────────────
let _binanceWs = null;

function connectBinance() {
  if (_binanceWs) { _binanceWs.onclose = null; _binanceWs.close(); }
  const sym = symbol.toLowerCase();
  // Do NOT encodeURIComponent — Binance expects raw @ and / in the streams param
  const streams = `${sym}@kline_${interval}/${sym}@bookTicker`;
  const url = `${binanceWsBase}?streams=${streams}`;
  const ws = new WebSocket(url);
  _binanceWs = ws;

  ws.onopen = () => { feedConnected = true; renderFeedStatus(); };
  ws.onclose = () => {
    feedConnected = false;
    renderFeedStatus();
    if (_binanceWs === ws) setTimeout(connectBinance, 2000);
  };
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

// ── Timeframe switching ────────────────────────────────────────────────────
function setTimeframe(tf) {
  interval = tf;
  candles = [];
  document.querySelectorAll(".tf").forEach(b => b.classList.toggle("active", b.dataset.tf === tf));
  seedCandles().then(() => connectBinance());
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tf").forEach(b => {
    b.addEventListener("click", () => setTimeframe(b.dataset.tf));
  });
});

function updateCandle(candle) {
  const last = candles[candles.length - 1];
  if (last && last.time === candle.time) {
    candles[candles.length - 1] = candle;
  } else {
    candles.push(candle);
    if (candles.length > CANDLE_LIMIT) candles.shift();
  }
}

// ── Server polling (pullback strategy state) ───────────────────────────────
async function pollServer() {
  try {
    const data = await fetch("/api/snapshot").then(r => r.json());
    // /api/snapshot now returns pullback state directly
    botState = data;
    stateSource = { source: "pullback_v1", last_ok_at: data.updated_at, last_error: "" };
    renderState();
    renderSource();
    drawChart();
  } catch (e) {}
  setTimeout(pollServer, 1000);
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
  const bias   = botState.daily_bias || "";
  const regimeEl = document.getElementById("regime");
  regimeEl.textContent = bias ? `${regime} · ${bias}` : regime;
  regimeEl.style.background =
    regime === "BULL"  ? "#0ecb81" :
    regime === "BEAR"  ? "#f6465d" :
    regime === "CRASH" ? "#7c3aed" : "#64748b";

  setText("mode", `RSI ${botState.rsi_5m ?? "--"}`);
  setText("gear", `ATR ${botState.atr_5m ?? "--"}`);
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
  const updatedAt = botState.updated_at
    ? new Date(botState.updated_at * 1000).toLocaleTimeString()
    : "never";
  const capital = botState.capital_total
    ? `$${fmt.format(botState.capital_total)} capital · $${fmt.format(botState.capital_free || 0)} free`
    : "";
  document.getElementById("source").textContent =
    `pullback_v1 · ${capital} · updated ${updatedAt}`;
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

  const pad = { left: 12, right: 76, top: 18, bottom: 44 };
  const chartW = width - pad.left - pad.right;
  const chartH = height - pad.top - pad.bottom;

  // Auto-size: target ~8px per candle so bodies are visible
  const targetCandleW = 8;
  const maxVisible = Math.max(10, Math.floor(chartW / targetCandleW));
  const visible = candles.slice(-maxVisible);

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

  drawGrid(width, height, pad, chartH, min, max, y, visible, x);

  const step = chartW / Math.max(visible.length, 1);
  const bodyW = Math.max(3, Math.min(12, step * 0.7));
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

  // ── Support zones (shaded bands) ──
  for (const zone of (botState.support_zones || [])) {
    if (zone.low < min || zone.high > max) continue;
    const alpha = zone.strength === "strong" ? 0.12 : zone.strength === "moderate" ? 0.07 : 0.04;
    ctx.save();
    ctx.fillStyle = `rgba(34,197,94,${alpha})`;
    ctx.fillRect(pad.left, y(zone.high), chartW, y(zone.low) - y(zone.high));
    ctx.strokeStyle = `rgba(34,197,94,${alpha * 2})`;
    ctx.lineWidth = 0.5;
    ctx.strokeRect(pad.left, y(zone.high), chartW, y(zone.low) - y(zone.high));
    ctx.fillStyle = "rgba(34,197,94,0.5)";
    ctx.font = "10px system-ui";
    ctx.fillText(`S ${zone.strength}`, pad.left + 4, y(zone.high) - 3);
    ctx.restore();
  }

  // ── Resistance zones (shaded bands) ──
  for (const zone of (botState.resistance_zones || [])) {
    if (zone.low < min || zone.high > max) continue;
    ctx.save();
    ctx.fillStyle = "rgba(246,70,93,0.06)";
    ctx.fillRect(pad.left, y(zone.high), chartW, y(zone.low) - y(zone.high));
    ctx.strokeStyle = "rgba(246,70,93,0.15)";
    ctx.lineWidth = 0.5;
    ctx.strokeRect(pad.left, y(zone.high), chartW, y(zone.low) - y(zone.high));
    ctx.restore();
  }

  if (support) drawPriceLine("Support", support, "#22c55e", y, pad, width);
  if (resistance) drawPriceLine("Resistance", resistance, "#f6465d", y, pad, width);
  if (avgEntry) drawPriceLine("Avg Entry", avgEntry, "#f0b90b", y, pad, width);

  // ── SL lines (one per open tranche) ──
  for (const sl of (botState.sl_lines || [])) {
    drawPriceLine(sl.label, sl.price, "#f6465d", y, pad, width, true);
  }

  // ── Current price label (Binance-style) ──
  const lastCandle = visible[visible.length - 1];
  if (lastCandle) {
    const price = lastCandle.close;
    const prevClose = visible.length > 1 ? visible[visible.length - 2].close : price;
    const isUp = price >= prevClose;
    const color = isUp ? "#0ecb81" : "#f6465d";
    const yy = y(price);

    // Dashed line across chart
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    ctx.setLineDash([]);

    // Price box on right axis
    const label = fmt.format(price);
    ctx.font = "bold 11px ui-monospace, SFMono-Regular, Menlo, monospace";
    const textW = ctx.measureText(label).width;
    const boxW = textW + 12;
    const boxH = 18;
    const boxX = width - pad.right + 1;
    const boxY = yy - boxH / 2;

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.roundRect(boxX, boxY, boxW, boxH, 3);
    ctx.fill();

    ctx.fillStyle = "#fff";
    ctx.textAlign = "left";
    ctx.fillText(label, boxX + 6, yy + 4);
    ctx.restore();
  }

  for (const o of orders) {
    drawPriceLine(`${o.side} ${o.label || ""}`.trim(), o.price, o.side === "BUY" ? "#22c55e" : "#f97316", y, pad, width, true);
  }
  for (const b of bags) {
    drawMarker(`Bag ${b.id || ""}`, b.entry_price, "#f0b90b", y, pad, width);
    if (b.tp_price) drawMarker("TP", b.tp_price, "#f97316", y, pad, width);
  }
}

function formatTimeLabel(ts) {
  const d = new Date(ts * 1000);
  if (interval === "1d") {
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }
  if (interval === "4h" || interval === "1h") {
    const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
    // Show date when it's midnight, otherwise just time
    return d.getHours() === 0 && d.getMinutes() === 0 ? date : time;
  }
  // 1m, 5m, 15m — show date when day changes, otherwise HH:MM
  const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
  const isNewDay = d.getHours() === 0 && d.getMinutes() < (interval === "15m" ? 15 : interval === "5m" ? 5 : 1);
  return isNewDay ? d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) : time;
}

function drawGrid(width, height, pad, chartH, min, max, y, visible, x) {
  const monoFont = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.lineWidth = 1;

  // ── Horizontal price lines ──
  ctx.strokeStyle = "#1a2030";
  ctx.fillStyle = "#8b949e";
  ctx.font = monoFont;
  for (let i = 0; i <= 5; i++) {
    const price = min + (max - min) * (i / 5);
    const yy = y(price);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    ctx.fillText(fmt.format(price), width - pad.right + 8, yy + 4);
  }

  // ── Time axis ──
  const chartW = width - pad.left - pad.right;
  const minLabelGap = 80; // px between labels
  const maxLabels = Math.floor(chartW / minLabelGap);
  const step = Math.max(1, Math.floor(visible.length / maxLabels));

  // Find label candidates — every `step` candles, prefer round times
  const labelIndices = [];
  for (let i = 0; i < visible.length; i++) {
    if (i % step === 0) labelIndices.push(i);
  }

  ctx.fillStyle = "#8b949e";
  ctx.font = monoFont;
  ctx.textAlign = "center";

  for (const i of labelIndices) {
    const cx = x(i);
    const label = formatTimeLabel(visible[i].time);

    // Vertical grid line
    ctx.strokeStyle = "#1a2030";
    ctx.beginPath();
    ctx.moveTo(cx, pad.top);
    ctx.lineTo(cx, pad.top + chartH);
    ctx.stroke();

    // Tick mark
    ctx.strokeStyle = "#29313d";
    ctx.beginPath();
    ctx.moveTo(cx, pad.top + chartH);
    ctx.lineTo(cx, pad.top + chartH + 5);
    ctx.stroke();

    // Label
    ctx.fillText(label, cx, pad.top + chartH + 18);
  }

  ctx.textAlign = "left";

  // ── Border ──
  ctx.strokeStyle = "#29313d";
  ctx.strokeRect(pad.left, pad.top, chartW, chartH);
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
  pollBinance();    // REST every 1s — always works
  connectBinance(); // WebSocket — instant updates if available
  pollServer();     // Bot state every 1s
}

boot();
