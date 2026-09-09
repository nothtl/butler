"""Phase 3: Course Intelligence.

Course registry + per-course website monitoring + document pipeline.

Design rules (from the Phase 3 spec):
  * The course website is the AUTHORITATIVE source for deadlines. Search results
    never override an official date.
  * Deterministic change detection (HTTP headers, content hashes, DOM/link
    comparison) runs on EVERY check and is cheap. DeepSeek (``chat._llm``) is
    invoked only when something *meaningful* changed (new asset, changed text).
  * DeepSeek proposes an assignment model (title/deadline/workload/requirements/
    milestones); the deterministic scheduler (``planner``) validates and places
    the resulting task — DeepSeek never schedules.

The monitor can fetch over ``http(s)`` or, for offline testing, a local
directory (a "static site"). That keeps the acceptance test deterministic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import extract

log = logging.getLogger("butler.course")


def _http_headers(url: str) -> int:
    """Return an ETag / Last-Modified header key for a URL."""
    try:
        import requests
        r = requests.head(url, allow_redirects=True, timeout=15)
        return str(r.headers.get("etag") or r.headers.get("last-modified") or "").strip()
    except Exception:
        return ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]


class CourseIntelligence:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.engine = container.engine
        self.indexer = getattr(container, "indexer", None)
        # last-seen snapshot / headers per course, keyed by code
        self._state_file = os.path.join(self.cfg.state_dir, "course_monitor.json")
        self._state = self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._state_file) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            dirn = os.path.dirname(self._state_file)
            os.makedirs(dirn, exist_ok=True)
            with open(self._state_file, "w") as fh:
                json.dump(self._state, fh, indent=2, default=default_json)
        except Exception as exc:  # noqa: BLE001
            log.warning("course state save failed: %s", exc)

    # --------------------------------------------------------------- registry
    def add_course(self, code: str, name: str = "", url: str = "",
                   platform: str = "", semester: str = "") -> dict[str, Any]:
        code = code.strip().upper()
        if not code:
            return {"ok": False, "error": "course code required"}
        url = url.strip()
        cid = self.db.add_course(code, name=name, url=url, platform=platform,
                                 semester=semester,
                                 monitoring_interval=self.cfg.course_monitor_interval)
        course = self.db.course_by_id(cid)
        return {"ok": True, "course_id": cid, "code": code,
                "need_url": not bool(course["url"]),
                "course": dict(course) if course else None}

    def set_url(self, code: str, url: str) -> dict[str, Any]:
        course = self.db.course_by_code(code)
        if not course:
            return {"ok": False, "error": f"no course {code}"}
        self.db.update_course(int(course["id"]), url=url.strip())
        return {"ok": True, "course_id": int(course["id"]), "code": code,
                "url": url.strip()}

    def list_courses(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.courses()]

    def course(self, code: str) -> dict[str, Any] | None:
        row = self.db.course_by_code(code)
        return dict(row) if row else None

    # ----------------------------------------------------------- directories
    def course_dir(self, code: str) -> str:
        code = code.strip().upper()
        base = self.cfg.course_dir or self.cfg.data_dir
        return os.path.join(base, code)

    def subdir(self, code: str, label: str = "Readings") -> str:
        d = os.path.join(self.course_dir(code), label)
        os.makedirs(d, exist_ok=True)
        return d

    # ----------------------------------------------------------------- fetch
    def _fetch(self, url: str) -> tuple[str, dict[str, str]]:
        """Return (text, headers) for http(s) or a local path/site."""
        if url.startswith(("http://", "https://")):
            import requests
            r = requests.get(url, headers={"User-Agent": "Butler/3"},
                             timeout=20)
            r.raise_for_status()
            headers = {k.lower(): v for k, v in r.headers.items()}
            return r.text, headers
        # local file / directory (offline test site)
        p = os.path.abspath(os.path.expanduser(url))
        if os.path.isdir(p):
            idx = os.path.join(p, "index.html")
            if os.path.exists(idx):
                return open(idx, encoding="utf-8", errors="ignore").read(), {"local": "dir"}
            listing = "\n".join(os.listdir(p))
            return listing, {"local": "dir"}
        if os.path.exists(p):
            return open(p, encoding="utf-8", errors="ignore").read(), {"local": "file"}
        return "", {"error": "not found"}

    # ---------------------------------------------------------- link extraction
    @staticmethod
    def _asset_links(html: str, base_url: str) -> list[str]:
        """Locate candidate asset URLs in page HTML (PDFs, office docs, slides)."""
        exts = r"(?:pdf|ppt|pptx|doc|docx|zip|txt|md|csv)"
        hrefs = re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html)
        out: list[str] = []
        for h in hrefs:
            if not re.search(rf"\.{exts}\b", h, re.I):
                continue
            if h.startswith(("http://", "https://")):
                out.append(h)
            elif h.startswith("/"):
                # absolute path on the site
                base = base_url.rstrip("/")
                if base.startswith(("http://", "https://")):
                    from urllib.parse import urlparse
                    u = urlparse(base_url)
                    out.append(f"{u.scheme}://{u.netloc}{h}")
                else:
                    out.append(os.path.join(base_url, h.lstrip("/")))
            else:
                out.append(os.path.join(base_url, h))
        # dedupe, preserve order
        seen: set[str] = set()
        return [u for u in out if not (u in seen or seen.add(u))]

    @staticmethod
    def _page_text(html: str) -> str:
        import re as _re
        body = _re.sub(r"<script.*?</script>|<style.*?</style>", " ", html,
                       flags=_re.S | _re.I)
        body = _re.sub(r"<[^>]+>", " ", body)
        body = _re.sub(r"\s+", " ", body)
        return body.strip()

    # --------------------------------------------------------------- monitoring
    def check_all(self) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        for course in self.db.courses_to_monitor():
            updates += self.check_course(str(course["code"]))
        return updates

    def check_course(self, code: str) -> list[dict[str, Any]]:
        course = self.db.course_by_code(code)
        if not course or not course["url"]:
            return []
        code = str(course["code"])
        url = str(course["url"])
        try:
            html, headers = self._fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch %s failed: %s", url, exc)
            return []
        page_hash = _sha(html)
        key = f"course:{code}"
        snap = self._state.get(key, {})

        # deterministic headers + content hash change detection
        header_key = f"{headers.get('etag', '')}|{headers.get('last-modified', '')}"
        unchanged = (snap.get("page_hash") == page_hash
                     and snap.get("header_key") == header_key)
        if unchanged and snap.get("assets") is not None:
            # page unchanged since last run and we already tracked its assets
            self._state[key] = {"page_hash": page_hash,
                                "header_key": header_key,
                                "assets": snap["assets"]}
            self._save_state()
            return []

        links = self._asset_links(html, url)
        known = self._known_asset_urls(int(course["id"]))
        new_assets = [u for u in links if u not in known]
        text_changed = snap.get("page_hash") not in (None, page_hash) and \
            _sha(self._page_text(html)) != snap.get("text_hash")

        updates: list[dict[str, Any]] = []
        first_run = not snap.get("page_hash")
        if first_run:
            updates.append(self._announce(course, "registered",
                                          f"Monitoring {code}: first snapshot recorded."))
        elif new_assets:
            updates.append(self._announce(
                course, "changed",
                f"Course page changed: {len(new_assets)} new item(s)."))
        elif text_changed:
            updates.append(self._announce(
                course, "changed",
                "Course page content changed (no new files). Logged snapshot."))
        else:
            updates.append(self._announce(course, "unchanged",
                                          f"{code}: checked, nothing meaningful changed."))
        # Always process newly discovered assets (deterministic), even first run.
        for u in new_assets:
            doc = self._process_asset(int(course["id"]), u, code)
            if doc:
                updates.append(self._announce(course, "new",
                                              f"New material detected: {u}", doc=doc))

        self._state[key] = {
            "page_hash": page_hash,
            "header_key": header_key,
            "text_hash": _sha(self._page_text(html)),
            "assets": links,
            "last_checked": int(datetime.now().timestamp()),
        }
        self._save_state()
        return updates

    def _known_asset_urls(self, course_id: int) -> set[str]:
        return {str(r["url"]) for r in self.db.course_documents(course_id) if r["url"]}

    # ------------------------------------------------------ document pipeline
    def _process_asset(self, course_id: int, url: str, code: str) -> dict[str, Any]:
        """Download a discovered asset, store it, extract, index, and record it."""
        filename = os.path.basename(urlsplit_path(url)) or "asset"
        subdir = self._classify_filename(filename)
        dest_dir = self.subdir(code, subdir)
        local, source = self._download(url, dest_dir, filename)
        if not local:
            return {}
        doc_type = "project" if subdir == "Projects" else \
            ("lecture" if subdir == "Lectures" else
             ("exam" if subdir == "Exams" else "reading"))
        title = self._title_from(filename)
        content_hash = self.engine.hash_file(local)
        doc_id = self.db.add_course_document(
            course_id, title, url=url, local_path=local,
            document_type=doc_type, content_hash=content_hash, external_id=url)
        self._index_document(local, title)
        return self._doc_dict(int(doc_id))

    def _classify_filename(self, name: str) -> str:
        n = name.lower()
        if re.search(r"(project|assignment|hw|homework|pa\d|milestone|spec)", n):
            return "Projects"
        if re.search(r"(exam|midterm|final|quiz|test)", n):
            return "Exams"
        if re.search(r"(lecture|slide|lesson|week\d|module)", n):
            return "Lectures"
        return "Readings"

    def _title_from(self, filename: str) -> str:
        return Path(filename).stem.replace("_", " ").replace("-", " ").strip()

    def _download(self, url: str, dest_dir: str, filename: str) -> tuple[str, str]:
        path = os.path.join(dest_dir, filename)
        if os.path.exists(path):
            return path, "cached"
        try:
            if url.startswith(("http://", "https://")):
                import requests
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                data = r.content
                source = "web"
            else:
                data = open(os.path.expanduser(url), "rb").read()
                source = "local"
            os.makedirs(dest_dir, exist_ok=True)
            # avoid collisions
            uniq, _ = self.engine._unique_name(dest_dir, filename)
            with open(uniq, "wb") as fh:
                fh.write(data)
            return uniq, source
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed %s: %s", url, exc)
            return "", "error"

    def _index_document(self, path: str, title: str) -> None:
        if self.indexer is None:
            return
        try:
            row = self.engine.file_row(path) if hasattr(self.engine, "file_row") else None
            self.indexer._index_file(path, {}, with_embeddings=True)
            if hasattr(self.engine, "classify_by_ext"):
                cat = self.engine.classify_by_ext(os.path.basename(path))
            else:
                cat = "University"
            self.db.set_category_by_path(path, cat or "University")
        except Exception as exc:  # noqa: BLE001
            log.debug("index %s failed: %s", path, exc)

    def _doc_dict(self, doc_id: int) -> dict[str, Any]:
        row = self.db.doc_by_id(doc_id)
        return dict(row) if row else {}

    # --------------------------------------------------------- assignment AI
    def understand(self, course_id: int, doc_id: int) -> dict[str, Any]:
        """DeepSeek proposes an assignment model; the scheduler validates it.

        Returns a task payload (or {} if the LLM is unavailable). Never mutates
        the schedule here — the caller/decider decides whether to create blocks.
        """
        doc = self.db.doc_by_id(doc_id)
        if not doc or not doc["local_path"] or not os.path.exists(doc["local_path"]):
            return {}
        text = extract.extract_text(doc["local_path"]).get("text", "")
        if not text or not self._llm_ready():
            return {}
        prompt = (
            "You read a course assignment specification and return a JSON object "
            "with keys: title, deadline (ISO date), est_hours (number), "
            "requirements (list of short strings), dependencies (list), "
            "milestones (list of {day, task}). No prose outside the JSON.",
            text[:6000],
        )
        raw = self._llm(prompt)
        data = _safe_json(raw)
        if not data:
            return {}
        return {
            "doc_id": doc_id,
            "title": data.get("title") or doc["title"],
            "deadline": _to_ts(data.get("deadline")),
            "est_hours": float(data.get("est_hours") or 0),
            "requirements": data.get("requirements") or [],
            "dependencies": data.get("dependencies") or [],
            "milestones": data.get("milestones") or [],
            "official_doc_id": doc_id,
        }

    # ------------------------------------------------------------ conflict logic
    def resolve_deadline(self, official: int | None, other: int | None,
                         other_source: str = "search") -> dict[str, Any]:
        """:class:`A4`: the course website is authoritative. Never replace the
        official deadline with a search-derived one."""
        if official is None:
            if other is not None:
                return {"deadline": other, "used": other_source}
            return {"deadline": None, "used": None}
        return {"deadline": official, "used": "official", "conflict":
                other is not None and other != official and
                {"official": _fmt_day(official), "other": _fmt_day(other)}}

    # -------------------------------------------------------------- notification
    def _announce(self, course: Any, kind: str, message: str,
                  doc: dict[str, Any] = {}) -> dict[str, Any]:
        code = str(course["code"])
        return {"kind": kind, "code": code, "message": f"{code} — {message}",
                "doc": doc}

    # ------------------------------------------------------------------ llm
    def _llm_ready(self) -> bool:
        chat = getattr(self.container, "chat", None)
        return bool(getattr(chat, "_llm_ready", lambda: False)())

    def _llm(self, prompt: tuple[str, str]) -> str | None:
        chat = getattr(self.container, "chat", None)
        if chat is None or not self._llm_ready():
            return None
        try:
            return chat._llm(prompt)
        except Exception:
            return None


def default_json(o: Any) -> str:
    return str(o)


def urlsplit_path(url: str) -> str:
    """Strip scheme/query so the basename is usable as a filename."""
    if url.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        p = urlparse(url).path
        return p or url
    return url


def _safe_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _fmt_day(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %-d") if ts else "?"


def _to_ts(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    try:
        dt = datetime.fromisoformat(str(value).strip())
        return int(dt.timestamp())
    except Exception:
        return 0
