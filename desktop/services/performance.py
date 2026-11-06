"""Numeric-only local diagnostics and supported performance profiles."""
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time
import math

from desktop.services.pacing import PacingConfig


@dataclass(frozen=True)
class PerformanceConfig:
    profile: str = "Balanced"
    media_workers: int = 1

    def __post_init__(self):
        if self.profile not in ("Balanced", "Conservative", "Custom"):
            raise ValueError("Choose Balanced, Conservative or Custom.")
        if type(self.media_workers) is not int or self.media_workers != 1:
            raise ValueError("This Instaloader integration safely supports one media transfer worker.")


def preset_pacing(profile, custom=None):
    PerformanceConfig(profile)
    if profile == "Balanced":
        return PacingConfig(enabled=False, long_rest_enabled=False)
    if profile == "Conservative":
        return PacingConfig()
    return custom if custom is not None else PacingConfig()


class RunStatistics:
    FIELDS = ("discovery_seconds", "enumeration_seconds", "transfer_seconds", "post_processing_seconds",
              "pacing_seconds", "rate_wait_seconds", "cooldown_seconds", "failed_requests_seconds",
              "decision_seconds", "retry_seconds", "native_delay_seconds", "http_header_seconds", "http_body_seconds",
              "duplicate_check_seconds", "history_seconds", "gui_seconds", "media_transfer_seconds",
              "inter_file_gap_seconds", "completion_interval_seconds", "http_media_fresh_header_seconds",
              "http_media_pooled_header_seconds", "http_media_fresh_body_seconds", "http_media_pooled_body_seconds") + tuple("http_" + category + "_seconds" for category in
                  ("media", "media_head", "post_metadata", "post_listing", "reels_listing", "profile_metadata", "mobile_metadata", "other_api", "bootstrap", "page"))

    def __init__(self, profile="Balanced", clock=time.perf_counter):
        PerformanceConfig(profile)
        self.clock, self.profile = clock, profile
        self.started = clock()
        self.ended = None
        self.lock = threading.RLock()
        self.values = dict.fromkeys(self.FIELDS, 0.0)
        self.counts = dict.fromkeys(("files", "failed_requests", "retry_attempts", "active_transfers", "peak_transfers"), 0)
        self.counts.update(dict.fromkeys(("http_requests", "http_sessions", "media_sessions", "http_body_bytes",
            "optimized_rich_posts", "optimized_hydrated_posts", "optimized_fallback_sections", "optimized_fallback_posts",
            "optimized_discovery_windows", "optimized_saved_files", "optimized_duplicate_files", "optimized_failed_files",
            "http_transport_errors", "http_body_errors", "http_2xx", "http_3xx", "http_4xx", "http_5xx", "media_transfer_samples",
            "inter_file_samples", "duplicate_checks", "file_check_probe_enabled", "listing_nodes",
            "http_media_fresh_requests", "http_media_pooled_requests", "http_media_fresh_bytes", "http_media_pooled_bytes") +
            tuple("http_" + category + "_requests" for category in
                  ("media", "media_head", "post_metadata", "post_listing", "reels_listing", "profile_metadata", "mobile_metadata", "other_api", "bootstrap", "page")) +
            tuple("listing_with_" + field for field in ("image_candidates", "display_url", "thumbnail", "video_versions", "timestamp", "caption", "carousel")), 0))
        self.depth = dict.fromkeys(self.FIELDS, 0)
        self.opened = {}
        self.last_published = float("-inf")
        self.pending_media_start = None
        self.last_media_complete = None
        self.configured_workers = 1

    def media_started(self):
        with self.lock:
            self.pending_media_start = self.clock()

    def media_completed(self, fallback_started=None):
        with self.lock:
            now = self.clock()
            started = self.pending_media_start if self.pending_media_start is not None else (fallback_started if fallback_started is not None else now)
            self.values["media_transfer_seconds"] += max(0, now - started)
            self.counts["media_transfer_samples"] += 1
            if self.last_media_complete is not None:
                self.values["inter_file_gap_seconds"] += max(0, started - self.last_media_complete)
                self.values["completion_interval_seconds"] += max(0, now - self.last_media_complete)
                self.counts["inter_file_samples"] += 1
            self.last_media_complete = now
            self.pending_media_start = None

    def media_interval(self, started, finished):
        """Account independent worker intervals in completion order after a wave.

        Per-file durations are summed; transfer_seconds separately measures the
        union of active transfer time. An overlap contributes no idle gap.
        """
        with self.lock:
            self.values["media_transfer_seconds"] += max(0, finished - started)
            self.counts["media_transfer_samples"] += 1
            if self.last_media_complete is not None:
                self.values["inter_file_gap_seconds"] += max(0, started - self.last_media_complete)
                self.values["completion_interval_seconds"] += max(0, finished - self.last_media_complete)
                self.counts["inter_file_samples"] += 1
            self.last_media_complete = max(finished, self.last_media_complete or finished)

    def add(self, name, value=1):
        with self.lock:
            target = self.values if name in self.values else self.counts
            if name not in target:
                raise ValueError("Unknown diagnostic metric")
            target[name] += value

    def measured(self, name):
        with self.lock:
            value = self.values[name]
            if self.depth[name]:
                value += self.clock() - self.opened[name]
            return value

    @contextmanager
    def measure(self, name, transfer=False):
        with self.lock:
            if self.depth[name] == 0:
                self.opened[name] = self.clock()
            self.depth[name] += 1
            if transfer:
                self.counts["active_transfers"] += 1
                self.counts["peak_transfers"] = max(self.counts["peak_transfers"], self.counts["active_transfers"])
        try:
            yield
        finally:
            with self.lock:
                self.depth[name] -= 1
                if self.depth[name] == 0:
                    self.values[name] += self.clock() - self.opened.pop(name)
                if transfer:
                    self.counts["active_transfers"] -= 1

    def snapshot(self):
        with self.lock:
            elapsed = max(0.0, (self.ended if self.ended is not None else self.clock()) - self.started)
            return dict(self.counts, **{name: round(self.measured(name), 6) for name in self.FIELDS},
                        profile=self.profile, elapsed_seconds=round(elapsed, 6),
                        files_per_minute=round(self.counts["files"] * 60 / elapsed, 2) if elapsed else 0.0,
                        http_requests_per_file=round(self.counts["http_requests"] / self.counts["files"], 4) if self.counts["files"] else 0.0,
                        average_http_seconds=round((self.values["http_header_seconds"] + self.values["http_body_seconds"]) / self.counts["http_requests"], 6) if self.counts["http_requests"] else 0.0,
                        average_transfer_seconds=round(self.values["media_transfer_seconds"] / self.counts["media_transfer_samples"], 6) if self.counts["media_transfer_samples"] else 0.0,
                        average_inter_file_gap_seconds=round(self.values["inter_file_gap_seconds"] / self.counts["inter_file_samples"], 6) if self.counts["inter_file_samples"] else 0.0,
                        average_completion_interval_seconds=round(self.values["completion_interval_seconds"] / self.counts["inter_file_samples"], 6) if self.counts["inter_file_samples"] else 0.0,
                        diagnostic_version=2,
                        media_workers=self.configured_workers, active_profiles=1 if self.ended is None else 0)

    def publish(self, report, force=False):
        now = self.clock()
        if force or now - self.last_published >= 0.25:
            self.last_published = now
            report("performance", self.snapshot())

    def finish(self):
        with self.lock:
            self.ended = self.clock()
        return self.snapshot()


