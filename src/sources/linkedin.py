"""LinkedIn adapter.

Required by the brief, and the least durable source here. Two things the
client should know, and which the README states plainly:

  1. LinkedIn forbids automated collection in its terms and actively blocks it.
     This uses a maintained third-party actor, which is pragmatic rather than
     durable. Both configured actors are "no cookies" builds - the alternatives
     want a session cookie from a real account, and that is the configuration
     most likely to get the client's own profile restricted.

  2. It is metered. At $0.005 per item a 50-post search costs about $0.25 a run.
     At the configured 6-hour interval that is roughly $22/month. Running it
     every minute would be about $360/day, which is why cadence is per source
     rather than global.

If the actor breaks, change the ID in config.yml. No code change needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import RawSignal
from .apify import run_with_fallback
from .base import Source

log = logging.getLogger(__name__)

USD_PER_ITEM = 0.005


class LinkedInSource(Source):
    name = "linkedin"
    metered = True

    def __init__(self, config: dict, apify_token: str | None = None, **kw):
        super().__init__(config, **kw)
        self.apify_token = apify_token

    @property
    def enabled(self) -> bool:
        # Silently inert without a token rather than failing every cycle.
        return bool(super().enabled and self.apify_token)

    def fetch(self, since: datetime) -> list[RawSignal]:
        if not self.apify_token:
            return []

        max_items = int(self.config.get("max_items_per_run", 50))
        items = run_with_fallback(
            self.config.get(
                "apify_actor",
                "apimaestro/linkedin-posts-search-scraper-no-cookies",
            ),
            self.config.get("apify_actor_fallback"),
            self.apify_token,
            {
                "keywords": self.config.get("keywords") or ["Y Combinator"],
                "maxItems": max_items,
                "sortBy": "date",
            },
        )

        out: list[RawSignal] = []
        for item in items:
            pid = str(item.get("urn") or item.get("id") or item.get("postUrl") or "")
            if not pid:
                continue
            author = item.get("authorName") or (item.get("author") or {}).get("name")
            out.append(
                RawSignal(
                    source=self.name,
                    external_id=pid,
                    text=item.get("text") or item.get("content") or "",
                    url=item.get("postUrl") or item.get("url") or "",
                    created_at=_parse(item.get("postedAt") or item.get("date")),
                    author=author,
                    raw={"via": "apify"},
                )
            )
        return out

    def estimated_cost(self, item_count: int) -> float:
        return item_count * USD_PER_ITEM


def _parse(value) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)
