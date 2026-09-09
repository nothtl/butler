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
    python -m butler.mcp_stdio  <=== 47 tools  ▼
                 Butler Python domain (planner, courses, food, ...)
```

## Prerequisites (do these first)

1. **Rotate the Telegram bot token.** Your current token was committed to chat
   and must be considered leaked — treat it as compromised. In BotFather: revoke
   and generate a new token. Set it via env only:
   `export BUTLER_TELEGRAM_TOKEN=<new_token>`.
2. **DeepSeek key:** `export BUTLER_LLM_KEY=<key>` (as used by `~/.config/butler/config.toml`).
3. **Home Assistant (optional):** `export BUTLER_HA_TOKEN=<token>` and
   `[home_assistant] url <url>` in `config.toml`, to enable `presence`/location.

Never commit the values: they are referenced as `$ENV` in `openclaw.json`.

## M0 — Stand up OpenClaw

1. Install OpenClaw (Node 20+ is available here):
   ```bash
   npm i -g openclaw && openclaw doctor
   ```
2. Copy this config to your OpenClaw config path (workspace or
   `~/.config/openclaw/openclaw.json`), replacing `#BUTLER_REPO#` with
   `/home/tingli/butler`.
3. Verify the model wires up and a turn runs with the built-in loop
   (`agentRuntime.id: "openclaw"`), which avoids the Codex runtime.

## M1 — Register + verify the MCP bridge

OpenClaw spawns `python -m butler.mcp_stdio` as a child process (config above).
Verify reachability and that all 47 tools list:

```bash
openclaw mcp doctor butler --probe
openclaw mcp list
```

## M3 — Guard the write tools

Side-effecting tools are gated behind operator approval:

```bash
openclaw mcp configure butler --approval prompt
```
Tools in the `prompt` list in `openclaw.json` (`task_*`, `came_up`,
`reschedule`, `undo`, `add_course`, `drop_course`, `routines_confirm`, ...)
require the operator to approve. They are the same idempotent transitions the
decider performs, so Butler's safety layer is unchanged.

## M5 — Optional: migrate Telegram to OpenClaw

Move the Telegram channel into OpenClaw (channel = Telegram) and retire
`butler/telebot.py`. This is a large behavioral change — do it LAST, only after
M0–M4 are green, because the callback-state machine and `_authorized` gating in
`telebot.py` currently own the human-in-the-loop UX.

## Development

- Run the MCP server directly (already works): 
  `printf '<jsonrpc>' | .venv/bin/python -m butler.mcp_stdio`
- Parity smoke test: `.venv/bin/python tests/run_acceptance_mcp.py`.
- Re-point the acceptance suites to call the MCP tools (parity tests); the
  scheduler/core suites (`tests/test_schedule.py`, `tests/run_acceptance.py`)
  stay as-is and must keep passing.
