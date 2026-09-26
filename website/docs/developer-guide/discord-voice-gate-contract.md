# Discord Voice Address Gate Contract

Status: implementation contract for the Discord VC receive path

Scope: completed Discord VC utterances after STT, before gateway ingress
Normative terms: **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are binding as used in RFC 2119.

## Purpose

An always-listening Discord bot hears speech that was not intended for it. Audio authorization and Whisper hallucination filtering are necessary, but they do not establish that an allowed speaker addressed Hermes. The voice address gate is a local, deterministic admission boundary: rejected speech stops before it can become a gateway message or an LLM turn.

This gate is not a model classifier and MUST NOT invoke an LLM, embedding model, remote service, or tool. It decides from bounded local configuration, transcript tokens, voice identity/context, monotonic time, and explicit lifecycle state.

## Observed Paths

The following snapshots were inspected on 2026-09-07 after fetching `origin/main`.

### Deployed checkout

The running gateway reports code SHA `c701ce0b2b577693f7d5b8022ba730e8c3f02f77` (`brodex/live`, version `0.20.5`). Discord is connected from this checkout.

The ingress path is:

1. `plugins/platforms/discord/adapter.py::VoiceReceiver.check_silence()` emits `(user_id, pcm_data)` after 1.5 seconds of silence and at least 0.5 seconds of buffered speech.
2. `DiscordAdapter._voice_listen_loop()` applies the Discord allowlist/role gate and calls `_process_voice_input(guild_id, user_id, pcm_data)`.
3. `DiscordAdapter._process_voice_input()` converts PCM to WAV, calls `transcribe_audio()`, strips surrounding whitespace, and rejects empty output or `is_whisper_hallucination()` output.
4. It currently logs a transcript excerpt and invokes `_voice_input_callback(guild_id=..., user_id=..., transcript=...)` for every other transcript.
5. `gateway/run.py::GatewayRunner._handle_voice_channel_input()` resolves the linked text channel and source, repeats authorization, deduplicates, echoes the transcript to Discord, constructs a `MessageEvent(message_type=VOICE)`, and calls `adapter.handle_message(event)`.
6. `BasePlatformAdapter.handle_message()` derives the session key and starts background processing. Its active-session behavior happens after the synthetic event exists and can queue, steer, redirect, or interrupt according to gateway policy; it is too late to be the voice safety boundary.

### Latest upstream

Latest fetched upstream is `origin/main` at `08b140d14e6c1d49f9b7ad02c9437fe940d54d65`.

The adapter path remains semantically the same: `DiscordAdapter._process_voice_input()` sends every nonempty, non-hallucination transcript to `_voice_input_callback`. Upstream removed the adapter's pre-callback transcript log, but it did not add address admission.

Gateway voice methods moved from the large runner module to `gateway/run_voice.py::GatewayVoiceMixin`. `_handle_voice_channel_input()` still authorizes, deduplicates, echoes the transcript, constructs a `MessageEvent`, and calls `adapter.handle_message(event)`. `_bind_voice_input_callback()` binds the capturing adapter for multiplexed profiles. There is no wake-name, follow-up, or pre-message busy gate in latest upstream.

## Exact Insertion Point

The mandatory gate call belongs in `DiscordAdapter._process_voice_input()` immediately after these existing STT filters:

```text
transcript = result.get("transcript", "").strip()
if not transcript or is_whisper_hallucination(transcript):
    return
```

and before every one of the following:

- any log containing the transcript or content derived from it;
- `_voice_input_callback(...)`;
- duplicate tracking outside the gate;
- Discord transcript echo (`channel.send(...)`);
- `SessionSource` or `MessageEvent` construction;
- `BasePlatformAdapter.handle_message()`;
- session/history persistence or prompt construction.

The reusable parser/state machine SHOULD live in a focused module such as `gateway/discord_voice_gate.py`; the adapter owns the call because the adapter is the last point at which rejection can guarantee that no callback or message object exists. The runner MUST provide profile-aware context and busy/lifecycle callbacks; putting the first gate only in `_handle_voice_channel_input()` is noncompliant because rejected transcript text has already crossed the callback boundary.

