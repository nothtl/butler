"""Phase 3: Intelligent File Management / NAS.

Wraps the existing deterministic file engine + organizer so the NAS storage is
just another searchable, classified tree with a drop-box for incoming files.

Safety / non-destructive rules:
  * All moves go through ``engine.move`` (collision-safe) and are logged to the
    operations table with a plan id, so ``organizer undo`` / ``trash`` recover
    them.
  * Nothing is deleted by the "ingest" path — files are moved or left in place.
  * The Samba ``smbd`` enablement is config-only (no passwordless sudo); we
    render a template and instruct the user to apply it. ``nas_enabled`` gates
    all real operations.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("butler.nas")

SAMBA_TEMPLATE = """# Butler-managed Samba config for the NAS share.
# Review then apply with:  sudo smbpasswd -a $USER && sudo systemctl enable --now smbd
# (Butler never runs sudo itself.)

[storage]
   path = {nas_dir}
   browseable = yes
   read only = no
   valid users = {user}
   create mask = 0664
   directory mask = 0775

[inbox]
   path = {nas_inbox_dir}
   browseable = yes
   read only = no
"""


class FileManager:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.engine = container.engine
        self.organizer = getattr(container, "organizer", None)

    # ------------------------------------------------------------- gate
    def enabled(self) -> bool:
        return bool(self.cfg.nas_enabled) and bool(self.cfg.nas_dir)

    def storage_dir(self) -> str:
        return self.cfg.nas_dir

    def inbox_dir(self) -> str:
        return self.cfg.nas_inbox_dir or os.path.join(self.cfg.nas_dir, "Inbox")

    def ensure(self) -> dict[str, Any]:
        """Ensure the storage + inbox dirs exist (idempotent, guarded)."""
        if not self.enabled():
            return {"ok": False, "message": "NAS not enabled (nas_enabled/nas_dir)"}
        self.cfg.ensure_dirs()
        return {"ok": True, "nas_dir": self.cfg.nas_dir,
                "inbox": self.inbox_dir()}

    def _route_dest(self, path: str) -> dict[str, Any]:
        """Two-stage classification: deterministic first, LLM only if ambiguous.

        Returns an enriched routing record; ``confirm=True`` items must not be
        applied without explicit user confirmation.
        """
        chat = getattr(self.container, "chat", None)
        record = self.organizer.route_semantic(path, chat=chat)
        record["dest"] = self._bind_into_nas(record.get("dest") or self.cfg.nas_dir)
        return record

    # ------------------------------------------------------------- organize
    def organize_file(self, path: str) -> dict[str, Any]:
        """Route a single file into the NAS classification tree and index it."""
        if not self.enabled() or self.organizer is None:
            return {"ok": False, "error": "NAS disabled"}
        route = self._route_dest(path)
        if route.get("confirm"):
            return {"ok": False, "confirm": True, "checkpoint": route}
        target = self._move(path, route["dest"])
        if not target:
            return {"ok": False, "error": "unable to move file"}
        self._index(target)
        return {"ok": True, "source": path, "dest": target,
                "method": route.get("method")}

    def ingest_inbox(self) -> dict[str, Any]:
        """Move every file from the NAS Inbox into its classified home.

        Low-confidence / ambiguous files are *not* moved — they are returned in
        ``pending`` for confirmation, so nothing is bulk-placed on a guess.
        """
        if not self.enabled():
            return {"ok": False, "error": "NAS disabled"}
        inbox = self.inbox_dir()
        if not os.path.isdir(inbox):
            return {"ok": False, "error": f"inbox not found: {inbox}"}
        moved: list[tuple[str, str]] = []
        pending: list[Any] = []
        for e in self.engine.list_dir(inbox)["entries"]:
            if e["dir"]:
                continue
            route = self._route_dest(e["path"])
            if route.get("confirm"):
                pending.append({"path": e["path"],
                                "route": {"dest": route["dest"],
                                          "method": route.get("method"),
                                          "confidence": route.get("confidence")}})
                continue
            target = self._move(e["path"], route["dest"])
            if target:
                moved.append((e["path"], target))
                self._index(target)
        return {"ok": True, "moved": moved, "count": len(moved),
                "pending": pending, "pending_count": len(pending)}

    def _bind_into_nas(self, dest_dir: str) -> str:
        """Re-root a routed destination *inside* the NAS root, preserving the
        full relative directory hierarchy (never flatten it, never escape NAS).

        The organizer routes course/category trees under ``course_dir`` (or
        ``data_dir``). We anchor the relative path at the *parent* of that local
        routing base so the top-level folder (e.g. ``University``) is preserved
        as a namespace under the NAS root.

        Example (course root ``/home/user/University``, NAS root ``/mnt/storage``):
            /home/user/University/CS168/Projects  ->  /mnt/storage/University/CS168/Projects
        """
        nas = os.path.realpath(self.cfg.nas_dir)
        real = os.path.realpath(dest_dir)
        # Already inside the NAS — leave it untouched.
        if real == nas or real.startswith(nas + os.sep):
            return real
        base = os.path.realpath(self.cfg.course_dir or self.cfg.data_dir)
        try:
            inside_base = os.path.commonpath([real, base]) == base
        except ValueError:
            inside_base = False
        if inside_base and base != os.path.dirname(base):
            anchor = os.path.dirname(base)
            rel = os.path.relpath(real, anchor)
            # Never allow traversal back above the anchor.
            if rel == ".." or rel.startswith(".." + os.sep):
                rel = os.path.basename(real)
            target = os.path.join(nas, rel)
        else:
            target = os.path.join(nas, os.path.basename(real))
        target = os.path.realpath(target)
        # Final guard: the result must be strictly inside the NAS root.
        if not (target == nas or target.startswith(nas + os.sep)):
            target = os.path.join(nas, os.path.basename(real))
        return target

    def _move(self, path: str, dest_dir: str) -> str:
        try:
            os.makedirs(dest_dir, exist_ok=True)
            self.db.log_operation("nas", "move", path, dest_dir, "ingest", "applied")
            target = self.engine.move(path, dest_dir)
            return target
        except Exception as exc:  # noqa: BLE001
            log.warning("nas move failed %s -> %s: %s", path, dest_dir, exc)
            return ""

    def _index(self, path: str) -> None:
        idx = getattr(self.container, "indexer", None)
        if idx is None:
            return
        try:
            idx._index_file(path, {}, with_embeddings=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("nas index failed %s: %s", path, exc)

    # ------------------------------------------------------------- listing
    def list_root(self) -> dict[str, Any]:
        if not self.enabled() or not os.path.isdir(self.cfg.nas_dir):
            return {"ok": False, "error": f"root missing: {self.cfg.nas_dir}"}
        entries = self.engine.list_dir(self.cfg.nas_dir)["entries"]
        return {"ok": True, "root": self.cfg.nas_dir, "entries": entries}

    def categories(self) -> list[str]:
        labels = ["University", "Documents", "Images", "Other"]
        if not self.enabled() or not os.path.isdir(self.cfg.nas_dir):
            return labels
        return sorted({classify_by_ext(e["name"]) if not e["dir"]
                       else os.path.basename(e["path"])
                       for e in self.engine.list_dir(self.cfg.nas_dir)["entries"]}
                      | set(labels))

    # ------------------------------------------------------------- samba
    def samba_config(self) -> str:
        return SAMBA_TEMPLATE.format(nas_dir=self.cfg.nas_dir or "/mnt/storage",
                                     nas_inbox_dir=self.inbox_dir(),
                                     user=self.cfg.samba_share or "pi")

    def write_samba_config(self, dest: str = "") -> dict[str, Any]:
        """Render the Samba config (optionally to a file). Never runs sudo."""
        text = self.samba_config()
        if not dest:
            return {"ok": True, "text": text,
                    "note": "apply via: sudo smbpasswd -a $USER && sudo systemctl enable --now smbd"}
        try:
            with open(dest, "w") as fh:
                fh.write(text)
            return {"ok": True, "wrote": dest}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


# re-used by NAS: classification is the same function the organizer uses
from .organizer import classify_by_ext, extract_course_code  # noqa: E402,F401
