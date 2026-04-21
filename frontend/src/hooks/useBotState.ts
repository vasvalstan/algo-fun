/* ── Zustand store for bot state ── */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { BotState, V2BotState, ConnectionStatus } from '../lib/types';

export type Platform = 'binance' | 'revolut';
export type AccountMode = 'live' | 'demo';

export interface TradingContext {
  platform: Platform;
  accountMode: AccountMode;
}

export type WsChannel =
  | 'live'
  | 'paper-v2'
  | 'binance-live'
  | 'binance-demo'
  | 'revolut-live';

export function tradingContextToChannel(ctx: TradingContext): WsChannel {
  // Revolut is live-only — paper sandbox was retired (no Revolut testnet,
  // and the in-memory simulator was confusing alongside the real account).
  if (ctx.platform === 'revolut') {
    return 'revolut-live';
  }
  // Binance: "demo" = testnet paper (via LIFO runner), "live" = mainnet.
  return ctx.accountMode === 'demo' ? 'binance-demo' : 'binance-live';
}

/**
 * True when the channel returns V2BotState (the multi-strategy paper-v2
 * sandbox shape).
 *
 * Every LIFO grid runner — binance-live, binance-paper (testnet), and
 * revolut-live — emits the same BotState shape, so they all render via
 * `LiveDashboardContent` (positions, grid, cycles, trade history,
 * exchange account). Only the dedicated `paper_runner_v2` multi-strategy
 * sandbox uses the V2 dashboard.
 *
 * NOTE: When `LIFO_ENABLED=true` (the production default), the legacy
 * `binance_demo_runner` is intentionally NOT spawned — the LIFO
 * binance-paper runner takes over the `binance_demo` channel. Routing
 * `binance-demo` through V2 in that mode crashed the dashboard with
 * "Cannot read properties of undefined (reading 'starting_capital')".
 */
export function isV2Channel(channel: WsChannel): boolean {
  return channel === 'paper-v2';
}

/**
 * Map a frontend WsChannel to the runner label the backend tags log
 * entries with via log_buffer.set_channel(). Used by the Live Log to
 * filter to the currently-viewed channel.
 *
 * The frontend uses 'binance-demo' for legacy reasons; the backend
 * runner is labeled 'binance-paper'. Other channels match 1:1.
 */
export function channelToRunnerLabel(channel: WsChannel): string | null {
  switch (channel) {
    case 'live':
    case 'binance-live':
      return 'binance-live';
    case 'binance-demo':
      return 'binance-paper';
    case 'revolut-live':
      return 'revolut-live';
    case 'paper-v2':
      return null;  // V2 sandbox runs outside the LIFO channel-tagging system
    default:
      return null;
  }
}

/**
 * Map a frontend WsChannel to the venue label used by the
 * /api/exchange/{venue} endpoint (real exchange: balances, open orders).
 *
 * Paper channels (in-memory simulators) intentionally return null —
 * there is no exchange account to query.
 */
export function channelToVenueLabel(channel: WsChannel): string | null {
  switch (channel) {
    case 'live':
    case 'binance-live':
      return 'binance-live';
    case 'binance-demo':
      return 'binance-paper';
    case 'revolut-live':
      return 'revolut-live';
    default:
      return null;
  }
}

interface BotStore {
  state: BotState | null;
  v2State: V2BotState | null;
  connectionStatus: ConnectionStatus;
  lastReceived: number;
  debugLogs: string[];
  tradingContext: TradingContext;

  setState: (state: BotState) => void;
  setV2State: (state: V2BotState) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setLastReceived: (ts: number) => void;
  addDebugLog: (entry: string) => void;
  clearDebugLogs: () => void;
  setTradingContext: (ctx: TradingContext) => void;
  setPlatform: (p: Platform) => void;
  setAccountMode: (m: AccountMode) => void;
}

export const useBotStore = create<BotStore>()(
  persist(
    (set) => ({
      state: null,
      v2State: null,
      connectionStatus: 'connecting',
      lastReceived: 0,
      debugLogs: [],
      tradingContext: { platform: 'binance', accountMode: 'live' },

      setState: (state) => set({ state }),
      setV2State: (v2State) => set({ v2State }),
      setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
      setLastReceived: (lastReceived) => set({ lastReceived }),
      addDebugLog: (entry) =>
        set((store) => ({
          debugLogs: [...store.debugLogs.slice(-7), entry],
        })),
      clearDebugLogs: () => set({ debugLogs: [] }),
      setTradingContext: (tradingContext) =>
        set({ tradingContext: normalizeContext(tradingContext) }),
      setPlatform: (platform) =>
        set((store) => ({
          tradingContext: normalizeContext({ ...store.tradingContext, platform }),
        })),
      setAccountMode: (accountMode) =>
        set((store) => ({
          tradingContext: normalizeContext({ ...store.tradingContext, accountMode }),
        })),
    }),
    {
      name: 'algo-fun-trading',
      partialize: (s) => ({ tradingContext: s.tradingContext }),
      // Cleanup: an older build persisted `{platform: 'revolut', accountMode: 'demo'}`
      // for users who picked Revolut Paper. That mode no longer exists, so
      // snap any rehydrated context back to live whenever Revolut is selected.
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<BotStore>;
        return {
          ...current,
          ...p,
          tradingContext: normalizeContext(p.tradingContext ?? current.tradingContext),
        };
      },
    }
  )
);

/**
 * Enforce the platform/account invariants that the rest of the app relies on:
 * Revolut is live-only (no testnet, no in-memory sim). Anything else passes through.
 */
function normalizeContext(ctx: TradingContext): TradingContext {
  if (ctx.platform === 'revolut' && ctx.accountMode !== 'live') {
    return { ...ctx, accountMode: 'live' };
  }
  return ctx;
}
