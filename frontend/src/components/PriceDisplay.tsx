import { formatPrice, pnlColorClass, formatPct } from '../lib/formatters';

interface Props {
  price: number;
  ma: number | null;
  prices: number[];
}

export function PriceDisplay({ price, ma, prices }: Props) {
  const diffPct = ma ? ((price - ma) / ma) * 100 : 0;

  const min = prices.length >= 2 ? Math.min(...prices) : 0;
  const max = prices.length >= 2 ? Math.max(...prices) : 0;
  const range = max - min || 1;

  const buildPath = () => {
    const h = 60;
    const w = 100;
    const pad = 4;
    const toY = (p: number) => pad + (1 - (p - min) / range) * (h - 2 * pad);
    let pts = '';
    let area = `M 0 ${h}`;
    prices.forEach((p, i) => {
      const x = (i / (prices.length - 1)) * w;
      const y = toY(p);
      pts += `${x.toFixed(2)},${y.toFixed(2)} `;
      area += ` L ${x.toFixed(2)} ${y.toFixed(2)}`;
    });
    area += ` L ${w} ${h} Z`;
    return { pts: pts.trim(), area, w, h };
  };

  return (
    <div className="card price-section">
      <div className="price-row">
        <div>
          <span className="price-label">Price</span>
          <span className="price-value mono">${formatPrice(price)}</span>
        </div>
        <div className="price-meta">
          <span style={{ color: 'var(--text-dim)', marginRight: 4 }}>EMA20</span>
          <span className="mono">{ma ? `$${formatPrice(ma)}` : 'building…'}</span>
        </div>
        {ma && (
          <span className={`mono ${pnlColorClass(diffPct)}`} style={{ fontWeight: 500 }}>
            {formatPct(diffPct)}
          </span>
        )}
      </div>

      {prices.length >= 2 && (() => {
        const { pts, area, w, h } = buildPath();
        return (
          <div className="price-chart-shell" aria-hidden>
            <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="price-chart-svg">
              <defs>
                <linearGradient id="liveLineGrad" x1="0%" y1="0%" x2="100%" y2="0%">
                  <stop offset="0%" stopColor="#34d399" stopOpacity="0.9" />
                  <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.9" />
                </linearGradient>
                <linearGradient id="liveFillGrad" x1="0%" y1="0%" x2="0%" y2="100%">
                  <stop offset="0%" stopColor="#34d399" stopOpacity="0.18" />
                  <stop offset="100%" stopColor="#34d399" stopOpacity="0" />
                </linearGradient>
              </defs>
              <path d={area} fill="url(#liveFillGrad)" />
              <polyline
                fill="none"
                stroke="url(#liveLineGrad)"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
                points={pts}
              />
            </svg>
            <span className="price-chart-range mono">
              range {(max - min).toFixed(2)}
            </span>
          </div>
        );
      })()}
    </div>
  );
}
