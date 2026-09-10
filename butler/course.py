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

    def suggest_url(self, code: str, name: str = "") -> list[dict[str, Any]]:
        """Propose candidate official course-site URLs for the user to verify.

        Uses the LLM (when configured) to propose the most-likely OFFICIAL
        course page(s) for a course code, then probes reachability. Pure read;
        nothing is written — the user confirms via inline buttons.
        """
        code = code.strip().upper()
        if not self._llm_ready():
            return []
        subject = f"{code}" + (f" ({name})" if name else "")
        prompt = (
            f"Academic course: {subject}. Return ONLY JSON of the form "
            "{\"urls\": [{\"url\": \"https://...\", \"title\": \"...\"}]} with the 2-3 "
            "most likely OFFICIAL course website URLs (the course's own homepage, a "
            "course site, or the instructor's course page). Use real plausible URLs "
            "for this course code/university. No prose outside the JSON.",
            f"course: {subject}",
        )
        raw = self._llm(prompt)
        data = _safe_json(raw)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for u in (data.get("urls") or [])[:3]:
            url = str(u.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                candidates.append({"url": url,
                                   "title": str(u.get("title") or url)})
        for c in candidates:
            c["reachable"] = self._http_ok(c["url"])
        return candidates

    def _http_ok(self, url: str) -> bool:
        """Lightweight reachability probe; returns the last-start tag header
        too so a reachable-but-header-less page still counts as reachable."""
        try:
            import requests
            r = requests.head(url, allow_redirects=True, timeout=12)
            if r.status_code < 400:
                return True
            r = requests.get(url, allow_redirects=True, timeout=12,
                             headers={"User-Agent": "Butler/3"}, stream=True)
            return r.status_code < 400
        except Exception:
            return False

    def list_courses(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.courses()]

    def course(self, code: str) -> dict[str, Any] | None:
        row = self.db.course_by_code(code)
        return dict(row) if row else None

    def remove_course(self, code: str) -> dict[str, Any]:
        code = code.strip().upper()
        course = self.db.course_by_code(code)
        if not course:
            return {"ok": False, "error": f"no course {code}"}
        self.db.delete_course(int(course["id"]))
        self._state.pop(f"course:{code}", None)
        self._save_state()
        return {"ok": True, "code": code}

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
        from urllib.parse import urljoin
        out: list[str] = []
        for h in hrefs:
            if not re.search(rf"\.{exts}\b", h, re.I):
                continue
            if base_url.startswith(("http://", "https://")):
                # Resolve absolute, protocol-relative (//host/...), root-relative
                # (/path), dot-relative (../) and query URLs against the site.
                joined = urljoin(base_url, h)
                out.append(joined)
            elif h.startswith(("http://", "https://")):
                # An http URL inside a local (offline) feed — keep it verbatim.
                out.append(h)
            else:
                # Local directory feed: treat as an on-disk path under base_url.
                out.append(os.path.join(base_url, h.lstrip("/")))
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

        # Remember any public class-time calendar the page exposes so the
        # planner can block those hours later.
        cal = self._discover_calendar_url(html)
        if cal and str(course["calendar_url"] or "") != cal:
            self.db.update_course(int(course["id"]), calendar_url=cal)

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
        new_docs = []
        for u in new_assets:
            doc = self._process_asset(int(course["id"]), u, code)
            if doc:
                new_docs.append(doc)
                updates.append(self._announce(course, "new",
                                              f"New material detected: {u}", doc=doc))

        # Course -> Assignment -> Task: have DeepSeek *propose* an assignment
        # model for any newly detected project/spec and materialise a task. The
        # scheduler remains deterministic; this is deduped by task_id.
        if new_docs and self._llm_ready():
            result = self.sync_assignments(code)
            for item in result.get("created", []) + result.get("updated", []):
                state = "updated" if item in result.get("updated", []) else "understood"
                updates.append({
                    "kind": "assignment", "code": code,
                    "message": f"{code} — assignment {state}: {item['title']}",
                    "task": item})

        # Course page -> dated tasks: the site's schedule carries the real due
        # dates. Re-scrape whenever the page text changed (or on first run).
        if self._llm_ready() and (first_run or text_changed):
            page_res = self.sync_page_assignments(code)
            for item in page_res.get("created", []) + page_res.get("updated", []):
                state = "updated" if item in page_res.get("updated", []) else "understood"
                updates.append({
                    "kind": "assignment", "code": code,
                    "message": f"{code} — assignment {state}: {item['title']}",
                    "task": item})

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
        # Graded work first (project specs, homeworks, programming assignments).
        if re.search(r"(project|assignment|homework|\bhw\s?\d|\bpa\s?\d|milestone|spec)", n):
            return "Projects"
        if re.search(r"(exam|midterm|final|quiz|test)", n):
            return "Exams"
        # Lecture/discussion/section material is NOT an assignment: match the
        # common ``lec01``/``disc01``/``sec01`` shorthands too, otherwise those
        # slide decks fall through to "Readings" and the assignment LLM invents
        # a deadline for them.
        if re.search(r"(lecture|slides?|lesson|\blec\s?\d|\bdisc(ussion)?\s?\d|"
                     r"\bsec(tion)?\s?\d|week\s?\d|module|recitation)", n):
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

    # ------------------------------------------------------ assignment -> task
    def sync_assignments(self, code: str, force: bool = False) -> dict[str, Any]:
        """Course -> Assignment -> Task -> Schedule pipeline.

        For every course document that looks like an assignment/spec, ask
        DeepSeek to *propose* a structured assignment model (title, official
        deadline, estimated workload, requirements, dependencies, milestones).
        The deterministic DB layer then creates (or updates) the task and the
        deterministic planner places its study blocks. DeepSeek never schedules
        — it only describes; the solver decides every start/end.

        Idempotency & updates:
          * a doc that already produced a task is re-understood only when its
            content hash changed (or ``force=True``); if the model shifted (e.g.
            a revised deadline) the EXISTING task is updated in place, never
            recreated — so no duplicate task ever appears;
          * the planner re-solves today's plan from scratch, so no duplicate
            schedule blocks accumulate;
          * with no official deadline, or when the LLM is unavailable/invalid,
            the doc is skipped — Butler never invents a task.

        Returns ``{created, updated, count}``; each entry carries a ``schedule``
        dict (its placed study slots + deadline capacity / conflict report).
        """
        course = self.db.course_by_code(code)
        if not course:
            return {"ok": False, "error": f"no course {code}"}
        cid = int(course["id"])
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        to_place: list[dict[str, Any]] = []
        for doc in self.db.course_documents(cid):
            row = dict(doc)
            if str(row.get("document_type") or "") not in (
                    "project", "assignment", "spec", "reading"):
                continue
            doc_id = int(row["id"])
            stored = self._stored_understanding(row.get("understanding") or "")
            link_changed = str(row.get("content_hash") or "") != stored.get("content_hash")
            task_id = int(row.get("task_id") or 0)
            if task_id and not (link_changed or force):
                continue  # unchanged + already pipeline'd -> idempotent no-op
            model = self.understand(cid, doc_id)
            if not model or not model.get("deadline"):
                continue  # no official deadline / LLM unavailable -> no bogus task
            fields = self._task_fields(code, row, model)
            if task_id:
                if self._apply_update(task_id, fields):
                    item = {"doc_id": doc_id, "task_id": task_id,
                            "title": fields["title"], "deadline": fields["deadline"],
                            "est_hours": model.get("est_hours"),
                            "est_minutes": fields["est_minutes"]}
                    updated.append(item)
                    to_place.append({"task_id": task_id,
                                     "est_minutes": fields["est_minutes"],
                                     "deadline": fields["deadline"]})
                    self._record_understanding(doc_id, row, model, task_id)
            else:
                task_id = self.db.add_task(fields["title"], detail=fields["detail"],
                                           deadline=fields["deadline"],
                                           priority=fields["priority"],
                                           est_minutes=fields["est_minutes"],
                                           tags=fields["tags"])
                if not task_id:
                    continue
                item = {"doc_id": doc_id, "task_id": task_id,
                        "title": fields["title"], "deadline": fields["deadline"],
                        "est_hours": model.get("est_hours"),
                        "est_minutes": fields["est_minutes"]}
                created.append(item)
                to_place.append({"task_id": task_id,
                                 "est_minutes": fields["est_minutes"],
                                 "deadline": fields["deadline"]})
                self._record_understanding(doc_id, row, model, task_id)
        schedule = self._place_affected(to_place)
        for item in created + updated:
            item["schedule"] = schedule.get(item["task_id"], {})
        return {"ok": True, "code": code, "created": created, "updated": updated,
                "count": len(created) + len(updated)}

    # ---------------------------------------------------- page -> assignments
    def sync_page_assignments(self, code: str,
                              force: bool = False) -> dict[str, Any]:
        """Scrape the course *page* (schedule/announcements) for graded items
        and materialise dated tasks.

        The website -- not the slide PDFs -- is the authoritative source for
        *when* something is due. DeepSeek reads the page text and proposes a
        list of items; the deterministic layer dedupes each item by a stable
        key (so re-running never duplicates), derives priority from the
        deadline and hands the tasks to the planner. No due date -> no task.
        """
        course = self.db.course_by_code(code)
        if not course or not course["url"]:
            return {"ok": False, "error": f"no course {code}"}
        if not self._llm_ready():
            return {"ok": True, "code": code, "created": [], "updated": [],
                    "count": 0}
        cid = int(course["id"])
        url = str(course["url"])
        try:
            html, _ = self._fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("page fetch failed %s: %s", url, exc)
            return {"ok": False, "error": str(exc)}
        text = self._page_text(html)
        page_hash = _sha(text)
        today = datetime.now().strftime("%A %Y-%m-%d")
        system = (
            "You extract graded items with explicit due dates from a course "
            "website. Return ONLY a JSON object shaped like "
            '{"assignments":[{"title":str,"due":"YYYY-MM-DD",'
            '"time":"HH:MM" or "","est_hours":number,'
            '"kind":"homework|project|exam|quiz"}]}. '
            f"Today is {today}; resolve month/day names against the current "
            "academic year. Include every homework, project, midterm, quiz and "
            "final that has a stated due date. Never invent a date: omit any "
            "item without one. No prose outside the JSON."
        )
        raw = self._llm((system, text[:9000]))
        data = _safe_json(raw)
        items = data.get("assignments") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return {"ok": True, "code": code, "created": [], "updated": [],
                    "count": 0}
        created: list[dict[str, Any]] = []
        updated: list[dict[str, Any]] = []
        to_place: list[dict[str, Any]] = []
        seen: set[str] = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            due = str(it.get("due") or "").strip()
            if not title or not due:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
            if not slug or slug in seen:
                continue
            seen.add(slug)
            deadline = _due_ts(due, it.get("time") or "")
            if not deadline:
                continue
            external_id = f"{url}#assignment/{slug}"
            doc_id = self.db.add_course_document(
                cid, title, document_type="assignment",
                content_hash=page_hash, external_id=external_id)
            row = dict(self.db.doc_by_id(doc_id))
            stored = self._stored_understanding(row.get("understanding") or "")
            task_id = int(row.get("task_id") or 0)
            if task_id and not force and stored.get("content_hash") == page_hash:
                continue
            hours = float(it.get("est_hours") or 0)
            if hours <= 0:
                hours = {"project": 6.0, "exam": 4.0, "quiz": 2.0}.get(
                    str(it.get("kind") or "").lower(), 2.0)
            model = {
                "title": title, "deadline": deadline, "est_hours": hours,
                "requirements": [
                    "due %s%s" % (due,
                                  (" %s" % it["time"]) if it.get("time") else ""),
                    "kind: %s" % (it.get("kind") or "assignment"),
                ],
            }
            fields = self._task_fields(code, {"local_path": "", "url": url},
                                       model)
            item = {"doc_id": doc_id, "task_id": 0, "title": fields["title"],
                    "deadline": fields["deadline"], "est_hours": hours,
                    "est_minutes": fields["est_minutes"]}
            if task_id:
                if self._apply_update(task_id, fields):
                    item["task_id"] = task_id
                    updated.append(item)
                    to_place.append({"task_id": task_id,
                                     "est_minutes": fields["est_minutes"],
                                     "deadline": fields["deadline"]})
            else:
                new_id = self.db.add_task(
                    fields["title"], detail=fields["detail"],
                    deadline=fields["deadline"], priority=fields["priority"],
                    est_minutes=fields["est_minutes"], tags=fields["tags"])
                if not new_id:
                    continue
                task_id = new_id
                item["task_id"] = task_id
                created.append(item)
                to_place.append({"task_id": task_id,
                                 "est_minutes": fields["est_minutes"],
                                 "deadline": fields["deadline"]})
            self._record_understanding(
                doc_id, {"content_hash": page_hash, "version": 1}, model, task_id)
        schedule = self._place_affected(to_place)
        for item in created + updated:
            item["schedule"] = schedule.get(item["task_id"], {})
        return {"ok": True, "code": code, "created": created, "updated": updated,
                "count": len(created) + len(updated)}

    @staticmethod
    def _discover_calendar_url(html: str) -> str:
        """Find a public Google Calendar .ics feed referenced by the page."""
        from urllib.parse import unquote, quote
        m = re.search(
            r"calendar\.google\.com/calendar/ical/([^\"'<>/]+)/public/basic\.ics",
            html)
        if m:
            return ("https://calendar.google.com/calendar/ical/"
                    f"{m.group(1)}/public/basic.ics")
        for m in re.finditer(
                r"calendar\.google\.com/calendar/embed\?[^\"'<>]*?src=([^\"'&<>]+)",
                html):
            cid = unquote(m.group(1))
            if cid.startswith("$") or "{" in cid:
                continue
            return ("https://calendar.google.com/calendar/ical/"
                    f"{quote(cid, safe='@')}/public/basic.ics")
        return ""

    def _place_affected(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Hand the newly created/updated tasks to the deterministic planner.
        Returns ``{task_id: {slots, capacity report}}`` (or {} when offline)."""
        planner = getattr(self.container, "planner", None)
        if not planner or not items:
            return {}
        try:
            return planner.schedule_assignment_blocks(items)
        except Exception as exc:  # noqa: BLE001
            log.warning("assignment scheduling failed: %s", exc)
            return {}

    def _record_understanding(self, doc_id: int, row: dict[str, Any],
                              model: dict[str, Any], task_id: int = 0) -> None:
        payload = {"content_hash": str(row.get("content_hash") or ""),
                   "version": int(row.get("version") or 1), "model": model}
        fields: dict[str, Any] = {"understanding": json.dumps(payload)}
        if task_id:
            fields["task_id"] = int(task_id)
        self.db.update_course_document(doc_id, **fields)

    def _stored_understanding(self, raw: str) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _task_fields(self, code: str, doc: dict[str, Any],
                     model: dict[str, Any]) -> dict[str, Any]:
        """Deterministically derive the task columns from an assignment model.
        Priority derives from deadline proximity (never from the LLM)."""
        deadline = int(model.get("deadline") or 0)
        est_hours = float(model.get("est_hours") or 1.0)
        est_minutes = min(8 * 60, max(15, int(est_hours * 60)))
        now = int(datetime.now().timestamp())
        days = max(0, (deadline - now) // 86400)
        if days <= 2:
            priority = 5
        elif days <= 5:
            priority = 4
        elif days <= 10:
            priority = 3
        elif days <= 20:
            priority = 2
        else:
            priority = 1
        reqs = model.get("requirements") or []
        detail = "Source: %s" % (doc.get("local_path") or doc.get("url") or "")
        if reqs:
            detail += "\n" + "\n".join("- %s" % r for r in reqs)
        deps = model.get("dependencies") or []
        if deps and not reqs:
            detail += "\n" + "\n".join("- depends: %s" % d for d in deps)
        return {"title": str(model.get("title") or doc["title"]),
                "detail": detail, "deadline": deadline, "priority": priority,
                "est_minutes": est_minutes, "tags": f"{code} assignment"}

    def _apply_update(self, task_id: int, fields: dict[str, Any]) -> int:
        """Update an existing task in place (never create a duplicate). Returns
        1 when something changed, 0 when the task already matches (no-op)."""
        cur = self.db.task_by_id(task_id)
        if cur and (str(cur["title"]) == str(fields["title"])
                    and int(cur["deadline"] or 0) == int(fields["deadline"] or 0)
                    and int(cur["est_minutes"] or 0) == int(fields["est_minutes"] or 0)
                    and int(cur["priority"] or 3) == int(fields["priority"] or 3)):
            return 0
        self.db.update_task(task_id, title=fields["title"], detail=fields["detail"],
                            deadline=fields["deadline"], priority=fields["priority"],
                            est_minutes=fields["est_minutes"], tags=fields["tags"])
        return 1

    def _create_task_from_model(self, code: str, doc: dict[str, Any],
                                model: dict[str, Any]) -> int:
        """Deterministically materialise a scheduler task from an assignment
        model. Priority derives from deadline proximity (never from the LLM)."""
        fields = self._task_fields(code, doc, model)
        return self.db.add_task(fields["title"], detail=fields["detail"],
                                deadline=fields["deadline"],
                                priority=fields["priority"],
                                est_minutes=fields["est_minutes"],
                                tags=fields["tags"])

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


def _due_ts(date_str: Any, time_str: Any = "") -> int:
    """Timestamp for a page-scraped due date (date-only -> end of day)."""
    raw = str(date_str or "").strip()
    if not raw:
        return 0
    s = raw
    t = str(time_str or "").strip()
    if "T" not in s and t:
        s = f"{s}T{t}"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return 0
    if "T" not in raw:
        dt = dt.replace(hour=23, minute=59)
    return int(dt.timestamp())
