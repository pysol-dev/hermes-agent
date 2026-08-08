from types import SimpleNamespace
import inspect

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner, TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


def test_discord_approval_notify_path_schedules_telegram_mirror_after_success():
    """Discord button approvals should schedule the Telegram mirror only after send success."""
    source = inspect.getsource(TurnRunner.run_sync)
    success_branch = source.split('if _outcome == "sent":', 1)[1].split("return", 1)[0]
    assert "ctx.source.platform == Platform.DISCORD" in success_branch
    assert "_mirror_discord_approval_to_telegram_home" in success_branch
    assert "session_key=_approval_session_key" in success_branch
    assert "source_chat_id=ctx.source.chat_id" in success_branch


class TelegramApprovalAdapter:
    typed_command_prefix = "/"

    def __init__(self):
        self.calls = []

    async def send_exec_approval(self, **kwargs):
        self.calls.append(kwargs)
        return SendResult(success=True, message_id="tg-approval")


class DiscordApprovalAdapter:
    typed_command_prefix = "/"

    def __init__(self):
        self.calls = []

    def pause_typing_for_chat(self, chat_id):
        self.paused_chat_id = chat_id

    async def send_exec_approval(self, **kwargs):
        self.calls.append(kwargs)
        return SendResult(success=True, message_id="discord-approval")


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


def test_discord_approval_notify_success_path_invokes_mirror(monkeypatch):
    """Exercise the registered approval callback, not just the helper directly."""
    import asyncio
    import gateway.run as run
    from tools import approval as approval_mod

    mirror_calls = []

    async def fake_mirror(**kwargs):
        mirror_calls.append(kwargs)
        return True

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.model = kwargs["model"]
            self.provider = "fake"
            self.tools = []
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.context_compressor = SimpleNamespace(last_prompt_tokens=0, context_length=0)

        def run_conversation(self, user_message, **kwargs):
            notify_cb = approval_mod._gateway_notify_cbs["agent:main:discord:dm:source-chat"]
            notify_cb({"command": "rm -rf /tmp/example", "description": "test approval"})
            return {
                "final_response": "ok",
                "messages": [],
                "api_calls": 1,
                "agent_persisted": True,
            }

    def run_coro_now(coro, loop, **kwargs):
        return _ImmediateFuture(asyncio.run(coro))

    status_adapter = DiscordApprovalAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._provider_routing = {}
    runner._prefill_messages = None
    runner._service_tier = None
    runner._session_db = None
    runner._agent_cache_lock = None
    runner._agent_cache = None
    runner._get_system_prompt_for_channel = lambda *args, **kwargs: ""
    runner._resolve_session_agent_runtime = lambda **kwargs: ("fake-model", {})
    runner._resolve_session_reasoning_config = lambda **kwargs: None
    runner._resolve_session_service_tier = lambda **kwargs: None
    runner._resolve_turn_agent_config = lambda message, model, runtime: {"model": model, "runtime": runtime}
    runner._agent_config_signature = lambda *args, **kwargs: "sig"
    runner._extract_cache_busting_config = lambda config: {}
    runner._refresh_fallback_model = lambda: None
    runner._consume_pending_turn_sidecar_notes = lambda session_key: []
    runner._consume_pending_native_image_paths = lambda session_key: []
    runner._sync_session_model_from_agent = lambda session_id, agent: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._is_discord_auto_thread_lane = lambda source: False
    runner._is_relay_discord_channel_lane = lambda source: False
    runner._mirror_discord_approval_to_telegram_home = fake_mirror

    monkeypatch.setattr(run, "safe_schedule_threadsafe", run_coro_now)

    ctx = TurnContext(
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="source-chat",
            user_id="user-1",
            user_name="User",
        ),
        _run_still_current=lambda: True,
        message="please do it",
        history=[],
        session_id="session-id",
        session_key="agent:main:discord:dm:source-chat",
        user_config={"gateway": {}, "display": {}},
        enabled_toolsets=[],
        disabled_toolsets=[],
        AIAgent=FakeAgent,
        resolve_display_setting=lambda *args, **kwargs: False,
        _loop_for_step=object(),
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
        _status_adapter=status_adapter,
        _status_chat_id="source-chat",
    )

    result = TurnRunner(runner, ctx).run_sync()

    assert result["final_response"] == "ok"
    assert len(status_adapter.calls) == 1
    assert mirror_calls == [
        {
            "approval_data": {
                "command": "rm -rf /tmp/example",
                "description": "test approval",
            },
            "session_key": "agent:main:discord:dm:source-chat",
            "source_chat_id": "source-chat",
        }
    ]


@pytest.mark.asyncio
async def test_discord_approval_mirror_sends_to_telegram_home_with_same_session_key():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = TelegramApprovalAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[assignment]
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="***",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="5945397802",
                    name="Home",
                ),
            )
        }
    )

    sent = await runner._mirror_discord_approval_to_telegram_home(
        approval_data={"command": "rm -rf /tmp/example", "description": "test approval"},
        session_key="discord:session:key",
        source_chat_id="1413024629500411911",
    )

    assert sent is True
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["chat_id"] == "5945397802"
    assert call["command"] == "rm -rf /tmp/example"
    assert call["session_key"] == "discord:session:key"
    assert "also remains in Discord" in call["description"]
    assert "1413024629500411911" in call["description"]


@pytest.mark.asyncio
async def test_discord_approval_mirror_noops_without_telegram_home():
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = TelegramApprovalAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}  # type: ignore[assignment]
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})

    sent = await runner._mirror_discord_approval_to_telegram_home(
        approval_data={"command": "ls"},
        session_key="discord:session:key",
        source_chat_id="123",
    )

    assert sent is False
    assert adapter.calls == []
