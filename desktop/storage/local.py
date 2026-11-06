import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from desktop.models import DownloadOptions, Job
from desktop.services.pacing import PacingConfig
from desktop.services.performance import PerformanceConfig, preset_pacing
from dataclasses import asdict


def data_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "IG Bulk Downloader"


def default_destination() -> str:
    if os.name == "nt" and Path("D:/").is_dir():
        return "D:\\Instagram"
    if os.name == "nt":
        # Respect Windows' redirected Downloads known folder.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                return os.path.expandvars(value)
        except OSError:
            pass
    return str(Path.home() / "Downloads")


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class LocalStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_directory()
        self.root.mkdir(parents=True, exist_ok=True)
        self.warning = ""
        self.settings = {"destination": default_destination(), "theme": "Dark", "accent": "#7c6ff0",
                         "account": "", "options": DownloadOptions().__dict__,
                         "pacing": asdict(preset_pacing("Balanced")), "custom_pacing": asdict(PacingConfig()),
                         "performance": asdict(PerformanceConfig()), "last_performance": {}, "restriction": {}}
        saved = self.read_json("settings.json", {})
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in self.settings and isinstance(value, type(self.settings[key])):
                    self.settings[key] = value
        else:
            self.warning = "Invalid settings structure; defaults were restored."
        try:
            pacing = PacingConfig(**self.settings["pacing"])
        except (TypeError, ValueError):
            pacing = PacingConfig()
            self.warning = "Invalid pacing settings; safe pacing defaults were restored."
        try:
            performance = PerformanceConfig(**self.settings["performance"])
        except (TypeError, ValueError):
            performance = PerformanceConfig()
            self.warning = "Unsupported performance settings; Balanced with one worker was restored."
        try:
            custom = PacingConfig(**self.settings["custom_pacing"])
        except (TypeError, ValueError):
            custom = PacingConfig()
        if isinstance(saved, dict) and "performance" not in saved:
            custom = pacing  # Preserve old manual values while migrating the default to Balanced.
        self.settings["custom_pacing"] = asdict(custom)
        self.settings["performance"] = asdict(performance)
        self.settings["pacing"] = asdict(pacing if performance.profile == "Custom" else preset_pacing(performance.profile))
        self.settings["options"]["metadata"] = False
        self.db = sqlite3.connect(self.root / "history.sqlite3")
        self.db.execute("""CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY, timestamp TEXT, username TEXT, status TEXT,
            items INTEGER, files INTEGER, destination TEXT, detail TEXT, job TEXT)""")
        self.db.commit()

    def read_json(self, name, fallback):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fallback
        except (ValueError, OSError):
            self.warning = f"Could not read {name}; defaults were restored. The original file was left in place."
            return fallback

    def save_settings(self):
        atomic_json(self.root / "settings.json", self.settings)

    def save_queue(self, jobs):
        atomic_json(self.root / "queue.json", [job.to_dict() for job in jobs])

    def load_queue(self):
        jobs = []
        saved = self.read_json("queue.json", [])
        if not isinstance(saved, list):
            self.warning = "Invalid saved queue; an empty queue was restored."
            return jobs
        for data in saved:
            try:
                jobs.append(Job.from_dict(data))
            except (ValueError, TypeError, KeyError):
                self.warning = "An invalid saved queue entry was skipped."
        return jobs

    def record(self, job):
        self.db.execute("INSERT INTO history VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), job.username,
                         job.status, job.items, job.files, str(job.folder), job.detail,
                         json.dumps(job.to_dict())))
        self.db.commit()

    def history(self):
        return self.db.execute("SELECT * FROM history ORDER BY id DESC LIMIT 1000").fetchall()

    def close(self):
        self.db.close()
