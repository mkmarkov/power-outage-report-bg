"""FCM topic names and place matching, mirroring the app.

Keep in sync with the Има ли ток app (electricity-app/mobile):
lib/services/topics.dart (topic names) and lib/services/matching.dart
(matchOutage). Both test suites assert the same topic vectors.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from parsers.normalize import normalize_place

SETTLEMENTS_PATH = Path(__file__).resolve().parent.parent / "data" / "settlements.json"

_WS_RE = re.compile(r"\s+")


def fnv1a64_hex(text: str) -> str:
    """64-bit FNV-1a of the UTF-8 bytes, as 16 lowercase hex digits."""
    h = 0xCBF29CE484222325
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def oblast_key(name: str) -> str:
    """Comparable oblast name across the gazetteer and the DSO feeds."""
    k = _WS_RE.sub(" ", name.lower().strip())
    if k in ("софия-град", "софия град"):
        return "софия"
    if k in ("софийска", "софия-област", "софия област"):
        return "софия област"
    return k


def place_topic(place: str, oblast: str, planned: bool) -> str:
    key = f"{oblast_key(oblast)}|{normalize_place(place)}"
    return f"p_{fnv1a64_hex(key)}_{'p' if planned else 'u'}"


def _is_city_region(needle: str, region: str) -> bool:
    r = region.lower().strip()
    return r.endswith("-град") and needle == r[: -len("-град")]


def matches(place_norm: str, oblast: str | None, outage: dict, text_norm: str | None = None) -> bool:
    """Same rules as the app's matchOutage; place_norm is normalize_place(place),
    text_norm optionally the precomputed normalize_place of the places text."""
    if not place_norm:
        return False
    region = outage.get("region") or ""
    if region:
        if _is_city_region(place_norm, region):
            return True
        if oblast is not None and oblast_key(oblast) != oblast_key(region):
            return False
    tokens = outage.get("affected_place_tokens") or []
    if place_norm in tokens or any(place_norm in t for t in tokens):
        return True
    if text_norm is None:
        text_norm = normalize_place(outage.get("affected_places_text") or "")
    return place_norm in text_norm


@dataclass(frozen=True)
class Settlement:
    name: str
    oblast: str
    norm: str


@lru_cache(maxsize=1)
def settlements() -> tuple[Settlement, ...]:
    """The app's gazetteer (data/settlements.json, copied from mobile/assets)."""
    rows = json.loads(SETTLEMENTS_PATH.read_text(encoding="utf-8"))
    seen: set[tuple[str, str]] = set()
    result = []
    for row in rows:
        s = Settlement(row["n"], row["o"], normalize_place(row["n"]))
        if (s.oblast, s.norm) not in seen:
            seen.add((s.oblast, s.norm))
            result.append(s)
    return tuple(result)


def topics_for(outage: dict) -> list[str]:
    """Topics of every gazetteer place the outage matches, for its type."""
    planned = outage.get("type") != "unplanned"
    text_norm = normalize_place(outage.get("affected_places_text") or "")
    return sorted({
        place_topic(s.name, s.oblast, planned)
        for s in settlements()
        if matches(s.norm, s.oblast, outage, text_norm)
    })
