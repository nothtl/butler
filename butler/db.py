"""SQLite persistence layer for Butler.

Holds the search index (FTS5), binary-content text chunks, embeddings,
trash registry, duplicate registry, classification, and the operation log.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import Config

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
    note        TEXT
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
"""


class DB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path = cfg.db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = __import__("threading").Lock()
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

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
        }
        for table, cols in pending.items():
            existing = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

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
                "SELECT * FROM tasks WHERE status IN ('todo','doing') "
                "ORDER BY deadline, priority DESC, sort")
        return self.query(
            "SELECT * FROM tasks WHERE status=? ORDER BY deadline, priority DESC, sort",
            (status,))

    def all_tasks(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM tasks ORDER BY id DESC")

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(
            f"UPDATE tasks SET {cols} WHERE id=?",
            tuple(fields.values()) + (task_id,),
        )

    def set_task_status(self, task_id: int, status: str) -> None:
        self.update_task(task_id, status=status, completed=int(time.time()))

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
