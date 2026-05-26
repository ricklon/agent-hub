"""Skill: web search via DuckDuckGo Instant Answers API."""

from typing import Any

import httpx

DEFINITION = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information and return a brief summary. "
            "Use this for factual questions, current events, or anything that may "
            "have changed since the model's knowledge cutoff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                }
            },
            "required": ["query"],
        },
    },
}


async def execute(args: dict[str, Any]) -> str:
    query = args.get("query", "").strip()
    if not query:
        return "Query required."
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": query,
                    "format": "json",
                    "no_redirect": "1",
                    "no_html": "1",
                    "skip_disambig": "1",
                },
                headers={"User-Agent": "agent-hub/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()

        parts: list[str] = []
        if data.get("Answer"):
            parts.append(data["Answer"])
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        if not parts:
            for topic in data.get("RelatedTopics", [])[:3]:
                if isinstance(topic, dict) and topic.get("Text"):
                    parts.append(topic["Text"])

        if parts:
            return " ".join(parts)[:800]
        return f"No results found for: {query!r}"
    except Exception as exc:
        return f"Search failed: {exc}"
