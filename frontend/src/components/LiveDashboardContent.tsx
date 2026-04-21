import { useMemo, useEffect, useState, useCallback } from 'react';
import type { BotState, TradeMarker, LogEntry } from '../lib/types';
import {
  formatUptime,
  formatPrice,
  formatPnl,
  formatPct,
  pnlColorClass,
  formatDate,
  regimeColorClass,
  modeColorClass,
} from '../lib/formatters';
import { TradingChart, type GridOverlay } from './TradingChart';
import { ErrorPanel } from './ErrorPanel';
import { apiUrl } from '../lib/apiBase';

interface Props {
  readonly s: BotState;
  /**
   * Runner label for the currently-viewed channel (e.g. 'binance-live',
   * 'binance-paper', 'revolut-live'). The Live Log filters entries to
   * this channel plus any "global" (unlabeled) entries. `null` disables
   * filtering.
   */
  readonly runnerLabel?: string | null;
  /**
   * Venue label for the /api/exchange/{venue} endpoint (real exchange
   * balances + open orders). e.g. 'binance-live' | 'revolut-live'.
   * `null` falls back to the legacy /api/open-orders endpoint (Binance).
   */
  readonly venueLabel?: string | null;
}

function cyclesToMarkers(s: BotState): TradeMarker[] {
  const markers: TradeMarker[] = [];
  for (const c of s.cycles) {
    markers.push({
      time: c.timestamp - 60,
      position: 'belowBar',
      color: '#22c55e',
      shape: 'arrowUp',
      text: `Buy #${c.number}`,
      price: c.buy_price,
      side: 'buy',
      active: false,
    });
    markers.push({
      time: c.timestamp,
      position: 'aboveBar',
      color: c.net_pnl >= 0 ? '#eab308' : '#ef4444',
      shape: 'arrowDown',
      text: `${c.net_pnl >= 0 ? '+' : ''}${c.gross_pct.toFixed(2)}%`,
      price: c.sell_price,
      side: 'sell',
      active: false,
    });
  }
  for (const p of s.positions) {
    if (p.entry_price > 0 && (p.state === 'HOLDING' || p.state === 'SELL_PLACED')) {
      markers.push({
        time: Math.floor(Date.now() / 1000) - 60,
        position: 'belowBar',
        color: '#22c55e',
        shape: 'arrowUp',
        text: `Entry`,
        price: p.entry_price,
        tp_price: p.entry_price * (1 + s.take_profit_pct / 100),
        side: 'buy',
        active: true,
      });
    }
  }
  return markers;
}

function StrategySignalBadge({ action }: { action: string }) {
  const configs: Record<string, { bg: string; color: string; label: string }> = {
    ENTRY_READY: { bg: 'rgba(34, 197, 94, 0.18)', color: '#22c55e', label: 'ENTRY READY' },
    WAIT_FOR_DIP: { bg: 'rgba(234, 179, 8, 0.15)', color: '#eab308', label: 'WAIT FOR DIP' },
    NO_TRADE: { bg: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', label: 'NO TRADE' },
    WAIT: { bg: 'rgba(148, 163, 184, 0.12)', color: '#94a3b8', label: 'WAITING' },
  };
  const c = configs[action] ?? { bg: 'rgba(148, 163, 184, 0.12)', color: '#94a3b8', label: action };
  return (
    <span
      style={{
        padding: '3px 10px',
        borderRadius: 6,
        background: c.bg,
        color: c.color,
        fontSize: '0.7rem',
        fontWeight: 700,
        letterSpacing: '0.06em',
        fontFamily: "'JetBrains Mono', monospace",
      }}
    >
      {c.label}
    </span>
  );
}

function LayerDot({ pass }: { pass: boolean }) {
  return (
    <span
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: pass ? '#22c55e' : '#ef4444',
        flexShrink: 0,
      }}
    />
  );
}

const LOG_VISIBLE_LIMIT = 50;

