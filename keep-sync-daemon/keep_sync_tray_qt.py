#!/usr/bin/env python3
"""
Linux/KDE system-tray app for keep-sync-daemon, built on Qt (PySide6)
instead of pystray/tkinter.

pystray's Linux backend (GTK/AppIndicator) often doesn't integrate
cleanly with KDE Plasma's StatusNotifierItem-based tray — icons can be
missing, mis-themed, or the menu can behave oddly. QSystemTrayIcon talks
StatusNotifierItem natively, so this gives a proper native tray icon
under Plasma. Functionally this is the same app as keep_sync_tray.py
(Windows): a one-time setup window for email + OAuth Token, then a tray
icon that re-syncs on its own timer — pulling Keep's state to state.json,
and pushing any Super Productivity edits queued in pending_changes.json
back to Keep (see keep_sync_core.sync_once).

Packaging into a standalone Linux binary (see README.md):
    pyinstaller --onefile --name KeepSyncTrayQt keep_sync_tray_qt.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
)

import keep_sync_core as core

APP_NAME = "Keep Sync"

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"

AUTOSTART_DESKTOP_PATH = Path.home() / ".config" / "autostart" / "keep-sync-tray.desktop"


def default_config() -> dict:
    return {
        "email": "",
        "state_dir": core.DEFAULT_STATE_DIR,
        "include_archived": False,
        "sync_interval_minutes": core.DEFAULT_SYNC_INTERVAL_MINUTES,
        "run_at_startup": False,
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
    state_dir = core.resolve_state_dir(cfg.get("state_dir", core.DEFAULT_STATE_DIR))
    return not (state_dir / "master_token").exists()


def set_run_at_startup(enable: bool) -> None:
    """Best-effort XDG autostart entry. No-op if the exe path is unknown
    (e.g. running keep_sync_tray_qt.py directly through a bare `python`
    with no reliable relaunch command)."""
    if enable:
        AUTOSTART_DESKTOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        if getattr(sys, "frozen", False):
            exec_cmd = str(sys.executable)
        else:
            exec_cmd = f'{sys.executable} "{Path(__file__).resolve()}"'
        AUTOSTART_DESKTOP_PATH.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Exec={exec_cmd}\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
    else:
        try:
            AUTOSTART_DESKTOP_PATH.unlink()
        except FileNotFoundError:
            pass


def make_icon() -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4285F4"))
    painter.drawEllipse(2, 2, 60, 60)
    painter.setBrush(QColor("white"))
    painter.drawRect(18, 16, 28, 32)
    painter.setPen(QColor("#4285F4"))
    for y in (24, 32, 40):
        painter.drawLine(22, y, 42, y)
    painter.end()
    return QIcon(pixmap)


class SetupDialog(QDialog):
    """Collects email + OAuth Token, exchanges it for a master token, and
    saves config.json. `result_cfg` is the finished config on success, or
    None if the dialog was closed without finishing."""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.result_cfg: dict | None = None

        self.setWindowTitle(f"{APP_NAME} — Setup")
        self.setWindowIcon(make_icon())

        layout = QGridLayout(self)

        info = QLabel(
            "Google Keep has no public API, so this connects the same way\n"
            "the Android app does. To get an OAuth Token: open\n"
            "accounts.google.com/EmbeddedSetup in a browser, sign in, then\n"
            "copy the 'oauth_token' cookie's value (dev tools > Application\n"
            "> Cookies) and paste it below. Full steps are in README.md."
        )
        layout.addWidget(info, 0, 0, 1, 2)

        layout.addWidget(QLabel("Google account email:"), 1, 0)
        self.email_edit = QLineEdit(self.cfg.get("email", ""))
        layout.addWidget(self.email_edit, 1, 1)

        layout.addWidget(QLabel("OAuth Token:"), 2, 0)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.token_edit, 2, 1)

        layout.addWidget(QLabel("Sync every (minutes):"), 3, 0)
        self.interval_spin = QSpinBox()
        self.interval_spin.setMinimum(1)
        self.interval_spin.setMaximum(24 * 60)
        self.interval_spin.setValue(int(self.cfg.get("sync_interval_minutes", core.DEFAULT_SYNC_INTERVAL_MINUTES)))
        layout.addWidget(self.interval_spin, 3, 1)

        self.startup_check = QCheckBox("Start automatically when I log in")
        self.startup_check.setChecked(bool(self.cfg.get("run_at_startup", False)))
        layout.addWidget(self.startup_check, 4, 0, 1, 2)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #c0392b;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, 5, 0, 1, 2)

        self.submit_btn = QPushButton("Save && Start Syncing")
        self.submit_btn.clicked.connect(self._submit)
        layout.addWidget(self.submit_btn, 6, 0, 1, 2)

    def _submit(self) -> None:
        email = self.email_edit.text().strip()
        token = self.token_edit.text().strip()
        interval = self.interval_spin.value()

        if not email or not token:
            self.status_label.setText("Email and OAuth Token are both required.")
            return

        self.submit_btn.setEnabled(False)
        self.status_label.setText("Signing in to Google...")
        QApplication.processEvents()

        try:
            master_token = core.exchange_master_token(email, token)
        except Exception as e:
            self.status_label.setText(f"Sign-in failed: {e}")
            self.submit_btn.setEnabled(True)
            return

        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        core.save_master_token(state_dir, master_token)

        self.cfg.update(
            email=email,
            state_dir=str(state_dir),
            include_archived=self.cfg.get("include_archived", False),
            sync_interval_minutes=interval,
            run_at_startup=self.startup_check.isChecked(),
        )
        core.save_config(CONFIG_PATH, self.cfg)

        try:
            set_run_at_startup(self.startup_check.isChecked())
        except OSError:
            pass  # best-effort; setup still succeeded

        self.result_cfg = self.cfg
        self.accept()


def run_setup_dialog(cfg: dict) -> dict | None:
    dialog = SetupDialog(cfg)
    dialog.exec()
    return dialog.result_cfg


class StatusDialog(QDialog):
    def __init__(self, app: "TrayApp", parent=None):
        super().__init__(parent)
        self.app = app

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())

        layout = QGridLayout(self)
        layout.addWidget(QLabel(f"Account: {app.cfg.get('email', '')}"), 0, 0, 1, 2)

        self.status_label = QLabel(app.status)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label, 1, 0, 1, 2)

        sync_btn = QPushButton("Sync now")
        sync_btn.clicked.connect(app.sync_now)
        layout.addWidget(sync_btn, 2, 0)

        reconfigure_btn = QPushButton("Reconfigure…")
        reconfigure_btn.clicked.connect(self._reconfigure)
        layout.addWidget(reconfigure_btn, 2, 1)

        app.status_changed.connect(self.status_label.setText)

    def _reconfigure(self) -> None:
        self.accept()
        self.app.reconfigure()


class TrayApp(QObject):
    status_changed = Signal(str)

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.status = "Starting..."
        self.stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._status_dialog: StatusDialog | None = None

        self.tray_icon = QSystemTrayIcon(make_icon())
        self.tray_icon.setToolTip(f"{APP_NAME} — starting…")
        self.tray_icon.activated.connect(self._on_activated)

        menu = QMenu()
        menu.addAction("Show status", self.show_status_window)
        menu.addAction("Sync now", self.sync_now)
        menu.addAction("Open data folder", self.open_data_folder)
        menu.addAction("Reconfigure…", self.reconfigure)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray_icon.setContextMenu(menu)

        self.status_changed.connect(self._update_tooltip)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # DoubleClick fires reliably on some Linux desktops but not all —
        # Plasma's StatusNotifierItem protocol doesn't guarantee it, so
        # "Show status" in the menu is the dependable fallback either way.
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_status_window()

    def _update_tooltip(self, text: str) -> None:
        self.tray_icon.setToolTip(f"{APP_NAME} — {text}")

    def show_status_window(self) -> None:
        if self._status_dialog is None:
            self._status_dialog = StatusDialog(self)
            self._status_dialog.finished.connect(self._on_status_dialog_closed)
        self._status_dialog.show()
        self._status_dialog.raise_()
        self._status_dialog.activateWindow()

    def _on_status_dialog_closed(self) -> None:
        self._status_dialog = None

    def _run_sync(self) -> None:
        result = core.sync_once(self.cfg)
        ts = time.strftime("%H:%M:%S")
        text = f"{'OK' if result.ok else 'error'} @ {ts}: {result.message}"
        self.status = text
        self.status_changed.emit(text)
        if not result.ok:
            self.tray_icon.showMessage(f"{APP_NAME} sync failed", result.message, QSystemTrayIcon.MessageIcon.Warning)

    def sync_now(self) -> None:
        threading.Thread(target=self._run_sync, daemon=True).start()

    def open_data_folder(self) -> None:
        state_dir = core.resolve_state_dir(self.cfg.get("state_dir", core.DEFAULT_STATE_DIR))
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(state_dir)])
        except OSError:
            pass

    def start_scheduler(self) -> None:
        self.stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self.stop_event.set()

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            self._run_sync()
            interval_minutes = max(1, int(self.cfg.get("sync_interval_minutes", core.DEFAULT_SYNC_INTERVAL_MINUTES)))
            self.stop_event.wait(interval_minutes * 60)

    def reconfigure(self) -> None:
        self.stop_scheduler()
        new_cfg = run_setup_dialog(self.cfg)
        if new_cfg is not None:
            self.cfg = new_cfg
        self.start_scheduler()

    def quit(self) -> None:
        self.stop_scheduler()
        self.tray_icon.hide()
        QApplication.instance().quit()

    def show(self) -> None:
        self.tray_icon.show()
        self.start_scheduler()


def run_selftest() -> int:
    """Non-interactive sanity check used by CI after a PyInstaller build:
    builds the tray icon/menu (Qt's offscreen platform plugin, no real
    display needed) without starting the event loop, touching config.json,
    or hitting the network. Catches packaging failures (missing hidden
    imports/plugins), not UX issues."""
    import tempfile

    app = QApplication.instance() or QApplication([])
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("SELFTEST: no system tray available in this environment (expected under Qt's offscreen platform) — skipping tray checks")
    make_icon()
    with tempfile.TemporaryDirectory() as tmp:
        tray = TrayApp({"email": "selftest@example.com", "state_dir": tmp})
        tray.tray_icon.setToolTip("selftest")
    print("SELFTEST OK")
    return 0


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    cfg = load_or_default_config()
    if needs_setup(cfg):
        cfg = run_setup_dialog(cfg)
        if cfg is None:
            return 0  # user closed setup without finishing

    tray = TrayApp(cfg)
    tray.show()
    return app.exec()


if __name__ == "__main__":
    if os.environ.get("KEEP_SYNC_TRAY_SELFTEST") == "1":
        raise SystemExit(run_selftest())
    try:
        raise SystemExit(main())
    except Exception as e:
        try:
            QMessageBox.critical(None, APP_NAME, f"Unexpected error, exiting:\n{e}")
        except Exception:
            pass
        raise
