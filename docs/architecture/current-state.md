# Butler — Current Architecture (Phase 7 / M0 audit)

Status snapshot before the Phase 7 agent work. Generated as the M0 deliverable:
audit the existing system, record the baseline, and identify what must change.

## 1. What Butler is today

Butler is a **local, single-user personal assistant** for a Raspberry Pi / Linux
box. It is reachable through three front-ends over one shared in-process
`Container`:

| Front-end | Entry point | Notes |
|-----------|-------------|-------|
| CLI | `python -m butler.cli <cmd> --json` | scripted/ops surface |
| Telegram bot | `butler-bot.service` → `butler.cli bot` | primary conversational UI |
| Remote HTTP | `butler/remote.py` + `Container.remote_route` | thin REST wrapper |
| MCP | `butler/mcp.py`, `mcp_stdio.py` | 51 tools exposed to OpenClaw |

Core philosophy already present: **the LLM reasons, deterministic code acts.**
`butler/chat.py` produces live, grounded prose (AGENTS.md hard rule: no canned
replies). `butler/agent.py` is an advisory-only LLM wrapper (interpret /
prioritise) that never schedules. `butler/schedule.py` is a pure, no-LLM,
no-I/O constraint solver. `butler/engine.py`, `safety.py`, `audit.py`,
`idempotency.py`, `recovery.py` form the deterministic guard-rail layer.

## 2. Module map

### Core wiring
- `butler/core.py` — `Container`: builds exactly one `Config`, `DB`, `Engine`,
  `Planner`, `Chat`, and all Phase 3–6 subsystems; owns `remote_route`.
- `butler/__init__.py` — re-exports `Container`, `Config`, `Engine`, `Trash`,
  `Search`, `Organizer`, `Decider`, `Intent`; `__version__ = "0.1.0"`.

### Input / reasoning
- `butler/decider.py` (1676 lines) — deterministic NLU + intent dispatch.
  `Intent` dataclass (lines 33–41): `kind`, `target`, `query`, `params`, `raw`,
  `plan` (dead, never assigned), `scope`. `parse()` (126–353) is a staged
  deterministic pipeline (normalize → slash → exact keyword → regex chain,
  first match wins); **no LLM in parse/`_slash`**. `resolve()` (513–698) is a
  long `if k == ...` dispatch. `_gate()` (82–123) is the safety boundary; it
  reads `getattr(intent, "confirmed", False)`, which is **always False** because
  `Intent` has no `confirmed` field.
- `butler/chat.py` — live LLM replies grounded in a live-state context string.
- `butler/agent.py` — advisory LLM helper (interpret / prioritise).
- `butler/context.py` — `ContextEngine.snapshot()` / `.describe()`: deterministic
  aggregation of calendar, tasks, courses, food, files, presence, timeline.

### Planning / scheduling
- `butler/schedule.py` — pure solver (`Task`, `Event`, `Slot`, `PlanState`,
  `free_intervals`, `solve`, `diff_old`). No LLM/I/O.
- `butler/planner.py` (1285 lines) — DB + Google Calendar wrapper over the
  solver: `plan_day`, `plan_week`, `what_now`, `explain_now`, `why`,
  `reschedule`, `undo`, task lifecycle, `sync_google` (multi-calendar),
  `_dedupe_events`, `sync_course_events`, `reconcile_calendar`.
- `butler/scheduler.py` — stdlib daemon-thread periodic jobs (reindex, backup,
  digest, gcal sync) via `_cadence_seconds`.

### Domains
- `butler/course.py` — `CourseIntelligence`: course CRUD, page/PDF/ICS scraping,
  LLM assignment extraction, `sync_page_assignments`, `_task_fields`
  (descriptive titles), `_classify_filename`, calendar discovery.
- `butler/gcal.py` — Google Calendar client: OAuth token, `list_events`,
  `read_calendar_ids`, `create_event`, `update_event`, `delete_event`.
- `butler/food.py`, `foodplan.py`, `house.py` (Home Assistant), `nas.py`,
  `timeline.py`, `routines.py`, `executive.py` (briefing/review), `proactive.py`
  (cadence nudges), `motivation.py`, `links.py`, `extract.py`.

### Persistence
- `butler/db.py` (1553 lines) — SQLite schema + migrations. Key tables:
  `events`, `tasks`, `task_gcal`, `courses`, `course_documents`, `plans`,
  `audit`, `idem`, `timeline`, `files`, `settings`, `shopping`, `rewards`.
