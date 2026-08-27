# SuperProd Keep Sync

Two-way sync between a Google Keep checklist and a project in
[Super Productivity](https://super-productivity.com) — pick the Keep list and
the target project once (from a tray app, or in `config.json`). Checking off
or renaming an item on either side updates the other. New items sync both
ways (a new Keep item makes a task; a new top-level task makes a Keep item);
deletes never propagate.

## How it works

Google Keep has no public API for personal accounts, so a small Python
process holds the Google "master token" and talks to Keep through the
unofficial `gkeepapi` library. It reconciles the chosen checklist against
the chosen Super Productivity project through **SP's Local REST API**
(`http://127.0.0.1:3876`). SP itself turns the resulting task
create/update calls into sync operations and pushes them to whatever
backend you've set up in SP — Super Sync, Dropbox, WebDAV, or local files.

```
┌──────────────────────────────┐          ┌────────────────────────────┐
│ keep-sync-daemon / tray app   │          │ Super Productivity (desktop)│
│ (Python, gkeepapi)            │          │                             │
│                                │── REST ─▶│ Local REST API :3876         │
│ timer → pull Keep checklist   │  add /   │  POST /tasks  PATCH /tasks   │
│      → reconcile vs. SP        │  update  │  GET /tasks   GET /projects  │
│      → push item edits to Keep │◀── read ─│                             │
│ item_map.json remembers the    │  tasks   │  ↓ SP's own sync            │
│ Keep-item ↔ SP-task pairing    │          │  Super Sync / Dropbox / …   │
└──────────────────────────────┘          └────────────────────────────┘
```

There is no longer a Super Productivity plugin or a `state.json` file
bridge — earlier versions had both. The Keep-item ↔ SP-task mapping now
lives in `~/.sp-keep-sync/item_map.json`, owned by the Python side.

**Trade-off:** the SP desktop app must be running (with its local REST API
enabled) for a sync pass to change anything on the SP side. A pass while
SP is closed still pulls Keep but can't create or update tasks until SP is
back up. The old plugin only ran while SP was open either way, so in
practice this changes little.

## Setup

You need two things from Super Productivity's desktop app first:

1. **Settings → Misc → Enable local REST API** (turn it on).
2. **Settings → Misc → Access Token** — copy this token.

Then follow [`keep-sync-daemon/README.md`](keep-sync-daemon/README.md):

- **Tray app** (`keep_sync_tray.py` on Windows, `keep_sync_tray_qt.py` on
  Linux): first launch shows a setup window — enter your Google email, a
  Google OAuth Token, and the SP Access Token, click **Connect & load
  lists**, then pick the Keep list and SP project from the dropdowns and
  hit **Save**.
- **CLI** (`keep_sync_daemon.py` via cron/systemd/Task Scheduler): mint a
  master token with `get_master_token.py`, fill in `config.json` from
  `config.example.json` (including `sp_access_token`, `sp_project_id`, and
  `keep_note_title`), and schedule the script.

## Constraints worth knowing before you rely on this

- **Desktop only, SP must be running.** The Local REST API is desktop-only
  and only answers while the app is open.
- **Unofficial Keep access.** `gkeepapi` reverse-engineers Google's
  internal mobile API. It can break without notice, and login occasionally
  gets challenged by Google (especially on 2FA accounts) — see the daemon
  README's troubleshooting section.
- **Checked/renamed/new items sync; deletes don't.** Checking off or
  renaming an item flows in whichever direction it happened. A new Keep
  item creates a matching SP task and a new top-level SP task creates a
  matching Keep item. Removing an item/task on either side never removes
  or completes its counterpart — deliberately, so a background loop never
  makes a destructive change.
- **Flat items only.** Indented Keep sub-items and SP subtasks are not
  mapped to each other; SP subtasks are skipped entirely in the SP → Keep
  direction.
- **On a conflict, Keep wins.** If the same item's text or checked state
  changed on both sides between passes, the Keep value is applied to SP.
