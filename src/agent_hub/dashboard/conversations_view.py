"""Rendering for the dashboard's conversations: the list, one conversation, and context.

The agent page used to show the last 60 messages as one undivided stream. It
now shows conversations: when each started, how long it ran, how many turns,
what it was about, and what the model will remember next time.
"""

from __future__ import annotations

import html
import re
import urllib.parse as _up
from datetime import datetime, tzinfo

from agent_hub.dashboard._timefmt import fmt_ts
from agent_hub.registry.models import Conversation, ConversationKind

_IMAGE_RE = re.compile(r"\[image:([^\]]+)\]")
_VOLATILE_RE = re.compile(r"\n?\[volatile-tools:[^\]]+\]")
_ROLE_COLOURS = {"user": "#79c0ff", "assistant": "#3fb950", "transcript": "#c9d1d9"}


def render_message(raw: str) -> str:
    """Message text as HTML: photos inline, everything else escaped.

    Message content is device- and model-supplied, so it must never be able to
    inject markup.
    """
    text = _VOLATILE_RE.sub("", raw).strip()
    parts: list[str] = []
    last = 0
    for match in _IMAGE_RE.finditer(text):
        parts.append(html.escape(text[last : match.start()]))
        enc = _up.quote(match.group(1), safe="")
        parts.append(
            f'<br><img src="/dashboard/image?path={enc}" '
            'style="max-width:320px;border-radius:6px;margin-top:0.4rem;display:block">'
        )
        last = match.end()
    parts.append(html.escape(text[last:]))
    return "".join(parts)


