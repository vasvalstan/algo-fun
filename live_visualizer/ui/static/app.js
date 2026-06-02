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

// ── Zoom / Pan state ───────────────────────────────────────────────────────
let visibleCount = null;   // null = auto; set by zoom
let panOffset    = 0;      // candles from right edge (0 = live)
let isDragging   = false;
let dragStartX   = 0;
let dragStartOffset = 0;
let lastTouchDist   = 0;
let touchStartX     = 0;
let touchStartOffset = 0;

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

  setText("mode", regime || "--");
  setText("gear", bias || "--");
  setText("rsi",  botState.rsi_5m != null ? `${botState.rsi_5m}` : "--");
  setText("cash", money(botState.cash));
  setText("realized",   money(botState.pnl_realized));
  setText("unrealized", money(botState.pnl_unrealized));
  document.getElementById("realized").className   = pnlClass(botState.pnl_realized);
  document.getElementById("unrealized").className = pnlClass(botState.pnl_unrealized);

  renderNextBuy();
  renderOrders(botState.active_orders || []);
  renderBags(botState.open_bags || []);
}

function renderNextBuy() {
  const statusEl  = document.getElementById("next-buy-status");
  const detailsEl = document.getElementById("next-buy-details");
  const regime    = botState.regime || "UNKNOWN";
  const bias      = botState.daily_bias || "UNKNOWN";
  const rsi       = botState.rsi_5m ?? 50;
  const zones     = botState.support_zones || [];
  const price     = botState.last_price || 0;
  const cash      = botState.cash || 0;
  const TP_PCT    = 0.0015;
  const ATR       = botState.atr_5m || 0;

  // Check what's blocking
  let blocker = null;
  if (regime === "CRASH")   blocker = "🚨 CRASH — no new entries";
  else if (regime === "BEAR") blocker = "🐻 BEAR regime — waiting";
  else if (regime === "SIDEWAYS" && bias === "BEARISH") blocker = "⏸ Sideways + Bearish bias — waiting";
  else if (cash < 1000)     blocker = "💰 No free capital ($1,000 needed)";
  else if (rsi >= 45)       blocker = `📈 RSI ${rsi} too high (need < 45)`;
  else if (!zones.length)   blocker = "🔍 No support zones detected yet";

  if (blocker) {
    statusEl.textContent = blocker;
    statusEl.className = "next-buy-status blocked";
    detailsEl.style.display = "none";
    return;
  }

  // Find nearest support zone below current price
  const nearZones = zones.filter(z => z.low < price).sort((a, b) => b.low - a.low);
  if (!nearZones.length) {
    statusEl.textContent = "🔍 No support zone below current price";
    statusEl.className = "next-buy-status blocked";
    detailsEl.style.display = "none";
    return;
  }

  const zone  = nearZones[0];
  const entry = zone.low;
  const tp    = entry * (1 + TP_PCT);
  const sl    = entry - ATR * 0.5;
  const qty   = 1000 / entry;
  const dist  = ((price - entry) / price * 100).toFixed(2);

  statusEl.textContent = `✅ Ready — price ${dist}% above zone`;
  statusEl.className = "next-buy-status ready";
  detailsEl.style.display = "";

  setText("nb-entry", `${money(entry)} (${zone.strength})`);
  setText("nb-tp",    `${money(tp)}  (+0.15%)`);
  setText("nb-sl",    `${money(sl)}  (−ATR×0.5)`);
  setText("nb-zone",  `${money(zone.low)} – ${money(zone.high)}  ×${zone.touches}`);
  setText("nb-size",  `$1,000 → ${qty.toFixed(6)} BTC`);
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

  // Auto-size: target ~8px per candle; zoom overrides
  const targetCandleW = 8;
  const autoCount = Math.max(10, Math.floor(chartW / targetCandleW));
  const count  = Math.min(candles.length, visibleCount || autoCount);
  const offset = Math.max(0, Math.min(panOffset, Math.max(0, candles.length - count)));
  const visible = candles.slice(
    Math.max(0, candles.length - count - offset),
    offset > 0 ? candles.length - offset : undefined,
  );

  // "Back to live" chip when panned away
  const liveChip = document.getElementById("live-chip");
  if (liveChip) liveChip.style.display = offset > 0 ? "flex" : "none";

  // Only include overlay prices within 5% of current price to avoid chart distortion
  const candleMax = Math.max(...visible.map(c => c.high));
  const candleMin = Math.min(...visible.map(c => c.low));
  const candleMid = (candleMax + candleMin) / 2;
  const priceRange = candleMid * 0.05;

  const overlayPrices = [
    ...orders.map(o => o.price),
    ...bags.flatMap(b => [b.entry_price, b.tp_price || 0]),
    support, resistance, avgEntry,
  ].filter(p => p && Math.abs(p - candleMid) < priceRange);

  const max = Math.max(candleMax, ...overlayPrices);
  const min = Math.min(candleMin, ...overlayPrices);
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

  // ── PLANNED next trade (shows bot's intent even before it acts) ──
  drawPlannedTrade(y, pad, width, min, max);

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

  drawStatusBanner(pad);
}

