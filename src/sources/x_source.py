"""X (Twitter) adapter - three tiers, merged.

Free access to X is lopsided: profile timelines are wide open, keyword search
is not. Rather than depend on one brittle call, this adapter runs three
strategies in descending order of reliability and merges what they return.

  Tier 1  Watchlist timelines via syndication.twitter.com. No key, no account,
          returns ~20 recent posts per profile as structured JSON. YC partners
          repost and congratulate incoming founders constantly, which leaks
          names before the directory updates.
  Tier 2  Reverse discovery - founders of companies we learn about get added
          to the watchlist, so coverage compounds on its own.
  Tier 3  Keyword search across all of X. The highest-yield channel and the
          only unreliable one. Off unless X_KEYWORD_TIER=apify.

Tier 3 failing does not stop tiers 1 and 2; the run reports a degraded source.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..models import RawSignal
from .base import Source

log = logging.getLogger(__name__)

SYNDICATION = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Measured against the live endpoint: it 429s by IP after a handful of rapid
# requests. These three constants are the whole rate-limit strategy.
HANDLES_PER_TICK = 2
REQUEST_SPACING = 2.0     # seconds between handles within one tick
COOLDOWN_SECONDS = 900    # global pause after a 429


class RateLimited(Exception):
    """Raised on 429. Signals a global backoff, not a per-handle failure."""


class XSource(Source):
    name = "x"
    # Tiers 1 and 2 are free timeline polling, so X is still worth running
    # when paid calls are switched off - only tier 3 goes quiet.
    has_free_tier = True

    def __init__(self, config: dict, apify_token: str | None = None,
                 keyword_tier: str = "off", **kw):
        super().__init__(config, **kw)
        self.apify_token = apify_token
        self.keyword_tier = keyword_tier
        self.watchlist: list[str] = list(config.get("watchlist") or [])
        self._discovered: set[str] = set()
        self._last_keyword_run = 0.0
        self._cursor = 0
        self._cooldown_until = 0.0
        # Only tier 3 costs money, so the governor must be charged for tier 3
        # items alone. Billing every tweet would price the free timeline tiers
        # as if they were paid and trip the daily cap on nothing.
        self.last_paid_item_count = 0

    @property
    def metered(self) -> bool:  # type: ignore[override]
        return self.keyword_tier == "apify"

    def add_handles(self, handles: list[str]) -> None:
        """Tier 2. Founders learned from the directory join the watchlist."""
        for h in handles:
            h = h.lstrip("@").strip()
            if h and h not in self.watchlist:
                self._discovered.add(h)

    def fetch(self, since: datetime) -> list[RawSignal]:
        self.last_paid_item_count = 0
        signals = self._fetch_timelines(since)
        if self._keyword_due():
            try:
                signals.extend(self._fetch_keywords(since))
                self._last_keyword_run = time.time()
            except Exception as exc:  # noqa: BLE001 - tier 3 fails soft
                log.warning("X keyword tier degraded, continuing on tiers 1-2: %s", exc)
        return signals

    # --- tier 1 + 2 ----------------------------------------------------
    def _fetch_timelines(self, since: datetime) -> list[RawSignal]:
        """Poll a slice of the watchlist, not all of it.

        The syndication endpoint rate-limits by IP and returns 429 after only a
        handful of rapid requests - measured, not assumed. So each tick walks a
        small window of the watchlist round-robin and every handle still gets
        covered, just spread over several ticks. With 6 handles, HANDLES_PER_TICK=2
        and a 180s interval, each account is checked about every 9 minutes and
        the endpoint sees roughly one request per 90 seconds.
        """
        if time.time() < self._cooldown_until:
            log.info("X syndication cooling down for %.0fs more",
                     self._cooldown_until - time.time())
            return []

        handles = self.watchlist + sorted(self._discovered)
        if not handles:
            return []

        window = handles[self._cursor:self._cursor + HANDLES_PER_TICK]
        if len(window) < HANDLES_PER_TICK:
            window += handles[: HANDLES_PER_TICK - len(window)]
        self._cursor = (self._cursor + HANDLES_PER_TICK) % len(handles)

        out: list[RawSignal] = []
        with httpx.Client(timeout=25, headers={"User-Agent": UA},
                          follow_redirects=True) as client:
            for i, handle in enumerate(window):
                if i:
                    # Jittered spacing so the requests never arrive as a burst.
                    time.sleep(REQUEST_SPACING + random.uniform(0, 1.5))
                try:
                    out.extend(self._timeline(client, handle, since))
                except RateLimited:
                    # Back off globally: a 429 is about our IP, not this handle.
                    self._cooldown_until = time.time() + COOLDOWN_SECONDS
                    log.warning("X syndication returned 429; backing off %ss",
                                COOLDOWN_SECONDS)
                    break
                except Exception as exc:  # noqa: BLE001 - one bad handle is not fatal
                    log.debug("timeline %s failed: %s", handle, exc)
        return out

    def _timeline(self, client: httpx.Client, handle: str,
                  since: datetime) -> list[RawSignal]:
        resp = client.get(SYNDICATION.format(handle=handle))
        if resp.status_code == 429:
            raise RateLimited(handle)
        resp.raise_for_status()
        match = NEXT_DATA_RE.search(resp.text)
        if not match:
            raise ValueError("no __NEXT_DATA__ payload")
        data = json.loads(match.group(1))
        entries = data["props"]["pageProps"]["timeline"]["entries"]

        out: list[RawSignal] = []
        for entry in entries:
            tweet = (entry.get("content") or {}).get("tweet")
            if not tweet:
                continue
            created = _parse_time(tweet.get("created_at"))
            if created and created < since:
                continue
            user = (tweet.get("user") or {}).get("screen_name") or handle
            out.append(
                RawSignal(
                    source=self.name,
                    external_id=tweet["id_str"],
                    text=tweet.get("full_text") or tweet.get("text") or "",
                    url=tweet.get("permalink")
                    and f"https://x.com{tweet['permalink']}"
                    or f"https://x.com/{user}/status/{tweet['id_str']}",
                    created_at=created or datetime.now(timezone.utc),
                    author=f"@{user}",
                    raw={"via": "syndication", "handle": handle},
                )
            )
        return out

    # --- tier 3 --------------------------------------------------------
    def _keyword_due(self) -> bool:
        if self.keyword_tier != "apify" or not self.apify_token:
            return False
        # An unpaid pass keeps tiers 1-2 and stops here.
        if not self.paid_calls_allowed:
            return False
        interval = int(self.config.get("keyword_interval_seconds", 1800))
        return (time.time() - self._last_keyword_run) >= interval

    def _fetch_keywords(self, since: datetime) -> list[RawSignal]:
        from .apify import run_actor

        actor = self.config.get("apify_actor")
        terms = self.config.get("keywords") or []

        # This actor takes every keyword in one run and applies the limit per
        # keyword, so a busy phrase cannot crowd out a quiet one and there is
        # no need to fan out into one call per term.
        items = run_actor(
            actor,
            self.apify_token,
            {
                "searchType": "tweets",
                "keywords": terms,
                "maxItemsPerKeyword": self.item_budget or int(
                    self.config.get("max_items_per_term", 50)),
                "sortBy": "latest",
            },
        )
        items = [i for i in items
                 if isinstance(i, dict) and not i.get("noResults")]
        log.info("keyword sweep: %d terms -> %d tweets", len(terms), len(items))
        # Apify bills on what the actor returned, not on what survives filtering.
        self.last_paid_item_count = len(items)

        out: list[RawSignal] = []
        for item in items:
            tid = str(item.get("id") or item.get("id_str") or "")
            if not tid:
                continue
            created = _parse_time(item.get("createdAt") or item.get("created_at"))
            # Actors disagree on this field: some nest it as
            # author.userName, this one returns a plain "@handle" string.
            raw_author = item.get("author")
            if isinstance(raw_author, dict):
                author = raw_author.get("userName") or raw_author.get("screen_name")
            else:
                author = raw_author or item.get("username")
            author = (author or "").lstrip("@") or None
            out.append(
                RawSignal(
                    source=self.name,
                    external_id=tid,
                    text=item.get("text") or item.get("full_text") or "",
                    url=item.get("url") or f"https://x.com/i/status/{tid}",
                    created_at=created or datetime.now(timezone.utc),
                    author=f"@{author}" if author else None,
                    raw={"via": "apify"},
                )
            )
        return out

    def estimated_cost(self, item_count: int) -> float:
        # apidojo/tweet-scraper bills per dataset item; treat as a rough cent
        # per item so the governor errs toward stopping early.
        # item_count is every signal the pass produced, most of them free.
        # Charge for the tier-3 items only.
        if self.keyword_tier != "apify":
            return 0.0
        return self.last_paid_item_count * 0.01


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # X returns RFC 2822: "Wed Aug 26 18:29:06 +0000 2026"
        return parsedate_to_datetime(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
