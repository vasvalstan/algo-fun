import type { StrategyState } from '../lib/types';
import { regimeColorClass, modeColorClass, actionColorClass } from '../lib/formatters';

interface Props {
  strategy: StrategyState;
  takeProfitPct: number;
  stopLossPct: number;
}

export function StrategyPanel({ strategy, takeProfitPct, stopLossPct }: Props) {
  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Strategy Analysis</span>
        <span
          className={actionColorClass(strategy.action)}
          style={{ fontSize: '0.75rem', fontWeight: 600, letterSpacing: '0.05em' }}
        >
          {strategy.action}
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 24px' }}>
        <div className="row" style={{ padding: '4px 0' }}>
          <span className="row-label">Macro</span>
          <span className={`row-value ${regimeColorClass(strategy.macro_regime)}`} style={{ fontWeight: 600 }}>
            {strategy.macro_regime}
          </span>
        </div>
        <div className="row" style={{ padding: '4px 0' }}>
          <span className="row-label">Daily</span>
          <span className="row-value" style={{ color: 'var(--cyan-400)', fontWeight: 500 }}>
            {strategy.daily_bias}
          </span>
        </div>
        <div className="row" style={{ padding: '4px 0' }}>
          <span className="row-label">Mode</span>
          <span className={`row-value ${modeColorClass(strategy.market_mode)}`} style={{ fontWeight: 600 }}>
            {strategy.market_mode}
          </span>
        </div>
        <div className="row" style={{ padding: '4px 0' }}>
          <span className="row-label">TP / SL</span>
          <span className="row-value mono" style={{ color: 'var(--text-secondary)' }}>
            {takeProfitPct}% / {stopLossPct}%
          </span>
        </div>
      </div>

      {strategy.wallet_base_qty > 0 && (
        <div
          style={{
            marginTop: 12,
            padding: '8px 12px',
            background: 'rgba(255, 255, 255, 0.03)',
            borderRadius: 'var(--radius-sm)',
            fontSize: '0.75rem',
            color: 'var(--text-dim)',
          }}
        >
          Wallet BTC: {strategy.wallet_base_qty.toFixed(8)} (not auto-managed)
        </div>
      )}

      {strategy.entry_block && (
        <div
          style={{
            marginTop: 8,
            fontSize: '0.75rem',
            color: 'var(--amber-400)',
            fontFamily: "'JetBrains Mono', monospace",
          }}
        >
          ⚠ {strategy.entry_block}
        </div>
      )}

      {strategy.sell_block && (
        <div
          style={{
            marginTop: 4,
            fontSize: '0.75rem',
            color: 'var(--red-400)',
            fontFamily: "'JetBrains Mono', monospace",
          }}
        >
          ⚠ {strategy.sell_block}
        </div>
      )}
    </div>
  );
}
