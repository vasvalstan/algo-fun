"""
Pydantic schemas for runtime strategy configuration (V2 paper).

Layer IDs (stable keys for UI / LLM):
  v2_adaptive:  l1_macro, l2_momentum, l3_keltner, l4_cvd
  mean_reversion: l1_regime, l2_bb_extreme, l3_reversal
  breakout: l1_compression, l2_high_break, l3_volume

Disabled layers are treated as PASS (skipped) so the cascade can still fire
if remaining layers pass — adjust UX copy if you prefer fail-closed semantics.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import config
from pydantic import BaseModel, Field, model_validator

StrategyId = Literal["v2_adaptive", "mean_reversion", "breakout", "bitcoin_sandbox"]

# ── Layer option metadata (for frontend + LLM system prompt) ──────────

STRATEGY_LAYER_OPTIONS: dict[str, list[dict[str, str]]] = {
    "v2_adaptive": [
        {"id": "l1_macro", "label": "Macro bias (daily VWAP + EMAs)"},
        {"id": "l2_momentum", "label": "Intraday momentum (1H VWAP + MACD)"},
        {"id": "l3_keltner", "label": "5M Keltner pullback"},
        {"id": "l4_cvd", "label": "Toxic flow gate (CVD)"},
    ],
    "mean_reversion": [
        {"id": "l1_regime", "label": "Market regime filter"},
        {"id": "l2_bb_extreme", "label": "Bollinger extreme"},
        {"id": "l3_reversal", "label": "Reversal confirmation"},
    ],
    "breakout": [
        {"id": "l1_compression", "label": "Volatility compression (BB width)"},
        {"id": "l2_high_break", "label": "1H high breakout"},
        {"id": "l3_volume", "label": "Volume surge vs SMA"},
    ],
    "bitcoin_sandbox": [],
}


class V2AdaptiveParams(BaseModel):
    """Tunable fields for get_v2_full_analysis."""

    model_config = {"extra": "forbid"}

    daily_vwap_lookback: int = Field(default=20, ge=5, le=200)
    daily_ema_fast: int = Field(default=20, ge=2, le=200)
    daily_ema_slow: int = Field(default=50, ge=2, le=300)
    vwap_1h_lookback: int = Field(default=24, ge=4, le=168)
    keltner_ema_period: int = Field(default=20, ge=5, le=100)
    keltner_atr_mult: float = Field(default=1.5, ge=0.5, le=5.0)
    keltner_near_lower_pct: float = Field(
        default=0.1, ge=0.0, le=5.0, description="Max %% above lower band to count as 'near'"
    )
    entry_price_multiplier: float = Field(default=0.9998, ge=0.99, le=1.0)
    cvd_window_seconds: int = Field(default=60, ge=10, le=600)
    atr_period_5m: int = Field(default=14, ge=5, le=50)
    atr_sl_mult: float = Field(default_factory=lambda: config.V2_ATR_SL_MULT, ge=0.5, le=10.0)
    atr_tp_mult: float = Field(default_factory=lambda: config.V2_ATR_TP_MULT, ge=0.5, le=20.0)
    layers_enabled: dict[str, bool] = Field(
        default_factory=lambda: {
            "l1_macro": True,
            "l2_momentum": True,
            "l3_keltner": True,
            "l4_cvd": True,
        }
    )

    @model_validator(mode="after")
    def _layer_keys(self) -> V2AdaptiveParams:
        for k in ("l1_macro", "l2_momentum", "l3_keltner", "l4_cvd"):
            self.layers_enabled.setdefault(k, True)
        return self


class MeanReversionParams(BaseModel):
    model_config = {"extra": "forbid"}

    bb_period: int = Field(default=20, ge=5, le=100)
    bb_std_dev: float = Field(default=2.5, ge=0.5, le=4.0)
    rsi_oversold: float = Field(default=25.0, ge=1.0, le=50.0)
    atr_period_15m: int = Field(default=14, ge=5, le=50)
    atr_sl_mult: float = Field(default=1.0, ge=0.1, le=5.0)
    swing_low_lookback: int = Field(default=3, ge=1, le=20)
    regime_allow: list[str] = Field(
        default_factory=lambda: ["SIDEWAYS", "UNKNOWN", "HEALTHY_PULLBACK"]
    )
    layers_enabled: dict[str, bool] = Field(
        default_factory=lambda: {
            "l1_regime": True,
            "l2_bb_extreme": True,
            "l3_reversal": True,
        }
    )

    @model_validator(mode="after")
    def _layers(self) -> MeanReversionParams:
        for k in ("l1_regime", "l2_bb_extreme", "l3_reversal"):
            self.layers_enabled.setdefault(k, True)
        return self


class BreakoutParams(BaseModel):
    model_config = {"extra": "forbid"}

    bandwidth_interval: str = Field(default="1h")
    bandwidth_bb_period: int = Field(default=20, ge=5, le=100)
    bandwidth_bb_std: float = Field(default=2.0, ge=0.5, le=4.0)
    volume_interval: str = Field(default="5m")
    volume_sma_period: int = Field(default=20, ge=5, le=100)
    volume_surge_ratio: float = Field(default=3.0, ge=1.0, le=10.0)
    high_lookback_bars: int = Field(default=24, ge=2, le=168)
    layers_enabled: dict[str, bool] = Field(
        default_factory=lambda: {
            "l1_compression": True,
            "l2_high_break": True,
            "l3_volume": True,
        }
    )

    @model_validator(mode="after")
    def _layers(self) -> BreakoutParams:
        for k in ("l1_compression", "l2_high_break", "l3_volume"):
            self.layers_enabled.setdefault(k, True)
        return self


class BitcoinSandboxParamsModel(BaseModel):
    """$60k–$75k grid sandbox (paper)."""

    model_config = {"extra": "forbid"}

    geofence_low: float = Field(default=60_000.0, ge=1.0)
    geofence_high: float = Field(default=85_000.0, ge=1.0)
    reserve_usdt: float = Field(default=1_000.0, ge=0.0)
    num_bullets: int = Field(default=26, ge=1, le=100)
    tp_pct: float = Field(default=0.71, ge=0.01, le=10.0, description="Take-profit %% above entry")
    dip_pct: float = Field(default=0.75, ge=0.01, le=10.0, description="Buy dip %% below anchor")


class StrategyConfigEnvelope(BaseModel):
    """Per-strategy effective config + metadata for API/WS."""

    model_config = {"extra": "forbid"}

    strategy_id: StrategyId
    updated_at: float = 0.0
    params: dict[str, Any] = Field(default_factory=dict)


class RuntimeStrategyConfigRoot(BaseModel):
    """Full snapshot of all strategy parameter blobs."""

    model_config = {"extra": "forbid"}

    version: int = 1
    updated_at: float = Field(default_factory=time.time)
    strategies: dict[str, dict[str, Any]] = Field(default_factory=dict)


def default_v2_adaptive() -> V2AdaptiveParams:
    return V2AdaptiveParams()


def default_mean_reversion() -> MeanReversionParams:
    return MeanReversionParams()


def default_breakout() -> BreakoutParams:
    return BreakoutParams()


def default_bitcoin_sandbox() -> BitcoinSandboxParamsModel:
    return BitcoinSandboxParamsModel()


def parse_strategy_params(strategy_id: str, data: dict[str, Any]) -> BaseModel:
    if strategy_id == "v2_adaptive":
        return V2AdaptiveParams.model_validate(data)
    if strategy_id == "mean_reversion":
        return MeanReversionParams.model_validate(data)
    if strategy_id == "breakout":
        return BreakoutParams.model_validate(data)
    if strategy_id == "bitcoin_sandbox":
        return BitcoinSandboxParamsModel.model_validate(data)
    raise ValueError(f"Unknown strategy_id: {strategy_id}")


def merge_strategy_params(strategy_id: str, base_dict: dict[str, Any], patch: dict[str, Any]) -> BaseModel:
    merged = {**base_dict, **patch}
    return parse_strategy_params(strategy_id, merged)


def params_model_to_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump()


def json_schema_for_llm(strategy_id: StrategyId) -> dict[str, Any]:
    """Subset schema the LLM is allowed to output as `param_patch`."""
    if strategy_id == "v2_adaptive":
        return V2AdaptiveParams.model_json_schema()
    if strategy_id == "mean_reversion":
        return MeanReversionParams.model_json_schema()
    if strategy_id == "breakout":
        return BreakoutParams.model_json_schema()
    if strategy_id == "bitcoin_sandbox":
        return BitcoinSandboxParamsModel.model_json_schema()
    raise ValueError(strategy_id)
