"""SQLite persistence layer for Butler.

Holds the search index (FTS5), binary-content text chunks, embeddings,
trash registry, duplicate registry, classification, and the operation log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import Config

# Phase 5.0 task lifecycle. The active set is what the planner may place;
# terminal statuses leave the active work set (and are what the Google Calendar
# writer preserves vs. removes). ``done`` is the historical alias for
# ``completed`` and is normalised to it wherever it meets the DB.
ACTIVE_TASK_STATUSES = ("todo", "doing", "scheduled")
TERMINAL_STATUSES = ("completed", "skipped", "cancelled", "deferred", "blocked")
_VALID_TASK_STATUSES = ACTIVE_TASK_STATUSES + TERMINAL_STATUSES

#: Current schema revision. Bumped whenever a migration is added; persisted in
#: ``PRAGMA user_version`` so startup can detect an old/new database.
SCHEMA_VERSION = 9


def normalize_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "done":
        return "completed"
    if s not in _VALID_TASK_STATUSES:
        return "todo"
    return s



def _day_bounds(ts: int) -> tuple[int, int]:
    """Start-of-day and start-of-next-day unix timestamps for a unix ts."""
    try:
        lt = time.localtime(int(ts))
        day_start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                     0, 0, 0, 0, 0, -1)))
        day_end = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                                   0, 0, 0, 0, 0, -1)))
    except Exception:  # pragma: no cover - defensive
        day_start = int(ts)
        day_end = int(ts) + 86400
    return day_start, day_end


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files(
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    ext         TEXT,
    size        INTEGER DEFAULT 0,
    mtime       INTEGER DEFAULT 0,
    hash        TEXT,
    mime        TEXT,
    meta        TEXT,          -- json: author, title, pages, etc.
    category    TEXT,          -- classification label
    is_dir      INTEGER DEFAULT 0,
    parent      TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_cat ON files(category);

CREATE TABLE IF NOT EXISTS chunks(
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    seq     INTEGER DEFAULT 0,
    text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
    chunk_id UNINDEXED, file_id UNINDEXED,
    path UNINDEXED, name UNINDEXED, body, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings(
    file_id   INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model     TEXT,
    dim       INTEGER,
    n_chunks  INTEGER DEFAULT 0,
    vec       BLOB          -- flat float32, one row per file (mean-pooled)
);

CREATE TABLE IF NOT EXISTS trash(
    id          INTEGER PRIMARY KEY,
    orig_path   TEXT UNIQUE NOT NULL,
    name        TEXT,
    trashed_rel TEXT NOT NULL,   -- path inside trash dir
    size        INTEGER,
    reason      TEXT,
    trashed_at  INTEGER,
    restored_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trash_at ON trash(trashed_at);

CREATE TABLE IF NOT EXISTS duplicates(
    group_id  TEXT,
    file_id   INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    is_primary INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dups_group ON duplicates(group_id);

CREATE TABLE IF NOT EXISTS operations(
    id      INTEGER PRIMARY KEY,
    ts      INTEGER,
    user    TEXT,
    action  TEXT,
    target  TEXT,
    dest    TEXT,
    detail  TEXT,
    status  TEXT,     -- planned | confirmed | applied | rejected | failed
    plan_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_ts ON operations(ts);

CREATE TABLE IF NOT EXISTS backups(
    id      INTEGER PRIMARY KEY,
    ts      INTEGER,
    source  TEXT,
    dest    TEXT,
    status  TEXT,
    detail  TEXT
);

CREATE TABLE IF NOT EXISTS links(
    id          INTEGER PRIMARY KEY,
    url         TEXT,
    title       TEXT,
    tag         TEXT,
    status      TEXT,   -- added | unchanged | updated | downloaded | error
    hash        TEXT,
    last_checked INTEGER,
    added       INTEGER,
    path        TEXT,   -- where saved content lives (when downloaded)
    note        TEXT
);

CREATE TABLE IF NOT EXISTS tasks(
    id          INTEGER PRIMARY KEY,
    title       TEXT,
    detail      TEXT,
    deadline    INTEGER,      -- unix ts; 0 = none
    priority    INTEGER,      -- 1 (low) .. 5 (critical); higher = bigger
    est_minutes INTEGER,      -- planned duration
    status      TEXT DEFAULT 'todo',  -- todo | doing | done | skipped
    sort        INTEGER DEFAULT 0,    -- manual tiebreak / creation
    created     INTEGER,
    completed   INTEGER,      -- unix ts when done/skipped
    tags        TEXT,
    note        TEXT,
    -- M3 project intelligence: optional links + explicit remaining effort.
    -- ``project_id``/``milestone_id`` of 0 mean "not part of a project", so
    -- every pre-M3 task keeps working untouched.
    project_id   INTEGER DEFAULT 0,
    milestone_id INTEGER DEFAULT 0,
    remaining_minutes INTEGER DEFAULT 0   -- 0 = derive from est_minutes
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS events(
    id          INTEGER PRIMARY KEY,
    source      TEXT,          -- 'local' | 'google'
    external_id TEXT,          -- gcal event id (dedupe)
    title       TEXT,
    all_day     INTEGER DEFAULT 0,
    start_ts    INTEGER,
    end_ts      INTEGER,
    location    TEXT,
    updated     INTEGER
);

CREATE TABLE IF NOT EXISTS plans(
    id          INTEGER PRIMARY KEY,
    created     INTEGER,
    day_start   INTEGER,
    day_end     INTEGER,
    state       TEXT,          -- 'active' | 'history' | 'applied'
    json        TEXT           -- serialised PlanState (slots + snapshot)
);
CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created);

-- ---------- Phase 3: courses ----------
CREATE TABLE IF NOT EXISTS courses(
    id             INTEGER PRIMARY KEY,
    code           TEXT UNIQUE NOT NULL,
    name           TEXT,
    instructor     TEXT,
    url            TEXT,
    platform       TEXT,
    semester       TEXT,
    calendar_url   TEXT DEFAULT '',   -- public .ics feed for class times
    monitoring_enabled INTEGER DEFAULT 1,
    monitoring_interval INTEGER DEFAULT 3600,   -- seconds
    created_at     INTEGER,
    updated_at     INTEGER
);

CREATE TABLE IF NOT EXISTS course_documents(
    id          INTEGER PRIMARY KEY,
    course_id   INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title       TEXT,
    url         TEXT,
    local_path  TEXT,
    document_type TEXT,        -- lecture | project | reading | exam | spec | announcement
    content_hash TEXT,
    downloaded_at INTEGER,
    version     INTEGER DEFAULT 1,
    external_id TEXT,          -- dedupe key (e.g. web page url / listing hash)
    task_id     INTEGER DEFAULT 0,  -- linked scheduler task id (0 = not yet understood)
    understanding TEXT DEFAULT ''   -- JSON of the assignment model the LLM proposed
);
CREATE INDEX IF NOT EXISTS idx_cdoc_course ON course_documents(course_id);

-- ---------- Phase 3: food ----------
CREATE TABLE IF NOT EXISTS food_items(
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    quantity    REAL DEFAULT 1,
    unit        TEXT DEFAULT '',
    expiration_date INTEGER,        -- unix day-ts of expiry; 0 = none
    opened_date INTEGER,
    category    TEXT,               -- protein | vegetable | dairy | pantry | etc
    storage_location TEXT,          -- fridge | freezer | pantry
    notes       TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_food_exp ON food_items(expiration_date);

CREATE TABLE IF NOT EXISTS shopping_items(
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    quantity    REAL DEFAULT 1,
    unit        TEXT DEFAULT '',
    category    TEXT,
    needed_for  TEXT,               -- recipe / meal referenced
    purchased   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recipes(
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    source      TEXT DEFAULT 'builtin',
    source_url  TEXT DEFAULT '',
    ingredients TEXT DEFAULT '[]',   -- JSON
    steps       TEXT DEFAULT '[]',   -- JSON
    tags        TEXT DEFAULT '[]',   -- JSON
    equipment   TEXT DEFAULT '[]',   -- JSON
    servings    INTEGER DEFAULT 2,
    prep_minutes INTEGER DEFAULT 0,
    cook_minutes INTEGER DEFAULT 0,
    difficulty  INTEGER DEFAULT 2,
    cost        REAL DEFAULT 2.0,
    rating      REAL DEFAULT 0.0,
    favorite    INTEGER DEFAULT 0,
    times_used  INTEGER DEFAULT 0,
    last_used   INTEGER DEFAULT 0,
    created_at  INTEGER DEFAULT 0,
    time_estimated   INTEGER DEFAULT 0,   -- 1 => time is an estimate, not verified
    cost_estimated   INTEGER DEFAULT 0,   -- 1 => cost is an estimate, not verified
    nutrition_source TEXT DEFAULT '',     -- '' = unverified/no source
    UNIQUE(name, source_url)
);
CREATE INDEX IF NOT EXISTS idx_recipes_fav ON recipes(favorite);
CREATE INDEX IF NOT EXISTS idx_recipes_last ON recipes(last_used);

CREATE TABLE IF NOT EXISTS meal_history(
    id          INTEGER PRIMARY KEY,
    recipe_id   INTEGER,
    meal        TEXT DEFAULT '',
    ts          INTEGER DEFAULT 0,
    created_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_meal_history_ts ON meal_history(ts);

-- ---------- Phase 4.3: context timeline ----------
-- A durable, zone-level history of *meaningful* events. Privacy-first: only
-- zone names are stored (never raw GPS coordinates), and no credentials /
-- tokens / full API responses are ever written here. The DB is the single
-- source of truth; recording is passive and never mutates the scheduler.
CREATE TABLE IF NOT EXISTS timeline_events(
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL,
    type        TEXT NOT NULL,          -- zone_change|calendar_start|calendar_end|task_started|task_completed|schedule_change|user_context
    zone_from   TEXT,
    zone_to     TEXT,
    source      TEXT,                   -- home_assistant|google|local|user|scheduler
    external_id TEXT,                   -- dedupe key (calendar event id / task id)
    title       TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tl_ts ON timeline_events(ts);
CREATE INDEX IF NOT EXISTS idx_tl_type ON timeline_events(type);

-- Tiny per-state key/value used to remember the last known zone so changing
-- presence can be detected (only a *change* produces a zone_change event).
CREATE TABLE IF NOT EXISTS tl_state(
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- ---------- Phase 4.4: learned routines & habits ----------
-- A discovered or explicitly-declared recurring habit. It is ONLY ever a soft
-- preference at recommendation time; it never mutates a committed plan and
-- never overrides a hard constraint (Google Calendar / deadlines / sleep).
-- ``state`` drives the lifecycle: candidate -> confirmed (or declined),
-- with user-forgotten routines disabled and old ones decaying to stale.
CREATE TABLE IF NOT EXISTS routines(
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- activity | sequence
    category    TEXT NOT NULL,          -- primary category, or "a->b" for sequence
    zone        TEXT,                   -- preferred zone (activity) / target zone
    weekday     INTEGER NOT NULL,       -- 0..6, or -1 = any day
    start_min   INTEGER NOT NULL,       -- approximate time window (minutes-in-day)
    end_min     INTEGER NOT NULL,
    title       TEXT,
    count       INTEGER DEFAULT 0,      -- observations
    weeks       INTEGER DEFAULT 0,      -- distinct weeks observed
    n_of_m      INTEGER DEFAULT 0,      -- how many of the last M weeks matched
    confidence  REAL DEFAULT 0,         -- deterministic, explainable score 0..1
    first_ts    INTEGER DEFAULT 0,
    last_ts     INTEGER DEFAULT 0,
    state       TEXT DEFAULT 'candidate',  -- candidate|confirmed|declined|disabled|stale
    source      TEXT DEFAULT 'inferred',   -- inferred|explicit
    note        TEXT,                   -- explicit phrase, or short description
    created_at  INTEGER DEFAULT 0,
    updated_at  INTEGER DEFAULT 0,
    signature   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_routines_state ON routines(state);

-- ---------- Phase 4.5: food + schedule + context integration ----------
-- A durable meal *suggestion* (not a committed decision). It is keyed by
-- (day, recipe) so repeated/restarted requests never produce duplicate rows.
-- The suggestion is advisory: it never mutates the schedule and never causes a
-- purchase on its own (grocery additions are explicit user confirmations).
CREATE TABLE IF NOT EXISTS meal_suggestions(
    id          INTEGER PRIMARY KEY,
    day_ts      INTEGER NOT NULL,        -- calendar day-ts
    recipe_id   INTEGER NOT NULL,
    meal        TEXT DEFAULT '',
    budget_minutes INTEGER DEFAULT 0,
    reason      TEXT,
    created_at  INTEGER DEFAULT 0,
    UNIQUE(day_ts, recipe_id)
);
CREATE INDEX IF NOT EXISTS idx_meal_sugg_day ON meal_suggestions(day_ts);

-- ---------- Phase 5.0: task lifecycle + Google Calendar write sync ----------
-- A durable audit of every status transition so "why did this task move?" is
-- answered deterministically, and Butler never loses track of a completion.
CREATE TABLE IF NOT EXISTS task_history(
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    from_status TEXT DEFAULT '',
    to_status   TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_th_task ON task_history(task_id);
CREATE INDEX IF NOT EXISTS idx_th_ts ON task_history(ts);

-- The stable link between a Butler task/block and the Google Calendar event
-- Butler created for it. One row per task (so a task maps to at most one event
-- and a rerun never duplicates an event). External events (no ``butler_managed``
-- marker) are NEVER recorded here and thus never touched by the write path.
CREATE TABLE IF NOT EXISTS task_gcal(
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    gcal_event_id TEXT DEFAULT '',   -- the event Butler created; '' = none yet
    state        TEXT DEFAULT 'none',-- none|synced|pending_create|pending_update|pending_delete
    title        TEXT DEFAULT '',
    start_ts     INTEGER DEFAULT 0,
    end_ts       INTEGER DEFAULT 0,
    last_error   TEXT DEFAULT '',
    created      INTEGER DEFAULT 0,
    updated      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tg_state ON task_gcal(state);

-- Phase 5.1 executive state: a tiny general-purpose key/value table for
-- idempotent delivery markers (daily briefing / daily review) and small
-- durable amounts of state the executive loop needs across restarts.
CREATE TABLE IF NOT EXISTS exec_state(
    id      INTEGER PRIMARY KEY,
    key     TEXT NOT NULL UNIQUE,
    value   TEXT NOT NULL DEFAULT ''
);

-- ---------- Phase 6: reliability, safety & recovery ----------
-- Durable structured audit log. One row per auditable action. ``run_id`` ties
-- a decision to the exact execution attempt (so a retry shares the run_id and
-- the audit trail stays coherent). ``actor`` is the responsible entity
-- (user | scheduler | telegram | cli | proactive | llm); the LLM itself is
-- NEVER an actor on an external side effect — it only proposes, and the
-- deterministic policy boundary records the decision.
CREATE TABLE IF NOT EXISTS audit_log(
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,             -- named operation
    kind        TEXT DEFAULT '',           -- read | low_risk_write | consequent_external
    target      TEXT DEFAULT '',           -- file path / event id / task id / ...
    decision    TEXT DEFAULT 'allowed',    -- allowed | denied | degraded | error
    reason      TEXT DEFAULT '',           -- human-readable rationale
    outcome     TEXT DEFAULT '',           -- ok | failed | partial | not_applied
    detail      TEXT DEFAULT '',           -- json metadata (never secrets)
    idem_key    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);

-- Universal idempotency registry: a dedupe key maps to a finished outcome so
-- a retried/replayed operation returns the SAME result instead of running the
-- side effect twice. Only successful/exhausted operations are stored here.
CREATE TABLE IF NOT EXISTS idempotency(
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    hash        TEXT DEFAULT '',
    actor       TEXT DEFAULT '',
    action      TEXT DEFAULT '',
    status      TEXT DEFAULT 'ok',         -- ok | in_progress | failed | exhausted
    result      TEXT DEFAULT '',           -- json of the canonical payload
    created     INTEGER DEFAULT 0,
    expires     INTEGER DEFAULT 0          -- ts; 0 = never expires
);

-- Scheduler job state: one row per named job so the scheduler knows what is
-- pending, when it last ran, and whether a run is already in flight (guard
-- against overlapping/duplicate runs). This makes the scheduler robust across
-- restarts and against a long-running job being triggered twice.
CREATE TABLE IF NOT EXISTS scheduler_state(
    id           INTEGER PRIMARY KEY,
    job          TEXT NOT NULL UNIQUE,
    cadence      INTEGER DEFAULT 0,        -- resolved cadence in seconds
    last_ts      INTEGER DEFAULT 0,        -- last accepted start
    last_done    INTEGER DEFAULT 0,        -- last successful finish
    last_status  TEXT DEFAULT '',          -- ok | failed | running | skipped
    last_error   TEXT DEFAULT '',
    next_ts      INTEGER DEFAULT 0,        -- next scheduled run
    locked_until INTEGER DEFAULT 0,        -- in-flight lease; 0 = not running
    consecutive_failures INTEGER DEFAULT 0, -- for the circuit breaker
    disabled     INTEGER DEFAULT 0
);

-- Heartbeat / health record. Kept in the DB (not just process memory) so
-- ``butler health`` can report last-alive after a crash, and the scheduler
-- can detect a stale lock (a crashed run) and reclaim it.
CREATE TABLE IF NOT EXISTS heartbeat(
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL UNIQUE,      -- db | scheduler | telebot | gcal | ...
    ts          INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'ok',         -- ok | degraded | down
    note        TEXT DEFAULT '',
    pid         INTEGER DEFAULT 0
);

-- Per-topic configuration for forum topics (topic routing + daily push).
CREATE TABLE IF NOT EXISTS topic_settings(
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    thread_id   INTEGER NOT NULL DEFAULT 0,
    topic       TEXT DEFAULT '',           -- resolved topic title (cache)
    routing     TEXT DEFAULT '',           -- legacy default intent kind for free text
    push_on     INTEGER DEFAULT 0,         -- 1 => push to this topic
    push_time   TEXT DEFAULT '',           -- HH:MM local
    push_freq   TEXT DEFAULT 'daily',      -- daily | weekdays | weekly
    updated_ts  INTEGER DEFAULT 0,
    -- N1: durable TopicProfile (topic = context/view over shared domain data)
    purpose     TEXT DEFAULT '',
    description TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',     -- pending_setup | active | paused | archived
    capabilities TEXT DEFAULT '',          -- JSON {capability: state}
    template    TEXT DEFAULT '',
    last_seen_at INTEGER DEFAULT 0,
    created_at  INTEGER DEFAULT 0,
    pin_message_id INTEGER DEFAULT 0,
    pin_message_version INTEGER DEFAULT 0,
    pin_content_hash TEXT DEFAULT '',
    UNIQUE(chat_id, thread_id)
);

-- N1: lightweight references from a topic to existing domain data. This is a
-- bridge, NOT a graph database: it only stores (topic, target_type, target_id)
-- so two topics can share the same underlying record without copying it.
CREATE TABLE IF NOT EXISTS topic_links(
    id               INTEGER PRIMARY KEY,
    topic_profile_id INTEGER NOT NULL,
    target_type      TEXT NOT NULL,        -- course|project|task|food|meal_plan|...
    target_id        INTEGER DEFAULT 0,
    relation         TEXT DEFAULT 'about',
    confidence       REAL DEFAULT 1.0,
    provenance       TEXT DEFAULT '',
    created_at       INTEGER DEFAULT 0,
    updated_at       INTEGER DEFAULT 0,
    UNIQUE(topic_profile_id, target_type, target_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_topic_links_profile ON topic_links(topic_profile_id);

-- Q13: generic persistent topic behaviors ("when I ask about X, also do Y").
-- One generic shape (trigger/strategy/constraints/scope/persistence); never a
-- per-domain class. Behaviors shape future requests but cannot bypass safety.
CREATE TABLE IF NOT EXISTS topic_behaviors(
    id               INTEGER PRIMARY KEY,
    topic_profile_id INTEGER NOT NULL,
    trigger          TEXT DEFAULT '',
    strategy         TEXT DEFAULT '',      -- JSON shaping flags
    constraints      TEXT DEFAULT '',      -- JSON
    scope            TEXT DEFAULT '',
    persistence      TEXT DEFAULT 'always',-- always | once
    enabled          INTEGER DEFAULT 1,
    priority         INTEGER DEFAULT 0,
    created_from     TEXT DEFAULT '',
    created_at       INTEGER DEFAULT 0,
    updated_at       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_topic_behaviors_profile
    ON topic_behaviors(topic_profile_id);

-- ---------------------------------------------------------------------------
-- N2: universal tracking / trigger engine. One compact Tracker row folds the
-- conceptual Tracker+Trigger split (condition/action are JSON); meaningful
-- events get a durable row with a UNIQUE deterministic key for idempotency.
-- Low-level poll results are NOT stored — only meaningful events.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trackers(
    id              INTEGER PRIMARY KEY,
    name            TEXT DEFAULT '',
    target_type     TEXT DEFAULT '',        -- food_item|project|course|event|task|web_page|github_repo|file_dir|global
    target_id       INTEGER DEFAULT 0,
    target_ref      TEXT DEFAULT '',
    source          TEXT DEFAULT '',        -- provider name
    condition       TEXT DEFAULT '',        -- JSON {type, ...}
    action          TEXT DEFAULT '',        -- JSON {type, ...}
    cadence_seconds INTEGER DEFAULT 21600,
    scope           TEXT DEFAULT 'object',  -- global | topic | object
    priority        TEXT DEFAULT 'medium',
    destination_chat_id   INTEGER DEFAULT 0,
    destination_thread_id INTEGER DEFAULT 0,
    destination_topic_id  INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    state           TEXT DEFAULT 'pending', -- pending|active|paused|degraded|error|disabled|archived
    last_checked_at INTEGER DEFAULT 0,
    next_check_at   INTEGER DEFAULT 0,
    last_state_hash TEXT DEFAULT '',
    last_snapshot   TEXT DEFAULT '',
    last_event      TEXT DEFAULT '',
    last_event_at   INTEGER DEFAULT 0,
    failure_count   INTEGER DEFAULT 0,
    cooldown_until  INTEGER DEFAULT 0,
    one_shot        INTEGER DEFAULT 0,
    completed       INTEGER DEFAULT 0,
    expires_at      INTEGER DEFAULT 0,
    provenance      TEXT DEFAULT '',
    created_at      INTEGER DEFAULT 0,
    updated_at      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trackers_state ON trackers(state);
CREATE INDEX IF NOT EXISTS idx_trackers_enabled ON trackers(enabled);
CREATE INDEX IF NOT EXISTS idx_trackers_next ON trackers(next_check_at);
CREATE INDEX IF NOT EXISTS idx_trackers_target ON trackers(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_trackers_dest ON trackers(destination_topic_id);

CREATE TABLE IF NOT EXISTS tracker_events(
    id           INTEGER PRIMARY KEY,
    tracker_id   INTEGER NOT NULL,
    event_key    TEXT NOT NULL UNIQUE,       -- deterministic idempotency key
    event_type   TEXT DEFAULT '',
    target_type  TEXT DEFAULT '',
    target_id    INTEGER DEFAULT 0,
    summary      TEXT DEFAULT '',
    evidence     TEXT DEFAULT '',            -- JSON
    before_state TEXT DEFAULT '',
    after_state  TEXT DEFAULT '',
    action       TEXT DEFAULT '',            -- JSON action proposal
    candidate_key TEXT DEFAULT '',
    observed_at  INTEGER DEFAULT 0,
    created_at   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tracker_events_tracker ON tracker_events(tracker_id);
CREATE INDEX IF NOT EXISTS idx_tracker_events_type ON tracker_events(event_type);
CREATE INDEX IF NOT EXISTS idx_tracker_events_at ON tracker_events(observed_at);

-- ---------------------------------------------------------------------------
-- N3: small alias table for natural-language resolution. Aliases never copy a
-- record; they only add another name that resolves to an existing domain row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity_aliases(
    id          INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id   INTEGER NOT NULL DEFAULT 0,
    alias       TEXT NOT NULL,
    canonical   TEXT DEFAULT '',
    source      TEXT DEFAULT '',
    confidence  REAL DEFAULT 1.0,
    created_at  INTEGER DEFAULT 0,
    updated_at  INTEGER DEFAULT 0,
    UNIQUE(target_type, target_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_target ON entity_aliases(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_alias_alias ON entity_aliases(alias);

-- Reward library (small treats / rest breaks the agent suggests on completion).
CREATE TABLE IF NOT EXISTS rewards(
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT DEFAULT 'rest',       -- rest | treat
    weight      INTEGER DEFAULT 1,         -- 1..5; scales the suggested reward to task size
    stock_food  TEXT DEFAULT '',           -- if set & treat, add to shopping list when low
    times_used  INTEGER DEFAULT 0,
    last_used   INTEGER DEFAULT 0,
    created_at  INTEGER DEFAULT 0
);

-- Application-level settings (push timezone, reward streak, defaults).
CREATE TABLE IF NOT EXISTS app_settings(
    key     TEXT PRIMARY KEY,
    value   TEXT DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- M3 project intelligence. A project is the durable unit of real work: a goal
-- broken into milestones, carried out by ordinary tasks (linked by id), with an
-- optional dependency DAG. Effort is tracked in minutes so progress can be
-- effort-based rather than a misleading completed/total task count. Every
-- field that may be inferred carries provenance so an inferred deadline/effort
-- can never silently become authoritative.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects(
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    objective   TEXT DEFAULT '',
    status      TEXT DEFAULT 'active',   -- active | paused | completed | archived | cancelled
    priority    INTEGER DEFAULT 3,       -- 1 (low) .. 5 (critical)
    course_id   INTEGER DEFAULT 0,       -- 0 = not tied to a course
    deadline    INTEGER DEFAULT 0,       -- unix ts; 0 = none
    estimated_total_minutes INTEGER DEFAULT 0,
    remaining_minutes INTEGER DEFAULT 0, -- explicit override; 0 = derive from tasks
    progress    REAL DEFAULT -1,         -- 0..1; -1 = unknown/derived
    risk        REAL DEFAULT -1,         -- 0..1; -1 = unknown (last computed)
    repo_url    TEXT DEFAULT '',
    links       TEXT DEFAULT '',         -- JSON [{label,url}]
    refs        TEXT DEFAULT '',         -- JSON [{kind,path|url,title}]
    provenance  TEXT DEFAULT '',         -- JSON field -> explicit|inferred|derived
    created_at  INTEGER DEFAULT 0,
    updated_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

CREATE TABLE IF NOT EXISTS milestones(
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    order_index INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'pending',  -- pending | active | completed | skipped
    deadline    INTEGER DEFAULT 0,
    estimated_minutes INTEGER DEFAULT 0,
    remaining_minutes INTEGER DEFAULT 0,
    progress    REAL DEFAULT -1,
    created_at  INTEGER DEFAULT 0,
    updated_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id);

-- Task dependency DAG (task_id depends on depends_on). ``inferred`` edges are
-- advisory: they are surfaced but never enforced silently by the scheduler.
CREATE TABLE IF NOT EXISTS task_deps(
    task_id    INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    inferred   INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT 0,
    PRIMARY KEY(task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_task ON task_deps(task_id);
CREATE INDEX IF NOT EXISTS idx_task_deps_dep ON task_deps(depends_on);

-- ---------------------------------------------------------------------------
-- M6: long-term memory + learning. A typed, provenance-aware, auditable store
-- of things Butler should remember across conversations. Inferred memories can
-- never become hard constraints; the write gate is the only way in.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories(
    id                INTEGER PRIMARY KEY,
    type              TEXT NOT NULL,          -- core_fact | preference | routine | ...
    subject           TEXT DEFAULT '',         -- e.g. "CS168", "CS168 Project 2"
    key               TEXT DEFAULT '',         -- stable attribute name
    value             TEXT DEFAULT '',         -- human-readable value (JSON ok)
    source            TEXT DEFAULT '',         -- free-form origin label
    source_detail     TEXT DEFAULT '',         -- url / task id / note
    confidence        REAL DEFAULT 0.0,        -- 0..1
    provenance        TEXT DEFAULT '',         -- explicit_user | routine_inferred | ...
    created_at        INTEGER DEFAULT 0,
    updated_at        INTEGER DEFAULT 0,
    observed_at       INTEGER DEFAULT 0,
    expires_at        INTEGER DEFAULT 0,       -- 0 = never
    last_confirmed_at INTEGER DEFAULT 0,
    confirmation_state TEXT DEFAULT 'unconfirmed',
    scope             TEXT DEFAULT 'personal', -- personal | external
    tags              TEXT DEFAULT '',         -- comma-separated
    active            INTEGER DEFAULT 1,
    supersedes_id     INTEGER DEFAULT 0,
    superseded_by     INTEGER DEFAULT 0,
    usage_count       INTEGER DEFAULT 0,
    last_used_at      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_mem_active ON memories(active);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject);
CREATE INDEX IF NOT EXISTS idx_mem_key ON memories(key);
CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at);
CREATE INDEX IF NOT EXISTS idx_mem_conf ON memories(confidence);

CREATE TABLE IF NOT EXISTS memory_evidence(
    id          INTEGER PRIMARY KEY,
    memory_id   INTEGER NOT NULL,
    kind        TEXT DEFAULT '',
    ref         TEXT DEFAULT '',
    detail      TEXT DEFAULT '',
    observed_at INTEGER DEFAULT 0,
    created_at  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mev_memory ON memory_evidence(memory_id);

CREATE TABLE IF NOT EXISTS memory_observations(
    id            INTEGER PRIMARY KEY,
    kind          TEXT DEFAULT '',            -- routine | estimate | behaviour
    subject       TEXT DEFAULT '',
    key           TEXT DEFAULT '',
    value         TEXT DEFAULT '',
    signature     TEXT NOT NULL,
    count         INTEGER DEFAULT 0,
    first_seen    INTEGER DEFAULT 0,
    last_seen     INTEGER DEFAULT 0,
    confidence    REAL DEFAULT 0.0,
    source_events TEXT DEFAULT '',            -- JSON list of evidence refs
    created_at    INTEGER DEFAULT 0,
    updated_at    INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mobs_sig ON memory_observations(signature);
CREATE INDEX IF NOT EXISTS idx_mobs_kind ON memory_observations(kind);

-- ---------------------------------------------------------------------------
-- M7: proactive executive state. Candidate generation is deterministic and
-- idempotent; notifications/responses/suppressions/snoozes are durable so a
-- restart never re-notifies or forgets a dismissal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS proactive_candidates(
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    category      TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    summary       TEXT DEFAULT '',
    priority      TEXT DEFAULT 'low',
    score         REAL DEFAULT 0.0,
    confidence    REAL DEFAULT 0.0,
    detected_at   INTEGER DEFAULT 0,
    updated_at    INTEGER DEFAULT 0,
    first_seen    INTEGER DEFAULT 0,
    last_seen     INTEGER DEFAULT 0,
    state         TEXT DEFAULT 'pending',
    evidence      TEXT DEFAULT '',        -- JSON
    proposed_action TEXT DEFAULT '',      -- JSON
    requires_confirmation INTEGER DEFAULT 0,
    expires_at    INTEGER DEFAULT 0,
    relevant_entities TEXT DEFAULT '',    -- JSON
    explanation   TEXT DEFAULT '',
    last_state_hash TEXT DEFAULT '',
    last_notified INTEGER DEFAULT 0,
    notification_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pc_state ON proactive_candidates(state);
CREATE INDEX IF NOT EXISTS idx_pc_category ON proactive_candidates(category);
CREATE INDEX IF NOT EXISTS idx_pc_updated ON proactive_candidates(updated_at);

CREATE TABLE IF NOT EXISTS proactive_notifications(
    id            INTEGER PRIMARY KEY,
    candidate_key TEXT NOT NULL,
    ts            INTEGER DEFAULT 0,
    channel       TEXT DEFAULT '',
    priority      TEXT DEFAULT '',
    state         TEXT DEFAULT 'sent',
    message       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pn_key ON proactive_notifications(candidate_key);
CREATE INDEX IF NOT EXISTS idx_pn_ts ON proactive_notifications(ts);

CREATE TABLE IF NOT EXISTS proactive_responses(
    id            INTEGER PRIMARY KEY,
    candidate_key TEXT NOT NULL,
    response      TEXT DEFAULT '',
    ts            INTEGER DEFAULT 0,
    note          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pr_key ON proactive_responses(candidate_key);
CREATE INDEX IF NOT EXISTS idx_pr_ts ON proactive_responses(ts);

CREATE TABLE IF NOT EXISTS proactive_suppressions(
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    scope         TEXT DEFAULT 'candidate',  -- candidate | category
    reason        TEXT DEFAULT '',
    created_at    INTEGER DEFAULT 0,
    until         INTEGER DEFAULT 0           -- 0 = permanent
);

CREATE TABLE IF NOT EXISTS proactive_snoozes(
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    until         INTEGER DEFAULT 0,
    created_at    INTEGER DEFAULT 0
);
"""


