from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import threading
import time

import instaloader
import pytest
import requests

from desktop.models import DownloadOptions, Job
from desktop.optimized_benchmark import OfflineInstagram, manifest, run_case
from desktop.services.control import Cancelled, Control
from desktop.services.engine import EngineService
from desktop.services.optimized import (ListingPost, Media, MediaTask, MediaTransfers,
                                        OptimizedEngineService, media_url_allowed, parse_listing_post)
from desktop.services.pacing import Pacer, PacingConfig
from desktop.services.performance import RunStatistics, diagnostic_report
from desktop.services.pipeline import PipelineConfig
from desktop.services.public_first import PublicFirstService
from desktop.services.queue import QueueManager
from desktop.services.safety import RestrictionGate, RestrictionStopped
from desktop.storage.local import LocalStore
from desktop.ui.window import MainWindow


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for an offline worker")
        time.sleep(0.005)


def make_engine(backend, workers=3, pacing=None):
    engine = OptimizedEngineService(config=PipelineConfig(True, workers), loader_factory=backend.loader,
        profile_factory=backend.profile, pacing=pacing or PacingConfig(enabled=False))
    engine.statistics, engine.safety = RunStatistics(), RestrictionGate()
    return engine


def job_at(path):
    return Job("fixture", str(path), DownloadOptions(captions=False, profile_picture=False))


def background(function):
    failures = []
    def run():
        try:
            function()
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, failures


@pytest.mark.parametrize("workers", [0, 6, -1, True, "3", 1.5])
def test_pipeline_rejects_invalid_worker_counts(workers):
    with pytest.raises(ValueError):
        PipelineConfig(True, workers)


@pytest.mark.parametrize("workers", [1, 2, 3, 4, 5])
def test_pipeline_supports_bounded_worker_counts(workers):
    assert PipelineConfig(True, workers).workers == workers


@pytest.mark.parametrize("url,allowed", [
    ("https://scontent.test.cdninstagram.com/p.jpg?sig=private", True),
    ("https://scontent.test.fbcdn.net/p.mp4", True),
    ("http://scontent.test.fbcdn.net/p.jpg", False),
    ("https://www.instagram.com/api/graphql", False),
    ("https://fbcdn.net.attacker.invalid/p.jpg", False),
    ("https://user:password@cdninstagram.com/p.jpg", False),
    ("https://cdninstagram.com:8443/p.jpg", False),
    ("https://cdninstagram.com/p.jpg#fragment", False),
])
def test_only_parallel_media_hosts_are_allowed(url, allowed):
    assert media_url_allowed(url) is allowed


def test_media_rich_shapes_carousel_selection_and_no_secret_repr():
    post = OfflineInstagram(1).posts[0]
    plan = parse_listing_post(post)
    assert isinstance(plan, ListingPost)
    assert len(plan.media) == 3
    assert [m.index for m in plan.media] == [1, 2, 3]
    assert "synthetic-private" not in repr(plan)
    assert "synthetic-private" not in repr(plan.media[0])
    post["carousel_media"][1] = {"media_type": 2, "video_versions": [{"url": "https://fixture.fbcdn.net/v.mp4"}]}
    photos = parse_listing_post(post, videos=False)
    assert [m.index for m in photos.media] == [1, 3]
    videos = parse_listing_post(post, photos=False)
    assert [m.index for m in videos.media] == [2] and videos.media[0].suffix == ".mp4"
    # Missing a selected child invalidates the entire plan, not just that child.
    post["carousel_media"][2]["image_versions2"] = {}
    assert parse_listing_post(post) is None
    assert len(parse_listing_post(post, photos=False).media) == 1


def test_no_thumbnail_substitution_and_incomplete_counts_use_fallback():
    post = OfflineInstagram(1).posts[0]
    post["carousel_media_count"] = 4
    assert parse_listing_post(post) is None
    assert parse_listing_post({"code": "ABC", "taken_at": 1, "media_type": 1,
                               "thumbnail_src": "https://fixture.cdninstagram.com/tiny.jpg"}) is None
    assert parse_listing_post({"code": "ABC"}) is None


