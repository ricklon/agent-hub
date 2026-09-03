"""LLM spend metering: metrics, a warning threshold, and a hard limit.

The hub can be reachable from the internet with a working API key, so an
unbounded bill is a real failure mode. Every LLM call is metered here and
checked against two independent caps — a daily one and a lifetime one.

Wiring: `configure()` at startup, then the LLM provider calls `guard()`
before each request and `record()` after. Both are no-ops until configured,
so tests and library use do not need a database.

This is a local backstop, not a substitute for a spend cap at the provider.
It can only count what it sees: a crash between the API call and `record()`
loses that call, and estimated costs are only as good as the configured
price table.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from agent_hub.registry.store import RegistryStore

_TAG = "spend"

# Cost per 1M tokens, used only when the provider does not report a cost.
_PRICE_SCALE = 1_000_000


class SpendLimitExceeded(Exception):
    """Raised instead of issuing an LLM call once a configured cap is hit."""

    def __init__(self, window: str, spent_usd: float, limit_usd: float) -> None:
        self.window = window
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(f"{window} LLM spend limit reached: ${spent_usd:.4f} of ${limit_usd:.2f}")


@dataclass
class ModelPrice:
    """Fallback per-1M-token prices for one model, in USD."""

    input_usd: float = 0.0
    output_usd: float = 0.0


@dataclass
class SpendConfig:
    """Limits and fallback pricing, from the `llm.spend` config section."""

    # 0 disables that cap. Both are checked independently.
    daily_limit_usd: float = 0.0
    total_limit_usd: float = 0.0
    # Fraction of a limit at which to start warning (0.8 = warn from 80%).
    warn_at: float = 0.8
    prices: dict[str, ModelPrice] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> SpendConfig:
        """Build from the raw config dict's `llm.spend` section."""
        raw = (config.get("llm") or {}).get("spend") or {}
        prices: dict[str, ModelPrice] = {}
        for model, entry in (raw.get("pricing") or {}).items():
            if isinstance(entry, dict):
                prices[str(model)] = ModelPrice(
                    input_usd=float(entry.get("input", 0.0)),
                    output_usd=float(entry.get("output", 0.0)),
                )
        return cls(
            daily_limit_usd=float(raw.get("daily_limit_usd", 0.0)),
            total_limit_usd=float(raw.get("total_limit_usd", 0.0)),
            warn_at=float(raw.get("warn_at", 0.8)),
            prices=prices,
        )

    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Cost from the local price table, or 0.0 when the model is unpriced."""
        price = self.prices.get(model)
        if price is None:
            return 0.0
        return (
            prompt_tokens * price.input_usd + completion_tokens * price.output_usd
        ) / _PRICE_SCALE


def day_start() -> datetime:
    """Midnight UTC today — the boundary the daily window resets on."""
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


class SpendTracker:
    """Meters LLM spend against a daily and a lifetime cap."""

    def __init__(self, store: RegistryStore, config: SpendConfig) -> None:
        self._store = store
        self._config = config
        # Warn once per window, so a hot loop near the threshold does not
        # flood the log with the same line. The daily entry clears when the
        # UTC day rolls over.
        self._warned: set[str] = set()
        self._warned_day = day_start()

    async def totals(self) -> dict[str, Any]:
        """Current spend, limits, and utilisation for both windows."""
        today = await self._store.llm_spend_summary(since=day_start())
        total = await self._store.llm_spend_summary()
        return {
            "today": today,
            "total": total,
            "limits": {
                "daily_usd": self._config.daily_limit_usd,
                "total_usd": self._config.total_limit_usd,
                "warn_at": self._config.warn_at,
            },
            "utilisation": {
                "daily": _fraction(float(today["cost_usd"]), self._config.daily_limit_usd),
                "total": _fraction(float(total["cost_usd"]), self._config.total_limit_usd),
            },
            "blocked": await self._blocked_window() is not None,
        }

    async def _blocked_window(self) -> tuple[str, float, float] | None:
        """Return (window, spent, limit) for the first breached cap, if any."""
        checks = []
        if self._config.daily_limit_usd > 0:
            today = await self._store.llm_spend_summary(since=day_start())
            checks.append(("daily", float(today["cost_usd"]), self._config.daily_limit_usd))
        if self._config.total_limit_usd > 0:
            total = await self._store.llm_spend_summary()
            checks.append(("total", float(total["cost_usd"]), self._config.total_limit_usd))

        for window, spent, limit in checks:
            if spent >= limit:
                return (window, spent, limit)
            if spent >= limit * self._config.warn_at and window not in self._warned:
                self._warned.add(window)
                logger.bind(tag=_TAG).warning(
                    f"{window} LLM spend at ${spent:.4f} of ${limit:.2f} "
                    f"({100 * spent / limit:.0f}% of the cap)"
                )
        return None

    async def guard(self) -> None:
        """Raise SpendLimitExceeded if a cap is already reached.

        Called before issuing a request, so the breaching call is the one that
        crosses the line — spend can exceed the cap by at most one call.
        """
        breach = await self._blocked_window()
        if breach is None:
            return
        window, spent, limit = breach
        logger.bind(tag=_TAG).error(
            f"blocking LLM call — {window} spend ${spent:.4f} reached the ${limit:.2f} cap"
        )
        raise SpendLimitExceeded(window, spent, limit)

    async def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        device_id: str | None = None,
    ) -> None:
        """Append a call to the ledger, estimating cost if none was reported."""
        estimated = cost_usd is None
        if estimated:
            cost_usd = self._config.estimate_cost(model, prompt_tokens, completion_tokens)
        await self._store.record_llm_spend(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=float(cost_usd or 0.0),
            cost_estimated=estimated,
            device_id=device_id,
        )
        # A new UTC day resets the daily window, so let it warn again.
        if self._warned_day != day_start():
            self._warned.discard("daily")
            self._warned_day = day_start()


def _fraction(spent: float, limit: float) -> float | None:
    """Spent/limit, or None when that cap is disabled."""
    return None if limit <= 0 else spent / limit


_tracker: SpendTracker | None = None


def configure(store: RegistryStore, config: dict[str, Any]) -> SpendTracker:
    """Install the process-wide tracker. Called once at server startup."""
    global _tracker
    spend_config = SpendConfig.from_config(config)
    _tracker = SpendTracker(store, spend_config)
    if spend_config.daily_limit_usd or spend_config.total_limit_usd:
        logger.bind(tag=_TAG).info(
            f"LLM spend limits — daily ${spend_config.daily_limit_usd:.2f}, "
            f"total ${spend_config.total_limit_usd:.2f}, "
            f"warning at {100 * spend_config.warn_at:.0f}%"
        )
    else:
        logger.bind(tag=_TAG).info("LLM spend metering on, no limits configured")
    return _tracker


def get_tracker() -> SpendTracker | None:
    """The configured tracker, or None when metering is not wired up."""
    return _tracker


def reset() -> None:
    """Drop the configured tracker. For tests."""
    global _tracker
    _tracker = None


async def guard() -> None:
    """Enforce limits if metering is configured; otherwise do nothing."""
    if _tracker is not None:
        await _tracker.guard()


# The agent on whose behalf the current task is calling the LLM. Set once per
# voice session / page turn; every provider call made inside that task (and
# tasks it spawns) is then attributed to that agent, whatever its kind. This
# is what lets the dashboard show spend per agent for devices, page agents
# and runners alike without threading device_id through the provider API.
_current_device: ContextVar[str | None] = ContextVar("agent_hub_spend_device", default=None)


def bind_device(device_id: str | None) -> None:
    """Attribute LLM spend in the current task (and its children) to device_id."""
    _current_device.set(device_id)


def current_device() -> str | None:
    """The agent bound with bind_device() in this task, if any."""
    return _current_device.get()


async def record(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None,
    device_id: str | None = None,
) -> None:
    """Meter one call if metering is configured; otherwise do nothing."""
    if _tracker is not None:
        attributed = device_id if device_id is not None else _current_device.get()
        await _tracker.record(model, prompt_tokens, completion_tokens, cost_usd, attributed)
