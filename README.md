# SuperProd Keep Sync

Two-way sync between a Google Keep checklist and a project in
[Super Productivity](https://super-productivity.com) — pick the Keep list and
the target project from a dropdown inside a Super Productivity plugin.
Checking off or renaming an item on either side updates the other. New items
only flow one direction, Keep → SP (see the constraints below).

## Why two parts

Google Keep has no public API for personal accounts, and Super Productivity
plugins run in a sandbox that cannot make network calls to `localhost` or
embed Python. So this is two pieces bridged by two small local JSON files:

```
┌─────────────────────────┐                  ┌───────────────────────────┐
│ keep-sync-daemon         │                  │ sp-plugin                  │
│ (Python, gkeepapi)       │                  │ (runs inside SP, Electron  │
│                           │──── writes ────▶│ desktop only)              │
│ cron/timer → polls Keep  │   state.json     │ state.json → reads via     │
│ → applies pending_       │                  │ executeNodeScript, then    │
│   changes.json to Keep,  │◀─── writes ──────│ PluginAPI.addTask/updateTask│
│   then re-reads Keep     │ pending_changes  │ TASK_UPDATE hook → queues  │
│ → ~/.sp-keep-sync/*.json │      .json       │ edited items to that file  │
└─────────────────────────┘                  └───────────────────────────┘
```

- **keep-sync-daemon/** — a Python script using the unofficial `gkeepapi`
  library, run on a schedule outside Super Productivity. Each pass first
  applies any queued Super Productivity edits (`pending_changes.json`) to
  Keep, then dumps every Keep checklist note to `~/.sp-keep-sync/state.json`.
- **sp-plugin/** — a Super Productivity plugin. Its UI (a picker for the
  Keep list + target project) runs as an iframe; its background sync loop
  runs as `plugin.js` so it keeps polling even while that UI isn't open. It
  reads `state.json` via SP's `executeNodeScript` (the only way a plugin can
  touch the filesystem) and creates/updates tasks through the normal
  `PluginAPI.addTask` / `updateTask` calls. It also listens for the
  `TASK_UPDATE` hook and writes checked/title edits made in SP to
  `pending_changes.json` for the daemon to pick up on its next cycle.

See each subfolder's README for setup specifics.

## Setup order

1. **keep-sync-daemon**: follow [`keep-sync-daemon/README.md`](keep-sync-daemon/README.md)
   to install deps, mint a Keep master token, and get a scheduled run
   writing `~/.sp-keep-sync/state.json`. Verify the file exists and has
   content before moving on.
2. **sp-plugin**: run `./build_plugin.sh` to produce `keep-list-sync.zip`,
   then in Super Productivity: **Settings → Plugins → Upload**. You'll get a
   native consent prompt for Node.js execution — this plugin needs it to
   read `state.json`; it's flagged as unverified third-party code by design
   (see [`sp-plugin`'s section of the plugin docs](https://github.com/super-productivity/super-productivity/blob/master/docs/plugin-development.md#nodejs-script-execution)
   for what that grants). Only accept it if you're comfortable with what
   this repo's code does — read `sp-plugin/plugin.js` first.
3. Open the plugin's panel in Super Productivity, pick your Keep list and
   target project, set a sync interval, and hit **Save**.

## Constraints worth knowing before you rely on this

- **Desktop only.** `executeNodeScript` (and therefore this whole design) is
  Electron-desktop-only — it will not work on the SP web app or mobile
  builds.
- **Unofficial Keep access.** `gkeepapi` reverse-engineers Google's internal
  mobile API. It can break without notice, and login occasionally gets
  challenged by Google (especially on 2FA accounts) — see the daemon
  README's troubleshooting section.
- **Checked/renamed items sync both ways; new items and deletes don't.**
  Checking off or renaming an item flows in whichever direction it happened.
  But a new item added directly in Keep creates a matching SP task, while a
  new task added directly in SP does **not** create a matching Keep item —
  and removing an item/task on either side never removes or completes its
  counterpart. Both are deliberate: they keep this from ever making a
  destructive or surprising change on either side of a background sync loop.
- **Flat items only (v1).** Indented/nested Keep checklist sub-items are
  synced as flat, independent tasks — no SP subtask hierarchy is inferred
  from Keep's indentation, to keep the first version simple.
- **`nodeExecution` is a broad grant.** Per Super Productivity's own docs, a
  plugin with this permission can run arbitrary code with full machine
  access, gated only by a one-time consent dialog. Review `plugin.js` before
  granting it, and revoke by disabling the plugin if you change your mind.
