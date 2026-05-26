"""Skill: get current weather for a location via wttr.in."""

from typing import Any

import httpx

DEFINITION = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get current weather conditions and temperature for a city or location. "
            "Call this when the user asks about weather, temperature, or conditions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name or location, e.g. 'Seattle' or 'London, UK'.",
                }
            },
            "required": ["location"],
        },
    },
}


async def execute(args: dict[str, Any]) -> str:
    location = args.get("location", "").strip()
    if not location:
        return "Location required."
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://wttr.in/{location}",
                params={"format": "j1"},
                headers={"User-Agent": "agent-hub/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()

        current = data["current_condition"][0]
        desc = current["weatherDesc"][0]["value"]
        temp_f = current["temp_F"]
        temp_c = current["temp_C"]
        feels_f = current["FeelsLikeF"]
        humidity = current["humidity"]
        wind_mph = current["windspeedMiles"]

        area = data["nearest_area"][0]
        city = area["areaName"][0]["value"]
        country = area["country"][0]["value"]

        return (
            f"Weather in {city}, {country}: {desc}. "
            f"{temp_f}°F ({temp_c}°C), feels like {feels_f}°F. "
            f"Humidity {humidity}%, wind {wind_mph} mph."
        )
    except Exception as exc:
        return f"Could not get weather for {location!r}: {exc}"
