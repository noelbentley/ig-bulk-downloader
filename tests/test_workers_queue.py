import threading
import time

from PySide6.QtCore import QThread
import pytest

from desktop.models import DownloadOptions
from desktop.services.engine import Cancelled, Control, explain_error
from desktop.services.queue import QueueManager
from desktop.storage.local import LocalStore
from desktop.workers.tasks import AccountWorker
import instaloader


def spin(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        app.processEvents()
        if time.monotonic() > deadline:
            pytest.fail("Qt worker did not complete within the test deadline")
        time.sleep(0.005)
    app.processEvents()


class FakeEngine:
    calls = []
    threads = []
    fail = False

    def __init__(self, *_, **kwargs):
        self.closed = False

    def run(self, job, control, report):
        self.calls.append(job.username)
        self.threads.append(QThread.currentThread())
        control.checkpoint(report)
        report("files", 2)
        report("items", 1)
        if self.fail:
            raise instaloader.ProfileNotExistsException()

    def close(self):
        self.closed = True


def test_queue_sequential_workers_and_history(app, tmp_path):
    FakeEngine.calls, FakeEngine.threads, FakeEngine.fail = [], [], False
    store = LocalStore(tmp_path)
    manager = QueueManager(store, None, FakeEngine)
    manager.add("one", str(tmp_path), DownloadOptions())
    manager.add("two", str(tmp_path), DownloadOptions())
    with pytest.raises(ValueError):
        manager.add("one", str(tmp_path), DownloadOptions())
    signal_threads = []
    manager.changed.connect(lambda: signal_threads.append(QThread.currentThread()))
    manager.start()
    spin(app, lambda: not manager.running and manager.worker is None)
    assert FakeEngine.calls == ["one", "two"]
    assert all(thread is not app.thread() for thread in FakeEngine.threads)
    assert all(thread is app.thread() for thread in signal_threads)
    assert [j.status for j in manager.jobs] == ["Completed", "Completed"]
    assert len(store.history()) == 2
    assert manager.jobs[0].files == 2
    manager.retry(manager.jobs[0])
    assert manager.jobs[0].status == "Queued"
    manager.cancel(manager.jobs[0])
    assert manager.jobs[0].status == "Cancelled"
    store.close()


def test_failure_stops_remaining_queue(app, tmp_path):
    class FailingEngine(FakeEngine):
        def run(self, *args):
            raise instaloader.TooManyRequestsException()
    store = LocalStore(tmp_path)
    manager = QueueManager(store, None, FailingEngine)
    manager.add("one", str(tmp_path), DownloadOptions())
    manager.add("two", str(tmp_path), DownloadOptions())
    manager.start()
    spin(app, lambda: not manager.running and manager.worker is None)
    assert [j.status for j in manager.jobs] == ["Failed", "Queued"]
    assert "rate limit" in manager.jobs[0].detail
    store.close()


def test_stop_cancels_worker_and_leaves_pending_jobs(app, tmp_path):
    started = threading.Event()
    class WaitingEngine(FakeEngine):
        def run(self, job, control, report):
            started.set()
            while not control.cancelled.wait(0.01):
                control.checkpoint(report)
            control.checkpoint(report)
    store = LocalStore(tmp_path)
    manager = QueueManager(store, None, WaitingEngine)
    manager.add("one", str(tmp_path), DownloadOptions())
    manager.add("two", str(tmp_path), DownloadOptions())
    manager.start()
    assert started.wait(1)
    manager.pause()
    spin(app, lambda: manager.current.status == "Paused")
    manager.stop()
    spin(app, lambda: manager.worker is None)
    assert [j.status for j in manager.jobs] == ["Cancelled", "Queued"]
    store.close()


def test_pause_resume_and_cancel():
    control = Control()
    control.resumed.clear()
    completed, paused = threading.Event(), threading.Event()
    def execute():
        control.checkpoint(lambda *_: paused.set())
        completed.set()
    thread = threading.Thread(target=execute)
    thread.start()
    assert paused.wait(1)
    assert not completed.is_set()
    control.resumed.set()
    thread.join(1)
    assert completed.is_set()
    control.cancel()
    with pytest.raises(Cancelled):
        control.checkpoint()


@pytest.mark.parametrize("error,text", [(instaloader.LoginRequiredException(), "session"),
    (instaloader.PrivateProfileNotFollowedException(), "Private"),
    (instaloader.TooManyRequestsException(), "rate limit"),
    (instaloader.AbortDownloadException(), "security"),
    (PermissionError("denied"), "Local file")])
def test_error_messages(error, text):
    assert text in explain_error(error)


def test_account_two_factor_never_saves_password(app, tmp_path):
    class Sessions:
        def save(self, username, cookies):
            self.saved = (username, cookies)
    class Loader:
        def __init__(self, **_):
            self.context = type("Context", (), {})()
        def login(self, username, password):
            assert password == "test-only-password"
            raise instaloader.TwoFactorAuthRequiredException()
        def two_factor_login(self, code):
            assert code == "123456"
        def test_login(self):
            return "example"
        def save_session(self):
            return {"csrftoken": "fake"}
        def close(self):
            pass
    sessions = Sessions()
    worker = AccountWorker(sessions, "example", "test-only-password", loader_factory=Loader)
    worker.two_factor.connect(lambda: worker.submit_code("123456"))
    worker.start()
    spin(app, lambda: not worker.isRunning())
    assert worker.success
    assert worker.password is worker.code is None
    assert sessions.saved == ("example", {"csrftoken": "fake"})
