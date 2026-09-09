---
name: butler-courses
description: >-
  Manage and answer questions about Butler's tracked university courses, their
  materials, and assignments. Use for course lists, adding/dropping a course,
  "download materials for CS188", assignment sync, or checking for updates.
metadata:
  openclaw:
    requires:
      bins:
        - python
---

# Butler Courses

Course work is stateful and stored in Butler's DB and course directories. Always
read through the tools; never hardcode course codes, titles, or URLs.

## Workflow

1. **List / materials**: `courses` then `course_documents` (code) — or
   `course_check` to scan all tracked courses for new materials/assignments.
2. **Track a course**: `add_course` (code, name, url, platform, semester). The
   code is the canonical key you must use for later calls.
3. **Stop tracking**: `drop_course` (code).
4. **Find a doc**: use `search`/`find` with the course code or topic, or
   `course_documents`.

## Rules

- Always get the exact `code` from `courses` before passing it to
  `course_documents` or `drop_course`.
- `add_course`/`drop_course` are side-effectful (create/remove the course dir +
  monitor entry) and require operator approval.
- `course_check` may take time (network). Prefer it when the user asks "are there
  updates", not on every message.
- Do not fabricate reading lists or assignment deadlines — surface what the tools
  return, and attribute uncertainty.