Authorization remains before or around the gate as it is today. Authorization does not replace addressing.

## Result Type

A gate evaluation returns a closed result object, not a bare string or boolean:

```text
VoiceGateResult
  accepted: bool
  reason: VoiceGateReason
  command_text: str | None
  admission: "addressed" | "follow_up" | None
  lease_id: opaque str | None
```

For rejection, `command_text` and `lease_id` MUST be `None`. Callers MUST switch on the reason enum rather than parsing human text.

For acceptance, `command_text` is the post-address command only. The noisy preamble and wake alias MUST NOT be sent to the LLM. `lease_id` identifies the atomic voice-turn reservation described below.

## Stable Reason Codes

Reason-code spelling is API. New codes may be added, but these meanings MUST NOT be repurposed.

Accepted:

| Code | Meaning |
|---|---|
| `accepted_addressed` | A canonical alias was found in the allowed address position and informative content remained. |
| `accepted_follow_up` | An explicitly armed, unexpired same-owner/same-context window admitted an unaddressed command. |

Rejected:

| Code | Meaning |
|---|---|
| `rejected_context_unbound` | Guild, connected voice channel, or linked text channel identity is unavailable. |
| `rejected_state_unavailable` | Required gate/busy state is missing or raised; fail closed. |
| `rejected_busy` | The same voice/text session already has a reserved, pending, or active turn. |
| `rejected_wake_missing` | No canonical alias appears as the first non-preamble token sequence. This includes aliases embedded inside larger words. |
| `rejected_preamble` | An alias exists but is preceded by too many tokens or by a token outside the permitted preamble grammar. |
| `rejected_content` | Removing preamble/address/courtesy noise leaves no informative command. |
| `rejected_follow_up_disabled` | Unaddressed input arrived while follow-up support is disabled. |
| `rejected_follow_up_unarmed` | Follow-up support is enabled but this context has no explicit arm. |
| `rejected_follow_up_expired` | The matching arm reached its monotonic deadline. |
| `rejected_follow_up_user` | An arm exists for this context, but belongs to another Discord user. |
| `rejected_follow_up_context` | The user has an arm, but not for this profile/guild/voice/text context. |
| `rejected_duplicate` | The same accepted command was already admitted within the bounded duplicate window. |

Reason precedence is deterministic:

1. context/state availability;
2. busy/reservation state;
3. addressed parse and content validation;
4. duplicate check for an addressed command;
5. follow-up enablement, ownership, context, expiry, content, and duplicate check.

For an ordinary unaddressed transcript with follow-up disabled, return `rejected_follow_up_disabled`. With follow-up enabled but no arm, return `rejected_follow_up_unarmed`. `rejected_wake_missing` is reserved for strict addressed parsing when no follow-up path is being considered (including parser unit tests). This distinction makes configuration/lifecycle failures observable without logging speech.

## Voice Context Identity

All mutable gate state MUST be scoped by the capturing adapter and exact voice binding:

```text
VoiceContextKey = (
  owner_profile,      # "default" or the multiplexed adapter owner
  guild_id,
  voice_channel_id,
  text_channel_id,
)
```

A guild ID alone is insufficient: the bot may move between voice channels while retaining a text binding. A text channel alone is insufficient under multiple guilds or profiles. `user_id` is the owner field on a follow-up arm, not part of the shared context key.

The adapter obtains `voice_channel_id` from its connected voice client for `guild_id`. Missing or disconnected voice state rejects with `rejected_context_unbound`.

## Normalization and Token Boundaries

Normalization is for matching only. It MUST NOT rewrite accepted command semantics.

