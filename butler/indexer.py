"""Indexer.

Walks Butler's managed roots, hashes files, extracts text, and populates the
search index (files + FTS5 content + embeddings). Incremental: only re-hashes
and re-extracts when size/mtime changed.

Feeds features 4, 5, 9, 13, 14 and the course route (feature 17).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .config import Config
from .db import DB
from .embed import Embedder, mean_pool, normalize, pack
from .engine import classify_by_ext, extract_course_code
from .extract import chunk_text, extract_text

log = logging.getLogger("butler.indexer")


class Indexer:
    def __init__(self, cfg: Config, db: DB, embedder: Embedder | None = None):
        self.cfg = cfg
        self.db = db
        self.embedder = embedder or Embedder(cfg.embed_model)

    def index_root(self, root: str, with_embeddings: bool = True) -> dict[str, Any]:
        stats = {"scanned": 0, "indexed": 0, "skipped": 0, "errors": 0,
                 "extracted": 0, "embedded": 0}
        root = os.path.realpath(root)
        if not os.path.isdir(root):
            return stats
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith(".") and not self.cfg.index_hidden:
                    continue
                path = os.path.join(dirpath, fname)
                stats["scanned"] += 1
                try:
                    self._index_file(path, stats, with_embeddings)
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    log.debug("index error %s: %s", path, exc)
        return stats

    def _index_file(self, path: str, stats: dict[str, Any], with_embeddings: bool) -> None:
        if not os.path.isfile(path):
            return
        st = os.stat(path)
        size = st.st_size
        if self.cfg.max_file_size_mb and size > self.cfg.max_file_size_mb * 1024 * 1024:
            stats["skipped"] += 1
            return
        existing = self.db.get_file(path)
        if existing and existing["size"] == size and int(existing["mtime"]) == int(st.st_mtime):
            stats["indexed"] += 1
            return

        hash_ = self.cfg_hash(path)
        ext = os.path.splitext(path)[1].lower()
        category = classify_by_ext(path)
        f = extract_text(path, ocr=self.cfg.ocr_enabled)
        text = f["text"]
        meta_json = json.dumps(f["meta"]) if f["meta"] else None

        file_id = self.db.upsert_file(
            path, os.path.basename(path), ext, size, int(st.st_mtime),
            hash_=hash_, mime=_mime(ext), meta=meta_json, category=category,
        )
        self.db.delete_chunks(file_id)

        if text:
            chunks = chunk_text(text)
            if chunks:
                self.db.add_chunks(file_id, chunks)
                stats["extracted"] += 1
                if with_embeddings and self.embedder.ready:
                    try:
                        vecs = self.embedder.embed(chunks)
                        if vecs:
                            pooled = normalize(mean_pool(vecs, self.cfg.embed_dim))
                            self.db.set_embedding(file_id, self.cfg.embed_model,
                                                  self.cfg.embed_dim, len(chunks),
                                                  pack(pooled))
                            stats["embedded"] += 1
                    except Exception as exc:
                        log.debug("embed error: %s", exc)

                if category == "University":
                    code = extract_course_code(path) or extract_course_code(text[:4000])
                    if code:
                        self.db.set_category(file_id, f"Course:{code}", update_files=False)

        stats["indexed"] += 1

    def cfg_hash(self, path: str) -> str:
        try:
            import hashlib
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
            return h.hexdigest()
        except OSError:
            return ""

    def prune_missing(self) -> int:
        """Remove index entries whose file no longer exists."""
        removed = 0
        rows = self.db.query("SELECT id, path FROM files")
        for r in rows:
            if not os.path.exists(r["path"]):
                self.db.delete_file(int(r["id"]))
                removed += 1
        return removed


def _mime(ext: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".csv": "text/csv",
        ".json": "application/json",
        ".html": "text/html",
        ".png": "image/png",
        ".jpg": "image/jpeg",
    }.get(ext, "application/octet-stream")
