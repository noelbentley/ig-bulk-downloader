"""Independent bounded public-feed pipeline; native API discovery stays serialized.

No alternate Instagram endpoint, authentication escalation or rate-rule override.
Only the pinned NodeIterator's object-construction boundary is adapted. Its query,
pagination, waits and RateController remain native. Sparse/unsupported nodes retain
their original hydration/download path. Media workers never receive that context.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import instaloader
from instaloader import exceptions as errors
import requests

from desktop.services.control import Cancelled
from desktop.services.engine import EngineService
from desktop.services.pipeline import PipelineConfig
from desktop.services.request_trace import RequestTrace
from desktop.services.safety import RestrictionGate, RestrictionStopped


def media_url_allowed(url):
    """Keep parallel work on HTTPS media hosts, never Instagram API/page routes."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and not parsed.fragment
                and any(host == domain or host.endswith("." + domain)
                        for domain in ("cdninstagram.com", "fbcdn.net")))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class Media:
    url: str = field(repr=False)
    suffix: str
    index: int = 0


@dataclass(frozen=True)
class ListingPost:
    shortcode: str
    timestamp: float
    typename: str
    media: tuple[Media, ...] = field(repr=False)

    @property
    def date_local(self):
        # Pinned NodeIterator's newest-item checker uses this property.
        return datetime.fromtimestamp(self.timestamp)


def _image(node):
    # Match the native public rendition rather than substitute thumbnails.
    if node.get("display_url") or node.get("display_src"):
        return node.get("display_url") or node["display_src"]
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    # Native normalized media also takes candidate zero. Do not rewrite signed URLs.
    return candidates[0].get("url") if candidates else None


def _video(node):
    if node.get("video_url"):
        return node["video_url"]
    versions = node.get("video_versions") or []
    return versions[0].get("url") if versions else None


def parse_listing_post(raw, photos=True, videos=True, url_allowed=media_url_allowed):
    """Return a complete download plan or None; never fetch missing fields.

    Validate every selected carousel item before returning any of them. This
    prevents partial rich payloads from silently dropping children. Native sparse
    hydration is retained rather than inferred from a thumbnail or shortcode.
    """
    try:
        node = raw.get("media", raw)
        code = node.get("shortcode") or node.get("code")
        stamp = next((node[k] for k in ("taken_at_timestamp", "taken_at", "date") if k in node), None)
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", code):
            return None
        if type(stamp) not in (int, float) or not 0 < stamp < 32503680000:
            return None
        datetime.fromtimestamp(stamp, timezone.utc)
        kind = node.get("__typename") or {1: "GraphImage", 2: "GraphVideo", 8: "GraphSidecar"}.get(node.get("media_type"))
        if kind not in ("GraphImage", "GraphVideo", "GraphSidecar"):
            return None
        sidecar = kind == "GraphSidecar"
        if sidecar:
            children = node.get("carousel_media")
            if children is None:
                sidecar_data = node["edge_sidecar_to_children"]
                children = [edge["node"] for edge in sidecar_data["edges"]]
                if "count" in sidecar_data and sidecar_data["count"] != len(children):
                    return None
            expected = node.get("carousel_media_count")
            if not children or (expected is not None and expected != len(children)):
                return None
        else:
            children = [node]
        selected = []
        for index, child in enumerate(children, 1):
            child_kind = child.get("__typename") or {1: "GraphImage", 2: "GraphVideo"}.get(child.get("media_type"))
            if child_kind:
                if child_kind not in ("GraphImage", "GraphVideo"):
                    return None
                is_video = child_kind == "GraphVideo"
            elif type(child.get("is_video")) is bool:
                is_video = child["is_video"]
            else:
                return None
            if (is_video and not videos) or (not is_video and not photos):
                continue
            url = _video(child) if is_video else _image(child)
            if not isinstance(url, str) or not url_allowed(url):
                return None
            if Path(urlsplit(url).path).suffix.lower() not in ((".mp4",) if is_video else (".jpg", ".jpeg")):
                return None
            selected.append(Media(url, ".mp4" if is_video else ".jpg", index if sidecar else 0))
        return ListingPost(code, stamp, kind, tuple(selected))
    except (KeyError, TypeError, IndexError, ValueError, OverflowError, OSError, AttributeError):
        return None


@dataclass(frozen=True)
class MediaTask:
    path: Path
    media: Media = field(repr=False)
    timestamp: float
    shortcode: str


