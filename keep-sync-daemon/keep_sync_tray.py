#!/usr/bin/env python3
"""
Windows system-tray app for keep-sync-daemon.

On first launch it shows a small setup window: enter your Google account
email and an OAuth Token (see README.md for how to get one), and it
exchanges that for a Keep master token and stores it locally — no terminal
required. After that it sits in the tray and re-syncs on its own timer for
as long as it's running: pulling Keep's state to state.json, and pushing
any Super Productivity edits queued in pending_changes.json back to Keep
(see keep_sync_core.sync_once).

This is the GUI/packaged alternative to keep_sync_daemon.py, which is
meant to be invoked by an external scheduler (cron/systemd timer/Task
Scheduler) instead.

Packaging into a standalone build (a folder, not a single .exe -- see the
--onedir note in build-windows-tray.yml; see README.md for the full command):
    pyinstaller --onedir --windowed --name KeepSyncTray --icon packaging/windows/keepsync.ico keep_sync_tray.py
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

import keep_sync_core as core

APP_NAME = "Keep Sync"

# When frozen by PyInstaller, __file__ resolves inside a temporary
# extraction directory that doesn't persist between runs — config.json
# (and the log file) must live next to the .exe instead so they survive
# a restart.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "keep_sync_tray.log"

log = core.setup_logging(LOG_PATH, relaunch_on_crash=True)


def default_config() -> dict:
    return {
        "email": "",
        "state_dir": core.DEFAULT_STATE_DIR,
        "include_archived": False,
        "sync_interval_minutes": core.DEFAULT_SYNC_INTERVAL_MINUTES,
        "run_at_startup": False,
        "sp_api_base_url": core.DEFAULT_SP_API_BASE_URL,
        "sp_access_token": "",
        "sp_project_id": "",
        "sp_project_title": "",
        "sp_new_task_tag_ids": [],
        "sp_new_task_tag_titles": [],
        "sp_default_task_minutes": 0,
        "keep_note_title": "",
        "keep_sort_alphabetically": False,
        "ai_merchant_rename_enabled": False,
        "anthropic_api_key": "",
    }


def load_or_default_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return core.load_config(CONFIG_PATH)
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    return default_config()


def needs_setup(cfg: dict) -> bool:
    if not cfg.get("email"):
        return True
    if not (cfg.get("sp_access_token") and cfg.get("sp_project_id") and cfg.get("keep_note_title")):
        return True
    state_dir = core.resolve_state_dir(cfg.get("state_dir", core.DEFAULT_STATE_DIR))
    return not (state_dir / "master_token").exists()


def set_run_at_startup(enable: bool) -> None:
    """Best-effort HKCU Run-key registration. No-op off Windows."""
    if sys.platform != "win32":
        return
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            if getattr(sys, "frozen", False):
                command = f'"{sys.executable}"'
            else:
                command = f'"{sys.executable}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def make_icon_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill="#1F6FEB")
    d.rectangle((18, 16, 46, 48), fill="white")
    for y in (24, 32, 40):
        d.line((22, y, 42, y), fill="#1F6FEB", width=3)
    return img


class SetupWindow(tk.Tk):
    """Collects the Google + Super Productivity credentials, then (via the
    "Connect & load lists" button) fetches the Keep checklists and SP
    projects to pick from. Returns the finished config via `result`, or
    leaves `result` as None if the user closes the window without
    finishing."""

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = dict(cfg)
        self.result: dict | None = None
        self._project_ids: list[str] = []
        self._tag_ids: list[str] = []

        self.title(f"{APP_NAME} — Setup")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        pad = {"padx": 10, "pady": 4}

        info = (
            "Google Keep has no public API, so this connects the same way\n"
            "the Android app does. To get an OAuth Token: open\n"
            "accounts.google.com/EmbeddedSetup in a browser, sign in, then\n"
            "copy the 'oauth_token' cookie's value (dev tools > Application\n"
            "> Cookies). Leave it blank to reuse an already-saved token.\n"
            "The Super Productivity Access Token is in the desktop app under\n"
            "Settings > Misc (enable the local REST API there first)."
        )
        tk.Label(self, text=info, justify="left").grid(row=0, column=0, columnspan=2, **pad)

        tk.Label(self, text="Google account email:").grid(row=1, column=0, sticky="e", **pad)
        self.email_var = tk.StringVar(value=self.cfg.get("email", ""))
        tk.Entry(self, textvariable=self.email_var, width=36).grid(row=1, column=1, **pad)

        tk.Label(self, text="OAuth Token:").grid(row=2, column=0, sticky="e", **pad)
        token_frame = tk.Frame(self)
        token_frame.grid(row=2, column=1, sticky="w", **pad)
        self.token_var = tk.StringVar()
        tk.Entry(token_frame, textvariable=self.token_var, width=26, show="•").pack(side="left")
        tk.Button(
            token_frame, text="Open sign-in page",
            command=lambda: webbrowser.open(core.GOOGLE_EMBEDDED_SETUP_URL),
        ).pack(side="left", padx=(4, 0))

        tk.Label(self, text="SP API base URL:").grid(row=3, column=0, sticky="e", **pad)
        self.sp_url_var = tk.StringVar(value=self.cfg.get("sp_api_base_url", core.DEFAULT_SP_API_BASE_URL))
        tk.Entry(self, textvariable=self.sp_url_var, width=36).grid(row=3, column=1, **pad)

        tk.Label(self, text="SP Access Token:").grid(row=4, column=0, sticky="e", **pad)
        self.sp_token_var = tk.StringVar(value=self.cfg.get("sp_access_token", ""))
        tk.Entry(self, textvariable=self.sp_token_var, width=36, show="•").grid(row=4, column=1, **pad)

        self.connect_btn = tk.Button(self, text="Connect & load lists", command=self._connect)
        self.connect_btn.grid(row=5, column=0, columnspan=2, pady=6)

        tk.Label(self, text="Keep list:").grid(row=6, column=0, sticky="e", **pad)
        self.note_var = tk.StringVar()
        self.note_combo = ttk.Combobox(self, textvariable=self.note_var, width=34, state="disabled")
        self.note_combo.grid(row=6, column=1, **pad)

        tk.Label(self, text="Super Productivity project:").grid(row=7, column=0, sticky="e", **pad)
        self.project_var = tk.StringVar()
        self.project_combo = ttk.Combobox(self, textvariable=self.project_var, width=34, state="disabled")
        self.project_combo.grid(row=7, column=1, **pad)

        tk.Label(self, text="Tag new tasks with:").grid(row=8, column=0, sticky="ne", **pad)
        tag_frame = tk.Frame(self)
        tag_frame.grid(row=8, column=1, sticky="w", **pad)
        self.tag_list = tk.Listbox(
            tag_frame, height=4, width=32, selectmode=tk.EXTENDED,
            exportselection=False, state="disabled",
        )
        tag_scroll = tk.Scrollbar(tag_frame, orient="vertical", command=self.tag_list.yview)
        self.tag_list.config(yscrollcommand=tag_scroll.set)
        self.tag_list.pack(side="left", fill="y")
        tag_scroll.pack(side="left", fill="y")

        tk.Label(self, text="Default task estimate (minutes, 0 = none):").grid(row=9, column=0, sticky="e", **pad)
        self.estimate_var = tk.StringVar(value=str(self.cfg.get("sp_default_task_minutes", 0) or 0))
        tk.Entry(self, textvariable=self.estimate_var, width=8).grid(row=9, column=1, sticky="w", **pad)

        tk.Label(self, text="Sync every (minutes):").grid(row=10, column=0, sticky="e", **pad)
        self.interval_var = tk.StringVar(
            value=str(self.cfg.get("sync_interval_minutes", core.DEFAULT_SYNC_INTERVAL_MINUTES))
        )
        tk.Entry(self, textvariable=self.interval_var, width=8).grid(row=10, column=1, sticky="w", **pad)

        self.startup_var = tk.BooleanVar(value=self.cfg.get("run_at_startup", False))
        tk.Checkbutton(
            self, text="Start automatically when Windows starts", variable=self.startup_var
        ).grid(row=11, column=0, columnspan=2, sticky="w", **pad)

        self.sort_var = tk.BooleanVar(value=self.cfg.get("keep_sort_alphabetically", False))
        tk.Checkbutton(
            self, text="Keep the Keep checklist sorted alphabetically", variable=self.sort_var
        ).grid(row=12, column=0, columnspan=2, sticky="w", **pad)

        self.ai_rename_var = tk.BooleanVar(value=self.cfg.get("ai_merchant_rename_enabled", False))
        tk.Checkbutton(
            self, text='Use AI to prefix new items with the merchant name (e.g. "Walmart - Toilet tablets")',
            variable=self.ai_rename_var,
        ).grid(row=13, column=0, columnspan=2, sticky="w", **pad)

        tk.Label(self, text="Anthropic API Key:").grid(row=14, column=0, sticky="e", **pad)
        self.anthropic_key_var = tk.StringVar(value=self.cfg.get("anthropic_api_key", ""))
        tk.Entry(self, textvariable=self.anthropic_key_var, width=36, show="•").grid(row=14, column=1, **pad)

        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(self, textvariable=self.status_var, fg="red", wraplength=380, justify="left")
        self.status_label.grid(row=15, column=0, columnspan=2, **pad)

        self.submit_btn = tk.Button(self, text="Save & Start Syncing", command=self._submit, state="disabled")
        self.submit_btn.grid(row=16, column=0, columnspan=2, pady=10)

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _resolve_master_token(self, email: str, oauth_token: str, state_dir) -> str:
        if oauth_token:
            master_token = core.exchange_master_token(email, oauth_token)
            core.save_master_token(state_dir, master_token)
            return master_token
        return core.get_master_token(state_dir)  # raises RuntimeError if none saved

    def _connect(self) -> None:
        email = self.email_var.get().strip()
        oauth_token = self.token_var.get().strip()
        sp_url = self.sp_url_var.get().strip() or core.DEFAULT_SP_API_BASE_URL
        sp_token = self.sp_token_var.get().strip()

        if not email or not sp_token:
            self.status_label.config(fg="red")
            self.status_var.set("Email and SP Access Token are required.")
            return

        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        self.connect_btn.config(state="disabled")
        self.status_label.config(fg="red")
        self.status_var.set("Connecting to Google and Super Productivity…")
        self.update_idletasks()

        try:
            master_token = self._resolve_master_token(email, oauth_token, state_dir)
        except Exception as e:
            self.status_var.set(f"Google sign-in failed: {e}")
            self.connect_btn.config(state="normal")
            return

        try:
            titles = core.list_keep_checklist_titles_isolated(
                email, master_token, state_dir, self.cfg.get("include_archived", False)
            )
        except Exception as e:
            self.status_var.set(f"Could not read Keep lists: {e}")
            self.connect_btn.config(state="normal")
            return

        try:
            projects = core.list_sp_projects(sp_url, sp_token)
        except Exception as e:
            self.status_var.set(f"Could not reach Super Productivity: {e}")
            self.connect_btn.config(state="normal")
            return

        self.note_combo.config(values=titles, state="readonly" if titles else "disabled")
        if self.cfg.get("keep_note_title") in titles:
            self.note_var.set(self.cfg["keep_note_title"])
        elif titles:
            self.note_var.set(titles[0])

        self._project_ids = [pid for pid, _ in projects]
        project_titles = [title for _, title in projects]
        self.project_combo.config(values=project_titles, state="readonly" if projects else "disabled")
        if self.cfg.get("sp_project_id") in self._project_ids:
            self.project_var.set(project_titles[self._project_ids.index(self.cfg["sp_project_id"])])
        elif project_titles:
            self.project_var.set(project_titles[0])

        try:
            tags = core.list_sp_tags(sp_url, sp_token)
        except Exception:
            tags = []  # optional -- an old SP without GET /tags shouldn't block setup
        self._tag_ids = [tid for tid, _ in tags]
        self.tag_list.config(state="normal")
        self.tag_list.delete(0, tk.END)
        for _, title in tags:
            self.tag_list.insert(tk.END, title)
        wanted = set(self.cfg.get("sp_new_task_tag_ids") or [])
        for i, tid in enumerate(self._tag_ids):
            if tid in wanted:
                self.tag_list.selection_set(i)
        if not tags:
            self.tag_list.config(state="disabled")

        self.connect_btn.config(state="normal")
        if not titles:
            self.status_var.set("Connected, but no Keep checklists were found.")
        elif not projects:
            self.status_var.set("Connected, but no Super Productivity projects were found.")
        else:
            self.status_label.config(fg="#27ae60")
            self.status_var.set("Connected. Pick a list and a project, then Save.")
            self.submit_btn.config(state="normal")

    def _submit(self) -> None:
        try:
            interval = int(self.interval_var.get().strip())
            if interval < 1:
                raise ValueError
        except ValueError:
            self.status_label.config(fg="red")
            self.status_var.set("Sync interval must be a whole number of minutes (1 or more).")
            return

        try:
            estimate_minutes = int(self.estimate_var.get().strip() or "0")
            if estimate_minutes < 0:
                raise ValueError
        except ValueError:
            self.status_label.config(fg="red")
            self.status_var.set("Default task estimate must be 0 or a positive whole number of minutes.")
            return

        note_title = self.note_var.get().strip()
        project_title = self.project_var.get().strip()
        project_titles = list(self.project_combo.cget("values"))
        project_id = (
            self._project_ids[project_titles.index(project_title)]
            if project_title in project_titles and len(self._project_ids) == len(project_titles)
            else ""
        )

        if not note_title or not project_id:
            self.status_label.config(fg="red")
            self.status_var.set("Pick a Keep list and a project first (use Connect).")
            return

        selected_rows = [i for i in self.tag_list.curselection() if i < len(self._tag_ids)]
        tag_ids = [self._tag_ids[i] for i in selected_rows]
        tag_titles = [self.tag_list.get(i) for i in selected_rows]

        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        self.cfg.update(
            email=self.email_var.get().strip(),
            state_dir=str(state_dir),
            include_archived=self.cfg.get("include_archived", False),
            sync_interval_minutes=interval,
            run_at_startup=self.startup_var.get(),
            sp_api_base_url=self.sp_url_var.get().strip() or core.DEFAULT_SP_API_BASE_URL,
            sp_access_token=self.sp_token_var.get().strip(),
            sp_project_id=project_id,
            sp_project_title=project_title,
            sp_new_task_tag_ids=tag_ids,
            sp_new_task_tag_titles=tag_titles,
            sp_default_task_minutes=estimate_minutes,
            keep_note_title=note_title,
            keep_sort_alphabetically=self.sort_var.get(),
            ai_merchant_rename_enabled=self.ai_rename_var.get(),
            anthropic_api_key=self.anthropic_key_var.get().strip(),
        )
        core.save_config(CONFIG_PATH, self.cfg)

        try:
            set_run_at_startup(self.startup_var.get())
        except OSError:
            pass  # best-effort; setup still succeeded

        self.result = self.cfg
        self.destroy()


def run_setup_window(cfg: dict) -> dict | None:
    window = SetupWindow(cfg)
    window.mainloop()
    return window.result


class StatusWindow(tk.Tk):
    """Small status window opened by double-clicking the tray icon or via
    its "Show status" menu entry. Runs on its own thread (see
    TrayApp._show_status_window) since pystray's icon.run() already owns
    the main thread."""

    def __init__(self, app: "TrayApp"):
        super().__init__()
        self.app = app

        self.title(APP_NAME)
        self.resizable(True, True)
        self.minsize(420, 420)

        pad = {"padx": 10, "pady": 6}
        row = 0

        tk.Label(self, text=f"Account: {app.cfg.get('email', '')}", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", **pad
        )
        row += 1

        project = app.cfg.get("sp_project_title") or app.cfg.get("sp_project_id") or "(not set)"
        tk.Label(self, text=f"Project: {project}", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", **pad
        )
        row += 1

        tags = app.cfg.get("sp_new_task_tag_titles") or app.cfg.get("sp_new_task_tag_ids") or []
        tk.Label(self, text=f"Tags on new tasks: {', '.join(tags) if tags else 'none'}", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", **pad
        )
        row += 1

        self.ai_status_var = tk.StringVar()
        self.ai_status_label = tk.Label(self, textvariable=self.ai_status_var, anchor="w")
        self.ai_status_label.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        self._ai_status_text: str | None = None
        self._ai_status_color = "gray"
        self._init_ai_status()

        self.status_var = tk.StringVar()
        tk.Label(self, textvariable=self.status_var, wraplength=340, justify="left", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", **pad
        )
        row += 1

        tk.Button(self, text="Sync now", command=self._sync_now).grid(
            row=row, column=0, padx=10, pady=10, sticky="ew"
        )
        tk.Button(self, text="Reconfigure…", command=self._reconfigure).grid(
            row=row, column=1, padx=10, pady=10, sticky="ew"
        )
        row += 1

        self.update_var = tk.StringVar()
        self.update_label = tk.Label(
            self, textvariable=self.update_var, anchor="w", fg="#1a7f37", cursor="hand2"
        )
        self.update_label.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        self.update_label.bind("<Button-1>", self._open_release_page)
        row += 1

        tk.Label(self, text=f"Version {core.get_version()}", anchor="w", fg="gray").grid(
            row=row, column=0, columnspan=2, sticky="w", **pad
        )
        row += 1

        tk.Label(self, text="Log:", anchor="w").grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        log_frame = tk.Frame(self)
        log_frame.grid(row=row, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="nsew")
        self.log_text = tk.Text(log_frame, height=12, width=56, state="disabled", wrap="none")
        log_scroll = tk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="left", fill="y")
        # The window is resizable (see resizable() above); this is what
        # actually sends the extra space to the log row instead of leaving
        # it as blank padding elsewhere when the window is enlarged.
        self.grid_rowconfigure(row, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # Polled, not pushed: reuses the same log file setup_logging() already
        # writes (see core.LogTailer), so there's no second logging handler
        # to keep wired up. Folded into the same poll loop as the status
        # line below rather than a second .after() chain.
        self._log_tailer = core.LogTailer(log.log_path)
        self._append_log_text(self._log_tailer.seed())

        self._refresh_status()

    LOG_MAX_LINES = 2000  # caps growth for a long-open window; matches the Qt tray's setMaximumBlockCount

    def _init_ai_status(self) -> None:
        enabled = bool(self.app.cfg.get("ai_merchant_rename_enabled"))
        model = (self.app.cfg.get("ai_merchant_rename_model") or "").strip() or core.DEFAULT_AI_RENAME_MODEL
        api_key = core.get_anthropic_api_key(self.app.cfg)
        if not enabled:
            self._ai_status_text, self._ai_status_color = "AI rename: off", "gray"
        elif not api_key:
            self._ai_status_text = f"AI rename: {model} — no API key set"
            self._ai_status_color = "#c0392b"
        else:
            self._ai_status_text = f"AI rename: {model} — checking…"
            self._ai_status_color = "gray"
            threading.Thread(
                target=self._check_ai_connection, args=(api_key, model), daemon=True, name="ai-status-check",
            ).start()

    def _check_ai_connection(self, api_key: str, model: str) -> None:
        connected = core.check_anthropic_connection(api_key)
        # Plain attribute writes read back by the next _refresh_status tick
        # -- same cross-thread pattern already used for app.status/
        # app._download_percent elsewhere in this file (safe under the GIL).
        if connected:
            self._ai_status_text, self._ai_status_color = f"AI rename: {model} — connected", "#1a7f37"
        else:
            self._ai_status_text = f"AI rename: {model} — not reachable"
            self._ai_status_color = "#c0392b"

    def _append_log_text(self, text: str) -> None:
        if not text:
            return
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, text if text.endswith("\n") else text + "\n")
        overflow = int(self.log_text.index("end-1c").split(".")[0]) - self.LOG_MAX_LINES
        if overflow > 0:
            self.log_text.delete("1.0", f"{overflow + 1}.0")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def _open_release_page(self, event=None) -> None:
        if self.app.update_info:
            webbrowser.open(self.app.update_info.url)

    def _refresh_status(self) -> None:
        self.status_var.set(self.app.status or "Waiting for the first sync...")
        info = self.app.update_info
        self.update_var.set(f"Update available: {info.version} (click to open)" if info else "")
        self.ai_status_var.set(self._ai_status_text or "")
        self.ai_status_label.config(fg=self._ai_status_color)
        self._append_log_text(self._log_tailer.read_new())
        self.after(1000, self._refresh_status)

    def _sync_now(self) -> None:
        self.app._sync_now()

    def _reconfigure(self) -> None:
        self.destroy()
        self.app._reconfigure()


class DownloadProgressWindow(tk.Tk):
    """Shown while an update installer downloads (see
    TrayApp._start_download). Same "own tk.Tk(), own thread" pattern as
    StatusWindow -- pystray's icon.run() already owns the main thread.
    Polls app._download_percent the same way StatusWindow polls app.status:
    the download runs on a worker thread, and a plain int read/write is
    safe across threads under the GIL without extra locking."""

    def __init__(self, app: "TrayApp"):
        super().__init__()
        self.app = app
        self.title(APP_NAME)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no closing mid-download

        pad = {"padx": 10, "pady": 6}
        version = app.update_info.version if app.update_info else ""
        tk.Label(self, text=f"Downloading {version}…", anchor="w").grid(
            row=0, column=0, sticky="w", **pad
        )
        self.bar = ttk.Progressbar(self, length=280, maximum=100, mode="determinate")
        self.bar.grid(row=1, column=0, padx=10, pady=(0, 10))

        self._refresh()

    def _refresh(self) -> None:
        self.bar["value"] = self.app._download_percent or 0
        if not self.app._download_in_flight:
            self.destroy()
            return
        self.after(150, self._refresh)


class InstallReadyWindow(tk.Tk):
    """Shown once the installer has finished downloading. Launching it quits
    the tray first (see TrayApp._launch_installer) -- the installer will
    want to overwrite the running exe."""

    def __init__(self, app: "TrayApp"):
        super().__init__()
        self.app = app
        self.title(APP_NAME)
        self.resizable(False, False)

        pad = {"padx": 10, "pady": 6}
        version = app.update_info.version if app.update_info else ""
        tk.Label(self, text=f"{version} has been downloaded.", anchor="w").grid(
            row=0, column=0, columnspan=2, sticky="w", **pad
        )
        tk.Button(self, text="Later", command=self.destroy).grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        tk.Button(self, text="Launch installer", command=self._launch).grid(
            row=1, column=1, padx=10, pady=10, sticky="ew"
        )

    def _launch(self) -> None:
        self.destroy()
        self.app._launch_installer()


class TrayApp:
    def __init__(self, cfg: dict):
        import pystray

        self.cfg = cfg
        self.pystray = pystray
        self.stop_event = threading.Event()
        self.reconfigure_requested = False
        self.status = "Starting..."
        self.status_window_open = threading.Event()
        self.update_info: "core.UpdateInfo | None" = None
        self._notified_update_version: str | None = None
        self._download_in_flight = False
        self._download_percent: int | None = None
        self._downloaded_installer_path: str | None = None
        self._download_window_open = threading.Event()
        self._install_ready_window_open = threading.Event()

        self.icon = self._build_icon()

    def _build_icon(self):
        return self.pystray.Icon(
            APP_NAME, make_icon_image(), f"{APP_NAME} — {self.status}"[:127], menu=self._build_menu(),
        )

    def _build_menu(self):
        """Rebuilt (not mutated) at every state transition -- found an
        update, started/finished/failed a download -- and reassigned to
        self.icon.menu; pystray supports replacing the whole menu at
        runtime. Not rebuilt on every download-progress tick: the live
        percentage lives in DownloadProgressWindow's progress bar instead,
        so this doesn't need to churn ~100 times over one download."""
        items = [
            self.pystray.MenuItem("Show status", self._show_status_window, default=True),
            self.pystray.MenuItem("Sync now", self._sync_now),
            self.pystray.MenuItem("Check for update", self._check_for_update_now),
            self.pystray.MenuItem("Open data folder", self._open_data_folder),
            self.pystray.MenuItem("Reconfigure…", self._reconfigure),
        ]
        if self.update_info is not None:
            items.append(self._build_update_menu_item())
        items.append(self.pystray.MenuItem("Quit", self._quit))
        return self.pystray.Menu(*items)

    def _build_update_menu_item(self):
        info = self.update_info
        if self._downloaded_installer_path:
            return self.pystray.MenuItem(f"✓ {info.version} downloaded — Launch installer", self._launch_installer)
        if self._download_in_flight:
            return self.pystray.MenuItem(f"⬇ Downloading {info.version}…", self._noop, enabled=False)
        if info.asset_url:
            return self.pystray.MenuItem(f"⬆ Update available: {info.version} — Download", self._start_download)
        # No installer for this platform (e.g. Linux) -- just point at the
        # release page, same as the status window's link.
        return self.pystray.MenuItem(f"⬆ Update available: {info.version}", self._open_release_page)

    def _refresh_menu(self) -> None:
        try:
            self.icon.menu = self._build_menu()
        except Exception:
            log.debug("tray menu refresh failed", exc_info=True)  # not supported on every backend

    def _set_status(self, text: str) -> None:
        self.status = text
        self.icon.title = f"{APP_NAME} — {text}"[:127]  # Windows tooltip length limit

    @core.log_callback_errors("sync run")
    def _run_sync(self) -> None:
        start = time.monotonic()
        log.info("sync run starting")
        result = core.sync_once_isolated(self.cfg)
        elapsed = time.monotonic() - start
        ts = time.strftime("%H:%M:%S")
        self._set_status(f"{'OK' if result.ok else 'error'} @ {ts}: {result.message}")
        log.info("sync run done in %.1fs: ok=%s %s", elapsed, result.ok, result.message)
        if not result.ok:
            try:
                self.icon.notify(result.message, f"{APP_NAME} sync failed")
            except Exception:
                log.debug("tray notify failed", exc_info=True)  # not supported on every backend
        self._check_for_update()

    def _check_for_update(self, force: bool = False, manual: bool = False) -> None:
        """Cheap on most calls: maybe_check_for_update() only hits GitHub
        once a day (see its docstring), so calling this every sync tick is
        fine. `force` bypasses that cache for an on-demand check (the
        "Check for update" menu item). Automatic calls (force=False) notify
        once per newly-seen version; `manual` calls always notify, found or
        not -- someone who clicked "Check for update" wants to know either
        way, not just be told once and never again."""
        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        info = (
            core.maybe_check_for_update(state_dir, min_interval_hours=0) if force
            else core.maybe_check_for_update(state_dir)
        )
        self.update_info = info
        is_new = bool(info) and info.version != self._notified_update_version
        if is_new:
            self._notified_update_version = info.version
            self._downloaded_installer_path = None
        if info and (is_new or manual):
            log.info("update available: %s", info.version)
            try:
                self.icon.notify(f"{info.version} is out — {info.url}", f"{APP_NAME} update available")
            except Exception:
                log.debug("tray notify failed", exc_info=True)  # not supported on every backend
        elif manual and not info:
            try:
                self.icon.notify(f"You're on the latest version ({core.get_version()}).", APP_NAME)
            except Exception:
                log.debug("tray notify failed", exc_info=True)  # not supported on every backend
        self._refresh_menu()

    def _check_for_update_now(self, icon=None, item=None) -> None:
        try:
            threading.Thread(
                target=self._check_for_update, kwargs={"force": True, "manual": True},
                daemon=True, name="check-for-update",
            ).start()
        except Exception:
            log.exception("could not start update check thread")

    # NB: pystray menu callbacks must NOT use @core.log_callback_errors --
    # pystray inspects action.__code__.co_argcount and a (*args, **kwargs)
    # wrapper fails its check with ValueError. Each body is guarded inline
    # instead. They can also run on the same thread as icon.run(), so an
    # uncaught exception here would take the whole tray down.
    def _noop(self, icon=None, item=None) -> None:
        pass

    def _open_release_page(self, icon=None, item=None) -> None:
        try:
            if self.update_info:
                webbrowser.open(self.update_info.url)
        except Exception:
            log.exception("could not open release page")

    def _sync_now(self, icon=None, item=None) -> None:
        try:
            threading.Thread(target=self._run_sync, daemon=True, name="sync-now").start()
        except Exception:
            log.exception("could not start sync thread")

    def _open_data_folder(self, icon=None, item=None) -> None:
        try:
            state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
            state_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(state_dir)  # noqa: S606 — local, user-owned path
        except Exception:
            log.exception("failed to open data folder")

    def _show_status_window(self, icon=None, item=None) -> None:
        try:
            if self.status_window_open.is_set():
                return  # already open — only one at a time
            threading.Thread(target=self._run_status_window, daemon=True, name="status-window").start()
        except Exception:
            log.exception("could not open status window")

    def _run_status_window(self) -> None:
        self.status_window_open.set()
        try:
            StatusWindow(self).mainloop()
        except Exception:
            log.exception("status window crashed")
        finally:
            self.status_window_open.clear()

    def _start_download(self, icon=None, item=None) -> None:
        try:
            info = self.update_info
            if info is None or self._download_in_flight:
                return
            self._download_in_flight = True
            self._download_percent = None
            self._refresh_menu()
            threading.Thread(target=self._download_worker, args=(info,), daemon=True, name="update-download").start()
            if not self._download_window_open.is_set():
                threading.Thread(
                    target=self._run_download_progress_window, daemon=True, name="download-progress",
                ).start()
        except Exception:
            log.exception("could not start update download")

    def _run_download_progress_window(self) -> None:
        self._download_window_open.set()
        try:
            DownloadProgressWindow(self).mainloop()
        except Exception:
            log.exception("download progress window crashed")
        finally:
            self._download_window_open.clear()

    def _download_worker(self, info: "core.UpdateInfo") -> None:
        try:
            state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
            dest = core.download_update_asset(info, state_dir, progress=self._on_download_progress)
            self._on_download_finished(str(dest))
        except Exception as e:
            log.error("update download failed", exc_info=True)
            self._on_download_failed(str(e))

    def _on_download_progress(self, percent: int) -> None:
        self._download_percent = percent  # polled by DownloadProgressWindow

    def _on_download_finished(self, path: str) -> None:
        self._download_in_flight = False
        self._downloaded_installer_path = path
        self._refresh_menu()
        log.info("update installer downloaded: %s", path)
        if not self._install_ready_window_open.is_set():
            threading.Thread(
                target=self._run_install_ready_window, daemon=True, name="install-ready",
            ).start()

    def _run_install_ready_window(self) -> None:
        self._install_ready_window_open.set()
        try:
            InstallReadyWindow(self).mainloop()
        except Exception:
            log.exception("install-ready window crashed")
        finally:
            self._install_ready_window_open.clear()

    def _on_download_failed(self, message: str) -> None:
        self._download_in_flight = False
        self._refresh_menu()
        try:
            self.icon.notify(f"Could not download the update: {message}", f"{APP_NAME} update failed")
        except Exception:
            log.debug("tray notify failed", exc_info=True)  # not supported on every backend

    def _launch_installer(self, icon=None, item=None) -> None:
        try:
            path = self._downloaded_installer_path
            if not path:
                return
            log.info("launching installer: %s", path)
            # The installer needs to overwrite this running exe; quit first
            # so it isn't fighting a locked file.
            subprocess.Popen([path], close_fds=True)
            self._quit()
        except Exception:
            log.exception("could not launch installer")

    def _reconfigure(self, icon=None, item=None) -> None:
        try:
            log.info("reconfigure requested")
            self.reconfigure_requested = True
            self.stop_event.set()
            self.icon.stop()
        except Exception:
            log.exception("reconfigure failed")

    def _quit(self, icon=None, item=None) -> None:
        try:
            log.info("quit requested")
            self.stop_event.set()
            self.icon.stop()
        except Exception:
            log.exception("quit failed")

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._run_sync()
            except Exception:
                # A single bad sync (e.g. one malformed Keep item) must not
                # permanently kill background syncing for every other note.
                log.exception("sync raised unexpectedly; will retry next interval")
            interval_minutes = max(1, int(self.cfg.get("sync_interval_minutes", core.DEFAULT_SYNC_INTERVAL_MINUTES)))
            log.debug("scheduler sleeping %d min until next sync", interval_minutes)
            self.stop_event.wait(interval_minutes * 60)
        log.info("scheduler loop stopped")

    def run(self) -> None:
        threading.Thread(target=self._scheduler_loop, daemon=True, name="scheduler").start()
        log.info("scheduler started; entering tray event loop")
        # The Windows notification-area COM channel can drop (Explorer
        # restart, RDP reconnect, shell hang) -> pystray's mainloop dies
        # with RPC_E_DISCONNECTED (0x80010108). Rebuild the icon and carry
        # on rather than letting the whole app fall over.
        fails = 0
        while not self.stop_event.is_set() and not self.reconfigure_requested:
            try:
                self.icon.run()  # blocks until self.icon.stop() is called
                break  # clean stop (Quit / Reconfigure)
            except Exception:
                fails += 1
                log.exception("tray icon loop crashed (%d)", fails)
                if fails >= 5:
                    raise  # give up; __main__ logs it and the relaunch cap applies
                self.stop_event.wait(3)
                if self.stop_event.is_set() or self.reconfigure_requested:
                    break
                try:
                    self.icon = self._build_icon()
                except Exception:
                    log.exception("could not rebuild tray icon")
                    raise
        log.info("tray event loop exited")


