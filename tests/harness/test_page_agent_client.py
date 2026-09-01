"""Tests for the page-agent harness client — also its usage examples."""

from __future__ import annotations

import pytest

from agent_hub.registry.models import AgentKind
from agent_hub.registry.store import RegistryStore
from tests.harness import PageAgentClient, PageAgentError, ScriptedLLM, install_scripted_llm


async def test_register_creates_a_page_agent_with_a_token(store: RegistryStore) -> None:
    async with PageAgentClient.session(store) as page:
        await page.register(label="fixture page")

        assert page.device_id.startswith("page-")
        assert page.token
        agent = await store.get_agent(page.device_id)
        assert agent is not None
        assert agent.kind == AgentKind.PAGE.value
        assert agent.label == "fixture page"


async def test_ask_returns_the_reply_and_no_tool_calls(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_scripted_llm(monkeypatch, ScriptedLLM(reply="It is sunny."))
    async with PageAgentClient.session(store) as page:
        await page.register()

        turn = await page.ask("what's the weather?")

        assert turn.reply == "It is sunny."
        assert turn.tool_calls == []


async def test_ask_routes_a_page_tool_call_through_the_bridge(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = install_scripted_llm(
        monkeypatch,
        ScriptedLLM(tool_calls=[("get_screen", {"selector": "#alert"})], reply="Disk is at 92%."),
    )
    seen_args: dict[str, object] = {}

    def get_screen(args: dict[str, object]) -> str:
        seen_args.update(args)
        return "disk 92%"

    async with PageAgentClient.session(store) as page:
        page.add_tool("get_screen", "Return text on screen", get_screen)
        await page.register()

        turn = await page.ask("what's the disk usage?")

    assert turn.reply == "Disk is at 92%."
    assert turn.called("get_screen")
    assert turn.call_args("get_screen") == {"selector": "#alert"}
    assert seen_args == {"selector": "#alert"}
    # The handler's result flows back to the model through _exec_tool.
    assert llm.results == ["disk 92%"]
    assert turn.tool_calls[0].duration_s >= 0.0


async def test_async_tool_handlers_are_awaited(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_scripted_llm(monkeypatch, ScriptedLLM(tool_calls=[("slow_look", {})], reply="done"))

    async def slow_look(_args: dict[str, object]) -> str:
        return "looked"

    async with PageAgentClient.session(store) as page:
        page.add_tool("slow_look", "async look", slow_look)
        await page.register()

        turn = await page.ask("look around")

    assert turn.reply == "done"
    assert turn.called("slow_look")


async def test_a_raising_handler_surfaces_as_a_tool_error(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_scripted_llm(monkeypatch, ScriptedLLM(tool_calls=[("boom", {})], reply="unreached"))

    def boom(_args: dict[str, object]) -> str:
        raise ValueError("handler exploded")

    async with PageAgentClient.session(store) as page:
        page.add_tool("boom", "always fails", boom)
        await page.register()

        with pytest.raises(PageAgentError) as excinfo:
            await page.ask("trigger it")

    assert excinfo.value.status_code == 500
    assert "handler exploded" in str(excinfo.value)


async def test_server_skills_run_but_are_not_recorded_as_tool_calls(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_current_time is a real server skill: it executes in process, so the
    # bridge (and therefore Turn.tool_calls) never sees it.
    llm = install_scripted_llm(
        monkeypatch,
        ScriptedLLM(tool_calls=[("get_current_time", {})], reply="It's about 3pm."),
    )
    async with PageAgentClient.session(store) as page:
        await page.register()

        turn = await page.ask("what time is it?")

    assert turn.reply == "It's about 3pm."
    assert turn.tool_calls == []
    assert llm.results and "unknown tool" not in llm.results[0].lower()


async def test_history_persists_across_turns(
    store: RegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_scripted_llm(monkeypatch, ScriptedLLM(reply="noted"))
    async with PageAgentClient.session(store) as page:
        await page.register()

        await page.ask("first")
        await page.ask("second")

        history = await store.load_history(page.device_id)

    assert [row["role"] for row in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "first"
    assert history[2]["content"] == "second"


async def test_ask_before_register_is_an_error(store: RegistryStore) -> None:
    async with PageAgentClient.session(store) as page:
        with pytest.raises(PageAgentError):
            await page.ask("too early")
