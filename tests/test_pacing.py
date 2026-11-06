from dataclasses import asdict, replace
from datetime import datetime
import io
from pathlib import Path
import threading
from types import SimpleNamespace

import instaloader
import pytest

from desktop.models import DownloadOptions, Job
from desktop.services.control import Cancelled, Control
from desktop.services.engine import EngineService
from desktop.services.pacing import Pacer, PacingConfig
from desktop.services.queue import QueueManager
from desktop.storage.local import LocalStore
from desktop.storage.sessions import SessionStore
from desktop.ui.window import MainWindow


class Draws:
    def __init__(self, values):
        self.values = iter(values)
        self.bounds = []

    def randint(self, low, high):
        self.bounds.append((low, high))
        value = next(self.values)
        assert low <= value <= high
        return value


class NoWait(Control):
    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, seconds, kind, report, remaining_changed=lambda *_: None):
        self.checkpoint(report)
        self.waits.append((seconds, kind))
        report("pacing", {"kind": kind, "remaining": seconds, "paused": False})
        remaining_changed(0)


@pytest.mark.parametrize("batch,pause", [(3, 15), (8, 45)])
def test_independent_batch_and_duration_draws_at_both_boundaries(batch, pause):
    draws = Draws([batch, 40, pause, batch])
    pacer = Pacer(PacingConfig(), draws)
    control = NoWait()
    for _ in range(batch - 1):
        pacer.completed_file(control, lambda *_: None)
    assert control.waits == []
    pacer.completed_file(control, lambda *_: None)
    assert control.waits == [(pause, "Batch pause")]
    assert draws.bounds == [(3, 8), (40, 60), (15, 45), (3, 8)]
    assert pacer.batch_count == 0
    assert pacer.long_count == batch


def test_batch_is_redrawn_instead_of_reusing_previous_target():
    draws = Draws([3, 15, 8, 45, 3])
    pacer = Pacer(PacingConfig(long_rest_enabled=False), draws)
    control = NoWait()
    for _ in range(11):
        pacer.completed_file(control, lambda *_: None)
    assert control.waits == [(15, "Batch pause"), (45, "Batch pause")]
    assert pacer.batch_target == 3


@pytest.mark.parametrize("interval,duration,next_interval", [(40, 120, 60), (60, 300, 40)])
def test_long_rest_trigger_reset_and_independent_redraw(interval, duration, next_interval):
    config = PacingConfig(batch_min=100, batch_max=100)
    draws = Draws([100, interval, duration, next_interval, 100])
    pacer = Pacer(config, draws)
    control = NoWait()
    for _ in range(interval):
        pacer.completed_file(control, lambda *_: None)
    assert control.waits == [(duration, "Long rest")]
    assert pacer.long_count == pacer.batch_count == 0
    assert pacer.long_target == next_interval
    assert draws.bounds == [(100, 100), (40, 60), (120, 300), (40, 60), (100, 100)]


def test_overlapping_thresholds_take_only_long_rest():
    config = PacingConfig(batch_min=3, batch_max=3, long_interval_min=3, long_interval_max=3)
    draws = Draws([3, 3, 120, 3, 3])
    pacer = Pacer(config, draws)
    control = NoWait()
    for _ in range(3):
        pacer.completed_file(control, lambda *_: None)
    assert control.waits == [(120, "Long rest")]
    assert (15, 45) not in draws.bounds


def test_disabled_pacing_has_no_draws_or_waits():
    draws = Draws([])
    pacer = Pacer(PacingConfig(enabled=False), draws)
    control = NoWait()
    for _ in range(100):
        pacer.completed_file(control, lambda *_: None)
    assert not draws.bounds and not control.waits


@pytest.mark.parametrize("changes", [
    {"batch_min": 0}, {"batch_min": 9}, {"pause_min": -1}, {"pause_max": 0},
    {"pause_max": 86401}, {"long_interval_max": 39}, {"long_pause_max": 119},
    {"long_interval_max": 100001}, {"batch_min": 3.0}, {"batch_max": True},
    {"enabled": "yes"}, {"long_rest_enabled": 1},
])
def test_invalid_ranges_rejected(changes):
    with pytest.raises(ValueError):
        replace(PacingConfig(), **changes)


