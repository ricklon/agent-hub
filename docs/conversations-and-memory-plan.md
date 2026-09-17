# Plan: conversations and memory

Status: **proposed.** Plan 2 of the three fixes for the confusing multi-user
experience (plan 1, [page agent ownership](page-agent-ownership-plan.md), is
built). Decisions below were made on 2026-09-17.

## The complaint

Conversation and memory features aren't integrated clearly. History is cut off
at 60 lines. There's no clear date and time, no names for past conversations,
and none of it can be configured per agent.

## What happens today

There is no such thing as a conversation. There is one endless list of
messages per device, and "memory" is the tail of that list.

| Piece | Code | What it does |
| --- | --- | --- |
| Storage | `ConversationTurn` (`conversation_history`) | One row per message: `device_id`, `role`, `content`, `created_at`. Nothing marks where one conversation ends and the next begins. |
| Grouping | `ConversationTurn.session_id` | Only transcriber devices set it (one per listen start/stop). `store.list_sessions` exists; no page uses it. |
| Memory | `persona.memory_window` | The last `memory_window × 2` messages for the device, whatever conversation or persona they came from. Loaded in three places: `ws_session.py` (device voice), `page_agent.py` (page voice), `agent_turn.py` (dashboard ask, page text). |
| Dashboard | agent page "History" | The last 60 messages (`load_history(limit=60)`), with timestamps, no grouping. "Clear history" deletes everything. |
| Persona editor | "Memory" section | Just the window number, with no link to the history it describes. |
| Export | `transcript.txt?session=` | Whole history, or one transcription session by id. |

So memory and history are the same rows seen two ways, and the dashboard
explains neither:

1. **No conversations.** Nothing to name, list, open, or delete one at a time.
2. **Memory leaks across contexts.** A conversation from last week, or one held
   under a different persona, is still in the window until it scrolls out.
3. **60-message cap** on the only history view.
4. **Timestamps without structure.** Each line has a time, but nothing shows
   where a conversation started, how long it ran, or which day it was.
5. **Nothing is configurable per agent.** The window is per persona.

### Bug found while planning: non-chat rows reach the model

Photos write `image` rows (`image_explain.py`) and transcriber devices write
`transcript` rows (`ws_session.py`). The device voice path filters those out
before calling the model (`_history_for_llm`). Dashboard **Ask this agent**
(`agent_turn.py`) and **page agent voice** (`page_agent.py`) do not.

OpenRouter rejects a message with role `image` with HTTP 400 (checked
2026-09-17 against `inclusionai/ling-3.0-flash-vl:free`). So on any agent whose
recent history includes a photo or a transcript line, those two paths fail
every turn. This is fixed first (phase 0), independent of the rest.

## Decisions

| Question | Decision |
| --- | --- |
| What starts a new conversation? | An **idle gap** (default 30 minutes, configurable) or a manual **New conversation**. Also, added here: switching the agent's persona. |
| Where do the settings live? | **Defaults on the persona, optional override per agent.** |
| Who writes titles? | **The persona's own model.** |
| Long-term memory? | **In the first release**: summarize each conversation when it ends, and carry recent summaries into a new one. |

## Concepts, as the dashboard will name them

- **Conversation**: a run of turns with one agent. Starts at the first turn
  after a boundary; ends at the next boundary. Has a start and end time, a
  title, a summary, the persona it ran under, and a turn count. A transcriber
  device's listen session is a conversation of kind `transcript`.
- **Recent turns**: the last *N* turns of the *current* conversation, sent to
  the model verbatim. This is today's `memory_window`, no longer spanning
  conversations.
- **Remembered conversations**: summaries of this agent's last *K* finished
  conversations, sent to the model as a short "earlier conversations" note
  when a new conversation starts. This is the long-term memory.
- **What the model sees next**: recent turns plus remembered conversations,
  shown on the agent page exactly as they'd be sent.

## Data model

New table `conversations`:

