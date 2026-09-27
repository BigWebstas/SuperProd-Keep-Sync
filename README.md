# SuperProd Keep Sync

Two-way sync between a Google Keep checklist and a project in
[Super Productivity](https://super-productivity.com). Pick the Keep list and the
target project once (from a tray app or `config.json`). Checking off or renaming
an item on either side updates the other; new items sync both ways. **Deletes
never propagate.**

## How it works

Google Keep has no public API for personal accounts, so a small Python process
holds a Google "master token" and talks to Keep through the unofficial `gkeepapi`
library. It reconciles the checklist against the Super Productivity project
through **SP's Local REST API** (`http://127.0.0.1:3876`). SP then pushes those
task changes onward through whatever backend you've configured (Super Sync,
Dropbox, WebDAV, local files).

The Keep-item ↔ SP-task pairing lives in `~/.sp-keep-sync/item_map.json`. SP must
be running for a pass to change anything on the SP side.

## Setup

1. In Super Productivity: **Settings → Misc → Enable local REST API**, copy the
   **Access Token**.
2. Then follow [`keep-sync-daemon/README.md`](keep-sync-daemon/README.md):
   - **Tray app** — `keep_sync_tray.py` (Windows) / `keep_sync_tray_qt.py`
     (Linux). First launch walks you through a setup window and schedules itself.
   - **CLI** — mint a master token with `get_master_token.py`, fill in
     `config.json`, and schedule `keep_sync_daemon.py` (cron / systemd / Task
     Scheduler).

## What syncs

| | Behaviour |
|---|---|
| Checked / renamed items | Flow in whichever direction the change happened |
| New items | Keep item → SP task; new top-level SP task → Keep item |
| Tagging new tasks | Optional: pick one or more SP tags in setup and every task created from a Keep item gets them (SP only allows this at creation) |
| Default task estimate | Optional: set a minutes value in setup (`sp_default_task_minutes`) and every task created from a Keep item gets that `timeEstimate` (SP only allows this at creation); `0` = none |
| Deletes | Never propagate — deliberately, so a background loop is never destructive |
| Nested sub-items / subtasks | Not mapped; SP subtasks are skipped SP → Keep |
| Conflict (both sides changed) | Keep wins |
| Keep auth failure alert | If the master token goes stale/revoked or Google can't be reached, a "⚠️ Keep Sync needs attention" task is created in the synced SP project (find-or-create, not one per failed pass) and auto-resolved once Keep is reachable again |
| Sort Keep alphabetically | Optional (`keep_sort_alphabetically`): the Keep checklist gets re-alphabetized after every pass it's out of order. **Keep-side only** — SP's Local REST API has no reorder endpoint, so the SP task list's order is whatever SP itself does with it |
| AI merchant-prefix rename | Optional (`ai_merchant_rename_enabled` + an Anthropic API key): a freely-dictated new item ("from walmart add toilet tablets") gets rewritten to "Walmart - Item" (Claude Haiku) before its SP task is created, and the Keep item is renamed to match. One API call per item, ever — `<state_dir>/ai_renamed.json` remembers which items have already been looked at (renamed or not), so nothing gets asked twice or re-billed every pass |

Every entry point also checks GitHub for a newer release at most once a day
(cached in `<state_dir>/update_check.json`) and reports it. On Windows the tray
menu grows a **Download** button that fetches the installer with a progress
popup, then a **Launch installer** button; the Linux tray and the CLI daemon
just get a link (there's no Linux installer yet). Nothing downloads or launches
without you clicking it.

`gkeepapi` is reverse-engineered and can break without notice; Google
occasionally challenges logins (especially with 2FA). If it starts failing, mint
a fresh token.
