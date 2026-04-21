"""
Live terminal dashboard.

Renders a fixed-layout screen using ANSI escape codes so the display
refreshes in-place rather than scrolling.  No external dependency —
works in macOS Terminal, iTerm, VS Code terminal.
"""

import sys
import time
from typing import Optional

from strategy import TrendAwareMakerStrategy, MeanReversionStrategy, State, Position
import config

# ANSI helpers
CLEAR = "\033[H\033[J"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _color_pnl(value: float) -> str:
    if value > 0:
        return f"{GREEN}{value:+.2f}{RESET}"
    elif value < 0:
        return f"{RED}{value:+.2f}{RESET}"
    return f"{value:+.2f}"


def _color_pct(value: float) -> str:
    if value > 0:
        return f"{GREEN}{value:+.2f}%{RESET}"
    elif value < 0:
        return f"{RED}{value:+.2f}%{RESET}"
    return f"{value:+.2f}%"


def _state_label(state: State) -> str:
    colors = {
        State.WATCHING: DIM,
        State.BUY_PLACED: YELLOW,
        State.HOLDING: CYAN,
        State.SELL_PLACED: GREEN,
    }
    c = colors.get(state, "")
    return f"{c}{state.name}{RESET}"


def _format_uptime(start: float) -> str:
    elapsed = int(time.time() - start)
    h, remainder = divmod(elapsed, 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"


def _format_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _mode_color(mode: str) -> str:
    colors = {"UP": GREEN, "DOWN": RED, "WATCH": YELLOW}
    c = colors.get(mode, DIM)
    return f"{c}{mode}{RESET}"


def _regime_color(regime: str) -> str:
    colors = {"BULL_RUN": GREEN, "HEALTHY_PULLBACK": CYAN, "BEARISH": RED, "SIDEWAYS": YELLOW}
    c = colors.get(regime, DIM)
    return f"{c}{regime}{RESET}"


def _slot_row(p: Position) -> str:
    """One line per slot for the positions table."""
    sid = f"#{p.slot_id}"
    st = _state_label(p.state)
    if p.state == State.WATCHING:
        return f"{sid:<4} {st:<24} {DIM}—{RESET}"
    if p.open_order is not None:
        o = p.open_order
        age = int(time.time() - o.placed_at)
        extra = ""
        if p.entry_price > 0:
            tgt = p.entry_price * (1 + config.TAKE_PROFIT_PCT / 100)
            extra = f"  entry {p.entry_price:,.0f}→tgt {tgt:,.0f}"
        return (
            f"{sid:<4} {st:<24} {o.side} {o.quantity} @ {o.price:,.2f}{extra}  ({age}s)"
        )
    if p.state == State.HOLDING and p.entry_price > 0:
        tgt = p.entry_price * (1 + config.TAKE_PROFIT_PCT / 100)
        return (
            f"{sid:<4} {st:<24} {DIM}placing sell{RESET}  entry {p.entry_price:,.0f}→tgt {tgt:,.0f}"
        )
    return f"{sid:<4} {st:<24} {DIM}(pending){RESET}"


def render(
    strat: MeanReversionStrategy,
    start_time: float,
    last_action: Optional[str] = None,
) -> None:
    """Clear the screen and draw the full dashboard."""
    w = 62
    sep = "─" * w

    lines = []

    # ── Header ───────────────────────────────────────────────────────
    env = "TESTNET" if not config.USE_MAINNET else f"{RED}MAINNET{RESET}"
    uptime = _format_uptime(start_time)
    lines.append(
        f" {BOLD}ALGO-FUN{RESET}  {config.SYMBOL}  {env}"
        f"{'':>{w - 30 - len(config.SYMBOL)}}uptime {uptime}"
    )
    lines.append(sep)

    # ── Price row ────────────────────────────────────────────────────
    price_str = f"{strat.current_price:,.2f}"
    if strat.ma is not None:
        ema_str = f"{strat.ma:,.2f}"
        diff_pct = (strat.current_price - strat.ma) / strat.ma * 100
        diff_str = _color_pct(diff_pct)
    else:
        ema_str = "building…"
        diff_str = "--"

    lines.append(
        f" PRICE  {BOLD}{price_str:>12}{RESET}"
        f"    EMA20(1H)  {ema_str:>12}"
        f"    diff {diff_str}"
    )

    # ── State row ────────────────────────────────────────────────────
    cycles_done = len(strat.cycles)
    free_slots = sum(1 for p in strat.positions if p.state == State.WATCHING)
    total_slots = len(strat.positions)
    lines.append(
        f" STATE  {_state_label(strat.state):<22}"
        f"  cycles  {cycles_done:<4}"
        f"  slots {free_slots}/{total_slots} free"
    )

    # ── All slots (parallel positions) ─────────────────────────────
    buy_n = sum(1 for p in strat.positions if p.state == State.BUY_PLACED)
    sell_n = sum(1 for p in strat.positions if p.state == State.SELL_PLACED)
    hold_n = sum(1 for p in strat.positions if p.state == State.HOLDING)
    lines.append(
        f" {BOLD}POSITIONS{RESET}  watch {free_slots}  buy {buy_n}  hold {hold_n}  sell {sell_n}"
    )
    for p in strat.positions:
        row = _slot_row(p)
        lines.append(f"  {row}")
    lines.append("")

    # ── Strategy analysis ────────────────────────────────────────────
    has_analysis = hasattr(strat, "macro_regime")
    if has_analysis:
        action_color = GREEN if strat.action == "ENTRY_READY" else YELLOW if strat.action == "WAIT_FOR_DIP" else DIM
        lines.append(
            f" {BOLD}STRATEGY{RESET}"
            f"    macro {_regime_color(strat.macro_regime)}"
            f"    daily {CYAN}{strat.daily_bias}{RESET}"
        )
        lines.append(
            f"           mode  {_mode_color(strat.market_mode)}"
            f"       action {action_color}{strat.action}{RESET}"
            f"    TP {config.TAKE_PROFIT_PCT}% / SL {config.STOP_LOSS_PCT}%"
        )
        a = strat.last_analysis or {}
        mode_ind = a.get("market_mode", {}).get("indicators", {})
        ema50 = mode_ind.get("ema_50")
        ema200 = mode_ind.get("ema_200")
        rsi = mode_ind.get("rsi_14")
        atr = mode_ind.get("atr_14")
        ind_parts = []
        if ema50 is not None:
            ind_parts.append(f"EMA50 {ema50:,.0f}")
        if ema200 is not None:
            ind_parts.append(f"EMA200 {ema200:,.0f}")
        if rsi is not None:
            rsi_c = GREEN if rsi > 50 else RED if rsi < 40 else YELLOW
            ind_parts.append(f"RSI {rsi_c}{rsi:.1f}{RESET}")
        if atr is not None:
            ind_parts.append(f"ATR {atr:.1f}")
        if ind_parts:
            lines.append(f"           {DIM}1H:{RESET} {'  '.join(ind_parts)}")
        wb = getattr(strat, "wallet_base_qty", 0.0) or 0.0
        if wb > 0:
            wb_usdt = wb * strat.current_price if strat.current_price else 0
            lines.append(
                f"           {DIM}wallet BTC{RESET}  {wb:.8f}  (~{wb_usdt:,.0f} USDT)  "
                f"{DIM}not auto-managed{RESET}"
            )
        blk = getattr(strat, "last_entry_block_reason", None)
        if blk:
            lines.append(f"           {YELLOW}{blk}{RESET}")
        sblk = getattr(strat, "last_sell_block_reason", None)
        if sblk:
            lines.append(f"           {RED}{sblk}{RESET}")
        lines.append("")

    # ── Price history sparkline ────────────────────────────────────────
    if len(strat.prices) >= 2:
        p_list = list(strat.prices)
        p_min, p_max = min(p_list), max(p_list)
        p_range = p_max - p_min if p_max > p_min else 1
        sparks = "▁▂▃▄▅▆▇█"
        spark_line = ""
        for p in p_list:
            idx = min(len(sparks) - 1, int((p - p_min) / p_range * (len(sparks) - 1)))
            spark_line += sparks[idx]
        spread_pct = p_range / p_min * 100
        lines.append(
            f" {DIM}history{RESET}  {spark_line}"
            f"  {DIM}range{RESET} {p_range:.2f} ({spread_pct:.4f}%)"
        )
        lines.append("")

    # ── Open orders ──────────────────────────────────────────────────
    active_orders = [
        p for p in strat.positions if p.open_order is not None
    ]
    lines.append(
        f" {BOLD}OPEN ORDERS{RESET}"
        f"  ({len(active_orders)}/{total_slots} slots)"
    )
    if active_orders:
        for p in active_orders:
            o = p.open_order
            age = int(time.time() - o.placed_at)
            entry_info = ""
            if p.entry_price > 0:
                gain = (strat.current_price - p.entry_price) / p.entry_price * 100
                entry_info = f"  entry {p.entry_price:,.2f} {_color_pct(gain)}"
            lines.append(
                f"  #{p.slot_id}  {o.side}  {o.quantity}  @ {o.price:>10,.2f}"
                f"{entry_info}  ({age}s)"
            )
    else:
        lines.append(f"  {DIM}(none){RESET}")
    lines.append("")

    # ── ALL-TIME P&L (persistent) ────────────────────────────────────
    lt = strat.ledger
    at_pnl = lt.total_net_pnl
    at_color = GREEN if at_pnl > 0 else RED if at_pnl < 0 else ""
    at_sign = "+" if at_pnl >= 0 else ""
    lines.append(sep)
    lines.append(
        f" {BOLD}ALL-TIME P&L{RESET}   "
        f"{at_color}{BOLD}{at_sign}{at_pnl:.4f} USDT{RESET}   "
        f"{DIM}({lt.total_cycles} cycles  fees {lt.total_fees:.4f}){RESET}"
    )
    if lt.first_cycle_ts > 0:
        running_since = time.strftime("%Y-%m-%d %H:%M", time.localtime(lt.first_cycle_ts))
        lines.append(f" {DIM}since {running_since}{RESET}")
    lines.append(sep)
    lines.append("")

    # ── Session P&L (live USDT + base×price from Binance each tick) ──
    lines.append(f" {BOLD}SESSION P&L{RESET}")
    lines.append(f"  starting  {strat.starting_balance:>10.2f} USDT")
    current_eq = strat.session_equity_usdt
    pnl = current_eq - strat.starting_balance
    pnl_pct = (pnl / strat.starting_balance * 100) if strat.starting_balance else 0
    lines.append(
        f"  current   {current_eq:>10.2f} USDT"
        f"   ({_color_pct(pnl_pct)})"
    )
    lines.append(f"  fees paid {strat.total_fees:>10.4f} USDT")
    lines.append("")

    # ── Trade log (last 10) ──────────────────────────────────────────
    lines.append(f" {BOLD}LAST TRADES{RESET}")
    if strat.cycles:
        for cycle in reversed(strat.cycles[-10:]):
            lines.append(
                f"  s{cycle.slot_id} #{cycle.number:<3}"
                f" BUY {cycle.buy_price:>10,.2f}"
                f" → SELL {cycle.sell_price:>10,.2f}"
                f"  {_color_pct(cycle.gross_pct)}"
                f"  {_color_pnl(cycle.net_pnl)} USDT"
                f"  {_format_time(cycle.timestamp)}"
            )
    else:
        lines.append(f"  {DIM}(no completed cycles yet){RESET}")
    lines.append("")

    # ── Last action ──────────────────────────────────────────────────
    if last_action:
        lines.append(f" {DIM}> {last_action}{RESET}")
    lines.append(sep)
    lines.append(f" {DIM}Ctrl+C to stop{RESET}")

    # ── Errors (show last 3) ─────────────────────────────────────────
    if strat.errors:
        lines.append("")
        lines.append(f" {RED}ERRORS (last 3):{RESET}")
        for err in strat.errors[-3:]:
            lines.append(f"  {RED}{err}{RESET}")

    sys.stdout.write(CLEAR + "\n".join(lines) + "\n")
    sys.stdout.flush()
