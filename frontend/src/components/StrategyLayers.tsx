import type { StrategyLayer } from '../lib/types';

interface Props {
  layers: StrategyLayer[];
}

const statusColors: Record<string, string> = {
  PASS: 'var(--green-400)',
  FAIL: 'var(--red-400)',
  WAITING: 'var(--amber-400)',
  NOT_READY: 'var(--text-muted)',
  NOT_CHECKED: 'var(--text-muted)',
};

const statusBg: Record<string, string> = {
  PASS: 'rgba(52, 211, 153, 0.08)',
  FAIL: 'rgba(248, 113, 113, 0.08)',
  WAITING: 'rgba(251, 191, 36, 0.08)',
  NOT_READY: 'rgba(90, 90, 110, 0.05)',
  NOT_CHECKED: 'rgba(90, 90, 110, 0.05)',
};

export function StrategyLayers({ layers }: Props) {
  if (!layers || layers.length === 0) return null;

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: '0.7rem', fontWeight: 600, color: 'var(--text-dim)', marginBottom: 8 }}>
        Strategy Layers
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {layers.map((layer, i) => (
          <div
            key={i}
            className="strategy-layer"
            style={{
              background: statusBg[layer.status] || statusBg.NOT_READY,
              borderLeft: `3px solid ${statusColors[layer.status] || 'var(--text-muted)'}`,
              borderRadius: 'var(--radius-sm)',
              padding: '8px 12px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: '0.85rem' }}>{layer.icon}</span>
                <span style={{
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  color: statusColors[layer.status] || 'var(--text-dim)',
                }}>
                  {layer.name}
                </span>
              </div>
              <span style={{
                fontSize: '0.65rem',
                fontWeight: 600,
                color: statusColors[layer.status] || 'var(--text-dim)',
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
              }}>
                {layer.status}
              </span>
            </div>
            <p style={{
              margin: '4px 0 0 28px',
              fontSize: '0.72rem',
              color: 'var(--text-secondary)',
              lineHeight: 1.5,
              fontStyle: 'italic',
            }}>
              "{layer.detail}"
            </p>
          </div>
        ))}
      </div>

      {/* Progress bar */}
      <div style={{ marginTop: 8 }}>
        <div className="progress-track" style={{ height: 6 }}>
          <div
            className={`progress-fill ${
              layers.filter(l => l.status === 'PASS').length === layers.length
                ? 'high'
                : layers.filter(l => l.status === 'PASS').length >= layers.length / 2
                  ? 'medium'
                  : 'low'
            }`}
            style={{
              width: `${(layers.filter(l => l.status === 'PASS').length / layers.length) * 100}%`,
            }}
          />
        </div>
        <div style={{ fontSize: '0.65rem', color: 'var(--text-muted)', marginTop: 3, textAlign: 'right' }}>
          {layers.filter(l => l.status === 'PASS').length}/{layers.length} conditions met
        </div>
      </div>
    </div>
  );
}
