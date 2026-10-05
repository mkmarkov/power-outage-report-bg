"""Normalization helpers shared by all DSO parsers and the matching engine.

All affected-place text is reduced to lowercase tokens with settlement-type
prefixes stripped, so a watchlist entry like "Дряново" matches
"гр. Дряново- ВСК Кентавър" or "Община Дряново - с. Българени".
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

SOFIA_TZ = ZoneInfo("Europe/Sofia")

# Settlement-type prefixes used across DSO texts (гр.=town, с.=village,
# кв.=quarter, общ./Община=municipality, обл.=oblast, ж.к.=housing complex).
# Anchored at the token start; single-letter prefixes (с, м) require a dot so
# names like "Места" or "Снежина" are not mangled.
_PREFIX_RE = re.compile(
    r"^(?:(?:гр|кв|общ|община|обл|област|ж\.к|жк|местност)\.?\s+"
    r"|(?:гр|кв|с|м|общ|обл|жк)\.\s*)+",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Tokens are split on punctuation that separates place names in the lists.
# A dot followed by an uppercase letter is a sentence boundary; dots in
# lowercase prefixes ("гр. Девня" after lowering) are handled by _PREFIX_RE.
_SPLIT_RE = re.compile(r"[,;:\n\r\(\)\[\]/]|(?:\s[-–—]\s)|(?:--)|(?:\.\s+(?=[А-ЯA-Z]))")

# Bare settlement prefixes / generic words that are never place names
_STOPWORDS = {"гр", "с", "кв", "ул", "общ", "обл", "жк", "м", "и", "на", "от", "до", "г"}

PLANNED_KEYWORDS = (
    "планиран", "ремонтни дейности", "профилактика", "подмяна",
    "присъединяване", "реконструкция", "инвестиционн",
)
UNPLANNED_KEYWORDS = (
    "авари", "непланиран", "повреда", "бур", "мълни", "наводнен",
    "заледяван", "снежн", "свличан",
)


def strip_html(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def normalize_place(name: str) -> str:
    """Lowercase, strip settlement prefixes and collapse whitespace."""
    s = unicodedata.normalize("NFC", name).lower().strip()
    s = _PREFIX_RE.sub("", s)
    s = s.replace("\u201e", "").replace("\u201c", "").replace('"', "")
    return _WS_RE.sub(" ", s).strip(" .-")


# Boilerplate phrases that appear in DSO texts but are not place names
_BOILERPLATE_SUBSTRINGS = (
    "проверете за вашия обект",
    "публикувано на",
    "ремонтни дейности",
    "оперативни превключвания",
    "електрозахранван",
    "електроенергия",
    "районите на",
    "възможни смущения",
    "изключването",
    "част от населено",
)


def tokenize_places(text: str) -> list[str]:
    """Split a free-text affected-area description into normalized tokens."""
    plain = strip_html(text)
    tokens: set[str] = set()
    for part in _SPLIT_RE.split(plain):
        norm = normalize_place(part)
        if not (2 <= len(norm) <= 80):
            continue
        if norm in _STOPWORDS:
            continue
        if any(b in norm for b in _BOILERPLATE_SUBSTRINGS):
            continue
        tokens.add(norm)
    return sorted(tokens)


def classify_outage(text: str) -> str:
    """Return 'planned' or 'unplanned' based on description wording."""
    low = text.lower()
    if any(k in low for k in UNPLANNED_KEYWORDS):
        return "unplanned"
    if any(k in low for k in PLANNED_KEYWORDS):
        return "planned"
    return "planned"  # DSO planned-outage feeds default to planned


_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def parse_period(text: str) -> tuple[datetime | None, datetime | None]:
    """Parse period strings such as:
      'На 15.06.2026 г. В периода 9:00 ч. до 13:00 ч.'
      'От 15.06.2026 г. до 19.06.2026 г. В периода 8:30 ч. до 16:30 ч.'

    Dates are extracted first and removed so day/month digits are never
    mistaken for times. Returns timezone-aware (start, end) in Europe/Sofia.
    """
    plain = strip_html(text)
    dates = _DATE_RE.findall(plain)
    if not dates:
        return None, None
    times = _TIME_RE.findall(_DATE_RE.sub(" ", plain))

    d1, mo1, y1 = (int(x) for x in dates[0])
    d2, mo2, y2 = (int(x) for x in dates[-1])
    h1 = m1 = h2 = m2 = 0
    if times:
        h1, m1 = int(times[0][0]), int(times[0][1])
        h2, m2 = int(times[-1][0]), int(times[-1][1])
    try:
        start = datetime(y1, mo1, d1, h1, m1, tzinfo=SOFIA_TZ)
        end = datetime(y2, mo2, d2, h2, m2, tzinfo=SOFIA_TZ)
    except ValueError:
        return None, None
    if not times:
        return start, None
    return start, end
