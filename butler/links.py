"""Saved-link collector (feature: save links, watch for updates, then act).

The user saves links (via CLI ``butler add <url>``, Telegram ``/add``, a plain
message containing a URL, or the remote/MCP API). On each check — run by the
scheduler every ``links_check_hours`` (default 12h), or on demand via ``butler
check`` — we fetch each link, hash its *extracted* content, and only act when it
changed:

  * new / update  -> download the content, route it into a managed folder and
    index it so it becomes searchable / surfaced via ``find``, ``ask``, etc.
  * unchanged     -> leave it alone (no churn).
  * error         -> record it and keep waiting (retried next cycle).

Plain HTML is saved as a Markdown note (title + source + visible text). PDFs
are downloaded as-is. Everything is written under ``links_dir`` and indexed.
"""

from __future__ import annotations

import hashlib
import html.parser
import logging
import os
import re
import time
import urllib.parse
from typing import Any

log = logging.getLogger("butler.links")


class _TextOnly(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip == 0 and data.strip():
            self.parts.append(data.strip())


def _html_to_text(html_text: str) -> str:
    p = _TextOnly()
    p.feed(html_text)
    text = " ".join(p.parts)
    return re.sub(r"\s+", " ", text).strip()


def _copy_body(html_text: str) -> str:
    """Extract the main <body>/<article> region (fallback: whole page)."""
    m = re.search(r"<body[^>]*>(.*?)</body>", html_text, re.S | re.I)
    return m.group(1) if m else html_text


def _title_of(html_text: str, url: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    if m:
        t = m.group(1).strip()
        if t:
            return re.sub(r"\s+", " ", t)
    return urllib.parse.urlparse(url).netloc or url


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-") or "page"
    return s[:60]


def _now() -> int:
    return int(time.time())


class LinkCollector:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db

    # ---------------- public ----------------
    def add(self, url: str, tag: str = "", fetch: bool = True) -> dict[str, Any]:
        url = url.strip().strip('"').strip("'")
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        lid = self.db.add_link(url, tag)
        log.info("link added #%s %s", lid, url)
        if fetch:
            return self._fetch(lid)
        return {"id": lid, "url": url, "status": "added"}

    def check_all(self) -> list[dict[str, Any]]:
        return [self._fetch(int(r["id"])) for r in self.db.links()]

    def list_links(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.links()]

    def remove(self, link_id: int) -> bool:
        row = self.db.link_by_id(link_id)
        if not row:
            return False
        self.db.execute("DELETE FROM links WHERE id=?", (link_id,))
        return True

    # ---------------- internals ----------------
    def _get(self, url: str) -> Any:
        import requests
        resp = requests.get(
            url,
            timeout=30,
            headers={"User-Agent": "Butler/1.0 (link collector)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp

    def _fetch(self, lid: int) -> dict[str, Any]:
        row = self.db.link_by_id(lid)
        if not row:
            return {"id": lid, "status": "error", "note": "unknown link"}
        url = row["url"]
        try:
            resp = self._get(url)
        except Exception as exc:  # noqa: BLE001
            self.db.update_link_state(lid, status="error", last_checked=_now(),
                                      note=str(exc)[:240])
            return {"id": lid, "url": url, "status": "error", "note": str(exc)[:240]}

        ct = (resp.headers.get("content-type", "") or "").lower()
        is_pdf = "pdf" in ct or url.lower().split("?")[0].endswith(".pdf")

        if is_pdf:
            title = row["title"] or urllib.parse.urlparse(url).netloc
            body = resp.content
            content_hash = self._hash_bytes(body)
            kind = "pdf"
        else:
            html_text = resp.text
            title = row["title"] or _title_of(html_text, url)
            text = _html_to_text(_copy_body(html_text))
            content_hash = self._hash_text(text)
            kind = "html"

        changed = not row["hash"] or row["hash"] != content_hash
        saved_path = row["path"] or ""
        note = ""
        if changed and self.cfg.links_download:
            path = self._save(url, title, body if is_pdf else text, kind)
            if path:
                saved_path = path
                try:
                    self.container.indexer._index_file(
                        path, {}, with_embeddings=True)
                except Exception as exc:  # noqa: BLE001
                    log.debug("index saved link error: %s", exc)
                status = "downloaded"
            else:
                status = "updated"
                note = "changed but not downloaded"
        elif changed:
            status = "updated"
        else:
            status = "unchanged"

        self.db.update_link_state(
            lid, title=title, status=status, hash=content_hash,
            last_checked=_now(), path=saved_path, note=note,
        )
        return {"id": lid, "url": url, "title": title, "status": status,
                "path": saved_path, "note": note, "changed": changed}

    def _save(self, url: str, title: str, content: Any, kind: str) -> str:
        dest = self.cfg.links_dir or "/tmp/"
        os.makedirs(dest, exist_ok=True)
        if kind == "pdf":
            filename = _slug(title) + ".pdf"
        else:
            filename = _slug(title) + ".md"
        target = self._unique(os.path.join(dest, filename))
        if kind == "pdf":
            with open(target, "wb") as fh:
                fh.write(content)
        else:
            body = f"# {title}\n\nSource: {url}\nFetched: {time.strftime('%Y-%m-%d %H:%M')}\n\n---\n\n{content}\n"
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(body)
        self.db.update_link_state(
            self._current_id(url), path=target,
        )
        return target

    @staticmethod
    def _unique(path: str) -> str:
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        return f"{base}_{i}{ext}"

    def _current_id(self, url: str) -> int:
        row = self.db.one("SELECT id FROM links WHERE url=? ORDER BY id DESC LIMIT 1", (url,))
        return int(row["id"]) if row else 0

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {
            "id": int(r["id"]), "url": r["url"], "title": r["title"] or "",
            "tag": r["tag"] or "", "status": r["status"] or "added",
            "last_checked": r["last_checked"], "path": r["path"] or "",
            "note": r["note"] or "",
        }
