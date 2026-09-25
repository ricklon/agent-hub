"""Per-token prices from OpenRouter's catalogue, for metering calls that report no cost.

Speech models are only listed with ``output_modalities=all``; the default
catalogue call leaves them out. Prices are USD per token, as OpenRouter
publishes them, and are cached for an hour.
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

_TAG = "openrouter.prices"
_TTL_S = 3600.0

_cache: tuple[float, dict[str, tuple[float, float]]] | None = None
_lock = asyncio.Lock()


async def token_prices(
    model: str,
    base_url: str = "https://openrouter.ai/api/v1",
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[float, float] | None:
    """(input, output) USD per token for ``model``, or None if it is not listed.

    Args:
        model: OpenRouter model id.
        base_url: API base; the catalogue is ``{base_url}/models``.
        transport: HTTP transport override, for tests.
    """
    global _cache
    async with _lock:
        now = asyncio.get_running_loop().time()
        if _cache is None or now - _cache[0] > _TTL_S:
            prices = await _fetch(base_url, transport)
            if prices:
                _cache = (now, prices)
            elif _cache is None:
                return None
    return _cache[1].get(model) if _cache else None


async def _fetch(
    base_url: str, transport: httpx.AsyncBaseTransport | None
) -> dict[str, tuple[float, float]]:
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            resp = await client.get(
                base_url.rstrip("/") + "/models", params={"output_modalities": "all"}
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:
        logger.bind(tag=_TAG).warning(f"OpenRouter price list unavailable: {exc}")
        return {}
    prices: dict[str, tuple[float, float]] = {}
    for model in data:
        pricing = model.get("pricing") or {}
        try:
            prices[str(model["id"])] = (
                float(pricing.get("prompt") or 0),
                float(pricing.get("completion") or 0),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return prices


def reset_cache() -> None:
    """Forget cached prices (tests)."""
    global _cache
    _cache = None
