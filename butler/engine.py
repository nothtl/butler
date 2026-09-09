"""Deterministic file engine.

This is the SAFETY LAYER. The AI/organiser may decide what *should* happen,
but every filesystem operation must be validated and executed here. The engine:

  * refuses anything outside the configured roots,
  * never overwrites existing files (collision-safe),
  * never permanently deletes (moves to trash instead),
  * normalises/canonicalises paths and rejects traversal.

Features covered: 3 (list), 5 (directory), 6 (safe move/rename), 8 (dupes).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config import Config


class EngineError(Exception):
    """A guard violation or unsupported operation."""


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.allowed = [os.path.realpath(p) for p in cfg.roots + cfg.index_roots]
        if cfg.course_dir:
            self.allowed.append(os.path.realpath(cfg.course_dir))
        if cfg.incoming_dir:
            self.allowed.append(os.path.realpath(cfg.incoming_dir))
        if cfg.nas_enabled and cfg.nas_dir:
            self.allowed.append(os.path.realpath(cfg.nas_dir))
        # trash is interior; allow engine to write there
        self.allowed.append(os.path.realpath(cfg.state_dir))

    # ---------------- guards ----------------
    def _real(self, path: str | os.PathLike) -> str:
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))

    def within_roots(self, path: str | os.PathLike) -> bool:
        real = self._real(path)
        return any(real == r or real.startswith(r + os.sep) for r in self.allowed)

    def require_inside(self, path: str | os.PathLike) -> str:
        real = self._real(path)
        if not self.within_roots(real):
            raise EngineError(
                f"Refusing: '{path}' is outside Butler's managed roots."
            )
        return real

    def _exists(self, path: str) -> bool:
        return os.path.exists(path)

    # ---------------- feature 3: list ----------------
    def list_dir(self, path: str | os.PathLike, depth: int = 1,
                 hidden: bool = False) -> dict[str, Any]:
        real = self.require_inside(path)
        if not os.path.isdir(real):
            raise EngineError(f"'{real}' is not a directory.")
        out: list[dict[str, Any]] = []
        for entry in sorted(os.scandir(real), key=lambda e: e.name.lower()):
            if not hidden and entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            rec = {
                "name": entry.name,
                "path": entry.path,
                "dir": entry.is_dir(),
                "size": 0 if entry.is_dir() else stat.st_size,
                "mtime": int(stat.st_mtime),
            }
            out.append(rec)
        return {"path": real, "entries": out, "count": len(out)}

    # ---------------- feature 5: create directories ----------------
    def mkdir(self, path: str | os.PathLike, parents: bool = True) -> str:
        real = self.require_inside(path)
        if os.path.isdir(real):
            return real
        if not parents:
            parent = os.path.dirname(real)
            if not os.path.isdir(parent):
                raise EngineError(f"Parent directory does not exist: '{parent}'")
        os.makedirs(real, exist_ok=True)
        return real

    # ---------------- feature 6: safe move / rename ----------------
    def _unique_name(self, directory: str, filename: str) -> tuple[str, bool]:
        """Return a collision-free target and whether a suffix was added."""
        candidate = os.path.join(directory, filename)
        if not os.path.exists(candidate):
            return candidate, False
        stem, ext = os.path.splitext(filename)
        n = 1
        while True:
            cand = os.path.join(directory, f"{stem} (copy {n}){ext}")
            if not os.path.exists(cand):
                return cand, True
            n += 1

    def resolve_dest(self, dest_dir: str, filename: str) -> tuple[str, bool]:
        dest_dir = self.require_inside(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        return self._unique_name(dest_dir, filename)

    def move(self, src: str, dest_dir: str) -> str:
        src = self.require_inside(src)
        dest_dir = self.require_inside(dest_dir)
        if not os.path.exists(src):
            raise EngineError(f"Source does not exist: '{src}'")
        os.makedirs(dest_dir, exist_ok=True)
        filename = os.path.basename(src)
        target, renamed = self._unique_name(dest_dir, filename)
        target = self.require_inside(target)
        shutil.move(src, target)
        return target

    def rename(self, src: str, new_name: str) -> str:
        src = self.require_inside(src)
        if not os.path.exists(src):
            raise EngineError(f"Source does not exist: '{src}'")
        if not new_name or "/" in new_name:
            raise EngineError("New name must be a plain filename.")
        parent = os.path.dirname(src)
        target, _ = self._unique_name(parent, new_name)
        target = self.require_inside(target)
        os.rename(src, target)
        return target

    # ---------------- hashing ----------------
    def hash_file(self, path: str, chunk: int = 1 << 20) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()

    # ---------------- feature 8: duplicate detection ----------------
    def detect_duplicates(self, root: str,
                          by: str = "hash") -> list[dict[str, Any]]:
        """Find duplicate files under root. Returns groups of file records.

        Each record is ``(path, size, mtime)``. Within a group, records are
        ordered so that ``files[0]`` is the *primary* (kept) file: the oldest
        mtime wins, with a clean filename (no "copy"/"(n)" suffix) as a
        tiebreaker — so renames of an original are treated as duplicates, and
        we preserve the "important" copy.
        """
        root = self.require_inside(root)
        seen: dict[str, list[tuple[str, int, float]]] = {}
        if by == "name_size":
            sizes: dict[tuple[str, int], list[str]] = {}
            for path in self._walk_files(root):
                size = os.path.getsize(path)
                sizes.setdefault((os.path.basename(path), size), []).append(path)
            for group in sizes.values():
                if len(group) > 1:
                    key = "ns#%s#%d" % (os.path.basename(group[0]), os.path.getsize(group[0]))
                    seen.setdefault(key, []).extend(
                        (p, os.path.getsize(p), os.path.getmtime(p)) for p in group)
            return self._collate(seen)

        # hash first-pass by size, then sha256
        buckets: dict[int, list[str]] = {}
        for path in self._walk_files(root):
            buckets.setdefault(os.path.getsize(path), []).append(path)
        for size, group in buckets.items():
            if len(group) < 2:
                continue
            for path in group:
                key = self.hash_file(path)
                seen.setdefault(key, []).append((path, size, os.path.getmtime(path)))

        groups = [self._finalize(k, v) for k, v in seen.items() if len(v) > 1]
        groups.sort(key=lambda g: -g["size"])
        return groups

    @staticmethod
    def _clean_rank(name: str) -> int:
        """A lower rank means a cleaner, more 'original' filename."""
        low = name.lower()
        if any(mark in low for mark in (" copy", "- copy", "(copy", " (1)", " (2)",
                                        ".1.", ".2.", "duplicate", "-copy", "copy of")):
            return 1
        return 0

    def _finalize(self, key: str, records: list[tuple[str, int, float]]) -> dict[str, Any]:
        # primary first: cleanest name, then oldest mtime
        records = sorted(records, key=lambda r: (
            self._clean_rank(os.path.basename(r[0])), r[2]
        ))
        return {"key": key, "files": records, "size": records[0][1]}

    def _walk_files(self, root: str):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                yield os.path.join(dirpath, fname)

    def _collate(self, seen: dict) -> list[dict[str, Any]]:
        return [
            {"key": k, "files": v, "size": v[0][1]}
            for k, v in seen.items() if len(v) > 1
        ]


# ------------------ classification helpers (used by decider) ------------------
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Images", tuple(".jpg .jpeg .png .gif .bmp .webp .heic .raw .tiff".split())),
    ("Videos", tuple(".mp4 .mkv .mov .avi .webm .m4v .flv .wmv".split())),
    ("Audio", tuple(".mp3 .flac .wav .m4a .ogg .aac .opus".split())),
    ("Documents", tuple(".pdf .doc .docx .odt .rtf .txt .md".split())),
    ("Spreadsheets", tuple(".xls .xlsx .csv .ods .tsv".split())),
    ("Presentations", tuple(".ppt .pptx .odp .key".split())),
    ("Archives", tuple(".zip .tar .gz .bz2 .7z .rar .xz".split())),
    ("Code", tuple(
        ".py .js .ts .c .cpp .h .java .rs .go .rb .php .sh .html .css .json .yml .yaml .toml .sql".split()
    )),
    ("University", tuple(".lecture .slides".split())),
]

_COURSE_RE = re.compile(r"\b([A-Z]{2,4}\s?-?\s?\d{2,4})(?![A-Za-z0-9])", re.IGNORECASE)


def classify_by_ext(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    for cat, exts in CATEGORY_RULES:
        if ext in exts:
            return cat
    return "Other"


def extract_course_code(text: str) -> str | None:
    hit = _COURSE_RE.search(text)
    return hit.group(1).replace(" ", "").upper() if hit else None