def test_graphql_media_rich_carousel_preserves_urls_and_validates_child_count():
    raw = {"shortcode": "ABC", "__typename": "GraphSidecar", "taken_at_timestamp": 1700000000,
        "edge_sidecar_to_children": {"count": 2, "edges": [
            {"node": {"is_video": False, "display_url": "https://fixture.fbcdn.net/p.jpg?sig=opaque"}},
            {"node": {"is_video": True, "video_url": "https://fixture.fbcdn.net/v.mp4?sig=opaque"}}]}}
    plan = parse_listing_post(raw)
    assert [media.index for media in plan.media] == [1, 2]
    assert plan.media[0].url == raw["edge_sidecar_to_children"]["edges"][0]["node"]["display_url"]
    raw["edge_sidecar_to_children"]["count"] = 3
    assert parse_listing_post(raw) is None


@pytest.mark.parametrize("rich", [False, True])
def test_pipelines_write_identical_media_and_native_compatible_paths(tmp_path, rich):
    native, native_files = run_case(tmp_path / "native", rich=rich, posts=14, media_latency=0, api_latency=0)
    optimized, optimized_files = run_case(tmp_path / "optimized", True, 3, rich, 14, 0, 0)
    assert native_files == optimized_files and len(native_files) == native["files"] == optimized["files"]
    assert optimized["http_requests"] == optimized["observed_requests"]
    assert optimized["optimized_rich_posts"] == (14 if rich else 0)
    assert optimized["observed_metadata_requests"] == (0 if rich else 14)
    assert native["observed_metadata_requests"] == 14
    assert optimized["observed_listing_requests"] == 2
    assert optimized["files"] == 20
    assert "synthetic-private" not in json.dumps(diagnostic_report(optimized))


def test_actual_workers_overlap_and_have_isolated_sessions(tmp_path, monkeypatch):
    backend = OfflineInstagram(6, api_latency=0, media_latency=0.03)
    session_cookies = {}
    original = backend.send
    lock = threading.Lock()
    def observe(adapter, request, **kwargs):
        if "fixture.cdninstagram.com" in request.url:
            assert "saved-account-secret" not in request.headers.get("Cookie", "")
            with lock:
                session_cookies[id(adapter)] = request.headers.get("Cookie", "")
        return original(adapter, request, **kwargs)
    backend.send = observe
    engine = make_engine(backend, 3)
    with backend.offline():
        # Discovery's account-like cookie must not be inherited by workers.
        original_loader = backend.loader
        def loader(**kwargs):
            instance = original_loader(**kwargs)
            instance.context._session.cookies.set("sessionid", "saved-account-secret")
            return instance
        engine.loader_factory = loader
        engine.run(job_at(tmp_path), Control(), lambda *_: None)
    engine.close()
    data = engine.statistics.snapshot()
    assert backend.peak == data["peak_transfers"] == 3
    assert len(session_cookies) == 3
    assert data["media_workers"] == 3 and data["active_transfers"] == 0
    assert data["media_transfer_samples"] == data["files"] == 10


def test_duplicates_never_retransfer_or_overwrite_and_do_not_count(tmp_path):
    backend = OfflineInstagram(6, api_latency=0, media_latency=0)
    engine = make_engine(backend)
    events = []
    with backend.offline():
        job = job_at(tmp_path)
        engine.run(job, Control(), lambda *a: events.append(a))
        original = manifest(tmp_path)
        media = backend.media
        events.clear()
        engine.run(job, Control(), lambda *a: events.append(a))
    engine.close()
    assert backend.media == media == 10
    assert manifest(tmp_path) == original
    assert not any(kind == "files" for kind, _ in events)
    assert engine.statistics.snapshot()["optimized_duplicate_files"] == 10


