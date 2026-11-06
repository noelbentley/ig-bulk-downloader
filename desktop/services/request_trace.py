"""Per-context HTTP observation. URLs, bodies, headers and tokens are never retained."""
from contextlib import contextmanager
import os
import sys
import time
from urllib.parse import urlsplit


PURPOSES = {"27128499623469141": "post_metadata", "28904005159232689": "post_listing",
            "29104886249145907": "post_listing", "7845543455542541": "reels_listing",
            "27937681195819736": "profile_metadata"}


class TimedBody:
    def __init__(self, raw, trace, media_mode=None):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_trace", trace)
        object.__setattr__(self, "_mode", media_mode)
        object.__setattr__(self, "_failed", False)

    def _error(self):
        if not self._failed:
            object.__setattr__(self, "_failed", True)
            self._trace.stats.add("http_body_errors")

    def _record(self, elapsed, size=0):
        self._trace.stats.add("http_body_seconds", elapsed)
        self._trace.stats.add("http_body_bytes", size)
        if self._mode:
            self._trace.stats.add(f"http_media_{self._mode}_body_seconds", elapsed)
            self._trace.stats.add(f"http_media_{self._mode}_bytes", size)

    def __setattr__(self, name, value):
        setattr(self._raw, name, value)

    def __getattr__(self, name):
        target = getattr(self._raw, name)
        if name in ("read", "readinto"):
            def read(*args, **kwargs):
                started = self._trace.stats.clock()
                size = 0
                try:
                    result = target(*args, **kwargs)
                    size = result if name == "readinto" else len(result or b"")
                    return result
                except Exception:
                    self._error()
                    raise
                finally:
                    self._record(self._trace.stats.clock() - started, size or 0)
            return read
        if name in ("stream", "read_chunked"):
            def stream(*args, **kwargs):
                iterator = iter(target(*args, **kwargs))
                while True:
                    started = self._trace.stats.clock()
                    size = 0
                    try:
                        chunk = next(iterator)
                        size = len(chunk or b"")
                    except StopIteration:
                        return
                    except Exception:
                        self._error()
                        raise
                    finally:
                        self._record(self._trace.stats.clock() - started, size)
                    yield chunk
            return stream
        return target