@dataclass(frozen=True)
class TransferResult:
    task: MediaTask
    saved: bool
    started: float
    finished: float


class MediaTransfers:
    """At most one task per worker; no shared Requests/Instaloader session.

    A failure closes admission for the entire group. Already admitted requests
    finish or stop at chunk boundaries; they are never automatically retried.
    Manual pause drains the current wave before admitting another one.
    """
    def __init__(self, workers, session_factory, control, safety, report, statistics=None,
                 url_allowed=media_url_allowed):
        PipelineConfig(workers=workers)
        self.workers, self.control, self.safety = workers, control, safety
        self.report, self.stats, self.url_allowed = report, statistics, url_allowed
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.error = None
        self.sessions = []
        self.available = deque()
        self.executor = None
        try:
            for _ in range(workers):
                session = session_factory()
                self.sessions.append(session)
                session._desktop_media_mode = "pooled"
                trace = RequestTrace(statistics) if statistics else None
                if trace:
                    trace.attach(session, media=True)
                self.available.append((session, session.cookies.copy(), trace))
            self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ig-media")
        except BaseException:
            self.close()
            raise

    def fail(self, error):
        with self.lock:
            if (self.error is None or isinstance(error, RestrictionStopped)
                    or (isinstance(self.error, Cancelled) and not isinstance(error, Cancelled))):
                self.error = error
            self.stopped.set()

    def _check(self):
        if self.control.cancelled.is_set() or self.stopped.is_set():
            raise Cancelled()

    def _get(self, session, trace, url):
        for redirect in range(4):
            self._check()
            if not self.url_allowed(url):
                raise errors.ConnectionException("Unsupported media host; transfer stopped.")
            with trace.scope(operation="media") if trace else nullcontext():
                response = session.get(url, stream=True, allow_redirects=False)
            try:
                # A shared gate records Retry-After before a status exception loses it.
                self.safety.inspect_response(response, self.report)
                status = response.status_code
                if status in (301, 302, 303, 307, 308):
                    location = urljoin(url, response.headers.get("Location", ""))
                    if redirect == 3 or not self.url_allowed(location):
                        raise errors.ConnectionException("Media redirect requires unsupported access.")
                    url = location
                    response.close()
                    continue
                if status == 401:
                    raise errors.LoginRequiredException("Media requires authentication.")
                if status == 403:
                    raise errors.QueryReturnedForbiddenException("Media access denied (HTTP 403).")
                if status == 404:
                    raise errors.QueryReturnedNotFoundException("Media unavailable (HTTP 404).")
                if status != 200:
                    raise errors.ConnectionException(f"Media transfer returned HTTP {status}.")
                return response
            except BaseException:
                response.close()
                raise

    def transfer(self, task):
        started = self.stats.clock() if self.stats else time.perf_counter()
        temporary = task.path.with_name("." + task.path.name + "." + uuid4().hex + ".part")
        lease = None
        try:
            self._check()
            checked = self.stats.clock() if self.stats else 0
            exists = task.path.is_file()
            if self.stats:
                self.stats.add("duplicate_checks")
                self.stats.add("duplicate_check_seconds", max(0, self.stats.clock() - checked))
            if exists:
                return TransferResult(task, False, started, started)
            with self.lock:
                lease = self.available.popleft()
            session, cookies, trace = lease
            session.cookies.clear()
            session.cookies.update(cookies)
            with self.stats.measure("transfer_seconds", transfer=True) if self.stats else nullcontext():
                response = self._get(session, trace, task.media.url)
                try:
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    allowed = {".jpg": ("image/jpeg", "image/jpg"), ".mp4": ("video/mp4",)}
                    if content_type and content_type not in allowed[task.media.suffix]:
                        raise errors.ConnectionException("Unexpected media content type; no file saved.")
                    task.path.parent.mkdir(parents=True, exist_ok=True)
                    total = 0
                    with temporary.open("xb") as output:
                        for chunk in response.iter_content(256 * 1024):
                            self._check()  # Cancellation/restriction only; manual pause lets this file finish.
                            if chunk:
                                output.write(chunk)
                                total += len(chunk)
                    length = response.headers.get("Content-Length")
                    if total == 0 or (length and not response.headers.get("Content-Encoding") and total != int(length)):
                        raise errors.ConnectionException("Incomplete media body; no file saved.")
                    self._check()
                    os.utime(temporary, (time.time(), task.timestamp))
                    try:
                        if os.name == "nt":
                            # Windows rename is atomic and refuses to overwrite an existing target.
                            os.rename(temporary, task.path)
                        else:
                            os.link(temporary, task.path)
                    except FileExistsError:
                        return TransferResult(task, False, started, self.stats.clock() if self.stats else time.perf_counter())
                finally:
                    response.close()
            finished = self.stats.clock() if self.stats else time.perf_counter()
            return TransferResult(task, True, started, finished)
        except BaseException as error:
            # Avoid exception text from Requests: it may contain a signed URL.
            if isinstance(error, requests.exceptions.RequestException) or not isinstance(
                    error, (Cancelled, errors.InstaloaderException, errors.AbortDownloadException, OSError)):
                error = errors.ConnectionException("Media transfer failed; partial output was removed.")
            self.fail(error)
            if self.stats and not isinstance(error, Cancelled):
                self.stats.add("optimized_failed_files")
                if not isinstance(error, OSError):
                    self.stats.add("failed_requests")
                    self.stats.add("failed_requests_seconds", max(0, self.stats.clock() - started))
            raise error from None
        finally:
            temporary.unlink(missing_ok=True)
            if lease:
                session, cookies, trace = lease
                session.cookies.clear()
                session.cookies.update(cookies)
                with self.lock:
                    self.available.append(lease)

    def wave(self, tasks, completed):
        """Drain all admitted work and account successes before raising any error."""
        self.control.checkpoint(self.report)
        self.safety.wait(self.control, self.report, self.stats)
        self._check()
        futures = []
        for task in tasks:
            if self.stopped.is_set() or self.control.cancelled.is_set() or not self.control.resumed.is_set():
                break
            futures.append(self.executor.submit(self.transfer, task))
        results = []
        for future in as_completed(futures):
            try:
                result = future.result()
            except BaseException as error:
                self.fail(error)
            else:
                results.append(result)
                completed(result)
        if self.stats:
            for result in sorted(results, key=lambda r: r.finished):
                if result.saved:
                    self.stats.media_interval(result.started, result.finished)
        if self.error:
            raise self.error
        if self.control.cancelled.is_set():
            raise Cancelled()
        return len(futures)

    def close(self):
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
        for session in self.sessions:
            session.close()
        self.sessions.clear()


