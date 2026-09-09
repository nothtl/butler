# Butler

A self-hosted, on-device file assistant for the Raspberry Pi (Raspberry Pi **5**,
M.2 NVMe SSD, Python **3.13.5**). It watches your folders, understands natural-language
commands, and moves/organizes/search-catalogs your files deterministically.

Everything runs locally: `python-telegram-bot`, `PyMuPDF`, `python-docx`/`python-pptx`,
`fastembed` (local embeddings), `watchdog`, and SQLite `FTS5`.

## Principle

> The AI decides what makes sense. The deterministic file engine decides what is
> allowed. The filesystem performs the actual operation.

- **Everything stays inside your managed roots.** Actions outside a root are refused.
- **Bulk mutations require confirmation.** Nothing irreversible is done without a yes.
- **Nothing is ever permanently deleted** by the pipeline — duplicates and old files
  move to the **trash** (`.butler/trash`) and can be restored.
- An in-app **operation log** records every mutation for auditability.

## Install / Run

```bash
cd /home/tingli/butler
VENV=.venv
python3 -m venv $VENV && $VENV/bin/pip install -r requirements.txt
# on first semantic search the embedding model downloads automatically

# CLI (also available via `butler` on PATH after `bash butler.sh`)
./butler.sh status
./butler.sh index --json
./butler.sh organize --yes
./butler.sh bot             # start the Telegram bot
./butler.sh monitor         # watch + auto-place course files
./butler.sh remote          # tiny HTTP control server
```

Config lives at `config/butler.toml` (installed copy: `~/.config/butler/config.toml`).
Set `BUTLER_TELEGRAM_TOKEN` or put it in the config to use the bot.

## Layout

```
butler/
  cli.py       command-line entrypoint (python -m butler.cli)
  config.py    config load + env overrides
  db.py        SQLite schema (files, chunks/content_fts, embeddings, ops, trash)
  engine.py    deterministic filesystem engine (guards, dedupe, move, trash)
  extract.py   text extraction (pdf/docx/pptx/odt/code)
  embed.py     local semantic embeddings
  indexer.py   walk + extract + index + embed
  search.py    keyword (FTS), name, and semantic search
  organizer.py plan/apply (organize, workspace, routes)
  decider.py   natural-language intent -> deterministic plan
  monitor.py   watchdog course monitor (auto-place + index)
  telebot.py   Telegram bot with inline confirm buttons
  remote.py    HTTP control server (optional)
  backup.py    git-backed snapshot service
  logging.py   operation log
  trash.py     trash & recovery
  core.py      Container: wires config/db/engine/search/decider
config/butler.toml
tests/run_acceptance.py
```

## Acceptance

`python tests/run_acceptance.py` — **24/24 checks passing**:

- Workspace create (propose → confirm → apply) ✓
- Organize propose (no mutation until confirmed) ✓
- Search CS168 spec via indexed content (FTS) ✓
- Duplicate delete → trash (original preserved) ✓
- "Where is my latest resume?" → newest resume ranked first ✓
- Course monitor auto-place + index + searchable ✓
- Semantic search ✓
- Trash recovery (restore to original path) ✓
- Operation logging ✓

## Commands

`status  storage  index  list  find  search  resume  organize  dupes  dupes-trash
trash  trash-list  recover  mkdir  move  rename  apply  backup  bot  monitor  remote`

Append `--json` for machine-readable output, `--yes` to auto-confirm plans.
