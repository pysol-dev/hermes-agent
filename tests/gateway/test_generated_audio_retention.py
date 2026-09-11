from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway.audio_retention import (
    DEFAULT_GENERATED_AUDIO_RETENTION_HOURS,
    build_generated_audio_path,
    finalize_generated_audio,
    prune_generated_audio,
    resolve_generated_audio_retention_hours,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.base import build_auto_tts_output_path
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_retention_defaults_to_24_hours_and_accepts_shorter_or_longer():
    assert DEFAULT_GENERATED_AUDIO_RETENTION_HOURS == 24.0
    assert resolve_generated_audio_retention_hours({}) == 24.0
    assert resolve_generated_audio_retention_hours(
        {"tts": {"generated_audio_retention_hours": 0.5}}
    ) == 0.5
    assert resolve_generated_audio_retention_hours(
        {"tts": {"generated_audio_retention_hours": 168}}
    ) == 168.0


@pytest.mark.parametrize("value", [-1, "forever", None, True, float("inf")])
def test_invalid_retention_values_fail_safe_to_default(value):
    assert resolve_generated_audio_retention_hours(
        {"tts": {"generated_audio_retention_hours": value}}
    ) == 24.0


def test_generated_audio_path_is_private_and_provider_neutral(tmp_path):
    path = build_generated_audio_path("mp3", hermes_home=tmp_path, now=1_800_000_000)
    assert path.parent == tmp_path / "audio" / "generated"
    assert path.name.startswith("tts_reply_1800000000_")
    assert path.suffix == ".mp3"
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_finalize_preserves_recent_audio_until_expiry(tmp_path):
    path = build_generated_audio_path("mp3", hermes_home=tmp_path, now=1_800_000_000)
    path.write_bytes(b"delivered audio")
    os.utime(path, (1_800_000_000, 1_800_000_000))

    finalize_generated_audio(
        [path], retention_hours=24, hermes_home=tmp_path, now=1_800_000_001
    )

    assert path.read_bytes() == b"delivered audio"
    assert path.stat().st_mode & 0o777 == 0o600


def test_prune_removes_only_expired_regular_audio(tmp_path):
    directory = tmp_path / "audio" / "generated"
    directory.mkdir(parents=True)
    old = directory / "tts_reply_old.mp3"
    recent = directory / "tts_reply_recent.wav"
    unrelated = directory / "notes.txt"
    target = tmp_path / "outside.mp3"
    symlink = directory / "tts_reply_link.mp3"
    for path in (old, recent, unrelated, target):
        path.write_bytes(path.name.encode())
    symlink.symlink_to(target)
    os.utime(old, (1_799_900_000, 1_799_900_000))
    os.utime(recent, (1_799_999_999, 1_799_999_999))

    removed = prune_generated_audio(
        retention_hours=24, hermes_home=tmp_path, now=1_800_000_000
    )

    assert removed == 1
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()
    assert symlink.is_symlink()
    assert target.exists()


def test_zero_retention_deletes_after_delivery(tmp_path):
    path = build_generated_audio_path("ogg", hermes_home=tmp_path, now=1_800_000_000)
    path.write_bytes(b"audio")

    finalize_generated_audio(
        [path], retention_hours=0, hermes_home=tmp_path, now=1_800_000_001
    )

    assert not path.exists()


def test_zero_retention_does_not_prune_another_inflight_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gateway.audio_retention.configured_generated_audio_retention_hours",
        lambda: 0.0,
    )
    monkeypatch.setattr(
        "gateway.audio_retention.prune_generated_audio",
        lambda **_kwargs: pytest.fail("allocation must not prune at zero retention"),
    )
    monkeypatch.setattr(
        "gateway.audio_retention.build_generated_audio_path",
        lambda _extension: tmp_path / "new-reply.mp3",
    )

    assert build_auto_tts_output_path(Platform.DISCORD) == str(tmp_path / "new-reply.mp3")


@pytest.mark.asyncio
async def test_gateway_voice_reply_keeps_exact_delivered_audio(monkeypatch, tmp_path):
    output = tmp_path / "tts_reply_integration.mp3"
    delivered = []

    def fake_tts(*, text, output_path):
        assert text == "Retain this reply."
        Path(output_path).write_bytes(b"exact delivered bytes")
        return json.dumps({"success": True, "file_path": output_path})

    async def send_voice(**kwargs):
        delivered.append(Path(kwargs["audio_path"]).read_bytes())

    runner = object.__new__(GatewayRunner)
    runner.adapters = cast(Any, {
        Platform.DISCORD: SimpleNamespace(
            send_voice=send_voice,
            is_in_voice_channel=lambda *_args: False,
        )
    })
    event = MessageEvent(
        text="trigger",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            user_id="456",
            user_name="tester",
        ),
        message_type=MessageType.TEXT,
        message_id="789",
    )
    monkeypatch.setattr("gateway.run.build_auto_tts_output_path", lambda _platform: str(output))
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", lambda text: text)
    monkeypatch.setattr(
        "gateway.audio_retention.configured_generated_audio_retention_hours",
        lambda: 24.0,
    )

    await runner._send_voice_reply(event, "Retain this reply.")

    assert delivered == [b"exact delivered bytes"]
    assert output.read_bytes() == b"exact delivered bytes"
    assert output.stat().st_mode & 0o777 == 0o600