- `butler/config.py` — TOML config (`~/.config/butler/config.toml`) + env
  overrides; `ensure_dirs()`, `to_dict()`.

### Guard-rails (Phase 6)
- `butler/safety.py` — `ActionClass` (READ / LOW_RISK_WRITE /
  CONSEQUENT_EXTERNAL), deterministic `_KNOWN_RISK` table, `SafetyPolicy.check`.
- `butler/audit.py` — append-only structured trail + `redact()`.
- `butler/idempotency.py` — `make_key`, `Idempotency.once` (exactly-once).
- `butler/recovery.py` — `undo_task`, DB backup/restore.
- `butler/engine.py` — filesystem SAFETY layer (path sandbox).
- `butler/retry.py`, `health.py` — rate-limit / circuit breaker / modes.

### Front-ends / integration
- `butler/telebot.py` (1236 lines) — python-telegram-bot handlers (slash 102–144,
  `/settings` 145, free text 148, media, callbacks). Contains a mix of
  structured renderers and some candidate canned conversational strings
  (lines ~741, 772, 838, 866, 874, 917, 1102, 1206, 1211).
- `butler/mcp.py` (359 lines) — `_tools_spec` (31–135) is a **hand-maintained
  list of exactly 51 tools**, dispatched by string name in `_call_tool`; **no
  safety / audit / idempotency gating** on this path. `VERSION = "1.2.0"`.
- `butler/cli.py`, `remote.py`, `monitor.py`.

## 3. Capabilities (working)

- File search / organize / trash / recover / dedupe over managed roots.
- Semantic + hybrid search, indexing, resume detection.
- Day / week planning from tasks + hard events using the pure solver.
- Google Calendar sync: dedicated "Butler Tasks" target calendar plus read of
  personal calendars (`google_read_calendars`), overlap dedupe/merge.
- Course intelligence: CS188 page + ICS sync, assignment extraction, 22 tasks.
- Food inventory, recipes, meal planning, NAS ingest, Home Assistant presence.
- Executive briefing/review, proactive nudges, routines, timeline.
- Reliability: audit, idempotency, retry/circuit-breaker, degraded/offline
  modes, DB backup/restore, undo.
- OpenClaw MCP bridge exposing 51 tools.

## 4. Baseline (M0)

Recorded on `fbbfe0e`, working tree clean:

- 17 acceptance suites, **559 checks, 0 failed**.
- `tests/test_schedule.py`: **11 unittests OK**.
- `python -m compileall -q butler`: OK.
- MCP tool count: **51**.
- Bot service active; scheduler not run under `butler-bot.service`.

After M1 (`tests/run_acceptance_p70.py` added): **18 suites / 596 checks,
0 failed**, unittests still OK.

After M2 (`tests/run_acceptance_p71.py` added): **19 suites / 632 checks,
0 failed**, unittests still OK.

Command:
```
.venv/bin/python tests/run_acceptance_*.py      # each prints N/N passed
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m compileall -q butler
```

## 5. Gaps / limitations

1. **No unified typed intent.** ~~`decider.Intent` is untyped (`params: dict`),
   has no `confirmed`, no confidence, no provenance, and a dead `plan` field.~~
   **Resolved in M2**: `decider` emits the canonical `butler.agent.models.Intent`
   (typed, with `channel`/`needs_confirmation`); the dead `plan` field is gone.
2. **No tool abstraction.** ~~Every subsystem is called directly from the giant
   `Decider.resolve` if-chain, `telebot.py`, `mcp.py`, and `core.remote_route`.
   MCP hand-maintains a separate 51-entry list with **no safety gate**.~~
   **Partially resolved in M2**: MCP derives its 51-tool schema and dispatch from
   one registry (`butler/agent/mcp_tools.py`); the decider is reachable through
   the agent runtime via a single delegated-gate bridge tool. The giant
   `resolve` if-chain itself is intentionally untouched (deferred to M3+).
3. **No shared context builder for agents.** `ContextEngine.snapshot()` exists
   but there is no single "context bundle" passed to reasoning/tools.
4. **No session state.** Conversation/turn state lives ad-hoc in `TelegramBot`
   fields (`pending`, `_pending_urls`, `_await_url`, `_url_seed`).
