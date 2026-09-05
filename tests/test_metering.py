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


def _wire(tmp_path, monkeypatch):
    """An agent with the paid actors recorded instead of called."""
    import src.sources.apify as apify
    from src.config import load_config
    from src.store import Store
    from src.slack import SlackNotifier
    from src.agent import MonitorAgent
    from src.scheduler import build_sources
    import src.sources.linkedin as lim

    calls = []

    def record(actor, *a, **k):
        calls.append(actor)
        return []

    monkeypatch.setattr(apify, "run_actor", record)
    monkeypatch.setattr(apify, "run_with_fallback", record)
    monkeypatch.setattr(lim, "run_with_fallback", record)

    cfg = load_config()
    store = Store(tmp_path / "t.db")
    sources = build_sources(cfg, store)
    agent = MonitorAgent(cfg, store, sources, SlackNotifier(None, "#test"))
    sources["x"]._fetch_timelines = lambda since: []   # isolate the paid tier
    monkeypatch.setattr(agent, "run_directory_pass", lambda: "")
    return agent, sources, calls


def test_a_free_cycle_cannot_eat_the_timer_a_requested_sweep_needs(
        tmp_path, monkeypatch):
    """The defect a real Pond run exposed.

    The background loop and the request handler drive the same Source objects.
    A free background cycle calls x.collect(), which stamps the source's poll
    timer - so when the request cycle reached X moments later, due() was False
    and the paid sweep was skipped entirely. The caller was billed for a result
    that had not looked at X at all.

    LinkedIn survived only by accident: an unpaid cycle skips it outright, so
    nothing stamped its timer.

    The fix makes the reset part of the cycle rather than a call before it, so
    nothing can land in between. This test models exactly that gap.
    """
    import time

    agent, sources, calls = _wire(tmp_path, monkeypatch)
    x = sources["x"]

    # State a background free cycle leaves behind, in the window where the old
    # code had already done its reset and had not yet reached X.
    x._last_run = time.time()
    x.allow_paid_calls(False)
    x._last_keyword_run = time.time()

    agent.run_cycle(include_metered=True, force_metered=True)

    assert any("search-x-by-keywords" in c for c in calls), \
        f"X keyword sweep was skipped: {calls}"
    assert any("linkedin" in c for c in calls), \
        f"LinkedIn sweep was skipped: {calls}"


def test_concurrent_cycles_still_deliver_the_requested_sweep(
        tmp_path, monkeypatch):
    """Smoke test for the cycle lock under real concurrency.

    A timing bug cannot be reproduced on demand, so this does not prove the
    race is gone - the lock does that by construction, since two cycles can no
    longer interleave. This guards against the lock being removed or a cycle
    path being added that does not take it.
    """
    import threading

    agent, sources, calls = _wire(tmp_path, monkeypatch)
    stop = threading.Event()

    def background():
        while not stop.is_set():
            agent.run_cycle(include_metered=False)

    t = threading.Thread(target=background, daemon=True)
    t.start()
    try:
        agent.run_cycle(include_metered=True, force_metered=True)
    finally:
        stop.set()
        t.join(timeout=10)

    assert any("search-x-by-keywords" in c for c in calls), \
        f"X keyword sweep lost under concurrency: {calls}"
    assert any("linkedin" in c for c in calls), \
        f"LinkedIn sweep lost under concurrency: {calls}"


def test_a_paid_cycle_records_health_for_the_sources_it_touched(
        tmp_path, monkeypatch):
    """/health must show LinkedIn after an on-request run.

    LinkedIn is only ever exercised by a request now. When the cycle did not
    record its health, /health listed no LinkedIn row at all - which reads as
    a missing source, the same misreading that got the listing rejected.
    """
    agent, sources, _ = _wire(tmp_path, monkeypatch)
    agent.run_cycle(include_metered=True, force_metered=True)
    recorded = {r["source"] for r in agent.store.health()}
    assert "linkedin" in recorded, f"LinkedIn health not recorded: {recorded}"
    assert "x" in recorded, f"X health not recorded: {recorded}"


def test_an_unpaid_cycle_leaves_linkedin_health_untouched(tmp_path, monkeypatch):
    """A free pass must not overwrite what LinkedIn's last real run reported."""
    agent, sources, _ = _wire(tmp_path, monkeypatch)
    agent.store.set_health("linkedin", False, "the error from the last real run")
    agent.run_cycle(include_metered=False)
    row = {r["source"]: r for r in agent.store.health()}["linkedin"]
    assert row["last_error"] == "the error from the last real run"


def test_a_scan_that_finds_nothing_bills_nothing(tmp_path, monkeypatch):
    """Pond bills on the usage a run reports.

    The flat `quantity: 1` charged the caller for a scan that delivered no
    leads, and paid the same for one that delivered several. The quantity has
    to be what the caller actually received.
    """
    agent, _, _ = _wire(tmp_path, monkeypatch)
    outcome = agent.run_cycle(include_metered=True, force_metered=True)
    assert outcome.results == 0


def test_only_leads_this_cycle_delivered_are_billed(tmp_path, monkeypatch):
    """Leads from an earlier cycle must not be re-billed to a later caller."""
    agent, _, _ = _wire(tmp_path, monkeypatch)
    agent.store.claim_alert_slot("acme|a", "Acme", "yc", "F26", "x", "early",
                                 founder="f", post_url="u")
    outcome = agent.run_cycle(include_metered=True, force_metered=True)
    assert outcome.results == 0


def test_the_sweep_is_sized_by_the_leads_asked_for(tmp_path, monkeypatch):
    """A request for fewer leads must buy fewer items.

    The point of the on-request model is that spend follows demand. A fixed
    sweep would charge the same whether the caller wanted one lead or twenty.
    """
    import src.sources.apify as apify
    from src.config import load_config
    from src.store import Store
    from src.slack import SlackNotifier
    from src.agent import MonitorAgent
    from src.scheduler import build_sources
    import src.sources.linkedin as lim

    def sizes_for(ask):
        seen = {}

        def rec(actor, token, payload, **k):
            if "search-x" in actor:
                seen["x"] = payload.get("maxItemsPerKeyword")
            return []

        def rec_fb(primary, fallback, token, payload, **k):
            seen["linkedin"] = payload.get("limit")
            return []

        monkeypatch.setattr(apify, "run_actor", rec)
        monkeypatch.setattr(apify, "run_with_fallback", rec_fb)
        monkeypatch.setattr(lim, "run_with_fallback", rec_fb)

        cfg = load_config()
        store = Store(tmp_path / f"s{ask}.db")
        sources = build_sources(cfg, store)
        agent = MonitorAgent(cfg, store, sources, SlackNotifier(None, "#t"))
        sources["x"]._fetch_timelines = lambda since: []
        monkeypatch.setattr(agent, "run_directory_pass", lambda: "")
        agent.run_cycle(include_metered=True, force_metered=True,
                        max_results=ask)
        return seen

    small, big = sizes_for(1), sizes_for(2)
    assert small["x"] < big["x"], f"X not scaled: {small} vs {big}"
    assert small["linkedin"] < big["linkedin"], \
        f"LinkedIn not scaled: {small} vs {big}"


def test_a_huge_ask_is_still_capped_by_config(tmp_path, monkeypatch):
    """The per-source ceiling, not the caller, bounds what a run can spend."""
    from src.config import load_config

    cfg = load_config()
    agent, _, _ = _wire(tmp_path, monkeypatch)
    budget = agent._item_budget("x", 10_000)
    assert budget == int(cfg.source("x")["max_items_per_term"])