class RequestTrace:
    def __init__(self, statistics):
        self.stats = statistics
        self.purpose = "other"
        self.operation = "other"

    @contextmanager
    def scope(self, purpose=None, operation=None):
        old = self.purpose, self.operation
        if purpose:
            self.purpose = purpose
        if operation:
            self.operation = operation
        try:
            yield
        finally:
            self.purpose, self.operation = old

    def category(self, request):
        # Inspect ephemeral values only; return a fixed label, never a path or URL.
        parsed = urlsplit(request.url)
        if self.operation == "media":
            return "media_head" if request.method == "HEAD" else "media"
        path = parsed.path.rstrip("/")
        if path in ("/api/graphql", "/graphql/query"):
            return self.purpose if self.purpose in PURPOSES.values() else "other_api"
        if path.startswith("/api/v1/media/"):
            return "mobile_metadata"
        if not path:
            return "bootstrap"
        if path.startswith("/api/"):
            return "other_api"
        return "page"

    def attach(self, session, media=False):
        session = getattr(session, "_desktop_underlying_session", session)
        if session is None or not hasattr(session, "adapters"):
            return
        if getattr(session, "_desktop_request_trace", None) is self:
            return
        session._desktop_request_trace = self
        self.stats.add("http_sessions")
        if media:
            self.stats.add("media_sessions")
        for adapter in set(session.adapters.values()):
            if getattr(adapter, "_desktop_request_trace", None) is self:
                continue
            adapter._desktop_request_trace = self
            original = adapter.send
            def send(request, *args, _original=original, _session=session, **kwargs):
                category = self.category(request)
                mode = getattr(_session, "_desktop_media_mode", "fresh") if category == "media" else None
                if mode not in (None, "fresh", "pooled"):
                    mode = "fresh"
                self.stats.add("http_requests")
                self.stats.add("http_" + category + "_requests")
                if mode:
                    self.stats.add(f"http_media_{mode}_requests")
                started = self.stats.clock()
                try:
                    response = _original(request, *args, **kwargs)
                except Exception:
                    self.stats.add("http_transport_errors")
                    raise
                else:
                    status = int(response.status_code)
                    group = status // 100
                    if 2 <= group <= 5:
                        self.stats.add(f"http_{group}xx")
                    if response.raw is not None and not isinstance(response.raw, TimedBody):
                        response.raw = TimedBody(response.raw, self, mode)
                    return response
                finally:
                    elapsed = self.stats.clock() - started
                    self.stats.add("http_header_seconds", elapsed)
                    self.stats.add("http_" + category + "_seconds", elapsed)
                    if mode:
                        self.stats.add(f"http_media_{mode}_header_seconds", elapsed)
            adapter.send = send

    def install(self, context):
        self.attach(getattr(context, "_session", None))
        original = getattr(context, "get_anonymous_session", None)
        if original:
            def anonymous():
                session = original()
                self.attach(session, media=self.operation == "media")
                return session
            context.get_anonymous_session = anonymous
        original_query = getattr(context, "doc_id_graphql_query", None)
        if original_query:
            def query(doc_id, *args, **kwargs):
                with self.scope(purpose=PURPOSES.get(doc_id, "other_api")):
                    return original_query(doc_id, *args, **kwargs)
            context.doc_id_graphql_query = query
        original_json = getattr(context, "get_json", None)
        if original_json:
            def get_json(*args, **kwargs):
                session = kwargs.get("session") or (args[3] if len(args) > 3 else None)
                self.attach(session if session is not None else getattr(context, "_session", None))
                return original_json(*args, **kwargs)
            context.get_json = get_json
        original_sleep = getattr(context, "do_sleep", None)
        if original_sleep:
            def sleep():
                with self.stats.measure("native_delay_seconds"):
                    return original_sleep()
            context.do_sleep = sleep
        for name in ("get_raw", "head"):
            original_media = getattr(context, name, None)
            if original_media:
                def media(*args, _original=original_media, _get=name == "get_raw", **kwargs):
                    if _get:
                        self.stats.media_started()
                    with self.scope(operation="media"):
                        return _original(*args, **kwargs)
                setattr(context, name, media)

    @contextmanager
    def file_checks(self):
        # Thread-local CPython hook; no monkeypatch of os, Requests or engine globals.
        if sys.getprofile() is not None:
            yield
            return
        function = os.path.isfile
        code = getattr(function, "__code__", None)
        opened = {}
        def profile(frame, event, arg):
            if code is None and arg is function:
                if event == "c_call" and frame.f_globals.get("__name__", "").startswith("instaloader"):
                    opened[id(frame)] = time.perf_counter()
                elif event in ("c_return", "c_exception"):
                    started = opened.pop(id(frame), None)
                    if started is not None:
                        self.stats.add("duplicate_check_seconds", time.perf_counter() - started)
                        self.stats.add("duplicate_checks")
            elif code is not None and frame.f_code is code:
                if event == "call" and frame.f_back and frame.f_back.f_globals.get("__name__", "").startswith("instaloader"):
                    opened[id(frame)] = time.perf_counter()
                elif event == "return":
                    started = opened.pop(id(frame), None)
                    if started is not None:
                        self.stats.add("duplicate_check_seconds", time.perf_counter() - started)
                        self.stats.add("duplicate_checks")
        self.stats.add("file_check_probe_enabled", 1)
        sys.setprofile(profile)
        try:
            yield
        finally:
            sys.setprofile(None)

    def inspect_iterator(self, iterator):
        wrapper = getattr(iterator, "_node_wrapper", None)
        if not callable(wrapper):
            return iterator
        seen = set()
        def node(data):
            media = data.get("media", data)
            key = media.get("code") or media.get("shortcode") or id(data)
            if key not in seen:
                seen.add(key)
                self.stats.add("listing_nodes")
                checks = {"image_candidates": bool((media.get("image_versions2") or {}).get("candidates")),
                          "display_url": bool(media.get("display_url") or media.get("display_src")),
                          "thumbnail": bool(media.get("thumbnail_src") or media.get("thumbnail_url")),
                          "video_versions": bool(media.get("video_versions") or media.get("video_url")),
                          "timestamp": "taken_at" in media or "taken_at_timestamp" in media,
                          "caption": "caption" in media or "edge_media_to_caption" in media,
                          "carousel": bool(media.get("carousel_media") or media.get("edge_sidecar_to_children"))}
                for field, present in checks.items():
                    if present:
                        self.stats.add("listing_with_" + field)
            return wrapper(data)
        iterator._node_wrapper = node
        return iterator
