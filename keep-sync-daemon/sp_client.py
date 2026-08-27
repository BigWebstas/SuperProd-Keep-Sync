#!/usr/bin/env python3
"""
Minimal client for Super Productivity's Local REST API.

SP desktop exposes a localhost REST API (default http://127.0.0.1:3876)
once it's turned on in Settings -> Misc -> "Enable local REST API". Every
request except /health needs the bearer token shown next to that setting
(Settings -> Misc -> Access Token).

This replaces the old sp-plugin + state.json file bridge: keep_sync_core
now talks to the running SP app directly, and SP itself turns the
addTask/updateTask calls into sync operations and pushes them to whatever
sync backend (Super Sync, Dropbox, WebDAV, ...) the user has configured.

The tradeoff vs. the old design: the SP desktop app has to be running for
a sync pass to do anything on the SP side. A pass while it's closed still
pulls Keep and updates state.json-equivalent bookkeeping, but can't
create/update tasks until SP is back up.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:3876"


class SPError(RuntimeError):
    """Any failure talking to the Local REST API (unreachable, auth, 5xx)."""


@dataclass
class SPTask:
    id: str
    title: str
    is_done: bool
    parent_id: str | None
    project_id: str | None

    @classmethod
    def from_json(cls, data: dict) -> "SPTask":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            is_done=bool(data.get("isDone", False)),
            parent_id=data.get("parentId") or None,
            project_id=data.get("projectId") or None,
        )


@dataclass
class SPProject:
    id: str
    title: str
    is_archived: bool

    @classmethod
    def from_json(cls, data: dict) -> "SPProject":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            is_archived=bool(data.get("isArchived", False)),
        )


class SPClient:
    """Thin wrapper over the handful of Local REST API endpoints this sync
    needs. Never returns partial data: any non-2xx or transport error is
    raised as SPError so callers can leave both sides untouched."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = "", timeout: float = 8.0):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise SPError(
                f"could not reach Super Productivity at {self.base_url} ({e}). "
                "Is the desktop app running with the local REST API enabled "
                "(Settings -> Misc)?"
            ) from e
        if resp.status_code == 401:
            raise SPError(
                "Super Productivity rejected the access token (401). Copy a "
                "fresh one from Settings -> Misc -> Access Token."
            )

        payload = None
        if resp.content:
            try:
                payload = resp.json()
            except ValueError:
                payload = None

        # The Local REST API wraps every response as
        # {"ok": true, "data": ...} or {"ok": false, "error": {...}}.
        if isinstance(payload, dict) and "ok" in payload:
            if not payload.get("ok"):
                err = payload.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise SPError(f"{method} {path}: {msg or 'request failed'}")
            return payload.get("data")

        if not resp.ok:
            body = (resp.text or "").strip()
            raise SPError(f"{method} {path} failed: HTTP {resp.status_code} {body[:200]}")
        return payload

    def health(self) -> bool:
        """True if the API answers /health at all (no auth needed)."""
        try:
            requests.get(f"{self.base_url}/health", timeout=self.timeout)
            return True
        except requests.RequestException:
            return False

    @staticmethod
    def _rows(data, key: str) -> list[dict]:
        """Normalize the shapes the Local REST API might return: a bare
        list, {"<key>": [...]}, or an NgRx-style {id: {...}} entity map."""
        if data is None:
            return []
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            inner = data.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, dict):
                return [r for r in inner.values() if isinstance(r, dict)]
            if isinstance(data.get("entities"), dict):
                return [r for r in data["entities"].values() if isinstance(r, dict)]
            return [v for v in data.values() if isinstance(v, dict)]
        return []

    def list_projects(self) -> list[SPProject]:
        data = self._request("GET", "/projects")
        return [SPProject.from_json(p) for p in self._rows(data, "projects")]

    def list_tasks(self, project_id: str, include_done: bool = True, source: str = "active") -> list[SPTask]:
        params = {"projectId": project_id, "source": source}
        if include_done:
            params["includeDone"] = "true"
        data = self._request("GET", "/tasks", params=params)
        return [SPTask.from_json(t) for t in self._rows(data, "tasks")]

    def add_task(self, title: str, project_id: str, is_done: bool = False) -> str:
        payload = {"title": title, "projectId": project_id, "isDone": bool(is_done)}
        data = self._request("POST", "/tasks", json=payload)
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            task_id = data.get("id") or data.get("taskId") or (data.get("task") or {}).get("id")
            if task_id:
                return task_id
        raise SPError(f"POST /tasks did not return a task id (got {data!r})")

    def update_task(self, task_id: str, patch: dict) -> None:
        self._request("PATCH", f"/tasks/{task_id}", json=patch)
