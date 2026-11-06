from dataclasses import asdict
from datetime import datetime
import io
import json
from types import SimpleNamespace

import instaloader
import pytest
import requests

from desktop.models import DownloadOptions, Job
from desktop.optimized_benchmark import run_case
from desktop.services.control import Control
from desktop.services.engine import EngineService
from desktop.services.pacing import PacingConfig
from desktop.storage.local import LocalStore
from desktop.ui.window import MainWindow
from test_engine_ui import FakeLoader, profile


def test_new_jobs_use_one_plain_username_folder_and_roundtrip(tmp_path):
    job = Job("Example", str(tmp_path))
    assert job.folder == tmp_path / "example"
    assert job.media_folder("posts") == job.media_folder("reels") == job.media_folder("highlights/1") == job.folder
    assert Job.from_dict(job.to_dict()).layout == "flat"
    assert Job.from_dict(job.to_dict()).folder == job.folder
    assert "{target}" not in job.dirname_pattern


@pytest.mark.parametrize("username", ["con", "aux", "lpt1", "com1.name"])
def test_reserved_windows_names_keep_a_safe_distinct_folder(tmp_path, username):
    assert Job(username, str(tmp_path)).folder.name == "@" + username
    assert Job("_" + username, str(tmp_path)).folder.name == "_" + username


def test_legacy_queue_and_history_paths_are_retained_and_files_untouched(tmp_path):
    archive = tmp_path / "archive"
    legacy_folder = archive / "@example" / "posts"
    legacy_folder.mkdir(parents=True)
    photo, caption = legacy_folder / "photo.jpg", legacy_folder / "photo.txt"
    photo.write_bytes(b"existing media")
    caption.write_text("existing caption", encoding="utf8")
    data = Job("example", str(archive), DownloadOptions(videos=False, profile_picture=False)).to_dict()
    data.pop("layout")
    data["options"].update(captions=True, metadata=True)
    store = LocalStore(tmp_path / "state")
    (store.root / "queue.json").write_text(json.dumps([data]), encoding="utf8")
    legacy = store.load_queue()[0]
    assert legacy.layout == "legacy" and legacy.media_folder("posts") == legacy_folder
    assert not legacy.options.captions and not legacy.options.metadata
    store.record(legacy)
    assert store.history()[0][6] == str(archive / "@example")
    assert Job.from_dict(json.loads(store.history()[0][8])).layout == "legacy"
    engine = EngineService(loader_factory=FakeLoader, profile_factory=lambda *_: profile(),
                           pacing=PacingConfig(enabled=False))
    try:
        events = []
        engine.run(legacy, Control(), lambda *args: events.append(args))
        assert not any(kind == "files" for kind, _ in events)
        new = Job("example", str(archive), DownloadOptions(videos=False, profile_picture=False))
        engine.run(new, Control(), lambda *_: None)
        assert (archive / "example" / "photo.jpg").exists()
        assert photo.read_bytes() == b"existing media"
        assert caption.read_text(encoding="utf8") == "existing caption"
    finally:
        engine.close()
        store.close()


def test_saved_caption_preference_is_disabled_and_gui_has_no_text_export(app, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"options": dict(asdict(DownloadOptions()), captions=True, metadata=True)}))
    store = LocalStore(tmp_path)
    window = MainWindow(store)
    try:
        assert not store.settings["options"]["captions"] and not store.settings["options"]["metadata"]
        assert "captions" not in window.options and "metadata" not in window.options
        assert not window.get_options().captions
    finally:
        window.close()
        store.close()


@pytest.mark.parametrize("optimized", [False, True])
def test_real_native_and_parallel_media_go_directly_in_username_folder(tmp_path, optimized):
    data, files = run_case(tmp_path, optimized, 3, True, 6, 0, 0)
    assert len(files) == data["files"] == 10
    assert all(len(path.replace("\\", "/").split("/")) == 2 for path in files)
    assert all(path.replace("\\", "/").startswith("fixture/") for path in files)
    assert not list(tmp_path.rglob("*.txt"))


def test_real_stories_highlights_and_profile_picture_share_folder_without_collisions(tmp_path):
    date = datetime(2026, 1, 1)
    photo = SimpleNamespace(shortcode="PHOTO", typename="GraphImage", is_video=False,
        date_utc=date, date_local=date, url="https://test.invalid/photo.jpg", caption="Must not be exported")
    story = SimpleNamespace(mediaid=123, is_video=False, date_utc=date, date_local=date,
        url="https://test.invalid/story.jpg", caption="Must not be exported")
    p = SimpleNamespace(is_private=False, userid=1, username="example", get_posts=lambda: [photo],
                        profile_pic_url="https://test.invalid/profile.jpg")
    def loader_factory(**kwargs):
        loader = instaloader.Instaloader(**kwargs)
        def response(url):
            result = requests.Response()
            result.status_code = 200
            result.headers.update({"Content-Type": "image/jpeg", "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"})
            result.raw = io.BytesIO(url.encode("utf8"))
            return result
        loader.context.get_raw = response
        loader.load_session = lambda *_: None
        loader.test_login = lambda: "approved"
        loader.get_stories = lambda *_: [SimpleNamespace(get_items=lambda: [story])]
        loader.get_highlights = lambda *_: [SimpleNamespace(unique_id="1", get_items=lambda: [story])]
        return loader
    engine = EngineService(SimpleNamespace(load=lambda _: {}), "approved", loader_factory=loader_factory,
                           profile_factory=lambda *_: p, pacing=PacingConfig(enabled=False))
    job = Job("example", str(tmp_path), DownloadOptions(stories=True, highlights=True))
    try:
        engine.run(job, Control(), lambda *_: None)
        files = list(job.folder.rglob("*"))
        assert len(files) == 3 and all(path.parent == job.folder for path in files)
        assert any("story_123" in path.name for path in files)
        assert len({path.read_bytes() for path in files}) == 3
        assert engine.loader.filename_pattern == "{date_utc}_UTC"
    finally:
        engine.close()