class DB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = __import__("threading").Lock()
        # Hardening: wait briefly instead of failing immediately under
        # contention, and keep durable writes reasonably cheap on the Pi.
        for pragma in ("PRAGMA busy_timeout=5000",
                       "PRAGMA synchronous=NORMAL",
                       "PRAGMA foreign_keys=ON"):
            try:
                self.conn.execute(pragma)
            except sqlite3.Error:  # pragma: no cover — best effort
                pass
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._set_schema_version()
        self.conn.commit()

    def _set_schema_version(self) -> None:
        try:
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.Error:  # pragma: no cover
            pass

    def schema_version(self) -> int:
        try:
            row = self.conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:  # pragma: no cover
            return 0

    def integrity_check(self) -> str:
        """SQLite ``quick_check`` result (``"ok"`` when healthy)."""
        try:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "unknown"
        except sqlite3.Error as exc:  # pragma: no cover
            return f"error: {exc}"

    def _migrate(self) -> None:
        """Idempotently add newly-introduced columns to pre-existing tables.

        ``CREATE TABLE IF NOT EXISTS`` only helps fresh databases; we use
        ``ALTER TABLE ... ADD COLUMN`` for tables a running install may already
        have created without the newer columns.
        """
        pending: dict[str, list[tuple[str, str]]] = {
            "recipes": [
                ("time_estimated", "INTEGER DEFAULT 0"),
                ("cost_estimated", "INTEGER DEFAULT 0"),
                ("nutrition_source", "TEXT DEFAULT ''"),
            ],
            "course_documents": [
                ("task_id", "INTEGER DEFAULT 0"),
                ("understanding", "TEXT DEFAULT ''"),
            ],
            "courses": [
                ("calendar_url", "TEXT DEFAULT ''"),
            ],
            # M3: link tasks to projects/milestones and allow explicit remaining
            # effort. Existing tasks default to 0/0/0 = "not in a project".
            "tasks": [
                ("project_id", "INTEGER DEFAULT 0"),
                ("milestone_id", "INTEGER DEFAULT 0"),
                ("remaining_minutes", "INTEGER DEFAULT 0"),
            ],
            # N1: evolve the existing topic store into a durable TopicProfile
            # rather than creating a parallel table.
            "topic_settings": [
                ("purpose", "TEXT DEFAULT ''"),
                ("description", "TEXT DEFAULT ''"),
                ("status", "TEXT DEFAULT 'active'"),
                ("capabilities", "TEXT DEFAULT ''"),
                ("template", "TEXT DEFAULT ''"),
                ("last_seen_at", "INTEGER DEFAULT 0"),
                ("created_at", "INTEGER DEFAULT 0"),
                ("pin_message_id", "INTEGER DEFAULT 0"),
                ("pin_message_version", "INTEGER DEFAULT 0"),
                ("pin_content_hash", "TEXT DEFAULT ''"),
            ],
        }
        for table, cols in pending.items():
            existing = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # Indexes on migrated columns must be created *after* the ALTER above
        # (a pre-M3 tasks table has no project_id when SCHEMA first runs).
        self.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project "
                     "ON tasks(project_id)")

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 — checkpoint is best effort
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def reopen(self) -> None:
        """Re-open the connection (used after a restore replaced the file)."""
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
        self.conn.commit()

    # ---------- generic helpers ----------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---------- executive / meta state (Phase 5.1) ----------
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM exec_state WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        cur = self.one("SELECT id FROM exec_state WHERE key=?", (key,))
        if cur:
            self.execute("UPDATE exec_state SET value=? WHERE key=?", (value, key))
        else:
            self.execute("INSERT INTO exec_state(key, value) VALUES(?,?)", (key, value))

    def delete_meta(self, key: str) -> None:
        self.execute("DELETE FROM exec_state WHERE key=?", (key,))

    def all_meta(self) -> dict[str, str]:
        return {str(r["key"]): str(r["value"])
                for r in self.query("SELECT key, value FROM exec_state")}

    # ---------- Phase 6: audit log ----------
    def log_audit(self, run_id: str, ts: int, actor: str, action: str,
                  kind: str = "", target: str = "", decision: str = "allowed",
                  reason: str = "", outcome: str = "", detail: str = "",
                  idem_key: str = "") -> int:
        cur = self.execute(
            "INSERT INTO audit_log(run_id,ts,actor,action,kind,target,decision,"
            "reason,outcome,detail,idem_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ts, actor, action, kind, target, decision, reason,
             outcome, detail, idem_key))
        return int(cur.lastrowid)

    def audit_recent(self, limit: int = 100, action: str = "",
                     actor: str = "") -> list[sqlite3.Row]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if action:
            sql += " AND action=?"
            params.append(action)
        if actor:
            sql += " AND actor=?"
            params.append(actor)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, tuple(params))

    def audit_by_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM audit_log WHERE run_id=? ORDER BY id", (run_id,))

    def audit_count(self) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM audit_log")
        return int(row["n"]) if row else 0

    def audit_prune(self, before_ts: int) -> int:
        """Delete rows ending before ``before_ts`` (retention). Returns count."""
        cur = self.execute("DELETE FROM audit_log WHERE ts < ?", (before_ts,))
        return int(cur.rowcount)

    # ---------- Phase 6: idempotency ----------
    def idem_get(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM idempotency WHERE key=?", (key,))

    def idem_put(self, key: str, status: str, result: str = "", hash_: str = "",
                 actor: str = "", action: str = "", expires: int = 0) -> None:
        now = int(time.time())
        self.execute(
            "INSERT INTO idempotency(key,hash,actor,action,status,result,created,expires) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET status=excluded.status, "
            "result=excluded.result, hash=excluded.hash, actor=excluded.actor, "
            "action=excluded.action, expires=excluded.expires",
            (key, hash_, actor, action, status, result, now, expires))

    def idem_delete(self, key: str) -> None:
        self.execute("DELETE FROM idempotency WHERE key=?", (key,))

    def idem_count(self) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM idempotency")
        return int(row["n"]) if row else 0

    def idem_prune_expired(self, now: int | None = None) -> int:
        now = now if now is not None else int(time.time())
        cur = self.execute("DELETE FROM idempotency WHERE expires>0 AND expires<?",
                           (now,))
        return int(cur.rowcount)

    # ---------- Phase 6: scheduler state ----------
    def scheduler_state(self, job: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM scheduler_state WHERE job=?", (job,))

    def scheduler_states(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM scheduler_state ORDER BY job")

    def upsert_scheduler_state(self, job: str, cadence: int = 0,
                               last_status: str = "", next_ts: int = 0) -> None:
        now = int(time.time())
        self.execute(
            "INSERT INTO scheduler_state(job,cadence,last_status,next_ts,"
            "last_done,locked_until,last_error,consecutive_failures,disabled) "
            "VALUES(?,?,?,?,?,?,?,0,0) "
            "ON CONFLICT(job) DO UPDATE SET cadence=excluded.cadence, "
            "last_status=excluded.last_status, next_ts=excluded.next_ts",
            (job, cadence, last_status, next_ts, now, 0, ""))

    def scheduler_mark_start(self, job: str) -> bool:
        """Acquire a lease so overlapping/duplicate runs are prevented.
        Returns True if the caller may run, False if another run holds the lock
        or past_due lease has not yet been reclaimed."""
        now = int(time.time())
        row = self.one("SELECT locked_until FROM scheduler_state WHERE job=?", (job,))
        if row is not None and int(row["locked_until"]) > now:
            return False
        self.execute(
            "UPDATE scheduler_state SET locked_until=?, last_status='running' "
            "WHERE job=?",
            (now + 300, job))
        return True

    def scheduler_mark_done(self, job: str, ok: bool, error: str = "") -> None:
        now = int(time.time())
        row = self.one("SELECT consecutive_failures, disabled FROM scheduler_state "
                       "WHERE job=?", (job,))
        failures = (int(row["consecutive_failures"]) if row else 0)
        failures = 0 if ok else failures + 1
        self.execute(
            "UPDATE scheduler_state SET last_done=?, last_status=?, "
            "locked_until=0, last_error=?, consecutive_failures=? WHERE job=?",
            (now, "ok" if ok else "failed", error if not ok else "", failures,
             job))

    def scheduler_note_skipped(self, job: str, reason: str = "") -> None:
        now = int(time.time())
        self.execute(
            "UPDATE scheduler_state SET last_status='skipped', locked_until=0, "
            "last_error=? WHERE job=?",
            (reason, job))

    def scheduler_reclaim_stale(self, now: int | None = None) -> int:
        """Reclaim a lease left by a crashed run (older than the lease TTL)."""
        now = now if now is not None else int(time.time())
        cur = self.execute(
            "UPDATE scheduler_state SET locked_until=0 WHERE locked_until>0 "
            "AND locked_until<?",
            (now,))
        return int(cur.rowcount)

    # ---------- Phase 6: heartbeat ----------
    def heartbeat(self, source: str, ts: int | None = None, status: str = "ok",
                  note: str = "", pid: int = 0) -> None:
        ts = ts if ts is not None else int(time.time())
        pid = pid or os.getpid()
        self.execute(
            "INSERT INTO heartbeat(source,ts,status,note,pid) VALUES(?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET ts=excluded.ts, status=excluded.status, "
            "note=excluded.note, pid=excluded.pid",
            (source, ts, status, note, pid))

    def heartbeats(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM heartbeat ORDER BY source")

    def heartbeat_age(self, source: str, now: int | None = None) -> int | None:
        row = self.one("SELECT ts FROM heartbeat WHERE source=?", (source,))
        if not row:
            return None
        now = now if now is not None else int(time.time())
        return max(0, now - int(row["ts"]))

    # ---------- files ----------
    def upsert_file(
        self, path: str, name: str, ext: str, size: int, mtime: int,
        hash_: str | None = None, mime: str | None = None,
        meta: str | None = None, category: str | None = None,
    ) -> int:
        existing = self.one("SELECT id FROM files WHERE path=?", (path,))
        parent = str(Path(path).parent)
        if existing:
            self.execute(
                """UPDATE files SET name=?, ext=?, size=?, mtime=?, hash=?,
                   mime=?, meta=?, category=?, parent=?
                   WHERE id=?""",
                (name, ext, size, mtime, hash_, mime, meta, category,
                 parent, existing["id"]),
            )
            return int(existing["id"])
        cur = self.execute(
            """INSERT INTO files(path,name,ext,size,mtime,hash,mime,meta,category,parent)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (path, name, ext, size, mtime, hash_, mime, meta, category, parent),
        )
        return int(cur.lastrowid)

    def delete_file(self, file_id: int) -> None:
        self.execute("DELETE FROM files WHERE id=?", (file_id,))

    def delete_chunks_for_path(self, path: str) -> None:
        f = self.get_file(path)
        if f:
            self.delete_chunks(int(f["id"]))
            self.delete_file(int(f["id"]))

    def delete_chunks(self, file_id: int) -> None:
        rows = self.query("SELECT id FROM chunks WHERE file_id=?", (file_id,))
        ids = [r["id"] for r in rows]
        for cid in ids:
            self.execute("DELETE FROM content_fts WHERE chunk_id=?", (cid,))
        self.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        self.execute("DELETE FROM embeddings WHERE file_id=?", (file_id,))

    def get_file(self, path: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM files WHERE path=?", (path,))

    def file_by_id(self, file_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM files WHERE id=?", (file_id,))

    def add_chunks(self, file_id: int, texts: list[str]) -> None:
        for i, text in enumerate(texts):
            cur = self.execute(
                "INSERT INTO chunks(file_id,seq,text) VALUES(?,?,?)",
                (file_id, i, text),
            )
            cid = int(cur.lastrowid)
            f = self.file_by_id(file_id)
            self.execute(
                "INSERT INTO content_fts(chunk_id,file_id,path,name,body) "
                "VALUES(?,?,?,?,?)",
                (cid, file_id, f["path"], f["name"], text),
            )

    def set_embedding(self, file_id: int, model: str, dim: int,
                      n_chunks: int, vec: bytes) -> None:
        self.execute(
            """INSERT INTO embeddings(file_id,model,dim,n_chunks,vec)
               VALUES(?,?,?,?,?)
               ON CONFLICT(file_id) DO UPDATE SET
                 model=excluded.model, dim=excluded.dim,
                 n_chunks=excluded.n_chunks, vec=excluded.vec""",
            (file_id, model, dim, n_chunks, vec),
        )

    def all_embeddings(self, model: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT file_id, dim, vec FROM embeddings WHERE model=? AND vec IS NOT NULL",
            (model,),
        )

    def set_category(self, file_id: int, category: str, update_files: bool = True) -> None:
        self.execute("UPDATE files SET category=? WHERE id=?", (category, file_id))
        if update_files:
            f = self.file_by_id(file_id)
            if f:
                self.execute("UPDATE files SET category=? WHERE path=?", (category, f["path"]))

    def set_category_by_path(self, path: str, category: str) -> None:
        self.execute("UPDATE files SET category=? WHERE path=?", (category, path))

    def search_fts(self, query: str, limit: int = 25, name_only: bool = False) -> list[sqlite3.Row]:
        if name_only:
            q = (
                "SELECT f.id, f.path, f.name, f.size, f.mtime, f.category, f.mime, 0 AS score "
                "FROM files f WHERE f.name LIKE ? ORDER BY f.mtime DESC LIMIT ?"
            )
            return self.query(q, (f"%{query}%", limit))
        sql = (
            "SELECT f.id, f.path, f.name, f.size, f.mtime, f.category, f.mime, "
            "       bm25(content_fts) AS score "
            "FROM content_fts JOIN files f ON f.id = content_fts.file_id "
            "WHERE content_fts MATCH ? "
            "ORDER BY score LIMIT ?"
        )
        try:
            rows = self.query(sql, (query, limit * 6))
        except sqlite3.OperationalError:
            return []
        # one row per file, keep the best-ranked chunk (BM25 lower == better)
        best: dict[int, sqlite3.Row] = {}
        for r in rows:
            fid = int(r["id"])
            if fid not in best or r["score"] < best[fid]["score"]:
                best[fid] = r
        return list(best.values())[:limit]

    def chunk_results(self, query: str, limit: int = 12) -> list[sqlite3.Row]:
        """Top-ranked chunk bodies (for RAG context). `query` is an FTS MATCH."""
        sql = (
            "SELECT chunk_id, file_id, path, name, body, bm25(content_fts) AS score "
            "FROM content_fts WHERE content_fts MATCH ? "
            "ORDER BY score LIMIT ?"
        )
        try:
            return self.query(sql, (query, limit))
        except sqlite3.OperationalError:
            return []

    def search_files(self, term: str, limit: int = 50) -> list[sqlite3.Row]:
        q = (
            "SELECT id, path, name, size, mtime, category, mime FROM files "
            "WHERE is_dir=0 AND (name LIKE ? OR path LIKE ?) "
            "ORDER BY mtime DESC LIMIT ?"
        )
        like = f"%{term}%"
        return self.query(q, (like, like, limit))

    # ---------- trash ----------
    def add_trash(self, orig_path: str, name: str, trashed_rel: str,
                  size: int, reason: str) -> int:
        cur = self.execute(
            "INSERT INTO trash(orig_path,name,trashed_rel,size,reason,trashed_at) "
            "VALUES(?,?,?,?,?,?)",
            (orig_path, name, trashed_rel, size, reason, int(time.time())),
        )
        return int(cur.lastrowid)

    def trash_items(self, include_restored: bool = False) -> list[sqlite3.Row]:
        if include_restored:
            return self.query("SELECT * FROM trash ORDER BY trashed_at DESC")
        return self.query(
            "SELECT * FROM trash WHERE restored_at IS NULL ORDER BY trashed_at DESC"
        )

    def restore_trash(self, tid: int) -> sqlite3.Row | None:
        self.execute(
            "UPDATE trash SET restored_at=? WHERE id=? AND restored_at IS NULL",
            (int(time.time()), tid),
        )
        return self.one("SELECT * FROM trash WHERE id=?", (tid,))

    # ---------- duplicates ----------
    def add_duplicate(self, group_id: str, file_id: int, is_primary: int) -> None:
        self.execute(
            "INSERT OR IGNORE INTO duplicates(group_id,file_id,is_primary) VALUES(?,?,?)",
            (group_id, file_id, is_primary),
        )

    def duplicate_groups(self, root: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT group_id, COUNT(*) AS n, MIN(file_id) AS primary_id "
            "FROM duplicates WHERE is_primary=0 GROUP BY group_id HAVING n > 0 "
            "ORDER BY n DESC"
        )
        return self.query(sql)

    def duplicates_in(self, root: str) -> list[sqlite3.Row]:
        sql = (
            "SELECT d.group_id, d.file_id, d.is_primary, f.path, f.name, f.size "
            "FROM duplicates d JOIN files f ON f.id=d.file_id "
            "WHERE f.path LIKE ? ORDER BY d.group_id, d.is_primary DESC"
        )
        return self.query(sql, (root + "%",))

    def flush_duplicates(self, root: str) -> None:
        ids = self.query("SELECT id FROM files WHERE path LIKE ?", (root + "%",))
        for r in ids:
            self.execute("DELETE FROM duplicates WHERE file_id=?", (r["id"],))

    # ---------- operations ----------
    def log_operation(self, user: str, action: str, target: str = "",
                      dest: str = "", detail: str = "", status: str = "applied",
                      plan_id: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO operations(ts,user,action,target,dest,detail,status,plan_id) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), user, action, target, dest, detail, status, plan_id),
        )
        return int(cur.lastrowid)

    def recent_operations(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- backups ----------
    def log_backup(self, source: str, dest: str, status: str, detail: str = "") -> int:
        cur = self.execute(
            "INSERT INTO backups(ts,source,dest,status,detail) VALUES(?,?,?,?,?)",
            (int(time.time()), source, dest, status, detail),
        )
        return int(cur.lastrowid)

    def latest_backup(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM backups ORDER BY ts DESC LIMIT 1")

    # ---------- links (feature: saved-link checker) ----------
    def add_link(self, url: str, tag: str = "", title: str = "") -> int:
        cur = self.execute(
            "INSERT INTO links(url,title,tag,status,added,last_checked) VALUES(?,?,?,?,?,?)",
            (url, title, tag, "added", int(time.time()), 0),
        )
        return int(cur.lastrowid)

    def links(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM links ORDER BY id DESC")

    def link_by_id(self, link_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM links WHERE id=?", (link_id,))

    def update_link_state(self, link_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE links SET {cols} WHERE id=?",
            tuple(fields.values()) + (link_id,),
        )

    # ---------- tasks (Phase 2 scheduler) ----------
    def add_task(self, title: str, detail: str = "", deadline: int = 0,
                 priority: int = 3, est_minutes: int = 60, tags: str = "") -> int:
        cur = self.execute(
            "INSERT INTO tasks(title,detail,deadline,priority,est_minutes,status,"
            "sort,created,tags) VALUES(?,?,?,?,?,?,?,?,?)",
            (title, detail, deadline, priority, est_minutes, "todo", 0,
             int(time.time()), tags),
        )
        return int(cur.lastrowid)

    def task_by_id(self, task_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def tasks(self, status: str = "todo") -> list[sqlite3.Row]:
        if status == "active":
            return self.query(
                "SELECT * FROM tasks WHERE status IN ('todo','doing','scheduled') "
                "ORDER BY deadline, priority DESC, sort")
        return self.query(
            "SELECT * FROM tasks WHERE status=? ORDER BY deadline, priority DESC, sort",
            (status,))

    def all_tasks(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM tasks ORDER BY id DESC")

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "status" in fields:
            fields["status"] = normalize_status(fields["status"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE tasks SET {cols} WHERE id=?",
            tuple(fields.values()) + (task_id,),
        )

    # ---------- M3 projects / milestones / dependencies ----------
    @staticmethod
    def _json_field(value: Any) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""

    _PROJECT_JSON_FIELDS = ("links", "refs", "provenance")

    def add_project(self, name: str, *, objective: str = "", status: str = "active",
                    priority: int = 3, course_id: int = 0, deadline: int = 0,
                    estimated_total_minutes: int = 0, remaining_minutes: int = 0,
                    repo_url: str = "", links: Any = None, refs: Any = None,
                    provenance: Any = None, now: int | None = None) -> int:
        ts = int(now if now is not None else time.time())
        cur = self.execute(
            "INSERT INTO projects(name,objective,status,priority,course_id,deadline,"
            "estimated_total_minutes,remaining_minutes,progress,risk,repo_url,links,"
            "refs,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, objective, status, priority, course_id, deadline,
             estimated_total_minutes, remaining_minutes, -1.0, -1.0, repo_url,
             self._json_field(links), self._json_field(refs),
             self._json_field(provenance), ts, ts),
        )
        return int(cur.lastrowid)

    def project_by_id(self, project_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM projects WHERE id=?", (project_id,))

    def projects(self, status: str = "") -> list[sqlite3.Row]:
        if status:
            return self.query(
                "SELECT * FROM projects WHERE status=? ORDER BY priority DESC, "
                "deadline, id", (status,))
        return self.query(
            "SELECT * FROM projects ORDER BY priority DESC, deadline, id")

    def update_project(self, project_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in self._PROJECT_JSON_FIELDS:
            if key in fields:
                fields[key] = self._json_field(fields[key])
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE projects SET {cols} WHERE id=?",
            tuple(fields.values()) + (project_id,),
        )

    def delete_project(self, project_id: int) -> None:
        self.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self.execute("DELETE FROM milestones WHERE project_id=?", (project_id,))
        self.execute("UPDATE tasks SET project_id=0, milestone_id=0 "
                     "WHERE project_id=?", (project_id,))

    def add_milestone(self, project_id: int, name: str, *, description: str = "",
                      order_index: int = 0, status: str = "pending",
                      deadline: int = 0, estimated_minutes: int = 0,
                      remaining_minutes: int = 0, now: int | None = None) -> int:
        ts = int(now if now is not None else time.time())
        cur = self.execute(
            "INSERT INTO milestones(project_id,name,description,order_index,status,"
            "deadline,estimated_minutes,remaining_minutes,progress,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, name, description, order_index, status, deadline,
             estimated_minutes, remaining_minutes, -1.0, ts, ts),
        )
        return int(cur.lastrowid)

    def milestone_by_id(self, milestone_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM milestones WHERE id=?", (milestone_id,))

    def milestones(self, project_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM milestones WHERE project_id=? "
            "ORDER BY order_index, id", (project_id,))

    def update_milestone(self, milestone_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE milestones SET {cols} WHERE id=?",
            tuple(fields.values()) + (milestone_id,),
        )

    def delete_milestone(self, milestone_id: int) -> None:
        self.execute("DELETE FROM milestones WHERE id=?", (milestone_id,))
        self.execute("UPDATE tasks SET milestone_id=0 WHERE milestone_id=?",
                     (milestone_id,))

    def project_tasks(self, project_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM tasks WHERE project_id=? ORDER BY deadline, priority DESC, id",
            (project_id,))

    def milestone_tasks(self, milestone_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM tasks WHERE milestone_id=? ORDER BY deadline, priority DESC, id",
            (milestone_id,))

    def link_task(self, task_id: int, project_id: int,
                  milestone_id: int = 0) -> None:
        self.execute(
            "UPDATE tasks SET project_id=?, milestone_id=? WHERE id=?",
            (project_id, milestone_id, task_id))

    def add_task_dependency(self, task_id: int, depends_on: int,
                            inferred: bool = False) -> None:
        self.execute(
            "INSERT OR IGNORE INTO task_deps(task_id,depends_on,inferred,created_at) "
            "VALUES(?,?,?,?)",
            (task_id, depends_on, 1 if inferred else 0, int(time.time())))

    def remove_task_dependency(self, task_id: int, depends_on: int) -> None:
        self.execute("DELETE FROM task_deps WHERE task_id=? AND depends_on=?",
                     (task_id, depends_on))

    def task_dependencies(self, task_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM task_deps WHERE task_id=? ORDER BY depends_on",
            (task_id,))

    def task_dependents(self, task_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM task_deps WHERE depends_on=? ORDER BY task_id",
            (task_id,))

    def all_task_dependencies(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM task_deps ORDER BY task_id, depends_on")

    def dependencies_for_tasks(self, task_ids: list[int]) -> list[sqlite3.Row]:
        if not task_ids:
            return []
        marks = ",".join("?" for _ in task_ids)
        return self.query(
            f"SELECT * FROM task_deps WHERE task_id IN ({marks}) "
            "ORDER BY task_id, depends_on", tuple(task_ids))

    def set_task_status(self, task_id: int, status: str,
                        reason: str = "") -> None:
        """Transition a task's status, stamping ``completed`` when it leaves the
        active set and writing a ``task_history`` audit row. Idempotent for the
        *digital* status (a repeated call is a no-op), but ``completed`` is only
        set when the task actually leaves the active set."""
        to = normalize_status(status)
        row = self.task_by_id(task_id)
        frm = normalize_status((row["status"] if row else "") or "todo")
        if row and frm != to:
            self.execute(
                "UPDATE tasks SET status=?, completed=? WHERE id=?",
                (to, int(time.time()) if to in TERMINAL_STATUSES else 0, task_id),
            )
        elif row and to in TERMINAL_STATUSES and not row["completed"]:
            self.execute(
                "UPDATE tasks SET completed=? WHERE id=?", (int(time.time()), task_id))
        self.log_task_transition(task_id, frm if row else "", to, reason)

    def log_task_transition(self, task_id: int, from_status: str,
                            to_status: str, reason: str = "") -> None:
        self.execute(
            "INSERT INTO task_history(task_id,from_status,to_status,reason,ts) "
            "VALUES(?,?,?,?,?)",
            (task_id, normalize_status(from_status), normalize_status(to_status),
             reason, int(time.time())))

    def task_history(self, task_id: int | None = None,
                     limit: int = 50) -> list[sqlite3.Row]:
        if task_id is not None:
            return self.query(
                "SELECT * FROM task_history WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit))
        return self.query(
            "SELECT * FROM task_history ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- task -> Google Calendar mapping (Phase 5.0) ----------
    def task_gcal(self, task_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM task_gcal WHERE task_id=?", (task_id,))

    def task_gcal_all(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM task_gcal ORDER BY task_id")

    def upsert_task_gcal(self, task_id: int, gcal_event_id: str, state: str,
                         title: str = "", start_ts: int = 0, end_ts: int = 0,
                         last_error: str = "") -> None:
        now = int(time.time())
        self.execute(
            "INSERT INTO task_gcal(task_id,gcal_event_id,state,title,start_ts,end_ts,"
            "last_error,created,updated) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "gcal_event_id=excluded.gcal_event_id, state=excluded.state, "
            "title=excluded.title, start_ts=excluded.start_ts, "
            "end_ts=excluded.end_ts, last_error=excluded.last_error, "
            "updated=excluded.updated",
            (task_id, gcal_event_id, state, title, start_ts, end_ts, last_error,
             now, now))

    def clear_task_gcal(self, task_id: int) -> None:
        self.execute("DELETE FROM task_gcal WHERE task_id=?", (task_id,))

    # ---------- events (external hard commitments) ----------
    def add_event(self, title: str, start_ts: int, end_ts: int, source: str = "local",
                  external_id: str = "", all_day: int = 0, location: str = "") -> int:
        cur = self.execute(
            "INSERT INTO events(source,external_id,title,all_day,start_ts,end_ts,"
            "location,updated) VALUES(?,?,?,?,?,?,?,?)",
            (source, external_id, title, all_day, start_ts, end_ts, location,
             int(time.time())),
        )
        return int(cur.lastrowid)

    def events_between(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM events WHERE start_ts < ? AND end_ts > ? "
            "ORDER BY start_ts", (end_ts, start_ts))

    def events(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM events ORDER BY start_ts")

    def clear_events(self, source: str = "") -> None:
        if source:
            self.execute("DELETE FROM events WHERE source=?", (source,))
        else:
            self.execute("DELETE FROM events")

    def event_by_external(self, external_id: str, source: str = "") -> sqlite3.Row | None:
        if source:
            return self.one(
                "SELECT * FROM events WHERE source=? AND external_id=?",
                (source, external_id))
        return self.one("SELECT * FROM events WHERE external_id=?", (external_id,))

    def update_event(self, event_id: int, title: str, start_ts: int, end_ts: int,
                     all_day: int = 0, location: str = "") -> None:
        self.execute(
            "UPDATE events SET title=?,start_ts=?,end_ts=?,all_day=?,location=?,"
            "updated=? WHERE id=?",
            (title, start_ts, end_ts, all_day, location, int(time.time()), event_id))

    def delete_event(self, event_id: int) -> None:
        self.execute("DELETE FROM events WHERE id=?", (event_id,))

    def google_events_in_window(self, start_ts: int, end_ts: int) -> list[sqlite3.Row]:
        """Google events where the instance START falls inside ``[start,end)``.
        Used to prune instances the remote calendar no longer returns (so we
        never trust a stale half-imported recurring series)."""
        return self.query(
            "SELECT * FROM events WHERE source='google' AND start_ts>=? AND start_ts<?",
            (start_ts, end_ts))

    # ---------- plans (deterministic schedule + history) ----------
    def save_plan(self, day_start: int, day_end: int, state: str,
                  payload: str) -> int:
        cur = self.execute(
            "INSERT INTO plans(created,day_start,day_end,state,json) VALUES(?,?,?,?,?)",
            (int(time.time()), day_start, day_end, state, payload),
        )
        return int(cur.lastrowid)

    def update_plan_state(self, plan_id: int, state: str) -> None:
        self.execute("UPDATE plans SET state=?, created=? WHERE id=?",
                     (state, int(time.time()), plan_id))

    def plan_by_id(self, plan_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM plans WHERE id=?", (plan_id,))

    def latest_plan(self) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM plans WHERE state='active' ORDER BY id DESC LIMIT 1")

    def history_plans(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT * FROM plans WHERE state!='active' "
               "ORDER BY id DESC")
        if limit:
            sql += " LIMIT ?"
            return self.query(sql, (limit,))
        return self.query(sql)

    # ---------- courses (Phase 3) ----------
    def add_course(self, code: str, name: str = "", instructor: str = "",
                   url: str = "", platform: str = "", semester: str = "",
                   monitoring_enabled: int = 1,
                   monitoring_interval: int = 3600) -> int:
        now = int(time.time())
        code = code.strip().upper()
        existing = self.one("SELECT id FROM courses WHERE code=?", (code,))
        if existing:
            self.execute(
                "UPDATE courses SET name=?, instructor=?, url=?, platform=?, "
                "semester=?, monitoring_enabled=?, monitoring_interval=?, "
                "updated_at=? WHERE id=?",
                (name, instructor, url, platform, semester, monitoring_enabled,
                 monitoring_interval, now, int(existing["id"])),
            )
            return int(existing["id"])
        cur = self.execute(
            "INSERT INTO courses(code,name,instructor,url,platform,semester,"
            "monitoring_enabled,monitoring_interval,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (code, name, instructor, url, platform, semester,
             monitoring_enabled, monitoring_interval, now, now),
        )
        return int(cur.lastrowid)

    def course_by_id(self, course_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM courses WHERE id=?", (course_id,))

    def course_by_code(self, code: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM courses WHERE code=?", (code.strip().upper(),))

    def courses(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM courses ORDER BY code")

    def update_course(self, course_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE courses SET {cols} WHERE id=?",
            tuple(fields.values()) + (course_id,),
        )

    def delete_course(self, course_id: int) -> None:
        self.execute("DELETE FROM courses WHERE id=?", (course_id,))

    def courses_to_monitor(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM courses WHERE monitoring_enabled=1 ORDER BY code")

    # ---------- course documents ----------
    def add_course_document(self, course_id: int, title: str, url: str = "",
                            local_path: str = "", document_type: str = "reading",
                            content_hash: str = "", external_id: str = "") -> int:
        existing = None
        if external_id:
            existing = self.one(
                "SELECT id FROM course_documents WHERE course_id=? AND external_id=?",
                (course_id, external_id))
        if existing is None and url:
            existing = self.one(
                "SELECT id FROM course_documents WHERE course_id=? AND url=?",
                (course_id, url))
        if existing:
            self.execute(
                "UPDATE course_documents SET title=?, local_path=?, "
                "document_type=?, downloaded_at=? WHERE id=?",
                (title, local_path, document_type, int(time.time()),
                 int(existing["id"])),
            )
            return int(existing["id"])
        cur = self.execute(
            "INSERT INTO course_documents(course_id,title,url,local_path,"
            "document_type,content_hash,downloaded_at,version,external_id) "
            "VALUES(?,?,?,?,?,?,?,1,?)",
            (course_id, title, url, local_path, document_type,
             content_hash, int(time.time()), external_id),
        )
        return int(cur.lastrowid)

    def course_documents(self, course_id: int | None = None) -> list[sqlite3.Row]:
        if course_id is not None:
            return self.query(
                "SELECT * FROM course_documents WHERE course_id=? ORDER BY downloaded_at DESC",
                (course_id,))
        return self.query("SELECT * FROM course_documents ORDER BY downloaded_at DESC")

    def doc_by_id(self, doc_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM course_documents WHERE id=?", (doc_id,))

    def update_course_document(self, doc_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE course_documents SET {cols} WHERE id=?",
            tuple(fields.values()) + (doc_id,),
        )

    # ---------- food inventory (Phase 3) ----------
    def add_food(self, name: str, quantity: float = 1.0, unit: str = "",
                 expiration_date: int = 0, opened_date: int = 0,
                 category: str = "", storage_location: str = "",
                 notes: str = "") -> int:
        now = int(time.time())
        cur = self.execute(
            "INSERT INTO food_items(name,quantity,unit,expiration_date,opened_date,"
            "category,storage_location,notes,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name.strip().lower(), quantity, unit, expiration_date, opened_date,
             category, storage_location, notes, now, now),
        )
        return int(cur.lastrowid)

    def food_by_id(self, food_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM food_items WHERE id=?", (food_id,))

    def food(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM food_items ORDER BY name")

    def update_food(self, food_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        fields["updated_at"] = int(time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE food_items SET {cols} WHERE id=?",
            tuple(fields.values()) + (food_id,),
        )

    def delete_food(self, food_id: int) -> None:
        self.execute("DELETE FROM food_items WHERE id=?", (food_id,))

    def find_food(self, name: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM food_items WHERE name LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{name.strip().lower()}%",))

    # ---------- shopping list (Phase 3) ----------
    def add_shopping(self, name: str, quantity: float = 1.0, unit: str = "",
                     category: str = "", needed_for: str = "") -> int:
        name = name.strip().lower()
        existing = self.one(
            "SELECT id FROM shopping_items WHERE name=? AND purchased=0", (name,))
        if existing:
            self.execute(
                "UPDATE shopping_items SET quantity=quantity+? WHERE id=?",
                (quantity, int(existing["id"])),
            )
            return int(existing["id"])
        cur = self.execute(
            "INSERT INTO shopping_items(name,quantity,unit,category,needed_for) "
            "VALUES(?,?,?,?,?)",
            (name, quantity, unit, category, needed_for),
        )
        return int(cur.lastrowid)

    def shopping(self, purchased: int = 0) -> list[sqlite3.Row]:
        if purchased is None:
            return self.query("SELECT * FROM shopping_items ORDER BY category, name")
        return self.query(
            "SELECT * FROM shopping_items WHERE purchased=? ORDER BY category, name",
            (purchased,))

    def shopping_by_id(self, item_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM shopping_items WHERE id=?", (item_id,))

    def update_shopping(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE shopping_items SET {cols} WHERE id=?",
            tuple(fields.values()) + (item_id,),
        )

    def clear_shopping(self, purchased: bool = True) -> int:
        if purchased:
            cur = self.execute("DELETE FROM shopping_items WHERE purchased=1")
        else:
            cur = self.execute("DELETE FROM shopping_items")
        return int(cur.rowcount)

    # ------------------------------------------------------ recipe library
    def add_recipe(self, name: str, source: str = "builtin", source_url: str = "",
                   ingredients: list[str] | None = None,
                   steps: list[str] | None = None, tags: list[str] | None = None,
                   equipment: list[str] | None = None, servings: int = 2,
                   prep_minutes: int = 0, cook_minutes: int = 0,
                   difficulty: int = 2, cost: float = 2.0,
                   time_estimated: int = 0, cost_estimated: int = 0,
                   nutrition_source: str = "") -> int:
        import json as _json
        existing = self.one(
            "SELECT id FROM recipes WHERE name=? AND source_url=?",
            (str(name), str(source_url)))
        if existing:
            return int(existing["id"])
        cur = self.execute(
            "INSERT INTO recipes(name,source,source_url,ingredients,steps,tags,"
            "equipment,servings,prep_minutes,cook_minutes,difficulty,cost,"
            "time_estimated,cost_estimated,nutrition_source,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(name), str(source), str(source_url),
             _json.dumps(ingredients or [], ensure_ascii=False),
             _json.dumps(steps or [], ensure_ascii=False),
             _json.dumps(tags or [], ensure_ascii=False),
             _json.dumps(equipment or [], ensure_ascii=False),
             int(servings), int(prep_minutes), int(cook_minutes),
             int(difficulty), float(cost),
             int(time_estimated), int(cost_estimated), str(nutrition_source),
             int(time.time())))
        return int(cur.lastrowid)

    def recipe_by_id(self, recipe_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM recipes WHERE id=?", (recipe_id,))

    def recipe_by_name(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM recipes WHERE name=? ORDER BY id DESC LIMIT 1",
                        (str(name),))

    def recipe_by_key(self, name: str, source_url: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM recipes WHERE name=? AND source_url=?",
                        (str(name), str(source_url)))

    def recipes(self, favorite_only: int | None = None) -> list[sqlite3.Row]:
        if favorite_only is not None:
            return self.query("SELECT * FROM recipes WHERE favorite=? "
                              "ORDER BY rating DESC, times_used DESC", (favorite_only,))
        return self.query("SELECT * FROM recipes "
                          "ORDER BY favorite DESC, rating DESC, times_used DESC")

    def recipes_recent(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM recipes WHERE times_used>0 "
                          "ORDER BY last_used DESC LIMIT ?", (int(limit),))

    def set_favorite(self, recipe_id: int, favorite: bool) -> None:
        self.execute("UPDATE recipes SET favorite=? WHERE id=?",
                     (1 if favorite else 0, int(recipe_id)))

    def set_rating(self, recipe_id: int, rating: float) -> None:
        self.execute("UPDATE recipes SET rating=? WHERE id=?",
                     (float(rating), int(recipe_id)))

    def touch_usage(self, recipe_id: int) -> None:
        self.execute("UPDATE recipes SET times_used=times_used+1, last_used=? "
                     "WHERE id=?", (int(time.time()), int(recipe_id)))

    def delete_recipe(self, recipe_id: int) -> None:
        self.execute("DELETE FROM recipes WHERE id=?", (int(recipe_id),))

    def add_meal_history(self, recipe_id: int, meal: str = "",
                         ts: int = 0) -> int:
        cur = self.execute(
            "INSERT INTO meal_history(recipe_id, meal, ts, created_at) VALUES(?,?,?,?)",
            (int(recipe_id), str(meal), int(ts or time.time()), int(time.time())))
        return int(cur.lastrowid)

    def meal_history(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query(
            "SELECT m.*, r.name AS recipe_name, r.source "
            "FROM meal_history m LEFT JOIN recipes r ON r.id=m.recipe_id "
            "ORDER BY m.ts DESC LIMIT ?", (int(limit),))

    def meal_history_on_day(self, recipe_id: int, day_ts: int) -> sqlite3.Row | None:
        """Already logged this recipe this day? (avoids dup rows on restart)."""
        y0, y1 = _day_bounds(day_ts)
        return self.one(
            "SELECT id FROM meal_history WHERE recipe_id=? AND ts BETWEEN ? AND ?",
            (int(recipe_id), y0, y1))

    # -------------------------------------------------- meal suggestions
    def add_meal_suggestion(self, day_ts: int, recipe_id: int, meal: str = "",
                            budget_minutes: int = 0, reason: str = "") -> int:
        """Idempotent: repeated requests never create duplicate rows."""
        existing = self.one(
            "SELECT id FROM meal_suggestions WHERE day_ts=? AND recipe_id=?",
            (int(day_ts), int(recipe_id)))
        if existing:
            self.execute(
                "UPDATE meal_suggestions SET meal=?, budget_minutes=?, reason=? "
                "WHERE id=?", (str(meal), int(budget_minutes), str(reason),
                               int(existing["id"])))
            return int(existing["id"])
        cur = self.execute(
            "INSERT INTO meal_suggestions(day_ts, recipe_id, meal, budget_minutes,"
            "reason, created_at) VALUES(?,?,?,?,?,?)",
            (int(day_ts), int(recipe_id), str(meal), int(budget_minutes),
             str(reason), int(time.time())))
        return int(cur.lastrowid)

    def meal_suggestions(self, day_ts: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM meal_suggestions WHERE day_ts=? ORDER BY id",
            (int(day_ts),))

    def clear_meal_suggestions(self, day_ts: int) -> int:
        cur = self.execute("DELETE FROM meal_suggestions WHERE day_ts=?",
                           (int(day_ts),))
        return int(cur.rowcount)

    # ------------------------------------------------------ topic settings
    def topic_setting(self, chat_id: int, thread_id: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM topic_settings WHERE chat_id=? AND thread_id=?",
            (int(chat_id), int(thread_id)))

    def topic_settings_all(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM topic_settings ORDER BY topic, chat_id")

    def topic_setting_by_title(self, title: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM topic_settings WHERE lower(topic)=? ORDER BY id LIMIT 1",
            (str(title).strip().lower(),))

    def set_title_routing(self, title: str, routing: str) -> None:
        """Preset a routing command for every topic with this title.

        Uses a unique negative thread_id as the global-by-title sentinel so it
        never collides with a real forum thread (thread_id >= 0) or the main
        chat (thread_id == 0).
        """
        title = str(title).strip()
        if not title:
            return
        routing = str(routing).strip()[:24]
        row = self.topic_setting_by_title(title)
        if row is not None:
            self.execute("UPDATE topic_settings SET routing=? WHERE id=?",
                         (routing, int(row["id"])))
            return
        used = {int(r["thread_id"]) for r in
                self.query("SELECT thread_id FROM topic_settings WHERE thread_id<0")}
        tid = -1
        while tid in used:
            tid -= 1
        self.execute(
            "INSERT INTO topic_settings(chat_id, thread_id, topic, routing,"
            " updated_ts) VALUES(0,?,?,?,?)",
            (tid, title, routing, int(time.time())))

    def upsert_topic_setting(self, chat_id: int, thread_id: int,
                             **fields: Any) -> None:
        cols = ["chat_id", "thread_id", "updated_ts"]
        vals: list[Any] = [int(chat_id), int(thread_id), int(time.time())]
        for k, f in fields.items():
            if k not in {"chat_id", "thread_id"}:
                cols.append(k)
                vals.append(f)
        placeholders = ", ".join(["?"] * len(cols))
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not
                            in {"chat_id", "thread_id"})
        self.execute(
            f"INSERT INTO topic_settings({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(chat_id, thread_id) DO UPDATE SET {updates}",
            tuple(vals))

    def delete_topic_setting(self, chat_id: int, thread_id: int) -> None:
        self.execute("DELETE FROM topic_settings WHERE chat_id=? AND thread_id=?",
                     (int(chat_id), int(thread_id)))

    # ------------------------------------------------------ reward library
    def add_reward(self, name: str, kind: str = "rest", weight: int = 1,
                   stock_food: str = "") -> int:
        cur = self.execute(
            "INSERT INTO rewards(name, kind, weight, stock_food, created_at) "
            "VALUES(?,?,?,?,?)",
            (str(name).strip()[:120], str(kind).strip()[:8],
             int(weight), str(stock_food).strip()[:120], int(time.time())))
        return int(cur.lastrowid)

    def rewards(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM rewards ORDER BY weight DESC, id")

    def reward_by_id(self, reward_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM rewards WHERE id=?", (int(reward_id),))

    def update_reward(self, reward_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE rewards SET {cols} WHERE id=?",
            tuple(fields.values()) + (int(reward_id),))

    def delete_reward(self, reward_id: int) -> None:
        self.execute("DELETE FROM rewards WHERE id=?", (int(reward_id),))

    def touch_reward(self, reward_id: int) -> None:
        self.execute(
            "UPDATE rewards SET times_used=times_used+1, last_used=? WHERE id=?",
            (int(time.time()), int(reward_id)))

    # ------------------------------------------------------ app settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM app_settings WHERE key=?", (str(key),))
        return str(row["value"]) if row and row["value"] is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), str(value)))
