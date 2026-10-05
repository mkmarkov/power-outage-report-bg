"""ER Yug (EVN, South-East Bulgaria) planned-shutdowns scraper.

The public page https://www.elyug.bg/Customers/Planned_shutdowns.aspx embeds a
legacy ASP app at /oldservices/ec_ep/abschaltung_bg/abschaltung.asp with one
page per oblast (abschaltung1.asp .. abschaltung9.asp), encoded windows-1251.

Each outage is a group of label/value <tr> rows:
    Област                       | БУРГАС
    КЕЦ                          | NR-BN Бургас Север
    Адрес(и)                     | <affected places free text>
    Срок на изключването от / до | 09:00 - 16:00
    Дата(и) на изключването      | 8.6.2026 до 12.6.2026
"""
from __future__ import annotations

import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from .base import RawOutage
from .normalize import SOFIA_TZ, classify_outage, tokenize_places

BASE = "https://www.elyug.bg/oldservices/ec_ep/abschaltung_bg/"
PUBLIC_PAGE = "https://www.elyug.bg/Customers/Planned_shutdowns.aspx"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Region page numbers from the image-map on abschaltung.asp
REGION_PAGES = range(1, 10)

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")


def _build_outage(fields: dict[str, str]) -> RawOutage | None:
    address = fields.get("адрес", "")
    if not address.strip():
        return None

    start_at = end_at = None
    dates = _DATE_RE.findall(fields.get("дата", ""))
    time_m = _TIME_RE.search(fields.get("срок", ""))
    if dates:
        d, mo, y = (int(x) for x in dates[0])
        d2, mo2, y2 = (int(x) for x in dates[-1])
        h1 = m1 = h2 = m2 = 0
        if time_m:
            h1, m1, h2, m2 = (int(g) for g in time_m.groups())
        try:
            start_at = datetime(y, mo, d, h1, m1, tzinfo=SOFIA_TZ)
            end_at = datetime(y2, mo2, d2, h2, m2, tzinfo=SOFIA_TZ)
        except ValueError:
            start_at = end_at = None
    if start_at is None:
        return None

    description = " | ".join(
        f"{k}: {v}" for k, v in fields.items() if v.strip()
    )
    return RawOutage(
        source="er_yug",
        type=classify_outage(address),
        description_raw=description,
        affected_places_text=address,
        affected_place_tokens=tokenize_places(address),
        start_at=start_at,
        end_at=end_at,
        region=fields.get("област") or None,
        source_url=PUBLIC_PAGE,
    )


def parse_region_html(html: str) -> list[RawOutage]:
    soup = BeautifulSoup(html, "lxml")
    outages: list[RawOutage] = []
    fields: dict[str, str] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 2:
            continue
        label = cells[0].get_text(" ", strip=True).lower()
        value = cells[1].get_text(" ", strip=True)
        if not label or label == "\xa0":
            continue
        if label.startswith("област"):
            # New record begins; flush the previous one
            if fields:
                outage = _build_outage(fields)
                if outage:
                    outages.append(outage)
            fields = {"област": value}
        elif label.startswith("кец"):
            fields["кец"] = value
        elif label.startswith("адрес"):
            fields["адрес"] = value
        elif label.startswith("срок"):
            fields["срок"] = value
        elif label.startswith("дата"):
            fields["дата"] = value

    if fields:
        outage = _build_outage(fields)
        if outage:
            outages.append(outage)
    return outages


def fetch_region(client: httpx.Client, region_page: int) -> list[RawOutage]:
    resp = client.get(f"{BASE}abschaltung{region_page}.asp", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "windows-1251"
    return parse_region_html(resp.text)


def fetch_all() -> list[RawOutage]:
    results: list[RawOutage] = []
    errors: list[str] = []
    for region_page in REGION_PAGES:
        try:
            with httpx.Client() as client:
                results.extend(fetch_region(client, region_page))
        except Exception as exc:  # noqa: BLE001 - collected for source monitoring
            errors.append(f"page={region_page}: {exc}")
    if errors and not results and len(errors) == len(list(REGION_PAGES)):
        raise RuntimeError("ER Yug fetch failed for all regions: " + "; ".join(errors))
    return results
