"""One anonymous native bulk-list query. Stop on any error; retain shapes, never URLs."""
import argparse
import json
from pathlib import Path

from desktop.live_diagnostic import BoundedControl
from desktop.models import parse_profile
from desktop.services.engine import EngineService
from desktop.services.errors import classify_error
from desktop.services.performance import RunStatistics, preset_pacing
from desktop.services.request_trace import RequestTrace
from desktop.services.safety import RestrictionGate
from desktop.storage.local import atomic_json, data_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    username = parse_profile(args.profile)
    root = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    if not output.is_relative_to(root / "build"):
        parser.error("Output must be below build.")
    state = data_directory()
    settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
    gate = RestrictionGate(settings.get("restriction"))
    if gate.review or gate.remaining() > 0:
        raise RuntimeError("Restriction prevents probing; no request made.")
    stats = RunStatistics()
    control = BoundedControl(state / "queue.json", 1, 1, 60, stats)
    control.checkpoint()
    engine = EngineService(pacing=preset_pacing("Balanced"))
    engine.statistics, engine.safety, engine.control = stats, gate, control
    engine.trace = RequestTrace(stats)
    before = gate.events
    result = {"outcome": "unavailable", "nodes": 0, "nodes_with_media_urls": 0}
    try:
        loader = engine._create()
        # Exactly the query already used by bundled Profile.get_posts for the web feed.
        # This probe remains anonymous. No fallback endpoint or authentication is attempted.
        response = loader.context.doc_id_graphql_query("29104886249145907", {
            "data": {"count": 12, "include_reel_media_seen_timestamp": True,
                     "include_relationship_info": True, "latest_besties_reel_media": True, "latest_reel_media": True},
            "first": 12, "include_multi_captions": True, "username": username,
            "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": True,
            "__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider": False,
            "__relay_internal__pv__PolarisThreadsStyleCommentsEnabledrelayprovider": False,
            "__relay_internal__pv__PolarisReelsRecoDebugOverlayEnabledrelayprovider": False},
            f"https://www.instagram.com/{username}/", web_api=True)
        edges = ((response.get("data") or {}).get("xdt_api__v1__feed__user_timeline_graphql_connection") or {}).get("edges") or []
        result["nodes"] = len(edges)
        for edge in edges:
            media = edge.get("node") or {}
            if (media.get("image_versions2") or {}).get("candidates"):
                result["nodes_with_media_urls"] += 1
        if result["nodes_with_media_urls"]:
            result["outcome"] = "bulk_available"
    except Exception as error:
        gate.inspect_exception(error, lambda *_: None, before)
        result["outcome"] = classify_error(error).kind
    finally:
        engine.close()
        result["diagnostics"] = stats.finish()
        atomic_json(output, result)
        if gate.events > before or gate.remaining() > 0:
            current = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            current["restriction"] = gate.state()
            atomic_json(state / "settings.json", current)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
