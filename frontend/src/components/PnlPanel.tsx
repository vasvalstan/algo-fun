import type { SessionState, AllTimeState } from '../lib/types';
import { formatPnl, formatPct, pnlColorClass, formatDate } from '../lib/formatters';

interface Props {
  session: SessionState;
  alltime: AllTimeState;
}

export function PnlPanel({ session, alltime }: Props) {
  const sessionPnl = session.equity_usdt - session.starting_balance;
  const sessionPct = session.starting_balance ? (sessionPnl / session.starting_balance) * 100 : 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* All-time P&L */}
      <div
        className="card"
        style={{
          borderColor: alltime.total_net_pnl > 0
            ? 'rgba(52, 211, 153, 0.15)'
            : alltime.total_net_pnl < 0
              ? 'rgba(248, 113, 113, 0.15)'
              : undefined,
          boxShadow: alltime.total_net_pnl > 0
            ? 'var(--shadow-glow-green)'
            : alltime.total_net_pnl < 0
              ? 'var(--shadow-glow-red)'
              : undefined,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
          <span className="card-title">All-Time P&L</span>
          <span
            className={`mono ${pnlColorClass(alltime.total_net_pnl)}`}
            style={{ fontSize: '1.3rem', fontWeight: 700 }}
          >
            {formatPnl(alltime.total_net_pnl)} USDT
          </span>
        </div>
        <div style={{ fontSize: '0.7rem', color: 'var(--text-dim)', marginTop: 6 }}>
          {alltime.total_cycles} cycles · fees {alltime.total_fees.toFixed(4)} USDT
          {alltime.first_cycle_ts > 0 && (
            <span> · since {formatDate(alltime.first_cycle_ts)}</span>
          )}
        </div>
      </div>

      {/* Session P&L */}
      <div className="card">
        <div className="card-header" style={{ borderBottom: 'none', paddingBottom: 0, marginBottom: 8 }}>
          <span className="card-title">Session P&L</span>
        </div>
        <div className="row">
          <span className="row-label">Starting</span>
          <span className="row-value mono">{session.starting_balance.toFixed(2)} USDT</span>
        </div>
        <div className="row">
          <span className="row-label">Current</span>
          <span className="row-value mono">
            {session.equity_usdt.toFixed(2)} USDT{' '}
            <span className={pnlColorClass(sessionPnl)}>
              ({formatPct(sessionPct)})
            </span>
          </span>
        </div>
        <div className="row">
          <span className="row-label">Fees paid</span>
          <span className="row-value mono">{session.fees_paid.toFixed(4)} USDT</span>
        </div>
      </div>
    </div>
  );
}
