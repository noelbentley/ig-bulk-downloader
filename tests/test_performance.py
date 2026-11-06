from dataclasses import asdict
from datetime import datetime, timezone
from email.utils import format_datetime
import io
import json
from pathlib import Path
import threading

import instaloader
import pytest
import requests

from desktop.models import DownloadOptions, Job
from desktop.services.control import Cancelled, Control
from desktop.services.engine import EngineService
from desktop.services.errors import classify_error, safe_activity
from desktop.services.performance import PerformanceConfig, RunStatistics, preset_pacing, summary_text
from desktop.services.public_first import PublicFirstService
from desktop.services.queue import QueueManager
from desktop.services.safety import RestrictionGate, RestrictionStopped, retry_after_seconds
from desktop.storage.local import LocalStore
from desktop.ui.window import MainWindow
from test_engine_ui import FakeLoader, profile
from test_workers_queue import FakeEngine, spin


@pytest.mark.parametrize("workers", [0, 2, 3, 4, True, "1"])
def test_unsupported_concurrency_is_rejected(workers):
    with pytest.raises(ValueError):
        PerformanceConfig(media_workers=workers)


def test_profiles_defaults_migration_and_custom_persistence(app, tmp_path):
    old = {"pacing": asdict(preset_pacing("Conservative")), "account": "never_automatically_loaded"}
    (tmp_path / "settings.json").write_text(json.dumps(old), encoding="utf-8")
    store = LocalStore(tmp_path)
    assert store.settings["performance"]["profile"] == "Balanced"
    assert not store.settings["pacing"]["enabled"] and not store.settings["pacing"]["long_rest_enabled"]
    assert store.settings["custom_pacing"] == old["pacing"]
    window = MainWindow(store)
    assert window.performance_profile.currentText() == "Balanced"
    assert window.media_workers.count() == 1 and not window.media_workers.isEnabled()
    window.performance_profile.setCurrentText("Conservative")
    assert window.get_pacing() == preset_pacing("Conservative")
    window.pacing_inputs["pause_min"].setValue(22)
    assert window.performance_profile.currentText() == "Custom"
    window.performance_profile.setCurrentText("Balanced")
    assert not window.get_pacing().enabled
    window.performance_profile.setCurrentText("Custom")
    assert window.get_pacing().pause_min == 22 and window.get_pacing().enabled
    window.close()
    store.close()
    reopened = LocalStore(tmp_path)
    assert reopened.settings["performance"]["profile"] == "Custom"
    assert reopened.settings["pacing"]["pause_min"] == 22
    reopened.close()


def test_statistics_measures_nested_phases_throughput_and_numeric_privacy():
    now = [0.0]
    stats = RunStatistics(clock=lambda: now[0])
    with stats.measure("transfer_seconds", transfer=True):
        now[0] = 2
        with stats.measure("transfer_seconds"):
            now[0] = 3
        assert stats.snapshot()["active_transfers"] == 1
        now[0] = 5
    stats.add("files", 10)
    now[0] = 60
    result = stats.finish()
    assert result["transfer_seconds"] == 5
    assert result["files_per_minute"] == 10
    assert result["active_transfers"] == 0 and result["peak_transfers"] == 1
    assert all(isinstance(value, (int, float)) for key, value in result.items() if key != "profile")
    assert "10 files" in summary_text(result)
    assert summary_text({"secret": "not displayed"}) == "No completed queue run yet."
    with pytest.raises(ValueError):
        stats.add("signed_url", "secret")


@pytest.mark.parametrize("value,expected", [("120", 120), ("0", 0), ("invalid", None), (None, None), ("-1", None)])
def test_retry_after_numeric_and_invalid(value, expected):
    assert retry_after_seconds(value, now=1000) == expected


def test_retry_after_http_date_and_gate_persistence():
    now = [1000.0]
    header = format_datetime(datetime.fromtimestamp(1120, timezone.utc), usegmt=True)
    assert retry_after_seconds(header, now=1000) == 120
    gate = RestrictionGate(wall=lambda: now[0], monotonic=lambda: now[0])
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = header
    with pytest.raises(RestrictionStopped):
        gate.inspect_response(response, lambda *_: None)
    assert gate.remaining() == 120 and gate.events == 1 and not gate.review
    reopened = RestrictionGate(gate.state(), wall=lambda: now[0], monotonic=lambda: now[0])
    assert reopened.remaining() == 120
    now[0] += 30
    assert reopened.remaining() == 90
    with pytest.raises(RestrictionStopped):
        reopened.inspect_response(response, lambda *_: None)
    assert reopened.review
    remainder = reopened.remaining()
    reopened.acknowledge_review()
    assert not reopened.review and reopened.remaining() == remainder


