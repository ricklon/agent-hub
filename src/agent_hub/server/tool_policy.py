"""Device MCP tool permission policy.

Decides which discovered device (microcontroller) tools a persona may expose to
the LLM, and which it may actually execute.

Semantics of a persona's decoded ``mcp_tools_allowlist`` (the value returned by
``Persona.mcp_tools_allowlist_list``):

  * ``None`` — no explicit allowlist. Use the **safe default set**: every
    discovered tool *except* risky device-management tools.
  * ``[]``   — treated identically to ``None``. A blank, ``NULL``, or legacy
    empty list must **never** silently disable device tools (see
    docs/lessons-learned.md, "Empty allowlists must not disable default MCP
    capabilities").
  * ``[...]`` — an explicit admin/custom allowlist. Exactly the named tools are
    allowed, including any risky ones the admin deliberately enabled. This is
    the only way risky device-management tools become reachable.

Risky tools are device-management capabilities that can brick, reconfigure, or
take over a board: reboot/reset, firmware/OTA updates, Wi-Fi/network
configuration, filesystem writes/deletes, arbitrary command execution, and
persistent config mutation. Safe, discovered capabilities (camera/photo/image,
screen brightness, volume, status reads, etc.) are exposed by default.

The same policy is enforced in two places (per the spec):
  1. when building the tool list handed to the LLM, and
  2. when executing a tool the LLM requested.
"""

from __future__ import annotations

# Substrings (matched case-insensitively against the sanitized tool name) that
# mark a tool as a risky device-management capability. Kept deliberately broad:
# the cost of over-blocking is that an admin must explicitly allowlist the tool,
# whereas the cost of under-blocking is a device that can be remotely bricked or
# reconfigured by a model hallucination.
RISKY_KEYWORDS: tuple[str, ...] = (
    # reboot / reset / power
    "reboot",
    "reset",
    "restart",
    "shutdown",
    "poweroff",
    "power_off",
    # firmware / OTA / update
    "firmware",
    "ota",
    "upgrade",
    "flash",
    "self_update",
    "selfupdate",
    # Wi-Fi / network configuration
    "wifi",
    "ssid",
    "provision",
    "set_network",
    "network_config",
    "config_network",
    "set_ssid",
    # filesystem writes / deletes
    "delete",
    "erase",
    "format",
    "unlink",
    "rmdir",
    "remove_file",
    "write_file",
    "writefile",
    "file_write",
    "fs_write",
    "filesystem",
    # arbitrary command execution
    "exec",
    "shell",
    "run_command",
    "system_command",
    "eval",
    # persistent config mutation
    "factory",
    "set_config",
    "config_set",
    "write_config",
    "save_config",
)


def is_risky_tool(name: str) -> bool:
    """True if ``name`` looks like a risky device-management tool."""
    low = name.lower()
    return any(keyword in low for keyword in RISKY_KEYWORDS)


def allowed_device_tools(
    tool_names: list[str],
    allowlist: list[str] | None,
) -> list[str]:
    """Filter discovered device tool names to those a persona may use.

    ``allowlist`` is the persona's decoded ``mcp_tools_allowlist_list``.
    See the module docstring for the None / [] / [...] semantics. Order of
    ``tool_names`` is preserved.
    """
    if allowlist:  # explicit, non-empty → admin/custom mode: exactly these
        permitted = set(allowlist)
        return [n for n in tool_names if n in permitted]
    # None or [] → safe default: every non-risky discovered tool
    return [n for n in tool_names if not is_risky_tool(n)]


def is_tool_allowed(name: str, allowlist: list[str] | None) -> bool:
    """True if a persona may execute the device tool ``name``.

    Mirrors :func:`allowed_device_tools` for a single tool so execution-time
    enforcement cannot diverge from build-time filtering.
    """
    if allowlist:  # explicit, non-empty → admin/custom mode
        return name in set(allowlist)
    return not is_risky_tool(name)