1. Apply Unicode NFKC and `casefold()`.
2. Normalize curly apostrophes and Unicode dash variants to separators.
3. Tokenize only ASCII letters and digits as `[a-z0-9]+`; all other characters are separators.
4. Collapse separator runs for token matching, while retaining source spans so the accepted command can be sliced from the original transcript after the alias.
5. Normalize configured aliases with the same function at startup.
6. Match exact token sequences only. Do not use substring search, edit distance, phonetic matching, or an LLM.

Consequences:

- `Sifrot, status` matches `sifrot`.
- `ZIP ROT, status` matches the two-token alias `zip rot`.
- `sifrotic status`, `cypherotic status`, `fitfrightening`, and `broccoli` do not match any alias.
- Punctuation adjacent to an alias establishes a boundary; letters/digits do not.

After finding the alias, slice the raw transcript immediately after its source span, discard only leading separators and configured courtesy tokens, then normalize whitespace. Do not lowercase or otherwise rewrite the command sent onward.

## Canonical Alias Set

The built-in alias set is closed and version-controlled. These are the approved STT forms; downstream code MUST test each normalized token sequence.

| Canonical family | Accepted token sequences |
|---|---|
| Sifrot | `sifrot`, `sifrod`, `sifra`, `cifra`, `defra`, `frott`, `fit fright`, `fitfright`, `sif front`, `steve brod`, `sit` |
| Cypherot | `cypherot`, `cephrot`, `cipri`, `ziprot`, `zip rot` |

Configuration MAY add exact aliases through `extra_aliases`, but MUST NOT silently remove the built-ins. Empty aliases, aliases containing no ASCII alphanumeric token, duplicates after normalization, and aliases longer than three tokens MUST be rejected during configuration load.

The short/common alias `sit` is intentionally retained because it is an approved STT form. Its risk is bounded by the address-position and informative-content rules; it still MUST NOT match inside a larger token. Operators that need to remove a built-in alias require a future explicit deny-list design, not an undocumented override of this contract.

## Permitted Noisy Preambles

The alias MUST be the first token sequence after a bounded preamble. The default permits at most three preamble tokens and 32 source characters before the alias. Every preamble token MUST match one of these local forms:

- address cues: `hey`, `hi`, `hello`, `yo`;
- hesitation/filler: `uh`, `um`, `erm`, `hmm`, `okay`, `ok`, `well`;
- acoustic elongations matching `^(?:a+h+|u+h+|u+m+|h+m+|w+o+a+h+|w+h+a+)$`.

Punctuation does not count as a token. Multiple permitted tokens are allowed within both limits, for example `uh, okay, Sifrot ...`. Arbitrary lexical clauses are not permitted preambles even when short. This rejects `Nate said Sifrot was here` rather than treating a mention as an address.

A future configuration may extend the preamble token allowlist with exact tokens. It MUST retain the token/character bounds and MUST NOT accept an unrestricted wildcard or arbitrary leading text.

## Informative Content

Addressing alone is not a command. After stripping the preamble, alias, leading separators, and courtesy-only prefixes, the remaining text MUST contain informative content.

Courtesy/noise tokens are:

```text
a, an, the, hey, hi, hello, uh, um, erm, hmm, oh, okay, ok,
please, thanks, thank, you
```

A command is informative when either:

- at least one remaining token is not in the courtesy/noise set and has at least four ASCII alphanumeric characters; or
- it contains at least two tokens outside that set; or
- its sole non-noise token is one of the explicit short controls/questions:
  `go`, `no`, `why`, `how`, `who`, `stop`, `cancel`, `pause`, `resume`, `repeat`, `help`, `status`.

The same test applies to addressed and follow-up input. In particular, a follow-up arm MUST NOT admit `oh`, `uh`, `okay`, repeated thanks, or punctuation-only fragments.

Examples:

- `Sifrot` -> `rejected_content`.
- `Sifrot, uh...` -> `rejected_content`.
- `Sifrot, status` -> accepted command `status`.
- `Cypherot, why?` -> accepted command `why?`.
- `Zip rot, check the gateway` -> accepted command `check the gateway`.

## Follow-up Window

Follow-up is off by default. Enabling it only makes the mechanism available; it does not create an arm.

