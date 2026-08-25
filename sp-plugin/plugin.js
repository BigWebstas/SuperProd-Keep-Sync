// Google Keep List Sync — host-side background logic.
// UI for picking the Keep list / SP project lives in index.html; this file
// owns the actual polling loop and task create/update calls, since iframe
// timers die whenever the iframe UI is closed (see plugin-development.md).

const CFG_KEY = 'config';
const MAP_KEY = 'itemMap';
const STATUS_KEY = 'status';
const DEFAULT_INTERVAL_MIN = 5;

// The plugin-development.md prose calls these `plugin.onReady` /
// `plugin.onUnload` / `plugin.executeNodeScript`, while the published
// types.ts puts them directly on PluginAPI. Resolve whichever the running
// host actually provides instead of guessing wrong.
const nodeApi =
  typeof PluginAPI.executeNodeScript === 'function'
    ? PluginAPI
    : typeof plugin !== 'undefined'
      ? plugin
      : PluginAPI;

let intervalId = null;
let isSyncing = false;

function buildReadStateScript(customPath) {
  const pathExpr = customPath
    ? JSON.stringify(customPath)
    : "require('path').join(require('os').homedir(), '.sp-keep-sync', 'state.json')";
  return `
    const fs = require('fs');
    const statePath = ${pathExpr};
    if (!fs.existsSync(statePath)) {
      return { ok: false, error: 'not_found', path: statePath };
    }
    try {
      const raw = fs.readFileSync(statePath, 'utf8');
      return { ok: true, data: JSON.parse(raw) };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  `;
}

function buildQueueChangeScript(customPath, noteId, itemId, text, checked) {
  const dirExpr = customPath
    ? `require('path').dirname(${JSON.stringify(customPath)})`
    : "require('path').join(require('os').homedir(), '.sp-keep-sync')";
  return `
    const fs = require('fs');
    const path = require('path');
    const dir = ${dirExpr};
    fs.mkdirSync(dir, { recursive: true });
    const pendingPath = path.join(dir, 'pending_changes.json');
    let pending = {};
    try {
      if (fs.existsSync(pendingPath)) {
        pending = JSON.parse(fs.readFileSync(pendingPath, 'utf8'));
      }
    } catch (e) {}
    const noteId = ${JSON.stringify(noteId)};
    const itemId = ${JSON.stringify(itemId)};
    pending[noteId] = pending[noteId] || {};
    pending[noteId][itemId] = { text: ${JSON.stringify(text)}, checked: ${JSON.stringify(!!checked)} };
    fs.writeFileSync(pendingPath, JSON.stringify(pending, null, 2));
    return { ok: true };
  `;
}

async function getConfig() {
  const raw = await PluginAPI.loadSyncedData(CFG_KEY);
  return raw ? JSON.parse(raw) : null;
}

async function getMap() {
  const raw = await PluginAPI.loadSyncedData(MAP_KEY);
  return raw ? JSON.parse(raw) : {};
}

async function setStatus(status) {
  await PluginAPI.persistDataSynced(JSON.stringify(status), STATUS_KEY);
}

// Queues a checked/title edit for the keep-sync daemon to push to Keep on
// its next cycle (it owns the only Keep-writing credentials — this plugin
// can only shell out through Node's fs module). Deliberately doesn't touch
// itemMap: see the long comment on the TASK_UPDATE hook below for why.
async function queuePendingChange(cfg, noteId, itemId, text, checked) {
  try {
    const nodeResult = await nodeApi.executeNodeScript({
      script: buildQueueChangeScript(cfg.statePath, noteId, itemId, text, checked),
      timeout: 8000,
    });
    if (!nodeResult || !nodeResult.success) {
      console.warn('[keep-list-sync] failed to queue SP -> Keep change', nodeResult);
    }
  } catch (e) {
    console.warn('[keep-list-sync] failed to queue SP -> Keep change', e);
  }
}

