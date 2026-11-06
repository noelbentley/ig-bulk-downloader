from PySide6.QtCore import QObject, QTimer, Signal
from contextlib import nullcontext

from desktop.models import Job
from desktop.services.engine import Control, EngineService
from desktop.services.public_first import PublicFirstService
from desktop.services.pacing import PacingConfig
from desktop.services.performance import PerformanceConfig, RunStatistics
from desktop.services.pipeline import PipelineConfig
from desktop.services.safety import RestrictionGate
from desktop.services.errors import safe_activity
from desktop.services.performance import diagnostic_report
from desktop.storage.local import atomic_json
from desktop.workers.tasks import DownloadWorker


class QueueManager(QObject):
    changed = Signal()
    activity = Signal(str)
    idle = Signal()
    authentication_required = Signal(object)
    mode_changed = Signal(str)
    performance_updated = Signal(object)
    performance_finished = Signal(object)

    def __init__(self, store, sessions, engine_factory=PublicFirstService, parent=None):
        super().__init__(parent)
        self.store, self.sessions, self.engine_factory = store, sessions, engine_factory
        self.jobs = store.load_queue()
        self.worker = self.current = self.engine = self.control = None
        self.running = False
        self.paused = False
        self.auth_pending = None
        self.control_factory = Control
        self.statistics = None
        self.safety = RestrictionGate(store.settings.get("restriction"))
        self.latest_activity = None
        self.mode = "Public Mode — No Login"
        self.ui_timer = QTimer(self)
        self.ui_timer.setSingleShot(True)
        self.ui_timer.setInterval(100)
        self.ui_timer.timeout.connect(self.flush_ui)

    def schedule_ui(self):
        if not self.ui_timer.isActive():
            self.ui_timer.start()

    def flush_ui(self):
        self.ui_timer.stop()
        if self.latest_activity is not None:
            self.activity.emit(self.latest_activity)
            self.latest_activity = None
        if self.statistics:
            self.performance_updated.emit(self.statistics.snapshot())
        self.changed.emit()

    def review_restrictions(self):
        self.safety.acknowledge_review()
        self.store.settings["restriction"] = self.safety.state()
        self.store.save_settings()
        self.changed.emit()

    def persist(self):
        self.store.save_queue(self.jobs)
        self.changed.emit()

    def add(self, username, destination, options):
        options.validate()
        if any(j.username == username and j.status in ("Queued", "Downloading", "Paused", "Pausing", "Batch pause", "Long rest", "Awaiting authentication", "Restriction cooldown") for j in self.jobs):
            raise ValueError(f"@{username} is already in the active queue.")
        job = Job(username, destination, options)
        self.jobs.append(job)
        self.persist()
        return job

    def start(self, account=""):
        if self.running or self.worker is not None:
            return
        if self.safety.review:
            self.activity.emit("Review Instagram restrictions in Settings before continuing. The cooldown will remain in effect.")
            return
        self.running, self.paused = True, False
        performance = PerformanceConfig(**self.store.settings["performance"])
        self.statistics = RunStatistics(performance.profile)
        # Snapshot settings once for this queue run; the engine/pacer spans every profile.
        factory = self.engine_factory
        pipeline = PipelineConfig(**self.store.settings["pipeline"])
        if pipeline.enabled and factory is PublicFirstService:
            from functools import partial
            from desktop.services.optimized import OptimizedEngineService
            factory = partial(PublicFirstService, engine_factory=partial(OptimizedEngineService, config=pipeline))
        self.engine = factory(self.sessions, account,
                                         pacing=PacingConfig(**self.store.settings["pacing"]))
        self.engine.statistics = self.statistics
        self.engine.safety = self.safety
        self._next()

    def _next(self):
        if self.worker is not None:
            return
        job = next((j for j in self.jobs if j.status == "Queued"), None) if self.running else None
        if job is None:
            self.running = False
            if self.engine:
                self.engine.close()
            self.engine = None
            self.current = self.worker = self.control = None
            if self.statistics:
                result = self.statistics.finish()
                self.store.settings["last_performance"] = result
                self.store.save_settings()
                atomic_json(self.store.root / "diagnostics" / "latest.json", diagnostic_report(result))
                self.performance_finished.emit(result)
            self.flush_ui()
            self.persist()
            self.idle.emit()
            return
        self.current = job
        job.status, job.items, job.files, job.detail = "Downloading", 0, 0, "Starting"
        self.control = self.control_factory()
        if self.paused:
            self.control.set_paused(True)
        self.worker = DownloadWorker(self.engine, job, self.control, self)
        self.worker.event.connect(self._event)
        self.worker.authentication.connect(self._authentication)
        self.worker.finished.connect(self._finished)
        self.persist()
        self.worker.start()

    def _event(self, kind, value):
        if kind == "mode":
            if value != self.mode:
                self.mode = str(value)
                self.mode_changed.emit(self.mode)
        elif kind == "activity":
            if value:
                self.current.detail = safe_activity(value)
                self.latest_activity = self.current.detail
        elif kind == "state":
            self.current.status = str(value)
        elif kind == "pacing":
            seconds = value["remaining"]
            prefix = "Paused · " if value["paused"] else ""
            message = f"{prefix}{value['kind']}: {seconds // 60:02}:{seconds % 60:02} remaining"
            self.current.detail = message
            self.latest_activity = message
        elif kind == "safety":
            self.store.settings["restriction"] = dict(value)
            self.store.save_settings()
        elif kind in ("files", "items"):
            setattr(self.current, kind, int(value))
        self.schedule_ui()

    def _authentication(self, request):
        if self.control.cancelled.is_set():
            self.worker.answer_auth()
            return
        self.auth_pending = dict(request, job_id=self.current.id, worker=self.worker)
        self.current.status = "Awaiting authentication"
        self.current.detail = request["reason"]
        self.changed.emit()
        self.authentication_required.emit(self.auth_pending)

    def respond_auth(self, account=None, request=None):
        pending = self.auth_pending
        if pending is None or (request is not None and request is not pending):
            return
        self.auth_pending = None
        pending["worker"].answer_auth(account)
        self.changed.emit()

    def _finished(self):
        worker = self.worker
        self.flush_ui()
        self.auth_pending = None
        self.current.status, self.current.detail = worker.outcome, worker.detail
        with self.statistics.measure("history_seconds") if self.statistics else nullcontext():
            self.store.record(self.current)
        self.activity.emit(f"@{self.current.username}: {worker.detail}")
        # Network, rate and security restrictions stop requests; access failures are scoped to one profile.
        if worker.outcome == "Failed" and worker.failure_kind not in ("private_not_followed", "session_expired", "authentication_required", "unavailable"):
            self.running = False
        self.persist()
        worker.deleteLater()
        self.worker = self.current = self.control = None
        self._next()

    def pause(self):
        if self.control:
            self.paused = not self.paused
            if self.paused:
                self.control.set_paused(True)
                self.current.status = "Pausing"
                self.activity.emit("Pause requested; the current item/request will finish first")
            else:
                self.control.set_paused(False)
                self.current.status = "Downloading"
            self.changed.emit()

    def cancel(self, job):
        if job is self.current and self.control:
            self.control.cancel()
            job.status = "Cancelling"
        elif job.status == "Queued":
            job.status, job.detail = "Cancelled", "Removed from pending downloads"
            self.store.record(job)
        self.persist()

    def stop(self):
        self.running = False
        if self.control:
            self.control.cancel()

    def retry(self, job):
        if job.status not in ("Failed", "Cancelled", "Completed", "Skipped"):
            return
        if any(j is not job and j.username == job.username and j.status == "Queued" for j in self.jobs):
            raise ValueError("This profile is already queued.")
        job.status, job.detail = "Queued", "Ready to retry; existing files will be skipped"
        self.persist()