An arm is an explicit state transition:

```text
arm_follow_up(context_key, user_id, source_lease_id, now_monotonic)
```

The first implementation MUST arm only after the actual gateway turn for an `accepted_addressed` lease completes successfully. It MUST use the real turn-completion boundary, not the return from `adapter.handle_message()`, because that method intentionally spawns background processing and returns quickly. The accepted `MessageEvent` SHOULD carry an opaque lease ID in metadata so the turn's completion/cancellation path can settle the right reservation without transcript content.

A successful `/voice join` MUST NOT arm follow-up merely because the bot connected. A later feature may provide a separate explicit join-time arm, but it must be independently configured and tested. Voice activity, TTS playback, typed text, rejected speech, duplicate speech, and accepted follow-ups MUST NOT arm or extend the window.

An arm stores only:

```text
FollowUpArm
  context_key
  user_id
  armed_at_monotonic
  expires_at_monotonic
  source_lease_id
```

Rules:

- Expiry uses `time.monotonic()`, never wall time.
- Default window: 12 seconds. Hard clamp: 1-30 seconds.
- `now >= expires_at` is expired.
- Only the exact `user_id` and `VoiceContextKey` may consume the arm.
- An accepted follow-up does not refresh, extend, or replace the arm.
- A fresh successfully completed addressed turn replaces the prior arm for that same context.
- An expired arm is deleted before returning `rejected_follow_up_expired`.
- A different user cannot steal, consume, or invalidate the owner's arm.
- Addressed input does not need an arm; it still obeys busy and content checks.

## Busy-Turn and Atomic Reservation Contract

Checking `_running_agents` only after callback dispatch leaves a race: `BasePlatformAdapter.handle_message()` creates its active guard only after a synthetic event exists, and it returns after scheduling background work. Two completed STT tasks can therefore both pass a check-before-dispatch unless the gate owns an atomic reservation.

Each `VoiceContextKey` MUST have a dedicated lock and at most one active reservation. Under that lock, evaluation MUST:

1. reject if the gate already has an unsettled lease for the context;
2. call a runner-provided, profile-aware busy predicate for the exact bound text session;
3. fail closed if that predicate is missing or raises;
4. perform admission/content/duplicate checks;
5. create and store an opaque lease before releasing the lock or invoking `_voice_input_callback`.

The busy predicate MUST treat runner pending sentinels, live agents, adapter active-session guards, and an existing gate lease as busy. It MUST derive the real profile-aware `SessionSource`/session key; brittle string-position scans across `_running_agents` are not the new contract.

The accepted lease remains active until the real gateway turn settles. Completion clears it and, only for a successful addressed turn with follow-up enabled, arms follow-up. Failure, cancellation, interrupt, timeout, or dispatch error clears it without arming.

A bounded reservation watchdog (default 15 minutes) MAY clear a leaked lease, but only after confirming that neither runner nor adapter reports the session active. It must emit `rejected_state_unavailable`/state telemetry rather than allowing a possibly concurrent turn. Ordinary follow-up expiry is not a lease timeout.

Busy rejection has priority over wake matching. While a turn is active, even a correctly addressed transcript is dropped with `rejected_busy`; it is not queued, steered, redirected, merged, or used to interrupt the active agent. The user can use normal text control commands for intentional interruption.

## Privacy and Side-Effect Boundary

For every rejected result, all of the following are mandatory:

- `_voice_input_callback` call count remains zero;
- no `SessionSource`, `MessageEvent`, or pending adapter message is created;
- no Discord transcript echo is sent;
- no session database/history/transcript row is written;
- no prompt, memory-provider input, tool input, or LLM request contains the text;
- no hook receives the text;
- no log record contains raw text, normalized text, a text excerpt, token list, content length, or a transcript hash.

Rejection telemetry may contain only:

- the stable reason code;
- owner profile;
- guild ID, voice channel ID, linked text channel ID, and Discord user ID (subject to existing PII-redaction policy);
- an implicit log timestamp or counter increment.