@pytest.mark.parametrize("status", [400, 401, 403])
def test_status_alone_does_not_mean_ip_blocking_or_create_cooldown(status):
    gate = RestrictionGate()
    response = requests.Response()
    response.status_code = status
    gate.inspect_response(response, lambda *_: None)
    assert gate.remaining() == 0 and gate.events == 0
    response.headers["Retry-After"] = "45"
    with pytest.raises(RestrictionStopped):
        gate.inspect_response(response, lambda *_: None)
    assert gate.remaining() > 44 and gate.events == 0


def test_security_challenge_requires_review_without_automatic_retry():
    gate = RestrictionGate()
    gate.inspect_exception(instaloader.AbortDownloadException("checkpoint_required"), lambda *_: None)
    assert gate.review and gate.remaining() > 899
    with pytest.raises(RestrictionStopped):
        gate.wait(Control(), lambda *_: pytest.fail("No requests or retry should occur"))


def test_cooldown_countdown_and_cancellation_before_any_profile_request(app, tmp_path):
    store = LocalStore(tmp_path)
    def factory(sessions, account, **kwargs):
        def engine_factory(*args, **kwargs):
            pytest.fail("No engine may be created during the restriction cooldown")
        return PublicFirstService(sessions, account, engine_factory=engine_factory, **kwargs)
    manager = QueueManager(store, None, factory)
    manager.safety.restrict(120, lambda *_: None)
    job = manager.add("example", str(tmp_path), DownloadOptions())
    manager.start()
    spin(app, lambda: job.status == "Restriction cooldown")
    manager.stop()
    spin(app, lambda: manager.worker is None)
    assert job.status == "Cancelled" and job.files == 0
    assert store.settings["last_performance"]["cooldown_seconds"] > 0
    store.close()


def test_response_429_never_retries_or_switches_identity_and_persists_gate(app, tmp_path):
    store = LocalStore(tmp_path)
    calls = []
    def loader_factory(**kwargs):
        loader = instaloader.Instaloader(**kwargs)
        response = requests.Response()
        response.status_code = 429
        response.reason = "Too Many Requests"
        response.headers["Retry-After"] = "120"
        def get(*args, **kwargs):
            calls.append(True)
            return response
        loader.context._session.get = get
        loader.context.do_sleep = lambda: None
        loader.context._rate_controller.wait_before_query = lambda _: None
        return loader
    def factory(sessions, account, **kwargs):
        def lookup(context, username):
            context.get_json("graphql/query", {"signed_token": "must-not-be-logged"})
        def engine_factory(sessions, account, **kwargs):
            assert account == ""
            return EngineService(sessions, account, loader_factory=loader_factory, profile_factory=lookup, **kwargs)
        return PublicFirstService(sessions, account, engine_factory=engine_factory, **kwargs)
    manager = QueueManager(store, None, factory)
    manager.authentication_required.connect(lambda _: pytest.fail("429 must not request login"))
    job = manager.add("example", str(tmp_path), DownloadOptions())
    manager.start()
    spin(app, lambda: manager.worker is None)
    assert len(calls) == 1 and job.status == "Failed"
    assert manager.safety.events == 1  # Do not count a wrapped status response twice.
    assert 119 < manager.safety.remaining() <= 120
    assert store.settings["restriction"]["until"] > 0
    data = store.settings["last_performance"]
    assert data["failed_requests"] == 1 and data["failed_requests_seconds"] >= 0
    assert "must-not-be-logged" not in json.dumps(store.settings)
    assert "must-not-be-logged" not in job.detail
    store.close()
    reopened = LocalStore(tmp_path)
    assert RestrictionGate(reopened.settings["restriction"]).remaining() > 115
    reopened.close()