| Column | Notes |
| --- | --- |
| `id` | primary key |
| `public_id` | 16 hex characters, used in URLs and exports |
| `device_id` | indexed |
| `kind` | `chat` or `transcript` |
| `persona_id`, `persona_name` | the persona it ran under; the name is a snapshot for when the persona is renamed or deleted |
| `title`, `title_source` | `auto`, `manual` (never overwritten), or `fallback` |
| `summary`, `wrapped_up_at` | written by the wrap-up call |
| `started_at`, `last_turn_at`, `ended_at` | `ended_at` is null while in progress |
| `turn_count` | user turns, for the list view |

`ConversationTurn` gains `conversation_id` (indexed). `session_id` stays until
the transcriber paths move over, then is dropped.

**Migration** (runs in `store._migrate()` on start): group existing rows per
device into conversations by the same 30-minute idle gap; transcript rows by
their `session_id` (which becomes the public id). Backfilled conversations get
fallback titles from their first words (the first reply, if the user's words
were lost), shown next to their start time rather than baked into the title,
since the store doesn't know the hub's time zone. They get no summary, and no
model is called during migration. Each gets a **Title this** button instead,
so nobody's bill jumps on upgrade. Each device's latest chat conversation stays
open, so a chat in progress at upgrade isn't cut off.

**Store API** replaces the ad-hoc `load_history` calls:

- `current_conversation(device_id, persona, settings, now)`: the open conversation,
  or a new one if the idle gap passed, the persona changed, or there is none
- `append_turn(conversation, role, content)`
- `context_messages(device_id, settings)`: the exact messages for the model,
  **one function for all three turn paths** (also where phase 0's role
  filtering ends up)
- `list_conversations(device_id, before=None, limit=20)`: newest first, paged
- `load_conversation(public_id)`: every turn, uncapped
- `end_conversation`, `rename_conversation`, `delete_conversation`

## Settings

