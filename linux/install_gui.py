#!/usr/bin/env python3
"""GUI installer for Android Photo Transfer (Linux port).

Launched by double-clicking the "Install Android Photo Transfer" desktop
file. Runs the SAME steps as install.sh -- every step lives in
install_lib.py; this file is only the window around it.

If PySide6 is missing for the Python that starts this file, the installer
first provisions a PySide6-capable Python through install_lib (apt, else a
private venv) and re-opens itself with that Python, so the window still
appears. If even that fails, it falls back to a plain message box (tkinter)
or a printed message.

Test hook (sandbox smoke test only): APT_INSTALL_GUI_TEST=1 auto-runs the
install and quits with the install's exit code.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import install_lib  # noqa: E402

BOOTSTRAPPED = "--bootstrapped"


def _plain_error(message: str) -> None:
    """Last-resort error display when no GUI toolkit is available."""
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Android Photo Transfer installer", message)
        root.destroy()
        return
    except Exception:
        pass
    print(f"FAILED: {message}", file=sys.stderr)


def _ensure_gui_python() -> bool:
    """Make sure the running Python can show this window.

    Returns True when PySide6 imports (or this process is already the
    re-exec after provisioning). Re-execs into a provisioned Python when
    needed; returns False only when no GUI is possible at all.
    """
    try:
        import PySide6  # noqa: F401
        return True
    except ImportError:
        pass
    if BOOTSTRAPPED in sys.argv:
        return False
    install_dir = install_lib.default_install_dir()
    python = install_lib.find_usable_python(install_dir)
    if python is None:
        python = install_lib.provision_python(install_dir)
    if python is None:
        return False
    argv = [python, os.path.abspath(__file__), BOOTSTRAPPED] + \
        [a for a in sys.argv[1:] if a != BOOTSTRAPPED]
    try:
        os.execv(python, argv)
    except OSError:
        pass
    return subprocess.run(argv).returncode == 0


def main() -> int:
    if not _ensure_gui_python():
        _plain_error(
            "PySide6 could not be installed, so the installer window cannot open.\n\n"
            "Try in a terminal:\n"
            "  sudo apt install python3-pyside6\n"
            "  bash install.sh")
        return 1

    from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
    from PySide6.QtWidgets import (
        QApplication, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
        QWidget,
    )

    class Worker(QObject):
        line = Signal(str)
        finished = Signal(int)

        def run(self):
            code = install_lib.install(log_fn=self.line.emit)
            self.finished.emit(code)

    class InstallerWindow(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Install Android Photo Transfer")
            self.setMinimumSize(560, 420)
            self._thread = None
            self._worker = None
            self._result = None

            layout = QVBoxLayout(self)
            intro = QLabel(
                "This installs Android Photo Transfer on this PC.\n"
                "It copies the app to your home folder and adds it to the "
                "applications menu. Nothing is removed.")
            intro.setWordWrap(True)
            layout.addWidget(intro)
            self.status = QLabel("Ready.")
            layout.addWidget(self.status)
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            layout.addWidget(self.log_view, 1)
            self.install_button = QPushButton("Install")
            self.install_button.clicked.connect(self.start_install)
            layout.addWidget(self.install_button)
            self.close_button = QPushButton("Close")
            self.close_button.setEnabled(False)
            self.close_button.clicked.connect(self.close)
            layout.addWidget(self.close_button)

        def start_install(self):
            self.install_button.setEnabled(False)
            self.status.setText("Installing...")
            self._thread = QThread(self)
            self._worker = Worker()
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.run)
            self._worker.line.connect(self._append_line)
            self._worker.finished.connect(self._on_finished)
            self._worker.finished.connect(self._thread.quit)
            self._thread.start()

        def _append_line(self, line):
            self.log_view.appendPlainText(line)

        def _on_finished(self, code):
            self._result = code
            if code == 0:
                self.status.setText(
                    "Installed. Find Android Photo Transfer in your applications menu.")
            else:
                self.status.setText(
                    "The install did not finish. See the messages above.")
            self.close_button.setEnabled(True)
            if os.environ.get("APT_INSTALL_GUI_TEST") == "1":
                QApplication.instance().exit(code)

    test_mode = os.environ.get("APT_INSTALL_GUI_TEST") == "1"
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Android Photo Transfer installer")
    window = InstallerWindow()
    window.show()
    # Auto-start: the user already double-clicked Install; one more click
    # is one too many. The button stays for re-runs after a failure.
    QTimer.singleShot(300, window.start_install)
    if test_mode:
        # Safety net: never hang the smoke test.
        QTimer.singleShot(120_000, lambda: app.exit(2))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
