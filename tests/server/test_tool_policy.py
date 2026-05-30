"""Tests for device MCP tool permission policy (server/tool_policy.py).

Covers the spec from docs/lessons-learned.md, "Empty allowlists must not
disable default MCP capabilities":
  * None / [] allowlist → safe defaults (risky device tools excluded).
  * explicit non-empty allowlist → exactly those tools (admin/custom mode).
  * build-time filtering and exec-time checks agree.
"""

from __future__ import annotations

import pytest

from agent_hub.server import tool_policy

SAFE = [
    "self_camera_take_photo",
    "self_screen_set_brightness",
    "self_audio_speaker_set_volume",
    "self_get_device_status",
]
RISKY = [
    "self_system_reboot",
    "self_ota_firmware_update",
    "self_wifi_set_config",
    "self_fs_write_file",
    "self_delete_file",
    "self_shell_exec",
    "self_factory_reset",
    "self_set_config",
]


class TestIsRiskyTool:
    @pytest.mark.parametrize("name", RISKY)
    def test_risky_names_flagged(self, name):
        assert tool_policy.is_risky_tool(name) is True

    @pytest.mark.parametrize("name", SAFE)
    def test_safe_names_not_flagged(self, name):
        assert tool_policy.is_risky_tool(name) is False

    def test_case_insensitive(self):
        assert tool_policy.is_risky_tool("Self_REBOOT_now") is True


class TestAllowedDeviceTools:
    def test_none_allowlist_keeps_safe_drops_risky(self):
        result = tool_policy.allowed_device_tools(SAFE + RISKY, None)
        assert result == SAFE

    def test_empty_allowlist_behaves_like_none(self):
        # The core regression: [] must NOT disable everything.
        assert tool_policy.allowed_device_tools(SAFE + RISKY, []) == SAFE

    def test_explicit_allowlist_is_exact_including_risky(self):
        allow = ["self_camera_take_photo", "self_system_reboot"]
        result = tool_policy.allowed_device_tools(SAFE + RISKY, allow)
        assert set(result) == set(allow)

    def test_explicit_allowlist_excludes_unlisted_safe_tools(self):
        result = tool_policy.allowed_device_tools(SAFE, ["self_camera_take_photo"])
        assert result == ["self_camera_take_photo"]

    def test_order_preserved(self):
        names = ["self_get_device_status", "self_camera_take_photo"]
        assert tool_policy.allowed_device_tools(names, None) == names


class TestIsToolAllowed:
    def test_safe_tool_allowed_by_default(self):
        assert tool_policy.is_tool_allowed("self_camera_take_photo", None) is True
        assert tool_policy.is_tool_allowed("self_camera_take_photo", []) is True

    def test_risky_tool_blocked_by_default(self):
        assert tool_policy.is_tool_allowed("self_system_reboot", None) is False
        assert tool_policy.is_tool_allowed("self_system_reboot", []) is False

    def test_explicit_allowlist_enables_risky(self):
        assert tool_policy.is_tool_allowed("self_system_reboot", ["self_system_reboot"]) is True

    def test_explicit_allowlist_blocks_unlisted(self):
        assert (
            tool_policy.is_tool_allowed("self_camera_take_photo", ["self_system_reboot"]) is False
        )

    @pytest.mark.parametrize("name", SAFE + RISKY)
    def test_exec_agrees_with_build_filter(self, name):
        """is_tool_allowed must match allowed_device_tools for every tool."""
        for allowlist in (None, [], ["self_camera_take_photo", "self_system_reboot"]):
            build = name in tool_policy.allowed_device_tools(SAFE + RISKY, allowlist)
            assert tool_policy.is_tool_allowed(name, allowlist) is build