def test_pause_drains_current_wave_and_resume_restarts_admission(tmp_path):
    backend = OfflineInstagram(6, api_latency=0, media_latency=0)
    ready, release = threading.Event(), threading.Event()
    original, count, lock = backend.send, [0], threading.Lock()
    def send(adapter, request, **kwargs):
        if "fixture.cdninstagram.com" in request.url:
            with lock:
                count[0] += 1
                first = count[0] <= 3
                if count[0] == 3:
                    ready.set()
            if first:
                assert release.wait(3)
        return original(adapter, request, **kwargs)
    backend.send = send
    engine, control = make_engine(backend), Control()
    with backend.offline():
        thread, failures = background(lambda: engine.run(job_at(tmp_path), control, lambda *_: None))
        try:
            assert ready.wait(3)
            control.set_paused(True)
            release.set()
            wait_for(lambda: engine.statistics.snapshot()["files"] == 3)
            assert count[0] == 3 and thread.is_alive()
            control.set_paused(False)
            thread.join(3)
            assert not thread.is_alive() and not failures
        finally:
            release.set()
            control.cancel()
            thread.join(3)
            engine.close()
    assert engine.statistics.snapshot()["files"] == 10


def test_cancellation_of_paused_scheduler_is_immediate_and_no_more_requests(tmp_path):
    backend = OfflineInstagram(6, api_latency=0, media_latency=0)
    engine, control = make_engine(backend), Control()
    control.set_paused(True)
    with backend.offline():
        thread, failures = background(lambda: engine.run(job_at(tmp_path), control, lambda *_: None))
        control.cancel()
        thread.join(1)
    engine.close()
    assert not thread.is_alive() and len(failures) == 1 and isinstance(failures[0], Cancelled)
    assert backend.requests == 0


def test_restriction_stops_all_admission_and_preserves_retry_after(tmp_path):
    backend = OfflineInstagram(12, api_latency=0, media_latency=0.01)
    backend.media_status, backend.media_headers = 429, {"Retry-After": "1200"}
    engine = make_engine(backend, 3)
    with backend.offline(), pytest.raises(RestrictionStopped):
        engine.run(job_at(tmp_path), Control(), lambda *_: None)
    engine.close()
    assert 1 <= backend.media <= 3  # Already admitted requests only, no retry/wave two.
    assert engine.safety.remaining() > 1190
    assert engine.statistics.snapshot()["files"] == 0
    assert not manifest(tmp_path) and not list(tmp_path.rglob("*.part"))


@pytest.mark.parametrize("problem", ["empty", "short", "content_type", "redirect"])
def test_invalid_media_does_not_publish_a_file(tmp_path, problem):
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    original = backend.send
    def broken(adapter, request, **kwargs):
        response = original(adapter, request, **kwargs)
        if "fixture.cdninstagram.com" in request.url:
            if problem == "empty":
                response.raw = io.BytesIO(b"")
            elif problem == "short":
                response.headers["Content-Length"] = "999999"
            elif problem == "content_type":
                response.headers["Content-Type"] = "text/html"
            else:
                response.status_code = 302
                response.headers["Location"] = "https://www.instagram.com/accounts/login/"
        return response
    backend.send = broken
    engine = make_engine(backend, 1)
    with backend.offline(), pytest.raises(instaloader.ConnectionException):
        engine.run(job_at(tmp_path), Control(), lambda *_: None)
    engine.close()
    assert not manifest(tmp_path) and not list(tmp_path.rglob("*.part"))
    assert engine.statistics.snapshot()["files"] == 0 and backend.media == 1