Example compliant log:

```text
Discord voice gate rejected reason=rejected_busy profile=default guild=... voice_channel=... text_channel=... user=...
```

The current deployed `logger.info("Voice input from user %d: %s", ..., transcript[:100])` MUST be removed or moved after acceptance. The runner's transcript-bearing duplicate log MUST not be reachable for rejected speech; duplicate suppression should move into the pre-callback gate or its log must become metadata-only.

Accepted command text may enter the normal callback/message/session path. The original preamble and wake alias SHOULD NOT be echoed or persisted; the cleaned `command_text` is the user utterance of record.

Temporary WAV cleanup remains mandatory on every decision path.

## Configuration

Use non-secret `config.yaml`; do not add a user-facing environment variable. The additive configuration is:

```yaml
discord:
  voice_listen:
    mode: strict                    # strict | off; strict is the default
    extra_aliases: []               # exact aliases added to built-ins
    preamble_max_tokens: 3
    preamble_max_characters: 32
    follow_up:
      enabled: false                # opt-in; availability is not an arm
      window_seconds: 12            # clamped to 1..30
    reservation_timeout_seconds: 900
```

Normative defaults:

| Setting | Default | Validation |
|---|---:|---|
| `mode` | `strict` | Unknown values fail closed to strict and warn without transcript content. |
| `extra_aliases` | `[]` | Exact list; each normalized alias is 1-3 nonempty tokens. |
| `preamble_max_tokens` | `3` | Integer, clamped to 0-5. |
| `preamble_max_characters` | `32` | Integer, clamped to 0-64. |
| `follow_up.enabled` | `false` | Boolean only; malformed values fail closed to false. |
| `follow_up.window_seconds` | `12` | Numeric, clamped to 1-30. |
| `reservation_timeout_seconds` | `900` | Numeric, clamped to 30-1800; cleanup still checks runner/adapter idle. |

`mode: off` is an explicit unsafe compatibility escape hatch that restores legacy pass-through admission after existing STT/auth filters. It MUST emit one startup warning and MUST NOT be selected implicitly because configuration is missing or malformed.

Existing audio/lifecycle defaults are orthogonal and do not satisfy this gate: 1.5-second end-of-utterance silence, 0.5-second minimum buffered speech, 300-second voice inactivity timeout, and 120-second minimum playback timeout.

For multiplexed profiles, `discord.voice_listen` MUST be propagated into each adapter's `PlatformConfig.extra` or equivalent profile-owned immutable config. It MUST NOT be bridged through process-global `os.environ`, because one process may serve multiple profile policies. Adding this nested key is a deep-merge addition and does not by itself require a config-version migration.

## Lifecycle and Cleanup

Gate state is in-memory, per adapter/profile, and is never persisted across gateway restart. Clear the active reservation and follow-up arm for the affected context on:

- successful or failed turn completion;
- agent cancellation, interrupt, or exception;
- callback failure before event dispatch;
- `/voice leave` and inactivity auto-leave;
- `/voice off` for the linked text chat;
- moving the bot to another voice channel;
- loss/replacement of the voice client or receiver;
- adapter disconnect, reconnect replacement, fatal error, and gateway shutdown;
- linked text-channel rebinding;
- owner profile teardown;
- follow-up expiry (eager timer or lazy next-access cleanup).

Cancellation-safe cleanup MUST run in `finally` blocks. Timer tasks, if used, must be cancelled and awaited/safely consumed during adapter teardown. State mutation and expiry checks share the per-context lock so an expiry callback cannot delete a newly replaced arm.

Pending PCM flushed during `leave_voice_channel()` MUST still pass the gate. Because leave is a cleanup boundary, the implementation must choose one order and test it: process already-buffered PCM while the old binding is valid, then clear gate state before disconnect; or clear first and deterministically reject it as unbound. The recommended order preserves today's flush-before-disconnect behavior while preventing any newly arriving input after receiver stop.

