# Има ли ток — outages feed

Scheduled scraper that polls Bulgaria's three electricity distribution
operators and publishes a single static feed for the **Има ли ток** app:

**https://mkmarkov.github.io/power-outage-report-bg/outages.json**

| DSO | Coverage | Integration | Polled |
|-----|----------|-------------|--------|
| ЕРП Север (Energo-Pro) | North-East | JSON XHR endpoint | every run (~20 min) |
| ЕР Юг (EVN) | South-East | Legacy ASP pages (windows-1251) | every 40 min |
| ЕРМ Запад (Electrohold) | West | Daily PDF schedules | every 3 h |

A GitHub Actions workflow (`.github/workflows/publish.yml`) runs every
20 minutes, merges fresh results with the currently published feed and deploys
it to GitHub Pages. If a source fails, its last known outages are kept and its
failure count goes up; after 3 consecutive failures the run fails, which
emails the repository owner.

## Feed format

```jsonc
{
  "version": 1,
  "generated_at": "2026-10-06T12:00:00+00:00",
  "sources": {
    "erp_sever": {
      "ok": true, "consecutive_failures": 0, "last_error": null,
      "last_attempt_at": "...", "last_success_at": "...", "outage_count": 258
    }
  },
  "outages": [
    {
      "id": "stable 32-char hash",
      "source": "erp_sever | er_yug | erm_zapad",
      "type": "planned | unplanned",
      "start_at": "ISO 8601 UTC or null",
      "end_at": "ISO 8601 UTC or null",
      "affected_places_text": "raw text from the DSO",
      "affected_place_tokens": ["normalized", "place", "names"],
      "description": "...",
      "region": "oblast or null",
      "source_url": "...",
      "published_at": "ISO 8601 UTC or null",
      "first_seen_at": "when this feed first saw the outage"
    }
  ]
}
```

Outages are removed 6 hours after they end (2 days after start when the DSO
gives no end time). Pages caches files for 10 minutes; append `?t=<timestamp>`
to bypass the cache.

## Push notifications

After building the feed, `publisher/notify.py` sends each new outage to the
FCM topic of every gazetteer place it matches (`publisher/topics.py`, one topic
per place and outage type: `p_<fnv1a64(oblast|place)>_p` or `_u`), then marks
it with `notified_at` in the feed. The app subscribes to the topics of its
watched places (`mobile/lib/services/topics.dart`; both sides test the same
vectors). Guards: the first run only marks; without the
`FIREBASE_SERVICE_ACCOUNT` secret it only marks; planned outages are held
22:00-07:00 Sofia time; at most 500 messages per run. `data/settlements.json`
is a copy of the app's `mobile/assets/settlements.json`: update both together.

Dry run against a feed: `python -m publisher.notify --feed site/outages.json --dry-run`.

## Local run

```bash
pip install -r requirements.txt pytest
python -m pytest tests
python -m publisher.build_feed --out site --force
```