def test_cancel_mid_body_removes_partial_and_keeps_existing_files(tmp_path):
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    original = backend.send
    ready, release, control = threading.Event(), threading.Event(), Control()
    class Body(io.BytesIO):
        reads = 0
        def read(self, size=-1):
            self.reads += 1
            if self.reads == 2:
                ready.set()
                assert release.wait(3)
            return super().read(size)
    def send(adapter, request, **kwargs):
        response = original(adapter, request, **kwargs)
        if "fixture.cdninstagram.com" in request.url:
            response.raw = Body(b"x" * (512 * 1024))
            response.headers["Content-Length"] = str(512 * 1024)
        return response
    backend.send = send
    existing = tmp_path / "keep.jpg"
    existing.write_bytes(b"keep")
    engine = make_engine(backend, 1)
    with backend.offline():
        thread, failures = background(lambda: engine.run(job_at(tmp_path), control, lambda *_: None))
        try:
            assert ready.wait(3)
            assert list(tmp_path.rglob("*.part"))
            assert len(manifest(tmp_path)) == 1  # No final media name before full body.
            control.cancel()
        finally:
            release.set()
            thread.join(3)
            engine.close()
    assert not thread.is_alive() and isinstance(failures[0], Cancelled)
    assert engine.statistics.snapshot()["files"] == 0
    assert existing.read_bytes() == b"keep" and not list(tmp_path.rglob("*.part"))


def test_pacing_threshold_drains_workers_and_does_not_stack_rests(tmp_path):
    backend = OfflineInstagram(6, api_latency=0, media_latency=0.01)
    config = PacingConfig(batch_min=3, batch_max=3, pause_min=1, pause_max=1,
        long_interval_min=3, long_interval_max=3, long_pause_min=2, long_pause_max=2)
    engine = make_engine(backend, 5, config)
    waits = []
    class NoSleep(Control):
        def wait(self, seconds, kind, report, remaining_changed=lambda *_: None):
            assert backend.active == 0
            assert engine.statistics.snapshot()["active_transfers"] == 0
            waits.append((kind, engine.new_files, seconds))
            remaining_changed(0)
    with backend.offline():
        engine.run(job_at(tmp_path), NoSleep(), lambda *_: None)
    engine.close()
    assert waits == [("Long rest", 3, 2), ("Long rest", 6, 2), ("Long rest", 9, 2)]
    assert engine.statistics.snapshot()["peak_transfers"] <= 3


@pytest.mark.parametrize("field", ["captions", "reels", "stories", "highlights", "profile_picture"])
def test_unsupported_operations_keep_original_engine(monkeypatch, tmp_path, field):
    calls = []
    monkeypatch.setattr(EngineService, "_run", lambda self, *args: calls.append(args))
    engine = OptimizedEngineService()
    job = job_at(tmp_path)
    setattr(job.options, field, True)
    engine.run(job, Control(), lambda *_: None)
    assert len(calls) == 1


def test_authenticated_engine_always_keeps_original_session_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(EngineService, "_run", lambda self, *args: calls.append(self.account))
    OptimizedEngineService(account="approved").run(job_at(tmp_path), Control(), lambda *_: None)
    assert calls == ["approved"]


def test_another_profile_cannot_enter_an_active_engine(tmp_path):
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    ready, release = threading.Event(), threading.Event()
    original = backend.send
    def send(adapter, request, **kwargs):
        if "fixture.cdninstagram.com" in request.url:
            ready.set()
            assert release.wait(3)
        return original(adapter, request, **kwargs)
    backend.send = send
    engine = make_engine(backend, 1)
    with backend.offline():
        thread, failures = background(lambda: engine.run(job_at(tmp_path), Control(), lambda *_: None))
        try:
            assert ready.wait(3)
            with pytest.raises(RuntimeError, match="two profiles"):
                engine.run(replace(job_at(tmp_path), username="another"), Control(), lambda *_: None)
        finally:
            release.set()
            thread.join(3)
            engine.close()
    assert not thread.is_alive() and not failures