// Plain-English banner explaining what the bot is doing right now.
function drawStatusBanner(pad) {
  const regime = botState.regime || "UNKNOWN";
  const bias   = botState.daily_bias || "";
  const rsi    = botState.rsi_5m;
  const zones  = botState.support_zones || [];
  const bags   = botState.open_bags || [];
  const price  = botState.last_price || 0;

  let icon, text, color;

  if (bags.length) {
    const totalUnreal = bags.reduce((s, b) => s + (b.unrealized_pnl || 0), 0);
    icon = "📈"; color = "#0ecb81";
    text = `Holding ${bags.length} position${bags.length>1?"s":""} · waiting for TP · unrealized ${totalUnreal>=0?"+":""}$${totalUnreal.toFixed(2)}`;
  } else if (regime === "CRASH") {
    icon = "🚨"; color = "#f6465d";
    text = "CRASH regime — no new entries until market stabilizes";
  } else if (regime === "BEAR") {
    icon = "🐻"; color = "#f6465d";
    text = "BEAR regime — standing aside, not buying dips";
  } else if (!zones.length) {
    icon = "🔍"; color = "#f0b90b";
    text = `No support zone below $${fmt.format(price)} — waiting for price to form a base`;
  } else if (regime === "SIDEWAYS" && bias === "BEARISH") {
    icon = "⏸"; color = "#f0b90b";
    text = "Sideways + bearish bias — waiting for better setup";
  } else if (rsi != null && rsi >= (botState.params?.rsi_threshold || 45)) {
    const z = zones[0];
    icon = "⏳"; color = "#3b82f6";
    text = `Watching support $${fmt.format(z.low)} — waiting for RSI ${rsi} to drop below ${botState.params?.rsi_threshold||45}`;
  } else {
    const z = zones[0];
    icon = "✅"; color = "#0ecb81";
    text = `Ready to buy at support $${fmt.format(z.low)} on next pullback confirmation`;
  }

  ctx.save();
  ctx.font = "600 12px system-ui";
  const full = `${icon}  ${text}`;
  const tw = ctx.measureText(full).width;
  const bx = pad.left + 6, by = pad.top + 6, bw = tw + 20, bh = 26;
  ctx.fillStyle = "rgba(13,17,23,0.92)";
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(bx, by, bw, bh, 6);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = color;
  ctx.textAlign = "left";
  ctx.fillText(full, bx + 10, by + 17);
  ctx.restore();
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

// Draws the bot's INTENDED next trade (entry / TP / SL) so you can see its plan.
function drawPlannedTrade(y, pad, width, min, max) {
  const zones = botState.support_zones || [];
  const price = botState.last_price || 0;
  const params = botState.params || {};
  const atr = botState.atr_5m || 0;
  if (!zones.length || !price) return;

  // Don't draw if a position is already open (its own lines show instead)
  if ((botState.open_bags || []).length) return;

  // Nearest support below price = planned entry
  const below = zones.filter(z => z.low < price).sort((a, b) => b.low - a.low);
  if (!below.length) return;
  const zone = below[0];

  const entry = zone.low;
  const tp = params.tp_dollars > 0 ? entry + params.tp_dollars
                                    : entry * (1 + (params.tp_pct || 0.001));
  const sl = entry - atr * (params.atr_sl_mult || 0.5);

  // Only draw lines within visible price range
  const inRange = p => p >= min && p <= max;

  const drawDashLabeled = (price, color, label) => {
    if (!inRange(price)) return;
    const yy = y(price);
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, yy);
    ctx.lineTo(width - pad.right, yy);
    ctx.stroke();
    ctx.setLineDash([]);
    // Label pill on the left
    ctx.font = "bold 11px system-ui";
    const txt = `${label} ${fmt.format(price)}`;
    const w = ctx.measureText(txt).width + 12;
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.9;
    ctx.fillRect(pad.left + 4, yy - 9, w, 18);
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#000";
    ctx.fillText(txt, pad.left + 10, yy + 4);
    ctx.restore();
  };

  drawDashLabeled(tp,    "#0ecb81", "🎯 PLAN TP");
  drawDashLabeled(entry, "#3b82f6", "📥 PLAN BUY");
  drawDashLabeled(sl,    "#f6465d", "🛑 PLAN SL");
}

