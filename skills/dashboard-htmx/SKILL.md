---
name: dashboard-htmx
description: Use when adding or modifying Agent Hub dashboard routes, server-rendered HTML, HTMX interactions, operator authentication or authorization, dashboard security, or responsive styling under src/agent_hub/dashboard/.
---

# Dashboard HTMX

Read `src/agent_hub/dashboard/app.py` and the relevant dashboard tests before editing. Prefer extracting a sibling module instead of growing this already-large module when a feature has substantial independent logic.

## Invariants

- Keep server-rendered HTML and HTMX. Do not add a frontend build step or SPA framework.
- Escape all device-, model-, provider-, transcript-, and identity-supplied values before placing them in HTML.
- Dashboard auth is either verified Cloudflare Access identity, HTTP Basic auth, or an explicitly unprotected local/LAN setup. Cloudflare assertions are verified at the origin; proxy headers alone are not identity proof.
- Preserve same-origin checks for every state-changing method. HTMX requests inherit the page-level `X-Requested-With` header.
- Authorization is server-side: viewers are read-only, operators may run ordinary dashboard mutations, and admins alone manage human operators.
- Local and Basic-auth sessions remain administrators for backward compatibility unless the product explicitly changes that contract.
- Keep the dashboard on its separate port/trust boundary. Do not expose dashboard routes on device ports.
- Return small HTML fragments for HTMX mutations and complete pages through the shared page renderer.

Test authentication failures, role boundaries, CSRF/origin behavior, HTML escaping, and the successful HTMX path for new controls.
