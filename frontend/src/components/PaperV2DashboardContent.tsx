import { useState } from 'react';
import type { V2BotState } from '../lib/types';
import { formatUptime, formatPrice } from '../lib/formatters';
import { StrategyCard } from './StrategyCard';
import { EducationalPanel } from './EducationalPanel';
import { PerformancePanel } from './PerformancePanel';
import { StrategyConfigChat } from './StrategyConfigChat';
import { PaperSandboxControls } from './PaperSandboxControls';
import { TradingChart } from './TradingChart';

interface Props {
  readonly s: V2BotState;
  readonly badgeLabel?: string;
}

export function PaperV2DashboardContent({ s, badgeLabel }: Props) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Defensive: if a non-V2 snapshot ever leaks onto a V2 channel (e.g. a
  // LIFO runner publishing on `binance_demo` while the user has the V2
  // dashboard open), every `s.global_summary.*` access used to throw and
  // crash the whole route. Fall back to a zeroed summary so the UI shows
  // a "warming up" state instead of an error overlay.
  const summary = s.global_summary ?? {
    total_strategies: 0,
    active_positions: 0,
    combined_equity: 0,
    combined_pnl: 0,
    combined_pnl_pct: 0,
    starting_capital: 0,
  };
  const strategies = s.strategies ?? [];
  const prices = s.prices ?? [];
  const trade_markers = s.trade_markers ?? [];

  return (
    <>
      <div className="paper-v2-market-strip">
        <div className="paper-v2-strip-left">
          <span className="paper-v2-symbol">{s.symbol}</span>
          <span className="env-badge paper-v2-badge-paper">{badgeLabel || 'V2 PAPER'}</span>
          <span className="paper-v2-badge-capital">
            ${summary.starting_capital.toFixed(0)} · {summary.total_strategies} strateg
            {summary.total_strategies === 1 ? 'y' : 'ies'}
          </span>
        </div>
        <span className="paper-v2-uptime">uptime {formatUptime(s.uptime_s)}</span>
      </div>

      <div className="card paper-v2-price-card">
        <div className="paper-v2-price-top">
          <div>
            <span className="paper-v2-price-kicker">Spot</span>
            <div className="paper-v2-price-main mono">${formatPrice(s.price)}</div>
          </div>
          {prices.length >= 2 && (
            <div className="paper-v2-price-stats">
              <div className="paper-v2-stat">
                <span className="paper-v2-stat-label">Session high</span>
                <span className="paper-v2-stat-value mono">${formatPrice(Math.max(...prices))}</span>
              </div>
              <div className="paper-v2-stat">
                <span className="paper-v2-stat-label">Session low</span>
                <span className="paper-v2-stat-value mono">${formatPrice(Math.min(...prices))}</span>
              </div>
              <div className="paper-v2-stat">
                <span className="paper-v2-stat-label">Range</span>
                <span className="paper-v2-stat-value mono">
                  {(Math.max(...prices) - Math.min(...prices)).toFixed(2)}
                </span>
              </div>
            </div>
          )}
        </div>
        <TradingChart markers={trade_markers} height={380} />
      </div>

      <PerformancePanel summary={summary} />

      <PaperSandboxControls hasSandbox={strategies.some((x) => x.id === 'bitcoin_sandbox')} />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div
          style={{
            fontSize: '0.75rem',
            fontWeight: 600,
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
            color: 'var(--text-dim)',
          }}
        >
          Strategies ({strategies.length})
        </div>
        {strategies.map((strat) => (
          <StrategyCard
            key={strat.id}
            strategy={strat}
            price={s.price}
            expanded={expandedId === strat.id}
            onToggle={() => setExpandedId(expandedId === strat.id ? null : strat.id)}
          />
        ))}
      </div>

      <EducationalPanel strategies={strategies} glossary={s.glossary || {}} />

      <StrategyConfigChat v2={s} />
    </>
  );
}