// ── Zoom / Pan helpers ─────────────────────────────────────────────────────
function snapToLive() {
  panOffset = 0;
  drawChart();
}

function _chartWidth() {
  return canvas.getBoundingClientRect().width - 12 - 76; // pad.left + pad.right
}

function _applyZoom(delta) {
  const autoCount = Math.max(10, Math.floor(_chartWidth() / 8));
  const current = visibleCount || autoCount;
  visibleCount = Math.round(Math.max(10, Math.min(CANDLE_LIMIT, current * delta)));
  drawChart();
}

// Mouse wheel → zoom
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  _applyZoom(e.deltaY > 0 ? 1.12 : 0.88);
}, { passive: false });

// Double-click → reset zoom + snap to live
canvas.addEventListener("dblclick", () => {
  visibleCount = null;
  panOffset    = 0;
  drawChart();
});

// Mouse drag → pan
canvas.addEventListener("mousedown", e => {
  isDragging      = true;
  dragStartX      = e.clientX;
  dragStartOffset = panOffset;
  canvas.style.cursor = "grabbing";
});
canvas.addEventListener("mousemove", e => {
  if (!isDragging) return;
  const autoCount = Math.max(10, Math.floor(_chartWidth() / 8));
  const count = visibleCount || autoCount;
  const pxPerCandle = _chartWidth() / count;
  const dx = dragStartX - e.clientX;
  panOffset = Math.max(0, Math.min(candles.length - 10,
    dragStartOffset + Math.round(dx / pxPerCandle)));
  drawChart();
});
canvas.addEventListener("mouseup",    () => { isDragging = false; canvas.style.cursor = "default"; });
canvas.addEventListener("mouseleave", () => { isDragging = false; canvas.style.cursor = "default"; });

// Touch: single finger → pan, two fingers → pinch zoom
canvas.addEventListener("touchstart", e => {
  if (e.touches.length === 1) {
    touchStartX      = e.touches[0].clientX;
    touchStartOffset = panOffset;
  } else if (e.touches.length === 2) {
    lastTouchDist = Math.hypot(
      e.touches[0].clientX - e.touches[1].clientX,
      e.touches[0].clientY - e.touches[1].clientY,
    );
  }
}, { passive: true });

canvas.addEventListener("touchmove", e => {
  e.preventDefault();
  if (e.touches.length === 1) {
    const autoCount = Math.max(10, Math.floor(_chartWidth() / 8));
    const count = visibleCount || autoCount;
    const pxPerCandle = _chartWidth() / count;
    const dx = touchStartX - e.touches[0].clientX;
    panOffset = Math.max(0, Math.min(candles.length - 10,
      touchStartOffset + Math.round(dx / pxPerCandle)));
    drawChart();
  } else if (e.touches.length === 2) {
    const dist = Math.hypot(
      e.touches[0].clientX - e.touches[1].clientX,
      e.touches[0].clientY - e.touches[1].clientY,
    );
    if (lastTouchDist) _applyZoom(lastTouchDist / dist);
    lastTouchDist = dist;
  }
}, { passive: false });

canvas.addEventListener("touchend", () => { lastTouchDist = 0; });

// ── Tab switching ──────────────────────────────────────────────────────────
function showTab(tab) {
  ["chart","strategy","history","backtest","optimize"].forEach(t => {
    const el = document.getElementById("tab-" + t);
    if (el) el.style.display = t === tab ? "" : "none";
  });
  document.querySelectorAll(".main-tab").forEach(b =>
    b.classList.toggle("active", b.textContent.toLowerCase().includes(tab))
  );
  if (tab === "history")  loadHistory();
  else if (tab === "backtest") initBacktest();
  else if (tab === "optimize") initOptimize();
  else if (tab === "strategy") loadStrategy();
  else { resize(); drawChart(); }
}

