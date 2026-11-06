import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop.storage.local import LocalStore, data_directory
from desktop.ui.window import MainWindow


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        from desktop.smoke import run_smoke
        return run_smoke(sys.argv[2])
    app = QApplication(sys.argv)
    app.setApplicationName("IG Bulk Downloader")
    app.setOrganizationName("IG Bulk Downloader")
    app.setFont(QFont("Inter", 10))
    root = data_directory()
    root.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(root / "app.lock"))
    if not lock.tryLock(100):
        QMessageBox.information(None, "IG Bulk Downloader", "The downloader is already running for this Windows account.")
        return 0
    store = None
    try:
        store = LocalStore(root)
        window = MainWindow(store)
        window.show()
        return app.exec()
    except Exception as error:
        QMessageBox.critical(None, "IG Bulk Downloader", f"Could not open local application data:\n{error}")
        return 1
    finally:
        if store:
            store.close()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
