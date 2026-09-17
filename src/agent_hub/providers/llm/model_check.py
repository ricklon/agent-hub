"""Is a model usable for a voice agent? Ask it the way a voice turn does.

A model can be listed, even selected, and still be useless on a device:
removed from the provider (every call 404s), rate-limited, too slow to feel
conversational, or unwilling to call tools, which every persona here depends
on. The check runs a few short turns through the same streaming, tool-calling
path a voice turn uses and says which of those it is.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import openai

from agent_hub.providers.llm import LLMProvider
from agent_hub.spend import SpendLimitExceeded

Verdict = Literal["usable", "slow", "unreliable", "unusable"]

# Time to the first words of a reply, past which a voice conversation drags.
DEFAULT_SLOW_AFTER_S = 3.0
DEFAULT_TIMEOUT_S = 25.0

_SYSTEM_PROMPT = (
    "You are a voice assistant on a small device. Use tools when they help. "
    "Answer in one short sentence."
)
_TIME_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": (
            "Get the current local date and time. Always use this to answer "
            "questions about the time or date."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}
_CHAT_PROMPT = "Hi, can you hear me?"
_TOOL_PROMPT = "What time is it?"


@dataclass(frozen=True)
class Probe:
    """One short turn sent to the model."""

    prompt: str
    expects_tool: bool
    ok: bool
    total_s: float
    first_text_s: float | None = None
    reply: str = ""
    tool_called: bool = False
    error: str | None = None

    @property
    def problem(self) -> str | None:
        """Why this turn would have failed on a device, or None."""
        if self.error:
            return self.error
        if self.expects_tool and not self.tool_called:
            return "answered without calling the tool"
        if not self.reply:
            return "returned an empty reply"
        return None


@dataclass(frozen=True)
class ModelCheck:
    """The verdict on one model, with the turns it is based on."""

    model: str
    verdict: Verdict
    reason: str
    probes: list[Probe] = field(default_factory=list)
    median_first_text_s: float | None = None


def describe_error(exc: BaseException, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    """A short, plain reason for a failed model call."""
    detail = _provider_message(exc)
    if isinstance(exc, SpendLimitExceeded):
        return "blocked by the hub's spend limit"
    if isinstance(exc, openai.NotFoundError):
        return f"not available from the provider (removed or renamed){detail}"
    if isinstance(exc, openai.RateLimitError):
        return f"rate-limited by the provider{detail}"
    if isinstance(exc, openai.AuthenticationError):
        return "the provider rejected the API key"
    if isinstance(exc, openai.PermissionDeniedError):
        return f"refused by the provider{detail}"
    if isinstance(exc, (openai.APITimeoutError, TimeoutError)):
        return f"no complete reply within {timeout_s:g}s"
    if isinstance(exc, openai.APIConnectionError):
        return "could not reach the provider"
    if isinstance(exc, openai.APIStatusError):
        return f"provider error {exc.status_code}{detail}"
    return f"{type(exc).__name__}: {exc}"[:200]


def _provider_message(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    message = body.get("message") if isinstance(body, dict) else None
    return f": {str(message)[:160]}" if message else ""


async def _probe(llm: LLMProvider, prompt: str, *, expects_tool: bool, timeout_s: float) -> Probe:
    called: list[str] = []

    async def run_tool(name: str, args: dict[str, Any]) -> str:
        called.append(name)
        return "It is 3:15 PM on Thursday, September 17, 2026."

    started = time.monotonic()
    first_text: float | None = None
    parts: list[str] = []

    async def consume() -> None:
        nonlocal first_text
        async for delta in llm.stream_with_tools(
            [{"role": "user", "content": prompt}],
            [_TIME_TOOL],
            run_tool,
            system_prompt=_SYSTEM_PROMPT,
        ):
            if delta and first_text is None:
                first_text = time.monotonic() - started
            parts.append(delta)

    try:
        await asyncio.wait_for(consume(), timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a reason
        return Probe(
            prompt=prompt,
            expects_tool=expects_tool,
            ok=False,
            total_s=time.monotonic() - started,
            first_text_s=first_text,
            reply="".join(parts).strip(),
            tool_called="get_current_time" in called,
            error=describe_error(exc, timeout_s),
        )
    probe = Probe(
        prompt=prompt,
        expects_tool=expects_tool,
        ok=True,
        total_s=time.monotonic() - started,
        first_text_s=first_text,
        reply="".join(parts).strip(),
        tool_called="get_current_time" in called,
    )
    return probe if probe.problem is None else replace(probe, ok=False)


async def check_model(
    llm: LLMProvider,
    model: str,
    *,
    tool_rounds: int = 2,
    slow_after_s: float = DEFAULT_SLOW_AFTER_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ModelCheck:
    """Run a greeting and ``tool_rounds`` tool-requiring turns, then judge.

    Verdicts, worst first:

    - ``unusable``: nothing worked, or no turn called the tool. Tool use is
      required, so a model that never calls one cannot drive an agent.
    - ``unreliable``: some turns failed (an error, an empty reply, or a
      question answered without the tool it needed).
    - ``slow``: every turn worked but the median time to the first words of a
      reply is over ``slow_after_s``.
    - ``usable``: every turn worked, quickly enough.

    Args:
        llm: A provider already bound to ``model``.
        model: The model id, for the report.
        tool_rounds: How many times to ask the tool-requiring question.
        slow_after_s: Median time-to-first-words threshold for ``slow``.
        timeout_s: Per-turn limit; a turn past it counts as failed.
    """
    probes = [await _probe(llm, _CHAT_PROMPT, expects_tool=False, timeout_s=timeout_s)]
    for _ in range(tool_rounds):
        probes.append(await _probe(llm, _TOOL_PROMPT, expects_tool=True, timeout_s=timeout_s))

    firsts = [p.first_text_s for p in probes if p.ok and p.first_text_s is not None]
    median = statistics.median(firsts) if firsts else None
    failed = [p for p in probes if not p.ok]
    tool_probes = [p for p in probes if p.expects_tool]

    if len(failed) == len(probes):
        verdict: Verdict = "unusable"
        reason = failed[0].problem or "every turn failed"
    elif not any(p.tool_called for p in tool_probes):
        verdict = "unusable"
        reason = "never called a tool, and tool use is required"
    elif failed:
        verdict = "unreliable"
        problems = sorted({p.problem or "failed" for p in failed})
        reason = f"{len(failed)} of {len(probes)} turns failed: " + "; ".join(problems)
    elif median is not None and median > slow_after_s:
        verdict = "slow"
        reason = f"median {median:.1f}s to the first words (over {slow_after_s:.0f}s)"
    else:
        verdict = "usable"
        timing = f", median {median:.1f}s to the first words" if median is not None else ""
        reason = f"answered and called the tool every time{timing}"
    return ModelCheck(
        model=model, verdict=verdict, reason=reason, probes=probes, median_first_text_s=median
    )
