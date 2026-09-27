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
checklist title). Optional: `sp_default_task_minutes` gives every task created
from a Keep item that time estimate in minutes (`0` = none); `sp_new_task_tag_ids`
is a list of SP tag ids (from `GET /tags`) to put on those same tasks;
`keep_sort_alphabetically` re-alphabetizes the Keep checklist after every pass
(Keep-side only — SP has no reorder API); `ai_merchant_rename_enabled` +
`anthropic_api_key` (or the `ANTHROPIC_API_KEY` env var) rewrites a freely-dictated
new item like "from walmart add toilet tablets" to "Walmart - Toilet tablets"
via Claude before creating its SP task — one API call per item, ever, tracked in
`<state_dir>/ai_renamed.json`.

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

Prints how many tasks/items it created and updated on each side, plus a note if
a newer release is available on GitHub (checked at most once a day).

## Tray apps (no scheduler needed)

`keep_sync_tray.py` (Windows, pystray/tkinter) and `keep_sync_tray_qt.py`
(Linux/KDE, PySide6) package the whole flow into a system-tray app that schedules
itself. Use the Qt one on Linux — pystray's Linux tray backend doesn't integrate
cleanly with Plasma.

```bash
pip install -r requirements-windows.txt   # or requirements-linux.txt
python keep_sync_tray.py                   # or keep_sync_tray_qt.py

# standalone binary:
pyinstaller --onefile --windowed --name KeepSyncTray --icon packaging\windows\keepsync.ico keep_sync_tray.py

# Windows installer (needs Inno Setup: https://jrsoftware.org/isinfo.php):
iscc /DAppVersion=2.2.15 /DVersionTag=v2.2.15 installer\KeepSyncTray.iss
```

On Windows, `KeepSyncTray-Setup-vX.Y.Z.exe` (built by the command above, or
downloaded from a [release](https://github.com/BigWebstas/SuperProd-Keep-Sync/releases))
is the easiest way to get set up: per-user install (no admin needed), an optional
"start at login" checkbox, a Start Menu entry. `config.json` and logs live in the
install folder either way, same as the plain `.exe`; uninstalling leaves them and
`~/.sp-keep-sync` in place, so reinstalling picks the sync config back up.

First launch shows a setup window: Google email, OAuth Token (an "Open sign-in
page" button next to the field launches the embedded sign-in URL in your default
browser — copying the `oauth_token` cookie is still manual, see above), SP
Access Token → **Connect & load lists** → pick the Keep list and SP
project, optionally one or more tags and a default time estimate to put on every
task created from a Keep item → set an interval → optionally "start at login"
(per-user, no admin), sort the Keep checklist alphabetically, and/or AI-rename
new items with a merchant prefix (needs an Anthropic API key) → **Save & Start
Syncing**. The tray menu has Show status, Sync now, Check for update, Open data
folder, Reconfigure, and Quit. `config.json` and the autostart entry are
written next to the binary, so keep it in a stable
folder.

The status window also shows a link when a newer release is available (checked at
most once a day); the tray fires a one-time notification the moment it notices.
**Check for update** in the menu bypasses that once-a-day cache for an on-demand
check, and always tells you the result — found or already up to date — instead of
only notifying once. On Windows the tray menu also grows an
**Update available: vX.Y.Z — Download** entry once one's found; clicking it pops
a progress window, then (once downloaded) a **Launch installer** window —
launching quits the tray first so the installer isn't fighting a locked exe. On
Linux, where there's no installer to launch yet, that menu entry just opens the
release page, same as the status window's link.

## Logs

Every entry point writes a log rotated daily at midnight, 7 days kept. On **Linux** a sibling
`*.fault.log` also captures native crashes (segfaults, `SIGABRT`). On Windows the
`faulthandler` exception hook is left off — there it fires on first-chance
exceptions that are actually caught and handled, which just reads as a fatal
crash.

| Entry point | Log file |
| --- | --- |
| `keep_sync_tray_qt.py` / `keep_sync_tray.py` | next to the binary, e.g. `keep_sync_tray_qt.log` (falls back to `<state_dir>/` then the temp dir if that folder is read-only — the startup line names the path it chose) |
| `keep_sync_daemon.py` | `<state_dir>/keep_sync_daemon.log` |

- `KEEP_SYNC_DEBUG=1` in the environment switches the log to DEBUG detail.
- `KEEP_SYNC_NO_GOOGLE_CACHE=1` turns off the `google_sync_cache.json` optimisation
  entirely (use if that file is implicated in a crash; costs more frequent Google
  login challenges).
- The first log line (`logging up: version=… pid=… log=…`) records the running
  version and the resolved log path. The tray "Show status" window shows the
  version too.
- On Linux, `kill -USR1 <pid>` dumps a stack trace of every thread to the
  `*.fault.log` without stopping the app. A `SIGTERM` / `SIGHUP` (logout,
  `systemctl --user stop`, `killall`) also dumps there before the process dies.
  A `SIGKILL` / OOM kill can't be caught — check `journalctl` / `coredumpctl`.
- The Qt tray also routes Qt's own warnings (`Qt: ...` lines) into the log, and
  crashes inside a tray menu action are logged with a traceback instead of
  taking the tray down silently.
- On Windows the tray runs each Keep pull (setup's "Connect & load lists" and
  every background sync) in a short-lived child process, so if the frozen build
  hard-crashes parsing Google's response it just fails that one pass — the tray
  stays up and retries. Off Windows the sync runs in-process as before.

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