## Required Wiring

### Deployed layout

- Add immutable gate config and per-context state to `DiscordAdapter`.
- Wire a profile-aware busy/context callback at all existing callback assignment sites in `gateway/run.py`: startup connect, reconnect, and `_handle_voice_channel_join()`.
- Replace direct `_voice_input_callback` use in `_process_voice_input()` with gate evaluation and lease creation.
- Pass only accepted `command_text` plus opaque lease metadata to `_handle_voice_channel_input()`.
- Settle the lease from the real background turn completion/cancellation path, not from `handle_message()` return.
- Remove transcript-bearing rejection/duplicate logs.

### Latest-upstream layout

- Keep the same adapter insertion point.
- Extend `gateway/run_voice.py::GatewayVoiceMixin._bind_voice_input_callback()` to bind the capturing adapter and the gate's context/busy/lifecycle functions.
- Extend `GatewayVoiceMixin._handle_voice_channel_input()` to receive the accepted command and lease metadata only.
- Integrate lease settlement with the current extracted turn lifecycle modules rather than adding new logic back to `gateway/run.py`.
- Propagate `discord.voice_listen` through the Discord plugin's YAML config hook into profile-owned adapter extras.

The implementation should be authored against the latest layout or structured as a small module with thin adapters for both layouts. Do not re-grow the upstream runner god-file.

## Acceptance Matrix

All examples assume strict mode, built-in aliases, default preamble/content limits, no active turn, and an authorized speaker unless stated otherwise.

### Addressed acceptance

| Transcript | Result | Command |
|---|---|---|
| `Sifrot, check the gateway` | `accepted_addressed` | `check the gateway` |
| `Cypherot status` | `accepted_addressed` | `status` |
| `Sifrod, are you there?` | `accepted_addressed` | `are you there?` |
| `Cifra help` | `accepted_addressed` | `help` |
| `Defra, summarize this` | `accepted_addressed` | `summarize this` |
| `Frott repeat` | `accepted_addressed` | `repeat` |
| `Brock, check status` | `accepted_addressed` | `check status` |
| `Cipri, what changed?` | `accepted_addressed` | `what changed?` |
| `Ziprot, stop` | `accepted_addressed` | `stop` |
| `Zip rot, pause` | `accepted_addressed` | `pause` |
| `Steve Brod, speak briefly` | `accepted_addressed` | `speak briefly` |
| `FitFright, are you there?` | `accepted_addressed` | `are you there?` |
| `Sif Front, continue` | `accepted_addressed` | `continue` |
| `Sit, status` | `accepted_addressed` | `status` |

### Permitted and rejected preambles

| Transcript | Result |
|---|---|
| `uh, okay, SIFROT, doctor yourself` | `accepted_addressed` |
| `whaaaa Sifrot, speak up` | `accepted_addressed` |
| `hey Zip rot, check the queue` | `accepted_addressed` |
| `Nate said Sifrot was here` | `rejected_preamble` |
| `we discussed the release and then Sifrot came up earlier` | `rejected_preamble` |
| `uh okay well hey Sifrot status` | `rejected_preamble` (four preamble tokens) |

### Boundary false positives

Each result is `rejected_wake_missing` when follow-up is not considered:

- `the sifrotic acid sample is ready`;
- `a cypherotic pattern appeared`;
- `that was fitfrightening`;
- `ziprotation is broken`;
- `broccoli needs water`;
- `the sitter gave a status update`.

### Low-information rejection

Each result is `rejected_content`:

- `Sifrot`;
- `Cypherot, uh`;
- `Sifrod, okay`;
- `Zip rot...`;
- `Sifrot, thank you`;
- `Sifrot, oh`.

No case invokes the callback or emits a transcript-bearing log.

### Non-addressed conversation

With follow-up disabled, all return `rejected_follow_up_disabled` and have zero downstream effects:

- `Did you see the game last night?`;
- `I am just talking to Alex`;
- `Thank you very much. Thank you.`;
- `Give me your pocket arrow. I am just in the shower`.