def test_queue_reentrancy_and_order_never_allow_parallel_profiles(app, tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []
    class Blocking(FakeEngine):
        def run(self, job, control, report):
            calls.append(job.username)
            started.set()
            release.wait(2)
            control.checkpoint(report)
    store = LocalStore(tmp_path)
    manager = QueueManager(store, None, Blocking)
    manager.add("one", str(tmp_path), DownloadOptions())
    manager.add("two", str(tmp_path), DownloadOptions())
    manager.start()
    assert started.wait(1)
    first_worker = manager.worker
    manager.start()
    manager._next()
    assert manager.worker is first_worker and calls == ["one"]
    release.set()
    spin(app, lambda: manager.worker is None)
    assert calls == ["one", "two"]
    store.close()


def test_progress_events_are_coalesced_but_final_counts_are_exact(app, tmp_path):
    store = LocalStore(tmp_path)
    manager = QueueManager(store, None, FakeEngine)
    manager.current = manager.add("one", str(tmp_path), DownloadOptions())
    refreshes, messages = [], []
    manager.changed.connect(lambda: refreshes.append(True))
    manager.activity.connect(messages.append)
    for index in range(100):
        manager._event("files", index)
        manager._event("activity", f"file {index}")
    assert manager.current.files == 99
    assert refreshes == []
    manager.flush_ui()
    assert len(refreshes) == 1 and messages == ["file 99"]
    store.close()


def test_completed_run_counts_timings_and_summary_survive_restart(app, tmp_path):
    store = LocalStore(tmp_path)
    def factory(sessions, account, **kwargs):
        def engine_factory(sessions, account, **kwargs):
            return EngineService(sessions, account, loader_factory=FakeLoader,
                                 profile_factory=lambda *_: profile(), **kwargs)
        return PublicFirstService(sessions, account, engine_factory=engine_factory, **kwargs)
    manager = QueueManager(store, None, factory)
    for username in ("one", "two"):
        manager.add(username, str(tmp_path), DownloadOptions(profile_picture=False))
    manager.start()
    spin(app, lambda: manager.worker is None)
    result = store.settings["last_performance"]
    assert result["files"] == 4 and result["peak_transfers"] == 1 and result["active_transfers"] == 0
    assert result["active_profiles"] == 0 and result["pacing_seconds"] == 0
    assert result["files_per_minute"] > 0
    assert result["profile"] == "Balanced"
    store.close()
    reopened = LocalStore(tmp_path)
    window = MainWindow(reopened)
    assert "4 files" in window.last_performance.text()
    window.close()
    reopened.close()


def test_balanced_preserves_native_rate_controller_wait_accounting():
    engine = EngineService(pacing=preset_pacing("Balanced"))
    engine.statistics = RunStatistics()
    engine.control = Control()
    loader = engine._create()
    try:
        assert isinstance(loader.context._rate_controller, instaloader.RateController)
        loader.context._rate_controller.sleep(0.01)
        assert engine.statistics.snapshot()["rate_wait_seconds"] >= 0.009
        assert engine.pacer.config.enabled is False
    finally:
        engine.close()


def test_engine_and_coordinator_reject_overlapping_execution(tmp_path):
    service = PublicFirstService()
    engine = EngineService()
    job = Job("one", str(tmp_path))
    for target in (service, engine):
        target._execution.acquire()
        try:
            with pytest.raises(RuntimeError):
                target.run(job, Control(), lambda *_: None)
        finally:
            target._execution.release()


def test_atomic_completion_existing_path_collision_and_failed_stream(tmp_path):
    engine = EngineService(pacing=preset_pacing("Balanced"))
    stats = engine.statistics = RunStatistics()
    engine.control = Control()
    loader = engine._create()
    destination = tmp_path / "same"
    original_response = requests.Response()
    original_response.status_code = 200
    original_response.headers["Content-Type"] = "image/jpeg"
    class ObservedStream(io.BytesIO):
        def read(self, *args):
            assert not destination.with_suffix(".jpg").exists()
            return super().read(*args)
    original_response.raw = ObservedStream(b"original")
    loader.context.get_raw = lambda _: original_response
    try:
        assert loader.download_pic(str(destination), "https://offline.invalid/same.jpg", datetime.now())
        assert destination.with_suffix(".jpg").read_bytes() == b"original"
        assert not (tmp_path / "same.jpg.temp").exists()
        loader.context.get_raw = lambda _: pytest.fail("Existing destination must not transfer again")
        assert not loader.download_pic(str(destination), "https://offline.invalid/other.jpg", datetime.now())
        assert stats.snapshot()["files"] == 1 and stats.snapshot()["peak_transfers"] == 1
        assert destination.with_suffix(".jpg").read_bytes() == b"original"
        class Broken:
            def read(self, *_):
                raise requests.exceptions.ChunkedEncodingError("must-not-log-signed-url")
        failed = requests.Response()
        failed.raw = Broken()
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            loader.context.write_raw(failed, str(tmp_path / "failed.mp4"))
        assert not (tmp_path / "failed.mp4").exists()
        assert stats.snapshot()["files"] == 1 and stats.snapshot()["failed_requests"] == 1
        assert stats.snapshot()["active_transfers"] == 0
    finally:
        engine.close()


def test_diagnostics_redaction_never_exposes_credentials_or_signed_urls():
    text = safe_activity("https://cdn.invalid/image.jpg?signature=secret sessionid=abc token=def password=xyz")
    assert "cdn.invalid" not in text and "secret" not in text and "abc" not in text and "xyz" not in text
    message = classify_error(RuntimeError("https://cdn.invalid/signed?token=secret")).message
    assert "secret" not in message and "cdn.invalid" not in message
    assert "secret" not in safe_activity('{"sessionid": "secret", "password": "secret"}')
    assert "secret" not in safe_activity("Authorization: Bearer secret")
