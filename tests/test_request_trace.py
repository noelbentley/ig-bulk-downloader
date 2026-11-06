from contextlib import contextmanager
from dataclasses import asdict
import io
import json
from pathlib import Path
import threading

import instaloader
import pytest
import requests

from desktop.services.control import Control
from desktop.services.engine import EngineService
from desktop.services.media_pool import AnonymousMediaPool, BorrowedSession
from desktop.services.performance import RunStatistics, diagnostic_report, preset_pacing, summary_text
from desktop.services.request_trace import RequestTrace, TimedBody
from desktop.models import DownloadOptions, Job


def test_transport_requests_body_bytes_and_native_sleep_are_separate(monkeypatch):
    now = [0.0]
    stats = RunStatistics(clock=lambda: now[0])
    context = instaloader.Instaloader(quiet=True).context
    context.do_sleep = lambda: now.__setitem__(0, now[0] + 2)
    context._rate_controller.wait_before_query = lambda _: None
    class Body(io.BytesIO):
        def read(self, *args, **kwargs):
            now[0] += 0.01
            return super().read(*args, **kwargs)
    def send(adapter, request, **kwargs):
        now[0] += 0.1
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.raw = Body(b'{"status":"ok"}' if request.method == "POST" else b"photo")
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    trace = RequestTrace(stats)
    trace.install(context)
    with trace.scope(purpose="post_metadata"):
        assert context.get_json("api/graphql", {"private_token": "never-write-this"}, use_post=True)["status"] == "ok"
    response = context.get_raw("https://cdn.invalid/media.jpg?signature=never-write-this")
    assert response.raw.read() == b"photo"
    response.close()
    result = stats.snapshot()
    assert result["http_requests"] == 2 and result["http_post_metadata_requests"] == 1 and result["http_media_requests"] == 1
    assert result["native_delay_seconds"] == 2
    assert result["http_header_seconds"] == pytest.approx(0.2)
    assert result["http_body_seconds"] > 0
    assert result["http_body_bytes"] == len(b'{"status":"ok"}photo')
    assert "never-write-this" not in json.dumps(result)
    context.close()


def test_body_stream_readinto_error_and_attribute_delegation():
    stats = RunStatistics()
    trace = RequestTrace(stats)
    body = TimedBody(io.BytesIO(b"abc"), trace, "pooled")
    buffer = bytearray(3)
    assert body.readinto(buffer) == 3 and buffer == b"abc"
    assert stats.snapshot()["http_body_bytes"] == 3
    body.decode_content = True
    assert body._raw.decode_content is True
    class Broken:
        def stream(self):
            yield b"first"
            raise OSError("secret URL must not be retained")
    broken = TimedBody(Broken(), trace)
    iterator = broken.stream()
    assert next(iterator) == b"first"
    with pytest.raises(OSError):
        next(iterator)
    assert stats.snapshot()["http_body_errors"] == 1
    assert "secret" not in json.dumps(stats.snapshot())


def test_media_intervals_count_only_successful_completions():
    now = [0.0]
    stats = RunStatistics(clock=lambda: now[0])
    now[0] = 2
    stats.media_started()
    now[0] = 3
    stats.add("files")
    stats.media_completed()
    now[0] = 7
    stats.media_started()
    now[0] = 9
    stats.add("files")
    stats.media_completed()
    snapshot = stats.snapshot()
    assert snapshot["average_transfer_seconds"] == 1.5
    assert snapshot["average_inter_file_gap_seconds"] == 4
    assert snapshot["average_completion_interval_seconds"] == 6


def test_report_whitelist_rejects_urls_cookies_and_unsupported_fields():
    report = diagnostic_report(dict(RunStatistics().snapshot(), signed_url="https://private.invalid/",
                                    cookies={"sessionid": "secret"}, private_token="secret"))
    assert "signed_url" not in report and "cookies" not in report and "private_token" not in report
    assert "secret" not in json.dumps(report)
    old = RunStatistics().snapshot()
    old.pop("diagnostic_version")
    assert "unavailable" in summary_text(old)


