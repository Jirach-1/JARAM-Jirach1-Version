import os
import sys
import traceback
from typing import Optional
from PySide6.QtCore import Qt, QTimer, QRect, QSize
from PySide6.QtGui import QIcon, QPixmap, QColor, QFont, QPainter, QLinearGradient
from PySide6.QtWidgets import (
    QApplication,
    QSplashScreen,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QMessageBox,
)
from multiscope import APP_FOOTER


# Qt6 enum shim (matches gui.py behavior)
if hasattr(Qt, "AlignmentFlag"):
    if not hasattr(Qt, "AlignLeft"):
        Qt.AlignLeft = Qt.AlignmentFlag.AlignLeft
    if not hasattr(Qt, "AlignRight"):
        Qt.AlignRight = Qt.AlignmentFlag.AlignRight
    if not hasattr(Qt, "AlignTop"):
        Qt.AlignTop = Qt.AlignmentFlag.AlignTop
    if not hasattr(Qt, "AlignBottom"):
        Qt.AlignBottom = Qt.AlignmentFlag.AlignBottom
    if not hasattr(Qt, "AlignHCenter"):
        Qt.AlignHCenter = Qt.AlignmentFlag.AlignHCenter
    if not hasattr(Qt, "AlignVCenter"):
        Qt.AlignVCenter = Qt.AlignmentFlag.AlignVCenter
    if not hasattr(Qt, "AlignCenter"):
        Qt.AlignCenter = Qt.AlignmentFlag.AlignCenter


# Lightweight palette for the splash; mirrors ModernStyle defaults
BACKGROUND = "#1e1e1e"
PRIMARY = "#6366f1"
SECONDARY = "#10b981"
TEXT_PRIMARY = "#ffffff"
TEXT_SECONDARY = "#a1a1aa"


class _RaisedMessageSplash(QSplashScreen):
    def __init__(self, pixmap: QPixmap, message_y_offset: int = -14):
        super().__init__(pixmap)
        self._message_y_offset = message_y_offset

    def drawContents(self, painter: QPainter) -> None:
        painter.save()
        painter.translate(0, self._message_y_offset)
        super().drawContents(painter)
        painter.restore()


def _find_icon_path() -> Optional[str]:
    icon_path = "JARAM.ico"
    if os.path.exists(icon_path):
        return icon_path

    if hasattr(sys, "_MEIPASS"):
        icon_path = os.path.join(sys._MEIPASS, "JARAM.ico")
        if os.path.exists(icon_path):
            return icon_path

    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(script_dir, "JARAM.ico")
    if os.path.exists(icon_path):
        return icon_path

    return None