def duration(conversation: Conversation) -> str:
    """How long a conversation ran, as "4 min" / "1 h 12 min" / "—"."""
    end = conversation.ended_at or conversation.last_turn_at
    if end is None or conversation.started_at is None:
        return "—"
    minutes = max(0, int((end - conversation.started_at).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def title_of(conversation: Conversation) -> str:
    """Its title, or a placeholder for one not yet named."""
    return conversation.title or "Untitled conversation"


def render_list(
    conversations: list[Conversation],
    device_id: str,
    tz: tzinfo | None,
    *,
    more_before_id: int | None = None,
    noun: str = "conversation",
) -> str:
    """The conversations of one agent, newest first, with an Older button."""
    quoted = _up.quote(device_id, safe="")
    if not conversations:
        return f'<div id="conversation-list"><p class="doc-muted">No {noun}s yet.</p></div>'
    rows = []
    for conversation in conversations:
        public_id = html.escape(_up.quote(conversation.public_id, safe=""))
        state = (
            '<span class="badge badge-kind">in progress</span>'
            if conversation.ended_at is None
            else ""
        )
        summary = (
            '<div class="doc-muted" style="max-width:52rem">'
            f"{html.escape(conversation.summary)}</div>"
            if conversation.summary
            else ""
        )
        persona = (
            f" · {html.escape(conversation.persona_name)}" if conversation.persona_name else ""
        )
        rows.append(
            f"""\
<li style="padding:.35rem 0">
  <a href="/dashboard/agents/{quoted}/conversations/{public_id}"
     style="color:#58a6ff">{html.escape(title_of(conversation))}</a> {state}
  <div class="doc-muted" style="font-size:.8rem">
    {fmt_ts(conversation.started_at, tz, "%b %-d, %-I:%M %p")} ·
    {duration(conversation)} · {conversation.turn_count} turns{persona}</div>
  {summary}
</li>"""
        )
    older = (
        f"""\
<button class="secondary" hx-get="/dashboard/agents/{quoted}/conversations?before={more_before_id}"
        hx-target="#conversation-list" hx-swap="outerHTML">Older</button>"""
        if more_before_id is not None
        else ""
    )
    return f"""\
<div id="conversation-list">
<ul style="list-style:none;padding:0;margin:0">{"".join(rows)}</ul>
{older}
</div>"""


def render_conversation(
    conversation: Conversation,
    messages: list[dict[str, str]],
    device_id: str,
    tz: tzinfo | None,
    *,
    agent_label: str,
) -> str:
    """One conversation in full: what it was, then every message, uncapped."""
    quoted = _up.quote(device_id, safe="")
    public_id = _up.quote(conversation.public_id, safe="")
    rows: list[str] = []
    current_day = ""
    for message in messages:
        stamp = message.get("created_at")
        day = fmt_ts(stamp, tz, "%A, %B %-d")
        if day != current_day:
            current_day = day
            rows.append(
                f'<tr><th colspan="3" style="text-align:left;background:#161b22;color:#58a6ff">'
                f"{html.escape(day)}</th></tr>"
            )
        role = message.get("role", "")
        rows.append(
            "<tr>"
            f'<td style="color:#8b949e;white-space:nowrap;font-size:.75rem;vertical-align:top">'
            f"{fmt_ts(stamp, tz, '%-I:%M:%S %p')}</td>"
            f'<td style="color:{_ROLE_COLOURS.get(role, "#8b949e")};white-space:nowrap;'
            f'vertical-align:top">{html.escape(role)}</td>'
            f'<td style="white-space:pre-wrap;max-width:52rem">'
            f"{render_message(message.get('content') or '')}</td>"
            "</tr>"
        )
    ended = (
        fmt_ts(conversation.ended_at, tz, "%b %-d, %-I:%M %p")
        if conversation.ended_at
        else "in progress"
    )
    summary = (
        f"<p><strong>Summary</strong><br>{html.escape(conversation.summary)}</p>"
        if conversation.summary
        else '<p class="doc-muted">No summary yet.</p>'
    )
    return f"""\
<p><a href="/dashboard/agents/{quoted}" style="color:#58a6ff">← {html.escape(agent_label)}</a></p>
<h2>{html.escape(title_of(conversation))}</h2>
<p class="doc-muted">
  {fmt_ts(conversation.started_at, tz, "%b %-d, %-I:%M %p")} → {ended} ·
  {duration(conversation)} · {conversation.turn_count} turns ·
  {html.escape(conversation.persona_name or "no persona")} ·
  {html.escape(conversation.kind)}
</p>
<div id="conversation-actions" style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
  <form hx-post="/dashboard/agents/{quoted}/conversations/{public_id}/rename"
        hx-target="#conversation-actions" hx-swap="outerHTML"
        style="display:flex;gap:.3rem;align-items:center">
    <input type="text" name="title" maxlength="160" style="width:18rem"
           value="{html.escape(conversation.title or "")}" aria-label="Conversation title">
    <button type="submit">Rename</button>
  </form>
  <form hx-post="/dashboard/agents/{quoted}/conversations/{public_id}/title"
        hx-target="#conversation-actions" hx-swap="outerHTML" style="display:inline">
    <button type="submit" class="secondary"
            title="Ask this agent's model for a title and summary now">Title this</button>
  </form>
  <a class="action-link" href="/dashboard/agents/{quoted}/conversations/{public_id}.txt">⬇ .txt</a>
  <a class="action-link" href="/dashboard/agents/{quoted}/conversations/{public_id}.md">⬇ .md</a>
  <form hx-post="/dashboard/agents/{quoted}/conversations/{public_id}/delete"
        hx-confirm="Delete this conversation, its messages and its summary?"
        style="display:inline">
    <button type="submit" style="background:#b62324">Delete</button>
  </form>
</div>
{summary}
<table style="width:100%"><thead><tr><th>time</th><th>role</th><th>message</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p class="doc-muted">{len(messages)} messages · id {html.escape(conversation.public_id)}</p>"""


def render_context(note: str, messages: list[dict[str, str]], device_id: str) -> str:
    """ "What the model sees next": the memory note and the recent turns, verbatim."""
    quoted = _up.quote(device_id, safe="")
    note_html = (
        f'<pre style="white-space:pre-wrap">{html.escape(note)}</pre>'
        if note
        else '<p class="doc-muted">No earlier conversations are remembered '
        "(off, or none summarized yet).</p>"
    )
    turns = (
        "".join(
            f'<li><span style="color:{_ROLE_COLOURS.get(m["role"], "#8b949e")}">'
            f"{m['role']}</span>: {html.escape((m.get('content') or '')[:160])}</li>"
            for m in messages
        )
        or '<li class="doc-muted">Nothing yet: the next turn starts a conversation.</li>'
    )
    return f"""\
<div id="model-context" hx-get="/dashboard/agents/{quoted}/context"
     hx-trigger="every 10s" hx-swap="outerHTML">
  <p class="doc-muted">Exactly what goes to the model on the next turn, besides the
  persona's own prompt and its tools.</p>
  <h4 style="margin:.4rem 0 .2rem">Remembered conversations</h4>
  {note_html}
  <h4 style="margin:.6rem 0 .2rem">Recent turns ({len(messages)})</h4>
  <ul style="margin:0 0 0 1.1rem">{turns}</ul>
</div>"""


def export_text(
    conversation: Conversation,
    messages: list[dict[str, str]],
    tz: tzinfo | None,
    *,
    agent_label: str,
    device_id: str,
    markdown: bool,
) -> str:
    """One conversation as plain text or markdown, for download."""
    started = fmt_ts(conversation.started_at, tz, "%Y-%m-%d %H:%M:%S")
    ended = fmt_ts(conversation.ended_at, tz, "%Y-%m-%d %H:%M:%S") if conversation.ended_at else "—"
    title = title_of(conversation)
    lines: list[str] = []
    if markdown:
        lines += [f"# {title}", ""]
        lines += [
            f"- Agent: {agent_label} (`{device_id}`)",
            f"- Started: {started}",
            f"- Ended: {ended}",
            f"- Turns: {conversation.turn_count}",
            f"- Persona: {conversation.persona_name or '—'}",
        ]
        if conversation.summary:
            lines += ["", f"> {conversation.summary}"]
        lines += [""]
    else:
        lines += [
            f"{title}",
            f"Agent {agent_label} ({device_id})",
            f"{started} → {ended} · {conversation.turn_count} turns",
        ]
        if conversation.summary:
            lines += [f"Summary: {conversation.summary}"]
        lines += ["=" * 48, ""]
    for message in messages:
        stamp = fmt_ts(message.get("created_at"), tz, "%H:%M:%S")
        text = _VOLATILE_RE.sub("", message.get("content") or "").strip()
        text = _IMAGE_RE.sub("[photo]", text).strip() or "[photo]"
        role = message.get("role", "")
        lines.append(f"- **{stamp} {role}:** {text}" if markdown else f"[{stamp}] {role}: {text}")
    return "\n".join(lines) + "\n"


def export_filename(conversation: Conversation, device_id: str, suffix: str) -> str:
    """A download name from the title, falling back to the id."""
    base = re.sub(r"[^A-Za-z0-9]+", "-", title_of(conversation)).strip("-").lower()
    safe_device = device_id.replace(":", "-")
    return f"{safe_device}-{base or conversation.public_id}.{suffix}"


def conversation_json(conversation: Conversation) -> dict[str, object]:
    """One conversation as JSON, for tools and tests (UTC, ISO 8601)."""

    def iso(value: datetime | None) -> str | None:
        return value.replace(tzinfo=value.tzinfo).isoformat() if value else None

    return {
        "id": conversation.public_id,
        "device_id": conversation.device_id,
        "kind": conversation.kind,
        "title": conversation.title,
        "title_source": conversation.title_source,
        "summary": conversation.summary,
        "persona": conversation.persona_name,
        "started_at": iso(conversation.started_at),
        "last_turn_at": iso(conversation.last_turn_at),
        "ended_at": iso(conversation.ended_at),
        "turns": conversation.turn_count,
        "in_progress": conversation.ended_at is None,
        "is_transcript": conversation.kind == ConversationKind.TRANSCRIPT.value,
    }