def test_pool_reuses_only_media_sessions_restores_cookies_and_closes(monkeypatch):
    loader = instaloader.Instaloader(quiet=True)
    context = loader.context
    context.username = "approved_account"
    context._session.cookies.update({"sessionid": "saved-login-secret", "csrftoken": "saved-csrf-secret"})
    requests_seen = []
    def send(adapter, request, **kwargs):
        requests_seen.append((id(adapter), dict(request.headers)))
        assert "saved-login-secret" not in request.headers.get("Cookie", "")
        assert "saved-csrf-secret" not in request.headers.get("X-CSRFToken", "")
        response = requests.Response()
        response.status_code = 200
        response.url, response.request = request.url, request
        response.raw = io.BytesIO(b"identical original bytes")
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    pool = AnonymousMediaPool()
    pool.install(context)
    stats = RunStatistics()
    trace = RequestTrace(stats)
    trace.install(context)
    for _ in range(3):
        response = context.get_raw("https://cdn.invalid/photo.jpg")
        assert response.raw.read() == b"identical original bytes"
        response.close()
        pool.session.cookies.set("cdn_cookie", "must-not-carry-to-next-request")
    assert len({item[0] for item in requests_seen}) == 1
    assert stats.snapshot()["http_media_pooled_requests"] == 3
    assert stats.snapshot()["media_sessions"] == 1
    assert "cdn_cookie" not in requests_seen[1][1].get("Cookie", "")
    assert context._session.cookies.get("sessionid") == "saved-login-secret"
    pool.close()
    assert pool.session is None
    loader.close()


def test_alternating_connection_probe_is_serial_and_counts_each_arm(monkeypatch):
    context = instaloader.Instaloader(quiet=True).context
    def send(adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url, response.request = request.url, request
        response.raw = io.BytesIO(b"bytes")
        return response
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    pool = AnonymousMediaPool("alternate")
    pool.install(context)
    stats = RunStatistics()
    RequestTrace(stats).install(context)
    for _ in range(4):
        response = context.get_raw("https://cdn.invalid/media.jpg")
        response.raw.read()
        response.close()
    result = stats.snapshot()
    assert result["http_media_fresh_requests"] == result["http_media_pooled_requests"] == 2
    assert result["media_sessions"] == 3
    pool.close()
    context.close()


def test_pool_rejects_simultaneous_borrow_and_keeps_sessions_separate():
    contexts = [instaloader.Instaloader(quiet=True).context for _ in range(2)]
    pools = [AnonymousMediaPool() for _ in range(2)]
    for pool, context in zip(pools, contexts):
        pool.install(context)
        pool.active = pool.pooled_request = True
        with context.get_anonymous_session():
            with pytest.raises(RuntimeError):
                with BorrowedSession(pool):
                    pytest.fail("Concurrent borrower must fail")
    assert pools[0].session is not pools[1].session
    for pool, context in zip(pools, contexts):
        pool.close()
        context.close()


def test_native_public_iterator_still_needs_post_metadata_even_without_exports():
    loader = instaloader.Instaloader(quiet=True, save_metadata=False)
    context = loader.context
    metadata_calls = []
    def query(doc_id, variables, *args, **kwargs):
        metadata_calls.append(doc_id)
        return {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": [{
            "code": variables["shortcode"], "pk": "123", "media_type": 1, "taken_at": 1700000000,
            "user": {"pk": "1", "username": "example", "full_name": ""},
            "image_versions2": {"candidates": [{"url": "https://cdn.invalid/signed-secret"}]}}]}}}
    context.doc_id_graphql_query = query
    stats = RunStatistics()
    trace = RequestTrace(stats)
    profile = instaloader.Profile(context, {"username": "example", "id": "1", "is_private": False})
    profile._has_full_metadata = True
    profile._page_user = {"id": "page", "polaris_ordered_timeline_connection": {
        "edges": [{"node": {"code": "ABC", "caption": {}}}, {"node": {"code": "ABD", "caption": {}}}],
        "page_info": {"has_next_page": False}}}
    iterator = trace.inspect_iterator(profile.get_posts())
    assert len(list(iterator)) == 2
    assert metadata_calls == ["27128499623469141"] * 2
    assert stats.snapshot()["listing_nodes"] == 2
    assert stats.snapshot()["listing_with_image_candidates"] == 0
    assert "signed-secret" not in json.dumps(stats.snapshot())
    loader.close()
