---
name: telegram-bot-behavior
description: Use when understanding Telegram bot delivery mechanics for Hermes groups and DMs.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [telegram, bot-api, privacy-mode, group-delivery, mention-mechanics]
    related_skills: [telegram-troubleshooting]
---

# Telegram Bot Behavior for Hermes

## Overview

Telegram's bot delivery model is unintuitive: privacy mode, BotFather settings, and membership age interact to determine whether a message reaches your bot at all. This skill documents the exact mechanics so you can diagnose group-silence issues without guessing.

The core insight: Telegram does not deliver every group message to every bot. What the bot receives depends on (1) BotFather privacy mode, (2) whether the message is a `/command@botname` or a plain `@botname` mention, and (3) whether the bot's group membership predates the most recent privacy-mode change.

## When to Use

- A bot is silent in groups but works in DMs
- `/help@BotName` works but plain `@BotName text` does not
- You changed BotFather privacy settings and the bot stopped responding in groups
- You need to understand why a bot can "see" some messages but not others
- Debugging why `can_read_all_group_messages` does not deliver plain mentions

**Don't use for:** Discord, Slack, or other platform adapter issues — those have different delivery models.

## Telegram Bot Delivery Model

### Privacy Mode (BotFather `/setprivacy`)

BotFather has a setting called **Privacy Mode** that controls which group messages the bot receives via `getUpdates` / webhook.

| Privacy Mode | Inbound Delivery |
|---|---|
| **Disabled** (`/setprivacy` → Disable) | Bot receives **all** non-service group messages |
| **Enabled** (default) | Bot receives only: (1) messages starting with `/command@botname`, (2) replies to the bot's own messages, (3) messages from admin users in some configurations |

**Critical:** `can_read_all_group_messages` in `getMe` reports the bot's *capability* based on BotFather settings, but it does NOT override privacy-mode filtering for existing group memberships. See the next section.

### Commands vs Plain Mentions

| Message Form | What Telegram Delivers (Privacy Enabled) | What Telegram Delivers (Privacy Disabled) |
|---|---|---|
| `/help@BotName` | ✅ Delivered | ✅ Delivered |
| `/help` (no @botname) | ✅ Delivered (if bot has command handler) | ✅ Delivered |
| `@BotName hello` | ❌ Not delivered | ✅ Delivered |
| `hello` (no mention) | ❌ Not delivered | ✅ Delivered |
| Reply to bot's message | ✅ Delivered | ✅ Delivered |
| Bot is group admin | ⚠️ Varies by Telegram version | ✅ Delivered |

**Bottom line:** `/command@botname` is a Telegram-level trigger that works regardless of privacy mode. Plain `@botname` mentions are delivered only when privacy mode is disabled.

### The Re-Add Requirement

**Changing BotFather privacy settings does NOT retroactively update existing group memberships.** If you change `/setprivacy` from Enabled to Disabled while the bot is already in a group, the bot will NOT start receiving plain mentions in that group.

**Required procedure after changing privacy settings:**

1. Remove the bot from the affected group (or kick it via group admin)
2. Add the bot back to the group
3. Verify with a plain `@BotName probe-message`

This is a Telegram platform behavior, not a Hermes bug. The membership "remembers" the privacy setting from when the bot joined.

### `getMe` vs Actual Delivery

The Bot API `getMe` method returns:
- `can_join_groups`: whether the bot can be added to groups
- `can_read_all_group_messages`: whether privacy mode is disabled

`getWebhookInfo` returns:
- `pending_update_count`: number of unprocessed updates
- `last_error_date` / `last_error_message`: most recent API error

These are useful for confirming the bot's configuration but do NOT prove delivery is working for a specific message type. Always verify with an actual probe.

## Controlled Delivery Capture

The only reliable way to separate "Telegram didn't deliver" from "Hermes dropped it" is to capture the raw update from Bot API while Hermes is not consuming updates.

### Procedure