class OptimizedEngineService(EngineService):
    def __init__(self, *args, config=None, transfer_factory=MediaTransfers, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config or PipelineConfig(enabled=True)
        self.transfer_factory = transfer_factory

    def _add(self, metric, value=1):
        if self.statistics:
            self.statistics.add(metric, value)

    def _run(self, job, control, report, seen=None):
        options = job.options
        # These operations retain the complete original engine and consent path.
        if (not self.config.enabled or self.account or options.captions or options.reels
                or options.stories or options.highlights or options.profile_picture):
            self._add("optimized_fallback_sections")
            report("activity", "Using Instaloader for the selected content/options")
            return super()._run(job, control, report, seen)
        options.validate()
        self.control, self.report, self.new_files = control, report, 0
        self.pacer.statistics = self.statistics
        control.checkpoint(report)
        safety = self.safety or RestrictionGate()
        self.safety = safety
        safety.wait(control, report, self.statistics)
        self.pacer.resume_pending(control, report)
        loader = self.loader or self._create()
        if loader.filename_pattern != "{date_utc}_UTC" or loader.slide_start != 0 or loader.slide_end != -1:
            self._add("optimized_fallback_sections")
            return super()._run(job, control, report, seen)
        loader.dirname_pattern = job.dirname_pattern
        loader.download_pictures, loader.download_videos = options.photos, options.videos
        loader.download_video_thumbnails = loader.save_metadata = False
        loader.post_metadata_txt_pattern = ""
        if self.statistics:
            self.statistics.configured_workers = self.config.workers
        report("mode", "Public Mode — No Login")
        report("activity", f"Optimized public feed: {self.config.workers} media workers")
        key = (job.id, job.username)
        if key not in self.profiles:
            with self._measure("discovery_seconds"):
                self.profiles[key] = self.profile_factory(loader.context, job.username)
        profile = self.profiles[key]
        if profile.is_private:
            raise errors.PrivateProfileNotFollowedException("Private profile requires approved authentication.")
        seen = seen if seen is not None else set()
        discovered = set(seen)
        processed = 0
        with self._measure("enumeration_seconds"):
            iterator = iter(profile.get_posts())
        if isinstance(iterator, instaloader.NodeIterator) and instaloader.__version__ == "4.16":
            original = iterator._node_wrapper
            def adapt(node):
                parsed = parse_listing_post(node, options.photos, options.videos)
                if parsed is not None:
                    self._add("optimized_rich_posts")
                    return parsed
                self._add("optimized_hydrated_posts")
                return original(node)
            iterator._node_wrapper = adapt
            if self.trace:
                iterator = self.trace.inspect_iterator(iterator)

        transfers = None
        pending = []
        remaining = {}

        def item_done(code):
            nonlocal processed
            if code not in seen:
                seen.add(code)
                processed += 1
                report("items", processed)

        def completed(result):
            if result.saved:
                self.new_files += 1
                self._add("files")
                self._add("optimized_saved_files")
                report("files", self.new_files)
                self.pacer.record_completed_file()
            else:
                self._add("optimized_duplicate_files")
            remaining[result.task.shortcode] -= 1
            if remaining[result.task.shortcode] == 0:
                item_done(result.task.shortcode)
            self._publish()

        def flush():
            nonlocal transfers
            if not pending:
                return
            if transfers is None:
                # Call only the native anonymous factory, never the authenticated API session.
                factory = lambda: instaloader.InstaloaderContext.get_anonymous_session(loader.context)
                transfers = self.transfer_factory(self.config.workers, factory, control, safety, report, self.statistics)
            while pending:
                control.checkpoint(report)
                self.pacer.resume_pending(control, report)
                count = self.config.workers
                if self.pacer.config.enabled:
                    count = min(count, self.pacer.batch_target - self.pacer.batch_count)
                    if self.pacer.long_target is not None:
                        count = min(count, self.pacer.long_target - self.pacer.long_count)
                count = max(1, count)
                admitted = transfers.wave(pending[:count], completed)
                del pending[:admitted]
                # All active files have finished before a pacing/manual pause starts.
                self.pacer.resume_pending(control, report)
                control.checkpoint(report)

        try:
            exhausted = False
            reserved = set()
            while not exhausted:
                batch = []
                # A bounded window, not a whole-profile URL collection. Native pagination
                # and API requests execute only here, before the next transfer wave.
                for _ in range(12):
                    control.checkpoint(report)
                    with self._measure("enumeration_seconds"):
                        try:
                            post = next(iterator)
                        except StopIteration:
                            exhausted = True
                            break
                    code = post.shortcode
                    if code in discovered:
                        continue
                    discovered.add(code)
                    batch.append(post)
                self._add("optimized_discovery_windows")
                for post in batch:
                    control.checkpoint(report)
                    if ((post.typename == "GraphImage" and not options.photos)
                            or (post.typename == "GraphVideo" and not options.videos)):
                        continue
                    parsed = post if isinstance(post, ListingPost) else parse_listing_post(
                        getattr(post, "_node", {}), options.photos, options.videos)
                    if parsed is None:
                        flush()
                        self._add("optimized_fallback_posts")
                        loader.download_post(post, target="posts")
                        item_done(post.shortcode)
                        continue
                    remaining[post.shortcode] = 0
                    stem = datetime.fromtimestamp(parsed.timestamp, timezone.utc).strftime("%Y-%m-%d_%H-%M-%S") + "_UTC"
                    plans = [MediaTask(job.media_folder("posts") / (stem + (f"_{media.index}" if media.index else "") + media.suffix),
                                       media, parsed.timestamp, post.shortcode) for media in parsed.media]
                    # Resolve rare timestamp/path collisions before counting the next
                    # post, rather than marking it done while its dependency is pending.
                    if any(task.path in reserved and not task.path.is_file() for task in plans):
                        flush()
                    for task in plans:
                        path = task.path
                        with self._measure("duplicate_check_seconds"):
                            exists = path.is_file()
                        self._add("duplicate_checks")
                        if exists or path in reserved:
                            self._add("optimized_duplicate_files")
                            continue
                        reserved.add(path)
                        pending.append(task)
                        remaining[post.shortcode] += 1
                    if remaining[post.shortcode] == 0:
                        item_done(post.shortcode)
                flush()
            control.checkpoint(report)
        finally:
            if transfers:
                transfers.close()
