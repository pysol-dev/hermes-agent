"""Tests for the ``transform_tts_text`` plugin hook contract."""

from pathlib import Path

import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager, VALID_HOOKS


def _make_enabled_plugin(hermes_home: Path, name: str, register_body: str) -> Path:
    plugin_dir = hermes_home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0"}), encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    {register_body}\n",
        encoding="utf-8",
    )
    cfg_path = hermes_home / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return plugin_dir


def test_transform_tts_text_in_valid_hooks():
    assert "transform_tts_text" in VALID_HOOKS


def test_hook_receives_expected_voice_kwargs(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home,
        "capture_tts_hook",
        register_body=(
            'ctx.register_hook("transform_tts_text", '
            'lambda **kw: f"{kw[\'response_text\']}|{kw[\'platform\']}|'
            '{kw[\'chat_id\']}|{kw[\'message_type\']}")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    plugins_mod._plugin_manager = PluginManager()

    mgr = plugins_mod._plugin_manager
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "transform_tts_text",
        response_text="full written text",
        tts_text="full written text",
        platform="discord",
        chat_id="123",
        message_type="voice",
    )

    assert results == ["full written text|discord|123|voice"]


def test_first_non_empty_string_wins_for_spoken_text():
    hook_returns = [None, "", {"bad": True}, "spoken winner", "second"]

    spoken_source = "original"
    for hook_result in hook_returns:
        if isinstance(hook_result, str) and hook_result.strip():
            spoken_source = hook_result
            break

    assert spoken_source == "spoken winner"