Persona fields (defaults), each with a nullable column on `agents` of the same
name (null = use the persona's):

| Setting | Default | Meaning |
| --- | --- | --- |
| `conversation_idle_minutes` | 30 | silence that ends a conversation |
| `memory_window` | 20 (existing) | recent turns of the current conversation the model sees |
| `auto_title` | on | name finished conversations with the persona's model |
| `summarize_conversations` | on | write a summary when a conversation ends |
| `remember_conversations` | 3 | summaries of earlier conversations carried into a new one (0 = off) |

`remember_conversations` needs `summarize_conversations`. The editor disables
it when summaries are off. One resolver,
`effective_conversation_settings(agent, persona)`, returns each value and
where it came from (persona or agent), and every turn path uses it.

UI: the persona editor's "Memory" section becomes **Conversations & memory**.
The agent page gets the same fields, each showing "from persona hub-default"
until overridden, with a **Use persona default** reset.

## Wrap-up: titles and summaries

**When:** a conversation ends at the next boundary. Because an idle gap has no
event, a sweeper runs every 5 minutes and ends conversations whose
`last_turn_at` is older than their agent's idle gap, so titles appear without
waiting for the next turn. New conversation and persona switches end it
immediately.

**How:** one call to the persona's model (plain completion, no tools), asking
for JSON `{"title": ≤ 6 words, "summary": ≤ 3 sentences}`. Transcripts use a
prompt for a recording rather than a chat. One call gives both, so enabling
titles and summaries costs one call per conversation, not two.

- Skipped (fallback title, no summary) for conversations with fewer than 2
  user turns. A single "what time is it?" isn't worth a call.
- Goes through the spend guard. Free mode is respected automatically: the
  persona's model is already one free mode allowed.
- On failure: fallback title, `wrapped_up_at` left empty, retried once by the
  next sweep, then left for a manual **Title this**.
- `title_source = manual` titles are never replaced.

## Remembered conversations

At the start of a conversation (its first turn), if `remember_conversations >
0`, the context gets a system note built from this agent's last *K* ended
conversations that have summaries:

```
Earlier conversations with you (most recent first):
- Sep 16, 7:17 PM — Kitchen timer and pasta: The user set a 12-minute timer and asked how long to boil fettuccine.
- Sep 15, 9:02 AM — Weather for the bike ride: …
```

- Scoped to **this agent**, across personas. The persona switch starts a new
  conversation, but what the agent learned stays available. A persona setting
  can turn this off (`remember_conversations = 0`).
- Deleting a conversation deletes its summary, so it's forgotten too.
  **Delete all conversations** (replacing "Clear history") forgets everything.
- The note counts toward the prompt, so the editor shows its size next to the
  setting.

## Dashboard

**Agent page: Conversations** (replaces History):

- List, newest first: title, start date and time (hub time zone), duration,
  turns, persona. Current one marked **in progress**. 20 per page with
  **Older**, so there's no cap.
- **New conversation** button.
- **What the model sees next**: the remembered-conversations note and the
  recent turns, exactly as they'd be sent, with counts.
- **Delete all conversations** (with confirm).

**Conversation page** (`/dashboard/agents/{device}/conversations/{public_id}`):

- Title (click to rename), summary, persona, start/end, duration
- Every turn, uncapped, with a date heading per day and a time per line;
  photos inline as today
- **Title this** (for backfilled or failed ones), **Delete**, **Export** (.txt
  and .md)

**Page agent**: the current conversation's title, and a **New conversation**
button.

**Transcriber devices** use the same list and page (kind `transcript`),
replacing the current-session-only view and `transcript.txt?session=`. The
old URL redirects to the new export.

JSON: `GET /dashboard/agents/{device}/conversations.json` and
`…/conversations/{public_id}.json`, read-only, for tools and tests.

## Behaviour changes to announce

- **Memory stops spanning unrelated chats.** After an idle gap the model starts
  fresh, apart from the remembered-conversations note.
- **Switching persona starts a new conversation.**
- **Titles and summaries cost one model call per finished conversation** of 2+
  turns, on the persona's model.
- **"Clear history" becomes "Delete all conversations"**, and it also forgets
  the summaries.

## Phases

The first release is phases 1–3 together (memory is in the first release);
each is its own PR.

0. **Fix the role bug now.** One history-to-context function used by all three
   turn paths: only `user` and `assistant` rows, internal markers stripped,
   no extra keys. Tests for dashboard ask and page voice on an agent with
   photo and transcript rows. Deploy on its own.
1. **Conversations.** Table, migration and backfill, store API, boundaries
   (idle gap, persona switch, manual), all turn paths writing to and reading
   from the current conversation, per-agent override columns and the resolver.
2. **Wrap-up and memory.** Sweeper, title+summary call, fallbacks and retry,
   remembered-conversations note, settings in the persona editor and agent page.
3. **Dashboard.** Conversations list and page, New conversation (dashboard and
   page agent), rename/delete/export, "what the model sees next", transcriber
   views moved over, JSON endpoints.

Then deploy 1–3 together, and announce the behaviour changes.

## Tests

- Boundaries: idle gap (just under / just over), persona switch, manual; per-agent
  override beats persona; resolver reports the source
- Migration: backfill splits by gap, transcript sessions preserved, fallback titles,
  no model calls
- Context: current conversation only; window respected; non-chat rows never sent;
  remembered note has the last K summaries, newest first, this agent only
- Wrap-up: one call gives title and summary; <2 turns skipped; failure falls back
  and retries once; manual titles kept; spend limit respected
- Sweeper ends idle conversations per their own gap
- Dashboard: list paging (no cap), dates in hub time zone, conversation page
  uncapped, rename/delete forgets the summary, export, New conversation, "what
  the model sees next" matches `context_messages`
- Harness scenario: two conversations separated by an idle gap; the second
  remembers the first through its summary

## Out of scope

- Per-user model access and costs (plan 3).
- Editing or pinning individual memories ("always remember I'm vegetarian").
  Summaries are generated; a curated memory list can come later.
- Searching across conversations.
