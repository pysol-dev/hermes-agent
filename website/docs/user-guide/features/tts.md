---
sidebar_position: 9
title: "Voice & TTS"
description: "Text-to-speech and voice message transcription across all platforms"
---

# Voice & TTS

Hermes Agent supports both text-to-speech output and voice message transcription across all messaging platforms.

## Text-to-Speech

Convert text to speech with seven providers:

| Provider | Quality | Cost | API Key |
|----------|---------|------|---------|
| **Edge TTS** (default) | Good | Free | None needed |
| **ElevenLabs** | Excellent | Paid | `ELEVENLABS_API_KEY` |
| **OpenAI TTS** | Good | Paid | `VOICE_TOOLS_OPENAI_KEY` |
| **MiniMax TTS** | Excellent | Paid | `MINIMAX_API_KEY` |
| **Mistral (Voxtral TTS)** | Excellent | Paid | `MISTRAL_API_KEY` |
| **NeuTTS** | Good | Free | None needed |
| **Local HTTP** | Depends on local engine | Free/local | Optional `LOCAL_TTS_API_KEY` |

### Platform Delivery

| Platform | Delivery | Format |
|----------|----------|--------|
| Telegram | Voice bubble (plays inline) | Opus `.ogg` |
| Discord | Voice bubble (Opus/OGG), falls back to file attachment | Opus/MP3 |
| WhatsApp | Audio file attachment | MP3 |
| CLI | Saved to `~/.hermes/audio_cache/` | MP3 |

### Configuration

```yaml
# In ~/.hermes/config.yaml
tts:
  provider: "edge"              # "edge" | "elevenlabs" | "openai" | "minimax" | "mistral" | "neutts" | "local_http"
  speed: 1.0                    # Global speed multiplier (provider-specific settings override this)
  edge:
    voice: "en-US-AriaNeural"   # 322 voices, 74 languages
    speed: 1.0                  # Converted to rate percentage (+/-%)
  elevenlabs:
    voice_id: "pNInz6obpgDQGcFmaJgB"  # Adam
    model_id: "eleven_multilingual_v2"
  openai:
    model: "gpt-4o-mini-tts"
    voice: "alloy"              # alloy, echo, fable, onyx, nova, shimmer
    base_url: "https://api.openai.com/v1"  # Override for OpenAI-compatible TTS endpoints
    speed: 1.0                  # 0.25 - 4.0
  minimax:
    model: "speech-2.8-hd"     # speech-2.8-hd (default), speech-2.8-turbo
    voice_id: "English_Graceful_Lady"  # See https://platform.minimax.io/faq/system-voice-id
    speed: 1                    # 0.5 - 2.0
    vol: 1                      # 0 - 10
    pitch: 0                    # -12 - 12
  mistral:
    model: "voxtral-mini-tts-2603"
    voice_id: "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # Paul - Neutral (default)
  neutts:
    ref_audio: ''
    ref_text: ''
    model: neuphonic/neutts-air-q4-gguf
    device: cpu
  local_http:
    base_url: "http://127.0.0.1:8020/v1"  # Uses POST <base_url>/audio/speech
    endpoint: ""                          # Optional full endpoint override
    model: "local-tts"
    voice: "default"
    timeout: 60
    api_key: ""                           # Optional; LOCAL_TTS_API_KEY also supported
    response_format: ""                   # Optional override: mp3, opus, wav, flac
    extra_body: {}                         # Engine-specific JSON fields
```

**Speed control**: The global `tts.speed` value applies to all providers by default. Each provider can override it with its own `speed` setting (e.g., `tts.openai.speed: 1.5`). Provider-specific speed takes precedence over the global value. Default is `1.0` (normal speed).

### Telegram Voice Bubbles & ffmpeg

Telegram voice bubbles require Opus/OGG audio format:

