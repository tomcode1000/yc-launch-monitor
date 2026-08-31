"""YC Directory adapter.

Y Combinator publishes a clean, public, unauthenticated JSON API. There is no
reason to scrape the directory page and fight Cloudflare for data that is
served as structured JSON:

    GET https://api.ycombinator.com/v0.1/companies?batch=Fall%202026

    {"companies": [{"name": "...", "slug": "...", "batch": "F26",
                    "oneLiner": "...", "url": "...", "website": "..."}],
     "page": 1, "totalPages": 2, "nextPage": "..."}

This adapter does double duty. It is the source of "confirmed by YC" alerts,
and `lookup()` is the oracle the agent uses to decide whether a founder's post
is still ahead of the official announcement.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from ..models import Program, RawSignal, normalize_batch, normalize_company
from .base import Source

log = logging.getLogger(__name__)

API = "https://api.ycombinator.com/v0.1/companies"
UA = "yc-launch-monitor/1.0 (+https://github.com/)"


class YCDirectorySource(Source):
    name = "yc_directory"

    def __init__(self, config: dict, store=None, **kw):
        super().__init__(config, **kw)
        self.store = store
        self._index: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # --- collection ----------------------------------------------------
    def fetch(self, since: datetime) -> list[RawSignal]:
        batches = self.config.get("batches") or ["Fall 2026", "Winter 2027"]
        signals: list[RawSignal] = []
        for batch in batches:
            for company in self._companies_for(batch):
                key = normalize_company(company["name"])
                self._index[key] = company

                # Already alerted early from social? Then this arrival is the
                # confirmation, and the gap is our lead time. Not a new alert.
                if self.store is not None and self.store.has_alerted(key):
                    self.store.mark_confirmed(key)
                    continue
                if self.store is not None and self.store.seen_signal(self.name, company["slug"]):
                    continue

                signals.append(
                    RawSignal(
                        source=self.name,
                        external_id=company["slug"],
                        text=f"{company['name']} ({company.get('batch')}): "
                        f"{company.get('oneLiner') or ''}",
                        url=company.get("url") or "",
                        created_at=datetime.now(timezone.utc),
                        author=None,
                        raw=company,
                    )
                )
        self._loaded = True
        return signals

    # --- the verification oracle ---------------------------------------
    def lookup(self, company_name: str) -> dict[str, Any] | None:
        """Is this company already listed by YC?

        Absent means the founder's announcement is still ahead of YC - the
        scoop. Present means suppress, or alert as confirmed.
        """
        key = normalize_company(company_name)
        if key in self._index:
            return self._index[key]
        if not self._loaded:
            # Cold start: warm the index once so the first cycle can verify.
            try:
                for batch in self.config.get("batches") or []:
                    for company in self._companies_for(batch):
                        self._index[normalize_company(company["name"])] = company
                self._loaded = True
            except Exception as exc:  # noqa: BLE001
                log.warning("directory warm-up failed: %s", exc)
        return self._index.get(key)

    # --- http ----------------------------------------------------------
    def _companies_for(self, batch: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        url: str | None = API
        params: dict[str, str] | None = {"batch": batch}
        with httpx.Client(timeout=30, headers={"User-Agent": UA}) as client:
            while url:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                out.extend(data.get("companies", []))
                # nextPage already carries its query string.
                url = data.get("nextPage")
                params = None
        return out

    @staticmethod
    def to_alert_fields(company: dict[str, Any]) -> dict[str, Any]:
        return {
            "company": company.get("name"),
            "batch": normalize_batch(company.get("batch")),
            "program": Program.YC.value,
            "description": company.get("oneLiner"),
            "url": company.get("url"),
            "website": company.get("website"),
        }
