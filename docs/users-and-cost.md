# Design note: users, ownership, and who pays

Status: **step one built** (`server.dashboard_default_role`, this PR). Steps
two and three are proposed. Written after robot build night made the current
model creak in a way that patching would not fix.

## The problem

The hub has one identity concept and it is doing three jobs badly.

`DashboardOperator` is a Cloudflare Access identity with a role: admin,
operator, or viewer. That single ladder currently answers three unrelated
questions:

| Question | Answered by | Why it does not fit |
| --- | --- | --- |
| Who are you? | Access JWT → `DashboardOperator` | Works. This part is fine. |
| What may you do? | A global role | Roles are global; the thing people care about is per-object. "I may drive my robot" is not expressible. |
| Who pays for it? | Nothing | Every call runs on the hub's one OpenRouter key, capped globally. A busy room spends the hub owner's money with no per-person limit, attribution, or recourse. |

Build night made all three visible at once. Twelve builders each want a
collection of robots that is *theirs*, and each model call costs the person
running the hub. The patch available — promote everyone to operator — grants
everyone everything and still leaves one person paying.

## The pattern

**A workspace per person, plus bring-your-own-key.**

The second half is what makes the first half affordable. If each builder
supplies their own OpenRouter credentials, no money moves between people, so
the hub never needs accounts, invoices, or a payment processor. It needs to
know who someone is, which it already does, and which key to use, which is
one field.

Three principles:

1. **Cloudflare Access authenticates. It does not authorize.** It is the
   front door and the guest list. Everything past the door is the hub's
   business. Using the Access allowlist as the permission system is what
   forces the choice between "locked out" and "can do everything".
2. **Ownership is the unit of authorization, not a role.** Everyone in the
   room may *see* everything, because a room full of robots is the point.
   You may *drive* what you own. Staff may do anything. Three verbs, one
   sentence, teachable to a room in ten seconds.
3. **The person who starts a turn pays for it.** Not the robot's owner —
   otherwise driving someone else's robot spends their money, and a
   cooperative room turns into an argument.

## What each layer owns

### Cloudflare Access

- **Authentication.** The `email` claim is stable and always present; it is
  the identity. A one-time PIN policy lets a walk-in enter an email, receive
  a code, and be in, with no account anywhere and nothing to pre-create.
- **The guest list**, and nothing else.

Deliberately not used for authorization. Access can carry identity-provider
groups in the JWT, but only when the provider supports it and the claim is
configured, and Access trims custom claims near one kilobyte with groups
dropped first. That is too fragile to hang permissions on. Email is reliable.

Robots stay outside Access entirely. They register on the device port with
the enrollment token, which is the right mechanism for a headless client and
already works.

### The hub

- **A user**, keyed on the Access subject. `DashboardOperator` is already
  this; it is framed as a permission row rather than as somebody who owns
  things. Reframing is mostly relationships, not new tables.
- **Ownership**, already half-built: `Agent.owner_subject` records a verified
  claim, and `Agent.owner` is the display label. Making ownership *grant*
  driving rights is the remaining half.
- **A per-user OpenRouter key**, obtained through OpenRouter's OAuth PKCE
  flow rather than pasted. The user is redirected to OpenRouter, approves,
  and the hub receives a user-controlled key it can store. Their credit,
  their limits, revocable by them.
- **Key routing by initiator.** A dashboard turn uses the signed-in user's
  key. A device voice session has no signed-in user, so it falls back to the
  bot owner's key, then to the hub key.
- **Spend per person**, alongside the existing per-agent ledger.

### Nobody builds

Accounts with passwords, and billing. Access already does identity and
OpenRouter already does money. Owning either would turn a makerspace tool
into a product with a support burden.

## The free tier that removes the setup step

Free mode (`llm.free_only`) already restricts the model picker to free
OpenRouter models. Combined with the existing daily spend cap, that is a
zero-configuration tier: a builder who has connected nothing can still run a
robot on the hub's key, cheaply and boundedly. Connecting their own key is
what unlocks better models.

That single arrangement answers "who pays" for an event without any billing
code at all.

## Phasing

**Step one — built here.** `server.dashboard_default_role` decides what a
first-seen Access identity becomes. Set it to `operator` for an event where
everyone admitted is a builder; leave it `viewer` for a hub where the Access
policy is broader than the trust. Admin is refused as a default on purpose:
a mistake in a guest list should not hand out operator administration.

This is honestly a stopgap. It removes the promote-each-person step without
addressing ownership or cost.

**Step two — the workspace.** Ownership grants driving rights: you may drive
what you own, view everything, and staff may do anything. Add a "my bots"
home. No new dependencies, no schema beyond what PR #79 added.

**Step three — bring your own key.** OpenRouter OAuth, a per-user key, key
routing by initiator, and per-user spend. The hub key becomes the fallback
for anyone who has not connected one, held to the free tier.

**Never.** Passwords, invoices, payment processing.

## Consequences worth accepting deliberately

- **Storing user keys is a real security burden**, even keys obtained by
  OAuth and revocable by their owner. It wants encryption at rest and a
  written answer to "what happens when the database leaks".
- **Step two changes behaviour for existing hubs.** Today any operator can
  drive any device. Scoping to ownership means an unclaimed device is
  drivable by anyone until someone claims it, which is the sane migration,
  but it should be stated rather than discovered.
- **"View everything" is a deliberate choice**, not an oversight. Transcripts
  are conversations, and on a shared hub they are visible to everyone
  admitted. That is fine for a makerspace and wrong for a workplace. A hub
  that needs privacy between users needs step two extended to transcripts.

## References

- OpenRouter OAuth PKCE: https://openrouter.ai/docs/guides/overview/auth/oauth
- Cloudflare Access application token and claims:
  https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/