5. **Reasoning is not an agent loop.** There is no place where an LLM can select
   a typed tool and have the deterministic layer validate + gate + audit it.
6. **Hard-coded routing tables.** `_KNOWN_RISK`, decider regexes, and MCP tool
   names are three parallel, manually-synced lists. **Partially resolved in M2**:
   the MCP tool list is now derived from the registry; `_KNOWN_RISK` and the
   decider regex chain remain (classification was intentionally left unchanged to
   preserve offline/degraded semantics — see §9).
7. **No scenario benchmark.** Tests are per-feature acceptance scripts; there is
   no agent-level behavioral benchmark (`tests/scenarios/`).
8. **Memory is flat.** Preferences/settings are stored but there is no gated,
   typed Memory 2.0; personal facts can leak into code/config.

## 6. Proposed Phase 7 architecture (target)

```
butler/agent/
  models.py    # Intent, ToolCall, ToolResult, AgentReply, ContextBundle
  errors.py    # AgentError hierarchy
  registry.py  # Tool + ToolRegistry (typed schema, ActionClass, side_effect)
  context.py   # ContextBuilder -> ContextBundle (from ContextEngine, no LLM)
  session.py   # Session + SessionStore (turns, pending confirmations)
  intent.py    # IntentParser: deterministic first, LLM fallback, validated
  tools.py     # adapters registering existing subsystems as typed Tools
  prompts.py   # system / intent prompt templates
  runtime.py   # AgentRuntime: parse -> plan tool -> gate -> execute -> audit
```

Invariants:
- The LLM may only **propose** a `ToolCall`; `AgentRuntime` validates args,
  consults `SafetyPolicy`, applies `Idempotency`, executes, and `Audit`s.
- `READ` tools run freely; `LOW_RISK_WRITE` runs and is audited;
  `CONSEQUENT_EXTERNAL` requires confirmation and is deny-by-default.
- Existing deterministic slash commands remain and act as a fallback when the
  LLM is unavailable.
- No heavy framework is introduced until proven necessary; M1 uses stdlib
  dataclasses only.

## 7. Milestones (Phase 7)

- **M0** audit + baseline + this document. ✅
- **M1** agent core: typed `Intent`, `ContextBuilder`, session state, typed
  tool registry, gated runtime. ✅ (implemented as `butler/agent/`; 34 tools,
  see §8)
- **M2** migrate `Decider` intents to tools (keep slash commands + fallback). ✅
  (canonical `Intent`, registry-extended `Tool`, decider bridge, registry-driven
  MCP catalog; see §9)
- **M3** Memory 2.0 (gated, typed, validated).
- **M4** Project / Goal / Milestone model.
- **M5** web knowledge / Crawl4AI evaluation.
- **M6** GitHub integration.
- **M7** `SchedulerBackend` abstraction.
- **M8** Timefold benchmark. **M9** OR-Tools benchmark.
- **M10** global multi-day optimization.
- **M11** Executive Agent. **M12** proactive standing intentions.
- **M13** Telegram Settings Center. **M14** 50+ scenario benchmark.
- **M15** perf + failure testing. **M16** docs + release hardening.

After every milestone: full regression, new acceptance tests, compile/import
checks, `git diff --check`, secret scan, backward-compat check, commit.

## 8. M1 as implemented

`butler/agent/` (the old advisory `butler/agent.py` moved to
`butler/agent/advisory.py` and re-exported as `Agent`):

| Module | Responsibility |
|--------|----------------|
| `models.py` | `Intent`, `ToolCall`, `ToolResult`, `ContextBundle`, `AgentReply` |
| `errors.py` | typed `AgentError` hierarchy |
| `registry.py` | `Tool` / `Param` / `ToolRegistry` with deterministic validation |
| `context.py` | `ContextBuilder` → `ContextBundle` (wraps `ContextEngine`) |
| `session.py` | `Session` / `SessionStore` + `PendingAction` confirmations |
| `intent.py` | `IntentParser` (deterministic, optional LLM fallback) |
| `tools.py` | `build_default_registry` → 34 typed adapters over subsystems |
| `prompts.py` | tool-selection prompt + strict JSON tool-choice parser |
| `runtime.py` | `AgentRuntime`: parse → plan → validate → gate → idempotency → audit |

Control loop and invariants:

- `AgentRuntime.run(message)` → `IntentParser.parse` → `_plan` (intent kind →
  tool, with aliases) → `execute`.
