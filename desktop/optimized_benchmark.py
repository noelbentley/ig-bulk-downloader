"""Offline A/B benchmark of the native and optimized Instagram pipelines.

Real file writes, native NodeIterator/Post hydration and Requests adapter hooks;
synthetic payloads/latencies. Results establish no live Instagram throughput.
"""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import io
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import instaloader
import requests

from desktop.models import DownloadOptions, Job
from desktop.services.control import Control
from desktop.services.engine import EngineService
from desktop.services.optimized import OptimizedEngineService
from desktop.services.pacing import PacingConfig
from desktop.services.performance import RunStatistics, diagnostic_report
from desktop.services.pipeline import PipelineConfig
from desktop.services.safety import RestrictionGate


class OfflineInstagram:
    """Synthetic Instagram responses, never a production endpoint implementation."""
    def __init__(self, posts=24, rich=True, api_latency=0.005, media_latency=0.02):
        self.rich, self.api_latency, self.media_latency = rich, api_latency, media_latency
        self.posts = []
        self.lock = threading.Lock()
        self.requests = self.active = self.peak = 0
        self.metadata = self.pages = self.media = 0
        self.media_status = 200
        self.media_headers = {}
        for index in range(posts):
            def image(child):
                return {"media_type": 1, "image_versions2": {"candidates": [{
                    "url": f"https://fixture.cdninstagram.com/p{index}_{child}.jpg?sig=synthetic-private",
                    "width": 1080, "height": 1350}]}}
            post = dict(image(0), code=f"POST{index}", pk=str(1000 + index), taken_at=1700000000 + index,
                        user={"pk": "1", "username": "fixture", "full_name": "Fixture"}, caption=None)
            if index % 5 == 0:
                post.update(media_type=8, carousel_media=[image(i) for i in range(1, 4)], carousel_media_count=3)
            elif index % 5 == 1:
                post.update(media_type=2, video_versions=[{
                    "url": f"https://fixture.cdninstagram.com/p{index}.mp4?sig=synthetic-private"}])
            self.posts.append(post)

    @staticmethod
    def payload(path):
        return (sha256(path.encode("utf-8")).digest() * 256)

    def send(self, adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url, response.request = request.url, request
        host = urlsplit(request.url).hostname
        with self.lock:
            self.requests += 1
        if host == "www.instagram.com":
            time.sleep(self.api_latency)
            fields = parse_qs(request.body)
            variables = json.loads(fields["variables"][0])
            if fields["doc_id"][0] == "27128499623469141":
                with self.lock:
                    self.metadata += 1
                post = next(p for p in self.posts if p["code"] == variables["shortcode"])
                data = {"xdt_api__v1__media__shortcode__web_info": {"items": [post]}}
            else:
                with self.lock:
                    self.pages += 1
                start = int(variables.get("after") or 0)
                selected = self.posts[start:start + 12]
                nodes = selected if self.rich else [{"code": p["code"]} for p in selected]
                data = {"node": {"polaris_ordered_timeline_connection": {
                    "edges": [{"node": n} for n in nodes],
                    "page_info": {"has_next_page": start + 12 < len(self.posts), "end_cursor": str(start + 12)}}}}
            content = json.dumps({"data": data}).encode("utf-8")
            response.headers["Content-Type"] = "application/json"
        else:
            with self.lock:
                self.media += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(self.media_latency)
                path = urlsplit(request.url).path
                content = self.payload(path)
                response.status_code = self.media_status
                response.headers["Content-Type"] = "video/mp4" if path.endswith(".mp4") else "image/jpeg"
                response.headers.update(self.media_headers)
            finally:
                with self.lock:
                    self.active -= 1
        response.headers.setdefault("Content-Length", str(len(content)))
        response.raw = io.BytesIO(content)
        return response

    def loader(self, **kwargs):
        loader = instaloader.Instaloader(**kwargs)
        context = loader.context
        # Only the offline fixture replaces waits, with a repeatable modeled API
        # delay. Production never replaces these native safeguards.
        context.do_sleep = lambda: time.sleep(self.api_latency)
        context._rate_controller.wait_before_query = lambda *_: None
        def query(doc_id, variables, *args, **kwargs):
            context.do_sleep()
            context._rate_controller.wait_before_query(doc_id)
            with context._session.post("https://www.instagram.com/api/graphql",
                    data={"doc_id": doc_id, "variables": json.dumps(variables)}) as response:
                return response.json()
        context.doc_id_graphql_query = query
        return loader

    @staticmethod
    def profile(context, username):
        class Profile:
            is_private = False
            def get_posts(self):
                data = context.doc_id_graphql_query("28904005159232689", {})["data"]["node"]["polaris_ordered_timeline_connection"]
                return instaloader.NodeIterator(context=context, query_hash=None, doc_id="28904005159232689",
                    edge_extractor=lambda d: d["data"]["node"]["polaris_ordered_timeline_connection"],
                    node_wrapper=lambda n: instaloader.Post.from_shortcode(context, n["code"]),
                    is_first=instaloader.Profile._make_is_newest_checker(), first_data=data)
        return Profile()

    @contextmanager
    def offline(self):
        def blocked(*args, **kwargs):
            raise AssertionError("Network is disabled in the optimized benchmark")
        backend = self
        with patch.object(socket.socket, "connect", blocked), patch.object(requests.adapters.HTTPAdapter, "send",
                lambda adapter, request, **kwargs: backend.send(adapter, request, **kwargs)):
            yield


def manifest(root):
    return {str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.suffix in (".jpg", ".mp4")}


def run_case(root, optimized=False, workers=1, rich=True, posts=24, api_latency=0.005, media_latency=0.02):
    backend = OfflineInstagram(posts, rich, api_latency, media_latency)
    kwargs = dict(loader_factory=backend.loader, profile_factory=backend.profile,
                  pacing=PacingConfig(enabled=False))
    engine = OptimizedEngineService(config=PipelineConfig(True, workers), **kwargs) if optimized else EngineService(**kwargs)
    stats = RunStatistics()
    engine.statistics, engine.safety = stats, RestrictionGate()
    try:
        with backend.offline():
            engine.run(Job("fixture", str(root), DownloadOptions(captions=False, profile_picture=False)),
                       Control(), lambda *_: None)
    finally:
        engine.close()
    result = diagnostic_report(stats.finish())
    result.update(observed_media_peak=backend.peak, observed_requests=backend.requests,
                  observed_metadata_requests=backend.metadata, observed_listing_requests=backend.pages,
                  observed_media_requests=backend.media)
    return result, manifest(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--posts", type=int, default=24)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.posts <= 120 or not 1 <= args.repetitions <= 5:
        parser.error("Use 1–120 posts and 1–5 repetitions.")
    root = Path(__file__).resolve().parents[1]
    result = {"network": "disabled", "workload": "Synthetic Instagram feed; 8 KiB/file, mixed media and carousels",
              "modeled_latency_seconds": {"api_request": 0.005, "native_api_sleep": 0.005, "media_request": 0.02},
              "limitation": "Mocked transport and waits. No live Instagram speed or rate-limit claim.", "cases": {}}
    (root / "build").mkdir(exist_ok=True)
    for rich in (True, False):
        baseline_files = None
        for optimized, workers in ((False, 1), (True, 1), (True, 3), (True, 5)):
            name = ("rich" if rich else "sparse") + (f"_optimized_{workers}" if optimized else "_baseline")
            samples = []
            for _ in range(args.repetitions):
                with TemporaryDirectory(prefix="ig-engine-benchmark-", dir=root / "build") as temporary:
                    sample, files = run_case(Path(temporary), optimized, workers, rich, args.posts)
                if baseline_files is None:
                    baseline_files = files
                assert files == baseline_files, "The pipelines produced different files or bytes"
                assert sample["http_requests"] == sample["observed_requests"]
                samples.append(sample)
            result["cases"][name] = {"samples": samples, "identical_output": True}
            print(name + ": " + json.dumps({k: samples[0][k] for k in
                ("files", "elapsed_seconds", "http_requests", "observed_metadata_requests", "peak_transfers")}), flush=True)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