def test_pipeline_settings_persist_and_invalid_settings_restore_baseline(app, tmp_path):
    store = LocalStore(tmp_path)
    assert store.settings["pipeline"] == asdict(PipelineConfig())
    window = MainWindow(store)
    window.optimized_enabled.setChecked(True)
    window.optimized_workers.setValue(5)
    assert window.media_workers.currentText().startswith("5")
    window.close()
    store.close()
    reopened = LocalStore(tmp_path)
    assert reopened.settings["pipeline"] == {"enabled": True, "workers": 5}
    reopened.settings["pipeline"]["workers"] = 9
    reopened.save_settings()
    reopened.close()
    restored = LocalStore(tmp_path)
    assert restored.settings["pipeline"] == asdict(PipelineConfig())
    restored.close()


def test_queue_uses_opt_in_engine_and_preserves_profile_continuity(app, tmp_path, monkeypatch):
    from test_workers_queue import spin
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    original = OptimizedEngineService.__init__
    instances = []
    def initialize(self, *args, **kwargs):
        kwargs.update(loader_factory=backend.loader, profile_factory=backend.profile)
        original(self, *args, **kwargs)
        instances.append(self)
    monkeypatch.setattr(OptimizedEngineService, "__init__", initialize)
    store = LocalStore(tmp_path / "state")
    store.settings["pipeline"] = asdict(PipelineConfig(True, 2))
    manager = QueueManager(store, None)
    manager.add("first", str(tmp_path / "media"), job_at(tmp_path).options)
    manager.add("second", str(tmp_path / "media"), job_at(tmp_path).options)
    with backend.offline():
        manager.start()
        spin(app, lambda: manager.worker is None)
    assert [job.status for job in manager.jobs] == ["Completed", "Completed"]
    assert [job.files for job in manager.jobs] == [4, 4]
    assert manager.statistics.snapshot()["files"] == 8
    assert len(instances) == 1  # One anonymous engine/rate budget across queue profiles.
    assert len(store.history()) == 2
    store.close()


def test_api_discovery_only_uses_coordinator_thread(tmp_path):
    backend = OfflineInstagram(14, rich=False, api_latency=0, media_latency=0.01)
    original, api_threads, media_threads = backend.send, set(), set()
    def send(adapter, request, **kwargs):
        (api_threads if "www.instagram.com" in request.url else media_threads).add(threading.get_ident())
        return original(adapter, request, **kwargs)
    backend.send = send
    engine = make_engine(backend)
    with backend.offline():
        engine.run(job_at(tmp_path), Control(), lambda *_: None)
    engine.close()
    assert api_threads == {threading.get_ident()}
    assert len(media_threads) == 3 and not api_threads.intersection(media_threads)


def test_unrecognized_native_version_keeps_native_hydration(tmp_path, monkeypatch):
    monkeypatch.setattr(instaloader, "__version__", "future-version")
    data, files = run_case(tmp_path, True, 1, True, 2, 0, 0)
    assert data["observed_metadata_requests"] == 2
    assert len(files) == data["files"] == 4


def test_completed_files_remain_accurate_when_later_wave_fails(tmp_path):
    backend = OfflineInstagram(12, api_latency=0, media_latency=0)
    original, calls = backend.send, [0]
    def send(adapter, request, **kwargs):
        if "fixture.cdninstagram.com" in request.url:
            calls[0] += 1
            if calls[0] > 3:
                backend.media_status = 429
                backend.media_headers = {"Retry-After": "600"}
        return original(adapter, request, **kwargs)
    backend.send = send
    engine = make_engine(backend, 3)
    events = []
    with backend.offline(), pytest.raises(RestrictionStopped):
        engine.run(job_at(tmp_path), Control(), lambda *a: events.append(a))
    engine.close()
    assert engine.statistics.snapshot()["files"] == len(manifest(tmp_path)) == 3
    assert [v for k, v in events if k == "files"] == [1, 2, 3]
    assert [v for k, v in events if k == "items"] == [1]  # First carousel fully completed.


