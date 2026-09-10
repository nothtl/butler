# Pi Butler 2.0 — OpenClaw integration

This directory wires Butler's Python domain into [OpenClaw](https://openclaw.ai)
as an MCP server, with DeepSeek as the model and Butler's deterministic planner
as the fallback for side-effecting reliability. Butler stays the single source of
truth (`tools/*` re-read the DB on every call, so answers are always fresh).

```
 ┌────────────── OpenClaw gateway ──────────────┐
 │ chat (Telegram/CLI) ──> agent loop (openclaw) │
 │        skills: butler-{day,courses,food,      │
 │                 context,goals}                │
 │        MCP server "butler" (stdio) ─────────┐ │
 └─────────────────────────────────────────────┼─┘
    .venv/bin/python -m butler.mcp_stdio  <=== 49 tools  ▼
                 Butler Python domain (planner, courses, food, ...)
```

Status: **M0–M4/M6 done and verified, plus the gateway service is installed and
running.** M5 (migrating Telegram into OpenClaw) is intentionally last and not
started.

## Prerequisites

1. **Rotate the Telegram bot token.** The old token was leaked in chat — revoke it
   in BotFather and set the new one via env only:
   `export BUTLER_TELEGRAM_TOKEN=<new_token>`.
2. **DeepSeek key** in the environment. This machine exports it from
   `~/.bashrc` as `DEEPSEEK_API_KEY` (also present in
   `~/.config/butler/config.toml` for the Python side). Never commit the value.
3. **Home Assistant (optional):** `export BUTLER_HA_TOKEN=<token>` and a
   `[home_assistant] url` in `config.toml` to enable `presence`/location.

## M0 — Stand up OpenClaw (DONE)

OpenClaw 2026.9.3 requires **Node ≥24.16 <25 or ≥26.1**; the system Node (20.x)
is too old. Node 24 LTS is installed user-local (no sudo):

```bash
V=v24.21.0; A=linux-arm64
curl -fsSLO https://nodejs.org/dist/$V/node-$V-$A.tar.xz
curl -fsSLO https://nodejs.org/dist/$V/SHASUMS256.txt
grep "node-$V-$A.tar.xz" SHASUMS256.txt | sha256sum -c -
tar -xf node-$V-$A.tar.xz -C ~/.local
ln -sfn ~/.local/node-$V-$A ~/.local/node24
```

`~/.bashrc` and `~/.profile` prepend `~/.local/node24/bin` to `PATH`. Then:

```bash
npm i -g openclaw
# npm 11 gates install scripts; allow them once:
npm i -g openclaw --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs
```

**Config** lives at `~/.openclaw/openclaw.json` (not `~/.config/openclaw`). The
file in this directory is the secret-free template; copy it:

```bash
cp openclaw/openclaw.json ~/.openclaw/openclaw.json
chmod 600 ~/.openclaw/openclaw.json
```

The API key is an **env reference** (`{source: env, provider: default,
id: DEEPSEEK_API_KEY}`), so no plaintext secret is stored. Note the schema
changed from earlier drafts: per-model runtime now lives at
`agents.defaults.models["deepseek/deepseek-chat"].agentRuntime` (there is no
top-level `models["deepseek/deepseek-chat"]`, no provider `name`, and no
`mcp.servers.butler.tools` — use `toolFilter` for include/exclude).

Verify:

```bash
openclaw config validate
openclaw agent --local -m "Reply with exactly one word: pong"   # -> pong
```

## M1 — MCP bridge (DONE)

OpenClaw spawns `/home/tingli/butler/.venv/bin/python -m butler.mcp_stdio` as a
child process (see config). Verify all tools list:

```bash
openclaw mcp list
openclaw mcp probe butler     # -> butler: 49 tools
```

A live tool call through the model also works:

```bash
openclaw agent --local -m "Call the butler MCP tool named status and summarize it."
```

## M3 — Guard the write tools

MCP tools carry no safety annotations, so OpenClaw requires approval in
prompting session postures. Per-server tool visibility is controlled with a
`toolFilter` (`include`/`exclude`) under `mcp.servers.butler`; approval policy
is managed with:

```bash
openclaw approvals --help
openclaw mcp configure butler --help
```

Side-effecting Butler transitions stay idempotent and unchanged.

## Gateway service (DONE)

The gateway is OpenClaw's persistent daemon: it hosts the agent runtime and
sessions, connects chat channels, keeps MCP servers warm, and runs automations.
`openclaw agent --local` runs embedded/one-shot; the gateway is required for
channels (M5).

```bash
openclaw doctor --fix --generate-gateway-token   # sets gateway.auth mode=token
openclaw config set gateway.mode local
openclaw daemon install
openclaw daemon status
```

Installed as the user service `openclaw-gateway.service` (enabled, active,
`ws://127.0.0.1:18789`). If install is blocked with
`SERVICE_DEFINITION_UNKNOWN: [unsafe-permissions]`, the systemd user dir must
not be group/world-writable:

```bash
chmod go-w ~/.config ~/.config/systemd ~/.config/systemd/user
```

The service uses the user-local Node 24 binary explicitly (system Node 20 is
out of range).

## M5 — Optional: migrate Telegram to OpenClaw (NOT STARTED)

Move the Telegram channel into OpenClaw and retire `butler/telebot.py`. Do this
LAST, because the callback-state machine and `_authorized` gating in
`telebot.py` currently own the human-in-the-loop UX. With the gateway running,
configure the channel (`openclaw channels --help`) once the new BotFather token
is in the environment.

## Development

- Run the MCP server directly:
  `printf '<jsonrpc>' | .venv/bin/python -m butler.mcp_stdio`
- Parity smoke test: `.venv/bin/python tests/run_acceptance_mcp.py`.
- Keep the scheduler/core suites green (`tests/test_schedule.py`,
  `tests/run_acceptance.py`).
