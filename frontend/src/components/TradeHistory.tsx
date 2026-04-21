import type { Cycle } from '../lib/types';
import { formatPrice, formatPct, formatPnl, pnlColorClass, formatTime } from '../lib/formatters';

interface Props {
  cycles: Cycle[];
}

export function TradeHistory({ cycles }: Props) {
  const recent = [...cycles].reverse().slice(0, 15);

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Trade History</span>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>
          {cycles.length} total
        </span>
      </div>

      {recent.length > 0 ? (
        <div>
          {recent.map((c) => (
            <div key={c.number} className="trade-row">
              <span className="trade-num mono">#{c.number}</span>
              <span className="trade-prices mono">
                ${formatPrice(c.buy_price)} → ${formatPrice(c.sell_price)}
              </span>
              <span className={`trade-pct mono ${pnlColorClass(c.gross_pct)}`}>
                {formatPct(c.gross_pct)}
              </span>
              <span className={`trade-pnl mono ${pnlColorClass(c.net_pnl)}`}>
                {formatPnl(c.net_pnl)} USDT
              </span>
              <span className="trade-time mono">{formatTime(c.timestamp)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ padding: '16px 0', color: 'var(--text-dim)', fontSize: '0.85rem' }}>
          No completed cycles yet
        </div>
      )}
    </div>
  );
}
