import { useCallback, useEffect, useState } from 'react';
import { apiUrl } from '../lib/apiBase';
import { formatPrice, formatPnl, formatPct, pnlColorClass, formatDurationSec } from '../lib/formatters';

const SECRET_KEY = 'algo_fun_strategy_api_secret';

interface PendingBuy {
  side: string;
  kind: string;
  price: number;
  distance_usdt?: number;
  distance_pct?: number;
  tranche_usdt?: number;
  status: string;
}

interface ActiveSell {
  side: string;
  kind: string;
  lot_id: number;
  tp_price: number;
  entry_price: number;
  entry_time_iso?: string;
  qty: number;
  cost_usdt?: number;
  buy_fee_usdt?: number;
  unrealized_pct?: number;
  unrealized_usdt?: number;
  distance_to_tp_usdt?: number;
  distance_to_tp_pct?: number;
  hold_seconds?: number;
  status: string;
}

interface ClosedTrade {
  lot_id: number;
  entry_price: number;
  exit_price: number;
  qty: number;
  pnl_usdt: number;
  pnl_pct: number;
  entry_time_iso?: string;
  exit_time_iso?: string;
  exit_time?: string;
  buy_fee_usdt?: number;
  sell_fee_usdt?: number;
  total_fees_usdt?: number;
  fee_pct_of_turnover?: number;
  maker_fee_leg_pct?: number;
  notional_entry_usdt?: number;
  gross_exit_usdt?: number;
  net_profit_usdt?: number;
  hold_seconds?: number;
}

interface OrdersPayload {
  ok: boolean;
  detail?: string;
  sandbox_status?: string;
  mark_price?: number;
  geofence_low?: number;
  geofence_high?: number;
  pending_buys?: PendingBuy[];
  active_sells?: ActiveSell[];
  closed_trades?: ClosedTrade[];
  usdt?: number;
  btc?: number;
  equity_usdt?: number;
  starting_capital?: number;
  reserve_usdt?: number;
  tradable_usdt?: number;
  tranche_usdt?: number;
  num_bullets?: number;
  tp_pct?: number;
  dip_pct?: number;
  open_lots?: number;
  total_closed?: number;
  total_closed_pnl?: number;
  total_closed_fees?: number;
  peak_equity?: number;
  max_drawdown?: number;
}

interface Props {
  readonly hasSandbox: boolean;
}

const cardStyle: React.CSSProperties = {
  padding: '10px 12px',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--border-subtle)',
  background: 'rgba(0,0,0,0.2)',
  fontSize: '0.73rem',
  lineHeight: 1.5,
};

const dimLabel: React.CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: '0.68rem',
};

const sectionTitle: React.CSSProperties = {
  fontWeight: 700,
  fontSize: '0.72rem',
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  color: 'var(--text-dim)',
  marginTop: 14,
  marginBottom: 6,
};

