"""Core guarantees. Run with: python -m tests.test_core (no pytest needed)."""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import (  # noqa: E402
    RawSignal,
    lead_key,
    normalize_batch,
    normalize_company,
)
from src.store import Store  # noqa: E402


def test_batch_codes():
    # YC runs four cycles: Winter, Summer, sPring, Fall.
    assert normalize_batch("Summer 2026") == "S26"
    assert normalize_batch("Spring 2026") == "P26"
    assert normalize_batch("Fall 2026") == "F26"
    assert normalize_batch("Winter 2027") == "W27"
    assert normalize_batch("S26") == "S26"


def test_company_identity():
    # Two cofounders naming the company differently must collapse to one key.
    assert normalize_company("Acme AI, Inc.") == normalize_company("Acme")
    assert normalize_company("Example Labs") == normalize_company("example")
    assert normalize_company("Acme") != normalize_company("Beta")


def test_prefilter():
    now = datetime.now(timezone.utc)
    hit = RawSignal("x", "1", "We got into YC F26! Moving to SF.", "u", now)
    miss = RawSignal("x", "2", "I really enjoy Y Combinator podcasts", "u", now)
    speedrun = RawSignal("x", "3", "Joining a16z SPEEDRUN this cycle", "u", now)
    assert hit.looks_relevant()
    assert not miss.looks_relevant()
    assert speedrun.looks_relevant()


def test_a_lead_survives_without_a_company_name():
    # The client acts by messaging the founder through the post link, so a
    # post that names no company is still a complete lead.
    assert lead_key(None, "@janedoe") == "handle:janedoe"
    assert lead_key("Acme AI", "@jane") == normalize_company("Acme")
    assert lead_key(None, None) is None


def test_cofounders_collapse_but_strangers_do_not():
    # Same company, different people -> one lead.
    assert lead_key("Acme AI", "@jane") == lead_key("Acme", "@bob")
    # No company name, different people -> two leads, correctly.
    assert lead_key(None, "@jane") != lead_key(None, "@bob")
    # No company name, same person posting twice -> one lead.
    assert lead_key(None, "@jane") == lead_key(None, "@Jane")


def test_dedupe_is_structural():
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    assert store.claim_alert_slot("acme", "Acme AI", "yc", "F26", "x", "early")
    # The second claim must fail. This is what stops the agent double-posting.
    assert not store.claim_alert_slot("acme", "Acme AI", "yc", "F26", "x", "early")
    assert store.has_alerted("acme")


def test_lead_time_and_spend():
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    store.claim_alert_slot("acme", "Acme", "yc", "F26", "x", "early")
    store.mark_confirmed("acme")
    assert len(store.lead_times_hours()) == 1

    store.add_spend("linkedin", 0.25)
    store.add_spend("linkedin", 0.25)
    assert abs(store.spend_today() - 0.50) < 1e-9


def test_pond_idempotency():
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    store.put_run("run_1", "completed", {"ok": True}, task_id="task_1")
    assert store.get_run("run_1")["status"] == "completed"
    assert store.get_task("task_1")["run_id"] == "run_1"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print("ok" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
