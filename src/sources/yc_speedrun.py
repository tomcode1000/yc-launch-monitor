"""Speedrun adapter.

A note on naming, because it matters and the task brief gets it wrong.

The brief lists "YC Speedrun page - YC's dedicated Speedrun program directory"
as one of the four required sources. There is no such YC program. All 50
batches in YC's own dataset, plus every tag and industry label, contain zero
occurrences of "speedrun" - checked against api.ycombinator.com, not assumed.
SPEEDRUN is an Andreessen Horowitz accelerator, and its portfolio lives at
speedrun.a16z.com.

So this file keeps the name the brief uses - it is the required fourth source -
while pointing at the directory where the companies actually are. It also
matches Speedrun mentions coming from the social adapters, so a founder
announcing an a16z SPEEDRUN batch is detected the same way a YC founder is.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from ..models import Program, RawSignal, normalize_company
from .base import Source

log = logging.getLogger(__name__)

UA = "yc-launch-monitor/1.0"

# The portfolio is a rendered page rather than an API, so pull company links
# out of the markup and treat additions as new-company signals.
LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>[^"]*/(?:companies|portfolio)/[^"]+)"[^>]*>(?P<label>.*?)</a>',
    re.I | re.S,
)
TAG_RE = re.compile(r"<[^>]+>")


class SpeedrunSource(Source):
    name = "yc_speedrun"

    def __init__(self, config: dict, store=None, **kw):
        super().__init__(config, **kw)
        self.store = store
        self.url = config.get("portfolio_url", "https://speedrun.a16z.com/")
        self._index: dict[str, dict] = {}

    def fetch(self, since: datetime) -> list[RawSignal]:
        companies = self._scrape()
        signals: list[RawSignal] = []
        for company in companies:
            key = normalize_company(company["name"])
            self._index[key] = company
            if self.store is not None:
                if self.store.has_alerted(key):
                    self.store.mark_confirmed(key)
                    continue
                if self.store.seen_signal(self.name, company["slug"]):
                    continue
            signals.append(
                RawSignal(
                    source=self.name,
                    external_id=company["slug"],
                    text=f"{company['name']} joined a16z SPEEDRUN",
                    url=company["url"],
                    created_at=datetime.now(timezone.utc),
                    raw=company,
                )
            )
        return signals

    def lookup(self, company_name: str) -> dict | None:
        """Suppression oracle for the Speedrun side, mirroring the YC one."""
        return self._index.get(normalize_company(company_name))

    def _scrape(self) -> list[dict]:
        with httpx.Client(timeout=30, headers={"User-Agent": UA},
                          follow_redirects=True) as client:
            resp = client.get(self.url)
            resp.raise_for_status()
            html = resp.text

        seen: dict[str, dict] = {}
        for match in LINK_RE.finditer(html):
            href = match.group("href")
            label = TAG_RE.sub("", match.group("label")).strip()
            if not label or len(label) > 80:
                continue
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            if slug in seen:
                continue
            seen[slug] = {
                "name": label,
                "slug": slug,
                "url": httpx.URL(self.url).join(href).human_repr(),
                "program": Program.SPEEDRUN.value,
            }
        return list(seen.values())

    @staticmethod
    def to_alert_fields(company: dict) -> dict:
        return {
            "company": company.get("name"),
            "batch": "SPEEDRUN",
            "program": Program.SPEEDRUN.value,
            "description": None,
            "url": company.get("url"),
            "website": None,
        }
