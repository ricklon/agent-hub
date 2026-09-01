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


class TestGetCurrentTime:
    def test_uses_the_configured_iana_timezone(self, monkeypatch) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from agent_hub.skills import get_current_time

        monkeypatch.setenv("AGENT_HUB_SERVER_TIMEZONE", "America/New_York")
        result = get_current_time.execute({})

        assert result.ok
        expected_hour = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M %p")
        assert expected_hour in result.text
        assert ("EDT" in result.text) or ("EST" in result.text)

    def test_falls_back_to_the_fixed_offset(self, monkeypatch) -> None:
        from datetime import datetime, timedelta, timezone

        from agent_hub.skills import get_current_time

        # Empty string clears any configured IANA name (from data/.config.yaml
        # or the environment) so the fixed offset is what's exercised.
        monkeypatch.setenv("AGENT_HUB_SERVER_TIMEZONE", "")
        monkeypatch.setenv("AGENT_HUB_SERVER_TIMEZONE_OFFSET", "5")
        result = get_current_time.execute({})

        assert result.ok
        expected = datetime.now(timezone(timedelta(hours=5))).strftime("%A, %B %d, %Y")
        assert expected in result.text

    def test_unknown_timezone_name_does_not_crash(self, monkeypatch) -> None:
        from agent_hub.skills import get_current_time

        monkeypatch.setenv("AGENT_HUB_SERVER_TIMEZONE", "Not/AZone")
        result = get_current_time.execute({})
        assert result.ok and result.text
