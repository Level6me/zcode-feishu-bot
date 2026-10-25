---
name: feishu-bot
description: Deploy and operate the Feishu (Lark) bot powered by the zcode engine. Use when the user wants to install, configure, start, stop, restart, upgrade, or troubleshoot the Feishu bot, or when they ask what the bot can do / which slash commands it supports.
---

# Feishu Bot for ZCode — operations guide

This plugin ships a complete Feishu bot (`bot/`) that bridges Feishu chats to the
local **zcode** engine over the ZCode Protocol (stdio app-server). It is a full
1:1 feature port of the upstream engine-based Feishu bot, re-driven by zcode.

## Layout

- `bot/` — the Python bot project (entrypoint `bot/main.py`)
- `bot/zcode_protocol.py` — engine adapter (app-server JSON-RPC client, event translation, transcript journal)
- `bot/install.sh` — one-shot installer (venv + deps + PM2 service)
- `bot/.env` — credentials & settings (Feishu app id/secret, engine overrides)

## One-time setup

```bash
cd <plugin-root>/bot
bash install.sh          # guided: creates venv, installs deps, writes .env, registers PM2
```

Requirements on the host:

1. **Python 3.10+**, **Node.js** (the zcode engine runs on node), and **PM2** for
   production (`npm install -g pm2`) — `install.sh` checks all of these.
2. **zcode engine**: either ZCode Desktop installed (auto-detected at
   `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`) or a `zcode` CLI in
   PATH. Override with `ZCODE_BIN=` in `.env`.
3. **Engine login (required once)**: run `zcode login zai` (or
   `node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs login zai`) and
   complete the Z.ai browser authorization. This activates the Coding Plan
   account providers that power the bot's models.
4. **Feishu app**: create an enterprise app on open.feishu.cn, enable WebSocket
   long-connection event mode, subscribe to `im.message.receive_v1`, grant the
   `im:message`, `im:message:resource`, `im:image`, `im:file`,
   `im:message.reaction`, `im:chat:readonly`, `contact:user.base:readonly`
   scopes, publish a version, then put its App ID / App Secret into `bot/.env`
   as `FEISHU_APP_ID` / `FEISHU_APP_SECRET`.

## Daily operations (run from `bot/`)

```bash
bash install.sh status    # PM2 process status
pm2 logs zcode-feishu-bot       # live logs
pm2 restart zcode-feishu-bot    # restart
bash install.sh update    # git pull + dependency refresh + smooth restart
bash install.sh uninstall # remove services
```

Manual run without PM2: `venv/bin/python3 main.py`.

## What the bot does (feature parity with the original)

- Natural-language engineering tasks executed by the zcode agent engine
  (read/write code, run terminal commands, web search), with in-place streaming
  Feishu cards, task-plan step tracking and stall watchdogs.
- Slash console: `/help /model /context /memory /brain /clear /project /note
  /quota /status /stop /update /ping /auth /user /plugins /cron /s1` etc.
- First private-chat sender auto-becomes admin; `/auth` + three-tier
  authorization (Basic/Dev/Full); rate limiting.
- System One (Jev) decision gateway with local heuristic fallback, adaptive
  reasoning tiers mapped to zcode reasoning levels (low/high/max).
- Plugin ecosystem (notes, cron scheduler, AI memory, system updater, RPi GPIO,
  passport bridge) with hot reload, plus the dynamic plugin store.
- Multimodal: images / files / audio / video in; generated artifacts and voice
  replies (Edge-TTS) out.

## Model configuration

Bot-facing model ids (shown on the `/model` panel) map onto the engine like:

| bot id | engine |
| --- | --- |
| `glm-5.3`, `glm-5.3-flash`, `glm-5.2`, `glm-5-turbo` | provider `account:zai-start-plan` (env `ZCODE_PROVIDER_ID`) |
| suffix `-low` / `-medium` / `-high` | reasoningLevel `low` / `high` / `max` |

## Troubleshooting

- `zcode 引擎未找到` card → set `ZCODE_BIN` in `.env` or install ZCode.
- `Select a model before continuing` / empty `/model` panel → run
  `zcode login zai` once and restart (`pm2 restart zcode-feishu-bot`).
- Feishu WebSocket not connecting → check `FEISHU_APP_ID/SECRET`, app published,
  event mode = WebSocket long connection.
- See `~/.zcode/feishu-brain/<session>/.system_generated/logs/transcript.jsonl`
  for per-conversation journals used by context/choice-card features.
