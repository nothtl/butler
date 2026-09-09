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
    # --- NAS / file system (Phase 3) ---
    nas_enabled: bool = False
    nas_dir: str = ""                      # e.g. /mnt/storage
    nas_inbox_dir: str = ""                # e.g. /mnt/storage/Inbox
    samba_share: str = "butler"            # Samba share name to generate
    # --- proactive (Phase 3: proactive butler) ---
    proactive_enabled: bool = True
    proactive_schedule: str = "hourly"     # cadence for background checks
    notify_chat: int = 0                   # telegram chat id (0 = fall back to digest_chat)

    # --- home assistant (Phase 4.1: presence) ---
    # A long-lived access token for the HA REST API. Never logged and never
    # written to ``to_dict`` (so ``status``/the remote API can't leak it).
    home_assistant_enabled: bool = False
    home_assistant_url: str = ""           # e.g. http://homeassistant.local:8123
    home_assistant_token: str = ""

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
        cfg.courses = data.get("courses", ["CS168"])
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

        tg = page.get("telegram", {})
        cfg.telegram_token = os.environ.get("BUTLER_TELEGRAM_TOKEN", tg.get("token", ""))
        cfg.telegram_allowed_users = list(tg.get("allowed_users", []))

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
        cfg.google_calendar_enabled = bool(pl.get("google_calendar", True))
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

        ha = page.get("home_assistant", {})
        cfg.home_assistant_enabled = bool(ha.get("enabled", cfg.home_assistant_enabled))
        cfg.home_assistant_url = (ha.get("url", cfg.home_assistant_url) or "").rstrip("/")
        cfg.home_assistant_token = os.environ.get("BUTLER_HA_TOKEN",
                                                  cfg.home_assistant_token or ha.get("token", ""))

        emb = page.get("embed", {})
        cfg.embed_model = emb.get("model", cfg.embed_model)
        cfg.embed_dim = int(emb.get("dim", cfg.embed_dim))

        cfg.ensure_dirs()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": self.data_dir,
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
            "nas_enabled": self.nas_enabled,
            "nas_dir": self.nas_dir,
            "nas_inbox_dir": self.nas_inbox_dir,
            "proactive_enabled": self.proactive_enabled,
            "proactive_schedule": self.proactive_schedule,
            "notify_chat": self.notify_chat,
            "home_assistant_enabled": self.home_assistant_enabled,
            "home_assistant_url": self.home_assistant_url,
            "home_assistant_configured": bool(self.home_assistant_token),
        }
