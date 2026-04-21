import { createFileRoute } from '@tanstack/react-router';
import { useBotStore, tradingContextToChannel, isV2Channel } from '../hooks/useBotState';
import type { Cycle, V2BotState, V2Trade } from '../lib/types';
import {
  formatPrice,
  formatPct,
  formatPnl,
  pnlColorClass,
  formatTime,
  formatDurationSec,
} from '../lib/formatters';

export const Route = createFileRoute('/history')({
  component: HistoryPage,
});

function parseExitMs(t: V2Trade): number {
  const raw = t.exit_time || '';
  const n = Date.parse(raw);
  return Number.isFinite(n) ? n : 0;
}

function mergedPaperRows(v2: V2BotState): { strategy: string; trade: V2Trade }[] {
  const rows: { strategy: string; trade: V2Trade }[] = [];
  for (const st of v2.strategies) {
    const label = st.short || st.name || st.id;
    for (const tr of st.trade_history) {
      rows.push({ strategy: label, trade: tr });
    }
  }
  rows.sort((a, b) => parseExitMs(b.trade) - parseExitMs(a.trade));
  return rows;
}

function ctxLabel(ctx: { platform: string; accountMode: string }) {
  if (ctx.platform === 'revolut') return 'Revolut Live';
  return ctx.accountMode === 'demo' ? 'Binance Demo' : 'Binance Live';
}

function HistoryPage() {
  const ctx = useBotStore((s) => s.tradingContext);
  const connectionStatus = useBotStore((s) => s.connectionStatus);
  const state = useBotStore((s) => s.state);
  const v2State = useBotStore((s) => s.v2State);

  const channel = tradingContextToChannel(ctx);
  const v2 = isV2Channel(channel);
  const label = ctxLabel(ctx);

  const offline = connectionStatus === 'connecting' || connectionStatus === 'disconnected';

  const liveCycles: Cycle[] = state?.cycles ?? [];
  const liveSorted = [...liveCycles].sort((a, b) => b.timestamp - a.timestamp);

  const paperRows = v2State ? mergedPaperRows(v2State) : [];

  const badgeBg = ctx.platform === 'revolut'
    ? 'rgba(6, 102, 235, 0.2)'
    : ctx.accountMode === 'demo'
      ? 'rgba(245, 158, 11, 0.2)'
      : 'rgba(34, 211, 153, 0.15)';

  const badgeColor = ctx.platform === 'revolut'
    ? '#0666eb'
    : ctx.accountMode === 'demo'
      ? 'var(--amber-400)'
      : 'var(--green-400)';

  return (
    <div style={{ paddingTop: 16, maxWidth: 1100, margin: '0 auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 20 }}>
        <div>
          <h2 style={{ color: 'var(--text-primary)', margin: 0, fontSize: '1.15rem' }}>Trade history</h2>
          <p style={{ color: 'var(--text-dim)', fontSize: '0.8rem', margin: '6px 0 0' }}>
            Showing {label} data — use the platform &amp; account toggle in the header to switch feeds.
          </p>
        </div>
        <span className="env-badge" style={{ background: badgeBg, color: badgeColor }}>
          {label.toUpperCase()}
        </span>
      </div>

      {offline && (
        <div className="card" style={{ marginBottom: 16, padding: 14, color: 'var(--amber-400)', fontSize: '0.85rem' }}>
          Not connected to the backend — history will populate when the WebSocket connects.
        </div>
      )}

      {!v2 && (
        <div className="card">
          <div className="card-header">
            <span className="card-title">Completed cycles</span>
            <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>{liveSorted.length} in session memory</span>
          </div>
          {liveSorted.length === 0 ? (
            <p style={{ padding: '16px 0', color: 'var(--text-dim)', fontSize: '0.85rem' }}>No cycles yet on the live feed.</p>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="history-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Buy -&gt; Sell</th>
                    <th>Gross</th>
                    <th>Net PnL</th>
                    <th>Fee</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {liveSorted.map((c) => (
                    <tr key={`${c.number}-${c.slot_id}`}>
                      <td className="mono">{c.number}</td>
                      <td className="mono">
                        ${formatPrice(c.buy_price)} -&gt; ${formatPrice(c.sell_price)}
                      </td>
                      <td className={`mono ${pnlColorClass(c.gross_pct)}`}>{formatPct(c.gross_pct)}</td>
                      <td className={`mono ${pnlColorClass(c.net_pnl)}`}>{formatPnl(c.net_pnl)} USDT</td>
                      <td className="mono" style={{ color: 'var(--text-dim)' }}>
                        {c.fee.toFixed(4)}
                      </td>
                      <td className="mono" style={{ color: 'var(--text-dim)' }}>
                        {formatTime(c.timestamp)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {v2 && (
        <div className="card">
          <div className="card-header">
            <span className="card-title">{label} fills (all strategies)</span>
            <span style={{ fontSize: '0.7rem', color: 'var(--text-dim)' }}>{paperRows.length} rows</span>
          </div>
          {!v2State ? (
            <p style={{ padding: '16px 0', color: 'var(--text-dim)', fontSize: '0.85rem' }}>
              Waiting for bot state…
            </p>
          ) : paperRows.length === 0 ? (
            <p style={{ padding: '16px 0', color: 'var(--text-dim)', fontSize: '0.85rem' }}>No closed trades yet.</p>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="history-table">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Lot</th>
                    <th>Entry -&gt; Exit</th>
                    <th>Qty</th>
                    <th>Hold</th>
                    <th>Net</th>
                    <th>Fees</th>
                    <th>Exit</th>
                  </tr>
                </thead>
                <tbody>
                  {paperRows.map(({ strategy, trade: t }) => (
                    <tr key={`${strategy}-${t.lot_id ?? ''}-${t.entry_time}-${t.exit_time}`}>
                      <td>{strategy}</td>
                      <td className="mono">{t.lot_id ?? '—'}</td>
                      <td className="mono">
                        ${formatPrice(t.entry_price)} -&gt; ${formatPrice(t.exit_price)}
                      </td>
                      <td className="mono" style={{ color: 'var(--text-dim)' }}>
                        {t.qty != null ? t.qty.toFixed(6) : '—'}
                      </td>
                      <td className="mono" style={{ color: 'var(--text-dim)' }}>
                        {t.hold_seconds != null ? formatDurationSec(t.hold_seconds) : '—'}
                      </td>
                      <td className={`mono ${pnlColorClass(t.net_profit_usdt ?? t.pnl)}`}>
                        {formatPnl(t.net_profit_usdt ?? t.pnl)} USDT
                      </td>
                      <td className="mono" style={{ color: 'var(--text-dim)' }}>
                        {t.total_fees_usdt != null ? t.total_fees_usdt.toFixed(4) : '—'}
                      </td>
                      <td className="mono" style={{ color: 'var(--text-dim)', whiteSpace: 'nowrap' }}>
                        {t.exit_time || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
