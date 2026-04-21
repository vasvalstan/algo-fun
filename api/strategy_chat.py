"""
LLM helper: natural language + optional layer hints → validated param_patch JSON.

Providers (first match wins):
  1. GEMINI_API_KEY or GOOGLE_API_KEY → Google Gemini
  2. OPENAI_API_KEY → OpenAI
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from api.strategy_params import STRATEGY_LAYER_OPTIONS, json_schema_for_llm

log = logging.getLogger(__name__)


def _build_prompts(
    strategy_id: str,
    user_message: str,
    selected_layers: list[str],
    current_params: dict[str, Any],
) -> tuple[str, str]:
    schema = json_schema_for_llm(strategy_id)  # type: ignore[arg-type]
    layer_opts = STRATEGY_LAYER_OPTIONS.get(strategy_id, [])

    system = f"""You are a trading strategy configuration assistant for paper trading.
The user describes adjustments in plain language. You output ONLY valid JSON with this exact shape:
{{"summary": "one short sentence for the trader", "param_patch": {{ ... }}}}

Rules:
- param_patch must be a JSON object whose keys are valid fields for strategy "{strategy_id}" only.
- Use the JSON Schema below as the single source of truth for allowed keys, types, and ranges.
- Only include keys you are changing; omit unchanged fields.
- For layers_enabled, use stable ids: {json.dumps([o["id"] for o in layer_opts])}
  Value true = layer required; false = layer skipped (always passes).
- Never invent new keys. Never output code or explanations outside the JSON object.

JSON Schema (strategy parameters):
{json.dumps(schema, indent=2)}

Current effective parameters (for context):
{json.dumps(current_params, indent=2)}
"""

    layers_note = ""
    if selected_layers:
        layers_note = f"\nThe user selected these layers as focus: {', '.join(selected_layers)}"

    user = f"{user_message.strip()}{layers_note}"
    return system, user


def _parse_llm_json(raw: str) -> tuple[str, dict[str, Any]]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("LLM JSON parse failed: %s", raw[:500])
        raise ValueError(f"Model returned invalid JSON: {exc}") from exc

    summary = str(data.get("summary", "")).strip() or "Updated strategy parameters."
    patch = data.get("param_patch")
    if patch is not None and not isinstance(patch, dict):
        raise ValueError("param_patch must be a JSON object")
    patch = patch or {}
    return summary, patch


def _propose_gemini(
    strategy_id: str,
    user_message: str,
    selected_layers: list[str],
    current_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    import google.generativeai as genai

    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY or GOOGLE_API_KEY is not set")

    model_name = (os.getenv("GEMINI_STRATEGY_MODEL") or "gemini-2.5-flash").strip()
    genai.configure(api_key=key)

    system, user = _build_prompts(strategy_id, user_message, selected_layers, current_params)

    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=system,
        generation_config=genai.types.GenerationConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    response = model.generate_content(user)
    raw = (response.text or "").strip()
    return _parse_llm_json(raw)


def _propose_openai(
    strategy_id: str,
    user_message: str,
    selected_layers: list[str],
    current_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    from openai import OpenAI

    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    system, user = _build_prompts(strategy_id, user_message, selected_layers, current_params)
    model = (os.getenv("OPENAI_STRATEGY_MODEL") or "gpt-4o-mini").strip()
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    return _parse_llm_json(raw)


def propose_param_patch(
    strategy_id: str,
    user_message: str,
    selected_layers: list[str],
    current_params: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Returns (assistant_summary, param_patch dict).

    param_patch is merged with current params server-side; only include fields
    the user wants to change.
    """
    if (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip():
        return _propose_gemini(
            strategy_id, user_message, selected_layers, current_params
        )
    if (os.getenv("OPENAI_API_KEY") or "").strip():
        return _propose_openai(
            strategy_id, user_message, selected_layers, current_params
        )
    raise RuntimeError(
        "No LLM API key set. Add GEMINI_API_KEY or GOOGLE_API_KEY (Gemini), "
        "or OPENAI_API_KEY (OpenAI), on the backend."
    )
