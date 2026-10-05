"""Common types returned by all DSO parsers."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawOutage:
    """A normalized outage record produced by any DSO parser."""

    source: str  # erm_zapad | er_yug | erp_sever
    type: str  # planned | unplanned
    description_raw: str
    affected_places_text: str
    affected_place_tokens: list[str] = field(default_factory=list)
    start_at: datetime | None = None
    end_at: datetime | None = None
    published_at: datetime | None = None
    region: str | None = None  # oblast name when known
    source_url: str | None = None

    @property
    def source_ref(self) -> str:
        """Stable dedup key: hash of source + period + truncated places text."""
        basis = "|".join([
            self.source,
            self.start_at.isoformat() if self.start_at else "",
            self.end_at.isoformat() if self.end_at else "",
            self.affected_places_text[:200],
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