def test_fake_clock_countdown_preserves_exact_remaining_time_while_paused(monkeypatch):
    now = [0.0]
    control = Control(clock=lambda: now[0])
    remainders, countdowns = [], []
    stage = [0]

    def step(timeout):
        if stage[0] == 0:
            now[0] += 2
            control.set_paused(True)
        elif stage[0] == 1:
            now[0] += 1000  # No countdown time may be consumed during this pause.
        elif stage[0] == 2:
            control.set_paused(False)
        else:
            now[0] += 3
        stage[0] += 1

    monkeypatch.setattr(control._wake, "wait", step)
    control.wait(5, "Batch pause", lambda kind, value: countdowns.append((kind, value)), remainders.append)
    assert remainders == [5, 3, 3, 3, 0]
    ticks = [value for kind, value in countdowns if kind == "pacing"]
    assert ticks == [{"kind": "Batch pause", "remaining": 5, "paused": False},
                     {"kind": "Batch pause", "remaining": 3, "paused": True},
                     {"kind": "Batch pause", "remaining": 3, "paused": False}]


@pytest.mark.parametrize("paused", [False, True])
def test_cancellation_wakes_countdown_immediately(paused):
    control = Control()
    if paused:
        control.set_paused(True)
    entered, cancelled = threading.Event(), threading.Event()
    def run():
        try:
            control.wait(300, "Long rest", lambda *_: entered.set())
        except Cancelled:
            cancelled.set()
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        control.cancel()
        assert cancelled.wait(0.5)
    finally:
        control.cancel()
        thread.join(1)


def test_cancelled_rest_keeps_remainder_for_next_profile():
    config = PacingConfig(batch_min=1, batch_max=1, long_rest_enabled=False)
    pacer = Pacer(config, Draws([1, 15, 1]))
    class Interrupted(NoWait):
        def wait(self, seconds, kind, report, remaining_changed):
            remaining_changed(7.5)
            raise Cancelled()
    with pytest.raises(Cancelled):
        pacer.completed_file(Interrupted(), lambda *_: None)
    assert pacer.pending_kind == "Batch pause"
    assert pacer.batch_count == 1
    control = NoWait()
    pacer.resume_pending(control, lambda *_: None)
    assert control.waits == [(7.5, "Batch pause")]
    assert pacer.batch_count == 0 and pacer.pending_kind is None


def create_real_engine(tmp_path, config):
    engine = EngineService(pacing=config)
    control = NoWait()
    events = []
    engine.control, engine.report = control, lambda *args: events.append(args)
    loader = engine._create()
    import requests
    def response(url):
        result = requests.Response()
        result.status_code = 200
        result.headers["Content-Type"] = "video/mp4" if url.endswith(".mp4") else "image/jpeg"
        result.raw = io.BytesIO(b"original media bytes")
        return result
    loader.context.get_raw = response
    return engine, control, events


def test_real_carousel_boundary_counts_files_not_posts_and_excludes_duplicates(tmp_path):
    config = PacingConfig(batch_min=3, batch_max=3, pause_min=15, pause_max=15, long_rest_enabled=False)
    engine, control, events = create_real_engine(tmp_path, config)
    loader = engine.loader
    loader.dirname_pattern = str(tmp_path / "{target}")
    loader.filename_pattern = "carousel"
    loader.post_metadata_txt_pattern = ""
    loader.save_metadata = False
    loader.download_video_thumbnails = False
    nodes = [SimpleNamespace(is_video=False, video_url=None, display_url="https://test.invalid/one.jpg"),
             SimpleNamespace(is_video=True, video_url="https://test.invalid/two.mp4", display_url="unused"),
             SimpleNamespace(is_video=False, video_url=None, display_url="https://test.invalid/three.jpg")]
    post = SimpleNamespace(typename="GraphSidecar", mediacount=3, is_video=False,
                           get_is_videos=lambda: [False, True, False], date_local=datetime.now(),
                           get_sidecar_nodes=lambda *_: iter(nodes), url="https://test.invalid/cover.jpg")
    try:
        loader.download_post(post, "posts")
        assert engine.new_files == 3
        assert control.waits == [(15, "Batch pause")]
        assert [path.suffix for path in sorted((tmp_path / "posts").iterdir())] == [".jpg", ".mp4", ".jpg"]
        # All three exist: the real Instaloader skips writes and pacing counts stay unchanged.
        loader.download_post(post, "posts")
        assert engine.new_files == 3 and len(control.waits) == 1
        loader.context.write_raw(b"metadata", str(tmp_path / "post.json"))
        loader.context.write_raw(b"caption", str(tmp_path / "post.txt"))
        assert engine.new_files == 3 and engine.pacer.long_count == 3
    finally:
        engine.close()