// ── Strategy tab (self-describing, mirrors live params) ─────────────────────
async function loadStrategy() {
  const el = document.getElementById("strategy-content");
  try {
    const d = await fetch("/api/strategy").then(r => r.json());
    const params = Object.entries(d.params || {}).map(([k, v]) =>
      `<div class="strat-param"><span>${k}</span><strong>${v}</strong></div>`
    ).join("");

    const sections = (d.sections || []).map(s => `
      <div class="strat-section">
        <h3>${s.heading}</h3>
        <ul>${s.rules.map(r => `<li>${r}</li>`).join("")}</ul>
      </div>
    `).join("");

    el.innerHTML = `
      <div class="strat-hero">
        <div class="strat-name">${d.title || d.name}</div>
        <div class="strat-summary">${d.summary || ""}</div>
      </div>
      <div class="strat-params">${params}</div>
      <div class="strat-sections">${sections}</div>
      <div class="strat-note">
        ℹ️ These rules are generated live from the bot's current settings — they always
        reflect exactly what the bot is doing right now.
      </div>
    `;
  } catch (e) {
    el.innerHTML = '<div class="history-empty">Failed to load strategy</div>';
  }
}

// ── Optimizer ──────────────────────────────────────────────────────────────
function initOptimize() {
  const now  = new Date();
  const from = new Date(now - 30 * 86400000);
  document.getElementById("op-to").value   = now.toISOString().slice(0,10);
  document.getElementById("op-from").value = from.toISOString().slice(0,10);
}

function parseList(str) {
  return str.split(",").map(s => parseFloat(s.trim())).filter(n => !isNaN(n));
}

async function runOptimize() {
  const btn    = document.getElementById("op-run-btn");
  const status = document.getElementById("op-status");

  const tpList  = parseList(document.getElementById("op-tp").value);
  const slList  = parseList(document.getElementById("op-sl").value);
  const rsiList = parseList(document.getElementById("op-rsi").value);
  const combos  = tpList.length * slList.length * rsiList.length;

  if (combos === 0) { status.textContent = "❌ Enter at least one value per parameter"; return; }
  if (combos > 200) { status.textContent = `❌ ${combos} combos — max 200. Reduce values.`; return; }

  btn.disabled = true;
  btn.textContent = "⏳ Running...";
  document.getElementById("op-results").style.display = "none";
  status.textContent = `Fetching data once, then testing ${combos} combinations... (may take 1-2 min)`;

  const fromDate = new Date(document.getElementById("op-from").value);
  const toDate   = new Date(document.getElementById("op-to").value);
  toDate.setHours(23,59,59);

  const body = {
    from_ts: Math.floor(fromDate / 1000),
    to_ts:   Math.floor(toDate / 1000),
    capital: parseFloat(document.getElementById("op-capital").value),
    sort_by: document.getElementById("op-sort").value,
    grid: {
      tp_dollars:    tpList,
      atr_sl_mult:   slList,
      rsi_threshold: rsiList,
    },
  };

  try {
    const res  = await fetch("/api/optimize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.error) {
      status.textContent = "❌ " + data.error;
    } else {
      status.textContent = `✅ Tested ${data.combos_tested} combos in ${data.elapsed_s}s — ${data.candles_processed} candles`;
      renderOptimize(data);
      document.getElementById("op-results").style.display = "";
    }
  } catch (e) {
    status.textContent = "❌ " + e.message;
  }

  btn.disabled = false;
  btn.textContent = "🎯 Run Optimization";
}