def run_selftest() -> int:
    """Non-interactive sanity check used by CI after a PyInstaller build:
    exercises every bundled dependency (tkinter, pystray, Pillow) without
    opening a visible window, touching config.json, or hitting the
    network. A real build can still look broken to a user even if this
    passes — it only catches packaging failures (missing hidden imports,
    missing DLLs, bad pystray kwargs), not UX issues."""
    import tempfile

    def step(msg):
        print(f"SELFTEST: {msg}", flush=True)

    step("start")
    make_icon_image()
    step("icon built")
    root = tk.Tk()
    root.withdraw()
    root.destroy()
    step("tkinter ok")

    # Builds the real menu (incl. the default/double-click item) without
    # calling icon.run(), which would block waiting for a live tray.
    with tempfile.TemporaryDirectory() as tmp:
        TrayApp({"email": "selftest@example.com", "state_dir": tmp})
    step("tray app built")

    print("SELFTEST OK", flush=True)
    return 0


def main() -> int:
    log.info(
        "KeepSyncTray starting (frozen=%s, dir=%s, platform=%s)",
        getattr(sys, "frozen", False), APP_DIR, sys.platform,
    )
    cfg = load_or_default_config()
    force_setup = False

    while True:
        if force_setup or needs_setup(cfg):
            new_cfg = run_setup_window(cfg)
            if new_cfg is None:
                if force_setup:
                    # User cancelled a Reconfigure… — keep the existing
                    # config and go back to the tray rather than exiting.
                    log.info("reconfigure cancelled; keeping current config")
                    force_setup = False
                    continue
                log.info("setup window closed without finishing; exiting")
                return 0  # user closed initial setup without finishing
            cfg = new_cfg
            force_setup = False

        app = TrayApp(cfg)
        app.run()

        if app.reconfigure_requested:
            cfg = load_or_default_config()
            force_setup = True
            continue
        log.info("KeepSyncTray exiting normally")
        return 0


if __name__ == "__main__":
    if os.environ.get(core.KEEP_WORKER_ENV) == "1":
        raise SystemExit(core.run_keep_worker())
    if os.environ.get("KEEP_SYNC_TRAY_SELFTEST") == "1":
        raise SystemExit(run_selftest())
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise
    except BaseException:  # last-resort surface for a windowed (no console) exe
        log.critical("KeepSyncTray crashed", exc_info=True)
        # Try to come back up on our own; relaunch_after_crash() only returns
        # once it's crashed too many times in a row to keep trying.
        core.relaunch_after_crash(log)
        try:
            messagebox.showerror(
                APP_NAME,
                f"{APP_NAME} keeps crashing and won't restart itself again.\n\n"
                f"See {getattr(log, 'log_path', LOG_PATH)} (and the sibling "
                ".fault.log) for details.",
            )
        except Exception:
            pass
        raise