- **OpenAI, ElevenLabs, and Mistral** produce Opus natively — no extra setup
- **Edge TTS** (default) outputs MP3 and needs **ffmpeg** to convert:
- **MiniMax TTS** outputs MP3 and needs **ffmpeg** to convert for Telegram voice bubbles
- **NeuTTS** outputs WAV and also needs **ffmpeg** to convert for Telegram voice bubbles

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Fedora
sudo dnf install ffmpeg
```

Without ffmpeg, Edge TTS, MiniMax TTS, and NeuTTS audio are sent as regular audio files (playable, but shown as a rectangular player instead of a voice bubble). Local HTTP can produce Opus natively if your local server supports `response_format: opus`; otherwise use ffmpeg or MP3.

:::tip
If you want voice bubbles without installing ffmpeg, switch to the OpenAI, ElevenLabs, or Mistral provider.
:::

### Local HTTP TTS Provider

`local_http` lets Hermes delegate synthesis to any local or LAN service that exposes an OpenAI-compatible text-to-speech endpoint. This is useful for optional heavyweight engines such as Coqui/XTTS, Piper, VibeVoice, StyleTTS, or a custom voice-cloning stack without embedding those dependencies into the Hermes gateway process.

Hermes sends:

```http
POST /v1/audio/speech
Content-Type: application/json
Accept: audio/*
```

```json
{
  "model": "local-tts",
  "input": "Text to speak",
  "voice": "default",
  "response_format": "mp3"
}
```

The server should return raw audio bytes. Hermes chooses `response_format` from the output extension (`mp3`, `opus`, `wav`, or `flac`) unless `tts.local_http.response_format` is set. Any keys in `tts.local_http.extra_body` are merged into the request, so engine-specific options such as `language`, `speaker_wav`, or `temperature` can be passed without changing Hermes.

Example configuration for a local XTTS/Coqui wrapper:

```yaml
tts:
  provider: local_http
  local_http:
    base_url: "http://127.0.0.1:8020/v1"
    model: "xtts-v2"
    voice: "peach"
    timeout: 120
    extra_body:
      language: "en"
      speaker_wav: "/home/me/.hermes/voice_refs/peach/peach_ref.wav"
```

## Voice Message Transcription (STT)

Voice messages sent on Telegram, Discord, WhatsApp, Slack, or Signal are automatically transcribed and injected as text into the conversation. The agent sees the transcript as normal text.

| Provider | Quality | Cost | API Key |
|----------|---------|------|---------| 
| **Local Whisper** (default) | Good | Free | None needed |
| **Groq Whisper API** | Good–Best | Free tier | `GROQ_API_KEY` |
| **OpenAI Whisper API** | Good–Best | Paid | `VOICE_TOOLS_OPENAI_KEY` or `OPENAI_API_KEY` |

:::info Zero Config
Local transcription works out of the box when `faster-whisper` is installed. If that's unavailable, Hermes can also use a local `whisper` CLI from common install locations (like `/opt/homebrew/bin`) or a custom command via `HERMES_LOCAL_STT_COMMAND`.
:::

### Configuration

```yaml
# In ~/.hermes/config.yaml
stt:
  provider: "local"           # "local" | "groq" | "openai" | "mistral"
  local:
    model: "base"             # tiny, base, small, medium, large-v3
  openai:
    model: "whisper-1"        # whisper-1, gpt-4o-mini-transcribe, gpt-4o-transcribe
  mistral:
    model: "voxtral-mini-latest"  # voxtral-mini-latest, voxtral-mini-2602
```

### Provider Details

**Local (faster-whisper)** — Runs Whisper locally via [faster-whisper](https://github.com/SYSTRAN/faster-whisper). Uses CPU by default, GPU if available. Model sizes:

| Model | Size | Speed | Quality |
|-------|------|-------|---------|
| `tiny` | ~75 MB | Fastest | Basic |
| `base` | ~150 MB | Fast | Good (default) |
| `small` | ~500 MB | Medium | Better |
| `medium` | ~1.5 GB | Slower | Great |
| `large-v3` | ~3 GB | Slowest | Best |

**Groq API** — Requires `GROQ_API_KEY`. Good cloud fallback when you want a free hosted STT option.

**OpenAI API** — Accepts `VOICE_TOOLS_OPENAI_KEY` first and falls back to `OPENAI_API_KEY`. Supports `whisper-1`, `gpt-4o-mini-transcribe`, and `gpt-4o-transcribe`.

**Mistral API (Voxtral Transcribe)** — Requires `MISTRAL_API_KEY`. Uses Mistral's [Voxtral Transcribe](https://docs.mistral.ai/capabilities/audio/speech_to_text/) models. Supports 13 languages, speaker diarization, and word-level timestamps. Install with `pip install hermes-agent[mistral]`.

**Custom local CLI fallback** — Set `HERMES_LOCAL_STT_COMMAND` if you want Hermes to call a local transcription command directly. The command template supports `{input_path}`, `{output_dir}`, `{language}`, and `{model}` placeholders.

### Fallback Behavior

If your configured provider isn't available, Hermes automatically falls back:
- **Local faster-whisper unavailable** → Tries a local `whisper` CLI or `HERMES_LOCAL_STT_COMMAND` before cloud providers
- **Groq key not set** → Falls back to local transcription, then OpenAI
- **OpenAI key not set** → Falls back to local transcription, then Groq
- **Mistral key/SDK not set** → Skipped in auto-detect; falls through to next available provider
- **Nothing available** → Voice messages pass through with an accurate note to the user
