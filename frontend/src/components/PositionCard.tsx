import type { Position } from '../lib/types';
import { formatPrice } from '../lib/formatters';

interface Props {
  positions: Position[];
  takeProfitPct: number;
  stopLossPct: number;
}

function badgeClass(state: string): string {
  switch (state) {
    case 'WATCHING': return 'state-badge watching';
    case 'BUY_PLACED': return 'state-badge buy-placed';
    case 'HOLDING': return 'state-badge holding';
    case 'SELL_PLACED': return 'state-badge sell-placed';
    default: return 'state-badge watching';
  }
}

export function PositionCard({ positions, takeProfitPct, stopLossPct }: Props) {
  const watchCount = positions.filter(p => p.state === 'WATCHING').length;
  const buyCount = positions.filter(p => p.state === 'BUY_PLACED').length;
  const holdCount = positions.filter(p => p.state === 'HOLDING').length;
  const sellCount = positions.filter(p => p.state === 'SELL_PLACED').length;

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Positions</span>
        <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>
          watch {watchCount} · buy {buyCount} · hold {holdCount} · sell {sellCount}
        </span>
      </div>

      <div>
        {positions.map((p) => (
          <div
            key={p.slot_id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              padding: '8px 0',
              borderBottom: '1px solid var(--border-subtle)',
            }}
          >
            <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem', width: 24 }}>
              #{p.slot_id}
            </span>

            <span className={badgeClass(p.state)}>{p.state.replace('_', ' ')}</span>

            <div style={{ flex: 1, fontSize: '0.8rem' }}>
              {p.order ? (
                <span className="mono" style={{ color: 'var(--text-secondary)' }}>
                  {p.order.side} {p.order.quantity.toFixed(6)} @ ${formatPrice(p.order.price)}
                  {p.entry_price > 0 && (
                    <span style={{ color: 'var(--text-dim)', marginLeft: 8 }}>
                      TP ${formatPrice(p.entry_price * (1 + takeProfitPct / 100))}
                      {' · '}
                      SL ${formatPrice(p.entry_price * (1 - stopLossPct / 100))}
                    </span>
                  )}
                  <span style={{ color: 'var(--text-muted)', marginLeft: 8 }}>
                    ({p.order.age_s}s)
                  </span>
                </span>
              ) : p.state === 'WATCHING' ? (
                <span style={{ color: 'var(--text-muted)' }}>—</span>
              ) : (
                <span style={{ color: 'var(--text-muted)' }}>pending…</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
