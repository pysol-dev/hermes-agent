"""Spoken-brief voice middleware plugin.

Keeps the written assistant response intact while shortening/sanitizing the
text sent to TTS.  The plugin is opt-in via ``plugins.enabled`` and only affects
voice delivery paths that invoke the ``transform_tts_text`` hook.
"""

from __future__ import annotations

from .spoken_brief import transform_tts_text


def register(ctx):
    ctx.register_hook("transform_tts_text", transform_tts_text)
