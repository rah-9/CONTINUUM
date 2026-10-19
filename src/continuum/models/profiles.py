# SPDX-License-Identifier: LicenseRef-Continuum-Proprietary
# Copyright (c) 2026 the Continuum copyright holder. All rights reserved.
"""Capability profiles for model versions, including ones added after release.

Switching a run to a newer model version (``claude-opus-4-8`` -> ``claude-opus-5-5``,
``gpt-5.6`` -> ``gpt-6.1``) must not need code changes, but must never run on guessed
limits. A profile comes from, in order:

1. a verified entry in ``config/models.yaml`` (preferred; pin these for benchmarks);
2. ``profile: auto`` or an ad-hoc ``provider/model-id``: LiteLLM's model registry,
   accepted only when it reports a context window, an output limit and native tool
   calling. Incomplete registry entries are rejected with a clear error.

Prices of 0 from the registry are treated as *unknown* (an unknown model is often
reported with zero cost), so usage is flagged unpriced rather than free.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from continuum.core.errors import UnsupportedCapabilityError
from continuum.models.base import CapabilityProfile, Price

REGISTRY_SOURCE = "litellm_model_registry"


def _per_million(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(str(value)) * Decimal(1_000_000)
    except (ArithmeticError, ValueError):
        return None
    return price if price > 0 else None


def registry_profile(model_id: str, alias: str | None = None,
                     info: dict[str, Any] | None = None) -> CapabilityProfile:
    """Build a profile from LiteLLM's registry; raise if the entry is incomplete."""
    if info is None:
        try:
            import litellm

            info = dict(litellm.get_model_info(model_id))
        except Exception as exc:
            raise UnsupportedCapabilityError(
                f"model {model_id!r} is not in the model registry; add a verified profile "
                "to config/models.yaml") from exc
    window = info.get("max_input_tokens") or info.get("max_tokens")
    output = info.get("max_output_tokens")
    if not window or not output or info.get("supports_function_calling") is not True:
        raise UnsupportedCapabilityError(
            f"registry entry for {model_id!r} is incomplete (context={window}, "
            f"output={output}, tools={info.get('supports_function_calling')}); add a "
            "verified profile to config/models.yaml")
    in_price = _per_million(info.get("input_cost_per_token"))
    out_price = _per_million(info.get("output_cost_per_token"))
    priced = in_price is not None and out_price is not None
    return CapabilityProfile(
        alias=alias or model_id, model_id=model_id, context_window_tokens=int(window),
        max_output_tokens=int(output), supports_native_tools=True,
        supports_json_schema=bool(info.get("supports_response_schema")),
        supports_tool_call_ids=True,
        price=Price(input_per_million_usd=in_price if priced else None,
                    output_per_million_usd=out_price if priced else None,
                    as_of="registry" if priced else None),
        profile_source=REGISTRY_SOURCE)
