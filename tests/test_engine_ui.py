from pathlib import Path
from types import SimpleNamespace

import instaloader
import pytest

from desktop.models import DownloadOptions, Job
from desktop.services.engine import Cancelled, Control, EngineService
from desktop.storage.local import LocalStore
from desktop.ui.window import MainWindow


class FakeLoader:
    def __init__(self, **kwargs):
        self.context = SimpleNamespace(log=lambda *_: None, error=lambda *_: None, write_raw=self.write)
        self.post_calls = []
        self.story_calls = []

    def write(self, data, filename):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(data)

    def download_post(self, post, target):
        self.post_calls.append(post.shortcode)
        extension = ".mp4" if post.typename == "GraphVideo" else ".jpg"
        filename = Path(self.dirname_pattern.format(target=target)) / (post.shortcode + extension)
        if not filename.exists():
            self.context.write_raw(b"offline mock media", str(filename))

    def download_profilepic(self, profile):
        pass

    def get_stories(self, users):
        return [SimpleNamespace(get_items=lambda: [SimpleNamespace(mediaid="story")])]

    def get_highlights(self, profile):
        return [SimpleNamespace(unique_id="123", get_items=lambda: [SimpleNamespace(mediaid="highlight")])]

    def download_storyitem(self, item, target):
        self.story_calls.append(target)

    def close(self):
        pass


def profile():
    photo = SimpleNamespace(shortcode="photo", typename="GraphImage")
    video = SimpleNamespace(shortcode="video", typename="GraphVideo")
    return SimpleNamespace(is_private=False, userid=123, get_posts=lambda: [photo, video],
                           get_reels=lambda: [video])


@pytest.mark.parametrize("layout", ["flat", "legacy"])
def test_engine_creates_destination_preserves_files_and_reports(tmp_path, layout):
    engine = EngineService(loader_factory=FakeLoader, profile_factory=lambda *_: profile())
    job = Job("example", str(tmp_path / "{archive}"), DownloadOptions(profile_picture=False, reels=True), layout=layout)
    events = []
    engine.run(job, Control(), lambda *args: events.append(args))
    assert job.folder.is_dir()
    assert engine.loader.post_calls == ["photo", "video"]
    assert ("files", 2) in events
    path = job.media_folder("posts") / "photo.jpg"
    path.write_bytes(b"previously saved")
    events.clear()
    engine.run(job, Control(), lambda *args: events.append(args))
    assert path.read_bytes() == b"previously saved"
    assert not any(kind == "files" for kind, _ in events)
    engine.close()


def test_reels_are_not_lost_when_feed_videos_disabled(tmp_path):
    engine = EngineService(loader_factory=FakeLoader, profile_factory=lambda *_: profile())
    job = Job("example", str(tmp_path), DownloadOptions(videos=False, reels=True, profile_picture=False))
    engine.run(job, Control(), lambda *_: None)
    assert engine.loader.post_calls == ["photo", "video"]
    assert (job.media_folder("reels") / "video.mp4").exists()


def test_stories_require_account_before_network(tmp_path):
    engine = EngineService(loader_factory=lambda **_: pytest.fail("Must not create loader"))
    with pytest.raises(instaloader.LoginRequiredException):
        engine.run(Job("example", str(tmp_path), DownloadOptions(stories=True)), Control(), lambda *_: None)


def test_private_profile_access_and_own_account(tmp_path):
    private = profile()
    private.is_private, private.followed_by_viewer = True, False
    engine = EngineService(loader_factory=FakeLoader, profile_factory=lambda *_: private)
    with pytest.raises(instaloader.PrivateProfileNotFollowedException):
        engine.run(Job("example", str(tmp_path)), Control(), lambda *_: None)
    engine.account = "example"
    engine.run(Job("example", str(tmp_path)), Control(), lambda *_: None)


def test_authenticated_stories_and_highlights(tmp_path):
    engine = EngineService(account="example", profile_factory=lambda *_: profile())
    engine.loader = FakeLoader()
    options = DownloadOptions(photos=False, videos=False, stories=True, highlights=True, profile_picture=False)
    engine.run(Job("example", str(tmp_path), options), Control(), lambda *_: None)
    assert engine.loader.story_calls == ["stories", "highlights/123"]


def test_expired_session_stops_before_profile_lookup(tmp_path):
    class ExpiredLoader(FakeLoader):
        def load_session(self, *args):
            pass
        def test_login(self):
            return None
    engine = EngineService(SimpleNamespace(load=lambda _: {}), "example", loader_factory=ExpiredLoader,
                           profile_factory=lambda *_: pytest.fail("Must not request private profile with expired session"))
    with pytest.raises(instaloader.LoginRequiredException):
        engine.run(Job("example", str(tmp_path)), Control(), lambda *_: None)


def test_cancel_at_boundary(tmp_path):
    engine = EngineService(loader_factory=FakeLoader, profile_factory=lambda *_: profile())
    control = Control()
    def report(kind, value):
        if kind == "items":
            control.cancel()
    with pytest.raises(Cancelled):
        engine.run(Job("example", str(tmp_path), DownloadOptions(profile_picture=False)), control, report)
    assert engine.loader.post_calls == ["photo"]


def test_native_instaloader_existing_file_check(tmp_path):
    # Verify the actual engine's check too, with all network access blocked.
    loader = instaloader.Instaloader(quiet=True)
    base = tmp_path / "existing"
    base.with_suffix(".jpg").write_bytes(b"original")
    from datetime import datetime
    assert loader.download_pic(str(base), "https://example.invalid/test.jpg", datetime.now()) is False
    assert base.with_suffix(".jpg").read_bytes() == b"original"
    loader.close()


def test_window_queue_preferences_history_and_close(app, tmp_path):
    store = LocalStore(tmp_path)
    window = MainWindow(store)
    window.show()
    app.processEvents()
    window.profile.setText("https://instagram.com/example/")
    window.destination.setText(str(tmp_path / "archive"))
    window.add_profile()
    assert window.queue_table.rowCount() == 1
    assert window.manager.jobs[0].username == "example"
    window.theme.setCurrentText("Light")
    window.tabs.setCurrentIndex(2)
    window.manager.cancel(window.manager.jobs[0])
    window.load_history()
    assert window.history_table.rowCount() == 1
    window.close()
    assert store.settings["theme"] == "Light"
    store.close()