def _create_loading_screen(icon_path: Optional[str]) -> QSplashScreen:
    width, height = 520, 320
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(BACKGROUND))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    accent = QLinearGradient(0, 0, width, height)
    accent.setColorAt(0.0, QColor(PRIMARY))
    accent.setColorAt(1.0, QColor(SECONDARY))
    painter.fillRect(QRect(8, 8, width - 16, height - 16), accent)
    painter.fillRect(QRect(20, 20, width - 40, height - 40), QColor(0, 0, 0, 170))

    if icon_path and os.path.exists(icon_path):
        icon = QIcon(icon_path)
        logo = icon.pixmap(QSize(128, 128))  # use largest available; let Qt pick best frame
        # account for device pixel ratio so positioning stays sharp on HiDPI
        dpr = logo.devicePixelRatio()
        draw_w = int(logo.width() / (dpr or 1))
        draw_h = int(logo.height() / (dpr or 1))
        painter.drawPixmap((width - draw_w) // 2, 38, logo)

    painter.setPen(QColor(TEXT_PRIMARY))
    title_font = QFont("Segoe UI", 22)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.drawText(QRect(0, height - 150, width, 40), Qt.AlignHCenter, "J.JARAM is starting")

    painter.setPen(QColor(TEXT_SECONDARY))
    painter.setFont(QFont("Segoe UI", 11))
    painter.drawText(QRect(0, height - 110, width, 40), Qt.AlignHCenter, "Loading  UI and Services...")
    painter.end()

    splash = _RaisedMessageSplash(pixmap, message_y_offset=-14)
    splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    splash.setStyleSheet(f"color: {TEXT_PRIMARY};")
    splash.showMessage("Preparing workspace...", Qt.AlignBottom | Qt.AlignHCenter, QColor(TEXT_SECONDARY))
    return splash


def _maybe_prompt_cookie_unlock(app: QApplication, parent=None) -> None:
    try:
        from gui import ConfigManager
    except Exception:
        return

    try:
        cfg = ConfigManager()
    except Exception:
        return

    if not cfg.cookie_encryption_enabled():
        return
    if ConfigManager.is_cookie_unlocked():
        return
    if not cfg.cookie_encryption_available():
        return

    dlg = QDialog(parent)
    dlg.setWindowTitle("Unlock Cookies")
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)

    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel(
        "Cookies are encrypted.\n"
        "Enter your password to unlock now, or skip to unlock later in the app."
    ))
    pwd = QLineEdit()
    pwd.setEchoMode(QLineEdit.EchoMode.Password)
    layout.addWidget(pwd)

    buttons = QDialogButtonBox()
    unlock_btn = buttons.addButton("Unlock", QDialogButtonBox.ButtonRole.AcceptRole)
    skip_btn = buttons.addButton("Skip", QDialogButtonBox.ButtonRole.RejectRole)
    layout.addWidget(buttons)

    def _attempt_unlock():
        if not pwd.text().strip():
            QMessageBox.warning(dlg, "Password Required", "Please enter a password.")
            return
        if cfg.unlock_cookie_encryption(pwd.text()):
            try:
                if parent is not None and hasattr(parent, "_on_cookie_encryption_unlocked"):
                    parent._on_cookie_encryption_unlocked()
            except Exception:
                pass
            dlg.accept()
        else:
            err = cfg.get_cookie_error() or "Incorrect password."
            box = QMessageBox(dlg)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Unlock Failed")
            box.setText(err)
            try_again = box.addButton("Try Again", QMessageBox.ButtonRole.AcceptRole)
            skip_now = box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(try_again)
            box.exec()
            clicked = box.clickedButton()
            if clicked == skip_now:
                dlg.reject()

    unlock_btn.clicked.connect(_attempt_unlock)
    skip_btn.clicked.connect(dlg.reject)

    pwd.setFocus()
    dlg.exec()


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--merchant-fix-helper":
        from merchant_fix import run_helper_from_argv

        raise SystemExit(run_helper_from_argv(sys.argv[1:]))

    # Silence noisy Windows DPI-awareness warning:
    # "qt.qpa.window: SetProcessDpiAwarenessContext() failed: Access is denied."
    try:
        from PySide6.QtCore import QLoggingCategory

        QLoggingCategory.setFilterRules("qt.qpa.window.warning=false")
    except Exception:
        pass

    # Make sure Qt uses the highest-res icons on HiDPI displays
    try:
        from PySide6.QtCore import QCoreApplication

        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("J.JARAM")
    app.setApplicationVersion(APP_FOOTER)
    app.setOrganizationName("cresqnt")
    # Qt 6 ships a Windows 11 style that can change widget visuals. Force the
    # Windows 10-era style for consistent UI across OS versions.
    try:
        from PySide6.QtWidgets import QStyleFactory

        if "windowsvista" in {str(k).lower() for k in QStyleFactory.keys()}:
            style = QStyleFactory.create("windowsvista")
            if style is not None:
                app.setStyle(style)
    except Exception:
        pass

    try:
        from qt_event_filters import NoEnterInPopupsFilter

        app._no_enter_in_popups_filter = NoEnterInPopupsFilter(app)
        app.installEventFilter(app._no_enter_in_popups_filter)
    except Exception:
        pass

    icon_path = _find_icon_path()
    if icon_path and os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    splash = _create_loading_screen(icon_path)
    splash.show()
    app.processEvents()  # paint the splash immediately

    def build_window():
        # Delay heavy imports until after the splash appears
        try:
            from gui import RobloxManagerGUI

            splash.showMessage("Loading interface...", Qt.AlignBottom | Qt.AlignHCenter, QColor(TEXT_SECONDARY))
            window = RobloxManagerGUI()

            splash.showMessage("Starting services...", Qt.AlignBottom | Qt.AlignHCenter, QColor(TEXT_SECONDARY))
            app.processEvents()

            window.show()
            splash.finish(window)
            QTimer.singleShot(0, lambda: _maybe_prompt_cookie_unlock(app, window))
        except Exception:
            tb = traceback.format_exc()
            log_path = None
            try:
                appdata = os.environ.get("APPDATA", "")
                if appdata:
                    log_dir = os.path.join(appdata, "JARAM")
                    os.makedirs(log_dir, exist_ok=True)
                    log_path = os.path.join(log_dir, "launcher_crash.log")
                else:
                    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher_crash.log")
                with open(log_path, "w", encoding="utf-8") as f:
                    f.write(tb)
            except Exception:
                log_path = None

            try:
                splash.close()
            except Exception:
                pass

            msg = QMessageBox()
            msg.setIcon(QMessageBox.Icon.Critical)
            msg.setWindowTitle("J.JARAM failed to start")
            msg.setText("An error occurred while starting J.JARAM.")
            if log_path:
                msg.setInformativeText(f"Crash log written to:\n{log_path}")
            msg.setDetailedText(tb)
            msg.exec()
            raise

    QTimer.singleShot(0, build_window)
    sys.exit(app.exec())


if __name__ == "__main__":
    # Needed for frozen executables (Nuitka/PyInstaller) that use multiprocessing.
    from multiprocessing import freeze_support

    freeze_support()
    main()
