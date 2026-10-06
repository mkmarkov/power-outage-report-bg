"""Push notifications for new outages via FCM topics.

Runs after build_feed and before deploy (see .github/workflows/publish.yml):

    python -m publisher.notify --feed site/outages.json

Each outage the feed hasn't pushed yet (no `notified_at`) is sent to the topic
of every gazetteer place it matches (publisher/topics.py), then marked, and
the feed is written back so the next run knows. Guards against spam:

- The first run ever (feed without a "push" block, e.g. after the previous
  feed could not be loaded) marks everything as notified and sends nothing.
- Without credentials, outages are marked but not sent, so adding the secret
  later doesn't flush a backlog.
- New planned outages are held between 22:00 and 07:00 Sofia time; emergencies
  go out at once.
- At most MAX_MESSAGES per run; the rest wait for the next run.

Credentials: FIREBASE_SERVICE_ACCOUNT (the service-account JSON itself, as a
GitHub Actions secret) or GOOGLE_APPLICATION_CREDENTIALS (a path).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from parsers.normalize import normalize_place

from .topics import matches, place_topic, settlements

logger = logging.getLogger("publisher.notify")

SOFIA_TZ = ZoneInfo("Europe/Sofia")
MAX_MESSAGES = 500
FUTURE_WINDOW = timedelta(days=30)
QUIET_START_HOUR = 22
QUIET_END_HOUR = 7


@dataclass(frozen=True)
class Push:
    topic: str
    title: str
    body: str
    outage_id: str
    urgent: bool


Sender = Callable[[list[Push]], list[str | None]]  # per push: None = ok, else error


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def is_quiet(now: datetime) -> bool:
    hour = now.astimezone(SOFIA_TZ).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def is_relevant(outage: dict, now: datetime) -> bool:
    """Not over yet and not more than FUTURE_WINDOW away."""
    end = _parse(outage.get("end_at"))
    start = _parse(outage.get("start_at"))
    if end and end <= now:
        return False
    return start is None or start <= now + FUTURE_WINDOW


def _when(outage: dict) -> str:
    fmt = "%d.%m %H:%M"
    start = _parse(outage.get("start_at"))
    end = _parse(outage.get("end_at"))
    if not start:
        return ""
    text = start.astimezone(SOFIA_TZ).strftime(fmt)
    if end:
        same_day = end.astimezone(SOFIA_TZ).date() == start.astimezone(SOFIA_TZ).date()
        text += " - " + end.astimezone(SOFIA_TZ).strftime("%H:%M" if same_day else fmt)
    return text


def pushes_for(outage: dict) -> list[Push]:
    """One push per matched place topic, naming that place."""
    planned = outage.get("type") != "unplanned"
    title = "Планирано прекъсване на тока" if planned else "Авария - прекъсване на тока"
    when = _when(outage)
    text_norm = normalize_place(outage.get("affected_places_text") or "")
    result = []
    for s in settlements():
        if matches(s.norm, s.oblast, outage, text_norm):
            body = f"{s.name}: {when}" if when else s.name
            result.append(Push(place_topic(s.name, s.oblast, planned), title, body, outage["id"], urgent=not planned))
    return result


def run(feed: dict, now: datetime, send: Sender | None) -> dict:
    """Marks and (with a sender) pushes new outages. Returns a summary."""
    outages = feed.get("outages", [])
    if "push" not in feed:
        for o in outages:
            o["notified_at"] = _iso(now)
        feed["push"] = {"bootstrapped_at": _iso(now), "last_run_at": _iso(now), "last_sent": 0}
        logger.info("First run: marked %d outage(s) as notified, sent nothing", len(outages))
        return {"bootstrapped": len(outages)}

    quiet = is_quiet(now)
    pending: list[tuple[dict, list[Push]]] = []
    budget = MAX_MESSAGES
    held = skipped = 0
    for o in outages:
        if o.get("notified_at"):
            continue
        if not is_relevant(o, now):
            o["notified_at"] = _iso(now)
            skipped += 1
            continue
        if quiet and o.get("type") != "unplanned":
            held += 1
            continue
        pushes = pushes_for(o)
        if len(pushes) > budget:
            break  # next run
        budget -= len(pushes)
        pending.append((o, pushes))

    batch = [p for _, ps in pending for p in ps]
    errors: list[str | None] = []
    if batch and send is not None:
        errors = send(batch)
    failed = [e for e in errors if e]
    for o, _ in pending:
        o["notified_at"] = _iso(now)  # marked even on failure: never re-spam

    feed["push"].update(last_run_at=_iso(now), last_sent=len(batch) - len(failed) if send else 0)
    summary = {
        "outages": len(pending),
        "messages": len(batch),
        "failed": len(failed),
        "held_for_morning": held,
        "skipped_irrelevant": skipped,
        "sent": send is not None,
    }
    for e in failed[:5]:
        logger.error("FCM send failed: %s", e)
    logger.info("Push run: %s", summary)
    return summary


def firebase_sender() -> Sender | None:
    """FCM HTTP v1 sender, or None when no credentials are configured."""
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw and not path:
        return None

    import firebase_admin
    from firebase_admin import credentials, messaging

    cred = credentials.Certificate(json.loads(raw) if raw else path)
    app = firebase_admin.initialize_app(cred)

    def send(pushes: list[Push]) -> list[str | None]:
        results: list[str | None] = []
        for i in range(0, len(pushes), 500):  # send_each limit
            chunk = pushes[i:i + 500]
            messages = [
                messaging.Message(
                    topic=p.topic,
                    notification=messaging.Notification(title=p.title, body=p.body),
                    data={"outage_id": p.outage_id},
                    android=messaging.AndroidConfig(
                        priority="high" if p.urgent else "normal",
                        # Same tag = one notification per outage on a device that
                        # watches several matched places
                        notification=messaging.AndroidNotification(channel_id="outages", tag=p.outage_id),
                    ),
                )
                for p in chunk
            ]
            response = messaging.send_each(messages, app=app)
            results.extend(None if r.success else str(r.exception) for r in response.responses)
        return results

    return send


def main() -> int:
    parser = argparse.ArgumentParser(description="Push new outages to FCM topics.")
    parser.add_argument("--feed", type=Path, required=True, help="outages.json to read and update")
    parser.add_argument("--dry-run", action="store_true", help="Compute pushes, send nothing, don't write")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    feed = json.loads(args.feed.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    if args.dry_run:
        sent: list[Push] = []
        summary = run(feed, now, lambda ps: (sent.extend(ps), [None] * len(ps))[1])
        for p in sent[:20]:
            print(f"{p.topic}  {p.title} | {p.body}")
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    sender = firebase_sender()
    if sender is None:
        logger.warning("No Firebase credentials: outages are marked but no pushes are sent")
    summary = run(feed, now, sender)
    args.feed.write_text(json.dumps(feed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 1 if summary.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
