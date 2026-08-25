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
2. Run the helper in this repo and paste that OAuth Token when prompted —
   it reads `email`/`state_dir` from `config.json`, so that's the only
   input needed:
   ```bash
   python3 get_master_token.py
   ```
   It exchanges the token and writes the result straight to
   `<state_dir>/master_token` (e.g. `~/.sp-keep-sync/master_token`) with
   `0600` permissions — it does not print the master token unless you pass
   `--print` (useful if you'd rather use the `KEEP_MASTER_TOKEN` env var
   instead of the file).

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

## Windows: system-tray app (no terminal, no Task Scheduler)

`keep_sync_tray.py` packages the whole setup + scheduling flow above into a
single Windows app with a system-tray icon — a GUI alternative to
`get_master_token.py` + `keep_sync_daemon.py` + Task Scheduler.

Run it from source:
```bash
pip install -r requirements-windows.txt
python keep_sync_tray.py
```

Or build a standalone `.exe` (no Python install required on the target
machine):
```bash
pip install -r requirements-windows.txt
pyinstaller --onefile --windowed --name KeepSyncTray keep_sync_tray.py
```
The `.exe` is written to `dist/KeepSyncTray.exe`. Keep it in a stable
folder — `config.json` and the "start at login" registration are written
next to wherever the exe lives, so moving it later means re-checking the
startup box (or re-pointing the shortcut) once.

**First launch** shows a small setup window instead of a terminal prompt:
1. Enter your Google account email.
2. Enter an **OAuth Token** from Google's embedded sign-in flow — this is
   still the same manual, fragile step described above (see
   [Getting a master token](#getting-a-master-token-one-time-the-annoying-part));
   the GUI only replaces the token-exchange script, not that step.
3. Set a sync interval and optionally check "Start automatically when
   Windows starts" (adds a per-user `HKCU\...\Run` registry entry — no
   admin rights needed).
4. Click **Save & Start Syncing**. The app exchanges the OAuth Token for a
   master token (same `gpsoauth.exchange_token()` call as
   `get_master_token.py`), saves it to `<state_dir>/master_token`, and
   drops into the tray.

Once running, the tray icon's right-click menu has:
- **Sync now** — runs an out-of-band sync immediately.
- **Open data folder** — opens `state_dir` (where `state.json` lives) in
  Explorer.
- **Reconfigure…** — re-opens the setup window (e.g. to rotate a stale
  token or change the sync interval).
- **Quit** — stops the background sync loop and exits.

The tray icon's tooltip shows the outcome of the last sync; a failed sync
also raises a Windows notification and, as with the CLI daemon, never
touches the previous `state.json`.

## Scheduling (CLI daemon)

This section is for `keep_sync_daemon.py` run via an external scheduler.
If you're using the Windows tray app above, skip this — it schedules
itself.

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
