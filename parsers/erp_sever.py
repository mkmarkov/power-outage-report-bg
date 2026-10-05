"""ERPSever (Energo-Pro, North-East Bulgaria) outage fetcher.

Uses the same XHR endpoint as https://www.erpsever.bg/bg/prekysvanija.
The endpoint returns valid JSON only when called with browser-like headers
(Referer + X-Requested-With); without them it responds with an empty body.
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx

from .base import RawOutage
from .normalize import SOFIA_TZ, classify_outage, parse_period, strip_html, tokenize_places

BASE_URL = "https://www.erpsever.bg/bg/profil/xhr/"
PUBLIC_PAGE = "https://www.erpsever.bg/bg/prekysvanija"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": PUBLIC_PAGE,
    "X-Requested-With": "XMLHttpRequest",
}

REGIONS = {
    1: "Варна",
    2: "Велико Търново",
    3: "Габрово",
    4: "Добрич",
    5: "Разград",
    6: "Русе",
    7: "Силистра",
    8: "Търговище",
    9: "Шумен",
}

_PUBLISHED_RE = re.compile(r"Публикувано на (\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})")

# JSON keys holding outage lists, by query type
_LIST_KEYS = {
    "for_next_48_hours": "area_locations_for_next_48_hours",
    "all_active": "area_locations_all_active",
}


def _parse_published(html: str) -> datetime | None:
    m = _PUBLISHED_RE.search(html)
    if not m:
        return None
    d, mo, y, h, mi = (int(g) for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, tzinfo=SOFIA_TZ)
    except ValueError:
        return None


def fetch_region(
    client: httpx.Client, region_id: int, type_: str = "for_next_48_hours"
) -> list[RawOutage]:
    resp = client.get(
        BASE_URL,
        params={
            "method": "get_interruptions",
            "region_id": region_id,
            "type": type_,
            "offset": 0,
        },
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    if not resp.text.strip():
        return []
    data = resp.json()

    outages: list[RawOutage] = []
    list_key = _LIST_KEYS[type_]
    for area in data:
        for loc in area.get(list_key, []):
            period_html = loc.get("location_period", "")
            text_html = loc.get("location_text", "")
            start_at, end_at = parse_period(period_html)
            plain_text = strip_html(text_html)
            outages.append(
                RawOutage(
                    source="erp_sever",
                    type=classify_outage(plain_text),
                    description_raw=f"{strip_html(period_html)}\n{plain_text}",
                    affected_places_text=plain_text,
                    affected_place_tokens=tokenize_places(text_html),
                    start_at=start_at,
                    end_at=end_at,
                    published_at=_parse_published(text_html),
                    region=area.get("area_name") or REGIONS.get(region_id),
                    source_url=PUBLIC_PAGE,
                )
            )
    return outages


def fetch_all(types: tuple[str, ...] = ("for_next_48_hours", "all_active")) -> list[RawOutage]:
    """Fetch outages for all nine oblasts. Failures in one region don't abort the rest."""
    results: list[RawOutage] = []
    errors: list[str] = []
    with httpx.Client() as client:
        for region_id in REGIONS:
            for type_ in types:
                try:
                    results.extend(fetch_region(client, region_id, type_))
                except Exception as exc:  # noqa: BLE001 - collected for source monitoring
                    errors.append(f"region={region_id} type={type_}: {exc}")
    if errors and not results:
        raise RuntimeError("ERPSever fetch failed for all regions: " + "; ".join(errors))
    return results
