import threading

from PySide6.QtCore import QThread, Signal

import instaloader
from desktop.models import parse_profile
from desktop.services.engine import Cancelled, explain_error
from desktop.services.errors import SessionExpired, classify_error


class DownloadWorker(QThread):
    event = Signal(str, object)
    authentication = Signal(object)

    def __init__(self, engine, job, control, parent=None):
        super().__init__(parent)
        self.engine, self.job, self.control = engine, job, control
        self.outcome, self.detail = "Failed", "Download did not finish"
        self.failure_kind = ""
        self.auth_ready = threading.Event()
        self.auth_account = None

    def answer_auth(self, account=None):
        self.auth_account = account
        self.auth_ready.set()

    def ask_auth(self, request):
        self.auth_account = None
        self.auth_ready.clear()
        self.authentication.emit(request)
        while not self.auth_ready.wait(0.1):
            if self.control.cancelled.is_set():
                raise Cancelled()
        self.control.checkpoint(self.event.emit)
        return self.auth_account

    def run(self):
        safety = getattr(self.engine, "safety", None)
        observed_events = safety.events if safety else 0
        try:
            self.engine.auth_handler = self.ask_auth
            result = self.engine.run(self.job, self.control, self.event.emit)
            self.outcome, self.detail = "Completed", "Finished; existing media was kept"
            if result:
                self.outcome, self.detail = result["outcome"], result["detail"]
        except Cancelled:
            self.outcome, self.detail = "Cancelled", "Stopped at a safe boundary; retry skips existing media"
        except Exception as error:
            safety = getattr(self.engine, "safety", None)
            if safety:
                safety.inspect_exception(error, self.event.emit, observed_events)
            failure = classify_error(error, authenticated=bool(getattr(self.engine, "current_account", getattr(self.engine, "account", ""))))
            self.outcome, self.detail, self.failure_kind = "Failed", failure.message, failure.kind
        finally:
            self.event.emit("mode", "Public Mode — No Login")
            statistics = getattr(self.engine, "statistics", None)
            if statistics:
                statistics.publish(self.event.emit, force=True)


class AccountWorker(QThread):
    status = Signal(str)
    two_factor = Signal()

    def __init__(self, sessions, username, password=None, parent=None, loader_factory=instaloader.Instaloader):
        super().__init__(parent)
        self.sessions, self.username, self.password = sessions, parse_profile(username), password
        self.loader_factory = loader_factory
        self.success = False
        self.message = ""
        self.code = None
        self.ready = threading.Event()

    def submit_code(self, code):
        self.code = code
        self.ready.set()

    def run(self):
        loader = None
        try:
            loader = self.loader_factory(quiet=True, request_timeout=30, max_connection_attempts=1,
                                         fatal_status_codes=[401, 403, 429])
            loader.context.log = lambda *args, **kwargs: None
            def account_error(message, **kwargs):
                raise instaloader.ConnectionException(str(message))
            loader.context.error = account_error
            if self.password is None:
                loader.load_session(self.username, self.sessions.load(self.username))
            else:
                try:
                    loader.login(self.username, self.password)
                except instaloader.TwoFactorAuthRequiredException:
                    self.password = None
                    self.two_factor.emit()
                    if not self.ready.wait(180) or not self.code:
                        raise ValueError("Two-factor login cancelled or timed out.")
                    loader.two_factor_login(self.code)
            self.password = self.code = None
            if loader.test_login() != self.username:
                raise SessionExpired()
            self.sessions.save(self.username, loader.save_session())
            self.success = True
            self.message = f"@{self.username}: session verified and saved locally"
        except Exception as error:
            self.message = explain_error(error)
        finally:
            self.password = self.code = None
            if loader:
                loader.close()