- `execute` validates args, consults `SafetyPolicy` (`READ` free,
  `LOW_RISK_WRITE` audited, `CONSEQUENT_EXTERNAL` deny-by-default and
  confirmation-required), applies `Idempotency` to side-effecting tools, records
  the outcome in `Audit`, and returns a `ToolResult`.
- A denied external action is parked as a `PendingAction` in the session; the
  next `run(..., confirm=True)` executes it.
- `AgentReply.text` stays empty unless a renderer/LLM is explicitly enabled —
  the runtime never invents a sentence.
- `Container.agent` is built defensively (`try/except` → `None`); all legacy
  paths (`decider`, Telegram, MCP, remote) are untouched and still pass.

Deferred to later milestones: M3 makes sessions/memory durable and splits the
decider's per-kind handlers into typed tools; M13/M14 add the settings UI and
scenario benchmark.

## 9. M2 as implemented

**One canonical Intent.** `butler/agent/models.py::Intent` is the single model.
`decider.py` imports it (`from .agent.models import Intent`) and no longer owns a
parallel dataclass; `from .decider import Intent` still works. `Intent` gained
`channel` (origin: cli/telegram/mcp/agent) and `needs_confirmation`; the dead
`plan` field was removed. `Decider.parse(message, channel="")` wraps `_parse` and
stamps the channel. `_gate` sets `intent.needs_confirmation = True` when policy
defers a confirmation-required action.

**Registry is the catalog.** `Tool` gained `aliases`, `mcp_name`,
`needs_confirmation`, `delegated_gate`, `hidden`, plus `Tool.mcp_schema()`.
`ToolRegistry` resolves aliases in `get`/`has`, hides `hidden` tools from
`schema()`, and adds `find_mcp()`, `mcp_schema()`, `merge(prefix=...)`.

**Decider bridge.** `butler/agent/tools.py` registers one hidden `decider` tool
(`action="chat"`, `delegated_gate=True`) that calls
`Decider.resolve(intent, user)` — so every deterministic intent keeps its exact
result shape and its own `SafetyPolicy` gate. `AgentRuntime.run_intent(intent,
user, confirm=False)` lets front-ends that already parsed an intent (Telegram NL)
route through the one control loop. `_plan` sends `source=="llm"` intents to the
matching typed tool and everything else through the bridge. `execute` special-
cases `delegated_gate` tools: it calls the handler directly and does **not**
validate/gate/audit a second time (single authoritative boundary).

**MCP consumes the registry.** New `butler/agent/mcp_tools.py`
(`build_mcp_registry`) defines all 51 MCP tools once as `Tool`s under internal
`mcp_<name>` names with `mcp_name=<client name>`. `butler/mcp.py` now derives
both `tools/list` (`mcp_schema()`) and dispatch (`find_mcp()`) from it; `VERSION`
is `1.3.0`. Client-visible names and payloads are unchanged, and MCP dispatch
still calls handlers directly (no runtime gate) to preserve existing clients.

**Routing compatibility.** CLI and Telegram slash commands are unchanged. Free
text in Telegram goes through `AgentRuntime.run_intent` (with a fallback to the
decider if the agent raises). Return shapes are identical to pre-M2.

**Safety.** No double gate: the decider bridge's own `_gate` is the single
boundary for deterministic intents, and the runtime gate is the single boundary
for LLM/typed tools. `safety._KNOWN_RISK` was **intentionally not modified**:
adding previously-unknown kinds as `read` would silently allow them in offline
mode, changing behavior; classification hardening is deferred.

Deferred to M3: per-kind typed tools (replacing the bridge), a typed `params`
model for structured args, and completing `_KNOWN_RISK`.

## 10. Phase 0 (executive-assistant mission) — audit update

The mission was re-scoped toward a serious always-on personal executive
assistant and an **AI Butler** integration. The M0–M2 Phase 7 work above stands
as implemented history. The new milestone scheme is tracked in
`docs/architecture/ai-butler-integration.md` (M1 = AI Butler spike + read-only
MCP boundary; M2 = structured agent interface; M3 = project intelligence; …).

**Actual architecture** is unchanged from §1–§2 except that the MCP catalog is
now registry-derived and profile-aware (`butler/agent/mcp_tools.py`,
`butler/mcp.py`). One `Container`, three front-ends (CLI, Telegram, MCP), one
pure solver, one safety/audit/idempotency layer.

