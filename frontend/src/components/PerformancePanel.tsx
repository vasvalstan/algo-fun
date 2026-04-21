import type { V2GlobalSummary } from '../lib/types';
import { formatPnl, formatPct, pnlColorClass } from '../lib/formatters';

interface Props {
  summary: V2GlobalSummary;
}

export function PerformancePanel({ summary }: Props) {
  const pnlPct = summary.starting_capital > 0
    ? (summary.combined_pnl / summary.starting_capital) * 100
    : 0;

  return (
    <div
      className="card"
      id="v2-performance"
      style={{
        borderColor: summary.combined_pnl > 0
          ? 'rgba(52, 211, 153, 0.15)'
          : summary.combined_pnl < 0
            ? 'rgba(248, 113, 113, 0.15)'
            : undefined,
        boxShadow: summary.combined_pnl > 0
          ? 'var(--shadow-glow-green)'
          : summary.combined_pnl < 0
            ? 'var(--shadow-glow-red)'
            : undefined,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', textTransform: 'uppercase', letterSpacing: '0.08em', fontWeight: 600 }}>
            Combined Portfolio
          </div>
          <div className={`mono ${pnlColorClass(summary.combined_pnl)}`} style={{ fontSize: '1.6rem', fontWeight: 700, lineHeight: 1.2 }}>
            ${summary.combined_equity.toFixed(2)}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <span
            className={`mono ${pnlColorClass(summary.combined_pnl)}`}
            style={{ fontSize: '1.1rem', fontWeight: 700 }}
          >
            {formatPnl(summary.combined_pnl, 2)} USDT
          </span>
          <div className={`mono ${pnlColorClass(summary.combined_pnl)}`} style={{ fontSize: '0.8rem' }}>
            {formatPct(pnlPct)}
          </div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12 }}>
        <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
          <div style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
            {summary.total_strategies}
          </div>
          <div style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Strategies</div>
        </div>
        <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
          <div style={{ fontSize: '1rem', fontWeight: 700, color: summary.active_positions > 0 ? 'var(--cyan-400)' : 'var(--text-primary)' }}>
            {summary.active_positions}
          </div>
          <div style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Active Trades</div>
        </div>
        <div style={{ textAlign: 'center', padding: '8px 0', background: 'rgba(255,255,255,0.02)', borderRadius: 'var(--radius-sm)' }}>
          <div className="mono" style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--text-primary)' }}>
            ${summary.starting_capital.toFixed(0)}
          </div>
          <div style={{ fontSize: '0.65rem', color: 'var(--text-dim)', textTransform: 'uppercase' }}>Starting</div>
        </div>
      </div>
    </div>
  );
}
