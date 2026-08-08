from types import SimpleNamespace
import inspect

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner, TurnRunner


def test_discord_approval_notify_path_schedules_telegram_mirror_after_success():
    """Discord button approvals should schedule the Telegram mirror only after send success."""
    source = inspect.getsource(TurnRunner.run_sync)
    success_branch = source.split("if _approval_result.success:", 1)[1].split("return", 1)[0]
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