def test_failed_real_media_write_does_not_count_or_wait(tmp_path):
    engine, control, events = create_real_engine(tmp_path, PacingConfig(batch_min=1, batch_max=1))
    import requests
    class BrokenStream:
        def read(self, *_):
            raise OSError("simulated transfer failure")
    response = requests.Response()
    response.raw = BrokenStream()
    try:
        with pytest.raises(OSError):
            engine.loader.context.write_raw(response, str(tmp_path / "failed.mp4"))
        assert engine.new_files == 0 and engine.pacer.long_count == 0
        assert control.waits == []
        assert not (tmp_path / "failed.mp4").exists()
    finally:
        engine.close()


def test_http_429_is_not_retried_and_retry_after_is_untouched():
    import requests
    engine = EngineService()
    engine.control = NoWait()
    loader = engine._create()
    response = requests.Response()
    response.status_code = 429
    response.reason = "Too Many Requests"
    response.headers["Retry-After"] = "180"
    calls, budget_calls = [], []
    loader.context.do_sleep = lambda: None
    limiter = loader.context._rate_controller
    assert isinstance(limiter, instaloader.RateController)
    limiter.wait_before_query = lambda key: budget_calls.append(key)
    def request(*args, **kwargs):
        calls.append(args)
        return response
    try:
        with pytest.raises(instaloader.AbortDownloadException):
            loader.context.get_json("graphql/query", {"doc_id": "test"}, session=SimpleNamespace(get=request))
        assert len(calls) == len(budget_calls) == 1
        assert response.headers["Retry-After"] == "180"
        assert loader.context._rate_controller is limiter
        assert engine.new_files == engine.pacer.long_count == 0
    finally:
        engine.close()


def test_authenticated_loader_and_session_are_reused_across_paced_profiles(tmp_path):
    from test_engine_ui import FakeLoader, profile
    loaders, loaded_sessions, verified_sessions = [], [], []
    class AuthLoader(FakeLoader):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            loaders.append(self)
        def load_session(self, username, cookies):
            loaded_sessions.append((username, cookies))
        def test_login(self):
            verified_sessions.append(True)
            return "example"
    cookies = {"csrftoken": "offline-test-cookie"}
    engine = EngineService(SimpleNamespace(load=lambda _: cookies), "example", loader_factory=AuthLoader,
                           profile_factory=lambda *_: profile(),
                           pacing=PacingConfig(batch_min=3, batch_max=3, long_rest_enabled=False))
    controls = []
    try:
        for username in ("one", "two", "three"):
            control = NoWait()
            controls.append(control)
            engine.run(Job(username, str(tmp_path), DownloadOptions(profile_picture=False)), control, lambda *_: None)
        assert len(loaders) == len(loaded_sessions) == len(verified_sessions) == 1
        assert loaded_sessions == [("example", cookies)]
        assert sum(len(control.waits) for control in controls) == 2
    finally:
        engine.close()


def test_multi_profile_queue_uses_same_loader_pacer_and_correct_history(app, tmp_path, monkeypatch):
    from test_engine_ui import FakeLoader, profile
    import desktop.services.queue as queue_module
    monkeypatch.setattr(queue_module, "Control", NoWait)
    engines, controls = [], []
    class RecordingEngine(EngineService):
        def run(self, job, control, report):
            controls.append(control)
            return super().run(job, control, report)
    def factory(sessions, account, **kwargs):
        engine = RecordingEngine(sessions, account, loader_factory=FakeLoader,
                                 profile_factory=lambda *_: profile(), **kwargs)
        engines.append(engine)
        return engine
    store = LocalStore(tmp_path)
    store.settings["pacing"] = asdict(PacingConfig(batch_min=3, batch_max=3, long_rest_enabled=False))
    manager = QueueManager(store, None, factory)
    manager.add("one", str(tmp_path), DownloadOptions(profile_picture=False))
    manager.add("two", str(tmp_path), DownloadOptions(profile_picture=False))
    manager.start()
    # Editing the backing settings does not alter the current queue's immutable snapshot.
    store.settings["pacing"]["enabled"] = False
    from test_workers_queue import spin
    spin(app, lambda: manager.worker is None)
    assert len(engines) == 1 and engines[0].pacer.config.enabled
    assert controls[0].waits == []
    assert len(controls[1].waits) == 1  # 2 files in profile one + 1 in profile two.
    assert engines[0].pacer.batch_count == 1
    assert [j.files for j in manager.jobs] == [2, 2]
    assert [j.status for j in manager.jobs] == ["Completed", "Completed"]
    assert [row[5] for row in store.history()] == [2, 2]
    store.close()


