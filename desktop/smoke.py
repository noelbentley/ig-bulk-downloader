"""Offline developer smoke test using the real Qt event loop and rendered widgets."""
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from desktop.storage.local import LocalStore
from desktop.ui.window import MainWindow
from desktop.ui.authentication import AuthenticationDialog
from desktop.services.performance import RunStatistics


def run_smoke(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # This diagnostic must remain entirely offline, including in the frozen exe.
    def blocked(*args, **kwargs):
        raise RuntimeError("Network is disabled in the GUI smoke test")
    socket.socket.connect = blocked
    app = QApplication.instance() or QApplication(sys.argv[:1])
    failures = []
    with TemporaryDirectory(prefix="ig-desktop-smoke-") as temporary:
        store = LocalStore(temporary)
        window = MainWindow(store)
        assert window.auth_status.text() == "Public Mode — No Login"
        assert not window.use_session.isChecked()
        assert "metadata" not in window.options
        window.show()

        def exercise():
            try:
                window.destination.setText(str(Path(temporary) / "archive"))
                for name in ("example_one", "example_two"):
                    window.profile.setText(name)
                    window.add_profile()
                assert window.queue_table.rowCount() == 2
                for theme in ("Dark", "Light"):
                    window.theme.setCurrentText(theme)
                    for index, name in enumerate(("dashboard", "options", "accounts", "history", "settings")):
                        window.tabs.setCurrentIndex(index)
                        app.processEvents()
                        if not window.grab().save(str(output / f"{theme.lower()}-{name}.png")):
                            raise OSError("Could not save rendered widget preview")
                    dialog = AuthenticationDialog({"username": "example_one", "content": "Stories",
                        "reason": "Instagram requires authentication for Stories. The selected account must already have access.",
                        "required": True}, ["offline_example"], window)
                    dialog.show()
                    app.processEvents()
                    if not dialog.grab().save(str(output / f"{theme.lower()}-authentication.png")):
                        raise OSError("Could not save authentication prompt preview")
                    dialog.dismiss()
                    sample = RunStatistics().snapshot()
                    sample.update(files=20, elapsed_seconds=22.24, files_per_minute=53.95, http_requests=26,
                                  http_requests_per_file=1.3, native_delay_seconds=12.87,
                                  average_transfer_seconds=0.305, average_inter_file_gap_seconds=0.122,
                                  optimized_discovery_windows=1, optimized_rich_posts=4,
                                  optimized_saved_files=20, peak_transfers=3, media_workers=3)
                    window.finish_performance(sample)
                    window.tabs.setCurrentIndex(4)
                    app.processEvents()
                    # The nested form and scroll content resize on successive
                    # layout events after the summary grows to multiple lines.
                    app.processEvents()
                    assert window.export_report.y() >= window.last_performance.geometry().bottom()
                    assert window.pacing_group.y() >= window.performance_group.geometry().bottom()
                    if not window.grab().save(str(output / f"{theme.lower()}-diagnostic-summary.png")):
                        raise OSError("Could not save diagnostic summary preview")
                    window.last_performance.setText("No completed queue run yet.")
                    window.last_performance.setFixedHeight(window.fontMetrics().lineSpacing() + 8)
                    window.optimized_enabled.setChecked(True)
                    window.optimized_workers.setValue(5)
                    app.processEvents()
                    app.processEvents()
                    assert window.optimized_workers.isEnabled()
                    if not window.grab().save(str(output / f"{theme.lower()}-optimized-settings.png")):
                        raise OSError("Could not save optimized settings preview")
                    window.optimized_enabled.setChecked(False)
                    window.optimized_workers.setValue(3)
                store.save_queue(window.manager.jobs)
                assert len(store.load_queue()) == 2
                assert store.settings["pacing"]["batch_min"] == 3
                assert store.settings["performance"]["profile"] == "Balanced"
                assert store.settings["pacing"]["enabled"] is False
                assert store.settings["pacing"]["long_rest_enabled"] is False
                assert not store.settings["pipeline"]["enabled"]
                # Exercise the frozen scheduler, Requests hooks and native iterator,
                # rather than checking only that the new controls can be rendered.
                from desktop.optimized_benchmark import run_case
                native, native_files = run_case(Path(temporary) / "native", posts=2,
                    api_latency=0, media_latency=0.005)
                optimized, optimized_files = run_case(Path(temporary) / "optimized", True, 2, True, 2, 0, 0.005)
                assert native_files == optimized_files and optimized["files"] == 4
                assert native["http_requests"] == 7 and optimized["http_requests"] == 5
                assert optimized["peak_transfers"] == 2
                result = {"ok": True, "profiles": 2, "views": 16, "network": "disabled", "mode": "public",
                          "frozen": bool(getattr(sys, "frozen", False)),
                          "engine_check": {"files": 4, "identical_bytes": True, "native_requests": 7,
                                           "optimized_requests": 5, "peak_workers": 2}}
                (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            except Exception:
                failures.append(traceback.format_exc())
                (output / "error.txt").write_text(failures[0], encoding="utf-8")
            finally:
                window.close()
                app.quit()

        QTimer.singleShot(300, exercise)
        app.exec()
        store.close()
    return 1 if failures else 0
