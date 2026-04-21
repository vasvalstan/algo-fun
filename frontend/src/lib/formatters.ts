/* ── Formatting utilities for the dashboard ── */

/** Compact duration from seconds (e.g. hold time). */
export function formatDurationSec(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

export function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`;
}

export function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export function formatDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString();
}

function isFiniteNum(n: unknown): n is number {
  return typeof n === 'number' && Number.isFinite(n);
}

/** Safe for API/WebSocket payloads where numbers may be missing or null. */
export function formatPrice(price: number | null | undefined): string {
  const n = typeof price === 'number' ? price : Number(price);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatPnl(value: number | null | undefined, decimals = 4): string {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(decimals)}`;
}

export function formatPct(value: number | null | undefined): string {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(2)}%`;
}

export function pnlColorClass(value: number | null | undefined): string {
  if (!isFiniteNum(value)) return 'pnl-neutral';
  if (value > 0) return 'pnl-positive';
  if (value < 0) return 'pnl-negative';
  return 'pnl-neutral';
}

export function stateColorClass(state: string): string {
  switch (state) {
    case 'WATCHING': return 'state-watching';
    case 'BUY_PLACED': return 'state-buy';
    case 'HOLDING': return 'state-holding';
    case 'SELL_PLACED': return 'state-sell';
    default: return 'state-watching';
  }
}

export function actionColorClass(action: string): string {
  if (action === 'ENTRY_READY') return 'action-ready';
  if (action === 'WAIT_FOR_DIP') return 'action-wait';
  return 'action-dim';
}

export function regimeColorClass(regime: string): string {
  switch (regime) {
    case 'BULL_RUN': return 'regime-bull';
    case 'HEALTHY_PULLBACK': return 'regime-pullback';
    case 'BEARISH': return 'regime-bear';
    case 'SIDEWAYS': return 'regime-sideways';
    default: return 'regime-unknown';
  }
}

export function modeColorClass(mode: string): string {
  if (mode === 'UP') return 'mode-up';
  if (mode === 'DOWN') return 'mode-down';
  return 'mode-watch';
}
