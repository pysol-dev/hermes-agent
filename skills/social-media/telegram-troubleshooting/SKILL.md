---
name: telegram-troubleshooting
description: Use when diagnosing Hermes Telegram adapter failures, delivery issues, or connectivity problems.
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [telegram, troubleshooting, debugging, gateway, adapter, privacy-mode, groups]
    related_skills: [telegram-bot-behavior]
---

# Telegram Troubleshooting for Hermes

## Overview

This skill covers the diagnostic workflow for Hermes Telegram adapter failures. About 30% of Telegram issues are invalid/revoked bot tokens, 30% are pairing/privacy-mode problems, and the remaining 40% split across webhook conflicts, removed bots, and post-upgrade regressions.

The diagnostic principle: **isolate Telegram delivery from Hermes adapter gating** before changing any configuration. The two-layer model is critical to understand:

| Layer | Controls | Configured via |
|---|---|---|
| **Telegram delivery** | What messages the bot *receives* from Telegram servers | BotFather privacy mode, bot admin rights, membership |
| **Hermes adapter gating** | What received messages Hermes *processes* | `config.yaml` (require_mention, allow_from, observation) |

Both layers must be correct. A message blocked at either layer produces the same symptom: bot silence.

## When to Use

- Bot is silent in groups or DMs
- Gateway shows `connected` but messages are not processed
- `/restart` notification not delivered
- Send-path degradation warnings in logs
- Bot was working, then stopped after a change
- `409 Conflict` errors in logs
- Bot responds intermittently

**Don't use for:** Discord, Slack, or other platform issues.

## The Diagnostic Ladder

Run these commands in order. Stop at the first one that reveals the problem.

```bash
# 1. Is the gateway alive?
systemctl --user status hermes-gateway.service

# 2. Is Telegram connected?
cat ~/.hermes/gateway_state.json | jq '{gateway_state, telegram: .platforms.telegram}'

# 3. Is the bot token valid?
TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.hermes/.env | cut -d= -f2 | tr -d '"' | tr -d "'")
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | jq '{ok, can_join_groups, can_read_all_group_messages}'

# 4. Are there pending updates or errors?
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | jq '{pending_update_count, last_error_date, last_error_message}'

# 5. What do the logs say?
tail -50 ~/.hermes/logs/gateway.log
```

**Interpretation:**
- Step 1 fails → gateway crash (see Gateway Crash Loop)
- Step 2 shows `disconnected` or `error` → connection issue
- Step 3 shows `ok: false` → invalid/revoked token (most common cause)
- Step 4 shows errors → API or webhook problem
- Step 5 shows `drop` lines → adapter gating (see Drop Line Diagnosis)

## Drop Line Diagnosis

The single most useful diagnostic is the **drop line** in gateway logs. When Hermes receives a message but deliberately ignores it, the log tells you exactly why. Watch logs while sending a message:

```bash
tail -f ~/.hermes/logs/gateway.log | grep -i "drop\|inbound\|unauthorized"
```

| Log line | Meaning | Fix |
|---|---|---|
| `drop dm (pairing required)` | DM pairing not approved | Run `hermes pairing list`, approve pending |
| `drop dm (not in allowlist)` | User ID not in `allow_from` | Add numeric Telegram user ID to config |
| `drop group (not in allowlist)` | Group not configured | Add group to `group_allowed_chats` |
| `drop group (pairing required)` | Group pairing pending | Approve group pairing |
| `unauthorized` or `Forbidden` | Token or permission issue | Verify token with BotFather `/mybots` |
| `Chat not found` | Bot removed from group or wrong chat_id | Re-add bot or fix chat_id |

**If you see a drop line, you know the exact problem.** No further diagnosis needed for that message.

If you see NO log entry at all when messaging the bot, the message never reached Hermes — that's a Telegram delivery issue (privacy mode, bot token, or network).

## Common Failure Classes

### Bot Silent in Groups (DMs Work)

**The #1 Telegram bot issue.** Bot responds in DMs but ignores all group messages.

**Root cause:** Telegram privacy mode is enabled by default. Bots in groups only receive: (1) `/command@botname`, (2) replies to the bot's own messages, (3) messages from admins in some configurations. Plain `@botname` mentions are NOT delivered.

**Two-layer fix:**

