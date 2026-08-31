"""Rules-engine behaviour. Run: python -m tests.test_classify"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classify_rules import (  # noqa: E402
    classify,
    extract_batch,
    extract_company,
    is_candidate,
)
from src.models import ClaimType, Program  # noqa: E402
from src.classify_rules import extract_program  # noqa: E402

ACCEPTANCES = [
    "We got into YC F26! After a year building Acme AI we're moving to SF.",
    "Thrilled to share we're joining Y Combinator W27.",
    "We're in! YC F26. Huge thanks to everyone who helped.",
]

NOT_ACCEPTANCES = [
    "Our YC interview is next week, any advice?",
    "Just applied to YC W27, fingers crossed.",
    "We got rejected from YC this time. Here is what we learned.",
    "Back in 2019 when we did YC, things were different.",
    "I really enjoy Y Combinator's podcast episodes.",
]


def test_accepts_real_announcements():
    for text in ACCEPTANCES:
        claim, conf = classify(text)
        assert claim == ClaimType.ACCEPTED, f"missed: {text!r} -> {claim}"
        assert conf >= 0.6


def test_rejects_the_common_false_positives():
    for text in NOT_ACCEPTANCES:
        claim, _ = classify(text)
        assert claim != ClaimType.ACCEPTED, f"false positive: {text!r}"


def test_third_party_congratulation_is_not_first_party_news():
    claim, _ = classify("Congratulations to the team at Foo on getting into YC F26!")
    assert claim != ClaimType.ACCEPTED


def test_company_extraction_prefers_a_linked_domain():
    text = "We got into YC F26! Check us out at https://www.acme.ai and say hi."
    assert extract_company(text) == "acme.ai"


def test_company_extraction_ignores_social_hosts():
    text = "We got into YC F26! https://x.com/janedoe/status/1 building Acme AI now."
    assert extract_company(text) == "Acme AI"


def test_company_extraction_declines_when_unknowable():
    # A rules engine must decline rather than invent a name that ends up in a
    # cold email. This is exactly the case the model is worth paying for.
    assert extract_company("We're in!! YC F26 baby ") is None


def test_batch_and_program():
    assert extract_batch("We got into YC P26") == "P26"
    assert extract_batch("joining YC F26 this fall") == "F26"
    assert extract_program("joining a16z SPEEDRUN") == Program.SPEEDRUN
    assert extract_program("we got into YC W27") == Program.YC


def test_candidate_gate_filters_most_traffic():
    kept = [t for t in ACCEPTANCES + NOT_ACCEPTANCES if is_candidate(t)]
    assert len(kept) == len(ACCEPTANCES), kept


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
