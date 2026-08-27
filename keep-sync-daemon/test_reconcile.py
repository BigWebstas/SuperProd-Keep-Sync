#!/usr/bin/env python3
"""
Unit tests for keep_sync_core.reconcile_sp -- the Keep <-> Super
Productivity matrix (create/update each way, conflicts, deletes).

Run: python3 -m unittest test_reconcile   (needs gkeepapi importable;
no network, no real SP app).
"""
from __future__ import annotations

import unittest

import gkeepapi.node

import keep_sync_core as core
import sp_client


class FakeKeep:
    """Just enough of gkeepapi.Keep for find_keep_list(): an .all()."""

    def __init__(self, notes):
        self._notes = notes

    def all(self):
        return list(self._notes)


class FakeSP:
    """In-memory stand-in for sp_client.SPClient."""

    def __init__(self, tasks=None):
        self.tasks: dict[str, sp_client.SPTask] = {}
        for t in tasks or []:
            self.tasks[t.id] = t
        self._next = 1
        self.calls: list[tuple] = []

    def list_tasks(self, project_id, include_done=True, source="active"):
        return [t for t in self.tasks.values() if t.project_id == project_id]

    def add_task(self, title, project_id, is_done=False):
        tid = f"sp{self._next}"
        self._next += 1
        self.tasks[tid] = sp_client.SPTask(tid, title, bool(is_done), None, project_id)
        self.calls.append(("add", tid, title, is_done))
        return tid

    def update_task(self, task_id, patch):
        self.calls.append(("update", task_id, dict(patch)))
        t = self.tasks[task_id]
        if "title" in patch:
            t.title = patch["title"]
        if "isDone" in patch:
            t.is_done = bool(patch["isDone"])


def make_list(title="My List", items=()):
    note = gkeepapi.node.List()
    note.title = title
    handles = {}
    for text, checked in items:
        it = note.add(text, bool(checked))
        handles[text] = it
    return note, handles


def cfg(note_title="My List"):
    return {"keep_note_title": note_title, "sp_project_id": "p1", "include_archived": False}


class ReconcileTests(unittest.TestCase):
    def test_new_keep_item_creates_task(self):
        note, _ = make_list(items=[("buy milk", False)])
        sp = FakeSP()
        item_map = {}

        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.created_sp, 1)
        self.assertEqual(len(sp.tasks), 1)
        task = next(iter(sp.tasks.values()))
        self.assertEqual(task.title, "buy milk")
        entry = next(iter(item_map[note.id].values()))
        self.assertEqual(entry["taskId"], task.id)

    def test_keep_check_propagates_to_sp(self):
        note, handles = make_list(items=[("task a", False)])
        sp = FakeSP()
        item_map = {}
        core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)  # creates sp1
        sp.calls.clear()

        handles["task a"].checked = True
        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.updated_sp, 1)
        self.assertEqual(sp.calls, [("update", "sp1", {"isDone": True})])
        self.assertTrue(sp.tasks["sp1"].is_done)

    def test_sp_rename_propagates_to_keep(self):
        note, handles = make_list(items=[("old", False)])
        sp = FakeSP()
        item_map = {}
        core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)
        sp.tasks["sp1"].title = "new name"

        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.updated_keep, 1)
        self.assertTrue(res.keep_dirty)
        self.assertEqual(handles["old"].text, "new name")

    def test_new_toplevel_sp_task_creates_keep_item(self):
        note, _ = make_list(items=[])
        sp = FakeSP([sp_client.SPTask("sp1", "from sp", False, None, "p1")])
        item_map = {}

        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.created_keep, 1)
        self.assertEqual([it.text for it in note.items], ["from sp"])
        self.assertEqual(item_map[note.id][note.items[0].id]["taskId"], "sp1")

    def test_sp_subtask_is_ignored(self):
        note, _ = make_list(items=[])
        sp = FakeSP([sp_client.SPTask("sp1", "child", False, "parent1", "p1")])
        item_map = {}

        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.created_keep, 0)
        self.assertEqual(list(note.items), [])

    def test_conflict_keep_wins(self):
        note, handles = make_list(items=[("thing", False)])
        sp = FakeSP()
        item_map = {}
        core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        # both sides rename between passes
        handles["thing"].text = "keep version"
        sp.tasks["sp1"].title = "sp version"
        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(sp.tasks["sp1"].title, "keep version")
        self.assertEqual(handles["thing"].text, "keep version")
        self.assertEqual(res.updated_keep, 0)

    def test_delete_in_sp_does_not_touch_keep(self):
        note, handles = make_list(items=[("keep me", False)])
        sp = FakeSP()
        item_map = {}
        core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        del sp.tasks["sp1"]  # task removed in SP
        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.total, 0)
        self.assertEqual(handles["keep me"].text, "keep me")
        self.assertFalse(handles["keep me"].checked)

    def test_missing_note_raises_lookuperror(self):
        note, _ = make_list(title="Other")
        with self.assertRaises(LookupError):
            core.reconcile_sp(cfg("My List"), FakeKeep([note]), FakeSP(), {})

    def test_second_pass_is_noop(self):
        note, _ = make_list(items=[("a", False), ("b", True)])
        sp = FakeSP()
        item_map = {}
        core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)
        sp.calls.clear()

        res = core.reconcile_sp(cfg(), FakeKeep([note]), sp, item_map)

        self.assertEqual(res.total, 0)
        self.assertEqual(sp.calls, [])


class RowShapeTests(unittest.TestCase):
    """sp_client._rows: the response shapes the Local REST API might use
    (a bare list, a keyed list, an entity map)."""

    def test_bare_list(self):
        self.assertEqual(sp_client.SPClient._rows([{"id": "a"}, {"id": "b"}], "x"),
                         [{"id": "a"}, {"id": "b"}])

    def test_keyed_list(self):
        self.assertEqual(sp_client.SPClient._rows({"tasks": [{"id": "a"}]}, "tasks"),
                         [{"id": "a"}])

    def test_entity_map(self):
        rows = sp_client.SPClient._rows({"a": {"id": "a"}, "b": {"id": "b"}}, "tasks")
        self.assertEqual(sorted(r["id"] for r in rows), ["a", "b"])

    def test_none_and_junk(self):
        self.assertEqual(sp_client.SPClient._rows(None, "x"), [])
        self.assertEqual(sp_client.SPClient._rows("nope", "x"), [])


if __name__ == "__main__":
    unittest.main()