**Layer 1 — Telegram delivery (BotFather):**
1. Open @BotFather on Telegram
2. Send `/mybots` → select your bot → Bot Settings → Group Privacy
3. If "Privacy mode is enabled", tap "Turn off"
4. **Remove the bot from the group and re-add it** (mandatory — Telegram caches the setting at join time)

**Layer 2 — Hermes adapter gating:**
```bash
# Check require_mention setting
grep -A 5 "require_mention\|exclusive_bot_mentions" ~/.hermes/config.yaml

# Check group allowlist
grep -A 10 "group_allowed_chats\|allow_from" ~/.hermes/config.yaml
```

**Verification:**
```bash
# Stop Hermes, send a probe, capture raw update
systemctl --user stop hermes-gateway.service
# Send: @BotName hermes-probe-test in the group
curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?timeout=0" | jq '.result[] | select(.message.text | contains("hermes-probe"))'
systemctl --user start hermes-gateway.service
```

If probe present → Telegram delivered → check adapter config. If probe absent → Telegram didn't deliver → privacy mode / re-add issue.

### Bot Silent in DMs

**Diagnostic path:**
1. Check gateway status (Step 1 of Diagnostic Ladder)
2. Check logs for `drop dm` lines — this tells you exactly why
3. If `drop dm (pairing required)`: run `hermes pairing list` and approve
4. If `drop dm (not in allowlist)`: add your numeric Telegram user ID to `allow_from`
5. Find your Telegram user ID: send any message to @userinfobot
6. Check if bot was blocked by the user

### Invalid or Revoked Bot Token (~30% of issues)

**Symptoms:** Bot shows as `disconnected`, `getMe` returns `ok: false`, or bot simply never responds.

**Diagnosis:**
```bash
TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.hermes/.env | cut -d= -f2 | tr -d '"' | tr -d "'")
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | jq .
```

**Fix:**
1. Open @BotFather → `/mybots` → select bot → API Token
2. If token shows as "revoked", generate a new one
3. Update `~/.hermes/.env` with new token
4. Restart: `systemctl --user restart hermes-gateway.service`

### 409 Conflict: terminated by other getUpdates request

**Symptoms:** Gateway logs show `Conflict` error, bot responds intermittently or not at all.

**Cause:** Another Hermes instance (or the same bot in another profile) is polling with the same token. Only one process can poll a bot token at a time.

**Resolution:**
```bash
# Find other instances
ps aux | grep hermes | grep -v grep

# Check for other profiles
hermes profile list

# Kill conflicting instance (if safe)
kill <PID>
```

### Send-Path Degradation

**Symptoms:** `Restart notification deferred: Telegram send path degraded` in logs.

**Diagnostic path:**
1. Check bot token validity (Step 3 of Diagnostic Ladder)
2. Check for rate limiting: look for `429` errors in logs
3. Check network connectivity: `curl -s https://api.telegram.org`
4. Check if Telegram API is having issues: status.telegram.org

**Resolution:** Usually transient. Hermes retries automatically. If persistent, verify bot token and check for IP-based rate limiting.

### Gateway Crash Loop

**Symptoms:** `systemctl --user status` shows `failed` or repeated restarts.

```bash
# Check crash reason
journalctl --user -u hermes-gateway.service --since "10 minutes ago" --no-pager

# Reset failed state
systemctl --user reset-failed hermes-gateway.service

# Start fresh
systemctl --user start hermes-gateway.service
```

**Common causes:**
- Invalid `config.yaml` syntax (YAML parse error)
- Missing required environment variables
- Port conflict (if using webhook mode)
- Corrupted session state

### Bot Removed from Group

**Symptoms:** A previously working group stopped responding. Other groups still work.

**Diagnosis:** Check if the bot is still a member:
```bash
# Look for "not a member" or "kick" errors in recent logs
grep -i "not a member\|kicked\|Chat not found" ~/.hermes/logs/gateway.log | tail -10
```

**Fix:** Re-add the bot to the group. If the group requires approval, approve the new pairing request.

### Forum Topic Not Enabled

**Symptoms:** Bot works in the group's general chat but not in specific forum topics.

**Cause:** Forum topics in Telegram supergroups require explicit enablement in Hermes config.

**Fix:** Check topic configuration in `config.yaml`:
```yaml
telegram:
  groups:
    "-1001234567890":
      require_mention: true
      topics:
        "12345":  # topic thread ID
          enabled: true
```

### Post-Upgrade Regressions

