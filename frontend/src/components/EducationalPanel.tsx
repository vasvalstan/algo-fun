import type { StrategyInstance } from '../lib/types';
import { useState } from 'react';

interface Props {
  strategies: StrategyInstance[];
  glossary: Record<string, string>;
}

export function EducationalPanel({ strategies, glossary }: Props) {
  const [showGlossary, setShowGlossary] = useState(false);

  // Build a summary of what ALL strategies are doing
  const holding = strategies.filter(s => s.position !== null);
  const watching = strategies.filter(s => s.position === null);
  const entryReady = strategies.filter(s => s.last_signal.action === 'ENTRY_READY' && !s.position);

  return (
    <div className="card" id="educational-panel" style={{ background: 'linear-gradient(135deg, rgba(22, 22, 40, 0.9) 0%, var(--bg-card) 100%)' }}>
      <div className="card-header" style={{ borderBottom: '1px solid rgba(167, 139, 250, 0.1)' }}>
        <span className="card-title" style={{ color: 'var(--purple-400)' }}>
          📚 What's Happening Right Now
        </span>
      </div>

      <div style={{ fontSize: '0.85rem', lineHeight: 1.7, color: 'var(--text-primary)' }}>
        {/* Overall summary */}
        {holding.length === 0 && entryReady.length === 0 && (
          <p style={{ margin: '0 0 8px 0' }}>
            <strong>All {strategies.length} strategies are scanning</strong> the market for opportunities.
            None have found optimal entry conditions yet — this is normal. The bot checks every few seconds.
          </p>
        )}

        {holding.length > 0 && (
          <p style={{ margin: '0 0 8px 0' }}>
            <strong>{holding.length} strategy{holding.length > 1 ? ' strategies are' : ' is'} in a live trade:</strong>{' '}
            {holding.map(s => (
              <span key={s.id} style={{ color: s.color, fontWeight: 600 }}>
                {s.icon} {s.short}{' '}
              </span>
            ))}
            — watching price action for TP, SL, or time-based exits.
          </p>
        )}

        {entryReady.length > 0 && (
          <p style={{ margin: '0 0 8px 0', color: 'var(--green-400)' }}>
            🟢 <strong>{entryReady.length} strategy{entryReady.length > 1 ? ' strategies found' : ' found'} entry signals!</strong>{' '}
            {entryReady.map(s => s.short).join(', ')}
          </p>
        )}

        {watching.length > 0 && holding.length > 0 && (
          <p style={{ margin: '0 0 8px 0', color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
            {watching.length} other strateg{watching.length > 1 ? 'ies' : 'y'} scanning:
            {watching.map(s => ` ${s.icon} ${s.short}`).join(',')}
          </p>
        )}

        {/* Quick explanation of each strategy's current state */}
        <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {strategies.map(s => (
            <div key={s.id} style={{
              padding: '8px 12px',
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(255,255,255,0.02)',
              borderLeft: `2px solid ${s.color}`,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                <span>{s.icon}</span>
                <span style={{ fontSize: '0.75rem', fontWeight: 600, color: s.color }}>{s.short}</span>
                <span style={{
                  fontSize: '0.6rem',
                  padding: '1px 6px',
                  borderRadius: 100,
                  background: s.position ? 'var(--cyan-glow)' : 'rgba(90,90,110,0.2)',
                  color: s.position ? 'var(--cyan-400)' : 'var(--text-dim)',
                  fontWeight: 600,
                }}>
                  {s.position ? 'IN TRADE' : s.last_signal.action}
                </span>
              </div>
              <p style={{ fontSize: '0.75rem', color: 'var(--text-secondary)', margin: 0, lineHeight: 1.5 }}>
                {s.explanation.current_state}
              </p>
            </div>
          ))}
        </div>
      </div>

      {/* Glossary toggle */}
      <div style={{ marginTop: 16, borderTop: '1px solid var(--border-subtle)', paddingTop: 12 }}>
        <button
          onClick={() => setShowGlossary(!showGlossary)}
          style={{
            background: 'none',
            border: '1px solid var(--border-medium)',
            borderRadius: 'var(--radius-sm)',
            color: 'var(--text-dim)',
            fontSize: '0.72rem',
            padding: '4px 12px',
            cursor: 'pointer',
            transition: 'all 200ms',
          }}
        >
          {showGlossary ? '📕 Hide' : '📖 Show'} Trading Terms Glossary
        </button>

        {showGlossary && (
          <div style={{ marginTop: 10, display: 'grid', gridTemplateColumns: '1fr', gap: 6 }}>
            {Object.entries(glossary).map(([term, definition]) => (
              <div key={term} style={{ fontSize: '0.72rem', padding: '4px 0', borderBottom: '1px solid var(--border-subtle)' }}>
                <strong style={{ color: 'var(--purple-400)' }}>{term}:</strong>{' '}
                <span style={{ color: 'var(--text-secondary)' }}>{definition}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
