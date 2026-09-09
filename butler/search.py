"""Search layer.

* filename search        (features 4, 5, 9)
* full-text content      (features 4, 5, 9) — FTS5 BM25
* semantic search        (feature 14) — cached embeddings + cosine fallback

Also builds the "latest resume" answer (Test 5) by searching name + content +
metadata and ranking by recency/recency-plus-relevance.
"""

from __future__ import annotations

import time
import re as _re
from typing import Any

from .config import Config
from .db import DB
from .embed import Embedder, cosine, normalize, unpack

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "with",
    "is", "are", "was", "were", "my", "me", "please", "find", "locate",
    "search", "look", "show", "where", "which", "this", "that", "it",
}


def _tokens(query: str) -> list[str]:
    words = _re.findall(r"[A-Za-z0-9\-_]+", query.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 1]


class Search:
    def __init__(self, cfg: Config, db: DB, embedder: Embedder | None = None):
        self.cfg = cfg
        self.db = db
        self.embedder = embedder or Embedder(cfg.embed_model)
        self._vcache: set[str] | None = None

    def files(self, term: str, limit: int = 50) -> list[dict[str, Any]]:
        tokens = _tokens(term)
        if not tokens:
            rows = self.db.search_files(term, limit)
            return [self._row(r) for r in rows]
        results: dict[str, dict[str, Any]] = {}
        for tok in tokens:
            for r in self.db.search_files(tok, limit):
                results.setdefault(r["path"], self._row(r))
        return list(results.values())[:limit]

    def content(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        tokens = _tokens(query)
        if not tokens:
            return []
        results: dict[str, dict[str, Any]] = {}
        # try FTS5 (AND of tokens), fall back to per-token name/path LIKE
        fts_query = " AND ".join([f'"{t}"' for t in tokens])
        for r in self.db.search_fts(fts_query, limit, name_only=False):
            if r["path"] not in results:
                results[r["path"]] = self._row(r)
        if not results:
            for tok in tokens:
                for r in self.db.search_files(tok, limit):
                    results.setdefault(r["path"], self._row(r))
        return list(results.values())[:limit]

    def combined(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Feature 4/5: filenames + indexed content."""
        by_name = self.files(query, limit)
        by_content = self.content(query, limit)
        merged: dict[str, dict[str, Any]] = {}
        for item in by_name + by_content:
            merged.setdefault(item["path"], item)
        return list(merged.values())[:limit]

    def semantic(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Vector-similarity search over indexed text (feature 14)."""
        if self.embedder.ready:
            qvec = self.embedder.embed_one(query)
            if qvec:
                qvec = normalize(qvec)
                rows = self.db.all_embeddings(self.cfg.embed_model)
                scored = []
                for r in rows:
                    vec = unpack(r["vec"], self.cfg.embed_dim)
                    vec = normalize(vec)
                    score = cosine(qvec, vec)
                    if score <= 0:  # allow near-zero but not negative
                        continue
                    f = self.db.file_by_id(int(r["file_id"]))
                    if f:
                        scored.append((score, f))
                scored.sort(key=lambda x: -x[0])
                return [self._row(f, score=s) for s, f in scored[:limit]]
        # fallback: BM25 keyword search (still useful)
        return self.content(query, limit)

    # ------------------------- hybrid search (feature 20) -------------------------
    def hybrid(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Fuse semantic + BM25 + filename (fuzzy) via Reciprocal Rank Fusion.

        Each sub-search contributes a ranked list; scores are summed as
        ``1/(k + rank)`` so a term that ranks well across several sources wins
        even if no single source loves it. Resilient: works with no embeddings.
        """
        lists: list[list[str]] = []
        best: dict[str, dict[str, Any]] = {}

        def add(items: list[dict[str, Any]], source: str = "") -> None:
            lst: list[str] = []
            for r in items:
                if not r.get("path"):
                    continue
                if source and source not in r:
                    r = dict(r)
                    r["source"] = source
                best.setdefault(r["path"], r)
                lst.append(r["path"])
            if lst:
                lists.append(lst)

        if self.embedder.ready:
            add(self.semantic(query, 25), "semantic")
        add(self.content(query, 25), "word")
        add(self.files_fuzzy(query, 25), "name")

        if not lists:
            return []
        scores = self._rrf(lists)
        out: list[dict[str, Any]] = []
        for path, s in scores:
            if path in best:
                r = dict(best[path])
                r["score"] = round(s, 4)
                out.append(r)
            if len(out) >= limit:
                break
        return out

    def files_fuzzy(self, term: str, limit: int = 50) -> list[dict[str, Any]]:
        tokens = _tokens(term)
        if not tokens:
            return self.files(term, limit)
        vocab = self._vocab()
        results: dict[str, dict[str, Any]] = {}
        for tok in tokens:
            hits = self.files(tok, limit)
            if not hits:
                for alt in self._close_tokens(tok, vocab):
                    hits.extend(self.files(alt, limit))
            for r in hits:
                results.setdefault(r["path"], r)
        return list(results.values())[:limit]

    # ---------------- hybrid internals ----------------
    @staticmethod
    def _rrf(lists: list[list[str]], k: float = 60) -> list[tuple[str, float]]:
        scores: dict[str, float] = {}
        for lst in lists:
            for i, path in enumerate(lst):
                scores[path] = scores.get(path, 0.0) + 1.0 / (k + i)
        return sorted(scores.items(), key=lambda x: -x[1])

    def _vocab(self) -> set[str]:
        if self._vcache is None:
            words: set[str] = set()
            for r in self.db.query("SELECT name FROM files"):
                for w in _re.findall(r"[A-Za-z0-9]+", r["name"].lower()):
                    if len(w) > 2:
                        words.add(w)
            self._vcache = words
        return self._vcache

    def _close_tokens(self, tok: str, vocab: set[str], max_hits: int = 3) -> list[str]:
        out: list[str] = []
        for w in vocab:
            if abs(len(w) - len(tok)) > 1:
                continue
            if self._lev(tok, w) <= 1:
                out.append(w)
                if len(out) >= max_hits:
                    break
        return out

    @staticmethod
    def _lev(a: str, b: str) -> int:
        if a == b:
            return 0
        if abs(len(a) - len(b)) > 1:
            return 99
        m, n = len(a), len(b)
        prev = list(range(n + 1))
        for i in range(1, m + 1):
            cur = [i] + [0] * n
            for j in range(1, n + 1):
                cost = 0 if a[i - 1] == b[j - 1] else 1
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            prev = cur
        return prev[n]

    # ------------------------- Test 5: latest resume -------------------------
    def latest_resume(self, limit: int = 5) -> list[dict[str, Any]]:
        """Identify the most recent likely resume."""
        import re
        resume_hits: dict[str, dict[str, Any]] = {}

        # 1) filename / content keyword matches
        for q in ("resume", "cv"):
            for r in self.db.search_fts(q, limit=50, name_only=False):
                self._consider(resume_hits, r, 2)
            for r in self.db.search_files(q, limit=50):
                self._consider(resume_hits, self._row(r), 2)

        # 2) metadata title/author matches
        for r in self.db.query(
            "SELECT * FROM files WHERE meta LIKE '%resume%' OR meta LIKE '%cv%'"
        ):
            self._consider(resume_hits, self._row(r), 2)

        # 3) heuristics on names for any document
        for r in self.db.query(
            "SELECT * FROM files WHERE is_dir=0 AND (ext IN ('.pdf','.docx','.doc','.txt','.md'))"
        ):
            row = self._row(r)
            name = row["name"].lower()
            score = 0
            if any(k in name for k in ("resume", "cv", "curriculum")):
                score += 4
            if any(k in name for k in ("latest", "final", "updated", "current")):
                score += 3
            if score:
                self._consider(resume_hits, row, score)

        results = list(resume_hits.values())
        # recency is decisive for "latest resume": score first, then mtime
        results.sort(key=lambda x: (x["score"], x.get("mtime", 0)), reverse=True)
        return results[:limit]

    def _consider(self, bucket: dict, row: dict[str, Any], weight: float) -> None:
        if not row or not row.get("path"):
            return
        path = row["path"]
        if path in bucket:
            bucket[path]["score"] += weight
            return
        recency = 1.0 / (1.0 + (time.time() - row.get("mtime", 0)) / (30 * 86400))
        bucket[path] = dict(row)
        bucket[path]["score"] = weight * recency

    # ------------------------- helpers -------------------------
    @staticmethod
    def _row(r: Any, score: float | None = None) -> dict[str, Any]:
        m = r.keys() if hasattr(r, "keys") else r._fields
        d = {}
        for k in ("id", "path", "name", "size", "mtime", "category", "mime",
                  "hash", "meta"):
            try:
                if k in (r.keys() if hasattr(r, "keys") else []):
                    d[k] = r[k]
            except Exception:
                pass
        if score is not None:
            d["score"] = round(score, 4)
        return d
