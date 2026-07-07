from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import time

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")}
    )
    setattr(runner, "adapters", {Platform.TELEGRAM: SimpleNamespace(send=AsyncMock())})
    setattr(runner, "session_store", MagicMock())
    setattr(runner, "pairing_store", MagicMock())
    setattr(
        runner,
        "hooks",
        SimpleNamespace(
            emit_collect=AsyncMock(return_value=[]),
            emit=AsyncMock(),
        ),
    )
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompt_pending = {}
    runner._queued_events = {}
    runner._session_model_overrides = {}
    runner._external_drain_active = False
    runner._draining = False
    runner._startup_restore_in_progress = False
    setattr(runner, "_scale_to_zero_note_real_inbound", lambda: None)
    setattr(runner, "_is_user_authorized", lambda source: True)
    setattr(runner, "_check_slash_access", lambda source, canonical_cmd: None)
    setattr(runner, "_session_key_for_source", lambda source: build_session_key(source))
    setattr(runner, "_is_telegram_topic_root_lobby", lambda source: False)
    setattr(runner, "_claim_active_session_slot", lambda session_key, source: (None, None))
    setattr(runner, "_begin_session_run_generation", lambda session_key: 1)
    setattr(runner, "_persist_active_agents", lambda: None)
    setattr(runner, "_restore_moa_one_shot", lambda event, quick_key: None)

    def _release_running_agent_state(session_key, *args, **kwargs):
        runner._running_agents.pop(session_key, None)
        runner._running_agents_ts.pop(session_key, None)
        return True

    setattr(runner, "_release_running_agent_state", _release_running_agent_state)
    return runner


@pytest.mark.asyncio
async def test_ask_strips_prefix_and_runs_as_foreground_turn():
    runner = _make_runner()
    seen = {}

    async def _capture(event, source, quick_key, run_generation):
        seen["text"] = event.text
        seen["source"] = source
        seen["quick_key"] = quick_key
        seen["run_generation"] = run_generation
        return "agent-ok"

    setattr(runner, "_handle_message_with_agent", _capture)

    result = await runner._handle_message(_make_event("/ask summarize HN"))

    assert result == "agent-ok"
    assert seen["text"] == "summarize HN"
    assert seen["quick_key"] == build_session_key(seen["source"])
    assert seen["run_generation"] == 1


@pytest.mark.asyncio
async def test_ask_bypasses_quick_command_named_ask_after_stripping_prefix():
    runner = _make_runner()
    runner.config.quick_commands = {
        "ask": {"type": "exec", "command": "printf quick-ask-intercepted"}
    }
    seen = {}

    async def _capture(event, source, quick_key, run_generation):
        seen["text"] = event.text
        seen["quick_key"] = quick_key
        seen["run_generation"] = run_generation
        return "agent-ok"

    setattr(runner, "_handle_message_with_agent", _capture)

    result = await runner._handle_message(_make_event("/ask summarize HN"))

    assert result == "agent-ok"
    assert seen["text"] == "summarize HN"
    assert seen["run_generation"] == 1


@pytest.mark.asyncio
async def test_empty_ask_returns_usage_without_agent_turn():
    runner = _make_runner()
    handler = AsyncMock(return_value="should-not-run")
    setattr(runner, "_handle_message_with_agent", handler)

    result = await runner._handle_message(_make_event("/ask"))

    assert result == "Usage: /ask <prompt>"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_while_agent_running_is_not_queued_or_backgrounded():
    runner = _make_runner()
    source = _make_event("/ask summarize HN").source
    session_key = build_session_key(source)
    running_agent = SimpleNamespace(
        get_activity_summary=lambda: {"seconds_since_activity": 0.0}
    )
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts[session_key] = time.time()
    runner._background_tasks = set()
    runner._queued_events = {}

    result = await runner._handle_message(_make_event("/ask summarize HN"))

    assert result is not None
    assert "Agent is running" in result
    assert runner._queued_events == {}
    assert runner._background_tasks == set()
