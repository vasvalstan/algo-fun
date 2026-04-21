import type { StrategyInstance } from '../lib/types';
import { formatPrice, formatPnl, formatPct, pnlColorClass, formatDurationSec } from '../lib/formatters';
import { StrategyLayers } from './StrategyLayers';

interface Props {
  strategy: StrategyInstance;
  price: number;
  expanded: boolean;
  onToggle: () => void;
}

export function StrategyCard({ strategy, price, expanded, onToggle }: Props) {
  const s = strategy;
  const isHolding = s.position !== null;
  const statusLabel =
    s.status === 'PAUSED' ? 'PAUSED' : isHolding ? 'HOLDING' : s.last_signal.action;

  return (
    <div
      id={`strategy-card-${s.id}`}
      className="card strategy-card"
      style={{
        borderColor: `${s.color}22`,
        borderLeftWidth: 3,
        borderLeftColor: s.color,
      }}
    >
      {/* Header */}
      <div
        className="strategy-card-header"
        onClick={onToggle}
        style={{ cursor: 'pointer' }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: '1.2rem' }}>{s.icon}</span>
          <div>
            <span style={{ fontWeight: 600, fontSize: '0.9rem', color: s.color }}>
              {s.short}
            </span>
            <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)', marginLeft: 8 }}>
              {s.pair}
            </span>
          </div>
          <span
            className={`state-badge ${isHolding ? 'holding' : 'watching'}`}
            style={{ fontSize: '0.6rem' }}
          >
            {statusLabel}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <div style={{ textAlign: 'right' }}>
            <span className={`mono ${pnlColorClass(s.wallet.pnl)}`} style={{ fontWeight: 600, fontSize: '0.9rem' }}>
              {formatPnl(s.wallet.pnl, 2)} USDT
            </span>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>
              {formatPct(s.wallet.pnl_pct)}
            </div>
          </div>
          <span style={{ color: 'var(--text-dim)', fontSize: '0.8rem', transition: 'transform 200ms' }}>
            {expanded ? '▲' : '▼'}
          </span>
        </div>
      </div>

      {/* Position bar (always visible when holding) */}
      {isHolding && s.position && (
        <div className="strategy-position-bar">
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem' }}>
            <span className="mono" style={{ color: 'var(--text-dim)' }}>
              Entry ${formatPrice(s.position.entry_price)}
            </span>
            <span className={`mono ${pnlColorClass(s.position.unrealized_pct)}`} style={{ fontWeight: 500 }}>
              {formatPct(s.position.unrealized_pct)} ({formatPnl(s.position.unrealized_usdt, 4)} USDT)
            </span>
            <span className="mono" style={{ color: 'var(--text-muted)', fontSize: '0.7rem' }}>
              {s.position.hold_minutes}m
            </span>
          </div>
          {/* TP/SL visual bar */}
          {s.sl_price && s.tp_price && (
            <PriceBar
              price={price}
              entry={s.position.entry_price}
              tp={s.tp_price}
              sl={s.sl_price}
            />
          )}
        </div>
      )}

      {/* Expanded detail */}
      {expanded && (
        <div className="strategy-detail">
          {/* Explanation */}
          <div className="strategy-explain-box">
            <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 6 }}>
              💡 What's happening
            </div>
            <p style={{ fontSize: '0.8rem', color: 'var(--text-primary)', lineHeight: 1.6, margin: 0 }}>
              {s.explanation.current_state}
            </p>
            <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', marginTop: 6 }}>
              {s.explanation.layer_summary}
            </div>
          </div>

          {/* Layers */}
          <StrategyLayers layers={s.explanation.layers} />

          {/* Signal reasons */}
          {s.last_signal.reasons.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', marginBottom: 4 }}>Signal reasons:</div>
              {s.last_signal.reasons.map((r, i) => (
                <div key={i} style={{ fontSize: '0.72rem', color: 'var(--text-secondary)', fontFamily: "'JetBrains Mono', monospace", padding: '2px 0' }}>
                  · {r}
                </div>
              ))}
            </div>
          )}

          {/* Wallet */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px 16px', marginTop: 12 }}>
            <div className="row" style={{ padding: '3px 0' }}>
              <span className="row-label">Equity</span>
              <span className="row-value mono">${s.wallet.equity.toFixed(2)}</span>
            </div>
            <div className="row" style={{ padding: '3px 0' }}>
              <span className="row-label">Free USDT</span>
              <span className="row-value mono">${s.wallet.usdt.toFixed(2)}</span>
            </div>
            <div className="row" style={{ padding: '3px 0' }}>
              <span className="row-label">Win Rate</span>
              <span className="row-value mono">{s.performance.win_rate.toFixed(0)}%</span>
            </div>
            <div className="row" style={{ padding: '3px 0' }}>
              <span className="row-label">Trades</span>
              <span className="row-value mono">{s.performance.total_trades}</span>
            </div>
          </div>

          {/* TP/SL type */}
          <div style={{ marginTop: 8, fontSize: '0.7rem', color: 'var(--text-dim)' }}>
            Target type: <span style={{ color: s.color }}>{s.tp_type === 'dynamic_atr' ? '📐 ATR Dynamic' : s.tp_type === 'trailing' ? '📈 Trailing Stop' : s.tp_type === 'bollinger_mid' ? '📊 Bollinger Mid' : s.tp_type}</span>
            {s.tp_price && <span> · TP ${formatPrice(s.tp_price)}</span>}
            {s.sl_price && <span> · SL ${formatPrice(s.sl_price)}</span>}
          </div>

          {/* Strategy description */}
          <details style={{ marginTop: 10 }}>
            <summary style={{ fontSize: '0.72rem', color: 'var(--text-dim)', cursor: 'pointer' }}>
              📖 How this strategy works
            </summary>
            <p style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', lineHeight: 1.6, marginTop: 6 }}>
              {s.explanation.strategy_summary}
            </p>
          </details>

          {/* Recent trades */}
          {s.trade_history.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontSize: '0.7rem', fontWeight: 600, color: 'var(--text-dim)', marginBottom: 6 }}>
                Recent trades
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {s.trade_history.slice(-12).reverse().map((t, i) => {
                  const net = t.net_profit_usdt ?? t.pnl;
                  const feePct = t.fee_pct_of_turnover;
                  const legPct = t.maker_fee_leg_pct;
                  const rich = t.total_fees_usdt != null && t.entry_time != null && t.entry_time.length > 8;
                  return (
                    <div
                      key={i}
                      style={{
                        padding: '8px 10px',
                        borderRadius: 'var(--radius-sm)',
                        border: '1px solid var(--border-subtle)',
                        background: 'rgba(0,0,0,0.2)',
                        fontSize: '0.72rem',
                        lineHeight: 1.45,
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
                        <span className="mono" style={{ color: 'var(--text-secondary)' }}>
                          ${formatPrice(t.entry_price)} → ${formatPrice(t.exit_price)}
                          {t.qty != null && (
                            <span style={{ color: 'var(--text-muted)', marginLeft: 6 }}>
                              {t.qty.toFixed(6)} BTC
                            </span>
                          )}
                        </span>
                        <span className={`mono ${pnlColorClass(net)}`} style={{ fontWeight: 600 }}>
                          Net {formatPnl(net, 4)} USDT
                          {t.pnl_pct != null && (
                            <span style={{ fontWeight: 400, marginLeft: 6, color: 'var(--text-dim)' }}>
                              ({formatPct(t.pnl_pct)} vs entry)
                            </span>
                          )}
                        </span>
                      </div>
                      {rich ? (
                        <div style={{ marginTop: 6, color: 'var(--text-muted)', fontSize: '0.68rem' }}>
                          <div>
                            <span style={{ color: 'var(--text-dim)' }}>Open</span> {t.entry_time}{' '}
                            <span style={{ color: 'var(--text-dim)' }}>· Close</span> {t.exit_time}
                            {t.hold_seconds != null && (
                              <>
                                {' '}
                                <span style={{ color: 'var(--text-dim)' }}>· Hold</span> {formatDurationSec(t.hold_seconds)}
                              </>
                            )}
                          </div>
                          <div style={{ marginTop: 4 }}>
                            Fees: buy {t.buy_fee_usdt?.toFixed(4)} + sell {t.sell_fee_usdt?.toFixed(4)} ={' '}
                            <strong>{t.total_fees_usdt?.toFixed(4)} USDT</strong>
                            {feePct != null && legPct != null && (
                              <>
                                {' '}
                                ({feePct.toFixed(3)}% of entry+exit notional · {legPct.toFixed(2)}% maker / leg)
                              </>
                            )}
                          </div>
                          {t.notional_entry_usdt != null && t.gross_exit_usdt != null && (
                            <div style={{ marginTop: 2 }}>
                              Notional in {t.notional_entry_usdt.toFixed(2)} USDT · Gross out {t.gross_exit_usdt.toFixed(2)} USDT
                            </div>
                          )}
                        </div>
                      ) : (
                        <div style={{ marginTop: 4, color: 'var(--text-muted)', fontSize: '0.65rem' }}>
                          {t.exit_time} · {t.exit_reason}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Price bar visualization ── */

function PriceBar({ price, entry, tp, sl }: { price: number; entry: number; tp: number; sl: number }) {
  const lo = Math.min(sl, price) - (tp - sl) * 0.05;
  const hi = Math.max(tp, price) + (tp - sl) * 0.05;
  const range = hi - lo || 1;
  const pct = (v: number) => Math.max(0, Math.min(100, ((v - lo) / range) * 100));

  return (
    <div style={{ position: 'relative', height: 8, marginTop: 6, borderRadius: 4, background: 'rgba(255,255,255,0.03)' }}>
      {/* SL zone */}
      <div style={{
        position: 'absolute', left: `${pct(sl)}%`, top: 0, bottom: 0, width: 2,
        background: 'var(--red-400)', borderRadius: 1,
      }} />
      {/* Entry */}
      <div style={{
        position: 'absolute', left: `${pct(entry)}%`, top: 0, bottom: 0, width: 2,
        background: 'var(--text-dim)', borderRadius: 1,
      }} />
      {/* TP */}
      <div style={{
        position: 'absolute', left: `${pct(tp)}%`, top: 0, bottom: 0, width: 2,
        background: 'var(--green-400)', borderRadius: 1,
      }} />
      {/* Current price */}
      <div style={{
        position: 'absolute', left: `${pct(price)}%`, top: -1, width: 8, height: 10,
        borderRadius: '50%', transform: 'translateX(-4px)',
        background: price >= entry ? 'var(--green-400)' : 'var(--red-400)',
        boxShadow: `0 0 6px ${price >= entry ? 'rgba(52,211,153,0.5)' : 'rgba(248,113,113,0.5)'}`,
      }} />
    </div>
  );
}