def test_rate_restriction_wins_over_another_inflight_network_error(tmp_path):
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    barrier, original, calls, lock = threading.Barrier(2), backend.send, [0], threading.Lock()
    def send(adapter, request, **kwargs):
        response = original(adapter, request, **kwargs)
        if "fixture.cdninstagram.com" in request.url:
            with lock:
                calls[0] += 1
                number = calls[0]
            barrier.wait(3)
            if number == 1:
                response.status_code = 500
            else:
                time.sleep(0.02)
                response.status_code = 429
                response.headers["Retry-After"] = "700"
        return response
    backend.send = send
    engine = make_engine(backend, 2)
    with backend.offline(), pytest.raises(RestrictionStopped):
        engine.run(job_at(tmp_path), Control(), lambda *_: None)
    engine.close()
    assert calls == [2] and engine.safety.remaining() > 690


def test_path_collision_cannot_overwrite_or_prematurely_finish_a_post(tmp_path):
    backend = OfflineInstagram(4, api_latency=0, media_latency=0)
    backend.posts = backend.posts[2:4]
    backend.posts[1]["taken_at"] = backend.posts[0]["taken_at"]
    engine = make_engine(backend, 3)
    events = []
    with backend.offline():
        engine.run(job_at(tmp_path), Control(), lambda *a: events.append(a))
    engine.close()
    assert backend.media == 1 and len(manifest(tmp_path)) == 1
    assert [v for k, v in events if k == "files"] == [1]
    assert [v for k, v in events if k == "items"] == [1, 2]


def test_shared_pacing_counters_continue_across_profiles(tmp_path):
    backend = OfflineInstagram(2, api_latency=0, media_latency=0.01)
    pacing = PacingConfig(batch_min=3, batch_max=3, pause_min=1, pause_max=1, long_rest_enabled=False)
    engine = make_engine(backend, 5, pacing)
    waits = []
    class NoSleep(Control):
        def wait(self, seconds, kind, report, remaining_changed=lambda *_: None):
            assert backend.active == 0
            waits.append(engine.statistics.snapshot()["files"])
            remaining_changed(0)
    with backend.offline():
        engine.run(job_at(tmp_path), NoSleep(), lambda *_: None)
        engine.run(replace(job_at(tmp_path), username="second"), NoSleep(), lambda *_: None)
    engine.close()
    assert waits == [3, 6] and engine.statistics.snapshot()["files"] == 8


def test_private_profile_keeps_explicit_public_first_consent_and_native_fallback(tmp_path, monkeypatch):
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    calls, requests = [], []
    def original_run(self, *args):
        calls.append(self.account)
    monkeypatch.setattr(EngineService, "_run", original_run)
    def factory(*args, **kwargs):
        engine = make_engine(backend)
        engine.account = args[1]
        original_profile = engine.profile_factory
        def profile(*a):
            instance = original_profile(*a)
            instance.is_private = True
            return instance
        engine.profile_factory = profile
        return engine
    service = PublicFirstService(engine_factory=factory, pacing=PacingConfig(enabled=False))
    service.auth_handler = lambda request: requests.append(request) or "approved"
    with backend.offline():
        service.run(job_at(tmp_path), Control(), lambda *_: None)
    service.close()
    assert calls == ["approved"] and len(requests) == 1
    assert requests[0]["content"] == "private profile"


def test_disk_error_is_local_and_worker_pool_closes(tmp_path, monkeypatch):
    import desktop.services.optimized as module
    backend = OfflineInstagram(2, api_latency=0, media_latency=0)
    def denied(*args):
        raise PermissionError("No permission for destination")
    monkeypatch.setattr(module.os, "rename", denied)
    engine = make_engine(backend, 1)
    with backend.offline(), pytest.raises(PermissionError):
        engine.run(job_at(tmp_path), Control(), lambda *_: None)
    engine.close()
    assert not manifest(tmp_path) and not list(tmp_path.rglob("*.part"))
    assert engine.statistics.snapshot()["optimized_failed_files"] == 1
