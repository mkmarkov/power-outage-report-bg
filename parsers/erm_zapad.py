"""ERM Zapad (Electrohold, Western Bulgaria) daily PDF pipeline.

Endpoints discovered on https://info.ermzapad.bg/webint/vok/avplan.php:
  POST action=showpdf                -> HTML list of daily PDFs (previewdoc(ID) links)
  POST action=showdocid&doc_id={id}  -> the PDF bytes (Content-Type: application/pdf)
"""
from __future__ import annotations

import io
import re
from datetime import datetime

import httpx
import pdfplumber

from .base import RawOutage
from .normalize import SOFIA_TZ, tokenize_places

AVPLAN_URL = "https://info.ermzapad.bg/webint/vok/avplan.php"
PUBLIC_PAGE = "https://info.ermzapad.bg/webint/vok/avplan.php?PLAN=FYI"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_DOC_RE = re.compile(r"previewdoc\((\d+)\).*?(\d{2})\.(\d{2})\.(\d{4})", re.DOTALL)


def fetch_pdf_list(client: httpx.Client) -> list[tuple[int, datetime]]:
    """Return [(doc_id, schedule_date), ...] newest first."""
    resp = client.post(AVPLAN_URL, data={"action": "showpdf"}, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    docs: list[tuple[int, datetime]] = []
    seen: set[int] = set()
    for m in _DOC_RE.finditer(resp.text):
        doc_id = int(m.group(1))
        if doc_id in seen:
            continue
        seen.add(doc_id)
        day, month, year = int(m.group(2)), int(m.group(3)), int(m.group(4))
        docs.append((doc_id, datetime(year, month, day, tzinfo=SOFIA_TZ)))
    return docs


def download_pdf(client: httpx.Client, doc_id: int) -> bytes:
    resp = client.post(
        AVPLAN_URL,
        data={"action": "showdocid", "doc_id": doc_id},
        headers=HEADERS,
        timeout=60,
    )
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise ValueError(f"doc_id={doc_id}: response is not a PDF")
    return resp.content


# The daily schedule PDF is line-oriented:
#   област БЛАГОЕВГРАД Част от населено място в интервала между
#   община БАНСКО За индивидуална проверка натиснете ТУК
#   Част от БАНСКО 15.06.2026 08:30 15.06.2026 16:30
_OBLAST_RE = re.compile(r"^област\s+(.+?)(?:\s+Част от населено.*)?$", re.IGNORECASE)
_OBSHTINA_RE = re.compile(r"^община\s+(.+?)(?:\s+За индивидуална.*)?$", re.IGNORECASE)
_ROW_RE = re.compile(
    r"^Част от\s+(.+?)\s+"
    r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s+"
    r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})\s*$"
)


def parse_pdf(pdf_bytes: bytes, schedule_date: datetime) -> list[RawOutage]:
    """Extract outage rows from a daily schedule PDF (line-based layout)."""
    outages: list[RawOutage] = []
    oblast: str | None = None
    obshtina: str | None = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = _OBLAST_RE.match(line)
                if m:
                    oblast = m.group(1).strip()
                    obshtina = None
                    continue
                m = _OBSHTINA_RE.match(line)
                if m:
                    obshtina = m.group(1).strip()
                    continue
                m = _ROW_RE.match(line)
                if not m:
                    continue
                place = m.group(1).strip()
                d1, mo1, y1, h1, mi1 = (int(m.group(i)) for i in range(2, 7))
                d2, mo2, y2, h2, mi2 = (int(m.group(i)) for i in range(7, 12))
                try:
                    start_at = datetime(y1, mo1, d1, h1, mi1, tzinfo=SOFIA_TZ)
                    end_at = datetime(y2, mo2, d2, h2, mi2, tzinfo=SOFIA_TZ)
                except ValueError:
                    continue
                places_text = ", ".join(
                    p for p in (place, f"общ. {obshtina}" if obshtina else None) if p
                )
                outages.append(
                    RawOutage(
                        source="erm_zapad",
                        type="planned",  # daily schedule PDFs list planned works
                        description_raw=f"Част от {place} ({obshtina or '-'}, {oblast or '-'})",
                        affected_places_text=places_text,
                        affected_place_tokens=tokenize_places(places_text),
                        start_at=start_at,
                        end_at=end_at,
                        published_at=schedule_date,
                        region=oblast,
                        source_url=PUBLIC_PAGE,
                    )
                )
    return outages


def fetch_all(max_docs: int = 2) -> list[RawOutage]:
    """Download and parse the newest `max_docs` daily schedule PDFs."""
    results: list[RawOutage] = []
    with httpx.Client() as client:
        docs = fetch_pdf_list(client)
        for doc_id, schedule_date in docs[:max_docs]:
            pdf_bytes = download_pdf(client, doc_id)
            results.extend(parse_pdf(pdf_bytes, schedule_date))
    return results
