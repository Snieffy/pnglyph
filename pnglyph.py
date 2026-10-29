import sys
import os
import re
import traceback
import faulthandler
import logging
from datetime import datetime

MAX_KEPT_SESSIONS = 3   # older sessions get dropped so the log can't grow unbounded


def _trim_log(log_path: str, max_sessions: int = MAX_KEPT_SESSIONS):
    """Keep only the last few sessions' worth of log content. Runs
    before the new session's own content is appended, so a prior
    crash's diagnostics survive at least a few more launches instead of
    either growing forever or being wiped the moment the app restarts."""
    if not os.path.exists(log_path):
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return
    # split on the session-start marker, keeping the marker with each chunk
    parts = re.split(r"(?=\n--- session start )", content)
    parts = [p for p in parts if p.strip()]
    trimmed = "".join(parts[-max_sessions:]) if len(parts) > max_sessions else content
    if trimmed != content:
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(trimmed)
        except OSError:
            pass


def _setup_diagnostics():
    """Best-effort crash diagnostics: a log file next to the script that
    survives even a hard native crash (segfault-level -- the kind that
    exits with no Python traceback at all, which faulthandler is
    specifically designed to catch, unlike a normal try/except)."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = None

    log_path = os.path.join(log_dir, "pnglyph.log") if log_dir else None
    log_file = None
    if log_path:
        try:
            _trim_log(log_path, max_sessions=MAX_KEPT_SESSIONS - 1)   # room for the new session about to start
            log_file = open(log_path, "a", buffering=1, encoding="utf-8")
            log_file.write(f"\n--- session start {datetime.now().isoformat()} ---\n")
            faulthandler.enable(file=log_file, all_threads=True)
        except OSError:
            log_file = None

    file_handler = logging.FileHandler(log_path, encoding="utf-8") if log_path else None
    console_handler = logging.StreamHandler()
    # Routine info (like this startup message) only goes to the file --
    # the console only needs to speak up for warnings and worse, so it
    # doesn't clutter the output window under normal operation.
    console_handler.setLevel(logging.WARNING)

    handlers = ([file_handler] if file_handler else []) + [console_handler]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

    def excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("pnglyph").critical(
            "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook
    return log_path


def main():
    log_path = _setup_diagnostics()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        print("Could not import PySide6. Is it installed in this environment?")
        print(f"  {e}")
        print("Try: pip install -r requirements.txt")
        input("Press Enter to exit...")
        sys.exit(1)

    app = QApplication(sys.argv)
    # PNGlyph's capture-session flow deliberately hides the main window
    # and shows/hides the capture window and overlay independently of
    # it -- which is fundamentally incompatible with
    # Qt's default "quit once the last visible top-level window closes"
    # behavior. Without this, whichever of those capture windows
    # happens to be the only one currently visible (a routine state
    # during a session) would silently take the whole application down
    # with it when closed. MainWindow.closeEvent now quits explicitly
    # on its own confirmed-close path, so real shutdown still works.
    app.setQuitOnLastWindowClosed(False)

    try:
        from gui.style import get_stylesheet
        app.setStyleSheet(get_stylesheet())
    except Exception:
        pass  # styling is cosmetic; never block startup on it

    try:
        from gui.main_window import MainWindow
        win = MainWindow()
    except Exception:
        print("PNGlyph failed to start. Full error below:\n")
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)

    if log_path:
        logging.getLogger("pnglyph").info("Diagnostics log active at %s", log_path)

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