function renderOptimize(data) {
  const results = data.results || [];
  const best = results[0];

  if (best) {
    const p = best.params;
    document.getElementById("op-best").innerHTML = `
      <div class="bt-stat pos" style="border-color:var(--green);">
        <span>🏆 Best Config</span>
        <strong>TP $${p.tp_dollars} · SL ${p.atr_sl_mult}× · RSI ${p.rsi_threshold}</strong>
      </div>
      <div class="bt-stat ${best.total_pnl>=0?"pos":"neg"}"><span>Total P&L</span><strong>${best.total_pnl>=0?"+":""}$${best.total_pnl.toFixed(2)}</strong></div>
      <div class="bt-stat"><span>Return</span><strong>${best.return_pct}%</strong></div>
      <div class="bt-stat"><span>Win Rate</span><strong>${best.win_rate}%</strong></div>
      <div class="bt-stat"><span>Trades</span><strong>${best.total_trades}</strong></div>
      <div class="bt-stat"><span>Profit Factor</span><strong>${best.profit_factor}</strong></div>
      <div class="bt-stat neg"><span>Max DD</span><strong>-${best.max_drawdown}%</strong></div>
    `;
  }

  const tbody = document.getElementById("op-table");
  tbody.innerHTML = results.map((r, i) => {
    const p = r.params;
    const rowCls = i === 0 ? "style='background:rgba(14,203,129,0.08);'" : "";
    return `<tr ${rowCls}>
      <td>${i + 1}</td>
      <td>$${p.tp_dollars}</td>
      <td>${p.atr_sl_mult}×</td>
      <td>${p.rsi_threshold}</td>
      <td>${r.total_trades}</td>
      <td>${r.win_rate}%</td>
      <td class="${r.total_pnl>=0?"pos":"neg"}">${r.total_pnl>=0?"+":""}$${r.total_pnl.toFixed(2)}</td>
      <td class="${r.return_pct>=0?"pos":"neg"}">${r.return_pct}%</td>
      <td>${r.profit_factor}</td>
      <td class="neg">-${r.max_drawdown}%</td>
    </tr>`;
  }).join("");
}

// ── Backtest helper (dollar TP support) ─────────────────────────────────────

// ── Backtest ───────────────────────────────────────────────────────────────
function initBacktest() {
  // Set default dates: last 30 days
  const now  = new Date();
  const from = new Date(now - 30 * 86400000);
  document.getElementById("bt-to").value   = now.toISOString().slice(0,10);
  document.getElementById("bt-from").value = from.toISOString().slice(0,10);
}

