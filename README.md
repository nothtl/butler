# Butler

A self-hosted, on-device **personal executive assistant** for a Raspberry Pi 5
(M.2 NVMe, Python 3.13.5). It combines a deterministic file engine with
calendar, task, course, food, routine and scheduling intelligence, reachable
through Telegram, a CLI, and an MCP server.

Everything runs locally. The only external dependency is the LLM used to phrase
replies and interpret ambiguous requests (DeepSeek, configured in
`~/.config/butler/config.toml`).

## Principle

> The LLM reasons and proposes. The deterministic layer validates, gates and
> schedules. External systems perform side effects. The LLM never bypasses
> safety, permissions, idempotency, audit, or the scheduler.

- **The solver is the source of truth for feasibility.** Hard constraints
  (lectures, exams, appointments, sleep, imported events) are never overridden
  by the model.
- **Side effects are gated.** Every mutation goes through `SafetyPolicy` →
  permission → idempotency → execution → audit.
- **No canned replies** (see `AGENTS.md`): every conversational answer is
  generated live from live state, or the state itself is returned.
- **Files stay in managed roots**; deletions go to `.butler/trash` and are
  recoverable.

## Front-ends

| Front-end | Entry point |
|-----------|-------------|
| Telegram bot | `butler-bot.service` → `python -m butler.cli bot` |
| CLI | `python -m butler.cli <cmd> --json` |
| MCP (stdio) | `python -m butler.mcp_stdio` |

All front-ends share one in-process `Container`, so they see the same state.

## Install / Run

```bash
cd /home/tingli/butler
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

./butler.sh status
./butler.sh bot             # start the Telegram bot
./butler.sh monitor         # watch + auto-place course files
./butler.sh remote          # tiny HTTP control server
```

Config lives at `config/butler.toml`; the installed copy is
`~/.config/butler/config.toml`. Set `BUTLER_TELEGRAM_TOKEN` or put it in the
config to use the bot.

## MCP / AI Butler

`butler/mcp.py` exposes Butler over stdio JSON-RPC (`2024-11-05`) with two
disjoint profiles selected by `BUTLER_MCP_PROFILE`:

- **`full`** (default) — the historical 51-tool surface used by OpenClaw.
- **`readonly`** — a 23-tool, side-effect-free executive surface
  (`get_time`, `get_context`, `get_day`, `plan_day`, `get_schedule`,
  `get_tasks`, `get_courses`, `get_projects`, `get_project`,
  `get_project_workload`, `get_project_risk`, `get_project_dependencies`,
  `find_available_time`, `get_week`, `web_search`, `web_research`,
  `web_fetch`, `knowledge_lookup`, `optimize_day`, `optimize_week`,
  `evaluate_schedule`, `find_best_slot`, `executive_ask`) for an external agent
  runtime such as
  [AI Butler](https://github.com/LumabyteCo/aibutler).

The readonly profile is enforced at dispatch, not just hidden from
`tools/list`. See `docs/architecture/ai-butler-integration.md` for the verified
contract, boundary, security model and migration plan.

## Layout

```
butler/
  core.py      Container: wires config/db/engine/planner/agent/... (one instance)
  cli.py       command-line entrypoint (python -m butler.cli)
  telebot.py   Telegram bot (NL free text → agent runtime; slash commands)
  mcp.py       MCP stdio server (profile-aware)
  agent/       typed Intent, tool registry, runtime, MCP catalog
  decider.py   deterministic NLU + intent dispatch
  schedule.py  pure, no-LLM, no-I/O constraint solver
  planner.py   DB + Google Calendar wrapper over the solver
  course.py    course intelligence (page/PDF/ICS scraping)
  projects.py  project intelligence (project/milestone/dependency DAG)
  web.py       web & external knowledge (search/fetch/extract/verify)
  optimizer.py schedule optimization (multi-day, project/deadline aware)
  gcal.py      Google Calendar read/write
  safety.py    ActionClass + SafetyPolicy (the single gate)
  audit.py     append-only audit trail + redaction
  idempotency.py  exactly-once side effects
  recovery.py  undo + DB backup/restore
  db.py        SQLite schema + migrations
  config.py    TOML config + env overrides
  food.py foodplan.py house.py nas.py timeline.py routines.py
  executive.py proactive.py motivation.py engine.py search.py ...
docs/architecture/current-state.md      actual architecture + gaps
docs/architecture/ai-butler-integration.md  AI Butler boundary
docs/architecture/project-intelligence.md   M3 project/milestone/dependency model
docs/architecture/web-knowledge.md          M4 web & external knowledge layer
docs/architecture/schedule-optimization.md  M5 deterministic schedule optimization
```

## Acceptance

```
.venv/bin/python tests/run_acceptance_*.py      # each prints N/N passed
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m compileall -q butler
```

## Commands

`status storage index list find search resume organize dupes trash recover
mkdir move rename apply backup bot monitor remote day week now course ...`

Append `--json` for machine-readable output, `--yes` to auto-confirm plans.
