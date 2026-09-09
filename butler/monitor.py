"""Course monitor (feature 17) + automatic organisation (feature 16).

Watches Butler's managed roots (typically ``incoming_dir`` and ``course_dir``)
for new/modified files. When something appears it:

  * routes it into the correct course directory (by course code or category),
    reusing existing folders,
  * indexes it (hash + text extraction + embeddings) for search.

The monitor is safe: it only proposes/executes *.move* inside managed roots,
never deletes, and logs everything.
"""

from __future__ import annotations

import logging
import os
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .core import Container

log = logging.getLogger("butler.monitor")


def _route_dest(c: Container, path: str) -> str:
    name = os.path.basename(path)
    if not os.path.isfile(path) or name.startswith(".") or name.endswith((".tmp", ".part")):
        return ""
    try:
        return c.organizer.route_file(path)
    except Exception as exc:
        log.debug("route error: %s", exc)
        return ""


class ButlerEventHandler(FileSystemEventHandler):
    def __init__(self, container: Container, debounce: float = 2.0):
        self.container = container
        self.debounce = debounce
        self._pending: list[str] = []

    def on_created(self, event):  # noqa: N802
        if not event.is_directory:
            self._queue(event.src_path)

    def on_modified(self, event):  # noqa: N802
        if not event.is_directory:
            self._queue(event.src_path)

    def on_moved(self, event):  # noqa: N802
        if not event.is_directory:
            self._queue(event.dest_path)

    def _queue(self, path: str) -> None:
        self._pending.append(path)
        # simple debounce: swallow bursts by waiting on a brief timer
        time.sleep(self.debounce)
        batch = self._pending
        self._pending = []
        for p in batch:
            self._process(p)

    def _process(self, path: str) -> None:
        c = self.container
        if path.startswith(c.cfg.state_dir):
            return
        dest = _route_dest(c, path)
        if not dest:
            return
        try:
            if c.engine.within_roots(path) and os.path.realpath(path) != os.path.realpath(dest):
                target, _ = c.engine._unique_name(dest, os.path.basename(path))
                c.engine.move(path, dest)
                c.db.log_operation("monitor", "auto_place", path, target,
                                   "auto-routed", "applied")
                log.info("auto-placed %s -> %s", path, target)
        except Exception as exc:
            log.warning("auto-place failed for %s: %s", path, exc)
        # index regardless (so new files are searchable)
        try:
            c.indexer.index_root(os.path.dirname(path))
        except Exception as exc:
            log.warning("index failed for %s: %s", path, exc)


class CourseMonitor:
    def __init__(self, container: Container):
        self.container = container
        self._observer: Observer | None = None

    def watch_paths(self) -> list[str]:
        paths = []
        if self.container.cfg.incoming_dir and os.path.isdir(self.container.cfg.incoming_dir):
            paths.append(self.container.cfg.incoming_dir)
        if self.container.cfg.course_dir and os.path.isdir(self.container.cfg.course_dir):
            paths.append(self.container.cfg.course_dir)
        for p in self.container.cfg.roots:
            if os.path.isdir(p):
                paths.append(p)
        return paths

    def start(self) -> None:
        paths = self.watch_paths()
        if not paths:
            log.info("no watch paths configured")
            return
        self._observer = Observer(timeout=1)
        handler = ButlerEventHandler(self.container)
        for p in paths:
            self._observer.schedule(handler, p, recursive=True)
            log.info("watching %s", p)
        self._observer.start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
