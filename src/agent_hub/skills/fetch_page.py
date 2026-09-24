"""Skill: read a page from an allowed site, fetched by the hub.

A browser page agent cannot read most sites itself (CORS), so this runs on
the hub. Because the model chooses the URL, and the model can be steered by
what visitors say or by the pages it reads, every request is fenced:

- https only, and the host must be an allowed domain or a subdomain of one
  (``skills.fetch_allowed_domains``; default ``fubarlabs.org, meetup.com``);
- the host must resolve to public addresses only, so an allowed name cannot
  be pointed at the hub, its dashboard, or the cloud metadata service;
- redirects are followed by hand, each hop checked the same way;
- short timeout, capped download, capped text back to the model.

Web pages come back as their visible text. iCalendar feeds (``text/calendar``,
e.g. a Meetup group's ``/events/ical/``) come back as the upcoming events,
soonest first, in the hub's timezone. Recurring series (RRULE) are not
expanded; feeds that list each occurrence, as Meetup's does, work best.

Off by default: a persona gets it only when it is ticked on the persona.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from agent_hub.config import configured_timezone, load_config
from agent_hub.skills import SkillResult

DEFAULT_ENABLED = False

DEFAULT_ALLOWED_DOMAINS = ("fubarlabs.org", "meetup.com")
_MAX_BYTES = 1_000_000
_MAX_TEXT = 4000
_MAX_REDIRECTS = 5
_MAX_EVENTS = 12
_TIMEOUT_S = 10.0

DEFINITION = {
    "type": "function",
    "function": {
        "name": "fetch_page",
        "description": (
            "Read a web page or calendar feed from one of the hub's allowed sites and "
            "return its text. Calendar feeds (.ics, e.g. a Meetup group's /events/ical/) "
            "return the upcoming events with dates and times. Use it for schedules and "
            "other details that change; only https URLs on allowed sites work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The https URL to read."},
            },
            "required": ["url"],
        },
    },
}


class FetchRefused(ValueError):
    """The URL is outside what this skill may fetch."""


def allowed_domains(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    """The allow list from ``skills.fetch_allowed_domains`` (list or comma string)."""
    cfg = config if config is not None else load_config()
    raw = (cfg.get("skills") or {}).get("fetch_allowed_domains")
    if raw is None:
        return DEFAULT_ALLOWED_DOMAINS
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return tuple(d.strip().lower().lstrip(".") for d in items if str(d).strip())


def host_allowed(host: str, domains: tuple[str, ...]) -> bool:
    """True when ``host`` is an allowed domain or a subdomain of one."""
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def check_url(url: str, domains: tuple[str, ...]) -> str:
    """Return the host of an https URL on an allowed domain, else raise FetchRefused."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefused(f"only https URLs are allowed, not {url!r}")
    host = parts.hostname or ""
    if not host or parts.username or parts.password:
        raise FetchRefused(f"not a plain web address: {url!r}")
    if not host_allowed(host, domains):
        raise FetchRefused(f"{host} is not an allowed site (allowed: {', '.join(domains)})")
    return host


async def check_public(host: str) -> None:
    """Raise FetchRefused unless every address ``host`` resolves to is public."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchRefused(f"could not resolve {host}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise FetchRefused(f"{host} resolves to a non-public address")


async def fetch(
    url: str, domains: tuple[str, ...], transport: httpx.AsyncBaseTransport | None = None
) -> tuple[str, str, str]:
    """Fetch ``url`` within the fence; return (final url, content type, text).

    Args:
        url: The URL the model asked for.
        domains: The allow list.
        transport: HTTP transport override, for tests.
    """
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": "agent-hub/1.0"},
        transport=transport,
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            host = check_url(url, domains)
            await check_public(host)
            async with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"{url} answered HTTP {resp.status_code}")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        break
                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
                text = bytes(body[:_MAX_BYTES]).decode(resp.encoding or "utf-8", "replace")
                return url, content_type, text
    raise RuntimeError(f"too many redirects from {url}")


def page_text(markup: str) -> str:
    """Visible text of an HTML page: no scripts, styles, or tags."""
    markup = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _ics_lines(raw: str) -> list[str]:
    """Unfold iCalendar content lines (a leading space continues the line above)."""
    lines: list[str] = []
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _ics_value(value: str) -> str:
    return value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()


def _ics_start(params: str, value: str, tz: Any) -> datetime | None:
    """DTSTART as an aware datetime (all-day dates at midnight in ``tz``)."""
    value = value.strip()
    try:
        if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
            day = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
            return datetime(day.year, day.month, day.day, tzinfo=tz)
        moment = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if value.endswith("Z"):
        return moment.replace(tzinfo=UTC)
    zone = re.search(r"TZID=([^;:]+)", params)
    try:
        return moment.replace(tzinfo=ZoneInfo(zone.group(1)) if zone else tz)
    except (KeyError, ValueError):
        return moment.replace(tzinfo=tz)


def upcoming_events(raw: str, now: datetime | None = None, tz: Any = None) -> str:
    """The upcoming events of an iCalendar feed, soonest first, one per line."""
    tz = tz or configured_timezone()
    now = now or datetime.now(tz)
    events: list[tuple[datetime, str]] = []
    current: dict[str, str] | None = None
    for line in _ics_lines(raw):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            start = _ics_start(current.get("DTSTART_PARAMS", ""), current.get("DTSTART", ""), tz)
            if start is not None and start >= now - timedelta(hours=3):
                when = start.astimezone(tz).strftime("%a %b %d, %I:%M %p %Z").replace(" 0", " ")
                detail = " — ".join(
                    x for x in (current.get("SUMMARY"), current.get("LOCATION")) if x
                )
                url = f" ({current['URL']})" if current.get("URL") else ""
                events.append((start, f"{when}: {detail}{url}"))
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            name, _, params = key.partition(";")
            if name == "DTSTART":
                current["DTSTART_PARAMS"] = params
            if name in ("SUMMARY", "DTSTART", "LOCATION", "URL"):
                current[name] = _ics_value(value)
    if not events:
        return "No upcoming events in this calendar."
    events.sort(key=lambda e: e[0])
    return "Upcoming events:\n" + "\n".join(line for _start, line in events[:_MAX_EVENTS])


async def execute(args: dict[str, Any]) -> SkillResult:
    """Fetch an allowed page or calendar feed and return its text."""
    url = str(args.get("url") or "").strip()
    if not url:
        return SkillResult.failure("A URL is required.")
    try:
        final_url, content_type, body = await fetch(url, allowed_domains())
    except FetchRefused as exc:
        return SkillResult.failure(f"Not fetched: {exc}.")
    except (httpx.HTTPError, RuntimeError) as exc:
        return SkillResult.failure(f"Could not read {url}: {exc}", error=str(exc))
    if content_type == "text/calendar" or body.lstrip().startswith("BEGIN:VCALENDAR"):
        text = upcoming_events(body)
    elif content_type in ("text/html", "application/xhtml+xml"):
        text = page_text(body)
    elif content_type.startswith("text/") or content_type.endswith("json"):
        text = body.strip()
    else:
        return SkillResult.failure(f"{final_url} is {content_type or 'not text'}; not read.")
    return SkillResult.success(f"From {final_url}:\n{text[:_MAX_TEXT]}")