function LogFeed({ logs, channel }: { logs: LogEntry[]; channel: string | null }) {
  // Filter to entries belonging to this channel + any unlabeled "global"
  // entries (api.main, telegram_bot, paper_runner) so cross-runner noise
  // doesn't drown out what this dashboard cares about.
  const visible = useMemo(() => {
    const filtered = channel
      ? logs.filter((e) => e.channel === channel || e.channel == null)
      : logs;
    // Reverse so newest is at the top — no auto-scroll needed, the
    // freshest line is always anchored at the visible top of the panel.
    return [...filtered].reverse().slice(0, LOG_VISIBLE_LIMIT);
  }, [logs, channel]);

  const levelColor = (level: string) => {
    switch (level) {
      case 'ERROR': return '#ef4444';
      case 'WARNING': return '#eab308';
      case 'INFO': return '#94a3b8';
      case 'DEBUG': return '#64748b';
      default: return '#94a3b8';
    }
  };

  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{ padding: '8px 14px', borderBottom: '1px solid rgba(255,255,255,0.04)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
          Live Log{channel ? ` · ${channel}` : ''}
        </span>
        <span style={{ fontSize: '0.62rem', color: 'var(--text-muted)' }}>
          {visible.length}/{logs.length} entries · newest first
        </span>
      </div>
      <div
        style={{
          maxHeight: 520,
          overflowY: 'auto',
          padding: '6px 10px',
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: '0.7rem',
          lineHeight: 1.6,
          background: 'rgba(0,0,0,0.15)',
        }}
      >
        {visible.map((entry, i) => {
          const time = new Date(entry.ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
          return (
            <div key={`${entry.ts}-${i}`} style={{ display: 'flex', gap: 6, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              <span style={{ color: 'var(--text-muted)', flexShrink: 0 }}>{time}</span>
              <span style={{ color: levelColor(entry.level), flexShrink: 0, width: 20, textAlign: 'center' }}>
                {entry.level === 'WARNING' ? 'W' : entry.level === 'ERROR' ? 'E' : entry.level[0]}
              </span>
              <span style={{ color: 'rgba(167, 139, 250, 0.7)', flexShrink: 0 }}>{entry.name}</span>
              <span style={{ color: entry.level === 'ERROR' ? '#ef4444' : entry.level === 'WARNING' ? '#eab308' : 'var(--text-secondary)' }}>
                {entry.msg}
              </span>
            </div>
          );
        })}
        {visible.length === 0 && (
          <div style={{ color: 'var(--text-muted)', padding: '10px 0', textAlign: 'center' }}>
            {channel ? `No entries yet for ${channel}…` : 'Waiting for log data…'}
          </div>
        )}
      </div>
    </div>
  );
}

interface OpenOrderData {
  orderId: number;
  side: string;
  price: number;
  origQty: number;
  executedQty: number;
  status: string;
  time: number;
  notional: number;
}

interface ExchangeData {
  orders: OpenOrderData[];
  balances: Record<string, { free: number; locked: number }>;
  price: number;
  /** Set by /api/exchange/{venue}; absent on the legacy Binance endpoint. */
  venue?: string;
  platform?: string;
  symbol?: string;
  base_asset?: string;
  quote_asset?: string;
}

// NOTE: use the shared `apiUrl()` helper instead of a local `API_BASE` so we
// pick up the same `VITE_BACKEND_URL` the rest of the app uses. The previous
// `VITE_API_BASE` env var was never set on Railway, so the fetch went to the
// SPA's own origin, hit nginx's SPA fallback (try_files → index.html), got
// HTML back with HTTP 200, JSON-parsed into a thrown error, and the silent
// catch left `exchangeData` as null — which is why the "Exchange Account"
// card and open orders never appeared on production.

// Shared session-storage key with StrategyConfigChat. Either of the server's
// two secrets (TRADE_API_SECRET / STRATEGY_CHAT_SECRET) is accepted by all
// authenticated endpoints, so reusing the same value is safe.
const TRADE_SECRET_STORAGE_KEY = 'algo_fun_strategy_api_secret';

interface ForceBuyResult {
  ok: boolean;
  bag_id?: number;
  fill_price?: number;
  filled_qty?: number;
  notional_usdt?: number;
  sell_target_price?: number;
  open_bags?: number;
  max_bullets?: number;
  reason?: string;
  message?: string;
}

interface ForceBuyButtonProps {
  readonly venueLabel: string;
  readonly defaultAmount: number;
  readonly minNotional: number;
  readonly quoteAsset: string;
  readonly atMaxAmmo: boolean;
  readonly openBags: number;
  readonly maxBullets: number;
}

function ForceBuyButton({
  venueLabel,
  defaultAmount,
  minNotional,
  quoteAsset,
  atMaxAmmo,
  openBags,
  maxBullets,
}: ForceBuyButtonProps) {
  const [stage, setStage] = useState<'idle' | 'confirm' | 'submitting' | 'done'>('idle');
  const [amountStr, setAmountStr] = useState(String(defaultAmount.toFixed(2)));
  const [secret, setSecret] = useState<string>(() => {
    try {
      return sessionStorage.getItem(TRADE_SECRET_STORAGE_KEY) ?? '';
    } catch {
      return '';
    }
  });
  const [result, setResult] = useState<ForceBuyResult | null>(null);

  const reset = () => {
    setStage('idle');
    setResult(null);
  };

  const submit = async () => {
    const amount = parseFloat(amountStr);
    if (!Number.isFinite(amount) || amount <= 0) {
      setResult({ ok: false, reason: 'bad_amount', message: 'Enter a positive amount.' });
      setStage('done');
      return;
    }
    setStage('submitting');
    setResult(null);
    try {
      const resp = await fetch(apiUrl(`/api/exchange/${venueLabel}/force-buy`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          amount_usdt: amount,
          secret: secret.trim() || undefined,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      // FastAPI HTTPException → { detail: <result-dict> }
      const payload: ForceBuyResult = resp.ok ? data : (data?.detail ?? data);
      if (resp.ok && secret.trim()) {
        try {
          sessionStorage.setItem(TRADE_SECRET_STORAGE_KEY, secret.trim());
        } catch {
          /* ignore */
        }
      }
      setResult({ ...payload, ok: !!payload?.ok && resp.ok });
      setStage('done');
    } catch (err) {
      setResult({
        ok: false,
        reason: 'network',
        message: err instanceof Error ? err.message : String(err),
      });
      setStage('done');
    }
  };

  // Idle: just the trigger button.
  if (stage === 'idle') {
    return (
      <button
        type="button"
        onClick={() => setStage('confirm')}
        disabled={atMaxAmmo}
        title={atMaxAmmo
          ? `MAX_AMMO: already holding ${openBags}/${maxBullets} bags. Wait for a TP fill.`
          : `Place a market BUY of ~$${defaultAmount.toFixed(2)} ${quoteAsset} on ${venueLabel}. The bot will bracket the new bag with a TP sell.`
        }
        style={{
          width: '100%',
          padding: '8px 12px',
          marginBottom: 8,
          background: atMaxAmmo ? 'rgba(148,163,184,0.08)' : 'rgba(34, 197, 94, 0.10)',
          border: `1px solid ${atMaxAmmo ? 'rgba(148,163,184,0.20)' : 'rgba(34, 197, 94, 0.35)'}`,
          color: atMaxAmmo ? 'var(--text-muted)' : 'var(--green-400)',
          borderRadius: 'var(--radius-sm)',
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: '0.72rem',
          fontWeight: 700,
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          cursor: atMaxAmmo ? 'not-allowed' : 'pointer',
        }}
      >
        ⚡ Buy ${defaultAmount.toFixed(2)} {quoteAsset} at market
        {atMaxAmmo && <span style={{ marginLeft: 6, opacity: 0.7 }}>· max ammo</span>}
      </button>
    );
  }

  // Confirmation form.
  if (stage === 'confirm' || stage === 'submitting') {
    const submitting = stage === 'submitting';
    return (
      <div
        style={{
          padding: '10px 12px',
          marginBottom: 8,
          background: 'rgba(34, 197, 94, 0.04)',
          border: '1px solid rgba(34, 197, 94, 0.25)',
          borderRadius: 'var(--radius-sm)',
        }}
      >
        <div
          style={{
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: '0.66rem',
            letterSpacing: '0.08em',
            fontWeight: 700,
            color: 'var(--green-400)',
            textTransform: 'uppercase',
            marginBottom: 8,
          }}
        >
          Confirm market BUY · {venueLabel}
        </div>
        <div style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
          <label style={{ flex: '1 1 120px', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: '0.6rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Spend ({quoteAsset})
            </span>
            <input
              type="number"
              min={minNotional}
              step={1}
              value={amountStr}
              onChange={(e) => setAmountStr(e.target.value)}
              disabled={submitting}
              style={{
                width: '100%',
                padding: '6px 8px',
                background: 'rgba(0,0,0,0.25)',
                border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: 4,
                color: 'var(--text-primary)',
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: '0.82rem',
              }}
            />
          </label>
          <label style={{ flex: '1 1 160px', display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span style={{ fontSize: '0.6rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              API secret (if set)
            </span>
            <input
              type="password"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              disabled={submitting}
              placeholder="TRADE_API_SECRET"
              style={{
                width: '100%',
                padding: '6px 8px',
                background: 'rgba(0,0,0,0.25)',
                border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: 4,
                color: 'var(--text-primary)',
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: '0.82rem',
              }}
            />
          </label>
        </div>
        <div style={{ fontSize: '0.66rem', color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.5 }}>
          MARKET orders pay taker fees. The bot will open a new bag and immediately
          place a take-profit SELL above the fill price.
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          <button
            type="button"
            onClick={submit}
            disabled={submitting}
            style={{
              flex: 1,
              padding: '6px 10px',
              background: submitting ? 'rgba(34, 197, 94, 0.10)' : 'rgba(34, 197, 94, 0.18)',
              border: '1px solid rgba(34, 197, 94, 0.45)',
              color: 'var(--green-400)',
              borderRadius: 4,
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: '0.72rem',
              fontWeight: 700,
              letterSpacing: '0.06em',
              cursor: submitting ? 'wait' : 'pointer',
              textTransform: 'uppercase',
            }}
          >
            {submitting ? 'Placing…' : 'Place market BUY'}
          </button>
          <button
            type="button"
            onClick={() => setStage('idle')}
            disabled={submitting}
            style={{
              padding: '6px 10px',
              background: 'transparent',
              border: '1px solid rgba(255,255,255,0.12)',
              color: 'var(--text-secondary)',
              borderRadius: 4,
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: '0.72rem',
              cursor: 'pointer',
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
            }}
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  // Done — render success or failure result.
  const success = result?.ok === true;
  return (
    <div
      style={{
        padding: '10px 12px',
        marginBottom: 8,
        background: success ? 'rgba(34, 197, 94, 0.06)' : 'rgba(239, 68, 68, 0.06)',
        border: `1px solid ${success ? 'rgba(34, 197, 94, 0.30)' : 'rgba(239, 68, 68, 0.30)'}`,
        borderRadius: 'var(--radius-sm)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 6,
          fontFamily: "'JetBrains Mono', monospace",
          fontSize: '0.66rem',
          letterSpacing: '0.08em',
          fontWeight: 700,
          color: success ? 'var(--green-400)' : 'var(--red-400)',
          textTransform: 'uppercase',
        }}
      >
        <span>{success ? '✓ Market BUY filled' : '✗ Market BUY refused'}</span>
        <button
          type="button"
          onClick={reset}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            fontSize: '0.7rem',
            padding: 0,
          }}
        >
          dismiss
        </button>
      </div>
      {success ? (
        <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', lineHeight: 1.55 }}>
          <div>Lot <strong>#{result?.bag_id}</strong> opened.</div>
          {typeof result?.fill_price === 'number' && (
            <div className="mono">
              Entry ${formatPrice(result.fill_price)} · qty {(result.filled_qty ?? 0).toFixed(8)}
              {typeof result.notional_usdt === 'number' && ` (≈ $${result.notional_usdt.toFixed(2)} ${quoteAsset})`}
            </div>
          )}
          {typeof result?.sell_target_price === 'number' && (
            <div className="mono">
              TP at <span style={{ color: 'var(--cyan-400)' }}>${formatPrice(result.sell_target_price)}</span>
              {typeof result.open_bags === 'number' && typeof result.max_bullets === 'number' && (
                <> · bags {result.open_bags}/{result.max_bullets}</>
              )}
            </div>
          )}
        </div>
      ) : (
        <div style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
          <div className="mono" style={{ marginBottom: 4 }}>
            {result?.reason ? `[${result.reason}] ` : ''}{result?.message || 'Unknown error.'}
          </div>
        </div>
      )}
    </div>
  );
}

export function LiveDashboardContent({ s, runnerLabel, venueLabel }: Props) {
  const markers = useMemo(() => cyclesToMarkers(s), [s.cycles, s.positions, s.take_profit_pct]);
  const strategy = s.strategy;
  const hasStrategy = !!strategy?.macro_regime;

  const [exchangeData, setExchangeData] = useState<ExchangeData | null>(null);
  // venueLabel === undefined → caller doesn't pass this prop yet → use legacy
  //   Binance-only endpoint (back-compat).
  // venueLabel === null      → in-memory paper venue → no exchange to query;
  //   the dashboard hides the "Exchange Account" card.
  // venueLabel === string    → hit the per-venue endpoint.
  const exchangeUrl = venueLabel === undefined
    ? apiUrl('/api/open-orders')
    : (venueLabel ? apiUrl(`/api/exchange/${venueLabel}`) : null);
  const fetchExchange = useCallback(async () => {
    if (!exchangeUrl) return;
    try {
      const resp = await fetch(exchangeUrl);
      if (!resp.ok) {
        console.warn('[exchange] non-200 from', exchangeUrl, resp.status);
        return;
      }
      // Guard against the SPA fallback returning text/html with HTTP 200
      // (what happened when the URL accidentally pointed at the frontend
      // origin instead of the backend). Only parse if the server actually
      // says it's JSON.
      const ct = resp.headers.get('content-type') || '';
      if (!ct.includes('application/json')) {
        console.warn('[exchange] expected JSON, got', ct, 'from', exchangeUrl);
        return;
      }
      setExchangeData(await resp.json());
    } catch (err) {
      console.warn('[exchange] fetch failed for', exchangeUrl, err);
    }
  }, [exchangeUrl]);

  useEffect(() => {
    setExchangeData(null);
    if (!exchangeUrl) return;
    fetchExchange();
    const interval = setInterval(fetchExchange, 15000);
    return () => clearInterval(interval);
  }, [fetchExchange, exchangeUrl]);

  const isGridMode = !!(s as any).grid_mode;
  const gridData = (s as any).grid;
  const pos = s.positions[0];
  const holdingPositions = s.positions.filter((p: any) => p.state === 'HOLDING');
  const isInTrade = isGridMode
    ? holdingPositions.length > 0
    : pos && (pos.state === 'HOLDING' || pos.state === 'SELL_PLACED');
  const unrealizedPct = !isGridMode && isInTrade && pos.entry_price > 0
    ? ((s.price - pos.entry_price) / pos.entry_price) * 100
    : 0;
  const unrealizedUsdt = !isGridMode && isInTrade && pos.entry_price > 0 && pos.slot_qty
    ? (s.price - pos.entry_price) * pos.slot_qty
    : 0;
  const gridTotalUnrealized = isGridMode
    ? holdingPositions.reduce((sum: number, p: any) => sum + (p.unrealized_usdt || 0), 0)
    : 0;

  // Build the live "ladder & net" overlay for the chart so the user can
  // SEE what the bot is reasoning about — anchor/local high (the rungs
  // of the ladder), the resting BUY (the catching net), the trail
  // re-arm trigger, and every open bag's entry + TP sale. Only sourced
  // from the LIFO grid snapshot; non-grid runners get a plain chart.
  const gridOverlay: GridOverlay | null = useMemo(() => {
    if (!isGridMode || !gridData) return null;
    const bags = holdingPositions
      .filter((p: any) => Number(p.entry_price) > 0)
      .map((p: any) => ({
        bagId: Number(p.slot_id ?? 0),
        entry: Number(p.entry_price ?? 0),
        // Engine sets `tp_price` per bag (sell_target_price). Fall back
        // to a derived value if an older snapshot omits it so the line
        // still appears.
        tp: Number(
          p.tp_price
          ?? (Number(p.entry_price ?? 0) * (1 + Number(gridData.tp_pct ?? 0) / 100)),
        ),
      }));
    return {
      anchor: Number(gridData.anchor_price ?? 0) || undefined,
      localHigh: Number(gridData.local_high ?? 0) || undefined,
      restingBuy: gridData.resting_buy
        ? {
          price: Number(gridData.resting_buy.price ?? 0),
          tag: gridData.resting_buy.kind,
        }
        : undefined,
      pendingBuyTarget: gridData.pending_buy?.target_price
        ? Number(gridData.pending_buy.target_price)
        : undefined,
      trailTriggerHigh: gridData.pending_buy?.reason === 'trail'
        && gridData.pending_buy?.trigger_high_price
        ? Number(gridData.pending_buy.trigger_high_price)
        : undefined,
      bags,
      tpPct: typeof gridData.tp_pct === 'number' ? gridData.tp_pct : undefined,
      dipPct: typeof gridData.dip_pct === 'number' ? gridData.dip_pct : undefined,
    };
  }, [isGridMode, gridData, holdingPositions]);

  const sessionPnl = s.session.equity_usdt - s.session.starting_balance;
  const sessionPct = s.session.starting_balance ? (sessionPnl / s.session.starting_balance) * 100 : 0;

  const wins = s.cycles.filter((c) => c.net_pnl > 0).length;
  const losses = s.cycles.filter((c) => c.net_pnl <= 0).length;
  const winRate = s.cycles.length > 0 ? (wins / s.cycles.length) * 100 : 0;
  const bestTrade = s.cycles.length > 0 ? Math.max(...s.cycles.map((c) => c.gross_pct)) : 0;
  const worstTrade = s.cycles.length > 0 ? Math.min(...s.cycles.map((c) => c.gross_pct)) : 0;
  const avgPnl = s.cycles.length > 0 ? s.cycles.reduce((sum, c) => sum + c.net_pnl, 0) / s.cycles.length : 0;
  const priceVsMa = s.ma ? ((s.price - s.ma) / s.ma) * 100 : null;

  return (
    <>
      {/* Header */}
      <div className="paper-v2-market-strip">
        <div className="paper-v2-strip-left">
          <span className="paper-v2-symbol">{s.symbol}</span>
          <span
            className="env-badge"
            style={{
              background: s.mainnet ? 'rgba(34, 211, 153, 0.15)' : 'rgba(234, 179, 8, 0.15)',
              color: s.mainnet ? 'var(--green-400)' : 'var(--amber-400)',
            }}
          >
            {s.mainnet ? 'LIVE' : 'TESTNET'}
          </span>
          {isInTrade && (
            <span
              className="env-badge"
              style={{ background: 'rgba(34, 211, 238, 0.15)', color: 'var(--cyan-400)' }}
            >
              IN TRADE
            </span>
          )}
          {hasStrategy && <StrategySignalBadge action={strategy.action} />}
        </div>
        <span className="paper-v2-uptime">uptime {formatUptime(s.uptime_s)}</span>
      </div>

      {/* Live Log Feed */}
      {s.logs && <LogFeed logs={s.logs} channel={runnerLabel ?? null} />}

      {/* Price + Chart Card */}
      <div className="card paper-v2-price-card">
        <div className="paper-v2-price-top">
          <div>
            <span className="paper-v2-price-kicker">Spot</span>
            <div className="paper-v2-price-main mono">${formatPrice(s.price)}</div>
          </div>
          <div className="paper-v2-price-stats">
            {s.ma && (
              <div className="paper-v2-stat">
                <span className="paper-v2-stat-label">EMA 20</span>
                <span className="paper-v2-stat-value mono">${formatPrice(s.ma)}</span>
              </div>
            )}
            {priceVsMa !== null && (
              <div className="paper-v2-stat">
                <span className="paper-v2-stat-label">vs EMA</span>
                <span className={`paper-v2-stat-value mono ${pnlColorClass(priceVsMa)}`}>
                  {formatPct(priceVsMa)}
                </span>
              </div>
            )}
            {s.prices.length >= 2 && (
              <>
                <div className="paper-v2-stat">
                  <span className="paper-v2-stat-label">Session high</span>
                  <span className="paper-v2-stat-value mono">${formatPrice(Math.max(...s.prices))}</span>
                </div>
                <div className="paper-v2-stat">
                  <span className="paper-v2-stat-label">Session low</span>
                  <span className="paper-v2-stat-value mono">${formatPrice(Math.min(...s.prices))}</span>
                </div>
              </>
            )}
          </div>
        </div>
        <TradingChart
          markers={markers}
          height={420}
          candlesEndpoint="/api/candles"
          gridOverlay={gridOverlay}
        />
      </div>

      {/* Strategy Analysis Card */}
      {isGridMode && gridData ? (() => {
        const hasGeofence = typeof gridData.geofence_low === 'number'
          && typeof gridData.geofence_high === 'number'
          && Number.isFinite(gridData.geofence_low)
          && Number.isFinite(gridData.geofence_high);
        const cardLabel = hasGeofence ? 'Strategy — Sandbox Grid' : 'Strategy — LIFO Grid';
        const tileBaseStyle = {
          padding: '10px 12px',
          background: 'rgba(255,255,255,0.02)',
          borderRadius: 'var(--radius-sm)',
          border: '1px solid rgba(255,255,255,0.04)',
        };
        const tileLabelStyle = {
          fontSize: '0.62rem',
          color: 'var(--text-dim)',
          textTransform: 'uppercase' as const,
          fontWeight: 600,
          marginBottom: 4,
        };
        return (
        <div className="card">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
              {cardLabel}
            </div>
            <span style={{
              padding: '3px 10px', borderRadius: 6, fontSize: '0.68rem', fontWeight: 700,
              background: gridData.status === 'ACTIVE' ? 'rgba(34, 197, 94, 0.15)' : 'rgba(234, 179, 8, 0.15)',
              color: gridData.status === 'ACTIVE' ? 'var(--green-400)' : 'var(--amber-400)',
              fontFamily: "'JetBrains Mono', monospace",
            }}>
              {gridData.status}
            </span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8, marginBottom: 14 }}>
            {hasGeofence ? (
              <div style={tileBaseStyle}>
                <div style={tileLabelStyle}>Geofence</div>
                <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--cyan-400)' }}>
                  ${(gridData.geofence_low / 1000).toFixed(0)}k–${(gridData.geofence_high / 1000).toFixed(0)}k
                </div>
              </div>
            ) : (
              <div style={tileBaseStyle}>
                <div style={tileLabelStyle}>Anchor</div>
                <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--cyan-400)' }}>
                  ${formatPrice(gridData.anchor_price ?? 0)}
                </div>
              </div>
            )}
            <div style={tileBaseStyle}>
              <div style={tileLabelStyle}>Open Lots</div>
              <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                {gridData.open_lots} / {gridData.max_lots}
              </div>
            </div>
            <div style={tileBaseStyle}>
              <div style={tileLabelStyle}>Tranche</div>
              <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                ${gridData.tranche_usdt}
              </div>
            </div>
            <div style={tileBaseStyle}>
              <div style={tileLabelStyle}>Config</div>
              <div className="mono" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                TP +{gridData.tp_pct}% / Dip −{gridData.dip_pct}%
                {typeof gridData.trail_step_pct === 'number' && (
                  <span style={{ color: 'var(--text-muted)' }}> · Step {gridData.trail_step_pct}%</span>
                )}
              </div>
            </div>
            <div style={tileBaseStyle}>
              <div style={tileLabelStyle}>Local High</div>
              <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                ${formatPrice(gridData.local_high)}
              </div>
            </div>
            <div style={tileBaseStyle}>
              <div style={tileLabelStyle}>Total P&L</div>
              <div className={`mono ${pnlColorClass(gridData.total_pnl)}`} style={{ fontSize: '0.82rem', fontWeight: 700 }}>
                {formatPnl(gridData.total_pnl, 4)} USDT
              </div>
              <div style={{ fontSize: '0.62rem', color: 'var(--text-muted)' }}>{gridData.closed_count} cycles</div>
            </div>
          </div>

          {/* Manual market-buy: live venues only. Force-opens a bag on demand;
              the bot then brackets it with the standard TP sell. Disabled when
              the engine already holds the maximum number of bags. */}
          {(venueLabel === 'binance-live' || venueLabel === 'revolut-live') && (
            <ForceBuyButton
              venueLabel={venueLabel}
              defaultAmount={Number(gridData.tranche_usdt) || 10}
              minNotional={5}
              quoteAsset={
                exchangeData?.quote_asset
                ?? (venueLabel === 'revolut-live' ? 'USDC' : 'USDT')
              }
              atMaxAmmo={(gridData.open_lots ?? 0) >= (gridData.max_lots ?? 0)}
              openBags={gridData.open_lots ?? 0}
              maxBullets={gridData.max_lots ?? 0}
            />
          )}

          {gridData.resting_buy && (
            <div style={{ padding: '8px 12px', background: 'rgba(34, 197, 94, 0.05)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(34, 197, 94, 0.1)', marginBottom: 8 }}>
              <div className="mono" style={{ fontSize: '0.75rem', color: 'var(--green-400)' }}>
                Resting BUY @ ${formatPrice(gridData.resting_buy.price)}
                <span style={{ color: 'var(--text-muted)', marginLeft: 8 }}>
                  ({gridData.resting_buy.kind}) {gridData.resting_buy.distance_pct > 0 ? '+' : ''}{gridData.resting_buy.distance_pct?.toFixed(2)}% from spot
                </span>
              </div>
            </div>
          )}

          {!gridData.resting_buy && gridData.pending_buy && (() => {
            const pb = gridData.pending_buy;
            const reasonColor: Record<string, string> = {
              trail: 'var(--amber-400, #f59e0b)',
              backoff: 'var(--red-400)',
              max_ammo: 'var(--text-muted)',
            };
            const reasonLabel: Record<string, string> = {
              trail: 'WAITING · trail re-arm',
              backoff: 'PAUSED · venue backoff',
              max_ammo: 'IDLE · max ammo deployed',
            };
            const accent = reasonColor[pb.reason] || 'var(--text-muted)';
            return (
              <div
                style={{
                  padding: '10px 12px',
                  background: 'rgba(245, 158, 11, 0.04)',
                  borderRadius: 'var(--radius-sm)',
                  border: '1px solid rgba(245, 158, 11, 0.18)',
                  marginBottom: 8,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: 6,
                    fontFamily: "'JetBrains Mono', monospace",
                    fontSize: '0.65rem',
                    letterSpacing: '0.08em',
                    fontWeight: 700,
                    color: accent,
                    textTransform: 'uppercase',
                  }}
                >
                  <span>{reasonLabel[pb.reason] || 'WAITING'}</span>
                  {pb.reason === 'backoff' && typeof pb.backoff_remaining_s === 'number' && (
                    <span style={{ color: 'var(--text-muted)' }}>{pb.backoff_remaining_s}s left</span>
                  )}
                </div>

                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
                    gap: 6,
                    marginBottom: 8,
                  }}
                >
                  {typeof pb.target_price === 'number' && pb.target_price > 0 && (
                    <div style={{ padding: '6px 8px', background: 'rgba(255,255,255,0.02)', borderRadius: 4 }}>
                      <div style={{ fontSize: '0.6rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>BUY target</div>
                      <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--green-400)' }}>
                        ${formatPrice(pb.target_price)}
                      </div>
                      {typeof pb.spot_to_target_pct === 'number' && (
                        <div style={{ fontSize: '0.62rem', color: 'var(--text-muted)' }}>
                          {pb.spot_to_target_pct > 0 ? '+' : ''}{pb.spot_to_target_pct.toFixed(2)}% from spot
                        </div>
                      )}
                    </div>
                  )}

                  {typeof pb.projected_sell_target === 'number' && pb.projected_sell_target > 0 && (
                    <div style={{ padding: '6px 8px', background: 'rgba(255,255,255,0.02)', borderRadius: 4 }}>
                      <div style={{ fontSize: '0.6rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>↳ projected SELL</div>
                      <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: 'var(--cyan-400)' }}>
                        ${formatPrice(pb.projected_sell_target)}
                      </div>
                      <div style={{ fontSize: '0.62rem', color: 'var(--text-muted)' }}>
                        auto-bracket after fill
                      </div>
                    </div>
                  )}

                  {pb.reason === 'trail' && typeof pb.trigger_high_price === 'number' && pb.trigger_high_price > 0 && (
                    <div style={{ padding: '6px 8px', background: 'rgba(255,255,255,0.02)', borderRadius: 4 }}>
                      <div style={{ fontSize: '0.6rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>re-arm at HIGH</div>
                      <div className="mono" style={{ fontSize: '0.82rem', fontWeight: 700, color: accent }}>
                        ${formatPrice(pb.trigger_high_price)}
                      </div>
                      {typeof pb.high_to_trigger_pct === 'number' && (
                        <div style={{ fontSize: '0.62rem', color: 'var(--text-muted)' }}>
                          need {pb.high_to_trigger_pct > 0 ? `+${pb.high_to_trigger_pct.toFixed(2)}%` : 'ready'}
                        </div>
                      )}
                    </div>
                  )}
                </div>

                {pb.label && (
                  <div style={{ fontSize: '0.7rem', color: 'var(--text-secondary)', lineHeight: 1.45, fontStyle: 'italic' }}>
                    {pb.label}
                  </div>
                )}
              </div>
            );
          })()}

          {strategy?.reasons && strategy.reasons.length > 0 && (
            <div style={{ padding: '8px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', fontSize: '0.72rem', color: 'var(--text-muted)', fontFamily: "'JetBrains Mono', monospace" }}>
              {strategy.reasons.map((r: string, i: number) => (
                <div key={i} style={{ padding: '1px 0' }}>{r}</div>
              ))}
            </div>
          )}
        </div>
        );
      })() : hasStrategy && (
        <div className="card" style={{ borderColor: 'rgba(167, 139, 250, 0.12)' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
              Strategy — Trend-Aware Maker
            </div>
            <StrategySignalBadge action={strategy.action} />
          </div>

          {/* Multi-timeframe layers */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 8, marginBottom: 14 }}>
            <div style={{ padding: '10px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <LayerDot pass={strategy.macro_regime !== 'BEARISH'} />
                <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600 }}>Macro</span>
              </div>
              <div className={regimeColorClass(strategy.macro_regime)} style={{ fontSize: '0.85rem', fontWeight: 700 }}>
                {strategy.macro_regime.replace('_', ' ')}
              </div>
              {strategy.macro_detail && (
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 2 }}>{strategy.macro_detail}</div>
              )}
            </div>

            <div style={{ padding: '10px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <LayerDot pass={strategy.daily_bias !== 'BEARISH'} />
                <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600 }}>Daily</span>
              </div>
              <div style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--cyan-400)' }}>
                {strategy.daily_bias}
              </div>
              {strategy.daily_detail && (
                <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 2 }}>{strategy.daily_detail}</div>
              )}
            </div>

            <div style={{ padding: '10px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <LayerDot pass={strategy.trend_4h === 'up'} />
                <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600 }}>4H Trend</span>
              </div>
              <div style={{ fontSize: '0.85rem', fontWeight: 700, color: strategy.trend_4h === 'up' ? 'var(--green-400)' : strategy.trend_4h === 'down' ? 'var(--red-400)' : 'var(--text-secondary)' }}>
                {(strategy.trend_4h || 'unknown').toUpperCase()}
              </div>
            </div>

            <div style={{ padding: '10px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <LayerDot pass={strategy.market_mode === 'UP'} />
                <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600 }}>1H Mode</span>
              </div>
              <div className={modeColorClass(strategy.market_mode)} style={{ fontSize: '0.85rem', fontWeight: 700 }}>
                {strategy.market_mode}
              </div>
            </div>

            <div style={{ padding: '10px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <LayerDot pass={strategy.pullback_valid} />
                <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600 }}>5M Pullback</span>
              </div>
              <div style={{ fontSize: '0.85rem', fontWeight: 700, color: strategy.pullback_valid ? 'var(--green-400)' : 'var(--text-muted)' }}>
                {strategy.pullback_valid ? `${strategy.pullback_pct?.toFixed(2)}% dip` : 'No pullback'}
              </div>
            </div>

            <div style={{ padding: '10px 12px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600 }}>Config</span>
              </div>
              <div className="mono" style={{ fontSize: '0.78rem', color: 'var(--text-secondary)' }}>
                TP {s.take_profit_pct}% / SL {s.stop_loss_pct}%
              </div>
              <div className="mono" style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 2 }}>
                Size ${s.trade_size_usdt}
              </div>
            </div>
          </div>

          {/* Strategy reasoning */}
          {strategy.reasons && strategy.reasons.length > 0 && (
            <div style={{ padding: '10px 14px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)', border: '1px solid rgba(255,255,255,0.04)', marginBottom: 10 }}>
              <div style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase', fontWeight: 600, marginBottom: 6 }}>
                Why?
              </div>
              {strategy.reasons.map((r, i) => (
                <div key={i} style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', padding: '2px 0', fontFamily: "'JetBrains Mono', monospace" }}>
                  {r}
                </div>
              ))}
            </div>
          )}

          {/* Cooldown & blocks */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {strategy.cooldown_s > 0 && (
              <span style={{ fontSize: '0.72rem', color: 'var(--amber-400)', fontFamily: "'JetBrains Mono', monospace" }}>
                Cooldown {strategy.cooldown_s}s
              </span>
            )}
            {strategy.entry_block && (
              <span style={{ fontSize: '0.72rem', color: 'var(--amber-400)', fontFamily: "'JetBrains Mono', monospace" }}>
                {strategy.entry_block}
              </span>
            )}
            {strategy.sell_block && (
              <span style={{ fontSize: '0.72rem', color: 'var(--red-400)', fontFamily: "'JetBrains Mono', monospace" }}>
                {strategy.sell_block}
              </span>
            )}
            {strategy.suggested_entry && (
              <span className="mono" style={{ fontSize: '0.72rem', color: 'var(--green-400)' }}>
                Target entry: ${formatPrice(strategy.suggested_entry)}
              </span>
            )}
          </div>

          {strategy.wallet_base_qty > 0 && (
            <div style={{ marginTop: 8, padding: '6px 12px', background: 'rgba(255, 255, 255, 0.03)', borderRadius: 'var(--radius-sm)', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
              Wallet BTC: {strategy.wallet_base_qty.toFixed(8)} (not auto-managed)
            </div>
          )}
        </div>
      )}

      {/* Active Position / Grid Lots Card */}
      {isGridMode ? (
        <div className="card" style={{
          borderColor: holdingPositions.length > 0
            ? gridTotalUnrealized >= 0
              ? 'rgba(34, 197, 94, 0.2)'
              : 'rgba(239, 68, 68, 0.2)'
            : undefined,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
              Grid Lots
            </div>
            <span style={{
              padding: '3px 10px', borderRadius: 6, fontSize: '0.68rem', fontWeight: 700,
              fontFamily: "'JetBrains Mono', monospace",
              background: holdingPositions.length > 0 ? 'rgba(34, 211, 238, 0.15)' : 'rgba(148, 163, 184, 0.1)',
              color: holdingPositions.length > 0 ? 'var(--cyan-400)' : 'var(--text-muted)',
            }}>
              {holdingPositions.length > 0 ? `${holdingPositions.length} OPEN` : 'FLAT'}
            </span>
          </div>

          {holdingPositions.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {holdingPositions.map((lot: any) => {
                const lotPnlPct = lot.unrealized_pct || 0;
                const lotPnlUsdt = lot.unrealized_usdt || 0;
                return (
                  <div key={lot.slot_id} style={{
                    display: 'grid', gridTemplateColumns: '40px 1fr 1fr 1fr 1fr', gap: 8, alignItems: 'center',
                    padding: '8px 10px', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)',
                    border: '1px solid rgba(255,255,255,0.04)',
                  }}>
                    <div className="mono" style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>#{lot.slot_id}</div>
                    <div>
                      <div className="mono" style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--text-primary)' }}>
                        ${formatPrice(lot.entry_price)}
                      </div>
                      <div style={{ fontSize: '0.58rem', color: 'var(--text-dim)' }}>ENTRY</div>
                    </div>
                    <div>
                      <div className="mono" style={{ fontSize: '0.78rem', fontWeight: 600, color: 'var(--green-400)' }}>
                        ${formatPrice(lot.tp_price)}
                      </div>
                      <div style={{ fontSize: '0.58rem', color: 'var(--text-dim)' }}>TP</div>
                    </div>
                    <div>
                      <div className={`mono ${pnlColorClass(lotPnlPct)}`} style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                        {formatPct(lotPnlPct)}
                      </div>
                      <div style={{ fontSize: '0.58rem', color: 'var(--text-dim)' }}>P&L</div>
                    </div>
                    <div>
                      <div className={`mono ${pnlColorClass(lotPnlUsdt)}`} style={{ fontSize: '0.78rem', fontWeight: 600 }}>
                        {lotPnlUsdt >= 0 ? '+' : ''}{lotPnlUsdt.toFixed(4)}
                      </div>
                      <div style={{ fontSize: '0.58rem', color: 'var(--text-dim)' }}>USDT</div>
                    </div>
                  </div>
                );
              })}
              <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 10px', fontSize: '0.72rem', color: 'var(--text-muted)', borderTop: '1px solid rgba(255,255,255,0.04)', marginTop: 4 }}>
                <span>Total unrealized:</span>
                <span className={`mono ${pnlColorClass(gridTotalUnrealized)}`} style={{ fontWeight: 700 }}>
                  {gridTotalUnrealized >= 0 ? '+' : ''}{gridTotalUnrealized.toFixed(4)} USDT
                </span>
              </div>
            </div>
          ) : (
            <div style={{ padding: '20px 0', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              No open lots — trailing for entry
            </div>
          )}
        </div>
      ) : (
        <div className="card" style={{
          borderColor: isInTrade
            ? unrealizedPct >= 0
              ? 'rgba(34, 197, 94, 0.2)'
              : 'rgba(239, 68, 68, 0.2)'
            : undefined,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
              Position
            </div>
            <span style={{
              padding: '3px 10px', borderRadius: 6, fontSize: '0.68rem', fontWeight: 700,
              letterSpacing: '0.06em', fontFamily: "'JetBrains Mono', monospace",
              background: isInTrade ? 'rgba(34, 211, 238, 0.15)' : 'rgba(148, 163, 184, 0.1)',
              color: isInTrade ? 'var(--cyan-400)' : 'var(--text-muted)',
            }}>
              {pos?.state?.replace('_', ' ') || 'WATCHING'}
            </span>
          </div>

          {isInTrade && pos.entry_price > 0 ? (
            <>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 14 }}>
                <div style={{ textAlign: 'center', padding: '10px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
                  <div className="mono" style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>${formatPrice(pos.entry_price)}</div>
                  <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Entry</div>
                </div>
                <div style={{ textAlign: 'center', padding: '10px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
                  <div className={`mono ${pnlColorClass(unrealizedPct)}`} style={{ fontSize: '1rem', fontWeight: 700 }}>{formatPct(unrealizedPct)}</div>
                  <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Unrealized</div>
                </div>
                <div style={{ textAlign: 'center', padding: '10px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
                  <div className={`mono ${pnlColorClass(unrealizedUsdt)}`} style={{ fontSize: '1rem', fontWeight: 700 }}>{formatPnl(unrealizedUsdt, 2)} USDT</div>
                  <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>P&L</div>
                </div>
              </div>
              {pos.slot_qty && (
                <div className="mono" style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 4 }}>
                  Size: {pos.slot_qty.toFixed(6)} BTC (~${(pos.slot_qty * s.price).toFixed(2)})
                </div>
              )}
            </>
          ) : (
            <div style={{ padding: '20px 0', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
              No active position — scanning for entries
            </div>
          )}
        </div>
      )}

      {/* Exchange Orders & Balances */}
      {exchangeData && (() => {
        const trackedIds = new Set<string>();
        const restingId = (s as any)?.grid?.resting_buy?.order_id;
        if (restingId) trackedIds.add(String(restingId));
        for (const p of s.positions || []) {
          const sid = (p as any).sell_order_id;
          if (sid) trackedIds.add(String(sid));
        }
        // Venue-aware asset labels. Falls back to USDT/BTC for the legacy
        // /api/open-orders endpoint (which doesn't return base/quote_asset).
        const quoteAsset = exchangeData.quote_asset || 'USDT';
        const baseAsset = exchangeData.base_asset || 'BTC';
        const platform = exchangeData.platform
          || (venueLabel?.startsWith('revolut') ? 'revolut' : 'binance');
        const accountTitle = platform === 'revolut'
          ? 'Revolut X Account'
          : 'Binance Account';
        const isQuoteStable = (a: string) => a === quoteAsset || a === 'USDT' || a === 'USDC' || a === 'USD';
        const fmtQuote = (v: number) => `$${v.toFixed(2)}`;
        const quote = exchangeData.balances?.[quoteAsset] || { free: 0, locked: 0 };
        const base = exchangeData.balances?.[baseAsset] || { free: 0, locked: 0 };
        const totalValue = (quote.free + quote.locked)
          + (base.free + base.locked) * exchangeData.price;
        const sortedOrders = [...exchangeData.orders].sort((a, b) => b.time - a.time);
        const trackedCount = sortedOrders.filter((o) => trackedIds.has(String(o.orderId))).length;
        const orphanCount = sortedOrders.length - trackedCount;
        return (
          <div className="card">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
                {accountTitle}
                {exchangeData.symbol && (
                  <span style={{ color: 'var(--text-muted)', marginLeft: 6, textTransform: 'none', letterSpacing: 0 }}>
                    · {exchangeData.symbol}
                  </span>
                )}
                <span style={{ marginLeft: 6, color: 'var(--text-muted)', textTransform: 'none', letterSpacing: 0 }}>
                  · Total ≈ ${totalValue.toFixed(2)}
                </span>
              </div>
              <span style={{
                padding: '3px 10px', borderRadius: 6, fontSize: '0.65rem', fontWeight: 600,
                background: 'rgba(34, 197, 94, 0.1)', color: 'var(--green-400)',
                fontFamily: "'JetBrains Mono', monospace",
              }}>
                LIVE
              </span>
            </div>

            {/* Balances */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 }}>
              {Object.entries(exchangeData.balances).map(([asset, bal]) => {
                const isStable = isQuoteStable(asset);
                const total = bal.free + bal.locked;
                const usdValue = isStable ? total : total * exchangeData.price;
                const isLow = asset === quoteAsset && bal.free < 6;
                return (
                  <div key={asset} style={{
                    padding: '10px 12px',
                    background: 'rgba(255,255,255,0.02)',
                    borderRadius: 'var(--radius-sm)',
                    border: isLow ? '1px solid rgba(248,113,113,0.4)' : '1px solid transparent',
                  }}>
                    <div className="mono" style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--text-primary)' }}>
                      {isStable ? fmtQuote(bal.free) : bal.free.toFixed(8)}
                      <span style={{ fontSize: '0.65rem', color: 'var(--text-dim)', marginLeft: 6 }}>free</span>
                    </div>
                    {bal.locked > 0 && (
                      <div className="mono" style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 2 }}>
                        + {isStable ? fmtQuote(bal.locked) : bal.locked.toFixed(8)} locked
                      </div>
                    )}
                    <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase', marginTop: 4 }}>
                      {asset} · ≈ ${usdValue.toFixed(2)}
                      {isLow && <span style={{ color: 'var(--red-400)', marginLeft: 6, textTransform: 'none' }}>low — bot can&apos;t place new buys</span>}
                    </div>
                  </div>
                );
              })}
              {Object.keys(exchangeData.balances).length === 0 && (
                <div style={{
                  gridColumn: '1 / -1',
                  padding: '14px',
                  textAlign: 'center',
                  color: 'var(--text-muted)',
                  fontSize: '0.78rem',
                  background: 'rgba(255,255,255,0.02)',
                  borderRadius: 'var(--radius-sm)',
                }}>
                  No {quoteAsset} or {baseAsset} balance — fund the account to start trading.
                </div>
              )}
            </div>

            {/* Open Orders */}
            {sortedOrders.length > 0 ? (
              <div>
                <div style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase',
                  marginBottom: 6, fontWeight: 600,
                }}>
                  <span>Open Orders ({sortedOrders.length})</span>
                  <span style={{ color: 'var(--text-muted)', textTransform: 'none' }}>
                    {trackedCount} tracked by bot
                    {orphanCount > 0 && (
                      <span style={{ color: 'var(--amber-400)' }}> · {orphanCount} orphan</span>
                    )}
                  </span>
                </div>
                {sortedOrders.map((o) => {
                  const tracked = trackedIds.has(String(o.orderId));
                  const dt = o.time ? new Date(o.time) : null;
                  const stamp = dt
                    ? `${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
                    : '—';
                  return (
                    <div key={o.orderId} style={{
                      display: 'grid',
                      gridTemplateColumns: '50px 70px 90px 90px 70px 90px',
                      gap: 8, alignItems: 'center',
                      padding: '6px 10px',
                      background: tracked ? 'rgba(34,197,94,0.04)' : 'rgba(234,179,8,0.04)',
                      borderLeft: tracked ? '2px solid rgba(34,197,94,0.4)' : '2px solid rgba(234,179,8,0.4)',
                      borderRadius: 'var(--radius-sm)',
                      marginBottom: 3, fontSize: '0.72rem', fontFamily: "'JetBrains Mono', monospace",
                    }}>
                      <span style={{ color: o.side === 'BUY' ? 'var(--green-400)' : 'var(--red-400)', fontWeight: 700 }}>
                        {o.side}
                      </span>
                      <span style={{ color: 'var(--text-dim)', fontSize: '0.65rem' }}>{stamp}</span>
                      <span style={{ color: 'var(--text-primary)', textAlign: 'right' }}>${formatPrice(o.price)}</span>
                      <span style={{ color: 'var(--text-muted)', textAlign: 'right' }}>{o.origQty.toFixed(8)}</span>
                      <span style={{ color: 'var(--text-dim)', textAlign: 'right' }}>${o.notional.toFixed(2)}</span>
                      <span style={{
                        padding: '2px 6px', borderRadius: 4, fontSize: '0.58rem',
                        textAlign: 'center', fontWeight: 600,
                        background: tracked ? 'rgba(34,197,94,0.15)' : 'rgba(234,179,8,0.15)',
                        color: tracked ? 'var(--green-400)' : 'var(--amber-400)',
                      }}>
                        {tracked ? 'BOT' : 'ORPHAN'}
                      </span>
                    </div>
                  );
                })}
                {orphanCount > 0 && (
                  <div style={{
                    marginTop: 8, padding: '8px 10px', borderRadius: 'var(--radius-sm)',
                    background: 'rgba(234,179,8,0.06)', border: '1px solid rgba(234,179,8,0.18)',
                    fontSize: '0.7rem', color: 'var(--text-muted)', lineHeight: 1.45,
                  }}>
                    <strong style={{ color: 'var(--amber-400)' }}>{orphanCount} orphan order{orphanCount === 1 ? '' : 's'}</strong>
                    {' '}from earlier sessions are holding ~$
                    {sortedOrders.filter((o) => !trackedIds.has(String(o.orderId)))
                      .reduce((acc, o) => acc + o.notional, 0).toFixed(2)}
                    {' '}in funds. Cancel them on Binance to free up USDT for the bot.
                  </div>
                )}
              </div>
            ) : (
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textAlign: 'center', padding: '6px 0' }}>
                No open orders
              </div>
            )}
          </div>
        );
      })()}

      {/* Performance Stats */}
      <div
        className="card"
        style={{
          borderColor: s.alltime.total_net_pnl > 0
            ? 'rgba(52, 211, 153, 0.15)'
            : s.alltime.total_net_pnl < 0
              ? 'rgba(248, 113, 113, 0.15)'
              : undefined,
          boxShadow: s.alltime.total_net_pnl > 0
            ? 'var(--shadow-glow-green)'
            : s.alltime.total_net_pnl < 0
              ? 'var(--shadow-glow-red)'
              : undefined,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <div>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
              Portfolio
            </div>
            <div className={`mono ${pnlColorClass(sessionPnl)}`} style={{ fontSize: '1.6rem', fontWeight: 700, lineHeight: 1.2 }}>
              ${s.session.equity_usdt.toFixed(2)}
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <span className={`mono ${pnlColorClass(sessionPnl)}`} style={{ fontSize: '1.1rem', fontWeight: 700 }}>
              {formatPnl(sessionPnl, 2)} USDT
            </span>
            <div className={`mono ${pnlColorClass(sessionPnl)}`} style={{ fontSize: '0.8rem' }}>
              {formatPct(sessionPct)}
            </div>
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))', gap: 8 }}>
          <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
            <div style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>{s.alltime.total_cycles}</div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Total Trades</div>
          </div>
          <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
            <div style={{ fontSize: '1rem', fontWeight: 700, color: winRate >= 50 ? 'var(--green-400)' : winRate > 0 ? 'var(--amber-400)' : 'var(--text-primary)' }}>
              {winRate.toFixed(0)}%
            </div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Win Rate</div>
          </div>
          <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
            <div className="mono pnl-positive" style={{ fontSize: '1rem', fontWeight: 700 }}>
              {wins}
            </div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Wins</div>
          </div>
          <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
            <div className="mono pnl-negative" style={{ fontSize: '1rem', fontWeight: 700 }}>
              {losses}
            </div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Losses</div>
          </div>
          <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
            <div className="mono pnl-positive" style={{ fontSize: '1rem', fontWeight: 700 }}>
              {formatPct(bestTrade)}
            </div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Best</div>
          </div>
          <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
            <div className="mono pnl-negative" style={{ fontSize: '1rem', fontWeight: 700 }}>
              {formatPct(worstTrade)}
            </div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Worst</div>
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginTop: 12, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,0.04)' }}>
          <div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>All-Time P&L</div>
            <div className={`mono ${pnlColorClass(s.alltime.total_net_pnl)}`} style={{ fontSize: '0.9rem', fontWeight: 700 }}>
              {formatPnl(s.alltime.total_net_pnl, 4)} USDT
            </div>
          </div>
          <div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Total Fees</div>
            <div className="mono" style={{ fontSize: '0.9rem', fontWeight: 700, color: 'var(--text-secondary)' }}>
              {s.alltime.total_fees.toFixed(4)} USDT
            </div>
          </div>
          <div>
            <div style={{ fontSize: '0.62rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Avg P&L/Trade</div>
            <div className={`mono ${pnlColorClass(avgPnl)}`} style={{ fontSize: '0.9rem', fontWeight: 700 }}>
              {s.cycles.length > 0 ? `${formatPnl(avgPnl, 4)} USDT` : '--'}
            </div>
          </div>
        </div>

        {s.alltime.first_cycle_ts > 0 && (
          <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 8 }}>
            Trading since {formatDate(s.alltime.first_cycle_ts)}
          </div>
        )}
      </div>

      {/* Trade History */}
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
            Trade History
          </div>
          <span className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>{s.cycles.length} total</span>
        </div>

        {s.cycles.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {[...s.cycles].reverse().slice(0, 20).map((c) => (
              <div
                key={c.number}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '32px 1fr auto auto auto',
                  alignItems: 'center',
                  gap: 8,
                  padding: '6px 8px',
                  borderRadius: 'var(--radius-sm)',
                  background: 'rgba(255,255,255,0.015)',
                  border: '1px solid rgba(255,255,255,0.03)',
                }}
              >
                <span className="mono" style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>#{c.number}</span>
                <span className="mono" style={{ fontSize: '0.72rem', color: 'var(--text-secondary)' }}>
                  ${formatPrice(c.buy_price)} → ${formatPrice(c.sell_price)}
                </span>
                <span className={`mono ${pnlColorClass(c.gross_pct)}`} style={{ fontSize: '0.72rem', fontWeight: 600 }}>
                  {formatPct(c.gross_pct)}
                </span>
                <span className={`mono ${pnlColorClass(c.net_pnl)}`} style={{ fontSize: '0.72rem', fontWeight: 600 }}>
                  {formatPnl(c.net_pnl, 4)}
                </span>
                <span className="mono" style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>
                  {new Date(c.timestamp * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ padding: '20px 0', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
            No completed trades yet
          </div>
        )}
      </div>

      {/* Last action */}
      {s.last_action && (
        <div
          className="card"
          style={{
            fontSize: '0.75rem',
            color: 'var(--text-dim)',
            fontFamily: "'JetBrains Mono', monospace",
            padding: '10px 14px',
          }}
        >
          <span style={{ color: 'var(--text-muted)', marginRight: 6 }}>&gt;</span>
          {s.last_action}
        </div>
      )}

      <ErrorPanel errors={s.errors} />
    </>
  );
}
