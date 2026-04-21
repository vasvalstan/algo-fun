import { Outlet, createRootRoute, Link } from '@tanstack/react-router';
import { ConnectionStatus } from '../components/ConnectionStatus';
import { DebugPanel } from '../components/DebugPanel';

import {
  useBotStore,
  tradingContextToChannel,
  type Platform,
  type AccountMode,
} from '../hooks/useBotState';
import { useWebSocket } from '../hooks/useWebSocket';

export const Route = createRootRoute({
  component: RootLayout,
});

const pillBase = {
  border: 'none',
  borderRadius: 8,
  padding: '6px 14px',
  fontSize: '0.75rem',
  fontWeight: 600,
  cursor: 'pointer',
  transition: 'background 0.15s, color 0.15s',
} as const;

function pill(active: boolean, accent = 'rgba(167, 139, 250, 0.25)') {
  return {
    ...pillBase,
    background: active ? accent : 'transparent',
    color: active ? 'var(--text-primary)' : 'var(--text-dim)',
  } as const;
}

const groupStyle = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 2,
  padding: 3,
  margin: 0,
  borderRadius: 10,
  background: 'var(--bg-secondary)',
  border: '1px solid var(--border-subtle)',
} as const;

function PlatformAccountControls() {
  const ctx = useBotStore((s) => s.tradingContext);
  const setPlatform = useBotStore((s) => s.setPlatform);
  const setAccountMode = useBotStore((s) => s.setAccountMode);

  const setP = (p: Platform) => () => setPlatform(p);
  const setA = (m: AccountMode) => () => setAccountMode(m);

  return (
    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <fieldset aria-label="Platform" style={groupStyle}>
        <button type="button" style={pill(ctx.platform === 'binance', 'rgba(245, 158, 11, 0.25)')} onClick={setP('binance')}>
          Binance
        </button>
        <button type="button" style={pill(ctx.platform === 'revolut', 'rgba(6, 102, 235, 0.25)')} onClick={setP('revolut')}>
          Revolut
        </button>
      </fieldset>

      <fieldset aria-label="Account mode" style={groupStyle}>
        <button
          type="button"
          style={pill(ctx.accountMode === 'live', 'rgba(52, 211, 153, 0.25)')}
          onClick={setA('live')}
        >
          Live
        </button>
        {/*
          Paper mode is only meaningful for Binance (real testnet exchange
          we can hit with the same LIFO engine). Revolut has no testnet
          and we don't run an in-memory simulator anymore — hide the pill
          entirely on Revolut so the toggle doesn't suggest a non-option.
        */}
        {ctx.platform === 'binance' && (
          <button
            type="button"
            style={pill(ctx.accountMode === 'demo', 'rgba(245, 158, 11, 0.25)')}
            onClick={setA('demo')}
            title="Binance paper runs the same LIFO engine on the Binance testnet"
          >
            Paper
          </button>
        )}
      </fieldset>
    </div>
  );
}

function RootLayout() {
  const ctx = useBotStore((s) => s.tradingContext);
  const channel = tradingContextToChannel(ctx);
  useWebSocket(channel);

  const connectionStatus = useBotStore((s) => s.connectionStatus);
  const lastReceived = useBotStore((s) => s.lastReceived);
  const debugLogs = useBotStore((s) => s.debugLogs);

  return (
    <div className="dashboard">
      <header className="header">
        <div className="header-left">
          <span className="logo">ALGO-FUN</span>
          <nav style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap' }}>
            <Link to="/" className="nav-link" activeProps={{ style: { opacity: 1 } }}>
              Trading
            </Link>
            <Link to="/history" className="nav-link" activeProps={{ style: { opacity: 1 } }}>
              History
            </Link>
            <Link to="/chat" className="nav-link" activeProps={{ style: { opacity: 1 } }}>
              Chat
            </Link>
            <PlatformAccountControls />
          </nav>
        </div>
        <div className="header-right">
          <ConnectionStatus status={connectionStatus} lastReceived={lastReceived} />
          <DebugPanel logs={debugLogs} />
        </div>
      </header>

      <Outlet />
    </div>
  );
}
