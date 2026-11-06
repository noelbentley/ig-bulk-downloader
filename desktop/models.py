from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4


def parse_profile(value: str) -> str:
    value = value.strip()
    if "://" in value or value.lower().startswith(("instagram.com/", "www.instagram.com/")):
        url = urlsplit(value if "://" in value else "https://" + value)
        parts = url.path.strip("/").split("/")
        if (url.scheme not in ("http", "https") or url.hostname not in
                ("instagram.com", "www.instagram.com") or url.username or url.password or
                url.port not in (None, 80, 443) or len(parts) != 1):
            raise ValueError("Enter an Instagram profile URL or username, not a post or reel URL.")
        value = parts[0]
    value = value.removeprefix("@").lower()
    if (not re.fullmatch(r"[a-z0-9_](?:[a-z0-9._]{0,28}[a-z0-9_])?", value)
            or ".." in value or value in {"p", "reel", "reels", "stories", "explore", "accounts", "direct"}):
        raise ValueError("Use a valid Instagram username (1–30 letters, numbers, dots or underscores).")
    return value


@dataclass
class DownloadOptions:
    photos: bool = True
    videos: bool = True
    reels: bool = False
    stories: bool = False
    highlights: bool = False
    captions: bool = True
    metadata: bool = False
    profile_picture: bool = True

    def __post_init__(self):
        self.metadata = False  # Keep loading older queue/history records without allowing JSON exports.

    def validate(self):
        if not any((self.photos, self.videos, self.reels, self.stories, self.highlights, self.profile_picture)):
            raise ValueError("Select at least one media type or profile picture.")


@dataclass
class Job:
    username: str
    destination: str
    options: DownloadOptions = field(default_factory=DownloadOptions)
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "Queued"
    items: int = 0
    files: int = 0
    detail: str = "Waiting to start"

    @property
    def folder(self):
        # Prefix prevents reserved Windows device names such as CON or AUX.
        return Path(self.destination) / ("@" + self.username)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["username"] = parse_profile(data["username"])
        data["options"] = DownloadOptions(**data["options"])
        job = cls(**data)
        if job.status in ("Downloading", "Pausing", "Paused", "Cancelling", "Batch pause", "Long rest", "Awaiting authentication", "Restriction cooldown"):
            job.status, job.detail = "Queued", "Interrupted last time; ready to retry safely"
        return job
