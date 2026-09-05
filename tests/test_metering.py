"""The on-request metering gate.

The agent is listed on a marketplace, so it is idle most of the time. With
`metered_on_request_only` on, the background loop must never touch a paid
Apify actor - the credit is reserved for runs a caller actually asked for.
These tests fail if that gate is ever removed, which would silently drain the
Apify balance on a timer.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sources.linkedin import LinkedInSource  # noqa: E402
from src.sources.x_source import XSource  # noqa: E402


def _x():
    return XSource(
        {"keywords": ["we got into YC"], "keyword_interval_seconds": 0},
        apify_token="token",
        keyword_tier="apify",
    )


def test_x_keeps_its_free_tiers_but_drops_the_paid_one():
    x = _x()
    assert x.metered and x.has_free_tier

    x.allow_paid_calls(True)
    assert x._keyword_due() is True

    x.allow_paid_calls(False)
    # Tier 3 is the only paid part; tiers 1-2 still run, so X is not skipped.
    assert x._keyword_due() is False


def test_linkedin_has_no_free_tier_so_it_is_skipped_whole():
    li = LinkedInSource({"enabled": True}, apify_token="token")
    assert li.metered and not li.has_free_tier


def test_free_x_signals_are_not_billed_as_paid_ones():
    x = _x()
    # 40 signals from the free timeline tiers, no keyword sweep run.
    x.last_paid_item_count = 0
    assert x.estimated_cost(40) == 0.0
    # Only tier-3 items carry the per-item price.
    x.last_paid_item_count = 10
    assert x.estimated_cost(40) == 0.10


def test_unpaid_pass_makes_no_apify_call(monkeypatch):
    import src.sources.x_source as xm

    def boom(*a, **k):
        raise AssertionError("a paid actor was called on an unpaid pass")

    monkeypatch.setattr(xm, "run_actor", boom, raising=False)
    x = _x()
    x.allow_paid_calls(False)
    x.watchlist = []          # no handles, so tiers 1-2 do no network either
    x._discovered = set()
    assert x.fetch(datetime.now(timezone.utc)) == []