def test_qt_worker_countdown_manual_pause_cancel_and_retry_history(app, tmp_path):
    from test_engine_ui import FakeLoader, profile
    from test_workers_queue import spin
    store = LocalStore(tmp_path)
    store.settings["pacing"] = asdict(PacingConfig(batch_min=1, batch_max=1, long_rest_enabled=False))
    engines = []
    def factory(sessions, account, **kwargs):
        engine = EngineService(sessions, account, loader_factory=FakeLoader,
                               profile_factory=lambda *_: profile(), **kwargs)
        engines.append(engine)
        return engine
    manager = QueueManager(store, None, factory)
    job = manager.add("example", str(tmp_path), DownloadOptions(profile_picture=False))
    manager.start()
    spin(app, lambda: job.status == "Batch pause" and "remaining" in job.detail)
    assert job.files == 1
    manager.pause()
    spin(app, lambda: job.status == "Paused" and job.detail.startswith("Paused"))
    assert job.files == 1
    manager.pause()
    spin(app, lambda: job.status == "Batch pause")
    manager.cancel(job)
    spin(app, lambda: manager.worker is None)
    assert job.status == "Cancelled" and job.files == 1
    assert store.history()[0][5] == 1
    store.settings["pacing"]["enabled"] = False
    manager.retry(job)
    manager.start()
    spin(app, lambda: manager.worker is None)
    assert job.status == "Completed" and job.files == 1  # First file already exists.
    assert [row[5] for row in store.history()] == [1, 1]
    store.close()


def test_pacing_persistence_and_invalid_config_recovery(tmp_path):
    store = LocalStore(tmp_path)
    config = replace(PacingConfig(), batch_min=4, batch_max=6, pause_min=20, enabled=False)
    store.settings["pacing"] = asdict(config)
    store.settings["performance"]["profile"] = "Custom"
    store.save_settings()
    store.close()
    reopened = LocalStore(tmp_path)
    assert PacingConfig(**reopened.settings["pacing"]) == config
    reopened.settings["pacing"]["pause_min"] = -1
    reopened.save_settings()
    reopened.close()
    repaired = LocalStore(tmp_path)
    assert repaired.settings["pacing"] == asdict(PacingConfig())
    assert "pacing" in repaired.warning
    repaired.close()


def test_gui_pacing_range_validation_toggles_and_persistence(app, tmp_path):
    store = LocalStore(tmp_path)
    window = MainWindow(store)
    assert window.tabs.tabText(4) == "Settings"
    assert window.get_pacing() == PacingConfig(enabled=False, long_rest_enabled=False)
    window.performance_profile.setCurrentText("Conservative")
    assert window.get_pacing() == PacingConfig()
    window.pacing_inputs["batch_min"].setValue(10)
    assert window.pacing_inputs["batch_max"].value() == 10
    window.pacing_inputs["batch_max"].setValue(5)
    assert window.pacing_inputs["batch_min"].value() == 5
    window.long_rest_enabled.setChecked(False)
    assert not window.pacing_inputs["long_interval_min"].isEnabled()
    window.pacing_enabled.setChecked(False)
    assert not window.pacing_inputs["batch_min"].isEnabled()
    window.close()
    assert store.settings["pacing"]["enabled"] is False
    assert store.settings["pacing"]["batch_min"] == 5
    store.close()


def test_saved_session_is_not_selected_or_activated_on_startup(app, tmp_path):
    store = LocalStore(tmp_path)
    SessionStore(tmp_path).save("example", {"csrftoken": "offline-only"})
    store.settings["account"] = "example"
    window = MainWindow(store)
    assert window.account.currentData() == ""
    assert not window.use_session.isChecked()
    assert window.auth_status.text() == "Public Mode — No Login"
    assert "example" in window.sessions.accounts()
    window.close()
    store.close()