async function runSync() {
  if (isSyncing) return;
  isSyncing = true;
  try {
    const cfg = await getConfig();
    if (!cfg || !cfg.noteTitle || !cfg.projectId) {
      return; // not configured yet — user hasn't saved anything in the UI
    }

    const nodeResult = await nodeApi.executeNodeScript({
      script: buildReadStateScript(cfg.statePath),
      timeout: 8000,
    });
    if (!nodeResult || !nodeResult.success) {
      const err = nodeResult && nodeResult.error;
      await setStatus({
        ok: false,
        at: Date.now(),
        error: `Node script failed: ${(err && err.message) || err || 'unknown error'}`,
      });
      return;
    }

    const payload = nodeResult.result;
    if (!payload || !payload.ok) {
      const message =
        payload && payload.error === 'not_found'
          ? `state.json not found at ${payload.path}. Is keep-sync-daemon running on a schedule?`
          : `Failed to read state file: ${(payload && payload.error) || 'unknown error'}`;
      await setStatus({ ok: false, at: Date.now(), error: message });
      return;
    }

    const note = (payload.data.notes || []).find((n) => n.title === cfg.noteTitle);
    if (!note) {
      await setStatus({
        ok: false,
        at: Date.now(),
        error: `Keep list "${cfg.noteTitle}" not found in last daemon sync (generated ${payload.data.generatedAt}).`,
      });
      return;
    }

    const fullMap = await getMap();
    const noteMap = fullMap[note.id] || {};
    let created = 0;
    let updated = 0;

    for (const item of note.items) {
      const entry = noteMap[item.id];
      if (entry && entry.taskId) {
        const patch = {};
        if (entry.text !== item.text) patch.title = item.text;
        if (entry.checked !== item.checked) patch.isDone = item.checked;
        if (Object.keys(patch).length > 0) {
          try {
            await PluginAPI.updateTask(entry.taskId, patch);
            updated++;
          } catch (e) {
            console.warn('[keep-list-sync] failed to update task', entry.taskId, e);
          }
        }
        entry.text = item.text;
        entry.checked = item.checked;
      } else {
        try {
          const taskId = await PluginAPI.addTask({
            title: item.text,
            projectId: cfg.projectId,
            isDone: item.checked,
          });
          noteMap[item.id] = { taskId, text: item.text, checked: item.checked };
          created++;
        } catch (e) {
          console.warn('[keep-list-sync] failed to create task for item', item.id, e);
        }
      }
    }

    // Safety net alongside the TASK_UPDATE hook below: SP's own
    // cross-device sync delivers remote changes (from other desktops
    // running this same SP project) through a bulk-import/hydrate path
    // that never dispatches the granular updateTask action the hook
    // listens for, so those edits would otherwise never reach Keep. A
    // full diff against live task state on every cycle catches them
    // regardless of how the change actually arrived.
    try {
      const trackedTaskIds = Object.values(noteMap)
        .map((entry) => entry.taskId)
        .filter(Boolean);
      if (trackedTaskIds.length > 0) {
        const liveTasks = await PluginAPI.getTasks();
        const liveById = new Map(liveTasks.map((t) => [t.id, t]));
        for (const itemId of Object.keys(noteMap)) {
          const entry = noteMap[itemId];
          if (!entry.taskId) continue;
          const liveTask = liveById.get(entry.taskId);
          if (!liveTask) continue; // deleted in SP — deletes don't propagate (see README)
          if (liveTask.title !== entry.text || liveTask.isDone !== entry.checked) {
            await queuePendingChange(cfg, note.id, itemId, liveTask.title, liveTask.isDone);
          }
        }
      }
    } catch (e) {
      console.warn('[keep-list-sync] failed to reconcile local task changes', e);
    }

    fullMap[note.id] = noteMap;
    await PluginAPI.persistDataSynced(JSON.stringify(fullMap), MAP_KEY);
    await setStatus({
      ok: true,
      at: Date.now(),
      noteTitle: note.title,
      itemCount: note.items.length,
      created,
      updated,
      dataGeneratedAt: payload.data.generatedAt,
    });
  } catch (e) {
    console.error('[keep-list-sync] sync failed', e);
    await setStatus({ ok: false, at: Date.now(), error: String((e && e.message) || e) });
  } finally {
    isSyncing = false;
  }
}

function startLoop(intervalMinutes) {
  if (intervalId) clearInterval(intervalId);
  const ms = Math.max(1, intervalMinutes || DEFAULT_INTERVAL_MIN) * 60 * 1000;
  intervalId = setInterval(runSync, ms);
}

PluginAPI.registerHeaderButton({
  label: 'Keep Sync',
  icon: 'sync',
  onClick: () => {
    runSync();
    PluginAPI.showSnack({ msg: 'Keep sync started…', type: 'INFO' });
  },
});

// Fires on any persisted-data write, including the config/sync-request
// bumps the index.html UI makes on Save / "Sync now".
PluginAPI.registerHook(PluginAPI.Hooks.PERSISTED_DATA_CHANGED, async () => {
  const cfg = await getConfig();
  startLoop(cfg && cfg.intervalMinutes);
  await runSync();
});

// Fast path for locally-made edits (near-instant, vs. waiting for the next
// polling cycle) — only fires for tasks this plugin created from a Keep
// item; other edits are ignored. This does NOT catch edits delivered via
// SP's own cross-device sync (see the reconciliation pass in runSync()
// above, which exists specifically to also catch those).
//
// Deliberately doesn't touch fullMap/itemMap here, even though we could
// optimistically set the new value: persistDataSynced() fires
// PERSISTED_DATA_CHANGED, which immediately re-runs the Keep -> SP pull
// sync against state.json — but state.json is still stale at this point
// (the daemon hasn't applied this pending change yet), so that pull would
// see our fresh cache value vs. the stale file value, treat the file as
// authoritative, and instantly revert the edit we just made. Leaving the
// cache untouched means it still matches the equally-stale state.json, so
// the pull diff is a no-op until the daemon actually applies this change
// and re-writes a fresh state.json — at which point the normal pull-diff
// logic reconciles the cache for free (redundant but harmless, since SP
// already has the value by then).
PluginAPI.registerHook(PluginAPI.Hooks.TASK_UPDATE, async ({ taskId, task, changes }) => {
  if (!changes || (!('title' in changes) && !('isDone' in changes))) return;

  const fullMap = await getMap();
  let noteId = null;
  let itemId = null;
  for (const nId of Object.keys(fullMap)) {
    const foundItemId = Object.keys(fullMap[nId]).find((id) => fullMap[nId][id].taskId === taskId);
    if (foundItemId) {
      noteId = nId;
      itemId = foundItemId;
      break;
    }
  }
  if (!noteId) return; // not a task we're tracking

  const cfg = await getConfig();
  if (!cfg) return;

  await queuePendingChange(cfg, noteId, itemId, task.title, task.isDone);
});

async function init() {
  const cfg = await getConfig();
  startLoop(cfg && cfg.intervalMinutes);
  await runSync();
}

if (typeof nodeApi.onReady === 'function') {
  nodeApi.onReady(init);
} else {
  init();
}

if (typeof nodeApi.onUnload === 'function') {
  nodeApi.onUnload(() => {
    if (intervalId) clearInterval(intervalId);
  });
}
