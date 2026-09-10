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

import json
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

    def _rag_prompt(self, question: str, ctx: list[tuple[str, str, str]], mode: str,
                    live_context: str = "") -> tuple[str, str]:
        ctx_block = "\n\n".join(
            f"### {name}\n{path}\n{snippet}" for name, path, snippet in ctx
        )
        if mode == "teach":
            system = (
                "You are Butler, a patient tutor. Teach the user about their own "
                "material using ONLY the file context below, and link ideas to the file "
                "paths. Structure: 1) What it is, 2) key ideas, 3) a concrete "
                "example from the material, 4) a 3-question self-quiz with answers "
                "at the end. If the context is insufficient, say what is missing "
                "instead of guessing. Keep it focused and friendly."
            )
        else:
            system = (
                "You are Butler, the user's assistant. Answer using the LIVE STATE "
                "and/or the file context below. Use live state for anything about the "
                "user's own setup (calendar, tasks, courses, presence). Cite relevant "
                "file path(s) when you use file context. Be concise. If neither the "
                "live state nor the file context covers the answer, say so clearly "
                "and do not guess."
            )
        user = (
            f"Topic/Question: {question}\n\n"
            f"LIVE STATE:\n{live_context or '(none)'}\n\n"
            f"FOUND FILE CONTEXT:\n{ctx_block or '(none)'}"
        )
        return system, user

    # ------------------------- answering -------------------------
    def answer(self, question: str, live_context: str = "") -> str:
        ranked = self.retrieve(question)
        ctx = self._context(question, ranked)
        if self._llm_ready():
            body = self._llm(self._rag_prompt(question, ctx, "answer", live_context))
            if body:
                return self._cite(body, ranked) if ctx else body
        if ctx:
            return self._extractive(question, ctx)
        return live_context

    @staticmethod
    def _cite(answer: str, ranked) -> str:
        """Append the sources the answer was grounded in, so it's verifiable."""
        srcs, seen = [], set()
        for _, path, _ in ranked:
            if len(srcs) >= 3:
                break
            label = os.path.basename(path) if path else ""
            if label and label not in seen:
                seen.add(label)
                srcs.append(label)
        if not srcs:
            return answer
        return answer + "\n\n📎 " + " · ".join(f"`{s}`" for s in srcs)

    # --------------------- compose replies from live data ---------------------
    def respond_kind(self, kind: str, data: Any) -> str | None:
        """Write a natural reply from freshly-retrieved structured data.

        Returns ``None`` when there's no LLM so the caller can fall back to its
        deterministic template. The reply is grounded strictly in ``data`` and
        never prettifies or invents anything not present in it.
        """
        if not self._llm_ready():
            return None
        payload = _compact(data)
        system = (
            "You are Butler, a warm, concise assistant living in the user's Telegram. "
            "The system just pulled FRESH live data for the user's request. "
            "Write a short, natural, human reply (1-4 sentences) that reflects that data. "
            "Use ONLY the data given. If it's empty, say so plainly in your own words. "
            "Never invent courses, files, hours, numbers, names or dates. "
            "Never mention that you were given data or that you are an AI."
        )
        user = f"The user's live data ({kind}):\n{payload}\n\nWrite your reply:"
        return self._llm((system, user))

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

    # ------------------------- free-form chat -------------------------
    def converse(self, message: str, live_context: str = "") -> str | None:
        """A short, natural conversational reply grounded in LIVE state.

        Only used when an LLM is configured; otherwise returns ``None`` so the
        caller can fall back to the live state itself. Never returns a canned
        sentence (see AGENTS.md).
        """
        if not self._llm_ready():
            return None
        system = (
            "You are Butler, a warm, concise assistant living in the user's "
            "Telegram. Below is LIVE STATE about the user's setup, gathered right "
            "now. Use it to answer questions about your own access and the user's "
            "data truthfully and specifically (for example, whether you have their "
            "calendar and what is on it). Reply in 1-3 short, natural sentences. "
            "Never invent facts beyond the live state; if it does not cover "
            "something, say so plainly. Never mention that you were given state or "
            "that you are an AI."
        )
        user = f"LIVE STATE:\n{live_context or '(none)'}\n\nUser: {message}"
        return self._llm((system, user))

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
        # An OpenAI-compatible base URL must be configured explicitly. Requiring
        # it means we never silently assume a vendor endpoint.
        return bool(self.cfg.llm_api_key and self.cfg.llm_base_url)

    def _llm(self, prompt: tuple[str, str]) -> str | None:
        import requests
        system, user = prompt
        # ``base_url`` is required to call the LLM (openai-compatible or any
        # provider). Falling back to a hard-coded OpenAI endpoint would silently
        # assume a vendor, so the caller must configure it explicitly.
        base = (self.cfg.llm_base_url or "").rstrip("/")
        if not base:
            return None
        url = base + "/chat/completions"
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


def _compact(data: Any, limit: int = 2400) -> str:
    try:
        s = json.dumps(data, default=str, ensure_ascii=False, indent=1)
    except Exception:
        s = str(data)
    return s if len(s) <= limit else s[:limit] + "\n… (truncated)"
