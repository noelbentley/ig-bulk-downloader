from dataclasses import asdict, replace
from datetime import datetime, timezone
import io
from pathlib import Path
from types import SimpleNamespace

import instaloader
import pytest
import requests

from desktop.models import DownloadOptions, Job
from desktop.services.control import Control
from desktop.services.engine import EngineService
from desktop.services.errors import SessionExpired, classify_error
from desktop.services.pacing import PacingConfig
from desktop.services.public_first import PublicFirstService
from desktop.services.queue import QueueManager
from desktop.storage.local import LocalStore
from desktop.storage.sessions import SessionStore
from desktop.ui.window import MainWindow
from desktop.workers.tasks import AccountWorker
from test_engine_ui import FakeLoader, profile
from test_workers_queue import spin


NO_PACING = PacingConfig(enabled=False)


class Sessions:
    def __init__(self):
        self.loads = []
    def load(self, account):
        self.loads.append(account)
        return {"csrftoken": "csrf-" + account, "sessionid": "session-" + account}


class AuthLoader(FakeLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context.username = None
        self.context.cookies = {}
        self.closed = False
    def load_session(self, account, cookies):
        self.context.username = account
        self.context.cookies = cookies.copy()
    def test_login(self):
        return self.context.username
    def close(self):
        self.closed = True


def service_factory(sessions=None, profile_factory=None, loader_factory=AuthLoader, pacing=NO_PACING, account=""):
    observations = []
    def lookup(context, username):
        observations.append((username, context.username, dict(context.cookies)))
        if profile_factory:
            return profile_factory(context, username)
        return profile()
    def engine_factory(sessions, account, **kwargs):
        return EngineService(sessions, account, loader_factory=loader_factory,
                             profile_factory=lookup, **kwargs)
    service = PublicFirstService(sessions or Sessions(), account, pacing=pacing, engine_factory=engine_factory)
    return service, observations


def public_options(**kwargs):
    return DownloadOptions(profile_picture=False, **kwargs)


def test_public_profiles_download_without_loading_available_sessions(tmp_path):
    sessions = Sessions()
    service, observations = service_factory(sessions)
    service.auth_handler = lambda _: pytest.fail("Public content must not request authentication")
    events = []
    try:
        result = service.run(Job("public", str(tmp_path), public_options(reels=True)), Control(),
                             lambda *args: events.append(args))
        assert result is None
        assert sessions.loads == []
        assert observations == [("public", None, {})]
        assert ("files", 2) in events
        assert list(service.contexts) == [""]
        assert (tmp_path / "public/photo.jpg").exists()
        assert (tmp_path / "public/video.mp4").exists()
        assert not (tmp_path / "@public/reels/video.mp4").exists()  # Feed/Reels deduplication survives section splitting.
    finally:
        service.close()


@pytest.mark.parametrize("field,label", [("stories", "Stories"), ("highlights", "Highlights")])
def test_declining_restricted_content_preserves_public_files_and_counts(tmp_path, field, label):
    service, _ = service_factory()
    prompts, events = [], []
    service.auth_handler = lambda request: prompts.append(request)
    try:
        result = service.run(Job("public", str(tmp_path), public_options(**{field: True})), Control(),
                             lambda *args: events.append(args))
        assert [request["content"] for request in prompts] == [label]
        assert prompts[0]["required"] is True
        assert result["outcome"] == "Completed" and label in result["detail"]
        assert max(value for kind, value in events if kind == "files") == 2
        assert (tmp_path / "public/photo.jpg").exists()
        assert service.sessions.loads == []
    finally:
        service.close()


def test_restricted_only_declined_without_any_instagram_request(tmp_path):
    service, observations = service_factory()
    service.auth_handler = lambda _: None
    try:
        result = service.run(Job("private", str(tmp_path), public_options(photos=False, videos=False, stories=True)),
                             Control(), lambda *_: None)
        assert result["outcome"] == "Skipped"
        assert service.contexts == {} and observations == []
    finally:
        service.close()


def private_profile(context, username):
    result = profile()
    if username == "private":
        result.is_private = True
        result.followed_by_viewer = context.username == "allowed"
    return result


def test_private_profile_prompts_only_after_anonymous_lookup_and_skip_continues_next_profile(tmp_path):
    service, observations = service_factory(profile_factory=private_profile)
    requests_seen = []
    def decline(request):
        assert observations == [("private", None, {})]
        requests_seen.append(request)
        return None
    service.auth_handler = decline
    try:
        result = service.run(Job("private", str(tmp_path), public_options(stories=True)), Control(), lambda *_: None)
        assert result["outcome"] == "Skipped"
        assert [request["content"] for request in requests_seen] == ["private profile"]
        service.run(Job("public", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert observations[-1] == ("public", None, {})
        assert service.sessions.loads == []
    finally:
        service.close()


def test_private_permission_and_return_to_public_after_single_profile_consent(tmp_path):
    sessions = Sessions()
    service, observations = service_factory(sessions, private_profile)
    prompts = []
    service.auth_handler = lambda request: prompts.append(request) or "allowed"
    try:
        service.run(Job("private", str(tmp_path), public_options(stories=True, highlights=True)), Control(), lambda *_: None)
        assert len(prompts) == 1  # One approval covers the selected content of this private profile only.
        assert sessions.loads == ["allowed"]
        assert observations[:2] == [("private", None, {}), ("private", "allowed", {"csrftoken": "csrf-allowed", "sessionid": "session-allowed"})]
        service.run(Job("public", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert observations[-1] == ("public", None, {})
        assert service.contexts[""].loader is not service.contexts["allowed"].loader
    finally:
        service.close()


def test_pacing_continues_across_public_and_authenticated_profiles(tmp_path):
    from test_pacing import NoWait
    pacing = PacingConfig(batch_min=3, batch_max=3, pause_min=15, pause_max=15, long_rest_enabled=False)
    service, _ = service_factory(profile_factory=private_profile, pacing=pacing)
    service.auth_handler = lambda _: "allowed"
    controls = []
    try:
        for username in ("public_one", "private", "public_two"):
            control = NoWait()
            controls.append(control)
            service.run(Job(username, str(tmp_path), public_options()), control, lambda *_: None)
        assert controls[0].waits == []
        assert controls[1].waits == controls[2].waits == [(15, "Batch pause")]
        assert service.pacer.long_count == 6
        assert service.contexts[""].pacer is service.contexts["allowed"].pacer
        assert service.sessions.loads == ["allowed"]
    finally:
        service.close()


def test_authenticated_non_follower_is_not_offered_more_login_attempts(tmp_path):
    service, _ = service_factory(profile_factory=private_profile)
    prompts = []
    service.auth_handler = lambda request: prompts.append(request) or "notallowed"
    try:
        with pytest.raises(instaloader.PrivateProfileNotFollowedException) as caught:
            service.run(Job("private", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert len(prompts) == 1
        assert "Login does not grant access" in classify_error(caught.value, authenticated=True).message
    finally:
        service.close()


def test_optional_authenticated_retry_does_not_change_subsequent_public_jobs(tmp_path):
    def lookup(context, username):
        if username == "limited" and context.username is None:
            raise instaloader.ProfileNotExistsException()
        return profile()
    service, observations = service_factory(profile_factory=lookup)
    prompts = []
    service.auth_handler = lambda request: prompts.append(request) or "allowed"
    try:
        service.run(Job("limited", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert prompts[0]["required"] is False
        assert "optional" in prompts[0]["reason"]
        service.run(Job("public", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert observations[-1] == ("public", None, {})
    finally:
        service.close()


@pytest.mark.parametrize("error,category", [(instaloader.QueryReturnedBadRequestException(), "restricted"),
    (instaloader.QueryReturnedForbiddenException(), "restricted"),
    (instaloader.AbortDownloadException('responded with "429 Too Many Requests"'), "rate_limit"),
    (instaloader.AbortDownloadException('responded with "403 Forbidden"'), "restricted"),
    (instaloader.ConnectionException("offline"), "network"),
    (requests.exceptions.Timeout(), "network")])
def test_errors_do_not_offer_authentication_for_rate_security_or_network_failure(tmp_path, error, category):
    def lookup(*_):
        raise error
    service, _ = service_factory(profile_factory=lookup)
    service.auth_handler = lambda _: pytest.fail("This error must not prompt for authentication")
    try:
        with pytest.raises(type(error)):
            service.run(Job("public", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert classify_error(error).kind == category
        assert service.sessions.loads == []
    finally:
        service.close()


def test_expired_saved_session_is_closed_and_not_reused(tmp_path):
    class ExpiredLoader(AuthLoader):
        def test_login(self):
            return None
    service, _ = service_factory(loader_factory=ExpiredLoader)
    prompts = []
    service.auth_handler = lambda request: prompts.append(request) or "expired"
    try:
        with pytest.raises(SessionExpired):
            service.run(Job("public", str(tmp_path), public_options(photos=False, videos=False, stories=True)),
                         Control(), lambda *_: None)
        assert len(prompts) == 2  # Initial consent, then one session-expired recovery choice.
        assert "expired" in prompts[-1]["reason"]
        assert "expired" not in service.contexts
        assert service.sessions.loads == ["expired", "expired"]
    finally:
        service.close()


def test_known_expired_account_is_not_automatically_loaded_for_later_jobs(tmp_path):
    class ExpiredLoader(AuthLoader):
        def test_login(self):
            return None
    sessions = Sessions()
    service, _ = service_factory(sessions, loader_factory=ExpiredLoader, account="expired")
    prompts = []
    service.auth_handler = lambda request: prompts.append(request)
    try:
        for username in ("first", "second"):
            result = service.run(Job(username, str(tmp_path), public_options()), Control(), lambda *_: None)
            assert result["outcome"] == "Skipped"
        assert sessions.loads == ["expired"]
        assert len(prompts) == 2
        assert all("expired" in prompt["reason"] for prompt in prompts)
    finally:
        service.close()


def test_separate_real_instaloader_cookie_jars_and_headers_for_each_identity(tmp_path):
    observations = []
    def loader_factory(**kwargs):
        loader = instaloader.Instaloader(**kwargs)
        loader.test_login = lambda: loader.context.username
        loader.download_post = lambda *args, **kwargs: False
        return loader
    def lookup(context, username):
        observations.append((context, context._session, context._session.cookies.get_dict().copy(), dict(context._session.headers)))
        return profile()
    def engine_factory(sessions, account, **kwargs):
        return EngineService(sessions, account, loader_factory=loader_factory, profile_factory=lookup, **kwargs)
    sessions = Sessions()
    service = PublicFirstService(sessions, pacing=NO_PACING, engine_factory=engine_factory)
    try:
        service.run(Job("public_one", str(tmp_path), public_options()), Control(), lambda *_: None)
        service.account = "alice"  # Explicit authenticated operation, as selected in the GUI.
        service.run(Job("public_two", str(tmp_path), public_options()), Control(), lambda *_: None)
        service.account = "bob"
        service.run(Job("public_three", str(tmp_path), public_options()), Control(), lambda *_: None)
        service.account = ""
        service.run(Job("public_four", str(tmp_path), public_options()), Control(), lambda *_: None)
        assert observations[0][1] is observations[3][1]
        assert len({id(entry[1]) for entry in observations}) == 3
        assert not observations[0][2].get("sessionid") and not observations[3][2].get("sessionid")
        assert observations[0][0].username is None and observations[3][0].username is None
        assert observations[1][2]["sessionid"] == "session-alice"
        assert observations[2][2]["sessionid"] == "session-bob"
        assert observations[1][3]["X-CSRFToken"] == "csrf-alice"
        assert observations[3][3].get("X-CSRFToken") not in ("csrf-alice", "csrf-bob")
        assert sessions.loads == ["alice", "bob"]
    finally:
        service.close()


@pytest.mark.parametrize("layout", ["flat", "legacy"])
def test_real_download_disables_text_and_metadata_even_when_legacy_options_request_it(tmp_path, layout):
    post = SimpleNamespace(shortcode="post", typename="GraphImage", is_video=False,
                           date_local=datetime.now(), caption="A saved caption", url="https://test.invalid/image.jpg")
    def loader_factory(**kwargs):
        loader = instaloader.Instaloader(**kwargs)
        loader.filename_pattern = "{shortcode}"
        def response(url):
            result = requests.Response()
            result.status_code = 200
            result.headers["Content-Type"] = "image/jpeg"
            result.raw = io.BytesIO(b"original image")
            return result
        loader.context.get_raw = response
        return loader
    p = profile()
    p.get_posts = lambda: [post]
    options = public_options(metadata=True)
    engine = EngineService(loader_factory=loader_factory, profile_factory=lambda *_: p, pacing=NO_PACING)
    try:
        options.metadata = True  # Even callers bypassing migration cannot turn engine exports back on.
        options.captions = True
        job = Job("public", str(tmp_path), options, layout=layout)
        engine.run(job, Control(), lambda *_: None)
        assert (job.media_folder("posts") / "post.jpg").exists()
        assert not list(tmp_path.rglob("*.txt"))
        assert not list(tmp_path.rglob("*.json")) and not list(tmp_path.rglob("*.json.xz"))
        assert engine.loader.save_metadata is False
    finally:
        engine.close()


def queue_factory(sessions, account, **kwargs):
    def engine_factory(sessions, account, **options):
        return EngineService(sessions, account, loader_factory=AuthLoader,
                             profile_factory=private_profile, **options)
    return PublicFirstService(sessions, account, engine_factory=engine_factory, **kwargs)


def test_qt_private_skip_continues_queue_and_history_remains_accurate(app, tmp_path):
    store = LocalStore(tmp_path)
    store.settings["pacing"] = asdict(NO_PACING)
    manager = QueueManager(store, Sessions(), queue_factory)
    manager.add("private", str(tmp_path), public_options())
    manager.add("public", str(tmp_path), public_options())
    prompts = []
    def decline(request):
        prompts.append(request)
        manager.respond_auth(None, request)
    manager.authentication_required.connect(decline)
    manager.start()
    spin(app, lambda: manager.worker is None)
    assert [job.status for job in manager.jobs] == ["Skipped", "Completed"]
    assert [job.files for job in manager.jobs] == [0, 2]
    assert len(prompts) == 1
    assert [row[3] for row in store.history()] == ["Completed", "Skipped"]
    store.close()


def test_gui_saved_session_consent_and_public_status_after_job(app, tmp_path):
    store = LocalStore(tmp_path)
    store.settings["pacing"] = asdict(NO_PACING)
    SessionStore(tmp_path).save("allowed", {"csrftoken": "test", "sessionid": "offline"})
    store.settings["account"] = "allowed"
    window = MainWindow(store)
    window.manager.engine_factory = queue_factory
    window.manager.add("private", str(tmp_path), public_options())
    window.manager.add("public", str(tmp_path), public_options())
    assert window.auth_status.text() == "Public Mode — No Login"
    assert window.account.currentData() == "" and not window.use_session.isChecked()
    modes = []
    window.manager.mode_changed.connect(modes.append)
    window.start_downloads()
    spin(app, lambda: window.auth_dialog is not None)
    assert window.auth_dialog.saved.isEnabled()
    assert window.auth_dialog.login.text() == "Login with Instagram"
    assert window.auth_dialog.skip.text() == "Skip This Content"
    window.auth_dialog.saved.click()
    spin(app, lambda: window.manager.worker is None)
    assert [job.status for job in window.manager.jobs] == ["Completed", "Completed"]
    assert "Authenticated — @allowed" in modes
    assert modes[-1] == "Public Mode — No Login"
    assert not window.use_session.isChecked()
    window.close()
    store.close()


def test_gui_login_choice_waits_for_explicit_sign_in_then_resumes(app, tmp_path, monkeypatch):
    from functools import partial
    import desktop.ui.window as ui_module
    class LoginLoader(AuthLoader):
        def login(self, username, password):
            assert username == "allowed" and password == "offline-test-password"
            self.context.username = username
        def save_session(self):
            return {"csrftoken": "offline", "sessionid": "offline-session"}
    monkeypatch.setattr(ui_module, "AccountWorker", partial(AccountWorker, loader_factory=LoginLoader))
    store = LocalStore(tmp_path)
    store.settings["pacing"] = asdict(NO_PACING)
    window = MainWindow(store)
    window.manager.engine_factory = queue_factory
    window.manager.add("private", str(tmp_path), public_options())
    window.start_downloads()
    spin(app, lambda: window.auth_dialog is not None)
    assert not window.auth_dialog.saved.isEnabled()
    window.auth_dialog.login.click()
    assert window.tabs.currentIndex() == 2 and window.login_button.isEnabled()
    assert window.pending_login_request is not None
    window.login_user.setText("allowed")
    window.password.setText("offline-test-password")
    window.login()
    spin(app, lambda: window.manager.worker is None and window.account_worker is None)
    assert window.manager.jobs[0].status == "Completed"
    assert window.password.text() == ""
    assert window.sessions.accounts() == ["allowed"]
    assert window.auth_status.text() == "Public Mode — No Login"
    window.close()
    store.close()


def test_cancel_while_waiting_for_consent_closes_prompt_and_preserves_queue(app, tmp_path):
    store = LocalStore(tmp_path)
    store.settings["pacing"] = asdict(NO_PACING)
    window = MainWindow(store)
    window.manager.engine_factory = queue_factory
    job = window.manager.add("private", str(tmp_path), public_options())
    window.start_downloads()
    spin(app, lambda: window.auth_dialog is not None)
    window.manager.stop()
    spin(app, lambda: window.manager.worker is None)
    assert job.status == "Cancelled" and window.auth_dialog is None
    window.close()
    store.close()