async function runBacktest() {
  const btn    = document.getElementById("bt-run-btn");
  const status = document.getElementById("bt-status");
  btn.disabled = true;
  btn.textContent = "⏳ Running...";
  document.getElementById("bt-results").style.display = "none";
  status.textContent = "Fetching historical data and running simulation...";

  const fromDate = new Date(document.getElementById("bt-from").value);
  const toDate   = new Date(document.getElementById("bt-to").value);
  toDate.setHours(23,59,59);

  const body = {
    from_ts:       Math.floor(fromDate / 1000),
    to_ts:         Math.floor(toDate   / 1000),
    tp_pct:        parseFloat(document.getElementById("bt-tp").value)      / 100,
    atr_sl_mult:   parseFloat(document.getElementById("bt-sl").value),
    rsi_threshold: parseFloat(document.getElementById("bt-rsi").value),
    tranche_usdc:  parseFloat(document.getElementById("bt-tranche").value),
    capital:       parseFloat(document.getElementById("bt-capital").value),
  };

  try {
    const res  = await fetch("/api/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();

    if (data.error) {
      status.textContent = "❌ " + data.error;
    } else {
      status.textContent = `✅ Done in ${data.elapsed_s}s — ${data.candles_processed} candles processed`;
      renderBacktestResults(data);
      document.getElementById("bt-results").style.display = "";
    }
  } catch (e) {
    status.textContent = "❌ Request failed: " + e.message;
  }

  btn.disabled = false;
  btn.textContent = "▶ Run";
}

function renderBacktestResults(d) {
  const pnlCls = d.total_pnl >= 0 ? "pos" : "neg";
  document.getElementById("bt-summary").innerHTML = `
    <div class="bt-stat"><span>Trades</span><strong>${d.total_trades}</strong></div>
    <div class="bt-stat pos"><span>Wins</span><strong>✅ ${d.wins}</strong></div>
    <div class="bt-stat neg"><span>Losses</span><strong>❌ ${d.losses}</strong></div>
    <div class="bt-stat"><span>Win Rate</span><strong>${d.win_rate}%</strong></div>
    <div class="bt-stat ${pnlCls}"><span>Total P&L</span><strong>${d.total_pnl >= 0 ? "+" : ""}$${d.total_pnl.toFixed(4)}</strong></div>
    <div class="bt-stat"><span>Final Capital</span><strong>$${d.final_equity.toFixed(2)}</strong></div>
    <div class="bt-stat neg"><span>Max Drawdown</span><strong>-${d.max_drawdown}%</strong></div>
    <div class="bt-stat pos"><span>Best Trade</span><strong>+$${d.best_trade.toFixed(4)}</strong></div>
    <div class="bt-stat neg"><span>Worst Trade</span><strong>$${d.worst_trade.toFixed(4)}</strong></div>
  `;

  // Equity curve
  drawEquityCurve(d.equity_curve, d.capital);

  // Trades table
  const tbody = document.getElementById("bt-trades");
  if (!d.trades.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="history-empty">No trades in this period</td></tr>';
  } else {
    tbody.innerHTML = d.trades.map((t, i) => {
      const cls = t.result === "TP" ? "pos" : t.result === "SL" ? "neg" : t.result === "OPEN" ? "warn" : "";
      return `<tr>
        <td>${d.trades.length - i}</td>
        <td>${fmtTs(t.entry_time)}</td>
        <td>${t.exit_time ? fmtTs(t.exit_time) : "<span class='warn'>open</span>"}</td>
        <td>$${t.entry_price.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2})}</td>
        <td>${t.exit_price ? "$"+t.exit_price.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}) : "--"}</td>
        <td>${t.qty.toFixed(6)}</td>
        <td>$${t.size_usdc.toFixed(2)}</td>
        <td class="${t.pnl >= 0 ? "pos" : "neg"}">${t.pnl >= 0 ? "+" : ""}$${t.pnl.toFixed(4)}</td>
        <td class="${cls}">${t.result}</td>
      </tr>`;
    }).join("");
  }
}

function drawEquityCurve(points, startCapital) {
  const canvas = document.getElementById("bt-chart");
  if (!canvas || !points.length) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.offsetWidth;
  const H = canvas.offsetHeight;
  canvas.width  = W * (window.devicePixelRatio || 1);
  canvas.height = H * (window.devicePixelRatio || 1);
  ctx.setTransform(window.devicePixelRatio||1, 0, 0, window.devicePixelRatio||1, 0, 0);

  ctx.fillStyle = "#11161d";
  ctx.fillRect(0, 0, W, H);

  const equities = points.map(p => p.equity);
  const minE = Math.min(...equities, startCapital) * 0.999;
  const maxE = Math.max(...equities, startCapital) * 1.001;
  const span = maxE - minE || 1;

  const pad = { left: 8, right: 8, top: 12, bottom: 8 };
  const cW = W - pad.left - pad.right;
  const cH = H - pad.top  - pad.bottom;

  const xOf = i => pad.left + i / (points.length - 1) * cW;
  const yOf = v => pad.top  + (maxE - v) / span * cH;

  // Baseline
  const baseY = yOf(startCapital);
  ctx.strokeStyle = "#29313d";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.left, baseY); ctx.lineTo(W - pad.right, baseY); ctx.stroke();
  ctx.setLineDash([]);

  // Fill
  const lastColor = equities[equities.length - 1] >= startCapital ? "#0ecb81" : "#f6465d";
  ctx.beginPath();
  ctx.moveTo(xOf(0), yOf(equities[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(xOf(i), yOf(equities[i]));
  ctx.lineTo(xOf(points.length - 1), H);
  ctx.lineTo(xOf(0), H);
  ctx.closePath();
  ctx.fillStyle = lastColor + "22";
  ctx.fill();

  // Line
  ctx.strokeStyle = lastColor;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(xOf(0), yOf(equities[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(xOf(i), yOf(equities[i]));
  ctx.stroke();

  // Label final equity
  ctx.fillStyle = lastColor;
  ctx.font = "bold 12px system-ui";
  ctx.textAlign = "right";
  ctx.fillText("$" + equities[equities.length-1].toFixed(0), W - pad.right - 4, yOf(equities[equities.length-1]) - 6);
  ctx.textAlign = "left";
}

// ── History tab ────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const data = await fetch("/api/history").then(r => r.json());
    renderHistory(data);
  } catch (e) {
    document.getElementById("history-body").innerHTML =
      '<tr><td colspan="12" class="history-empty">Failed to load</td></tr>';
  }
}

function money2(v) {
  return "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDuration(s) {
  if (s == null) return "--";
  if (s < 60)    return s + "s";
  if (s < 3600)  return Math.floor(s/60) + "m " + (s%60) + "s";
  if (s < 86400) return Math.floor(s/3600) + "h " + Math.floor((s%3600)/60) + "m";
  return Math.floor(s/86400) + "d " + Math.floor((s%86400)/3600) + "h";
}

function renderHistory(data) {
  const rows = data.rows || [];

  // ── Summary ──
  document.getElementById("history-summary").innerHTML = `
    <span>${data.completed || 0} completed</span>
    <span class="${(data.total_pnl||0) >= 0 ? "pos" : "neg"}">Total P&L: ${(data.total_pnl||0)>=0?"+":""}$${(data.total_pnl||0).toFixed(4)}</span>
    <span class="pos">✅ ${data.wins || 0} wins</span>
    <span class="neg">❌ ${data.losses || 0} losses</span>
    <span>${data.win_rate || 0}% win rate</span>
    <span class="warn">${data.open || 0} open</span>
  `;

  // ── One consolidated table ──
  const tbody = document.getElementById("history-body");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="history-empty">No trades yet</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r, i) => {
    const isOpen = r.state === "OPEN" || r.state === "PENDING";
    const result = r.result;
    const resultCls = result === "TP" ? "pos" : result === "SL" || result === "STOPPED" ? "neg"
                    : isOpen ? "warn" : "";
    const pnlCls = isOpen ? "warn" : (r.pnl >= 0 ? "pos" : "neg");
    const pnlPrefix = r.pnl >= 0 ? "+" : "";
    return `<tr>
      <td>${rows.length - i}</td>
      <td>${fmtTs(r.entry_time)}</td>
      <td>${money2(r.entry_price)}</td>
      <td class="pos">${money2(r.tp_price)}</td>
      <td class="neg">${money2(r.sl_price)}</td>
      <td>${r.exit_time ? fmtTs(r.exit_time) : "<span class='warn'>—</span>"}</td>
      <td>${r.exit_price ? money2(r.exit_price) : "<span class='warn'>open</span>"}</td>
      <td>${r.qty.toFixed(6)}</td>
      <td>${money2(r.size_usdc)}</td>
      <td>${fmtDuration(r.duration_s)}</td>
      <td class="${pnlCls}">${pnlPrefix}$${r.pnl.toFixed(4)}${isOpen ? " (unrl)" : ""}</td>
      <td class="${resultCls}">${result}</td>
    </tr>`;
  }).join("");
}

function fmtTs(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString("en-GB", { day:"2-digit", month:"short" }) +
    " " + d.toLocaleTimeString("en-US", { hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false });
}

// ── Activity log ───────────────────────────────────────────────────────────
let _lastLogLines = [];

async function pollActivity() {
  try {
    const data = await fetch("/api/ledger").then(r => r.json());
    const lines = data.log || [];
    if (JSON.stringify(lines) !== JSON.stringify(_lastLogLines)) {
      _lastLogLines = lines;
      renderActivity(lines);
    }
  } catch (e) {}
  setTimeout(pollActivity, 2000);
}

function renderActivity(lines) {
  const el = document.getElementById("activity-log");
  if (!lines.length) {
    el.innerHTML = '<span class="activity-empty">Waiting for first tick...</span>';
    return;
  }
  el.innerHTML = [...lines].reverse().map(line => {
    const cls = line.includes("FILLED") || line.includes("BUY") ? "activity-buy"
              : line.includes("SELL") || line.includes("TP")   ? "activity-sell"
              : line.includes("SL") || line.includes("STOP")   ? "activity-sl"
              : line.includes("CANCEL")                         ? "activity-warn"
              : "activity-info";
    return `<div class="activity-line ${cls}">${line}</div>`;
  }).join("");
}

// ── Force buy ─────────────────────────────────────────────────────────────
async function forceBuy() {
  const btn = document.getElementById("force-buy-btn");
  const msg = document.getElementById("force-buy-msg");
  btn.disabled = true;
  btn.textContent = "⏳ Placing...";
  msg.textContent = "";
  try {
    const res = await fetch("/api/force-buy", { method: "POST" });
    const data = await res.json();
    if (data.status === "ok") {
      msg.className = "force-buy-msg pos";
      msg.textContent = "✅ " + data.message;
    } else {
      msg.className = "force-buy-msg neg";
      msg.textContent = "❌ " + data.message;
    }
  } catch (e) {
    msg.className = "force-buy-msg neg";
    msg.textContent = "❌ Request failed";
  }
  btn.disabled = false;
  btn.textContent = "⚡ Force Buy Now";
  setTimeout(() => { msg.textContent = ""; }, 6000);
}

// ── Boot ───────────────────────────────────────────────────────────────────
async function boot() {
  resize();
  renderFeedStatus();
  document.getElementById("force-buy-btn").addEventListener("click", forceBuy);

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
  pollActivity();   // Activity log every 2s
}

boot();
