"""Shared data shapes.

RawSignal is the one currency every source deals in. Adding a platform means
producing RawSignals from it; nothing downstream needs to know where they came
from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# YC runs four cycles a year. The codes are not the ones people assume:
# Winter, Summer, sPring, Fall -> W / S / P / F. Spring 2026 is "P26", verified
# against api.ycombinator.com. Matching only [WS] silently misses half the year.
BATCH_RE = re.compile(r"\b([WSPF])\s?(\d{2})\b", re.IGNORECASE)

# Speedrun is an a16z program, not a YC one (see README "A note on Speedrun").
SPEEDRUN_RE = re.compile(r"\bspeed\s?run\b", re.IGNORECASE)

YC_RE = re.compile(r"\b(y[\s-]?combinator|yc)\b", re.IGNORECASE)


class Program(str, Enum):
    YC = "yc"
    SPEEDRUN = "speedrun"


class ClaimType(str, Enum):
    """What a post is actually saying.

    Only ACCEPTED is alertable. INTERVIEWING and APPLIED are the two biggest
    false-positive sources - a naive keyword match treats "we have our YC
    interview next week" as an acceptance.
    """

    ACCEPTED = "accepted"
    INTERVIEWING = "interviewing"
    APPLIED = "applied"
    ALUMNI = "alumni"
    OTHER = "other"


class AlertStatus(str, Enum):
    EARLY = "early"          # founder announced, absent from the directory
    CONFIRMED = "confirmed"  # present in the directory
    SUPPRESSED = "suppressed"
    LOGGED = "logged"        # below the confidence threshold


@dataclass
class RawSignal:
    source: str
    external_id: str
    text: str
    url: str
    created_at: datetime
    author: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def looks_relevant(self) -> bool:
        """Cheap prefilter. Runs before any model call.

        Cuts the overwhelming majority of collected posts for free. A signal
        needs a program mention *and* a batch code, or an explicit Speedrun
        mention - "excited about Y Combinator" on its own is not news.
        """
        has_batch = bool(BATCH_RE.search(self.text))
        return (YC_RE.search(self.text) is not None and has_batch) or (
            SPEEDRUN_RE.search(self.text) is not None
        )


@dataclass
class Claim:
    """A structured reading of one signal."""

    company_name: str
    claim_type: ClaimType
    program: Program
    confidence: float
    batch: str | None = None
    founder_name: str | None = None
    founder_handle: str | None = None
    website: str | None = None
    quoted_evidence: str | None = None

    @property
    def key(self) -> str:
        """Normalized identity used for dedupe across sources.

        Two cofounders posting the same news must collapse to one alert, so the
        key deliberately ignores author and source.
        """
        return normalize_company(self.company_name)


def normalize_company(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"\b(inc|llc|ltd|corp|co|labs?|ai|hq)\b\.?", "", name)
    name = re.sub(r"[^a-z0-9]+", "", name)
    return name


def normalize_batch(batch: str | None) -> str | None:
    """Turn any batch spelling into the canonical short code.

    The two YC data sources disagree: the official API returns "S26" while the
    community mirror returns "Summer 2026". Both must land on "S26".
    """
    if not batch:
        return None
    batch = batch.strip()
    words = {"winter": "W", "summer": "S", "spring": "P", "fall": "F", "autumn": "F"}
    for word, letter in words.items():
        m = re.match(rf"{word}\s+(\d{{4}})", batch, re.IGNORECASE)
        if m:
            return f"{letter}{m.group(1)[-2:]}"
    m = BATCH_RE.search(batch)
    if m:
        return f"{m.group(1).upper()}{m.group(2)}"
    return batch.upper()
