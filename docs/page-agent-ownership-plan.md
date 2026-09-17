# Plan: page agents are made by known users

Status: **proposed.** First of three fixes for the confusing multi-user
experience on the droplet. The other two, conversations as first-class
records and per-user model access, come after this one. Builds on
[users-and-cost.md](users-and-cost.md) step two (the workspace).

## Why this first

The agents list on a shared hub is hard to read mostly because of page
agents: rows nobody recognises, owned by nobody, that pile up and disappear
a day later. Conversations and per-user models both need to hang off
"whose agent is this", so ownership has to come first.

## What happens today

The hub already knows who makes a page agent. Until #86 it forgot straight
away. #86 now records the owner and blocks takeovers, but ids are still
random per tab.

| Step | Code | What happens |
| --- | --- | --- |
| Open the page | `GET /dashboard/page-agent` | Requires a verified Access identity with the operator role. |
| Register | `POST /page-agent/register` | Same auth. `request.state.operator_identity` is set. |
| Create the row | `store.get_or_create_agent(...)` then `claim_agent` | Since #86 the verified operator is recorded as owner. Rows made before #86 have no owner. |
| Identity of the agent | `_page_html.py`, `sessionStorage` | A random `page-xxxxxxxxxxxxxxxx` for each tab. A new tab or restarted browser makes a new agent. |
| Cleanup | `cleanup.StalePolicy.page_after` (24h) | Unseen page rows are pruned, and `store.delete_agent` deletes their conversation history with them. |

Consequences:

1. **No grouping by owner.** Since #86 new page agents are owned and show
   under "mine", but the agents list is still one flat table.
2. **No continuity.** Reopening "my kitchen assistant" tomorrow makes a
   stranger with no memory. The old row is pruned after 24h, and its history
   is deleted with it.
3. ~~**Any operator can take over a page agent.**~~ **Fixed in #86.**
   Registration trusted the `device_id` in the payload and re-issued that
   row's token, including for boards. It now reuses an id only for a page
   agent that is the caller's, or unowned and not live, and otherwise issues
   a fresh id.
4. **Without Access there is no "who".** On basic-auth or LAN hubs, the
   identity is `None` and every request runs as a shared admin.

## Target behaviour

- A page agent always has a verified owner: the Access subject that
  registered it.
- A page agent has a **name chosen by its owner**, and the name is what
  gives it a stable identity. Opening "kitchen" again, in any tab on any
  day, is the same agent with the same history.
- Two tabs can still run two agents side by side. They just need different
  names. That is the case the `sessionStorage` change was protecting.
- Only the owner (or an admin) can register, re-register or release a page
  agent. Viewing stays open to everyone, as it does for other agents.
- The agents list groups by owner. Your own agents come first and page
  agents are listed under their owner, not in a flat table.

## Design

### Identity

`device_id = "page-" + short_hash(owner_subject, name)`

- Deterministic, so reopening a name finds the same row and history.
- Scoped to the owner, so two people can both have a page agent called
  "kitchen" without clashing.
- The client no longer creates or stores ids. The page sends `name` (and
  optionally `persona`), and the server derives the id and returns it.
- The `label` shown in the dashboard is the name.

### Registration rules (`POST /page-agent/register`)

1. Require a verified identity. If `operator_identity` is `None`:
   - refuse with 403 and a clear message, **unless**
   - `server.page_agents_allow_anonymous: true` (default `false`), for LAN
     and dev hubs without Access. Those agents get owner label `local` and
     no `owner_subject`.
2. Ignore any client-supplied `device_id`. Derive it from subject + name.
   This replaces #86's `_resolve_device_id` checks.
3. On create, set `owner_subject` and `owner` (the email) in the same
   transaction. Extend `get_or_create_agent` with optional owner arguments,
   replacing #86's claim straight after creating.
4. If the row exists and `owner_subject` differs from the caller (possible
   only for legacy rows or a hash collision), refuse with 409. Admins get no
   override here. They should release the agent first so the action is
   deliberate and audited.
5. If the row exists and the agent is **currently connected**, refuse with
   409 "kitchen is already open in another tab" instead of silently taking
   over the token. Add a `takeover: true` flag for the owner, so a crashed
   tab isn't stuck until the heartbeat times out.

### Page UI (`_page_html.py`)

- A name field before connecting. Default it to the persona name, and list
  the owner's existing page agents to pick from.
- Remember only the **last used name** in `localStorage`, as a per-browser
  convenience. Identity comes from the server, not storage.

### Cleanup

Built in step 3a.

- **Named** page agents follow the **device** stale policy
  (`stale_device_days`), not the 24h page policy, and the hourly automatic
  sweep never removes them. They are meant to come back. "Named" is decided
  by `registry.page_identity.is_named_page_agent`: the row's id must be the
  named id for its owner and label.
- This includes named agents on a hub with `page_agents_allow_anonymous`
  (owner `local`). An earlier draft kept those on the 24h policy, but a named
  agent on a LAN hub is just as much meant to be reopened.
- Per-tab page agents (random ids) keep the 24h policy and the hourly sweep.
- Removing an agent from its dashboard page has a "keep conversation history"
  checkbox, on by default for named page agents. Kept history comes back when
  the name is reopened, since the id is the same. Before this, removing always
  deleted the history.

### Dashboard grouping

- Agents list: "Mine" section first, then one collapsible section per owner,
  then "Unowned". Within a section, sort by kind (boards, robots, pages).
- Agent detail: show the verified owner. Show Release (owner or admin) in
  place of Claim for page agents, since they're always owned.
- Persona page: a "used by" list of agents grouped by owner, so it's visible
  which page agents run a persona.

### Migration

Rows from before #86 have random ids and no owner. #86 lets the next operator
to register one adopt it while it isn't live, which covers a tab reload. No
further adoption:

- Leave the rest to the current 24h prune.
- ~~Add a one-off admin button, "Remove unowned page agents".~~ Dropped: once
  step 2 is deployed no page creates per-tab rows any more, so the hourly
  sweep clears the old ones within a day anyway.
- No schema change: `owner` and `owner_subject` already exist
  (`registry/models.py`).

## Out of scope here

- **Driving rights.** "Only the owner can *ask* this agent" is step two of
  users-and-cost.md and applies to every agent kind, not just pages. This
  plan builds on #86, which closed the page-registration takeover.
- **Conversations and memory** (plan 2) and **per-user model access**
  (plan 3). This plan gives them the stable owner and agent identity they
  need.

## Tests

In `tests/`, alongside the existing page-agent and authorization tests:

- register with an Access identity → row has `owner_subject`, id is stable
  across two registrations with the same name
- same name, different subject → different id
- register with a spoofed `device_id` in the payload → ignored (the #86
  tests for board and other-owner ids keep passing)
- register while connected → 409; with `takeover` by the owner → ok
- no identity, flag off → 403; flag on → owner label `local`
- stale sweep: owned page agent survives past 24h; anonymous one doesn't
- dashboard list: groups by owner, "mine" first

## Rollout

1. Server: identity derivation, owner on create, registration rules, flag.
2. Page UI: name field, existing-agent picker.
3. (a) Cleanup policy and keeping history. (b) Dashboard grouping.
4. Deploy to the droplet. Announce that open page tabs need to be reopened
   once, since their old random ids won't be adopted.

Each step is its own PR with gates passing.