**AI Butler boundary (verified):** AI Butler is an MCP *client over stdio*; Pi
Butler is the domain/scheduling authority. The integration seam is the MCP
server, exposed as two disjoint profiles: `full` (historical 51 tools, OpenClaw)
and `readonly` (15 side-effect-free executive tools for AI Butler; M3 added the
four project reads). See
`docs/architecture/ai-butler-integration.md` for the verified protocol, config,
failure behaviour, security model and migration plan.

**M2 typed semantic contract:** `butler/agent/semantic.py` (request/result
models, HARD-vs-SOFT + explicit-vs-inferred constraints, ambiguity, bounded
conversation context), `temporal.py` (deterministic, DST-safe phrase
resolution), `context.py` (`build_snapshot`, JIT bounded context),
`interpret.py` (`DeterministicInterpreter`, strictly-validated
`LLMInterpreter`), `service.py` (`ExecutiveService`: validate → resolve →
snapshot → existing domain logic → `AgentResult`), `session.py` (bounded
focus/recent entities), and the readonly `executive_ask` MCP tool. Mutating
actions are gated (`needs_confirmation`) and never executed here. The legacy
`decider` remains the fallback; no second agent loop was introduced.

**Redundancy if AI Butler is adopted:** only the Python *LLM loop* becomes
redundant — `butler/agent/intent.py` LLM fallback, `prompts.py`, and the
orchestration half of `runtime.py`/`session.py`. The deterministic decider, the
typed registry, the solver/planner, and the safety/audit/idempotency layer
remain authoritative. The registry/MCP seam added in M1/M2 is what makes this
possible without a rewrite.

**Baseline after M2 (this mission):** `run_acceptance_mcp_readonly.py` (30
checks) and `run_acceptance_p72.py` (76 checks) cover the M1/M2 boundary; MCP
`VERSION = "1.4.0"`; the `full` profile is still exactly 51 tools. The
previously time-of-day-dependent proactive checks (`run_acceptance_p3.py`,
`run_acceptance_p45.py`) are now pinned with a test-local frozen clock and are
deterministic at any hour without weakening production quiet hours.

Command:
```
.venv/bin/python tests/run_acceptance_*.py      # each prints N/N passed
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -m compileall -q butler
```

## 11. M3 as implemented (project intelligence)

The mission milestone M3 (distinct from the older Phase 7 M3 "Memory 2.0"
label) is **project intelligence**: a durable Goal → Project → Milestone → Task
model. Full design in `docs/architecture/project-intelligence.md`.

- **Schema.** `butler/db.py` adds `projects`, `milestones`, `task_deps`, and
  three nullable `tasks` columns (`project_id`, `milestone_id`,
  `remaining_minutes`, all default 0). Migration is in-place and idempotent;
  the `tasks(project_id)` index is created after the `ALTER TABLE` loop because
  `SCHEMA` runs first.
- **Domain.** `butler/projects.py::ProjectIntelligence`: effort-based progress
  (never task counts; `unknown` without estimates), explainable weighted risk,
  validated dependency DAG (cycles rejected, inferred edges advisory),
  read-only workload/available-minutes, and deterministic project proposals.
  Wired into `Container` as `self.projects`.
- **Agent layer.** New `ActionKind`s (`PROJECT_STATUS/WORKLOAD/RISK/
  DEPENDENCIES/NEXT`, `CREATE_PROJECT`), `ContextSnapshot.projects`,
  interpreter keyword classification + `EntityType.PROJECT`, and service
  handlers. `CREATE_PROJECT` returns `NEEDS_CONFIRMATION` with a proposal and
  never writes.
- **MCP.** Read-only tools `get_project`, `get_project_workload`,
  `get_project_risk`, `get_project_dependencies`; the `get_projects` scaffold
  is replaced with a real handler. `full` stays exactly 51 tools.
- **Safety.** `project_create` / `project_update` / `project_link` /
  `project_milestone` / `project_dependency` are `low_risk_write`.
- **Backward compatibility.** Ordinary tasks, courses, assignments, the
  scheduler, the `full` MCP profile and the readonly profile all keep working.
  Assignments are not forced into projects.

**Baseline after M3:** `tests/run_acceptance_m3.py` (93 checks) added;
`run_acceptance_mcp_readonly.py` grows to 32 checks (readonly is now 15 tools);
`run_acceptance_p72.py` still passes with the readonly count updated.
