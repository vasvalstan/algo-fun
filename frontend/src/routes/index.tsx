import { createFileRoute } from '@tanstack/react-router';
import { useBotStore, tradingContextToChannel, isV2Channel, channelToRunnerLabel, channelToVenueLabel } from '../hooks/useBotState';
import { PaperV2DashboardContent } from '../components/PaperV2DashboardContent';
import { LiveDashboardContent } from '../components/LiveDashboardContent';

export const Route = createFileRoute('/')({
  component: TradingHome,
});

function contextLabel(ctx: { platform: string; accountMode: string }) {
  if (ctx.platform === 'revolut') return 'Revolut Live';
  return ctx.accountMode === 'demo' ? 'Binance Paper' : 'Binance Live';
}

function TradingHome() {
  const ctx = useBotStore((s) => s.tradingContext);
  const connectionStatus = useBotStore((s) => s.connectionStatus);
  const state = useBotStore((s) => s.state);
  const v2State = useBotStore((s) => s.v2State);

  const channel = tradingContextToChannel(ctx);
  const v2 = isV2Channel(channel);
  const label = contextLabel(ctx);

  if (connectionStatus === 'connecting' || connectionStatus === 'disconnected') {
    return (
      <div className="center-page" style={{ minHeight: '60vh' }}>
        <div className="offline-icon">{ctx.platform === 'revolut' ? 'R' : ctx.accountMode === 'demo' ? '🧪' : '⏸'}</div>
        <p className="loading-text">
          {connectionStatus === 'connecting' ? `Connecting to ${label}…` : 'Connection lost — reconnecting…'}
        </p>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
          Start the API with: uvicorn api.main:app
        </p>
      </div>
    );
  }

  if (v2) {
    if (!v2State) {
      return (
        <div className="center-page" style={{ minHeight: '60vh' }}>
          <div className="offline-icon" style={{ fontSize: '2rem', opacity: 0.5 }}>⏳</div>
          <p className="loading-text">Warming up {label}…</p>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
            Bootstrapping indicator data
          </p>
        </div>
      );
    }

    return <PaperV2DashboardContent s={v2State} badgeLabel={label} />;
  }

  if (!state) {
    return (
      <div className="center-page" style={{ minHeight: '60vh' }}>
        <p className="loading-text">Waiting for bot data…</p>
      </div>
    );
  }

  return (
    <LiveDashboardContent
      s={state}
      runnerLabel={channelToRunnerLabel(channel)}
      venueLabel={channelToVenueLabel(channel)}
    />
  );
}
