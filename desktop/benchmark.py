"""Offline native-engine benchmark. Network is blocked; pacing waits are simulated."""
import argparse
import io
import json
from pathlib import Path
import random
import socket
import statistics
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
from datetime import datetime

import instaloader
import requests
from PySide6.QtWidgets import QApplication

from desktop.models import DownloadOptions, Job
from desktop.services.control import Control
from desktop.services.engine import EngineService
from desktop.services.pacing import Pacer, PacingConfig
from desktop.services.public_first import PublicFirstService
from desktop.storage.local import LocalStore
from desktop.ui.window import MainWindow


class SimulatedWait(Control):
    def __init__(self):
        super().__init__()
        self.pacing_seconds = 0.0

    def wait(self, seconds, kind, report, remaining_changed=lambda *_: None):
        self.pacing_seconds += seconds
        remaining_changed(0)


def fixture_posts(count):
    for index in range(count):
        video = index % 5 == 1
        carousel = index % 5 == 0
        url = "https://offline.invalid/media.mp4" if video else "https://offline.invalid/media.jpg"
        nodes = [SimpleNamespace(is_video=False, video_url=None, display_url=url) for _ in range(3)]
        yield SimpleNamespace(shortcode=f"media_{index:05}", typename="GraphSidecar" if carousel else "GraphVideo" if video else "GraphImage",
                              is_video=video, date_local=datetime(2026, 1, 1), caption="Offline benchmark caption",
                              url=url, video_url=url, mediacount=3 if carousel else 1,
                              get_is_videos=lambda: [False] * 3,
                              get_sidecar_nodes=lambda *_, nodes=nodes: iter(nodes))


def run_case(app, root, count, paced, gui):
    counters = {"files": 0, "events": 0, "raw_write_seconds": 0.0, "gui_callback_seconds": 0.0, "refreshes": 0}
    payloads = {"image/jpeg": b"x" * (256 * 1024), "video/mp4": b"v" * (1024 * 1024)}
    def loader_factory(**kwargs):
        loader = instaloader.Instaloader(**kwargs)
        loader.filename_pattern = "{shortcode}"
        def get_raw(url):
            response = requests.Response()
            response.status_code = 200
            content_type = "video/mp4" if url.endswith(".mp4") else "image/jpeg"
            response.headers["Content-Type"] = content_type
            response.raw = io.BytesIO(payloads[content_type])
            return response
        loader.context.get_raw = get_raw
        original_write = loader.context.write_raw
        def write(*args, **kwargs):
            started = time.perf_counter()
            gui_before = counters["gui_callback_seconds"]
            try:
                return original_write(*args, **kwargs)
            finally:
                counters["raw_write_seconds"] += max(0, time.perf_counter() - started -
                                                     (counters["gui_callback_seconds"] - gui_before))
        loader.context.write_raw = write
        return loader
    profile = SimpleNamespace(is_private=False, get_posts=lambda: fixture_posts(count))
    def engine_factory(sessions, account, **kwargs):
        return EngineService(sessions, account, loader_factory=loader_factory, profile_factory=lambda *_: profile, **kwargs)
    config = PacingConfig(enabled=paced)
    service = PublicFirstService(pacing=config, engine_factory=engine_factory)
    # The pre-change baseline did not have instrumentation. Later runs include its overhead.
    try:
        from desktop.services.performance import RunStatistics
        service.statistics = RunStatistics("Conservative" if paced else "Balanced")
    except ImportError:
        pass
    service.pacer = Pacer(config, random.Random(42))
    control = SimulatedWait()
    job = Job("benchmark", str(root / "archive"), DownloadOptions(profile_picture=False))
    store = window = None
    if gui:
        store = LocalStore(root / "state")
        window = MainWindow(store)
        window.manager.jobs = [job]
        window.manager.current = job
        window.manager.running = True
        window.show()
        app.processEvents()
        # Count actual refreshes, including timer-driven refreshes in the optimized build.
        original_refresh = window.refresh
        window.manager.changed.disconnect(window.refresh)
        def refresh():
            counters["refreshes"] += 1
            original_refresh()
        window.manager.changed.connect(refresh)
    def report(kind, value):
        counters["events"] += 1
        if kind == "files":
            counters["files"] = value
        if window:
            started = time.perf_counter()
            window.manager._event(kind, value)
            app.processEvents()
            counters["gui_callback_seconds"] += time.perf_counter() - started
    started = time.perf_counter()
    try:
        service.run(job, control, report)
        if window and hasattr(window.manager, "flush_ui"):
            window.manager.flush_ui()
        elapsed = time.perf_counter() - started
    finally:
        service.close()
        if window:
            window.manager.running = False
            window.manager.current = None
            window.close()
            app.processEvents()
            store.close()
    projected = elapsed + control.pacing_seconds
    return dict(counters, processing_seconds=elapsed, simulated_pacing_seconds=control.pacing_seconds,
                projected_elapsed_seconds=projected, projected_files_per_minute=counters["files"] * 60 / projected,
                other_processing_seconds=max(0, elapsed - counters["raw_write_seconds"] - counters["gui_callback_seconds"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="after")
    parser.add_argument("--posts", type=int, default=100)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.posts <= 1000 or not 1 <= args.repetitions <= 10:
        parser.error("Use 1–1000 posts and 1–10 repetitions.")
    def blocked(*args, **kwargs):
        raise RuntimeError("All network access is disabled for the benchmark")
    socket.socket.connect = blocked
    app = QApplication.instance() or QApplication(sys.argv[:1])
    root = Path(__file__).resolve().parents[1]
    (root / "build").mkdir(exist_ok=True)
    result = {"phase": args.phase, "network": "disabled", "posts": args.posts,
              "repetitions": args.repetitions, "payloads": "256 KiB JPG and 1 MiB MP4 synthetic bytes; every fifth post is a 3-file carousel",
              "timing": "Real local processing; pacing seconds are modeled without sleeping. No live-throughput claim.", "cases": {}}
    for name, paced, gui in (("single_worker_no_pacing", False, False),
                              ("single_worker_conservative", True, False),
                              ("single_worker_with_gui", False, True)):
        samples = []
        for _ in range(args.repetitions):
            with TemporaryDirectory(prefix="bench-", dir=root / "build") as temporary:
                samples.append(run_case(app, Path(temporary), args.posts, paced, gui))
        result["cases"][name] = {"median": {key: statistics.median(sample[key] for sample in samples) for key in samples[0]}, "samples": samples}
        print(name + ": " + json.dumps(result["cases"][name]["median"]), flush=True)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"phase": args.phase, "cases": {key: value["median"] for key, value in result["cases"].items()}}, indent=2))


if __name__ == "__main__":
    main()
