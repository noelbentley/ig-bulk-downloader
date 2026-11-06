import time
import threading
from contextlib import nullcontext
import requests
import urllib3
from pathlib import Path

import instaloader
from instaloader import exceptions as errors
from desktop.services.control import Cancelled, Control
from desktop.services.pacing import Pacer, PacingConfig
from desktop.services.errors import SessionExpired, classify_error, safe_activity
from desktop.services.request_trace import RequestTrace
from desktop.services.media_pool import AnonymousMediaPool


def explain_error(error):
    return classify_error(error).message


class EngineService:
    """One engine per queue run retains Instaloader's rate budget across profiles."""
    def __init__(self, sessions=None, account="", loader_factory=instaloader.Instaloader,
                 profile_factory=instaloader.Profile.from_username, pacing=None):
        self.sessions, self.account = sessions, account
        self.loader_factory, self.profile_factory = loader_factory, profile_factory
        self.loader = None
        self.report = lambda *_: None
        self.control = None
        self.new_files = 0
        self.pacer = Pacer(pacing if pacing is not None else PacingConfig())
        self.profiles = {}
        self.statistics = None
        self.safety = None
        self._execution = threading.Lock()
        self.trace = None
        self.connection_mode = "pooled"
        self.media_pool = None

    def _measure(self, name, transfer=False):
        return self.statistics.measure(name, transfer=transfer) if self.statistics else nullcontext()

    def _publish(self):
        if self.statistics:
            self.statistics.publish(self.report)

    def _create(self):
        service = self

        class ResponsiveRateController(instaloader.RateController):
            def sleep(self, secs):
                service.report("activity", f"Respecting Instagram rate limit; waiting {int(secs)} seconds")
                with service._measure("rate_wait_seconds"):
                    deadline = time.monotonic() + secs
                    while time.monotonic() < deadline:
                        if service.control.cancelled.wait(min(0.2, max(0, deadline - time.monotonic()))):
                            raise Cancelled()

        loader = self.loader_factory(quiet=True, request_timeout=30, max_connection_attempts=1,
                                     fatal_status_codes=[401, 403, 429], sanitize_paths=True,
                                     save_metadata=False,
                                     rate_controller=ResponsiveRateController)
        self.loader = loader
        loader.context.log = lambda *args, **kwargs: self.report("activity", safe_activity("".join(map(str, args)).strip()))
        loader.context.error = lambda message, **kwargs: self._engine_error(message)
        write_raw = loader.context.write_raw

        # Observe responses before the original status checks discard Retry-After headers.
        # Each hook remains on this app-owned instance; the engine's checks are still called.
        if self.safety:
            for name in ("_raise_if_fatal_status", "_raise_for_download_status", "_raise_if_login_redirect"):
                original = getattr(loader.context, name, None)
                if original:
                    def checked(response, *args, _original=original, **kwargs):
                        self.safety.inspect_response(response, self.report)
                        return _original(response, *args, **kwargs)
                    setattr(loader.context, name, checked)

        for name in ("get_raw", "get_json", "get_page_data"):
            original = getattr(loader.context, name, None)
            if original:
                def request(*args, _original=original, _media=name == "get_raw", **kwargs):
                    started = time.perf_counter()
                    waits = self.statistics.measured("rate_wait_seconds") if self.statistics else 0
                    try:
                        with self._measure("transfer_seconds", transfer=True) if _media else nullcontext():
                            if _media:
                                self._publish()
                            return _original(*args, **kwargs)
                    except Cancelled:
                        raise
                    except Exception:
                        if self.statistics:
                            self.statistics.add("failed_requests")
                            self.statistics.add("failed_requests_seconds", max(0, time.perf_counter() - started -
                                (self.statistics.measured("rate_wait_seconds") - waits)))
                        raise
                setattr(loader.context, name, request)

        def write(resp, filename):
            self.report("activity", str(filename))
            started = time.perf_counter()
            media_started = self.statistics.clock() if self.statistics else 0
            try:
                with self._measure("transfer_seconds", transfer=True):
                    self._publish()
                    write_raw(resp, filename)
            except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError):
                if self.statistics:
                    self.statistics.add("failed_requests")
                    self.statistics.add("failed_requests_seconds", time.perf_counter() - started)
                raise
            if Path(filename).suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
                self.new_files += 1
                if self.statistics:
                    self.statistics.add("files")
                    self.statistics.media_completed(media_started)
                self.report("files", self.new_files)
                if Path(filename).suffix.lower() in (".jpg", ".jpeg", ".mp4"):
                    self.pacer.completed_file(self.control, self.report)
                # Stop/pause before the next file, including within a carousel.
                self.control.checkpoint(self.report)
                self._publish()

        loader.context.write_raw = write
        if self.connection_mode != "fresh" and hasattr(loader.context, "get_anonymous_session"):
            self.media_pool = AnonymousMediaPool(self.connection_mode)
            self.media_pool.install(loader.context)
        if self.trace:
            self.trace.install(loader.context)
        try:
            if self.account:
                try:
                    cookies = self.sessions.load(self.account)
                except (OSError, ValueError) as error:
                    raise SessionExpired() from error
                try:
                    loader.load_session(self.account, cookies)
                except (KeyError, TypeError, ValueError) as error:
                    raise SessionExpired() from error
                if loader.test_login() != self.account:
                    raise SessionExpired()
        except Exception:
            loader.close()
            self.loader = None
            raise
        return loader

    def _engine_error(self, message):
        # Do not quietly mark a profile successful when the engine catches a file error.
        if str(message).startswith("Error when checking if logged in:"):
            raise errors.ConnectionException(str(message))
        raise errors.InstaloaderException(str(message))

    def run(self, job, control, report, seen=None):
        if not self._execution.acquire(blocking=False):
            raise RuntimeError("An engine cannot process two profiles simultaneously.")
        try:
            if self.statistics and self.trace is None:
                self.trace = RequestTrace(self.statistics)
            with self.trace.file_checks() if self.trace else nullcontext():
                return self._run(job, control, report, seen)
        finally:
            self._execution.release()

    def _run(self, job, control, report, seen=None):
        self.control, self.report, self.new_files = control, report, 0
        self.pacer.statistics = self.statistics
        control.checkpoint(report)
        if self.safety:
            self.safety.wait(control, report, self.statistics)
        self.pacer.resume_pending(control, report)
        job.options.validate()
        if (job.options.stories or job.options.highlights) and not self.account:
            raise errors.LoginRequiredException()
        job.folder.mkdir(parents=True, exist_ok=True)
        loader = self.loader or self._create()
        report("mode", f"Authenticated — @{self.account}" if self.account else "Public Mode — No Login")
        options = job.options
        # Escape formatter braces in user-chosen paths; username targets are validated.
        loader.dirname_pattern = job.dirname_pattern
        loader.download_pictures = options.photos
        loader.download_videos = options.videos
        loader.download_video_thumbnails = False
        loader.save_metadata = False  # JSON exports are disabled, including legacy saved options.
        loader.compress_json = False
        loader.post_metadata_txt_pattern = ""
        loader.storyitem_metadata_txt_pattern = ""
        report("activity", f"Looking up @{job.username}")
        key = (job.id, job.username)
        if key not in self.profiles:
            with self._measure("discovery_seconds"):
                self.profiles[key] = self.profile_factory(loader.context, job.username)
        profile = self.profiles[key]
        control.checkpoint(report)
        if profile.is_private and (not self.account or not (self.account == job.username or profile.followed_by_viewer)):
            raise errors.PrivateProfileNotFollowedException()
        processed = 0
        seen = seen if seen is not None else set()

        def enumerated(factory):
            with self._measure("enumeration_seconds"):
                iterator = iter(factory())
                if self.trace:
                    iterator = self.trace.inspect_iterator(iterator)
            while True:
                control.checkpoint(report)
                try:
                    with self._measure("enumeration_seconds"):
                        item = next(iterator)
                except StopIteration:
                    return
                yield item

        def consume(factory, target, story=False):
            nonlocal processed
            for item in enumerated(factory):
                control.checkpoint(report)
                if not story:
                    if target == "posts" and ((item.typename == "GraphImage" and not options.photos) or
                                              (item.typename == "GraphVideo" and not options.videos)):
                        continue
                    if item.shortcode in seen:
                        continue
                report("state", "Downloading")
                report("activity", f"{target}: {getattr(item, 'shortcode', getattr(item, 'mediaid', 'media'))}")
                started = time.perf_counter()
                excluded = sum(self.statistics.measured(name) for name in ("transfer_seconds", "pacing_seconds", "rate_wait_seconds")) if self.statistics else 0
                original_pattern = getattr(loader, "filename_pattern", "{date_utc}_UTC")
                if story and job.layout == "flat":
                    # Stories/highlights may share a timestamp with feed media.
                    # Keep one folder while distinguishing their stable media ID.
                    loader.filename_pattern = original_pattern + "_story_{mediaid}"
                try:
                    if story:
                        loader.download_storyitem(item, target=target)
                    else:
                        loader.download_post(item, target=target)
                        seen.add(item.shortcode)
                finally:
                    loader.filename_pattern = original_pattern
                    if self.statistics:
                        after = sum(self.statistics.measured(name) for name in ("transfer_seconds", "pacing_seconds", "rate_wait_seconds"))
                        self.statistics.add("post_processing_seconds", max(0, time.perf_counter() - started - (after - excluded)))
                processed += 1
                report("items", processed)

        if options.profile_picture:
            loader.download_profilepic(profile)
            control.checkpoint(report)
        if options.photos or options.videos:
            consume(profile.get_posts, "posts")
        if options.reels:
            loader.download_videos = True
            loader.download_pictures = False
            consume(profile.get_reels, "reels")
        # Stories/highlights select all their media independently of feed filters.
        loader.download_pictures = loader.download_videos = True
        if options.stories:
            control.checkpoint(report)
            for story in enumerated(lambda: loader.get_stories([profile.userid])):
                consume(story.get_items, "stories", story=True)
        if options.highlights:
            control.checkpoint(report)
            for highlight in enumerated(lambda: loader.get_highlights(profile)):
                control.checkpoint(report)
                consume(highlight.get_items, f"highlights/{highlight.unique_id}", story=True)
        control.checkpoint(report)

    def close(self):
        if self.media_pool:
            self.media_pool.close()
        if self.loader:
            self.loader.close()
            self.loader = None