**Symptoms:** Bot broke immediately after a Hermes update.

**Known regressions:**
- 2026.2.24–2026.2.26: Group messages stopped being received while DMs continued. Fixed in 2026.2.27.

**Fix:**
1. Check Hermes version: `hermes --version`
2. Check changelog for known issues
3. If a regression is confirmed, update to the latest version
4. If update isn't possible, check if a config migration is needed: `hermes config migrate`

## Key Diagnostic Commands

```bash
# Bot configuration
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | jq .
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo" | jq .

# Capture updates (non-consuming — do NOT advance offset)
curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?timeout=0&allowed_updates=[\"message\"]" | jq '.result | length'

# Gateway status
systemctl --user status hermes-gateway.service
cat ~/.hermes/gateway_state.json | jq .

# Hermes full status
hermes status --all
hermes doctor

# Live log watching
tail -f ~/.hermes/logs/gateway.log

# Find your Telegram user ID
# Send any message to @userinfobot on Telegram
```

## The 8-Step Debugging Checklist

When the bot goes silent, run through this in order:

1. **Gateway alive?** — `systemctl --user status hermes-gateway.service`
2. **Logs show received messages?** — `tail -f ~/.hermes/logs/gateway.log` while messaging
3. **BotFather privacy mode off?** — @BotFather → `/mybots` → Bot Settings → Group Privacy → Turn off
4. **Re-added bot after privacy change?** — Remove from group, re-add (mandatory)
5. **Group/topic in allowlist?** — Check `group_allowed_chats` in config
6. **require_mention matches intent?** — `true` = mention-only, `false` = respond to all
7. **Topic enabled?** — Forum topics need `enabled: true` in config
8. **Bot has admin rights?** — Some group configs require bot to be admin with "Read Messages"

**Sanity check:** Send a test message in DM. If DMs work but groups don't, the problem is 100% in group/topic config. If DMs also fail, it's deeper (auth, model, gateway).

## Common Pitfalls

1. **Not reading the drop line.** The log tells you exactly why a message was dropped. Check logs before changing config.

2. **Changing privacy and expecting immediate effect.** Existing memberships retain the old setting. Re-add is mandatory.

3. **Confusing `/command@botname` with `@botname text`.** Commands are a Telegram-level trigger delivered regardless of privacy. Plain mentions are not.

4. **Not stopping Hermes before capturing.** If Hermes is polling, it will consume updates before you can inspect them.

5. **Confusing bot token with OAuth credentials.** Telegram uses bot tokens (in `.env`), not OAuth (in `auth.json`). OAuth issues affect the LLM provider, not Telegram.

6. **Assuming `connected` state means messages are processed.** The gateway can be connected but the adapter can still gate messages via auth/mention/observation settings.

7. **Looking in the wrong log file.** Gateway logs are in `~/.hermes/logs/gateway.log`. System logs are in `journalctl --user -u hermes-gateway.service`.

8. **Forgetting the two-layer model.** Even with privacy mode disabled, Hermes adapter gating (require_mention, allow_from, observation) can still block messages. Both layers must be correct.

## Verification Checklist

- [ ] Gateway is running and Telegram state shows `connected`
- [ ] Bot token is valid (`getMe` returns `ok: true`)
- [ ] No conflict errors (single instance per token)
- [ ] Controlled capture confirms Telegram delivery (or identifies BotFather issue)
- [ ] Adapter config matches intended behavior (`require_mention`, `allow_from`)
- [ ] For groups: bot was re-added after any privacy-mode change
- [ ] For DMs: user's Telegram ID is in `allow_from` (if set)
- [ ] For forum topics: topic is explicitly enabled in config
- [ ] No network connectivity issues to `api.telegram.org`
- [ ] Drop lines in logs explain any remaining silent messages

## Sources

This skill synthesizes diagnostic patterns from:
- Telegram Bot API FAQ (core.telegram.org/bots/faq)
- Hermes Agent issue #18580 (NousResearch/hermes-agent)
- NemoClaw issue #4068 (NVIDIA/NemoClaw) — detailed privacy-mode diagnostics
- OpenClaw channel troubleshooting guide (openclawlab.com)
- Stack Junkie OpenClaw Telegram debugging guide
- Omar Diaz OpenClaw Telegram debugging checklist
- SFAI Labs OpenClaw Telegram connection troubleshooting
- ZenClaw OpenClaw Telegram group privacy mode fix
