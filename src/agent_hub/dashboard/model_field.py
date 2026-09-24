"""The persona editor's LLM model field.

For an OpenRouter persona this is a ``<select>`` of the models the hub can
use, split into free and paid, rather than a text box with suggestions: a
text box pre-filled with a model OpenRouter has dropped (a ``:free`` variant
withdrawn, say) filters its suggestions down to nothing, so it looks as if
the model cannot be changed. Other providers (a local Ollama, say) keep the
free-text box, since their model ids are not in any catalogue.
"""

from __future__ import annotations

import html
from typing import Any

FREE_SUFFIX = ":free"


def access_note(free_mode: bool, may_use_paid: bool) -> str:
    """One line saying whether this viewer may pick paid models (free mode only)."""
    if not free_mode:
        return ""
    if may_use_paid:
        return (
            ' <span class="badge badge-paid" title="Free mode is on for the hub, but you may '
            "pick paid models; they are billed to the hub's OpenRouter key.\">"
            "paid models allowed for you</span>"
        )
    return (
        ' <span class="badge badge-free" title="An admin can allow paid models for you on '
        'the Operators page.">free mode: free models only</span>'
    )


def paid_version(model_id: str, catalogue: list[dict[str, Any]]) -> str | None:
    """The paid id of a withdrawn ``:free`` model, if OpenRouter still lists it."""
    if not model_id.endswith(FREE_SUFFIX):
        return None
    paid = model_id.removesuffix(FREE_SUFFIX)
    return paid if any(m["id"] == paid for m in catalogue) else None


def _option(model: dict[str, Any], current: str) -> str:
    price = "" if model["free"] else f" · {model['price_in']}/M in"
    selected = " selected" if model["id"] == current else ""
    return (
        f'<option value="{html.escape(model["id"])}"{selected}>'
        f"{html.escape(model['name'] or model['id'])} — {html.escape(model['id'])}{price}"
        "</option>"
    )


def model_select(current: str, usable: list[dict[str, Any]], default_model: str) -> str:
    """A select of usable models, free then paid, keeping ``current`` even if it is gone.

    Args:
        current: The persona's model id ("" = the hub default).
        usable: Tool-capable catalogue entries this viewer may pick.
        default_model: The hub default, shown on the blank option.
    """
    known = {m["id"] for m in usable}
    blank = " selected" if not current else ""
    options = [
        f'<option value=""{blank}>Hub default ({html.escape(default_model or "not set")})</option>'
    ]
    if current and current not in known:
        # Kept so saving other fields does not change the model behind your back.
        options.append(
            f'<option value="{html.escape(current)}" selected>'
            f"{html.escape(current)} — no longer available, pick another</option>"
        )
    groups = (
        ("Free", sorted((m for m in usable if m["free"]), key=lambda m: m["id"])),
        (
            "Paid (price per million input tokens)",
            sorted((m for m in usable if not m["free"]), key=lambda m: m["id"]),
        ),
    )
    for label, models in groups:
        if models:
            options.append(
                f'<optgroup label="{label}">'
                + "".join(_option(m, current) for m in models)
                + "</optgroup>"
            )
    return (
        '<select name="llm_model" id="llm-model" style="max-width:100%">'
        + "".join(options)
        + "</select>"
    )


def model_text_input(current: str, usable: list[dict[str, Any]]) -> str:
    """The free-text model box with suggestions, for providers outside the catalogue."""
    datalist = "".join(
        f'<option value="{html.escape(m["id"])}">{html.escape(m["name"])}</option>' for m in usable
    )
    return (
        f'<input type="text" name="llm_model" value="{html.escape(current)}" list="llm-models" '
        'style="width:300px" placeholder="type to search tool-capable models">'
        f'<datalist id="llm-models">{datalist}</datalist>'
    )
