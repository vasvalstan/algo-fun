"""
Trade manager — pending trade queue with approval/rejection and timeout expiry.

All live trades flow through here when TRADE_APPROVAL_REQUIRED is enabled.
External callers (Hermes MCP, Telegram bot, REST API) create pending trades,
and they only execute on Binance after explicit approval.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Coroutine, Dict, List, Optional

import config
from api import audit

log = logging.getLogger(__name__)


class TradeStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class PendingTrade:
    trade_id: str
    strategy: str
    pair: str
    side: str  # BUY or SELL
    quantity: float
    price_at_request: float
    size_usdt: float
    status: TradeStatus = TradeStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    resolved_at: Optional[float] = None
    execution_result: Optional[dict] = None
    reject_reason: Optional[str] = None
    telegram_message_id: Optional[int] = None
    source: str = "manual"  # manual | bot_signal | hermes

    def __post_init__(self):
        if self.expires_at == 0.0:
            timeout = float(getattr(config, "TRADE_APPROVAL_TIMEOUT", 300))
            self.expires_at = self.created_at + timeout

    @property
    def is_expired(self) -> bool:
        return self.status == TradeStatus.PENDING and time.time() > self.expires_at

    @property
    def age_s(self) -> int:
        return int(time.time() - self.created_at)

    @property
    def ttl_s(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "strategy": self.strategy,
            "pair": self.pair,
            "side": self.side,
            "quantity": self.quantity,
            "price_at_request": self.price_at_request,
            "size_usdt": self.size_usdt,
            "status": self.status.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resolved_at": self.resolved_at,
            "age_s": self.age_s,
            "ttl_s": self.ttl_s,
            "source": self.source,
            "reject_reason": self.reject_reason,
        }


# Callback signature: async fn(trade: PendingTrade) -> None
ApprovalCallback = Callable[[PendingTrade], Coroutine]


class TradeManager:
    """In-memory pending-trade queue with approval workflow."""

    def __init__(self) -> None:
        self._trades: Dict[str, PendingTrade] = {}
        self._history: List[PendingTrade] = []
        self._on_approval: Optional[ApprovalCallback] = None
        self._on_rejection: Optional[ApprovalCallback] = None
        self._on_created: Optional[ApprovalCallback] = None
        self._expiry_task: Optional[asyncio.Task] = None
        self._last_request_per_strategy: Dict[str, float] = {}
        self._strategy_cooldown = 30.0  # seconds between requests per strategy
        self.auto_approve: bool = True

        self.max_pending = int(getattr(config, "MAX_PENDING_TRADES", 5))
        self.max_trade_usdt = float(getattr(config, "MAX_TRADE_SIZE_USDT", 500))
        self.max_daily_loss_usdt = float(getattr(config, "MAX_DAILY_LOSS_USDT", 200))

    def set_callbacks(
        self,
        on_created: Optional[ApprovalCallback] = None,
        on_approval: Optional[ApprovalCallback] = None,
        on_rejection: Optional[ApprovalCallback] = None,
    ) -> None:
        if on_created:
            self._on_created = on_created
        if on_approval:
            self._on_approval = on_approval
        if on_rejection:
            self._on_rejection = on_rejection

    def start_expiry_loop(self) -> None:
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._expire_loop())

    def stop(self) -> None:
        if self._expiry_task and not self._expiry_task.done():
            self._expiry_task.cancel()

    async def _expire_loop(self) -> None:
        try:
            while True:
                self._expire_stale()
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    def _expire_stale(self) -> None:
        now = time.time()
        expired = [
            t for t in self._trades.values()
            if t.status == TradeStatus.PENDING and now > t.expires_at
        ]
        for trade in expired:
            trade.status = TradeStatus.EXPIRED
            trade.resolved_at = now
            self._archive(trade)
            audit.trade_expired(trade.trade_id)
            log.info("Trade %s expired (strategy=%s side=%s)", trade.trade_id[:8], trade.strategy, trade.side)

    def _archive(self, trade: PendingTrade) -> None:
        self._trades.pop(trade.trade_id, None)
        self._history.append(trade)
        if len(self._history) > 200:
            self._history = self._history[-200:]

    async def create_trade(
        self,
        strategy: str,
        pair: str,
        side: str,
        quantity: float,
        price: float,
        size_usdt: float = 0.0,
        source: str = "manual",
    ) -> PendingTrade:
        pending_count = sum(1 for t in self._trades.values() if t.status == TradeStatus.PENDING)
        if pending_count >= self.max_pending:
            raise ValueError(f"Too many pending trades ({pending_count}/{self.max_pending})")

        if size_usdt > self.max_trade_usdt:
            raise ValueError(f"Trade size {size_usdt:.0f} USDT exceeds limit {self.max_trade_usdt:.0f}")

        last_req = self._last_request_per_strategy.get(strategy, 0)
        if time.time() - last_req < self._strategy_cooldown:
            remaining = int(self._strategy_cooldown - (time.time() - last_req))
            raise ValueError(f"Rate limit: wait {remaining}s before next {strategy} trade request")

        trade = PendingTrade(
            trade_id=uuid.uuid4().hex[:12],
            strategy=strategy,
            pair=pair,
            side=side.upper(),
            quantity=quantity,
            price_at_request=price,
            size_usdt=size_usdt or (quantity * price),
            source=source,
        )
        self._trades[trade.trade_id] = trade
        self._last_request_per_strategy[strategy] = time.time()
        audit.trade_requested(trade.trade_id, strategy, pair, side, quantity, price, size_usdt or (quantity * price), source)
        log.info(
            "Trade created: %s %s %.6f %s @ %.2f (%s)",
            trade.trade_id[:8], side, quantity, pair, price, strategy,
        )

        if self._on_created:
            try:
                await self._on_created(trade)
            except Exception as exc:
                log.warning("on_created callback failed: %s", exc)

        if self.auto_approve and trade.status == TradeStatus.PENDING:
            log.info("Auto-approving trade %s (%s %s)", trade.trade_id[:8], trade.side, trade.pair)
            try:
                trade = await self.approve(trade.trade_id)
            except Exception as exc:
                log.warning("Auto-approve failed: %s", exc)

        return trade

    async def approve(self, trade_id: str) -> PendingTrade:
        trade = self._trades.get(trade_id)
        if not trade:
            found = next((t for t in self._history if t.trade_id == trade_id), None)
            if found:
                raise ValueError(f"Trade {trade_id[:8]} already resolved ({found.status.value})")
            raise ValueError(f"Trade {trade_id[:8]} not found")

        if trade.status != TradeStatus.PENDING:
            raise ValueError(f"Trade {trade_id[:8]} is {trade.status.value}, cannot approve")

        if trade.is_expired:
            trade.status = TradeStatus.EXPIRED
            trade.resolved_at = time.time()
            self._archive(trade)
            raise ValueError(f"Trade {trade_id[:8]} has expired")

        trade.status = TradeStatus.APPROVED
        trade.resolved_at = time.time()

        try:
            result = self._execute_on_exchange(trade)
            trade.execution_result = result
            trade.status = TradeStatus.EXECUTED
            audit.trade_approved(trade.trade_id, str(result.get("orderId", "?")))
            log.info("Trade %s executed on Binance: %s", trade.trade_id[:8], result.get("orderId", "?"))
        except Exception as exc:
            trade.status = TradeStatus.FAILED
            trade.execution_result = {"error": str(exc)}
            audit.trade_failed(trade.trade_id, str(exc))
            log.error("Trade %s execution failed: %s", trade.trade_id[:8], exc)

        self._archive(trade)

        if self._on_approval:
            try:
                await self._on_approval(trade)
            except Exception as exc:
                log.warning("on_approval callback failed: %s", exc)

        return trade

    async def reject(self, trade_id: str, reason: str = "") -> PendingTrade:
        trade = self._trades.get(trade_id)
        if not trade:
            raise ValueError(f"Trade {trade_id[:8]} not found")

        if trade.status != TradeStatus.PENDING:
            raise ValueError(f"Trade {trade_id[:8]} is {trade.status.value}, cannot reject")

        trade.status = TradeStatus.REJECTED
        trade.resolved_at = time.time()
        trade.reject_reason = reason or "User rejected"
        self._archive(trade)
        audit.trade_rejected(trade.trade_id, trade.reject_reason)
        log.info("Trade %s rejected: %s", trade.trade_id[:8], trade.reject_reason)

        if self._on_rejection:
            try:
                await self._on_rejection(trade)
            except Exception as exc:
                log.warning("on_rejection callback failed: %s", exc)

        return trade

    def _execute_on_exchange(self, trade: PendingTrade) -> dict:
        import trading as exchange

        price_str = f"{trade.price_at_request:.2f}"
        qty_str = f"{trade.quantity:.6f}"

        try:
            return exchange.place_maker_order(
                side=trade.side,
                quantity=qty_str,
                price=price_str,
                symbol=trade.pair,
            )
        except Exception as exc:
            if "would immediately" in str(exc).lower() or "-2010" in str(exc):
                return exchange.place_limit_order(
                    side=trade.side,
                    quantity=qty_str,
                    price=price_str,
                    symbol=trade.pair,
                )
            raise

    def get_pending(self) -> List[PendingTrade]:
        self._expire_stale()
        return [t for t in self._trades.values() if t.status == TradeStatus.PENDING]

    def get_trade(self, trade_id: str) -> Optional[PendingTrade]:
        return self._trades.get(trade_id) or next(
            (t for t in self._history if t.trade_id == trade_id), None
        )

    def get_history(self, limit: int = 50) -> List[PendingTrade]:
        return self._history[-limit:]


# Singleton
trade_manager = TradeManager()
