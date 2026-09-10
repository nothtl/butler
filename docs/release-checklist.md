# Pi Butler — Release Checklist

Run through this before deploying or tagging a release. Every item should be
verifiable on the Pi itself.

## Configuration
- [ ] `config.example.toml` copied to `~/.config/butler/config.toml` (mode 600)
- [ ] `[user] timezone` set
- [ ] `[storage] data_dir` on the NVMe and absolute
- [ ] `./butler.sh config-check` passes with no errors

## Secrets
- [ ] Telegram token in env (`BUTLER_TELEGRAM_TOKEN`), not committed
- [ ] LLM key in env (`BUTLER_LLM_KEY`), not committed
- [ ] `./butler.sh security` shows no warnings
- [ ] `grep -R "sk-\|:AA" .` (excluding test fixtures) finds nothing

## Database
- [ ] `./butler.sh health` reports `database` HEALTHY and `migrations` v8
- [ ] `./butler.sh backup` succeeds
- [ ] A restore has been tested on a copy (never on the only copy)

## Migrations
- [ ] Upgrade path from the previous release tested on a copy of the real DB
- [ ] `PRAGMA user_version` equals the expected schema version

## Startup
- [ ] `./butler.sh start` starts scheduler + monitor
- [ ] Startup report has no unexpected errors
- [ ] A stale scheduler lease is reclaimed on start

## Telegram
- [ ] Unauthorized user is denied
- [ ] Authorized user can run `/help`, `/day`, `/briefing`
- [ ] Callback buttons (task actions, proactive) work and are validated

## Calendar
- [ ] `calendar connect` / `sync` work (if enabled)
- [ ] Calendar outage leaves local state untouched and health DEGRADED

## Model
- [ ] Free-text replies are generated (if a key is configured)
- [ ] With no key, deterministic features still work

## Web
- [ ] Web provider configured (or explicitly offline)
- [ ] A research query cites a source; an outage is reported honestly

## Health & monitoring
- [ ] `./butler.sh health` overall is HEALTHY or an understood DEGRADED
- [ ] `journalctl -u butler-bot` shows no secrets

## Proactive
- [ ] Candidates are generated and de-duplicated across repeated cycles
- [ ] Quiet hours, daily budget, snooze and dismiss behave as configured
- [ ] Accepting a recommendation does not execute it

## Security
- [ ] Telegram deny-by-default with an empty allow-list
- [ ] MCP `readonly` exposes no mutating tool
- [ ] Web content cannot become an instruction or a personal memory
- [ ] Inferred memory can never become a hard constraint
- [ ] Consequent-external actions require confirmation

## Test suite
- [ ] `tests/run_acceptance_final.py` prints PASS
- [ ] `unittest`, `compileall`, MCP parity all green
- [ ] No live-service dependency in the deterministic suites

## Deployment
- [ ] systemd units installed, enabled and restarted
- [ ] `Restart=on-failure` and `KillSignal=SIGTERM` set
- [ ] Data dir and config are the expected paths

## Rollback
- [ ] Previous release tag recorded
- [ ] Pre-update backup taken and its path recorded
- [ ] Rollback procedure (stop, checkout, restore, start) documented and tested
