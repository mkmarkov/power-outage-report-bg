"""Static feed builder: polls the DSOs and writes outages.json for GitHub Pages.

Runs on a GitHub Actions schedule (.github/workflows/publish.yml). Each run
merges with the previously published feed so a source that fails (or is not
due yet) keeps its last known outages instead of disappearing from the app.

Run from the repository root:
    python -m publisher.build_feed --out site --previous-url https://.../outages.json
    python -m publisher.build_feed --check-health site/outages.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from parsers import er_yug, erm_zapad, erp_sever
from parsers.base import RawOutage

logger = logging.getLogger("publisher")

FEED_VERSION = 1

# source -> (fetch function, minimum minutes between polls). ERM Zapad
# publishes daily PDFs, so polling it on every run would only add load.
SOURCES: dict[str, tuple[Callable[[], list[RawOutage]], int]] = {
    "erp_sever": (erp_sever.fetch_all, 15),
    "er_yug": (er_yug.fetch_all, 40),
    "erm_zapad": (erm_zapad.fetch_all, 180),
}

# Outages are dropped from the feed this long after they end (or after they
# start, when the DSO gave no end time).
KEEP_AFTER_END = timedelta(hours=6)
KEEP_WITHOUT_END = timedelta(days=2)

# A source is reported unhealthy after this many consecutive failures (NFR-04)
HEALTH_THRESHOLD = 3


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def outage_to_json(o: RawOutage, first_seen_at: str) -> dict:
    return {
        "id": o.source_ref,
        "source": o.source,
        "type": o.type,
        "start_at": _iso(o.start_at),
        "end_at": _iso(o.end_at),
        "affected_places_text": o.affected_places_text,
        "affected_place_tokens": o.affected_place_tokens,
        "description": o.description_raw,
        "region": o.region,
        "source_url": o.source_url,
        "published_at": _iso(o.published_at),
        "first_seen_at": first_seen_at,
    }


def load_previous(url: str | None, path: Path | None) -> dict:
    """Previously published feed, or an empty one on first run / any error."""
    try:
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if url:
            # Cache-buster: Pages serves files with max-age=600
            resp = httpx.get(url, params={"t": int(datetime.now().timestamp())}, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Previous feed unavailable (HTTP %s), starting fresh", resp.status_code)
    except Exception:  # noqa: BLE001 - a broken previous feed must not block publishing
        logger.exception("Could not load previous feed, starting fresh")
    return {"sources": {}, "outages": []}


def is_current(outage: dict, now: datetime) -> bool:
    end = _parse_iso(outage.get("end_at"))
    if end:
        return end >= now - KEEP_AFTER_END
    start = _parse_iso(outage.get("start_at"))
    return start is None or start >= now - KEEP_WITHOUT_END


def build_feed(
    previous: dict,
    now: datetime,
    force: bool = False,
    sources_config: dict[str, tuple[Callable[[], list[RawOutage]], int]] = SOURCES,
) -> dict:
    prev_outages: dict[str, list[dict]] = {}
    for o in previous.get("outages", []):
        prev_outages.setdefault(o["source"], []).append(o)
    prev_sources: dict = previous.get("sources", {})

    sources: dict[str, dict] = {}
    outages: list[dict] = []

    for name, (fetch, min_interval) in sources_config.items():
        state = dict(prev_sources.get(name) or {})
        last_attempt = _parse_iso(state.get("last_attempt_at"))
        due = force or last_attempt is None or now - last_attempt >= timedelta(minutes=min_interval)
        kept = prev_outages.get(name, [])

        if not due:
            logger.info("%s: not due, keeping %d outage(s)", name, len(kept))
            outages.extend(kept)
            sources[name] = state
            continue

        state["last_attempt_at"] = _iso(now)
        try:
            raw = fetch()
        except Exception as exc:  # noqa: BLE001 - recorded in the feed's source status
            logger.exception("%s: fetch failed, keeping %d previous outage(s)", name, len(kept))
            state["ok"] = False
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            state["last_error"] = str(exc)[:500]
            outages.extend(kept)
            sources[name] = state
            continue

        first_seen = {o["id"]: o.get("first_seen_at") for o in kept}
        notified = {o["id"]: o["notified_at"] for o in kept if o.get("notified_at")}
        by_id: dict[str, dict] = {}
        for o in raw:
            entry = outage_to_json(o, first_seen.get(o.source_ref) or _iso(now))
            # Push state (publisher/notify.py) survives a re-fetch of the same outage
            if o.source_ref in notified:
                entry["notified_at"] = notified[o.source_ref]
            by_id[o.source_ref] = entry
        new_count = sum(1 for oid in by_id if oid not in first_seen)
        logger.info("%s: %d fetched, %d unique, %d new", name, len(raw), len(by_id), new_count)

        state.update(
            ok=True,
            consecutive_failures=0,
            last_error=None,
            last_success_at=_iso(now),
            outage_count=len(by_id),
        )
        outages.extend(by_id.values())
        sources[name] = state

    outages = [o for o in outages if is_current(o, now)]
    outages.sort(key=lambda o: (o.get("start_at") or "", o["id"]))
    feed = {
        "version": FEED_VERSION,
        "generated_at": _iso(now),
        "sources": sources,
        "outages": outages,
    }
    if "push" in previous:
        feed["push"] = previous["push"]
    return feed


def check_health(feed_path: Path) -> int:
    feed = json.loads(feed_path.read_text(encoding="utf-8"))
    bad = {
        name: s for name, s in feed.get("sources", {}).items()
        if s.get("consecutive_failures", 0) >= HEALTH_THRESHOLD
    }
    for name, s in bad.items():
        # GitHub Actions annotation; the failed run triggers an email
        print(f"::error::{name} failed {s['consecutive_failures']} times in a row: {s.get('last_error')}")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static outages.json feed.")
    parser.add_argument("--out", type=Path, help="Directory to write outages.json into")
    parser.add_argument("--previous-url", help="URL of the currently published outages.json")
    parser.add_argument("--previous-file", type=Path, help="Local previous outages.json (overrides URL)")
    parser.add_argument("--force", action="store_true", help="Poll every source regardless of interval")
    parser.add_argument("--check-health", type=Path, metavar="FEED",
                        help="Exit non-zero if any source in FEED is failing repeatedly")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.check_health:
        return check_health(args.check_health)
    if not args.out:
        parser.error("--out is required")

    previous = load_previous(args.previous_url, args.previous_file)
    feed = build_feed(previous, datetime.now(timezone.utc), force=args.force)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "outages.json").write_text(
        json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    # Serve files as-is instead of running them through Jekyll
    (args.out / ".nojekyll").touch()
    logger.info("Wrote %d outage(s) to %s", len(feed["outages"]), args.out / "outages.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
