# AI Butler integration — architecture & boundary (M1)

Status: M1 integration spike. Describes how Pi Butler and
[AI Butler](https://github.com/LumabyteCo/aibutler) fit together, the exact
verified contract between them, and what must never move across the boundary.

Decision (from the mission): **do not** rewrite Pi Butler around OpenClaw,
**do not** copy AI Butler wholesale into this Python repo, and **do not**
maintain three agent layers. Preferred split:

| Layer | Owner | Responsibility |
|-------|-------|----------------|
| Generic agent runtime / orchestration | **AI Butler** (Go) | LLM loop, tool calling, model/provider routing, MCP client, capabilities, audit plumbing |
| Domain intelligence & executive planning | **Pi Butler** (Python) | Life model, scheduler/solver, hard constraints, projects, courses, memory, deterministic execution, safety/permissions/idempotency |
| Presentation / transport | Telegram, CLI, OpenClaw | unchanged |

AI Butler is the *hands-and-loop*; Pi Butler is the *brain and the brakes*.

## 1. Verified AI Butler contract

Verified against AI Butler source, commit `c35d3af` (2026-07-08), Go 1.26
(`module github.com/LumabyteCo/aibutler`), cloned to `/tmp/opencode/aibutler`.

AI Butler is an **MCP client over stdio**:

- `internal/mcp/mcp.go:13` `ServerConfig{Name, Command, Args, Env, Transport}`;
  `Transport` defaults to stdio.
- `internal/mcp/transport.go:34-45` spawns `exec.CommandContext(command, args...)`,
  sets `cmd.Env = append(os.Environ(), env...)`, and speaks newline-delimited
  JSON-RPC on the child's stdin/stdout.
- Handshake (`internal/mcp/mcp.go:74`): `initialize` with
  `protocolVersion:"2024-11-05"` and `clientInfo{name:"aibutler",version:"0.1.0"}`,
  then `tools/list` (`internal/mcp/mcp.go:90`). It does **not** send
  `notifications/initialized`.
- Tool naming (`internal/mcp/tools.go:61`): each discovered tool is registered
  as **`mcp.<server>.<tool>`**; a generic `mcp.call` tool (`tools.go:96`) calls
  `{server, tool, args}`. Capability `mcp.call` is `AuditFull`
  (`internal/capability/defaults.go:33`).
- Config (`internal/config/config.go:128-138`, root field `yaml:"mcp"`):
  ```yaml
  mcp:
    servers:
      - name: butler
        command: /home/tingli/butler/.venv/bin/python
        args: ["-m", "butler.mcp_stdio"]
        env:
          PYTHONPATH: /home/tingli/butler
          BUTLER_MCP_PROFILE: readonly
          BUTLER_CONFIG: /home/tingli/.config/butler/config.toml
  ```
  (`vault_env` maps vault keys to env var names; no `cwd` field exists, so
  `PYTHONPATH` is required for `-m butler.mcp_stdio`.)

**Compatibility:** Pi Butler already speaks this exact protocol
(`butler/mcp.py`: `PROTOCOL="2024-11-05"`, `tools/list`, `tools/call`, results
as `content[]` text blocks with optional `isError`). The spike test
`tests/run_acceptance_mcp_readonly.py` replays AI Butler's handshake and parse
rules against the real server, and an end-to-end stdio run was verified with a
clean stdout (no stray logging).

## 2. MCP surfaces (profiles)

`butler/mcp.py` selects a **profile** via `MCPServer(profile=...)` or the
`BUTLER_MCP_PROFILE` env var (default `full`). Profiles are disjoint, so a
read-only client cannot reach a mutating tool even by guessing its name
(enforced in `_call_tool`, not just hidden from `tools/list`).

- **`full`** — the historical 51-tool surface for OpenClaw. Unchanged.
- **`readonly`** — the M1 executive surface for AI Butler (10 tools):

| Tool | Returns | Side effects |
|------|---------|--------------|
| `get_time` | local iso/date/time/timezone + day window | none |
| `get_context` | `ContextEngine.snapshot()` | none |
| `get_day` | committed active plan, else read-only preview | none |
| `plan_day` | fresh read-only proposal (`committed:false`) | none |
| `get_schedule` | committed active plan from the DB | none |
| `get_tasks` | active tasks | none |
| `get_courses` | tracked courses | none |
| `get_projects` | stable scaffold (real model in M3) | none |
| `find_available_time` | free waking intervals (`day_offset`, `min_minutes`) | none |
| `get_week` | read-only N-day preview | none |

Not yet exposed (designed, gated, later milestones): task create/update/complete,
schedule/defer/reschedule, calendar write, project/milestone update. Every
future action must pass the existing `SafetyPolicy → permission → idempotency →
execution → audit` path in `AgentRuntime`.

## 3. Failure & fallback behaviour

- **AI Butler down / not configured:** Pi Butler is unaffected — CLI, Telegram
  and OpenClaw keep working on the same `Container`. The readonly surface is
  opt-in via config.
- **Pi Butler MCP down:** AI Butler sees a failed `initialize`/`tools/list` and
  reports no `mcp.butler.*` tools; it must degrade to its own capabilities
  rather than invent schedule facts.
- **Pi Butler internal failure (DeepSeek, Google, network):** handlers return
  live state or an explicit error, never a fabricated answer (AGENTS.md rule).
  `plan_day`/`get_day`/`get_week` call `_maybe_sync()`, which swallows calendar
  outages and plans from cached events.
- **Timeout/restart:** AI Butler's client reconnects and re-issues `tools/list`
  (`internal/mcp/mcp.go:156` `RefreshTools`); the stdio server is stateless
  between lines, so reconnect is safe.

## 4. Security model

- The readonly profile is **deny-by-default for side effects**: only
  side-effect-free tools are listed *and* dispatchable.
- Pi Butler remains the single authority for hard constraints, permissions,
  confirmation and idempotency. AI Butler may *propose*; Pi Butler disposes.
- Secrets stay in Pi Butler's own config/env. AI Butler's `env`/`vault_env`
  should carry only `PYTHONPATH`, `BUTLER_CONFIG`, `BUTLER_MCP_PROFILE` — never
  `DEEPSEEK_API_KEY` or Google credentials.
- Audit: MCP calls are currently lenient (no per-call audit) to preserve
  existing clients. When mutating tools are exposed, they must go through
  `AgentRuntime` so `audit`/`idempotency` apply.

## 5. What must NOT move into AI Butler

- The pure solver (`butler/schedule.py`) and `Planner` day/week planning.
- Hard-constraint semantics, sleep windows, deadline/feasibility rules.
- `SafetyPolicy`, `audit`, `idempotency`, `recovery`/undo.
- The life model: tasks, events, courses, projects, routines, memory.
- Google Calendar / course-feed write paths.
- The no-canned-replies guarantee (`butler/chat.py`).

AI Butler owns *orchestration and model access*; it must not become a second
source of truth for the user's life state.

## 6. Migration strategy (phased)

1. **M1 (this doc):** readonly MCP surface + protocol-compat tests. Pi Butler
   keeps its own runtime; AI Butler can *read*.
2. **Later:** expose mutating tools one at a time behind `AgentRuntime`, with
   confirmation for `CONSEQUENT_EXTERNAL`, and run both agent layers in shadow
   mode before trusting AI Butler as the default loop.
3. **Only after parity + benchmark (M13/M16):** consider retiring the redundant
   Python LLM-loop pieces (see §7). Telegram→OpenClaw migration stays last.

## 7. Redundancy analysis (Phase 0)

If AI Butler is adopted as the generic runtime, these Pi Butler parts become
*redundant as an agent loop* but remain authoritative as domain logic:

| Pi Butler component | Becomes redundant | Must remain |
|---------------------|-------------------|-------------|
| `butler/agent/intent.py` LLM-fallback intent parser | LLM intent parsing (AI Butler does this) | deterministic `decider` parsing |
| `butler/agent/prompts.py` tool-selection prompt + JSON parser | model prompting / tool choice | — (can be deleted once AI Butler owns the loop) |
| `butler/agent/runtime.py` LLM plan→tool loop | the *orchestration* loop | the gate/idempotency/audit pipeline it wraps |
| `butler/agent/session.py` in-memory turns | conversation memory for the loop | pending-confirmation state, if not delegated |
| `butler/agent/advisory.py` | — | nothing (advisory only) |
| `butler/decider.py` `resolve` if-chain | — | yes (deterministic fallback + slash) |
| `butler/agent/registry.py`, `tools.py`, `mcp_tools.py` | — | yes (single catalog; already feeds MCP) |
| `butler/schedule.py`, `planner.py`, `safety.py`, `audit.py`, `idempotency.py` | — | yes (the brakes) |

Net: adoption removes the *Python LLM loop*, not the Python domain layer.
The registry/MCP boundary added in M1/M2 is exactly the seam that makes this
possible without a rewrite.

## 8. Verification

```
.venv/bin/python tests/run_acceptance_mcp_readonly.py   # 30 checks (M1)
.venv/bin/python tests/run_acceptance_mcp.py            # 19 checks (parity)
# end-to-end stdio, as AI Butler connects:
printf '%s\n' '<initialize>' '<tools/list>' '<tools/call get_time>' \
  | BUTLER_MCP_PROFILE=readonly .venv/bin/python -m butler.mcp_stdio
```
