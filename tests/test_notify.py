from datetime import datetime, timedelta, timezone

from publisher.notify import is_quiet, pushes_for, run
from publisher.topics import fnv1a64_hex, matches, oblast_key, place_topic

# 12:00 Sofia time (UTC+3 in October): outside quiet hours
NOON = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc)  # 00:00 Sofia


def _outage(oid="a1", type_="planned", region="ВАРНА", text="гр. Варна, кв. Чайка", tokens=("варна", "чайка"),
            start=NOON + timedelta(days=1), hours=4, **extra):
    return {
        "id": oid, "type": type_, "region": region, "affected_places_text": text,
        "affected_place_tokens": list(tokens),
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=hours)).isoformat(), **extra,
    }


class Recorder:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def __call__(self, pushes):
        self.sent.extend(pushes)
        return [self.error] * len(pushes)


# Shared vectors: the app's test/topics_test.dart asserts the same values
def test_topic_vectors_match_the_app():
    assert fnv1a64_hex("") == "cbf29ce484222325"
    assert fnv1a64_hex("a") == "af63dc4c8601ec8c"
    assert fnv1a64_hex("foobar") == "85944171f73967e8"
    assert place_topic("София", "София", True) == "p_b4dae05baaece499_p"
    assert place_topic("гр. Варна", "Варна", False) == "p_5c00df9ce92cc047_u"
    assert place_topic("София", "СОФИЯ-ГРАД", True) == place_topic("София", "София", True)


def test_matching_mirrors_the_app():
    sofia = {"region": "СОФИЯ-ГРАД", "affected_places_text": "ЖК.ЛОЗЕНЕЦ", "affected_place_tokens": ["лозенец"]}
    assert matches("софия", "София", sofia)
    assert matches("лозенец", "София", sofia)
    other = {"region": "БЛАГОЕВГРАД", "affected_places_text": "БИСТРИЦА", "affected_place_tokens": ["бистрица"]}
    assert matches("бистрица", "Благоевград", other)
    assert not matches("бистрица", "Кюстендил", other)
    assert oblast_key("СОФИЙСКА") == oblast_key("София област")


def test_pushes_name_each_matched_place():
    pushes = pushes_for(_outage())
    bodies = {p.body.split(":")[0] for p in pushes}
    assert "Варна" in bodies
    assert all(p.outage_id == "a1" and not p.urgent for p in pushes)
    assert all(p.topic.endswith("_p") for p in pushes)
    assert all(p.title == "Планирано прекъсване на тока" for p in pushes)


def test_first_run_bootstraps_without_sending():
    feed = {"outages": [_outage()]}
    rec = Recorder()
    summary = run(feed, NOON, rec)
    assert summary == {"bootstrapped": 1}
    assert rec.sent == []
    assert feed["outages"][0]["notified_at"]
    assert "push" in feed


def test_new_outage_is_sent_once():
    feed = {"push": {}, "outages": [_outage()]}
    rec = Recorder()
    run(feed, NOON, rec)
    assert rec.sent and feed["outages"][0]["notified_at"]
    again = Recorder()
    run(feed, NOON + timedelta(minutes=20), again)
    assert again.sent == []


def test_planned_held_at_night_unplanned_sent():
    feed = {"push": {}, "outages": [_outage("p1"), _outage("u1", type_="unplanned")]}
    rec = Recorder()
    summary = run(feed, NIGHT, rec)
    assert {p.outage_id for p in rec.sent} == {"u1"}
    assert summary["held_for_morning"] == 1
    assert "notified_at" not in feed["outages"][0]
    morning = Recorder()
    run(feed, NOON + timedelta(days=1), morning)
    assert {p.outage_id for p in morning.sent} == {"p1"}


def test_past_and_far_future_outages_are_marked_not_sent():
    feed = {"push": {}, "outages": [
        _outage("old", start=NOON - timedelta(days=2)),
        _outage("far", start=NOON + timedelta(days=60)),
    ]}
    rec = Recorder()
    summary = run(feed, NOON, rec)
    assert rec.sent == [] and summary["skipped_irrelevant"] == 2
    assert all(o.get("notified_at") for o in feed["outages"])


def test_no_credentials_marks_without_sending():
    feed = {"push": {}, "outages": [_outage()]}
    summary = run(feed, NOON, None)
    assert summary["sent"] is False and summary["messages"] > 0
    assert feed["outages"][0]["notified_at"]


def test_failed_sends_are_still_marked():
    feed = {"push": {}, "outages": [_outage()]}
    summary = run(feed, NOON, Recorder(error="quota"))
    assert summary["failed"] > 0
    assert feed["outages"][0]["notified_at"]


def test_quiet_hours_are_sofia_time():
    assert is_quiet(NIGHT)
    assert not is_quiet(NOON)
    assert is_quiet(datetime(2026, 10, 7, 3, 30, tzinfo=timezone.utc))  # 06:30 Sofia
    assert not is_quiet(datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc))  # 07:00 Sofia
