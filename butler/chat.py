"""Chat: retrieval-augmented Q&A + tutoring over the user's files.

Two modes, driven by config:
  * `[ai] api_key` set  -> LLM (OpenAI-compatible) answers grounded in retrieved
    chunks, citing source paths.
  * no key             -> offline "grounded extraction": answer with the most
    relevant sentences / a study guide pulled straight from the indexed text.

Grounding is always real retrieval: we never fabricate. If nothing is found,
we say so instead of guessing.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from typing import Any

from .config import Config
from .db import DB
from .search import Search, _tokens


class Chat:
    def __init__(self, cfg: Config, db: DB, search: Search):
        self.cfg = cfg
        self.db = db
        self.search = search

    # ------------------------- retrieval -------------------------
    def retrieve(self, question: str, n_files: int = 6) -> list[dict[str, Any]]:
        return self.search.hybrid(question, limit=n_files)

    def _query(self, text: str) -> str:
        toks = _tokens(text)
        return " OR ".join(f'"{t}"' for t in toks) if toks else ""

    def _context(self, question: str, ranked: list[dict[str, Any]], max_chunks: int = 6) -> list[tuple[str, str, str]]:
        """Return (name, path, body) snippets grounded in the question."""
        mq = self._query(question)
        by_file: dict[int, Any] = {}
        if mq:
            for c in self.db.chunk_results(mq, limit=24):
                fid = int(c["file_id"])
                if fid not in by_file:  # bm25 order == best chunk first
                    by_file[fid] = c

        out: list[tuple[str, str, str]] = []
        seen_paths: set[str] = set()

        def take(f: Any, c: Any) -> None:
            if f and len(out) < max_chunks and f["path"] not in seen_paths:
                seen_paths.add(f["path"])
                out.append((f["name"], f["path"], (c["body"] or "")[:420]))

        for r in ranked:  # highest-ranked files first
            f = self.db.get_file(r["path"])
            if f and int(f["id"]) in by_file:
                take(f, by_file[int(f["id"])])

        for fid, c in by_file.items():  # fill gaps from BM25-only hits
            if len(out) >= max_chunks:
                break
            take(self.db.file_by_id(fid), c)
        return out[:max_chunks]

    def _rag_prompt(self, question: str, ctx: list[tuple[str, str, str]], mode: str) -> tuple[str, str]:
        ctx_block = "\n\n".join(
            f"### {name}\n{path}\n{snippet}" for name, path, snippet in ctx
        )
        if mode == "teach":
            system = (
                "You are Butler, a patient tutor. Teach the user about their own "
                "material using ONLY the context below, and link ideas to the file "
                "paths. Structure: 1) What it is, 2) key ideas, 3) a concrete "
                "example from the material, 4) a 3-question self-quiz with answers "
                "at the end. If the context is insufficient, say what is missing "
                "instead of guessing. Keep it focused and friendly."
            )
        else:
            system = (
                "You are Butler, a search assistant grounded in the user's files. "
                "Answer ONLY using the context below and cite the relevant file "
                "path(s). Be concise. If the context does not contain the answer, "
                "say so clearly and do not guess."
            )
        user = (
            f"Topic/Question: {question}\n\nFOUND CONTEXT:\n{ctx_block}"
        )
        return system, user

    # ------------------------- answering -------------------------
    def answer(self, question: str) -> str:
        ranked = self.retrieve(question)
        ctx = self._context(question, ranked)
        if not ctx:
            return (
                "I couldn't find anything about that in your files yet.\n"
                "Try adding it to a managed folder and running `butler index`, "
                "or rephrase the question."
            )
        if self._llm_ready():
            body = self._llm(self._rag_prompt(question, ctx, "answer"))
            if body:
                return body
        return self._extractive(question, ctx)

    def teach(self, topic: str) -> str:
        ranked = self.retrieve(topic)
        ctx = self._context(topic, ranked)
        if not ctx:
            return (
                f"I don't have material to teach about `{topic}` yet.\n"
                "Index further or give me a file or folder to read first."
            )
        if self._llm_ready():
            body = self._llm(self._rag_prompt(topic, ctx, "teach"))
            if body:
                return body
        return self._study_guide(topic, ctx)

    # ------------------------- offline fallbacks -------------------------
    def _extractive(self, question: str, ctx: list[tuple[str, str, str]]) -> str:
        toks = set(_tokens(question))
        best, best_score, best_src = None, -1, ""
        for name, path, text in ctx:
            for sent in re.split(r"(?<=[.!?])\s+", text):
                if not sent.strip():
                    continue
                s = set(_tokens(sent))
                score = len(toks & s)
                if score > best_score:
                    best_score, best, best_src = score, sent.strip(), f"{name} ({path})"
        if best_score <= 0:
            return self._list_sources(question, ctx)
        return f"{best}\n\n— from {best_src}"

    def _list_sources(self, question: str, ctx: list[tuple[str, str, str]]) -> str:
        lines = [f"Here's what I found regarding `{question}`:", ""]
        for name, path, snippet in ctx:
            lines.append(f"- **{name}** `{path}`")
            lines.append(f"  {snippet[:160]}")
        return "\n".join(lines)

    def _study_guide(self, topic: str, ctx: list[tuple[str, str, str]]) -> str:
        toks = set(_tokens(topic))
        counts: Counter[str] = Counter()
        for _n, _p, text in ctx:
            for w in _tokens(text):
                if w not in toks:
                    counts[w] += 1
        lines = [f"# {topic} — study notes", ""]
        top = counts.most_common(8)
        if top:
            lines.append("## Glossary / key terms")
            lines.append("\n".join(f"- `{w}` (appears {c}x)" for w, c in top))
            lines.append("")
        lines.append("## Where to read it")
        for name, path, snippet in ctx:
            lines.append(f"- **{name}** `{path}`")
            lines.append(f"  {snippet[:170]}")
        lines.append("")
        if top:
            lines.append("## Practice")
            lines.append("\n".join(f"- Explain `{w}` in your own words." for w, _c in top))
            lines.append("")
            lines.append("Ask me `explain <term>` or `quiz me on " + topic + "` for more.")
        return "\n".join(lines)

    # ------------------------- llm -------------------------
    def _llm_ready(self) -> bool:
        return bool(self.cfg.llm_api_key)

    def _llm(self, prompt: tuple[str, str]) -> str | None:
        import requests
        system, user = prompt
        url = (self.cfg.llm_base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.cfg.llm_api_key}"},
                json={
                    "model": self.cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.3,
                },
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return text.strip()
        except Exception:
            return None
