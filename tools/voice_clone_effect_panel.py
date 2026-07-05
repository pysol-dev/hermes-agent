"""Interactive voice-clone effect panel helpers.

Local/private control surface for tuning the active local_http XTTS voice.
The gateway adapters render buttons; this module owns state, plain-language
labels, and active effect preset generation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
import json
import time

import yaml

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"


@dataclass(frozen=True)
class EffectControl:
    key: str
    label: str
    default: int
    minimum: int
    maximum: int
    definition: str


CONTROLS: Tuple[EffectControl, ...] = (
    EffectControl(
        "pitch", "Pitch-glitch", 4, 0, 10,
        "Digital pitch bending. Higher makes the voice wobble/shift in a glitch-core way without changing speaking speed.",
    ),
    EffectControl(
        "synth", "Synth color", 6, 0, 10,
        "Artificial electronic layer around the voice. Higher sounds less natural and more machine-like.",
    ),
    EffectControl(
        "crush", "Metal/crush", 5, 0, 10,
        "Ribbed metal / bit-crushed texture. Higher adds more robotic grit and physical crunch.",
    ),
    EffectControl(
        "echo", "Echo tail", 2, 0, 10,
        "Tiny after-sound after words. Higher feels more like a device or chamber, but too high smears words.",
    ),
    EffectControl(
        "chorus", "Voice split", 3, 0, 10,
        "Slight doubled-voice shimmer. Higher makes the voice wider and more synthetic, but can flutter.",
    ),
    EffectControl(
        "brightness", "Brightness", 4, 0, 10,
        "Sharpness and edge. Higher cuts through more; too high can sound hissy or close-mic.",
    ),
    EffectControl(
        "body", "Body", 6, 0, 10,
        "Warmth and chest/voice weight. Higher is fuller; lower is thinner and more radio-like.",
    ),
    EffectControl(
        "breath_guard", "Breath guard", 8, 0, 10,
        "Protection against breathy close-mic sound. Higher keeps lips-on-mic artifacts down.",
    ),
)

CONTROL_BY_KEY = {c.key: c for c in CONTROLS}


def _clamp(v: int, lo: int = 0, hi: int = 10) -> int:
    return max(lo, min(hi, int(v)))


def _hermes_config_path() -> Path:
    return get_hermes_home() / "config.yaml"


def current_local_http_voice() -> Tuple[str, Path]:
    cfg_path = _hermes_config_path()
    cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    tts = (cfg or {}).get("tts", {}) or {}
    local_http = (tts.get("local_http", {}) or {})
    extra = (local_http.get("extra_body", {}) or {})
    voice = str(local_http.get("voice") or "default")
    speaker = extra.get("speaker_wav")
    if speaker:
        speaker_path = Path(str(speaker)).expanduser()
        profile = speaker_path.parent if speaker_path.name == "multi" else speaker_path
    else:
        profile = get_hermes_home() / "voice_refs" / voice
    return voice, profile


def state_path(profile: Path | None = None) -> Path:
    if profile is None:
        _, profile = current_local_http_voice()
    return profile / "effects" / "panel_state.json"


def active_path(profile: Path | None = None) -> Path:
    if profile is None:
        _, profile = current_local_http_voice()
    return profile / "effects" / "active.json"


def default_state() -> Dict[str, int]:
    return {c.key: c.default for c in CONTROLS}


def load_state(profile: Path | None = None) -> Dict[str, int]:
    path = state_path(profile)
    state = default_state()
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            for c in CONTROLS:
                if c.key in raw:
                    state[c.key] = _clamp(raw[c.key], c.minimum, c.maximum)
        except Exception:
            pass
    return state


def save_state(state: Dict[str, int], profile: Path | None = None) -> None:
    path = state_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {c.key: _clamp(state.get(c.key, c.default), c.minimum, c.maximum) for c in CONTROLS}
    path.write_text(json.dumps(clean, indent=2) + "\n")


def _f(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


def build_ffmpeg_filter(state: Dict[str, int]) -> str:
    """Build a tempo-preserving Dr. Hayden-style effect chain.

    No atempo/asetrate/silence/gate filters are used. Sliders change color,
    pitch-glitch layering, crush, and tails only.
    """
    s = {c.key: _clamp(state.get(c.key, c.default), c.minimum, c.maximum) for c in CONTROLS}
    pitch = s["pitch"] / 10.0
    synth = s["synth"] / 10.0
    crush = s["crush"] / 10.0
    echo = s["echo"] / 10.0
    chorus = s["chorus"] / 10.0
    bright = s["brightness"] / 10.0
    body = s["body"] / 10.0
    guard = s["breath_guard"] / 10.0

    lowpass = 5600 + int(650 * (1 - guard)) + int(300 * bright)
    body_gain = -1.8 + (body * 2.2)
    synth_gain = 4.8 + synth * 3.2
    edge_gain = 1.2 + bright * 2.0 - guard * 1.2
    base_bits = max(8, int(round(12 - crush * 3)))
    post_bits = max(8, int(round(11 - crush * 3)))
    echo_in = 0.10 + echo * 0.14
    echo_out = 0.035 + echo * 0.055
    echo_delay = int(3 + echo * 4)
    # Keep tails short by design; user rejected smeared/repeated words.
    echo_decay = 0.006 + echo * 0.014
    comp_ratio = 2.0 + (1 - guard) * 1.0 + crush * 0.4
    makeup = 1.0 + synth * 1.0 + crush * 1.2

    up_pitch = 1.0 + 0.025 + pitch * 0.11
    down_pitch = 1.0 - (0.018 + pitch * 0.08)
    spark_pitch = 1.08 + pitch * 0.16
    up_vol = 0.08 + pitch * 0.22 + synth * 0.08
    down_vol = 0.05 + pitch * 0.17 + synth * 0.05
    spark_vol = 0.02 + pitch * 0.10
    flanger_depth = 0.8 + pitch * 2.6 + chorus * 0.8
    flanger_speed = 0.8 + pitch * 1.5
    vibrato_depth = 0.10 + pitch * 0.28
    vibrato_freq = 5.2 + pitch * 4.0
    tremolo_depth = 0.08 + pitch * 0.24
    tremolo_freq = 10 + int(pitch * 8)

    base = (
        f"highpass=f=115,lowpass=f={lowpass},"
        f"equalizer=f=420:t=q:w=1.0:g={_f(body_gain)},"
        f"equalizer=f=1500:t=q:w=0.9:g={_f(2.2 + body * 1.2)},"
        f"equalizer=f=2850:t=q:w=0.85:g={_f(synth_gain)},"
        f"equalizer=f=4200:t=q:w=1.1:g={_f(edge_gain)},"
        f"acrusher=level_in=1:level_out={_f(0.90 - crush * 0.08)}:bits={base_bits}:mode=log:aa=1"
    )
    up = (
        f"rubberband=pitch={_f(up_pitch)},highpass=f=600,lowpass=f=4500,volume={_f(up_vol)},"
        f"flanger=delay=2.0:depth={_f(flanger_depth)}:regen=1:width={int(25 + chorus * 35)}:speed={_f(flanger_speed)}:shape=triangular"
    )
    down = (
        f"rubberband=pitch={_f(down_pitch)},highpass=f=700,lowpass=f=3900,volume={_f(down_vol)},"
        f"vibrato=f={_f(vibrato_freq)}:d={_f(vibrato_depth)}"
    )
    spark = (
        f"rubberband=pitch={_f(spark_pitch)},highpass=f=1600,lowpass=f=5200,volume={_f(spark_vol)},"
        f"tremolo=f={tremolo_freq}:d={_f(tremolo_depth)}"
    )
    post = (
        f"highpass=f={int(215 + (1-body)*35)},lowpass=f={int(4050 + bright*250)},"
        f"equalizer=f=1750:t=q:w=0.72:g={_f(4.6 + synth * 2.6)},"
        f"aecho={_f(echo_in)}:{_f(echo_out)}:{echo_delay}:{_f(echo_decay)},"
        f"acompressor=threshold=-21dB:ratio={_f(comp_ratio)}:attack=8:release=78:makeup={_f(makeup)},"
        f"acrusher=level_in=1:level_out={_f(0.84 - crush*0.04)}:bits={post_bits}:mode=log:aa=1,"
        "alimiter=limit=0.87,loudnorm=I=-14.5:TP=-1.0:LRA=5"
    )
    return (
        "asplit=4[base][up][down][spark];"
        f"[base]{base}[basefx];"
        f"[up]{up}[upfx];"
        f"[down]{down}[downfx];"
        f"[spark]{spark}[sparkfx];"
        f"[basefx][upfx][downfx][sparkfx]amix=inputs=4:duration=first:normalize=0,{post}"
    )


def write_active_preset(state: Dict[str, int], profile: Path | None = None, voice: str | None = None) -> Path:
    if profile is None or voice is None:
        current_voice, current_profile = current_local_http_voice()
        voice = voice or current_voice
        profile = profile or current_profile
    path = active_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    preset = {
        "name": f"{voice}-interactive",
        "description": "Interactive voice-clone effect panel preset. Sliders preserve cadence: no tempo, silence, gate, or close-mic breathiness tricks.",
        "enabled": True,
        "panel_state": {c.key: _clamp(state.get(c.key, c.default), c.minimum, c.maximum) for c in CONTROLS},
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "ffmpeg_filter": build_ffmpeg_filter(state),
    }
    path.write_text(json.dumps(preset, indent=2) + "\n")
    return path


def reset_panel() -> Dict[str, int]:
    voice, profile = current_local_http_voice()
    state = default_state()
    save_state(state, profile)
    write_active_preset(state, profile, voice)
    return state


def adjust_control(control: str, delta: int) -> Dict[str, object]:
    if control not in CONTROL_BY_KEY:
        raise ValueError(f"Unknown voice effect: {control}")
    voice, profile = current_local_http_voice()
    state = load_state(profile)
    spec = CONTROL_BY_KEY[control]
    before = state.get(control, spec.default)
    after = _clamp(before + int(delta), spec.minimum, spec.maximum)
    state[control] = after
    save_state(state, profile)
    path = write_active_preset(state, profile, voice)
    return {"voice": voice, "profile": str(profile), "control": control, "before": before, "after": after, "active_path": str(path), "state": state}


def current_panel() -> Dict[str, object]:
    voice, profile = current_local_http_voice()
    state = load_state(profile)
    return {"voice": voice, "profile": str(profile), "state": state}


def render_panel_text() -> str:
    panel = current_panel()
    state: Dict[str, int] = panel["state"]  # type: ignore[assignment]
    lines: List[str] = [
        "🎛 Voice Clone Effects",
        f"Voice: {panel['voice']}",
        "Use + / − to adjust. Changes apply to the next TTS generation.",
        "",
    ]
    for c in CONTROLS:
        lines.append(f"{c.label}: {state.get(c.key, c.default)}/10")
        lines.append(f"  {c.definition}")
    lines.append("")
    lines.append("Reset restores the safe Dr. Hayden-3 baseline. No slider changes speech speed or inserts pauses.")
    return "\n".join(lines)


def controls_for_buttons() -> Iterable[EffectControl]:
    return CONTROLS


def button_labels(control: EffectControl, state: Dict[str, int] | None = None) -> Tuple[str, str]:
    """Return minus/plus button labels with the current 0-10 value embedded.

    Discord and Telegram render different component classes, but both should
    show the live setting so the panel is understandable without rereading the
    message body after every tap.
    """
    if state is None:
        state = load_state()
    current = _clamp(state.get(control.key, control.default), control.minimum, control.maximum)
    return (f"− {control.label} {current}/10", f"+ {control.label} {current}/10")
