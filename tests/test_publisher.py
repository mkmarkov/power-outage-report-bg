from datetime import datetime, timedelta, timezone

from parsers.base import RawOutage
from publisher.build_feed import build_feed

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _outage(source: str, place: str, start: datetime, hours: int = 4) -> RawOutage:
    return RawOutage(
        source=source,
        type="planned",
        description_raw=place,
        affected_places_text=place,
        affected_place_tokens=[place.lower()],
        start_at=start,
        end_at=start + timedelta(hours=hours),
    )


def _fail():
    raise RuntimeError("site down")


def test_first_run_publishes_all_sources():
    a = _outage("north", "Варна", NOW + timedelta(days=1))
    b = _outage("south", "Пловдив", NOW + timedelta(hours=2))
    feed = build_feed({}, NOW, sources_config={
        "north": (lambda: [a], 15),
        "south": (lambda: [b], 15),
    })
    assert [o["id"] for o in feed["outages"]] == [b.source_ref, a.source_ref]  # sorted by start
    assert feed["sources"]["north"]["ok"] is True
    assert feed["outages"][0]["first_seen_at"] == NOW.isoformat(timespec="seconds")


def test_failed_source_keeps_previous_outages_and_counts_failures():
    a = _outage("north", "Варна", NOW + timedelta(days=1))
    first = build_feed({}, NOW, sources_config={"north": (lambda: [a], 15)})

    later = NOW + timedelta(minutes=20)
    second = build_feed(first, later, sources_config={"north": (_fail, 15)})
    assert [o["id"] for o in second["outages"]] == [a.source_ref]
    assert second["sources"]["north"]["ok"] is False
    assert second["sources"]["north"]["consecutive_failures"] == 1

    third = build_feed(second, later + timedelta(minutes=20), sources_config={"north": (_fail, 15)})
    assert third["sources"]["north"]["consecutive_failures"] == 2


def test_source_not_due_is_not_polled():
    a = _outage("west", "София", NOW + timedelta(days=1))
    first = build_feed({}, NOW, sources_config={"west": (lambda: [a], 180)})

    second = build_feed(first, NOW + timedelta(minutes=20), sources_config={"west": (_fail, 180)})
    assert second["sources"]["west"]["ok"] is True  # fetch was skipped, not failed
    assert [o["id"] for o in second["outages"]] == [a.source_ref]


def test_first_seen_is_preserved_and_ended_outages_are_dropped():
    keep = _outage("north", "Варна", NOW + timedelta(days=1))
    ended = _outage("north", "Русе", NOW - timedelta(days=1))
    first = build_feed({}, NOW - timedelta(hours=1), sources_config={"north": (lambda: [keep], 15)})

    second = build_feed(first, NOW, sources_config={"north": (lambda: [keep, ended], 15)})
    assert [o["id"] for o in second["outages"]] == [keep.source_ref]
    assert second["outages"][0]["first_seen_at"] == first["outages"][0]["first_seen_at"]
