# Pi Butler

A self-hosted, always-on **personal executive assistant** for a Raspberry Pi 5
(8 GB, M.2 NVMe). It combines a deterministic file engine with calendar, task,
course, project, food, routine, scheduling, web-knowledge, long-term memory and
proactive intelligence, reachable through Telegram, a CLI, and an MCP server.

**Who it is for:** someone who wants a private, local assistant that manages
their real commitments without handing an LLM the keys to their calendar, files
or inbox. It runs entirely on your own hardware; the only optional external
dependency is an OpenAI-compatible LLM used to *phrase* replies and interpret
ambiguous requests.

## What it does

- **Schedule** — a pure, deterministic constraint solver places task blocks
  around hard commitments, sleep and buffers; an optimizer plans across days
  using deadlines, projects, dependencies and soft preferences.
- **Projects** — Goal → Project → Milestone → Task with effort-based progress,
  explainable risk and a validated dependency DAG.
- **Courses & food** — scrapes course pages/feeds, imports class times, tracks
  deadlines; tracks pantry/recipes and plans meals.
- **Web knowledge** — deterministic search/fetch/extract with source URLs,
  bounded content and prompt-injection defence.
- **Memory** — a typed, provenance-aware, auditable long-term store that learns
  soft preferences and estimate corrections. Inferred memory is never hard.
- **Proactive** — a deterministic engine that notices deadline risk, free time,
  conflicts, risk increases, missed work, routine opportunities and more, and
  notifies you with evidence — never spamming.
- **MCP** — a read-only executive surface for external agent runtimes (AI
  Butler), plus the historical `full` tool profile.
- **Topics** — each Telegram forum topic is a durable context/view over the
  shared domain data (courses, projects, tasks, food) with capabilities, a
  pinned control panel and topic-scoped memory. `/topic` opens the panel.
- **Tracking** — one generic engine for "tell me when X changes" (course
  deadlines, low stock, project risk, GitHub activity, flight changes, club
  announcements) with deterministic conditions, cooldowns and dedup. `/track`
  and `/trackers`. Trackers feed the existing proactive policy.
- **Creation** — natural-language create/link/update/organize over the existing
  domain services ("add CS188 Project 2 due Friday", "add milk to groceries",
  "link this to CS188", "organize this"). `/add`, `/link`, `/organize`; no
  duplicate records and no schema knowledge required.

## Architecture

```
User
  -> Telegram / CLI / MCP
  -> AI Butler / Agent Runtime (optional)
  -> ExecutiveService
       |- Context        |- Projects      |- Scheduler
       |- Memory         |- Tasks         |- Optimizer
       |- Courses        |- Web Knowledge |- Proactive engine
       |- Safety / Audit / Idempotency / Recovery
  -> Actions / Notifications
```

**The LLM interprets and phrases. Python decides and executes.** No model can
write files, run shell commands, alter the calendar, send messages, make
purchases, bypass confirmation, or turn inferred information into a hard
constraint. Every side effect goes through
`safety → permission → idempotency → execution → audit`.

See `docs/architecture/current-state.md` for the full, current implementation
map and `docs/architecture/` for the per-milestone design notes. User guides:
`docs/usage/telegram.md`, `docs/usage/topics.md`, `docs/usage/settings.md`.

## Safety model

- **Deny-by-default Telegram** when the allow-list is empty.
- **Consequent-external actions** (calendar writes/deletes, Telegram sends, file
  moves/deletes) are confirmation-gated.
- **No purchases, bookings or arbitrary messaging** are implemented.
- **Web content is data**, never instructions; private/local URLs are blocked.
- **Memory writes pass a gate** that rejects secrets and injection-like text and
  keeps inferred memories soft.
- **MCP `readonly`** is side-effect free and enforced at dispatch.
- Secrets are redacted from logs and error messages.

## Quick start

```bash
git clone <your-fork> butler && cd butler
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp config.example.toml ~/.config/butler/config.toml   # then edit
./butler.sh config-check        # validate configuration
./butler.sh health              # subsystem status
./butler.sh bot                 # run the Telegram bot
./butler.sh start               # scheduler + monitor (foreground service)
```

Set secrets via environment variables (`BUTLER_TELEGRAM_TOKEN`,
`BUTLER_LLM_KEY`, `BUTLER_HA_TOKEN`, `BUTLER_REMOTE_TOKEN`) rather than the
config file.

## Deploy to a Raspberry Pi

See **[docs/deployment/raspberry-pi.md](docs/deployment/raspberry-pi.md)** for
the full guide (OS, Python, venv, config, secrets, database, Telegram, Google
Calendar, systemd, logs, health, backup, update, rollback, troubleshooting).

Recommended production method: **systemd** (`butler-bot.service` for Telegram,
`butler.service` for the scheduler/monitor), with data on the NVMe SSD.

## Configuration

`~/.config/butler/config.toml` (override path with `BUTLER_CONFIG`). Every
section is documented in `config.example.toml`: `[user]`, `[storage]`,
`[telegram]`, `[backup]`, `[ai]`, `[scheduler]`, `[planner]`, `[web]`,
`[optimizer]`, `[memory]`, `[proactive]`, `[home_assistant]`, `[routines]`,
`[timeline]`.

`./butler.sh config-check` validates and prints first-run hints.

## Commands

```bash
./butler.sh health              # overall + per-subsystem status
./butler.sh security            # configuration security posture
./butler.sh backup              # snapshot the database
./butler.sh backups             # list snapshots
./butler.sh restore <file>      # restore a snapshot (safe: refuses outside backup dir)
./butler.sh version             # product version
```

Domain commands include `day week now tasks task done skip defer cancel resume
why whythis undo reschedule briefing review courses course checkcourses pantry
food recipe grocery context where around mcp bot monitor daemon`. Append `--json`
for machine-readable output.

## Testing

```bash
.venv/bin/python tests/run_acceptance_m8.py     # final product acceptance
.venv/bin/python tests/run_acceptance_product.py  # deterministic product benchmark
.venv/bin/python tests/run_acceptance_final.py  # everything + summary
.venv/bin/python tests/run_acceptance_real_world.py  # integration + live probes
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m compileall -q butler
```

All acceptance suites are deterministic and offline; live external services are
never required. `run_acceptance_real_world.py` probes live services only when
they are configured and reports `PASS` / `FAIL` / `BLOCKED` / `SKIPPED` (it
never turns "not configured" into a pass). A developer mode uses fake
providers, frozen clocks and mock interfaces so the full suite runs without
Telegram, Google Calendar, an LLM or the web.

## Backup & restore

`./butler.sh backup` writes a WAL-checkpointed snapshot under `[backup] dir`.
`./butler.sh restore <file>` takes a pre-restore snapshot, then replaces the
live database; it refuses files outside the backup directory. Restores never
silently overwrite without a rollback point.

## Current limitations

- Telegram is the only chat front-end (voice is a future task).
- The LLM is optional; without it replies are deterministic state, not prose.
- Google Calendar requires a one-time OAuth `connect`.
- No mobile app; MCP is the integration path for other agent runtimes.
- Purchases, bookings and arbitrary outbound messaging are intentionally not
  implemented.

## Roadmap / future work

Future work is maintenance and enhancement, not new architecture phases:
richer LLM phrasing, more proactive detectors, optional voice input, additional
integrations, and continued hardening. See the release checklist for the
operational process.
