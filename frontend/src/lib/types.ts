/* ── Bot state types — mirrors the JSON schema from the Python backend ── */

export interface Order {
  side: string;
  price: number;
  quantity: number;
  age_s: number;
}

export interface Position {
  slot_id: number;
  state: 'WATCHING' | 'BUY_PLACED' | 'HOLDING' | 'SELL_PLACED';
  entry_price: number;
  slot_qty?: number;
  order?: Order;
}

export interface Cycle {
  number: number;
  slot_id: number;
  buy_price: number;
  sell_price: number;
  gross_pct: number;
  net_pnl: number;
  fee: number;
  timestamp: number;
}

export interface StrategyState {
  macro_regime: string;
  daily_bias: string;
  market_mode: string;
  action: string;
  wallet_base_qty: number;
  entry_block: string | null;
  sell_block: string | null;
  reasons: string[];
  cooldown_s: number;
  trend_4h: string;
  trend_4h_strength: number;
  pullback_valid: boolean;
  pullback_pct: number;
  suggested_entry: number | null;
  position_size_mod: number;
  trade_type: string;
  mode_indicators: Record<string, unknown>;
  macro_detail: string;
  daily_detail: string;
}

export interface SessionState {
  starting_balance: number;
  equity_usdt: number;
  fees_paid: number;
}

export interface AllTimeState {
  total_cycles: number;
  total_net_pnl: number;
  total_fees: number;
  first_cycle_ts: number;
}

export interface LogEntry {
  ts: number;
  level: string;
  name: string;
  msg: string;
  /**
   * Runner label that produced this entry (e.g. 'binance-live',
   * 'revolut-live'). `null` means the entry came from non-runner code
   * (api.main, telegram_bot, paper_runner, etc.) — the dashboard treats
   * those as "global" and shows them regardless of selected channel.
   */
  channel?: string | null;
}

export interface BotState {
  timestamp: number;
  uptime_s: number;
  symbol: string;
  mainnet: boolean;
  price: number;
  ma: number | null;
  take_profit_pct: number;
  stop_loss_pct: number;
  trade_size_usdt: number;
  prices: number[];
  positions: Position[];
  cycles: Cycle[];
  strategy: StrategyState;
  session: SessionState;
  alltime: AllTimeState;
  errors: string[];
  last_action: string | null;
  logs?: LogEntry[];
}

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

/* ── V2 Multi-Strategy Types ── */

export interface StrategyLayer {
  name: string;
  status: 'PASS' | 'FAIL' | 'WAITING' | 'NOT_READY' | 'NOT_CHECKED';
  detail: string;
  icon: string;
  indicators?: Record<string, unknown>;
}

export interface StrategyExplanation {
  strategy_summary: string;
  current_state: string;
  layer_summary: string;
  layers: StrategyLayer[];
}

export interface V2Position {
  entry_price: number;
  entry_time: string;
  qty: number;
  usdt: number;
  unrealized_pct: number;
  unrealized_usdt: number;
  hold_minutes: number;
}

export interface V2Wallet {
  starting: number;
  equity: number;
  usdt: number;
  btc: number;
  pnl: number;
  pnl_pct: number;
}

export interface V2Performance {
  total_trades: number;
  win_rate: number;
  total_pnl: number;
  best_trade: number;
  worst_trade: number;
  avg_hold_time_min: number;
}

export interface V2Trade {
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  pnl: number;
  pnl_pct: number;
  exit_reason: string;
  /** Sandbox / extended paper fields */
  lot_id?: number;
  qty?: number;
  net_profit_usdt?: number;
  buy_fee_usdt?: number;
  sell_fee_usdt?: number;
  total_fees_usdt?: number;
  /** Total fees as % of (entry notional + gross exit) */
  fee_pct_of_turnover?: number;
  /** Per-leg maker rate, e.g. 0.1 for 0.1% */
  maker_fee_leg_pct?: number;
  notional_entry_usdt?: number;
  gross_exit_usdt?: number;
  hold_seconds?: number;
}

export interface StrategyInstance {
  id: string;
  name: string;
  short: string;
  pair: string;
  color: string;
  icon: string;
  status: 'WATCHING' | 'HOLDING' | 'TRAILING' | 'EXITING' | 'PAUSED';
  wallet: V2Wallet;
  position: V2Position | null;
  last_signal: {
    action: string;
    reasons: string[];
  };
  indicators: Record<string, unknown>;
  tp_price: number | null;
  sl_price: number | null;
  tp_type: string;
  trade_history: V2Trade[];
  performance: V2Performance;
  explanation: StrategyExplanation;
}

export interface V2GlobalSummary {
  total_strategies: number;
  active_positions: number;
  combined_equity: number;
  combined_pnl: number;
  combined_pnl_pct: number;
  starting_capital: number;
}

export interface OhlcCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  time: number;
  position: 'aboveBar' | 'belowBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle';
  text: string;
  price: number;
  tp_price?: number;
  side: 'buy' | 'sell';
  active: boolean;
}

export interface V2BotState {
  timestamp: number;
  uptime_s: number;
  symbol: string;
  price: number;
  prices: number[];
  trade_markers?: TradeMarker[];
  strategies: StrategyInstance[];
  global_summary: V2GlobalSummary;
  glossary: Record<string, string>;
  strategy_params?: Record<string, Record<string, unknown>>;
  strategy_params_meta?: { updated_at: number };
}
