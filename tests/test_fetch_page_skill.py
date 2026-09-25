"""The fetch_page skill: allowed sites only, public addresses only, calendars as events."""

from __future__ import annotations

import socket
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

import agent_hub.skills as skills
from agent_hub.config import _apply_env_overrides
from agent_hub.skills import fetch_page

_DOMAINS = ("fubarlabs.org", "meetup.com")
_NY = ZoneInfo("America/New_York")


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every name resolves to a public address, so tests never touch real DNS."""

    async def _resolve(self: Any, host: str, port: int, **_kw: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", _resolve)


def test_hosts_must_be_an_allowed_domain_or_a_subdomain() -> None:
    assert fetch_page.host_allowed("fubarlabs.org", _DOMAINS)
    assert fetch_page.host_allowed("www.meetup.com", _DOMAINS)
    assert not fetch_page.host_allowed("fubarlabs.org.evil.com", _DOMAINS)
    assert not fetch_page.host_allowed("evilfubarlabs.org", _DOMAINS)


@pytest.mark.parametrize(
    "url",
    [
        "http://fubarlabs.org/",
        "https://example.com/",
        "https://127.0.0.1/",
        "https://user:pw@fubarlabs.org/",
        "file:///etc/passwd",
    ],
)
def test_urls_outside_the_fence_are_refused(url: str) -> None:
    with pytest.raises(fetch_page.FetchRefused):
        fetch_page.check_url(url, _DOMAINS)


async def test_an_allowed_name_on_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolve(self: Any, host: str, port: int, **_kw: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", _resolve)
    with pytest.raises(fetch_page.FetchRefused, match="non-public"):
        await fetch_page.check_public("fubarlabs.org")


async def test_a_redirect_off_the_allow_list_is_not_followed(public_dns: None) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    with pytest.raises(fetch_page.FetchRefused, match="evil.example is not an allowed site"):
        await fetch_page.fetch(
            "https://fubarlabs.org/calendar", _DOMAINS, transport=httpx.MockTransport(handler)
        )
    assert requested == ["https://fubarlabs.org/calendar"]


async def test_redirects_within_the_allow_list_are_followed(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fubarlabs.org":
            return httpx.Response(301, headers={"location": "https://www.fubarlabs.org/"})
        return httpx.Response(200, text="<p>Hi</p>", headers={"content-type": "text/html"})

    url, content_type, body = await fetch_page.fetch(
        "https://fubarlabs.org/", _DOMAINS, transport=httpx.MockTransport(handler)
    )
    assert (url, content_type, body) == ("https://www.fubarlabs.org/", "text/html", "<p>Hi</p>")


def test_page_text_drops_scripts_and_tags() -> None:
    page = "<html><script>var x=1</script><h1>FUBAR&amp;Labs</h1><p>Open  Hack</p></html>"
    assert fetch_page.page_text(page) == "FUBAR&Labs\nOpen Hack"


_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260924T193000
SUMMARY:Open Hack Night
URL:https://www.meetup.com/fubarlabs/events/1/
END:VEVENT
BEGIN:VEVENT
DTSTART:20260920T233000Z
SUMMARY:Last week's hack
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261003
SUMMARY:Maker Faire
LOCATION:1510 Jersey Ave\\, North
  Brunswick
END:VEVENT
END:VCALENDAR
"""


def test_a_calendar_feed_becomes_upcoming_events_in_order() -> None:
    now = datetime(2026, 9, 24, 12, 0, tzinfo=_NY)
    text = fetch_page.upcoming_events(_ICS, now=now, tz=_NY)
    assert text.splitlines() == [
        "Upcoming events:",
        "Thu Sep 24, 7:30 PM EDT: Open Hack Night (https://www.meetup.com/fubarlabs/events/1/)",
        "Sat Oct 3, 12:00 AM EDT: Maker Faire — 1510 Jersey Ave, North Brunswick",
    ]


async def test_execute_reports_a_refusal_without_fetching() -> None:
    result = await fetch_page.execute({"url": "https://example.com/"})
    assert not result.ok
    assert "not an allowed site" in result.text


def test_the_allow_list_comes_from_config_or_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert fetch_page.allowed_domains({}) == _DOMAINS
    monkeypatch.setenv("AGENT_HUB_SKILLS_FETCH_ALLOWED_DOMAINS", "fubarlabs.org, .Example.org")
    config = _apply_env_overrides({})
    assert fetch_page.allowed_domains(config) == ("fubarlabs.org", "example.org")


def test_fetch_page_is_off_unless_a_persona_ticks_it() -> None:
    assert "fetch_page" not in skills.default_skill_names()
    assert not skills.is_enabled("fetch_page", None)
    assert skills.is_enabled("fetch_page", ["fetch_page"])
    assert skills.is_enabled("web_search", None)
    assert not skills.is_enabled("web_search", ["fetch_page"])


def test_browser_agents_get_only_the_personas_skills() -> None:
    from agent_hub.registry.models import Persona
    from agent_hub.server.agent_turn import agent_tool_defs

    def names(persona: Persona) -> set[str]:
        return {d["function"]["name"] for d in agent_tool_defs("page-none", persona)}

    defaults = names(Persona(name="d"))
    assert "web_search" in defaults and "fetch_page" not in defaults
    ticked = names(Persona(name="t", server_skills='["fetch_page"]'))
    assert ticked == {"fetch_page"}


async def test_ticking_fetch_page_on_a_persona_is_saved(store: Any) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from agent_hub.dashboard import app as dashboard_app

    app = FastAPI()
    app.include_router(dashboard_app.make_router(store, {}))
    everything = sorted(skills.default_skill_names() | {"fetch_page"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        saved = await c.post(
            "/dashboard/personas/hub-default",
            data={"system_prompt": "hi", "server_skills": everything},
        )
        page = await c.get("/dashboard/personas/hub-default")
    assert saved.status_code == 200
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None and "fetch_page" in (persona.server_skills_list or [])
    assert 'value="fetch_page" checked' in page.text
