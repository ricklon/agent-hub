"""Tests for structured server-side skill results."""

from __future__ import annotations

import agent_hub.skills as skills
from agent_hub.skills import get_weather, web_search


async def test_run_result_reports_unknown_skill_failure() -> None:
    result = await skills.run_result("missing_skill", {})

    assert result.ok is False
    assert "unknown skill" in result.text
    assert result.error == result.text


async def test_run_preserves_text_compatibility() -> None:
    text = await skills.run("missing_skill", {})

    assert "unknown skill" in text


async def test_weather_missing_location_is_structured_failure() -> None:
    result = await get_weather.execute({})

    assert result.ok is False
    assert result.text == "Location required."


async def test_web_search_missing_query_is_structured_failure() -> None:
    result = await web_search.execute({})

    assert result.ok is False
    assert result.text == "Query required."
