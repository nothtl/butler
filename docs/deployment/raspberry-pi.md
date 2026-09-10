# Deploying Pi Butler on a Raspberry Pi 5

This is the single recommended production guide. Pi Butler is a long-running
local service; the Pi is the always-on runtime.

## 1. Assumptions

- Raspberry Pi 5 (8 GB recommended), 64-bit Raspberry Pi OS Lite (Bookworm).
- An M.2 NVMe SSD mounted (recommended for the database and data dir).
- Python 3.11+ (3.13 recommended). Check with `python3 --version`.
- Outbound network only if you use Telegram, Google Calendar, an LLM or web.

## 2. System packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git sqlite3
# optional OCR
sudo apt install -y tesseract-ocr
```

## 3. Install

```bash
sudo mkdir -p /opt && sudo chown "$USER" /opt
git clone <your-fork> /opt/butler && cd /opt/butler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 4. Configure

```bash
mkdir -p ~/.config/butler
cp config.example.toml ~/.config/butler/config.toml
chmod 600 ~/.config/butler/config.toml
$EDITOR ~/.config/butler/config.toml
```

Set at minimum:

- `[user] timezone`
- `[storage] data_dir` (put this on the NVMe, e.g. `/mnt/nvme/ButlerStorage`)
- `[telegram] allowed_users` (your numeric id) — leave `open_when_empty=false`

Validate:

```bash
./butler.sh config-check
```

## 5. Secrets (prefer environment)

Do not commit tokens. Put them in the systemd unit or an `EnvironmentFile`:

```
BUTLER_TELEGRAM_TOKEN=...
BUTLER_LLM_KEY=...
# optional
BUTLER_HA_TOKEN=...
BUTLER_REMOTE_TOKEN=...
```

Keep that file `chmod 600` and outside the repository.

## 6. Initialize the database

The database is created and migrated automatically on first run:

```bash
./butler.sh health      # creates + migrates the DB, prints subsystem status
```

`./butler.sh backup` writes a snapshot under `[backup] dir`.

## 7. Telegram setup

1. Create a bot with @BotFather and copy the token.
2. Set `BUTLER_TELEGRAM_TOKEN` (env) and your numeric user id in
   `[telegram] allowed_users`.
3. `./butler.sh bot`.

With an empty allow-list the bot is **deny-by-default** (nobody can use it).
`open_when_empty=true` is a development-only escape hatch.

## 8. Google Calendar (optional)

1. Create OAuth desktop credentials in Google Cloud Console and save them to
   `~/.config/butler/client_secret.json`.
2. `./butler.sh calendar connect` (one-time browser consent).
3. `./butler.sh calendar sync`.

If Calendar is unavailable, Butler keeps local state and marks calendar data
degraded; it never invents availability.

## 9. Home Assistant (optional)

Set `[home_assistant] enabled=true`, `url=...` and `BUTLER_HA_TOKEN`. Presence
is reduced to a zone name; raw GPS is never stored.

## 10. AI provider (optional)

Set `BUTLER_LLM_KEY` and `[ai] base_url`/`model`. Without a key, Butler runs in
deterministic mode: all lookups, planning, memory, health and proactive
detection still work; only free-text phrasing is reduced.

## 11. systemd (recommended)

`/etc/systemd/system/butler-bot.service`:

```ini
[Unit]
Description=Pi Butler Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/butler
EnvironmentFile=/etc/butler.env
ExecStart=/opt/butler/.venv/bin/python -m butler.cli bot
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=20
# hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/butler.service` (scheduler + monitor):

```ini
[Unit]
Description=Pi Butler daemon
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/butler
EnvironmentFile=/etc/butler.env
ExecStart=/opt/butler/.venv/bin/python -m butler.cli start
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now butler.service butler-bot.service
```

On `SIGTERM`/`SIGINT` Butler stops the scheduler, releases leases and closes the
database cleanly.

## 12. Docker (alternative)

A systemd install is preferred on the Pi (simple, no image overhead). If you
containerise, mount the data dir and config as volumes, pass secrets as env,
and set `restart: unless-stopped`. Do not run both systemd and Docker copies
against the same database.

## 13. Logs

```bash
journalctl -u butler-bot -f
journalctl -u butler -f
```

Logs never contain tokens or keys (secrets are redacted).

## 14. Health & monitoring

```bash
./butler.sh health          # HEALTHY / DEGRADED / UNAVAILABLE / DISABLED per subsystem
./butler.sh security        # configuration security posture
./butler.sh audit 50        # recent audited actions
```

A disabled optional subsystem (e.g. no Telegram token) is reported `DISABLED`,
not a fatal error.

## 15. Backup

```bash
./butler.sh backup          # snapshot now
./butler.sh backups         # list
./butler.sh restore <file>  # restore (refuses files outside the backup dir)
```

Schedule `./butler.sh backup` daily (cron/systemd timer). Backups are
WAL-checkpointed copies of the SQLite database.

## 16. Update

```bash
cd /opt/butler
git pull
.venv/bin/pip install -r requirements.txt
./butler.sh backup
sudo systemctl restart butler.service butler-bot.service
./butler.sh health
```

Migrations run automatically and are idempotent. If a migration fails, the
previous backup can be restored.

## 17. Rollback

```bash
sudo systemctl stop butler.service butler-bot.service
cd /opt/butler && git checkout <previous-tag>
./butler.sh restore <backup-before-update>
sudo systemctl start butler.service butler-bot.service
```

## 18. Troubleshooting

| Symptom | Check |
|---------|-------|
| Bot ignores you | `allowed_users` contains your id; token set; service active |
| `health` says DEGRADED | the named subsystem detail (often deny-by-default or stale heartbeat) |
| Calendar not updating | `calendar connect`/`sync`; health `google_calendar` |
| Model replies are terse | `BUTLER_LLM_KEY` set; health `model` |
| Database locked | another process using the DB; only one copy should run |
| No proactive messages | quiet hours, daily budget, or suppressions (`proactive status`) |

## 19. Development mode

Run the full test suite offline with fake providers, frozen clocks and mocks:

```bash
.venv/bin/python tests/run_acceptance_m8.py
.venv/bin/python tests/run_acceptance_final.py
```

No live Telegram/Calendar/LLM/web/Home Assistant is required.