export function PaperSandboxControls({ hasSandbox }: Props) {
  const [secret, setSecret] = useState(() => {
    try { return sessionStorage.getItem(SECRET_KEY) ?? ''; } catch { return ''; }
  });
  const [force, setForce] = useState(false);
  const [orders, setOrders] = useState<OrdersPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [buyMsg, setBuyMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<'active' | 'closed'>('active');

  const fetchOrders = useCallback(async () => {
    if (!hasSandbox) return;
    setErr(null);
    try {
      const q = secret.trim() ? `?secret=${encodeURIComponent(secret.trim())}` : '';
      const res = await fetch(apiUrl(`/api/paper-v2/sandbox/orders${q}`));
      const data = (await res.json()) as OrdersPayload;
      if (!res.ok) {
        const d = typeof data.detail === 'string' ? data.detail : JSON.stringify(data);
        throw new Error(d || res.statusText);
      }
      setOrders(data);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setOrders(null);
    }
  }, [hasSandbox, secret]);

  useEffect(() => {
    if (!hasSandbox) return;
    void fetchOrders();
    const t = setInterval(() => void fetchOrders(), 6000);
    return () => clearInterval(t);
  }, [hasSandbox, fetchOrders]);

  async function manualBuy() {
    setLoading(true);
    setBuyMsg(null);
    setErr(null);
    try {
      const res = await fetch(apiUrl('/api/paper-v2/sandbox/manual-buy'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret: secret.trim() || undefined, force }),
      });
      const data = await res.json();
      if (!res.ok) {
        const d = typeof data.detail === 'string' ? data.detail : JSON.stringify(data);
        throw new Error(d || res.statusText);
      }
      if (secret.trim()) {
        try { sessionStorage.setItem(SECRET_KEY, secret.trim()); } catch { /* */ }
      }
      setBuyMsg(data.detail as string);
      await fetchOrders();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  if (!hasSandbox) return null;

  const o = orders;

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div className="card-header">
        <span className="card-title">Paper sandbox — manual buy & orders</span>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>Simulated only</span>
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'flex-end', marginBottom: 12 }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: '1 1 180px' }}>
          <span style={dimLabel}>API secret</span>
          <input
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            placeholder="TRADE_API_SECRET or STRATEGY_CHAT_SECRET"
            className="mono"
            style={{
              padding: '8px 10px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid rgba(255,255,255,0.12)',
              background: 'rgba(0,0,0,0.25)',
              color: 'var(--text-primary)',
            }}
          />
        </label>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.8rem', cursor: 'pointer' }}>
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
          Force
        </label>
        <button
          type="button"
          style={{
            padding: '10px 16px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-bright)',
            background: 'var(--purple-500)',
            color: '#fff',
            fontWeight: 600,
            cursor: loading ? 'wait' : 'pointer',
            opacity: loading ? 0.7 : 1,
          }}
          disabled={loading}
          onClick={() => void manualBuy()}
        >
          {loading ? 'Placing…' : 'Simulate buy now'}
        </button>
        <button
          type="button"
          onClick={() => void fetchOrders()}
          style={{
            padding: '10px 12px',
            borderRadius: 'var(--radius-sm)',
            border: '1px solid var(--border-subtle)',
            background: 'transparent',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          Refresh
        </button>
      </div>
      {buyMsg && <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: 8 }}>{buyMsg}</div>}
      {err && <div style={{ fontSize: '0.8rem', color: 'var(--red-400)', marginBottom: 8 }}>{err}</div>}

      {o && o.ok === false && (
        <div style={{ color: 'var(--amber-400)', fontSize: '0.78rem', marginBottom: 8 }}>{o.detail ?? 'No snapshot yet.'}</div>
      )}

      {o && o.ok !== false && (
        <>
          {/* Summary strip */}
          <div style={{ ...cardStyle, marginBottom: 10, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8 }}>
            <Stat label="Status" value={o.sandbox_status ?? '—'} />
            <Stat label="Mark price" value={`$${o.mark_price != null ? formatPrice(o.mark_price) : '—'}`} />
            <Stat label="Equity" value={`$${o.equity_usdt?.toFixed(2) ?? '—'}`} />
            <Stat label="Free USDT" value={`$${o.usdt?.toFixed(2) ?? '—'}`} />
            <Stat label="BTC held" value={o.btc?.toFixed(6) ?? '0'} />
            <Stat label="Tradable USDT" value={`$${o.tradable_usdt?.toFixed(2) ?? '—'}`} />
            <Stat label="Tranche size" value={`$${o.tranche_usdt?.toFixed(2) ?? '—'}`} />
            <Stat label="Geofence" value={`$${formatPrice(o.geofence_low ?? 0)}–$${formatPrice(o.geofence_high ?? 0)}`} />
            <Stat label="Open lots" value={`${o.open_lots ?? 0} / ${o.num_bullets ?? '—'}`} />
            <Stat label="Closed trades" value={String(o.total_closed ?? 0)} />
            <Stat label="All-time P&L" value={`${formatPnl(o.total_closed_pnl ?? 0, 4)} USDT`} positive={(o.total_closed_pnl ?? 0) >= 0} />
            <Stat label="All-time fees" value={`${(o.total_closed_fees ?? 0).toFixed(4)} USDT`} />
            <Stat label="Peak equity" value={`$${o.peak_equity?.toFixed(2) ?? '—'}`} />
            <Stat label="Max drawdown" value={`$${o.max_drawdown?.toFixed(2) ?? '—'}`} />
            <Stat label="TP / Dip %" value={`+${o.tp_pct ?? 0.71}% / −${o.dip_pct ?? 0.75}%`} />
          </div>

          {/* Tab switcher */}
          <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
            <TabBtn active={tab === 'active'} onClick={() => setTab('active')}>
              Active orders ({(o.pending_buys?.length ?? 0) + (o.active_sells?.length ?? 0)})
            </TabBtn>
            <TabBtn active={tab === 'closed'} onClick={() => setTab('closed')}>
              Trade history ({o.total_closed ?? 0})
            </TabBtn>
          </div>

          {tab === 'active' && (
            <>
              {/* Pending buys */}
              <div style={sectionTitle}>Pending limit buys</div>
              {(o.pending_buys?.length ?? 0) === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: '0.73rem' }}>No resting buy orders</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {o.pending_buys!.map((p, i) => (
                    <div key={i} style={cardStyle}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 6 }}>
                        <span className="mono" style={{ color: 'var(--green-400)', fontWeight: 600 }}>
                          BUY @ ${formatPrice(p.price)}
                        </span>
                        <span className="mono" style={{ color: 'var(--text-secondary)' }}>
                          {p.kind === 'trailing' ? 'Trailing (flat)' : 'Grid (next dip)'}
                        </span>
                      </div>
                      <div style={{ marginTop: 4, ...dimLabel }}>
                        ${p.distance_usdt?.toFixed(2)} below mark ({p.distance_pct?.toFixed(2)}%)
                        {' · '}
                        Tranche ≈${p.tranche_usdt?.toFixed(2)}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Active sells / open lots */}
              <div style={sectionTitle}>Open lots — resting TP sells</div>
              {(o.active_sells?.length ?? 0) === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: '0.73rem' }}>No open positions</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {o.active_sells!.map((a) => {
                    const ur = a.unrealized_usdt ?? 0;
                    return (
                      <div key={a.lot_id} style={cardStyle}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 6 }}>
                          <span className="mono" style={{ fontWeight: 600 }}>
                            Lot #{a.lot_id}
                          </span>
                          <span className={`mono ${pnlColorClass(ur)}`} style={{ fontWeight: 600 }}>
                            {formatPnl(ur, 4)} USDT ({formatPct(a.unrealized_pct ?? 0)})
                          </span>
                        </div>
                        <div className="mono" style={{ marginTop: 4, color: 'var(--text-secondary)' }}>
                          Entry ${formatPrice(a.entry_price)} → TP ${formatPrice(a.tp_price)}
                          <span style={{ marginLeft: 8, color: 'var(--text-muted)' }}>
                            {a.qty.toFixed(6)} BTC · cost ${a.cost_usdt?.toFixed(2)}
                          </span>
                        </div>
                        <div style={{ marginTop: 4, ...dimLabel }}>
                          TP in ${a.distance_to_tp_usdt?.toFixed(2)} ({a.distance_to_tp_pct?.toFixed(2)}% above mark)
                          {' · '}
                          Buy fee ${a.buy_fee_usdt?.toFixed(4)}
                          {' · '}
                          Hold {a.hold_seconds != null ? formatDurationSec(a.hold_seconds) : '—'}
                        </div>
                        {a.entry_time_iso && (
                          <div style={{ marginTop: 2, ...dimLabel }}>
                            Opened {a.entry_time_iso}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {tab === 'closed' && (
            <>
              <div style={sectionTitle}>Closed round-trips (newest first)</div>
              {(o.closed_trades?.length ?? 0) === 0 ? (
                <div style={{ color: 'var(--text-muted)', fontSize: '0.73rem' }}>No closed trades yet</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 420, overflowY: 'auto' }}>
                  {[...o.closed_trades!].reverse().map((c, i) => {
                    const net = c.net_profit_usdt ?? c.pnl_usdt;
                    return (
                      <div key={i} style={cardStyle}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 6 }}>
                          <span className="mono" style={{ fontWeight: 600 }}>
                            Lot #{c.lot_id}
                          </span>
                          <span className={`mono ${pnlColorClass(net)}`} style={{ fontWeight: 600 }}>
                            Net {formatPnl(net, 4)} USDT ({formatPct(c.pnl_pct)})
                          </span>
                        </div>
                        <div className="mono" style={{ marginTop: 4, color: 'var(--text-secondary)' }}>
                          ${formatPrice(c.entry_price)} → ${formatPrice(c.exit_price)}
                          <span style={{ marginLeft: 8, color: 'var(--text-muted)' }}>
                            {c.qty.toFixed(6)} BTC
                          </span>
                        </div>
                        <div style={{ marginTop: 4, ...dimLabel }}>
                          <span style={{ color: 'var(--text-dim)' }}>Open</span> {c.entry_time_iso ?? '—'}
                          {' · '}
                          <span style={{ color: 'var(--text-dim)' }}>Close</span> {c.exit_time_iso ?? c.exit_time ?? '—'}
                          {c.hold_seconds != null && (
                            <>
                              {' · '}
                              <span style={{ color: 'var(--text-dim)' }}>Hold</span> {formatDurationSec(c.hold_seconds)}
                            </>
                          )}
                        </div>
                        <div style={{ marginTop: 4, ...dimLabel }}>
                          Notional in ${c.notional_entry_usdt?.toFixed(2) ?? '—'}
                          {' · '}
                          Gross out ${c.gross_exit_usdt?.toFixed(2) ?? '—'}
                        </div>
                        <div style={{ marginTop: 2, ...dimLabel }}>
                          Fees: buy ${c.buy_fee_usdt?.toFixed(4) ?? '—'} + sell ${c.sell_fee_usdt?.toFixed(4) ?? '—'} ={' '}
                          <strong>${c.total_fees_usdt?.toFixed(4) ?? '—'}</strong>
                          {c.fee_pct_of_turnover != null && c.maker_fee_leg_pct != null && (
                            <>
                              {' '}
                              ({c.fee_pct_of_turnover.toFixed(3)}% of turnover · {c.maker_fee_leg_pct.toFixed(2)}%/leg)
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, positive }: Readonly<{ label: string; value: string; positive?: boolean }>) {
  return (
    <div>
      <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </div>
      <div
        className="mono"
        style={{
          fontSize: '0.78rem',
          fontWeight: 500,
          color: positive === true ? 'var(--green-400)' : positive === false ? 'var(--red-400)' : 'var(--text-primary)',
        }}
      >
        {value}
      </div>
    </div>
  );
}

function TabBtn({ active, onClick, children }: Readonly<{ active: boolean; onClick: () => void; children: React.ReactNode }>) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: '6px 14px',
        borderRadius: 'var(--radius-sm)',
        border: active ? '1px solid var(--purple-400)' : '1px solid var(--border-subtle)',
        background: active ? 'rgba(167,139,250,0.12)' : 'transparent',
        color: active ? 'var(--purple-400)' : 'var(--text-muted)',
        fontWeight: active ? 600 : 400,
        fontSize: '0.72rem',
        cursor: 'pointer',
      }}
    >
      {children}
    </button>
  );
}
