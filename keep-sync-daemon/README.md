# keep-sync-daemon

Talks to Google Keep via the unofficial [`gkeepapi`](https://github.com/kiwiz/gkeepapi)
library and to a running Super Productivity desktop app via its
[Local REST API](https://github.com/super-productivity/super-productivity/blob/master/docs/wiki/3.01-API.md).
Each run reconciles one Keep checklist against one SP project, then SP syncs the
task changes onward. See the top-level README for exactly what does and doesn't
sync.

On any failure (bad token, network error, Google login challenge, SP not
running) it exits non-zero and **leaves both sides untouched** — a transient
failure never makes a partial change.

## 1. Super Productivity

**Settings → Misc → Enable local REST API**, then copy the **Access Token**. SP
must be running for a pass to touch the SP side.

## 2. Install

```bash
cd keep-sync-daemon
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json`: `email`, `sp_access_token`, `sp_project_id` (from
`GET http://127.0.0.1:3876/projects`), and `keep_note_title` (the exact Keep
checklist title).

## 3. Master token (one-time, the fiddly part)

`gkeepapi` authenticates with a Google "master token" — as powerful as your
password, so store it carefully.

1. Get an **OAuth Token** from Google's embedded sign-in:
   - Open <https://accounts.google.com/EmbeddedSetup>, sign in, click "I agree".
     The page may hang on a loading screen — that's expected.
   - Dev tools (F12) → Application → Cookies → `accounts.google.com` → copy the
     `oauth_token` value.
   - **With 2-Step Verification on, this flow usually fails.** The common
     workaround is to disable 2FA, mint the token, then re-enable it.
2. `python3 get_master_token.py` and paste the OAuth Token. It reads
   `email`/`state_dir` from `config.json` and writes `<state_dir>/master_token`
   (mode `0600`). Pass `--print` to use the `KEEP_MASTER_TOKEN` env var instead.

After the first successful run the daemon caches Google's sync cursor in
`<state_dir>/google_sync_cache.json`, which reduces (not eliminates) suspicious-login
challenges.

## 4. Run

```bash
python3 keep_sync_daemon.py --config config.json
```

Prints how many tasks/items it created and updated on each side.

## Tray apps (no scheduler needed)

`keep_sync_tray.py` (Windows, pystray/tkinter) and `keep_sync_tray_qt.py`
(Linux/KDE, PySide6) package the whole flow into a system-tray app that schedules
itself. Use the Qt one on Linux — pystray's Linux tray backend doesn't integrate
cleanly with Plasma.

```bash
pip install -r requirements-windows.txt   # or requirements-linux.txt
python keep_sync_tray.py                   # or keep_sync_tray_qt.py

# standalone binary:
pyinstaller --onefile --windowed --name KeepSyncTray keep_sync_tray.py
```

First launch shows a setup window: Google email, OAuth Token (same manual step as
above), SP Access Token → **Connect & load lists** → pick the Keep list and SP
project → set an interval → optionally "start at login" (per-user, no admin) →
**Save & Start Syncing**. The tray menu has Show status, Sync now, Open data
folder, Reconfigure, and Quit. `config.json` and the autostart entry are written
next to the binary, so keep it in a stable folder.

## Scheduling the CLI daemon

Pick an interval matching how fast you want edits to converge; every 5 minutes is
a reasonable default.

**cron:**
```cron
*/5 * * * * cd /path/to/keep-sync-daemon && .venv/bin/python keep_sync_daemon.py >> ~/.sp-keep-sync/daemon.log 2>&1
```

**systemd timer:** a `keep-sync.service` + `.timer` with `OnUnitActiveSec=5min`,
then `systemctl --user enable --now keep-sync.timer`.

**launchd / Task Scheduler:** point at
`.venv/bin/python keep_sync_daemon.py --config /path/to/config.json`.

## Rough edges

- If `gkeepapi` starts failing, re-run `get_master_token.py` for a fresh token.
- Only flat checklist items sync (no nested sub-items). Blank lines are ignored
  (SP rejects empty titles). Trashed notes are always skipped; archived notes
  need `"include_archived": true`.
- Delete and recreate a Keep list with the same title and the mapping goes
  stale — clear `<state_dir>/item_map.json` to re-pair.
