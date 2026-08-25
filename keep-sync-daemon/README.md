# keep-sync-daemon

Reads a Google Keep account via the unofficial [`gkeepapi`](https://github.com/kiwiz/gkeepapi)
library and writes every checklist note (title + items + checked state) to a
local JSON file at `<state_dir>/state.json` (default `~/.sp-keep-sync/state.json`).

This script is intentionally one-directional and read-only against Keep — it
never writes back to Google. It's meant to run on a schedule (cron / systemd
timer / Task Scheduler); the `sp-plugin` half of this project reads the JSON
file it produces.

## Setup

```bash
cd keep-sync-daemon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
# edit config.json: set "email" to your Google account
```

## Getting a master token (one-time, the annoying part)

`gkeepapi` is unofficial — it authenticates using a Google "master token"
instead of a normal OAuth app flow, since Keep has no public API for
consumer accounts. A master token is as powerful as your password, so treat
it accordingly (see storage options below).

1. Follow the [gpsoauth alternative-flow instructions](https://github.com/simon-weber/gpsoauth#alternative-flow)
   to sign in through Google's embedded browser flow and capture an
   **OAuth Token**. This is the fragile, manual step: it involves loading a
   Google embedded-setup sign-in page yourself (not something this repo
   automates, since it changes without notice and actively resists
   automation).
   - **If your account has 2-Step Verification enabled**, this flow commonly
     fails outright. The common workaround is to temporarily disable 2FA,
     mint the token, then re-enable 2FA — a real tradeoff you should decide
     on deliberately, not something this script does for you.
2. Run the helper in this repo to exchange that OAuth Token for a master
   token:
   ```bash
   python3 get_master_token.py --email you@gmail.com
   ```
3. Store the printed master token as **either**:
   - an environment variable `KEEP_MASTER_TOKEN` (e.g. in the cron
     environment), or
   - a file at `<state_dir>/master_token` (e.g. `~/.sp-keep-sync/master_token`),
     containing just the token. The daemon creates `state_dir` with `0700`
     permissions; set the token file to `0600` yourself:
     ```bash
     mkdir -p ~/.sp-keep-sync && chmod 700 ~/.sp-keep-sync
     echo -n 'PASTE_TOKEN_HERE' > ~/.sp-keep-sync/master_token
     chmod 600 ~/.sp-keep-sync/master_token
     ```

The daemon caches Google's own sync cursor in `<state_dir>/google_sync_cache.json`
after the first successful run, which reduces (but doesn't eliminate) the
chance of Google flagging subsequent logins as suspicious.

## Running

```bash
python3 keep_sync_daemon.py --config config.json
```

On success it prints how many checklists it wrote and updates
`state.json`. On failure (bad token, network error, Google login challenge)
it prints an error to stderr, exits non-zero, and **leaves the previous
`state.json` untouched** — so a transient failure never blanks out what the
Super Productivity plugin sees.

## Scheduling

Pick an interval that matches the plugin's sync interval (configured in the
SP plugin's UI) — there's no point polling Keep faster than the plugin
re-reads the file.

**cron** (every 5 minutes):
```cron
*/5 * * * * cd /path/to/keep-sync-daemon && KEEP_MASTER_TOKEN=... .venv/bin/python keep_sync_daemon.py >> ~/.sp-keep-sync/daemon.log 2>&1
```
(Omit `KEEP_MASTER_TOKEN=...` if you stored the token in `master_token` instead.)

**systemd timer** (Linux): create `~/.config/systemd/user/keep-sync.service`
and a matching `.timer` with `OnUnitActiveSec=5min`, then
`systemctl --user enable --now keep-sync.timer`.

**launchd** (macOS) / **Task Scheduler** (Windows): point either at
`.venv/bin/python keep_sync_daemon.py --config /path/to/config.json` on the
same interval.

## Known rough edges

- `gkeepapi` is unofficial and reverse-engineered; Google occasionally
  changes internals or throws CAPTCHA/`LoginException` challenges at logins
  it finds suspicious. If the daemon starts failing, re-run
  `get_master_token.py` to mint a fresh token.
- Only flat checklist items are synced (no nested/indented sub-items) — see
  the top-level project README for why.
- Trashed notes are always skipped; archived notes are skipped unless
  `"include_archived": true` is set in `config.json`.
