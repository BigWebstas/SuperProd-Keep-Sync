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
