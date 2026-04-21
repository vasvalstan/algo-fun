"""
Async-safe runtime strategy parameters for V2 paper (merged with defaults).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel

from api.strategy_params import (
    BitcoinSandboxParamsModel,
    BreakoutParams,
    MeanReversionParams,
    V2AdaptiveParams,
    default_bitcoin_sandbox,
    default_breakout,
    default_mean_reversion,
    default_v2_adaptive,
    params_model_to_dict,
    parse_strategy_params,
)

_lock = asyncio.Lock()
# Full validated param dict per strategy_id.
_overrides: dict[str, dict[str, Any]] = {}


def _defaults_dict(strategy_id: str) -> dict[str, Any]:
    if strategy_id == "v2_adaptive":
        return params_model_to_dict(default_v2_adaptive())
    if strategy_id == "mean_reversion":
        return params_model_to_dict(default_mean_reversion())
    if strategy_id == "breakout":
        return params_model_to_dict(default_breakout())
    if strategy_id == "bitcoin_sandbox":
        return params_model_to_dict(default_bitcoin_sandbox())
    raise ValueError(f"Unknown strategy_id: {strategy_id}")


async def get_effective_params(
    strategy_id: str,
) -> V2AdaptiveParams | MeanReversionParams | BreakoutParams | BitcoinSandboxParamsModel:
    async with _lock:
        stored = _overrides.get(strategy_id)
    if not stored:
        return parse_strategy_params(strategy_id, _defaults_dict(strategy_id))
    return parse_strategy_params(strategy_id, stored)


async def get_effective_params_dict(strategy_id: str) -> dict[str, Any]:
    m = await get_effective_params(strategy_id)
    return params_model_to_dict(m)


async def get_all_effective_params() -> dict[str, Any]:
    async with _lock:
        out: dict[str, Any] = {}
        for sid in ("v2_adaptive", "mean_reversion", "breakout", "bitcoin_sandbox"):
            stored = _overrides.get(sid)
            out[sid] = stored if stored else _defaults_dict(sid)
        ts = time.time()
    out["_meta"] = {"updated_at": ts}
    return out


def _deep_merge_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = {**base, **patch}
    if "layers_enabled" in patch and isinstance(patch["layers_enabled"], dict):
        le_base = base.get("layers_enabled") or {}
        merged["layers_enabled"] = {**le_base, **patch["layers_enabled"]}
    return merged


async def merge_and_validate(strategy_id: str, patch: dict[str, Any]) -> BaseModel:
    async with _lock:
        base = _defaults_dict(strategy_id)
        cur = _overrides.get(strategy_id)
        cur_full = {**base, **(cur or {})}
        if cur and isinstance(cur.get("layers_enabled"), dict):
            cur_full["layers_enabled"] = {
                **base.get("layers_enabled", {}),
                **cur["layers_enabled"],
            }
        merged = _deep_merge_patch(cur_full, patch)
        model = parse_strategy_params(strategy_id, merged)
        _overrides[strategy_id] = params_model_to_dict(model)
        return model


async def reset_strategy_params(strategy_id: str) -> None:
    async with _lock:
        _overrides.pop(strategy_id, None)
