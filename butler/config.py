"""Configuration for Butler.

Feature 1: Configurable storage directory.
Feature 2: SMB/Samba NAS mounts.

All state is kept under ``data_dir`` (which lives on the M.2 SSD, since
``$HOME`` is backed by ``/dev/nvme0n1p2``). Values can be overridden via a
``config.toml`` file or environment variables.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "butler"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / APP_NAME / "config.toml"


def _expand(path: str | os.PathLike | None) -> str:
    if not path:
        return ""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


@dataclass
class SmbShare:
    """A mounted SMB/CIFS NAS share (feature 2)."""

    name: str
    mountpoint: str
    server: str = ""
    share: str = ""
    username: str = ""
    gvfs: bool = True  # True => already mounted over gvfs/your-fuse path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mountpoint": self.mountpoint,
            "server": self.server,
            "share": self.share,
            "username": self.username,
            "gvfs": self.gvfs,
        }


@dataclass
class Config:
    # --- user (audit: no hardcoded environment assumptions) ---
    # IANA timezone for all local-time decisions (day boundaries, "today",
    # display). Empty ("") means the system's local timezone. Butler prefers to
    # read/compute in UTC internally and converts at the edges.
    timezone: str = ""   # e.g. "America/Los_Angeles"; "" => system local

    # --- storage (feature 1) ---
    # Base directory that Butler manages and keeps state in. LIVES ON THE SSD.
    data_dir: str = ""
    # Directories Butler is allowed to manage (roots). AI may only act here.
    roots: list[str] = field(default_factory=list)
    # Directories indexed for search but not necessarily organised.
    index_roots: list[str] = field(default_factory=list)
    # Course library root (feature 17). Course codes route here.
    course_dir: str = ""
    # "incoming" drop box watched by the course monitor (feature 17).
    incoming_dir: str = ""
    # Course codes that are treated as course material, e.g. ["CS168"].
    courses: list[str] = field(default_factory=list)

    # --- trash (feature 7) ---
    trash_dir: str = ""
    trash_retention_days: int = 30

    # --- smb / nas (feature 2) ---
    smb_shares: list[SmbShare] = field(default_factory=list)

    # --- telegram (feature 9,10) ---
    telegram_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    # When the allow-list is empty Butler is DENY-by-default (no one may use the
    # bot). Set this to True to restore the old "open when no allow-list" mode.
    telegram_open_when_empty: bool = False

    # --- backup (feature 18) ---
    backup_dir: str = ""
    backup_schedule: str = "daily"

    # --- remote access (feature 19) ---
    remote_enabled: bool = False
    remote_port: int = 7841
    remote_token: str = ""

    # --- semantic search (feature 14) ---
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384

    # --- ai (feature 15/16) ---
    # Optional LLM provider for richer decisions when a key is present.
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4o-mini"
    # How many chunks to pull for RAG answers (chat/teach).
    rag_top_k: int = 6

    # --- affinity (Phase 4.2: let the user override the keyword/zone tables) ---
    # Optional overrides. Empty tables let the built-in keyword/zone defaults in
    # ``butler/affinity.py`` stand; see that module for the default categories.
    # ``affinity_keywords`` maps category -> list of substrings.
    # ``affinity_zone_keywords`` maps zone keyword -> {category: weight}.
    # ``affinity_unknown_category`` replaces the default "work" label for tasks
    # that match no keyword (empty string => treat as neutral/unclassified).
    affinity_keywords: dict[str, list[str]] = field(default_factory=dict)
    affinity_zone_keywords: dict[str, dict[str, int]] = field(default_factory=dict)
    affinity_unknown_category: str = ""

    # --- ocr (scanned pdfs / images) ---
    ocr_enabled: bool = False

    # --- scheduler (feature 18) ---
    scheduler_enabled: bool = True
    reindex_every_hours: int = 6
    digest_schedule: str = "daily"
    digest_chat: int = 0   # telegram chat id to push the daily digest to (0 = off)

    # --- links (feature: save links + watch for updates) ---
    links_dir: str = ""
    links_check_hours: float = 12.0
    links_download: bool = True

    # --- planner (Phase 2: deterministic task scheduler) ---
    # Local event source (testable without Google Calendar). Path to an .ics or
    # .json of external hard commitments that the scheduler must never overlap.
    local_calendar_file: str = ""
    google_calendar_credentials: str = ""   # path to client_secret.json
    google_calendar_enabled: bool = False
    google_calendar_id: str = ""            # dedicated Butler calendar id; "" -> primary
    # Comma-separated calendar ids whose events are imported as hard
    # commitments (lessons, appointments). The dedicated Butler calendar is
    # always read in addition to these. "primary" = the user's main calendar.
    google_read_calendars: str = "primary"
    calendar_sync_schedule: str = "15m"     # cadence for the write-back projection
    # Default working window (24h clock, minutes from midnight). Sleep hours.
    sleep_start: int = 23 * 60 + 0          # 23:00
    sleep_end: int = 7 * 60 + 0             # 07:00
    # Scheduler must never fill 100% of available time (leave buffer).
    buffer_fraction: float = 0.20
    buffer_minutes: int = 15
    # A task may only be placed in a gap if the gap is >= this long.
    min_slot_minutes: int = 25
    # Urgency weights used when ordering tasks (before deadline support).
    urgency_deadline_days: float = 3.0   # how far out a deadline is "close"
    # Depth of plan history kept for undo.
    undo_history_max: int = 50

    # --- indexing ---
    index_hidden: bool = False
    max_file_size_mb: int = 256

    # --- courses (Phase 3: course intelligence) ---
    course_monitor_interval: int = 3600   # default seconds between checks
    # --- food (Phase 3: chef) ---
    recipe_provider: str = "offline"       # offline | web (see food.py)

    # --- web & external knowledge (M4) ---
    # Deterministic web research is INFORMATION ONLY: search/fetch never mutate
    # Butler state and never perform an external action. ``offline`` disables
    # all network access (search returns a controlled "provider unavailable").
    web_enabled: bool = True
    web_search_provider: str = "offline"   # offline | duckduckgo (see web.py)
    web_max_results: int = 8               # search hits considered
    web_max_sources: int = 5               # pages fetched per research
    web_timeout: int = 12                  # per-request timeout (seconds)
    web_max_content_chars: int = 20000     # extracted text kept per source
    web_max_fetch_bytes: int = 2000000     # download cap per page
    web_cache_ttl: int = 3600              # TTL cache lifetime (seconds)
    # Optional allowlist: when non-empty, only these domains may be fetched.
    web_domain_allowlist: list[str] = field(default_factory=list)
    web_user_agent: str = "Butler/4 (+personal assistant)"

    # --- schedule optimization (M5) ---
    # The optimizer is a deterministic, read-only layer over the existing pure
    # solver. It never writes a plan; committing stays behind confirmation/undo.
    optimizer_enabled: bool = True
    optimizer_default_strategy: str = "balanced"  # baseline|deadline_first|risk_first|priority_first|balanced
    optimizer_max_horizon_days: int = 14           # bounded search horizon
    optimizer_max_session_minutes: int = 90        # cap on one contiguous block
    optimizer_max_iterations: int = 50             # bounded local improvement
    optimizer_churn_penalty: float = 1.0           # weight on preserving blocks
    optimizer_fragmentation_penalty: float = 1.0   # weight on fewer splits

    # --- long-term memory + learning (M6) ---
    # Memory is durable, provenance-aware and conservative. Inferred memories
    # are soft and can never become hard constraints.
    memory_enabled: bool = True
    memory_max_scan: int = 500             # bounded retrieval candidate set
    memory_search_limit: int = 8           # default results returned
    memory_context_limit: int = 5          # memories injected into a snapshot
    memory_min_confidence: float = 0.35    # below this, inferred writes are rejected
    memory_routine_min_observations: int = 3
    memory_routine_confidence_min: float = 0.5
    memory_routine_stale_days: int = 21
    memory_temporal_ttl_days: int = 7
    memory_course_stale_days: int = 120
    memory_project_stale_days: int = 30
    memory_estimate_min_samples: int = 3
    memory_estimate_min_ratio: float = 1.15   # learn only if off by >= 15%
    memory_max_value_chars: int = 2000
    memory_allow_web_facts: bool = True

    # --- proactive executive behavior (M7) ---
    # Deterministic candidate generation + a notification policy. The legacy
    # ``proactive_*`` fields below still drive the original alert loop.
    proactive_engine_enabled: bool = True
    proactive_daily_budget: int = 5          # normal proactive messages/day
    proactive_critical_budget: int = 2       # separate allowance for critical
    proactive_cooldown_minutes: int = 180    # per-candidate re-notify cooldown
    proactive_min_priority: str = "medium"   # only >= this priority notifies
    proactive_deadline_risk_ratio: float = 1.0   # remaining/available trigger
    proactive_min_free_window_minutes: int = 60
    proactive_risk_increase: float = 0.15    # project risk delta to notify
    proactive_min_confidence: float = 0.6
    proactive_candidate_expiry_minutes: int = 1440
    proactive_prep_lead_minutes: int = 60    # configured (not invented) lead
    proactive_web_check_enabled: bool = False
    proactive_web_ttl_minutes: int = 360
    proactive_max_candidates: int = 50
    proactive_quiet_critical: bool = True    # critical may bypass quiet hours
    proactive_briefing_enabled: bool = True
    # --- universal tracking / triggers (N2) ---
    tracker_enabled: bool = True
    tracker_schedule: str = "5m"           # cadence of the scheduler job
    tracker_max_per_cycle: int = 50        # bound trackers evaluated per run
    tracker_cooldown_minutes: int = 360    # per-tracker re-notify cooldown
    tracker_max_failures: int = 3          # before a tracker goes ERROR
    # Per-category toggles (all on by default; users may disable individually).
    proactive_cat_deadline_risk: bool = True
    proactive_cat_free_time: bool = True
    proactive_cat_missed_task: bool = True
    proactive_cat_schedule_conflict: bool = True
    proactive_cat_project_risk: bool = True
    proactive_cat_estimate: bool = True
    proactive_cat_routine: bool = True
    proactive_cat_travel: bool = True
    proactive_cat_web_change: bool = True
    proactive_cat_food: bool = True
    proactive_cat_course: bool = True
    # --- NAS / file system (Phase 3) ---
    nas_enabled: bool = False
    nas_dir: str = ""                      # e.g. /mnt/storage
    nas_inbox_dir: str = ""                # e.g. /mnt/storage/Inbox
    samba_share: str = "butler"            # Samba share name to generate
    # --- proactive (Phase 3: proactive butler) ---
    proactive_enabled: bool = True
    proactive_schedule: str = "hourly"     # cadence for background checks
    notify_chat: int = 0                   # telegram chat id (0 = fall back to digest_chat)
    # Notification throttling: quiet hours (minutes from midnight) and a
    # minimum gap between proactive pushes so Butler never spams. A per-cadence
    # cap and a dedup window keep repeated identical alerts down to one.
    notify_quiet_start: int = 22 * 60      # 22:00 (0 disables quiet hours)
    notify_quiet_end: int = 8 * 60         # 08:00
    notify_cooldown_minutes: int = 15      # min gap between pushes
    notify_max_per_cadence: int = 5        # upper bound on pushes per run
    notify_dedup_window_minutes: int = 30  # skip an identical alert this soon

    # --- executive loop (Phase 5.1: daily briefing / daily executive / review) ---
    # The whole executive loop can be switched off (0 cadence also disables it).
    executive_enabled: bool = True
    # Daily briefing cadence, chat and banner (see ``briefing_schedule``).
    briefing_schedule: str = "daily"     # cadence; empty/0 disables
    briefing_chat: int = 0               # telegram chat id (0 => notify_chat/digest_chat)
    briefing_banner: str = "📋 Daily briefing"
    # Daily review cadence, chat and banner.
    review_schedule: str = "daily"
    review_chat: int = 0
    review_banner: str = "📊 Daily review"
    # The executive may push a matching number of recommendations/reviews per day
    # via the proactive path (always deduplicated by delivery markers).
    executive_max_notifications: int = 2

    # --- home assistant (Phase 4.1: presence) ---
    # A long-lived access token for the HA REST API. Never logged and never
    # written to ``to_dict`` (so ``status``/the remote API can't leak it).
    home_assistant_enabled: bool = False
    home_assistant_url: str = ""           # e.g. http://homeassistant.local:8123
    home_assistant_token: str = ""

    # --- context timeline (Phase 4.3) ---
    # Zone-level history of meaningful events. Privacy-first: only zone names,
    # never GPS coordinates or tokens. ``retention_days`` = 0 keeps everything.
    timeline_enabled: bool = True
    timeline_retention_days: int = 30
    # Memory write gate (Phase 6): cap how many timeline events Butler may add
    # per local day before it stops writing to long-term memory (a runaway loop
    # can't infinite-spam its own history). 0 = unlimited (legacy behaviour).
    timeline_max_events_per_day: int = 0

    # --- learned routines & habits (Phase 4.4) ---
    # Deterministic, explainable pattern detection over the context timeline.
    # Routines are SOFT: they only nudge recommendation affinity, never alter a
    # committed plan and never override Google Calendar/deadlines/sleep.
    routines_enabled: bool = True
    routines_min_observations: int = 3   # < this many matches -> not a routine
    routines_confidence_min: float = 0.5 # candidate must reach this confidence
    routines_time_tolerance: int = 90    # minutes spread allowed within a routine
    routines_gap_minutes: int = 90       # max gap between A -> B for a sequence
    routines_scan_days: int = 90         # how far back to scan the timeline
    routines_stale_days: int = 21        # no update for this long -> stale
    routines_max_observations: int = 5   # saturating window for the count term
    routines_affinity_max: int = 4       # cap the soft boost (below explicit +/-6/8)

    # --- reliability, safety & recovery (Phase 6) ---
    # Centralized retry (bounded exponential backoff). A retry keeps the SAME
    # run_id + idempotency key so it never duplicates its side effect.
    retry_max: int = 3                   # max attempts per operation
    retry_base_delay: float = 0.5        # seconds before first retry
    retry_max_delay: float = 30.0        # cap on backoff
    # Rate limiting: token bucket over the consequent-external class so a
    # runaway/retry storm never overwhelms the outside world.
    rate_limit_capacity: float = 20.0    # burst allowed in the window
    rate_limit_window: float = 3600.0    # window seconds (capacity flows back)
    # Circuit breaker: open a failing class after this many consecutive errors,
    # then pause it for ``breaker_cooldown`` seconds before allowing a retry.
    breaker_threshold: int = 5
    breaker_cooldown: float = 300.0
    # Health: a heartbeat older than this is reported stale, and the scheduler
    # reclaims a lease this old (a crashed run).
    heartbeat_max_age: int = 300
    # Audit retention: rows finished after this many days are prunable (0 = keep).
    audit_retention_days: int = 90
    # Offline mode: never reach the outside world (Telegram/GCal/HA all skip).
    # Degraded mode: block external calls but keep local Butler-owned work going.
    offline_mode: bool = False
    degraded_mode: bool = False

    config_path: str = ""

    # ------------------------------------------------------------------
    @property
    def state_dir(self) -> str:
        return str(Path(self.data_dir) / ".butler")

    def db_path(self) -> str:
        return str(Path(self.state_dir) / "butler.db")

    def log_path(self) -> str:
        return str(Path(self.state_dir) / "butler.log")

    def _effective_trash(self) -> str:
        if self.trash_dir:
            return _expand(self.trash_dir)
        return str(Path(self.state_dir) / "trash")

    def ensure_dirs(self) -> None:
        for p in [self.data_dir, self.state_dir]:
            Path(p).mkdir(parents=True, exist_ok=True)
        Path(self._effective_trash()).mkdir(parents=True, exist_ok=True)
        for r in self.roots:
            Path(r).mkdir(parents=True, exist_ok=True)
        if self.course_dir:
            Path(self.course_dir).mkdir(parents=True, exist_ok=True)
        if self.incoming_dir:
            Path(self.incoming_dir).mkdir(parents=True, exist_ok=True)
        if self.links_dir:
            Path(self.links_dir).mkdir(parents=True, exist_ok=True)
        if self.nas_dir:
            try:
                Path(self.nas_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        if self.nas_inbox_dir and self.nas_inbox_dir != self.nas_dir:
            try:
                Path(self.nas_inbox_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = str(path or os.environ.get("BUTLER_CONFIG", DEFAULT_CONFIG_PATH))
        cfg = cls(config_path=path)
        page = {}
        if os.path.exists(path):
            with open(path, "rb") as fh:
                page = tomllib.load(fh)

        data = page.get("storage", {})
        cfg.data_dir = _expand(
            os.environ.get("BUTLER_DATA_DIR", data.get("data_dir", ""))
        ) or _expand("~/ButlerStorage")
        cfg.roots = [
            _expand(r) for r in data.get("roots", ["~/Downloads", "~/Documents"])
        ]
        cfg.index_roots = [
            _expand(r)
            for r in data.get("index_roots", cfg.roots + ["~/Desktop"])
        ]
        cfg.course_dir = _expand(data.get("course_dir", "~/University"))
        cfg.incoming_dir = _expand(data.get("incoming_dir", "~/Incoming"))
        cfg.courses = data.get("courses", [])
        cfg.index_hidden = bool(data.get("index_hidden", False))
        cfg.max_file_size_mb = int(data.get("max_file_size_mb", 256))

        trash = page.get("trash", {})
        cfg.trash_dir = _expand(trash.get("dir", ""))
        cfg.trash_retention_days = int(trash.get("retention_days", 30))

        cfg.smb_shares = [
            SmbShare(
                name=s.get("name", "nas"),
                mountpoint=_expand(s.get("mountpoint", "/mnt/ssd/nas")),
                server=s.get("server", ""),
                share=s.get("share", ""),
                username=s.get("username", ""),
                gvfs=bool(s.get("gvfs", True)),
            )
            for s in page.get("smb", [])
        ]

        usr = page.get("user", {})
        cfg.timezone = os.environ.get("BUTLER_TIMEZONE", usr.get("timezone", ""))

        tg = page.get("telegram", {})
        cfg.telegram_token = os.environ.get("BUTLER_TELEGRAM_TOKEN", tg.get("token", ""))
        cfg.telegram_allowed_users = list(tg.get("allowed_users", []))
        cfg.telegram_open_when_empty = bool(tg.get("open_when_empty",
                                                   cfg.telegram_open_when_empty))

        bk = page.get("backup", {})
        cfg.backup_dir = _expand(bk.get("dir", ""))
        cfg.backup_schedule = bk.get("schedule", "daily")

        rem = page.get("remote", {})
        cfg.remote_enabled = bool(rem.get("enabled", False))
        cfg.remote_port = int(rem.get("port", 7841))
        cfg.remote_token = os.environ.get("BUTLER_REMOTE_TOKEN", rem.get("token", ""))

        ai = page.get("ai", {})
        cfg.llm_api_key = os.environ.get("BUTLER_LLM_KEY", ai.get("api_key", ""))
        cfg.llm_base_url = ai.get("base_url", "")
        cfg.llm_model = ai.get("model", cfg.llm_model)
        cfg.rag_top_k = int(ai.get("rag_top_k", cfg.rag_top_k))

        af = page.get("affinity", {})
        cfg.affinity_keywords = dict(af.get("keywords", {}))
        cfg.affinity_zone_keywords = dict(af.get("zone_keywords", {}))
        cfg.affinity_unknown_category = af.get("unknown_category",
                                               cfg.affinity_unknown_category)

        ocr = page.get("ocr", {})
        cfg.ocr_enabled = bool(ocr.get("enabled", False))

        sched = page.get("scheduler", {})
        cfg.scheduler_enabled = bool(sched.get("enabled", True))
        cfg.reindex_every_hours = int(sched.get("reindex_every_hours", cfg.reindex_every_hours))
        cfg.digest_schedule = sched.get("digest", cfg.digest_schedule)
        cfg.digest_chat = int(sched.get("digest_chat", cfg.digest_chat))

        links = page.get("links", {})
        cfg.links_dir = _expand(links.get("dir", "")) or str(Path(cfg.data_dir) / "Web")
        cfg.links_check_hours = float(links.get("check_hours", cfg.links_check_hours))
        cfg.links_download = bool(links.get("download", cfg.links_download))

        pl = page.get("planner", {})
        cfg.local_calendar_file = _expand(pl.get("local_calendar", "")) or \
            str(Path(cfg.data_dir) / "calendar.ics")
        cfg.google_calendar_credentials = _expand(
            pl.get("google_credentials",
                   pl.get("google_calendar_credentials",
                          os.path.join(os.path.expanduser("~"), ".config", "butler",
                                       "client_secret.json"))))
        cfg.google_calendar_enabled = bool(pl.get("google_calendar",
                                                   cfg.google_calendar_enabled))
        cfg.google_calendar_id = (os.environ.get("BUTLER_GCAL_ID", "") or
                                  pl.get("google_calendar_id", "") or
                                  pl.get("gcal_id", "") or
                                  cfg.google_calendar_id)
        _read_cals = (os.environ.get("BUTLER_GCAL_READ", "") or
                      pl.get("google_read_calendars", "") or
                      pl.get("read_calendars", "") or
                      cfg.google_read_calendars)
        if isinstance(_read_cals, (list, tuple)):
            _read_cals = ",".join(str(x) for x in _read_cals)
        cfg.google_read_calendars = str(_read_cals)
        cfg.calendar_sync_schedule = pl.get("calendar_sync_schedule",
                                            cfg.calendar_sync_schedule)
        cfg.sleep_start = int(pl.get("sleep_start", cfg.sleep_start))
        cfg.sleep_end = int(pl.get("sleep_end", cfg.sleep_end))
        cfg.buffer_fraction = float(pl.get("buffer_fraction", cfg.buffer_fraction))
        cfg.buffer_minutes = int(pl.get("buffer_minutes", cfg.buffer_minutes))
        cfg.min_slot_minutes = int(pl.get("min_slot_minutes", cfg.min_slot_minutes))
        cfg.urgency_deadline_days = float(pl.get("urgency_deadline_days",
                                                 cfg.urgency_deadline_days))
        cfg.undo_history_max = int(pl.get("undo_history_max", cfg.undo_history_max))

        crs = page.get("courses", {})
        cfg.course_monitor_interval = int(
            crs.get("monitor_interval", cfg.course_monitor_interval))

        fd = page.get("food", {})
        cfg.recipe_provider = fd.get("recipe_provider", cfg.recipe_provider)

        web = page.get("web", {})
        cfg.web_enabled = bool(web.get("enabled", cfg.web_enabled))
        cfg.web_search_provider = web.get("search_provider",
                                          cfg.web_search_provider)
        cfg.web_max_results = int(web.get("max_results", cfg.web_max_results))
        cfg.web_max_sources = int(web.get("max_sources", cfg.web_max_sources))
        cfg.web_timeout = int(web.get("timeout", cfg.web_timeout))
        cfg.web_max_content_chars = int(web.get("max_content_chars",
                                                cfg.web_max_content_chars))
        cfg.web_max_fetch_bytes = int(web.get("max_fetch_bytes",
                                              cfg.web_max_fetch_bytes))
        cfg.web_cache_ttl = int(web.get("cache_ttl", cfg.web_cache_ttl))
        cfg.web_domain_allowlist = [str(d).strip().lower()
                                    for d in web.get("domain_allowlist", [])
                                    if str(d).strip()]
        cfg.web_user_agent = web.get("user_agent", cfg.web_user_agent)

        opt = page.get("optimizer", {})
        cfg.optimizer_enabled = bool(opt.get("enabled", cfg.optimizer_enabled))
        cfg.optimizer_default_strategy = opt.get("default_strategy",
                                                 cfg.optimizer_default_strategy)
        cfg.optimizer_max_horizon_days = int(opt.get("max_horizon_days",
                                                     cfg.optimizer_max_horizon_days))
        cfg.optimizer_max_session_minutes = int(opt.get("max_session_minutes",
                                                        cfg.optimizer_max_session_minutes))
        cfg.optimizer_max_iterations = int(opt.get("max_iterations",
                                                   cfg.optimizer_max_iterations))
        cfg.optimizer_churn_penalty = float(opt.get("churn_penalty",
                                                    cfg.optimizer_churn_penalty))
        cfg.optimizer_fragmentation_penalty = float(opt.get("fragmentation_penalty",
                                                            cfg.optimizer_fragmentation_penalty))

        mem = page.get("memory", {})
        cfg.memory_enabled = bool(mem.get("enabled", cfg.memory_enabled))
        cfg.memory_max_scan = int(mem.get("max_scan", cfg.memory_max_scan))
        cfg.memory_search_limit = int(mem.get("search_limit", cfg.memory_search_limit))
        cfg.memory_context_limit = int(mem.get("context_limit", cfg.memory_context_limit))
        cfg.memory_min_confidence = float(mem.get("min_confidence",
                                                  cfg.memory_min_confidence))
        cfg.memory_routine_min_observations = int(mem.get("routine_min_observations",
                                                          cfg.memory_routine_min_observations))
        cfg.memory_routine_confidence_min = float(mem.get("routine_confidence_min",
                                                          cfg.memory_routine_confidence_min))
        cfg.memory_routine_stale_days = int(mem.get("routine_stale_days",
                                                    cfg.memory_routine_stale_days))
        cfg.memory_temporal_ttl_days = int(mem.get("temporal_ttl_days",
                                                   cfg.memory_temporal_ttl_days))
        cfg.memory_course_stale_days = int(mem.get("course_stale_days",
                                                   cfg.memory_course_stale_days))
        cfg.memory_project_stale_days = int(mem.get("project_stale_days",
                                                    cfg.memory_project_stale_days))
        cfg.memory_estimate_min_samples = int(mem.get("estimate_min_samples",
                                                      cfg.memory_estimate_min_samples))
        cfg.memory_estimate_min_ratio = float(mem.get("estimate_min_ratio",
                                                      cfg.memory_estimate_min_ratio))
        cfg.memory_max_value_chars = int(mem.get("max_value_chars",
                                                 cfg.memory_max_value_chars))
        cfg.memory_allow_web_facts = bool(mem.get("allow_web_facts",
                                                  cfg.memory_allow_web_facts))

        pro7 = page.get("proactive", {})
        cfg.proactive_engine_enabled = bool(pro7.get("engine_enabled",
                                                     cfg.proactive_engine_enabled))
        cfg.proactive_daily_budget = int(pro7.get("daily_budget",
                                                  cfg.proactive_daily_budget))
        cfg.proactive_critical_budget = int(pro7.get("critical_budget",
                                                     cfg.proactive_critical_budget))
        cfg.proactive_cooldown_minutes = int(pro7.get("cooldown_minutes",
                                                      cfg.proactive_cooldown_minutes))
        cfg.proactive_min_priority = pro7.get("min_priority",
                                              cfg.proactive_min_priority)
        cfg.proactive_deadline_risk_ratio = float(pro7.get("deadline_risk_ratio",
                                                           cfg.proactive_deadline_risk_ratio))
        cfg.proactive_min_free_window_minutes = int(pro7.get("min_free_window_minutes",
                                                             cfg.proactive_min_free_window_minutes))
        cfg.proactive_risk_increase = float(pro7.get("risk_increase",
                                                     cfg.proactive_risk_increase))
        cfg.proactive_min_confidence = float(pro7.get("min_confidence",
                                                      cfg.proactive_min_confidence))
        cfg.proactive_candidate_expiry_minutes = int(pro7.get("candidate_expiry_minutes",
                                                              cfg.proactive_candidate_expiry_minutes))
        cfg.proactive_prep_lead_minutes = int(pro7.get("prep_lead_minutes",
                                                       cfg.proactive_prep_lead_minutes))
        cfg.proactive_web_check_enabled = bool(pro7.get("web_check_enabled",
                                                        cfg.proactive_web_check_enabled))
        cfg.proactive_web_ttl_minutes = int(pro7.get("web_ttl_minutes",
                                                     cfg.proactive_web_ttl_minutes))
        cfg.proactive_max_candidates = int(pro7.get("max_candidates",
                                                    cfg.proactive_max_candidates))
        cfg.proactive_quiet_critical = bool(pro7.get("quiet_critical",
                                                     cfg.proactive_quiet_critical))
        cfg.proactive_briefing_enabled = bool(pro7.get("briefing_enabled",
                                                       cfg.proactive_briefing_enabled))

        trk = page.get("tracker", {})
        cfg.tracker_enabled = bool(trk.get("enabled", cfg.tracker_enabled))
        cfg.tracker_schedule = trk.get("schedule", cfg.tracker_schedule)
        cfg.tracker_max_per_cycle = int(trk.get("max_per_cycle",
                                                cfg.tracker_max_per_cycle))
        cfg.tracker_cooldown_minutes = int(trk.get("cooldown_minutes",
                                                   cfg.tracker_cooldown_minutes))
        cfg.tracker_max_failures = int(trk.get("max_failures",
                                               cfg.tracker_max_failures))
        for cat in ("deadline_risk", "free_time", "missed_task",
                    "schedule_conflict", "project_risk", "estimate", "routine",
                    "travel", "web_change", "food", "course"):
            setattr(cfg, f"proactive_cat_{cat}",
                    bool(pro7.get(f"cat_{cat}",
                                  getattr(cfg, f"proactive_cat_{cat}", True))))

        nas = page.get("nas", {})
        cfg.nas_enabled = bool(nas.get("enabled", cfg.nas_enabled))
        cfg.nas_dir = _expand(nas.get("dir", "")) or cfg.nas_dir
        cfg.nas_inbox_dir = _expand(nas.get("inbox", "")) \
            or (cfg.nas_dir and str(Path(cfg.nas_dir) / "Inbox") or "")
        cfg.samba_share = nas.get("samba_share", cfg.samba_share)

        pro = page.get("proactive", {})
        cfg.proactive_enabled = bool(pro.get("enabled", cfg.proactive_enabled))
        cfg.proactive_schedule = pro.get("schedule", cfg.proactive_schedule)
        cfg.notify_chat = int(pro.get("notify_chat", cfg.notify_chat or cfg.digest_chat))
        cfg.notify_quiet_start = int(pro.get("quiet_start", cfg.notify_quiet_start))
        cfg.notify_quiet_end = int(pro.get("quiet_end", cfg.notify_quiet_end))
        cfg.notify_cooldown_minutes = int(pro.get("cooldown_minutes",
                                                  cfg.notify_cooldown_minutes))
        cfg.notify_max_per_cadence = int(pro.get("max_per_cadence",
                                                 cfg.notify_max_per_cadence))
        cfg.notify_dedup_window_minutes = int(pro.get("dedup_window_minutes",
                                                      cfg.notify_dedup_window_minutes))

        ha = page.get("home_assistant", {})
        cfg.home_assistant_enabled = bool(ha.get("enabled", cfg.home_assistant_enabled))
        cfg.home_assistant_url = (ha.get("url", cfg.home_assistant_url) or "").rstrip("/")
        cfg.home_assistant_token = os.environ.get("BUTLER_HA_TOKEN",
                                                  cfg.home_assistant_token or ha.get("token", ""))

        ex = page.get("executive", {})
        cfg.executive_enabled = bool(ex.get("enabled", cfg.executive_enabled))
        cfg.briefing_schedule = ex.get("briefing_schedule", cfg.briefing_schedule)
        cfg.briefing_chat = int(ex.get("briefing_chat", cfg.briefing_chat))
        cfg.briefing_banner = ex.get("briefing_banner", cfg.briefing_banner)
        cfg.review_schedule = ex.get("review_schedule", cfg.review_schedule)
        cfg.review_chat = int(ex.get("review_chat", cfg.review_chat))
        cfg.review_banner = ex.get("review_banner", cfg.review_banner)
        cfg.executive_max_notifications = int(ex.get("max_notifications",
                                                     cfg.executive_max_notifications))

        tl = page.get("timeline", {})
        cfg.timeline_enabled = bool(tl.get("enabled", cfg.timeline_enabled))
        cfg.timeline_retention_days = int(tl.get("retention_days",
                                                 cfg.timeline_retention_days))
        cfg.timeline_max_events_per_day = int(tl.get("max_events_per_day",
                                                     cfg.timeline_max_events_per_day))

        rt = page.get("routines", {})
        cfg.routines_enabled = bool(rt.get("enabled", cfg.routines_enabled))
        cfg.routines_min_observations = int(rt.get("min_observations",
                                                   cfg.routines_min_observations))
        cfg.routines_confidence_min = float(rt.get("confidence_min",
                                                   cfg.routines_confidence_min))
        cfg.routines_time_tolerance = int(rt.get("time_tolerance",
                                                 cfg.routines_time_tolerance))
        cfg.routines_gap_minutes = int(rt.get("gap_minutes",
                                              cfg.routines_gap_minutes))
        cfg.routines_scan_days = int(rt.get("scan_days", cfg.routines_scan_days))
        cfg.routines_stale_days = int(rt.get("stale_days", cfg.routines_stale_days))
        cfg.routines_max_observations = int(rt.get("max_observations",
                                                   cfg.routines_max_observations))
        cfg.routines_affinity_max = int(rt.get("affinity_max",
                                               cfg.routines_affinity_max))

        emb = page.get("embed", {})
        cfg.embed_model = emb.get("model", cfg.embed_model)
        cfg.embed_dim = int(emb.get("dim", cfg.embed_dim))

        rel = page.get("reliability", {})
        cfg.retry_max = int(rel.get("retry_max", cfg.retry_max))
        cfg.retry_base_delay = float(rel.get("retry_base_delay", cfg.retry_base_delay))
        cfg.retry_max_delay = float(rel.get("retry_max_delay", cfg.retry_max_delay))
        cfg.rate_limit_capacity = float(rel.get("rate_limit_capacity",
                                                cfg.rate_limit_capacity))
        cfg.rate_limit_window = float(rel.get("rate_limit_window",
                                              cfg.rate_limit_window))
        cfg.breaker_threshold = int(rel.get("breaker_threshold", cfg.breaker_threshold))
        cfg.breaker_cooldown = float(rel.get("breaker_cooldown", cfg.breaker_cooldown))
        cfg.heartbeat_max_age = int(rel.get("heartbeat_max_age", cfg.heartbeat_max_age))
        cfg.audit_retention_days = int(rel.get("audit_retention_days",
                                               cfg.audit_retention_days))
        cfg.offline_mode = bool(rel.get("offline_mode", cfg.offline_mode))
        cfg.degraded_mode = bool(rel.get("degraded_mode", cfg.degraded_mode))

        cfg.ensure_dirs()
        cfg.validate()
        return cfg

    # ---------------------------------------------------------- timezone
    def tz(self) -> Any:
        """Return a timezone object for ``timezone`` (or ``None`` = system local).

        Empty ``timezone`` means the host's local timezone, which is exactly
        what ``datetime.now()``/``datetime.fromtimestamp()`` already use, so an
        unset ``timezone`` never changes behaviour.
        """
        if not self.timezone:
            return None
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        except Exception:  # pragma: no cover — validated at load
            return None

    def now_local(self) -> "datetime":
        """The current instant in the configured timezone (system local if unset)."""
        from datetime import datetime
        tz = self.tz()
        if tz is None:
            return datetime.fromtimestamp(datetime.now().timestamp())
        return datetime.now(tz)

    def local_midnight(self, ts: int) -> int:
        """Absolute timestamp of the local midnight that contains ``ts``.

        Must strip the time on the *aware* datetime when a timezone is set.
        Round-tripping through a naive ``datetime(y, m, d)`` would re-interpret
        it in the host's timezone, shifting the boundary for any tz != host.
        """
        from datetime import datetime
        tz = self.tz()
        if tz is None:
            dt = datetime.fromtimestamp(ts)
            return int(datetime(dt.year, dt.month, dt.day).timestamp())
        aware = datetime.fromtimestamp(ts, tz)
        midnight = aware.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp())

    def validate(self) -> None:
        """Fail fast on invalid configuration (audit: no silent misconfig).

        Deliberately throws ``ValueError`` so a misconfigured Butler refuses to
        start rather than quietly scheduling with nonsense (e.g. a negative
        retention window or a scheduler that can never fit a task). This is
        called at the end of :meth:`load` and is safe to call again manually.
        """
        if self.data_dir and not os.path.isabs(self.data_dir):
            raise ValueError(f"data_dir must be absolute: {self.data_dir}")
        if not (0 <= self.buffer_fraction < 1.0):
            raise ValueError(f"buffer_fraction must be in [0, 1): {self.buffer_fraction}")
        if self.buffer_minutes < 0 or self.min_slot_minutes <= 0:
            raise ValueError(
                f"buffer_minutes and min_slot_minutes must be >= 0 / > 0: "
                f"{self.buffer_minutes} / {self.min_slot_minutes}")
        if not self.schedule_valid():
            raise ValueError(
                f"sleep window is invalid (sleep_start={self.sleep_start}, "
                f"sleep_end={self.sleep_end}); use 0-1439 minute values")
        if self.trash_retention_days < 0 or self.timeline_retention_days < 0:
            raise ValueError("retention days must be >= 0")
        if not (0 < self.remote_port < 65536):
            raise ValueError(f"remote_port out of range: {self.remote_port}")
        if self.timezone:
            self._validate_timezone(self.timezone)
        if self.retry_max < 1:
            raise ValueError(f"retry_max must be >= 1: {self.retry_max}")
        if self.retry_base_delay < 0 or self.retry_max_delay < 0:
            raise ValueError("retry delays must be >= 0")
        if self.retry_max_delay < self.retry_base_delay:
            raise ValueError("retry_max_delay must be >= retry_base_delay")
        if self.rate_limit_capacity <= 0 or self.rate_limit_window <= 0:
            raise ValueError("rate limit capacity and window must be > 0")
        if self.heartbeat_max_age < 1:
            raise ValueError("heartbeat_max_age must be >= 1")
        if self.audit_retention_days < 0:
            raise ValueError("audit_retention_days must be >= 0")
        if self.timeline_max_events_per_day < 0:
            raise ValueError("timeline_max_events_per_day must be >= 0")
        if self.web_timeout < 1 or self.web_max_fetch_bytes < 1 \
                or self.web_max_content_chars < 1 or self.web_cache_ttl < 0:
            raise ValueError("web timeout/limits must be positive (cache_ttl >= 0)")
        if self.web_max_results < 1 or self.web_max_sources < 1:
            raise ValueError("web max_results/max_sources must be >= 1")
        if self.optimizer_max_horizon_days < 1:
            raise ValueError("optimizer_max_horizon_days must be >= 1")
        if self.optimizer_max_session_minutes < 1:
            raise ValueError("optimizer_max_session_minutes must be >= 1")
        if self.optimizer_max_iterations < 0:
            raise ValueError("optimizer_max_iterations must be >= 0")
        if self.optimizer_default_strategy not in (
                "baseline", "deadline_first", "risk_first", "priority_first",
                "balanced"):
            raise ValueError(
                f"invalid optimizer_default_strategy: "
                f"{self.optimizer_default_strategy}")
        if self.memory_max_scan < 1 or self.memory_search_limit < 1 \
                or self.memory_context_limit < 1:
            raise ValueError("memory scan/search/context limits must be >= 1")
        if not (0.0 <= self.memory_min_confidence <= 1.0):
            raise ValueError("memory_min_confidence must be within [0, 1]")
        if self.memory_routine_min_observations < 1:
            raise ValueError("memory_routine_min_observations must be >= 1")
        if self.memory_estimate_min_samples < 1:
            raise ValueError("memory_estimate_min_samples must be >= 1")
        if self.memory_max_value_chars < 1:
            raise ValueError("memory_max_value_chars must be >= 1")
        if self.memory_routine_stale_days < 0 or self.memory_temporal_ttl_days < 0 \
                or self.memory_course_stale_days < 0 \
                or self.memory_project_stale_days < 0:
            raise ValueError("memory staleness windows must be >= 0")
        if self.proactive_daily_budget < 0 or self.proactive_critical_budget < 0:
            raise ValueError("proactive budgets must be >= 0")
        if self.proactive_cooldown_minutes < 0:
            raise ValueError("proactive_cooldown_minutes must be >= 0")
        if self.proactive_min_priority not in (
                "critical", "high", "medium", "low"):
            raise ValueError(
                f"invalid proactive_min_priority: {self.proactive_min_priority}")
        if not (0.0 <= self.proactive_min_confidence <= 1.0):
            raise ValueError("proactive_min_confidence must be within [0, 1]")
        if self.proactive_max_candidates < 1:
            raise ValueError("proactive_max_candidates must be >= 1")
        if self.tracker_max_per_cycle < 1:
            raise ValueError("tracker_max_per_cycle must be >= 1")
        if self.tracker_cooldown_minutes < 0 or self.tracker_max_failures < 1:
            raise ValueError("tracker cooldown/max_failures are invalid")
        if self.proactive_min_free_window_minutes < 0 \
                or self.proactive_prep_lead_minutes < 0 \
                or self.proactive_candidate_expiry_minutes < 1:
            raise ValueError("proactive window/lead/expiry values are invalid")

    def _validate_timezone(self, tz: str) -> None:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid timezone '{tz}': {exc}") from exc
        if tz.upper() in ("LOCAL", "SYSTEM"):
            raise ValueError(
                f"timezone '{tz}' means system-local; leave it empty instead")

    def schedule_valid(self) -> bool:
        """True when the sleep window is a valid pair of minute-of-day values."""
        def ok(v: int) -> bool:
            return 0 <= v <= 1439
        return ok(self.sleep_start) and ok(self.sleep_end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
            "timezone": self.timezone,
            "roots": self.roots,
            "index_roots": self.index_roots,
            "course_dir": self.course_dir,
            "incoming_dir": self.incoming_dir,
            "courses": self.courses,
            "trash_dir": self._effective_trash(),
            "trash_retention_days": self.trash_retention_days,
            "smb_shares": [s.to_dict() for s in self.smb_shares],
            "backup_dir": self.backup_dir,
            "backup_schedule": self.backup_schedule,
            "remote_enabled": self.remote_enabled,
            "remote_port": self.remote_port,
            "embed_model": self.embed_model,
            "embed_dim": self.embed_dim,
            "llm_configured": bool(self.llm_api_key),
            "llm_model": self.llm_model,
            "ocr_enabled": self.ocr_enabled,
            "scheduler_enabled": self.scheduler_enabled,
            "links_dir": self.links_dir,
            "links_check_hours": self.links_check_hours,
            "google_calendar_enabled": self.google_calendar_enabled,
            "google_calendar_id": self.google_calendar_id,
            "google_read_calendars": self.google_read_calendars,
            "calendar_sync_schedule": self.calendar_sync_schedule,
            "local_calendar_file": self.local_calendar_file,
            "sleep_start": self.sleep_start,
            "sleep_end": self.sleep_end,
            "buffer_fraction": self.buffer_fraction,
            "buffer_minutes": self.buffer_minutes,
            "min_slot_minutes": self.min_slot_minutes,
            "telegram_configured": bool(self.telegram_token),
            "telegram_allowed": self.telegram_allowed_users,
            "course_dir": self.course_dir,
            "course_monitor_interval": self.course_monitor_interval,
            "recipe_provider": self.recipe_provider,
            "web_enabled": self.web_enabled,
            "web_search_provider": self.web_search_provider,
            "web_max_results": self.web_max_results,
            "web_max_sources": self.web_max_sources,
            "web_domain_allowlist": list(self.web_domain_allowlist),
            "optimizer_enabled": self.optimizer_enabled,
            "optimizer_default_strategy": self.optimizer_default_strategy,
            "optimizer_max_horizon_days": self.optimizer_max_horizon_days,
            "optimizer_max_session_minutes": self.optimizer_max_session_minutes,
            "optimizer_max_iterations": self.optimizer_max_iterations,
            "optimizer_churn_penalty": self.optimizer_churn_penalty,
            "optimizer_fragmentation_penalty": self.optimizer_fragmentation_penalty,
            "memory_enabled": self.memory_enabled,
            "memory_max_scan": self.memory_max_scan,
            "memory_search_limit": self.memory_search_limit,
            "memory_context_limit": self.memory_context_limit,
            "memory_min_confidence": self.memory_min_confidence,
            "memory_routine_min_observations": self.memory_routine_min_observations,
            "memory_routine_confidence_min": self.memory_routine_confidence_min,
            "memory_routine_stale_days": self.memory_routine_stale_days,
            "memory_temporal_ttl_days": self.memory_temporal_ttl_days,
            "memory_course_stale_days": self.memory_course_stale_days,
            "memory_project_stale_days": self.memory_project_stale_days,
            "memory_estimate_min_samples": self.memory_estimate_min_samples,
            "memory_estimate_min_ratio": self.memory_estimate_min_ratio,
            "memory_max_value_chars": self.memory_max_value_chars,
            "memory_allow_web_facts": self.memory_allow_web_facts,
            "proactive_engine_enabled": self.proactive_engine_enabled,
            "proactive_daily_budget": self.proactive_daily_budget,
            "proactive_critical_budget": self.proactive_critical_budget,
            "proactive_cooldown_minutes": self.proactive_cooldown_minutes,
            "proactive_min_priority": self.proactive_min_priority,
            "proactive_deadline_risk_ratio": self.proactive_deadline_risk_ratio,
            "proactive_min_free_window_minutes": self.proactive_min_free_window_minutes,
            "proactive_risk_increase": self.proactive_risk_increase,
            "proactive_min_confidence": self.proactive_min_confidence,
            "proactive_candidate_expiry_minutes": self.proactive_candidate_expiry_minutes,
            "proactive_prep_lead_minutes": self.proactive_prep_lead_minutes,
            "proactive_web_check_enabled": self.proactive_web_check_enabled,
            "proactive_max_candidates": self.proactive_max_candidates,
            "proactive_quiet_critical": self.proactive_quiet_critical,
            "proactive_briefing_enabled": self.proactive_briefing_enabled,
            "tracker_enabled": self.tracker_enabled,
            "tracker_schedule": self.tracker_schedule,
            "tracker_max_per_cycle": self.tracker_max_per_cycle,
            "tracker_cooldown_minutes": self.tracker_cooldown_minutes,
            "tracker_max_failures": self.tracker_max_failures,
            "nas_enabled": self.nas_enabled,
            "nas_dir": self.nas_dir,
            "nas_inbox_dir": self.nas_inbox_dir,
            "proactive_enabled": self.proactive_enabled,
            "proactive_schedule": self.proactive_schedule,
            "notify_chat": self.notify_chat,
            "notify_quiet_start": self.notify_quiet_start,
            "notify_quiet_end": self.notify_quiet_end,
            "notify_cooldown_minutes": self.notify_cooldown_minutes,
            "notify_max_per_cadence": self.notify_max_per_cadence,
            "notify_dedup_window_minutes": self.notify_dedup_window_minutes,
            "executive_enabled": self.executive_enabled,
            "briefing_schedule": self.briefing_schedule,
            "briefing_chat": self.briefing_chat,
            "briefing_banner": self.briefing_banner,
            "review_schedule": self.review_schedule,
            "review_chat": self.review_chat,
            "review_banner": self.review_banner,
            "executive_max_notifications": self.executive_max_notifications,
            "home_assistant_enabled": self.home_assistant_enabled,
            "home_assistant_url": self.home_assistant_url,
            "home_assistant_configured": bool(self.home_assistant_token),
            "timeline_enabled": self.timeline_enabled,
            "timeline_retention_days": self.timeline_retention_days,
            "timeline_max_events_per_day": self.timeline_max_events_per_day,
            "routines_enabled": self.routines_enabled,
            "routines_min_observations": self.routines_min_observations,
            "routines_confidence_min": self.routines_confidence_min,
            "routines_time_tolerance": self.routines_time_tolerance,
            "routines_gap_minutes": self.routines_gap_minutes,
            "routines_scan_days": self.routines_scan_days,
            "routines_stale_days": self.routines_stale_days,
            "routines_max_observations": self.routines_max_observations,
            "routines_affinity_max": self.routines_affinity_max,
            "retry_max": self.retry_max,
            "retry_base_delay": self.retry_base_delay,
            "retry_max_delay": self.retry_max_delay,
            "rate_limit_capacity": self.rate_limit_capacity,
            "rate_limit_window": self.rate_limit_window,
            "breaker_threshold": self.breaker_threshold,
            "breaker_cooldown": self.breaker_cooldown,
            "heartbeat_max_age": self.heartbeat_max_age,
            "audit_retention_days": self.audit_retention_days,
            "offline_mode": self.offline_mode,
            "degraded_mode": self.degraded_mode,
            "degraded_or_offline": self.degraded_mode or self.offline_mode,
        }
