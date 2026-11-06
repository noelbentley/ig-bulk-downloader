"""Bounded anonymous diagnostic using the normal engine; never retries restrictions."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import time
import sys

from desktop.models import DownloadOptions, Job, parse_profile
from desktop.services.control import Cancelled, Control
from desktop.services.errors import classify_error
from desktop.services.performance import RunStatistics, preset_pacing
from desktop.services.public_first import PublicFirstService
from desktop.services.safety import RestrictionGate
from desktop.storage.local import atomic_json, data_directory


class BoundedControl(Control):
    def __init__(self, queue_path, maximum_files, maximum_posts, seconds, statistics):
        super().__init__()
        self.queue_path = queue_path
        self.maximum_files, self.maximum_posts = maximum_files, maximum_posts
        self.deadline = time.monotonic() + seconds
        self.statistics = statistics
        self.posts = 0
        self.last_queue_check = 0

    def checkpoint(self, report=lambda *_: None):
        super().checkpoint(report)
        now = time.monotonic()
        if now >= self.deadline or self.statistics.snapshot()["files"] >= self.maximum_files or self.posts >= self.maximum_posts:
            raise Cancelled()
        if now - self.last_queue_check >= 0.1:
            queue = json.loads(self.queue_path.read_text(encoding="utf-8"))
            if any(job.get("status") not in ("Queued", "Completed", "Cancelled", "Failed", "Skipped") for job in queue):
                raise RuntimeError("The desktop queue became active; this diagnostic stopped.")
            self.last_queue_check = now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--max-posts", type=int, default=12)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--connection-mode", choices=("fresh", "pooled", "alternate"), default="fresh")
    parser.add_argument("--gui", action="store_true", help="Exercise Qt worker, GUI and isolated history in offscreen mode")
    args = parser.parse_args()
    if not 1 <= args.max_files <= 50 or not 1 <= args.max_posts <= 20 or not 10 <= args.seconds <= 300:
        parser.error("Use 1–50 files, 1–20 posts and a 10–300 second budget.")
    profile = parse_profile(args.profile)
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / "build"):
        parser.error("Output must be below this repository's build directory.")
    output.mkdir(parents=True, exist_ok=True)
    state = data_directory()
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    statistics = RunStatistics("Balanced")
    gate = RestrictionGate(settings.get("restriction"))
    if gate.review or gate.remaining() > 0:
        raise RuntimeError("An existing restriction blocks live testing; no requests were made.")
    control = BoundedControl(state / "queue.json", args.max_files, args.max_posts, args.seconds, statistics)
    control.checkpoint()
    from desktop.services.engine import EngineService
    def factory(*args, **kwargs):
        engine = EngineService(*args, **kwargs)
        engine.connection_mode = args_connection_mode
        return engine
    args_connection_mode = args.connection_mode
    service = PublicFirstService(pacing=preset_pacing("Balanced"), engine_factory=factory)
    service.statistics, service.safety = statistics, gate
    service.auth_handler = lambda _: None  # No sessions, credentials or authenticated retries.
    events_before = gate.events
    outcome = "completed"
    def report(kind, value):
        if kind == "items":
            control.posts = value
        if kind == "files":
            atomic_json(output / "progress.json", statistics.snapshot())
            print(f"Saved media: {value}", flush=True)
    try:
        options = DownloadOptions(photos=True, videos=False, reels=False, captions=False, profile_picture=False)
        if args.gui:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtWidgets import QApplication
            from desktop.storage.local import LocalStore
            from desktop.ui.window import MainWindow
            app = QApplication.instance() or QApplication(sys.argv[:1])
            store = LocalStore(output / "state")
            window = MainWindow(store)
            window.manager.safety = gate
            window.manager.engine_factory = lambda sessions, account, **kwargs: PublicFirstService(
                sessions, account, engine_factory=factory, **kwargs)
            window.manager.control_factory = lambda: BoundedControl(state / "queue.json", args.max_files,
                args.max_posts, args.seconds, window.manager.statistics)
            window.manager.authentication_required.connect(lambda request: window.manager.respond_auth(None, request))
            job = window.manager.add(profile, str(output / "media"), options)
            window.show()
            window.start_downloads()
            while window.manager.worker is not None:
                app.processEvents()
                if window.manager.control:
                    window.manager.control.posts = job.items
                time.sleep(0.01)
            statistics = window.manager.statistics
            outcome = "bounded_stop" if job.status == "Cancelled" else job.status.lower()
            window.close()
            app.processEvents()
            store.close()
        else:
            service.run(Job(profile, str(output / "media"), options), control, report)
    except Cancelled:
        outcome = "bounded_stop"
    except Exception as error:
        gate.inspect_exception(error, report, events_before)
        outcome = classify_error(error).kind
    finally:
        service.close()
        result = statistics.finish()
        atomic_json(output / "report.json", dict(result, outcome=outcome))
        if gate.events > events_before or gate.remaining() > 0:
            # Read current preferences again and preserve every unrelated setting.
            current = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            current["restriction"] = gate.state()
            atomic_json(state / "settings.json", current)
        print(json.dumps(dict(result, outcome=outcome), indent=2), flush=True)


if __name__ == "__main__":
    main()
