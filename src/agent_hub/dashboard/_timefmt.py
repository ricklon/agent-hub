"""Render stored (UTC) timestamps in the server's configured display zone.

Datetimes persisted through SQLAlchemy ``func.now()`` come back naive and are
UTC. The dashboard used to ``strftime`` them straight, so every "last seen",
audit entry, and transcript line read hours off wherever the server is not on
UTC. Route them through here instead.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from agent_hub.config import to_local

_MISSING = "—"


def fmt_ts(
    value: datetime | str | None,
    tz: tzinfo | None = None,
    fmt: str = "%Y-%m-%d %H:%M",
) -> str:
    """Format ``value`` (a datetime or ISO string, assumed UTC if naive) in ``tz``.

    ``tz`` defaults to the server's configured display zone (config + env).
    Returns an em dash for a missing value and the original string if it cannot
    be parsed, so a formatting slip never blanks a page.
    """
    if value is None or value == "":
        return _MISSING
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value
    return to_local(dt, tz).strftime(fmt)