With follow-up enabled but unarmed, the same inputs return `rejected_follow_up_unarmed`.

### Follow-up ownership, context, and expiry

Given an explicit arm for user `U1` in context `C1` with expiry `T`:

| Input | State | Result |
|---|---|---|
| U1/C1: `that sounds good, continue` | now < T | `accepted_follow_up` |
| U1/C1: `oh` | now < T | `rejected_content` |
| U2/C1: `continue with that` | now < T | `rejected_follow_up_user` |
| U1/C2: `continue with that` | now < T | `rejected_follow_up_context` |
| U1/C1: `continue with that` | now == T | `rejected_follow_up_expired` |
| U1/C1: `continue with that` | after one accepted follow-up and before T | `accepted_follow_up`; v1 keeps the original arm and deadline unchanged. |

V1 uses a multi-use arm until expiry to permit a short conversational exchange, but no accepted follow-up changes `expires_at`. Tests must freeze/advance a monotonic clock rather than sleep.

### Busy-turn protection

1. U1/C1 says `Sifrot, inspect the service`; it is admitted and reserves C1.
2. Before the real turn completes, any U1 or U2 transcript in C1—including `Sifrot, stop`, `continue`, or background speech—returns `rejected_busy`.
3. The second transcript creates no callback, event, pending message, steer/redirect, interrupt, history row, prompt, LLM request, or transcript log.
4. When the first turn settles, its lease clears exactly once. A stale completion for an older lease cannot clear a newer reservation.

A separate test must race two concurrent addressed evaluations with a barrier. Exactly one may return `accepted_addressed`; the other must return `rejected_busy`.

### Privacy-negative assertions

For every rejection code, the unit/integration harness must use a unique sentinel phrase and assert that sentinel is absent from:

- callback arguments;
- constructed/sent Discord messages;
- adapter pending queues;
- session DB and JSONL history;
- captured prompts/LLM requests;
- hook payloads;
- captured logs.

Do not weaken these to “callback was not awaited”; assert the whole negative boundary.

## Test Plan

Focused unit tests should cover:

1. NFKC/casefold and exact token-sequence matching for every built-in alias.
2. Every embedded-word false positive.
3. permitted/disallowed preambles and both limits.
4. informative-content edge cases for addressed and follow-up admission.
5. deterministic reason precedence.
6. follow-up disabled/unarmed/owner/context/expiry behavior using a fake monotonic clock.
7. non-extension by accepted/rejected follow-up speech.
8. atomic concurrent reservation and stale lease completion.
9. cleanup on turn completion, cancellation, leave, move, timeout, reconnect, shutdown, and rebind.
10. malformed config fail-closed behavior and multiplex profile isolation.
11. zero callback/message/history/prompt/log effects for every rejection.
12. accepted command text excludes the wake phrase and preserves trailing command casing/content.

Integration tests should exercise `DiscordAdapter._process_voice_input()` with mocked local STT and the real gate, then exercise the real callback/event pipeline for accepted speech. The E2E harness should feed recorded PCM through `VoiceReceiver`/STT and assert both positive delivery and negative absence through gateway history and LLM seams.

Recommended targeted suites after implementation:

```text
scripts/run_tests.sh tests/gateway/test_voice_command.py -q
scripts/run_tests.sh tests/gateway/test_discord_voice_gate.py -q
scripts/run_tests.sh tests/e2e/test_discord_adapter.py -q
```

Use the actual final paths created by the implementation. Python tests must use `scripts/run_tests.sh`, not direct `pytest`.

## Non-Goals

- No cloud wake-word service or audio upload.
- No LLM classification of whether speech “sounds addressed.”
- No persistent follow-up state across restart.
- No cross-user, cross-channel, cross-guild, or cross-profile conversational window.
- No automatic queue/steer/interrupt behavior for speech received while busy.
- No transcript-bearing rejection diagnostics.
- No change to typed Discord messages or non-Discord voice-message attachments.
