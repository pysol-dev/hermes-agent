from types import SimpleNamespace
import inspect

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner


def test_gateway_approval_notify_path_speaks_prompt_before_returning():
    """Dangerous-command approvals should be surfaced in Discord VC too."""
    source = inspect.getsource(GatewayRunner._run_agent_inner)
    assert "def _speak_approval_prompt" in source
    assert "build_permission_prompt" in source
    assert "context=\"approval prompt\"" in source
    success_branch = source.split("if _approval_result.success:", 1)[1].split("return", 1)[0]
    assert "_speak_approval_prompt()" in success_branch


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