1. **Stop Hermes polling:** `systemctl --user stop hermes-gateway.service` (or stop the Hermes process)
2. **Send a unique probe** in the target group: `@BotName hermes-delivery-probe-<timestamp>`
3. **Capture raw updates** from Bot API (do NOT advance offset):
   ```bash
   # Get the bot token from ~/.hermes/.env
   TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.hermes/.env | cut -d= -f2 | tr -d '"' | tr -d "'")
   # Fetch updates without consuming them
   curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?timeout=0&allowed_updates=[\"message\"]" | jq '.result[] | select(.message.text | contains("hermes-delivery-probe"))'
   ```
4. **Interpret results:**
   - **Probe present:** Telegram delivered it → problem is in Hermes adapter gating
   - **Probe absent:** Telegram never delivered it → problem is BotFather privacy / membership
5. **Restart Hermes:** `systemctl --user start hermes-gateway.service`

### Interpreting the Raw Update

A delivered probe update looks like:
```json
{
  "update_id": 12345,
  "message": {
    "message_id": 678,
    "from": { "id": 123456, "is_bot": false, "first_name": "User" },
    "chat": { "id": -100123, "type": "supergroup" },
    "text": "@BotName hermes-delivery-probe-1234",
    "entities": [{ "type": "mention", "offset": 0, "length": 9 }]
  }
}
```

Key fields to check:
- `message.from` — sender identity present
- `message.chat.id` — correct group
- `message.text` — exact probe text
- `message.entities` — should contain a `mention` entity for `@BotName`

If the update exists but Hermes didn't process it, the issue is in adapter gating (auth, mention requirement, or observation settings).

## Hermes Adapter-Level Gating

After Telegram delivers an update, Hermes applies its own filters:

1. **Authorization** (`_is_user_authorized_from_message`): checks `allow_from` in adapter config, runner auth, or `TELEGRAM_ALLOWED_USERS` env var
2. **Mention requirement** (`require_mention=true`): plain group messages without `@BotName` are ignored
3. **Exclusive mentions** (`exclusive_bot_mentions=true`): only exact `@BotName` triggers, not partial matches
4. **Observation** (`observe_unmentioned_group_messages=false`): unmentioned messages are not even logged

These are additive — all must pass for a group message to reach the agent.

## Quick Reference

| Symptom | Likely Cause | Fix |
|---|---|---|
| Bot works in DMs, silent in groups | Privacy mode enabled | Disable privacy + re-add bot to group |
| `/help@Bot` works, `@Bot text` doesn't | Privacy mode enabled | Same as above |
| Bot was working, now silent in groups | Privacy setting changed without re-add | Re-add bot to affected groups |
| `getMe` says `can_read_all_group_messages=true` but bot is silent | Privacy changed after membership | Re-add bot to affected groups |
| Probe shows Telegram delivered, Hermes didn't respond | Adapter gating (auth, mention, observation) | Check `config.yaml` telegram section |

## Common Pitfalls

1. **Assuming `getMe` proves delivery.** It reports configuration, not runtime delivery. Always probe with an actual message.

2. **Changing privacy and expecting immediate effect.** Existing memberships retain the old setting. Re-add is mandatory.

3. **Confusing `/command@botname` with `@botname text`.** Commands are a special Telegram trigger delivered regardless of privacy. Plain mentions are not.

4. **Not stopping Hermes before capturing.** If Hermes is polling, it will consume the update before you can capture it. Stop the service first.

5. **Using `getUpdates` with offset advancement.** This consumes the updates. Use `timeout=0` and do NOT pass an offset to keep updates available for inspection.

## Verification Checklist

- [ ] Confirmed whether the issue is Telegram delivery or Hermes gating (controlled capture)
- [ ] Checked `getMe` and `getWebhookInfo` for bot configuration
- [ ] Verified privacy mode setting via BotFather `/getprivacy`
- [ ] If privacy was changed: removed and re-added bot to affected groups
- [ ] Tested with both `/command@BotName` and plain `@BotName text`
- [ ] Checked Hermes `config.yaml` for `require_mention`, `exclusive_bot_mentions`, `allow_from` settings
