from pathlib import Path
import os

import pytest

from desktop.models import DownloadOptions, Job, parse_profile
from desktop.storage.local import LocalStore
from desktop.storage.sessions import SessionStore


@pytest.mark.parametrize("value,expected", [(" @Example_1 ", "example_1"),
    ("https://www.instagram.com/Some.Name/?igsh=123", "some.name"),
    ("instagram.com/example", "example"), ("a", "a")])
def test_parse(value, expected):
    assert parse_profile(value) == expected


@pytest.mark.parametrize("value", ["", "../escape", "a..b", "trailing.", "a" * 31,
    "https://instagram.com.evil.com/user", "https://instagram.com/p/123",
    "https://evil.com/name", "https://instagram.com/reels", "file://instagram.com/user",
    "https://person@instagram.com/user", "https://instagram.com:1234/user", "x y"])
def test_parse_rejects(value):
    with pytest.raises(ValueError):
        parse_profile(value)


def test_settings_queue_history_roundtrip(tmp_path):
    store = LocalStore(tmp_path)
    store.settings.update(destination=str(tmp_path / "archive"), theme="Light")
    store.save_settings()
    job = Job("example", str(tmp_path), status="Downloading", files=3)
    store.save_queue([job])
    store.record(job)
    store.close()
    reopened = LocalStore(tmp_path)
    assert reopened.settings["theme"] == "Light"
    assert reopened.load_queue()[0].status == "Queued"
    assert reopened.load_queue()[0].files == 3
    assert reopened.history()[0][2] == "example"
    reopened.close()


def test_corrupt_settings_recovery(tmp_path):
    (tmp_path / "settings.json").write_text("{bad", encoding="utf-8")
    store = LocalStore(tmp_path)
    assert store.warning
    assert store.settings["theme"] == "Dark"
    store.close()


def test_empty_selection_and_reserved_device_name(tmp_path):
    with pytest.raises(ValueError):
        DownloadOptions(**dict.fromkeys(DownloadOptions.__dataclass_fields__, False)).validate()
    assert Job("con", str(tmp_path)).folder.name == "@con"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_session_protected_roundtrip_logout(tmp_path):
    sessions = SessionStore(tmp_path)
    cookies = {"csrftoken": "offline-test", "sessionid": "fake-cookie-not-a-real-session"}
    sessions.save("example", cookies)
    assert b"fake-cookie" not in sessions.path("example").read_bytes()
    assert sessions.load("example") == cookies
    assert sessions.accounts() == ["example"]
    sessions.delete("example")
    assert sessions.accounts() == []
    sessions.delete("example")
    with pytest.raises(FileNotFoundError):
        sessions.load("example")