def summary_text(data):
    required = ("files", "elapsed_seconds", "files_per_minute", "discovery_seconds", "enumeration_seconds",
                "transfer_seconds", "pacing_seconds", "rate_wait_seconds", "cooldown_seconds", "failed_requests",
                "failed_requests_seconds", "retry_attempts")
    if not isinstance(data, dict) or any(type(data.get(key)) not in (int, float) or not math.isfinite(data[key]) or data[key] < 0 for key in required):
        return "No completed queue run yet."
    base = (f"{data['files']} files in {data['elapsed_seconds']:.1f}s · {data['files_per_minute']:.1f} files/min\n"
            f"Discovery {data['discovery_seconds']:.1f}s · Enumeration {data['enumeration_seconds']:.1f}s · Media {data['transfer_seconds']:.1f}s\n"
            f"Pacing {data['pacing_seconds']:.1f}s · Rate waits {data['rate_wait_seconds']:.1f}s · Cooldown {data['cooldown_seconds']:.1f}s\n"
            f"Failed requests {data['failed_requests']} ({data['failed_requests_seconds']:.1f}s) · Retries {data['retry_attempts']} ({data.get('retry_seconds', 0):.1f}s)")
    if data.get("diagnostic_version", 0) < 2:
        return base + "\nHTTP detail unavailable in this older report; run the diagnostic build once."
    detail = (base + f"\nHTTP {data.get('http_requests', 0)} requests · {data.get('http_requests_per_file', 0):.2f}/file · Native request sleep {data.get('native_delay_seconds', 0):.1f}s\n"
            f"Average transfer {data.get('average_transfer_seconds', 0):.3f}s · Inter-file gap {data.get('average_inter_file_gap_seconds', 0):.3f}s\n"
            f"Duplicate checks {data.get('duplicate_check_seconds', 0):.3f}s · History {data.get('history_seconds', 0):.3f}s · GUI callbacks {data.get('gui_seconds', 0):.3f}s")
    if data.get("optimized_discovery_windows") or data.get("optimized_fallback_sections"):
        detail += (f"\nOptimized: {data.get('optimized_rich_posts', 0)} rich posts · {data.get('optimized_hydrated_posts', 0)} hydrated · "
                   f"{data.get('optimized_saved_files', 0)} files · Peak {data.get('peak_transfers', 0)} transfers\n"
                   f"Instaloader fallback: {data.get('optimized_fallback_sections', 0)} sections · {data.get('optimized_fallback_posts', 0)} posts")
    return detail


def diagnostic_report(data):
    """Whitelist only known numeric fields and the validated preset label."""
    schema = RunStatistics().snapshot()
    result = {}
    if not isinstance(data, dict):
        return result
    for key, value in data.items():
        if key == "profile" and value in ("Balanced", "Conservative", "Custom"):
            result[key] = value
        elif key in schema and type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result[key] = value
    return result
