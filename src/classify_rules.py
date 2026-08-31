"""Deterministic classification.

This module does two jobs:

  1. It is a free prefilter. Most posts mentioning YC are not acceptances, and
     the cheap negative patterns below reject them without spending a token.
     Only what survives is worth a model call.

  2. It is the fallback. With LLM_MODE=off the whole pipeline runs on these
     rules alone - no Anthropic key, no API cost.

Rules are good at rejecting and weak at extracting. Deciding that "our YC
interview is next week" is not an acceptance is pattern matching. Pulling
"Acme AI" out of "after a year building Acme AI we're moving to SF" is not,
which is exactly the boundary where the model earns its cost.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .models import BATCH_RE, SPEEDRUN_RE, ClaimType, Program

# Phrases that make a post NOT an acceptance. Checked first: rejecting is
# cheap and these are the dominant false positives.
NEGATIVE = [
    (ClaimType.INTERVIEWING, re.compile(
        r"\b(interview(ing|s)?\s+(with|at|next|this|tomorrow|coming)"
        r"|got\s+an?\s+interview|interview\s+invit|prepping\s+for)", re.I)),
    (ClaimType.APPLIED, re.compile(
        r"\b(appli(ed|cation|cations|ying)|submitted\s+(our|my|the)\s+app"
        r"|deadline\s+to\s+apply|apply\s+to)", re.I)),
    (ClaimType.OTHER, re.compile(
        r"\b(reject(ed|ion)|didn'?t\s+get\s+in|did\s+not\s+get\s+in|turned\s+down"
        r"|got\s+a\s+no|waitlist(ed)?|not\s+accepted)", re.I)),
    (ClaimType.ALUMNI, re.compile(
        r"\b(alum(ni|nus)?|back\s+in\s+20\d\d|years?\s+ago|our\s+batch\s+was"
        r"|when\s+we\s+(did|went\s+through))", re.I)),
]

# Phrases that indicate a genuine acceptance by the poster.
POSITIVE = re.compile(
    r"\b(got\s+into|accepted\s+(in)?to|we'?re\s+in\b|joining\s+(yc|y\s+combinator|speedrun)"
    r"|part\s+of\s+(the\s+)?(yc|y\s+combinator|speedrun)|backed\s+by\s+y\s+combinator"
    r"|thrilled\s+to\s+(share|announce)|excited\s+to\s+(share|announce)"
    r"|proud\s+to\s+announce)", re.I)

# First person - distinguishes the founder's own news from congratulating
# someone else's. "Congrats to @x on YC" is a lead, but not this company.
FIRST_PERSON = re.compile(r"\b(we|we'?re|we'?ve|our|i'?m|i've|my)\b", re.I)
THIRD_PARTY = re.compile(r"\b(congrat|congratulations|shoutout|proud\s+of|welcome\s+to)\b", re.I)

IGNORED_HOSTS = {
    "x.com", "twitter.com", "t.co", "linkedin.com", "www.linkedin.com",
    "lnkd.in", "ycombinator.com", "www.ycombinator.com", "news.ycombinator.com",
    "github.com", "youtube.com", "youtu.be", "medium.com",
}

URL_RE = re.compile(r"https?://[^\s\)\]]+")

# "building Acme AI", "at Acme AI," - a capitalised run after a cue word.
NAME_AFTER_CUE = re.compile(
    r"\b(?:building|launched|founded|started|co-?founded|behind|at)\s+"
    r"([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})")


def classify(text: str, author_bio: str | None = None) -> tuple[ClaimType, float]:
    """Return (claim_type, confidence) from patterns alone."""
    for claim, pattern in NEGATIVE:
        if pattern.search(text):
            return claim, 0.85

    if not POSITIVE.search(text):
        return ClaimType.OTHER, 0.5

    if THIRD_PARTY.search(text) and not FIRST_PERSON.search(text):
        # Someone congratulating a company - real news, wrong author. Worth
        # logging, not worth alerting as a first-party announcement.
        return ClaimType.OTHER, 0.4

    has_batch = bool(BATCH_RE.search(text))
    has_program = bool(SPEEDRUN_RE.search(text)) or re.search(
        r"\b(yc|y[\s-]?combinator)\b", text, re.I)

    if has_program and has_batch:
        return ClaimType.ACCEPTED, 0.8
    if has_program:
        return ClaimType.ACCEPTED, 0.6
    return ClaimType.OTHER, 0.4


def extract_batch(text: str) -> str | None:
    m = BATCH_RE.search(text)
    return f"{m.group(1).upper()}{m.group(2)}" if m else None


def extract_program(text: str) -> Program:
    return Program.SPEEDRUN if SPEEDRUN_RE.search(text) else Program.YC


def extract_company(text: str, author_bio: str | None = None) -> str | None:
    """Best-effort company name, in descending order of reliability.

    1. A linked domain that is not a social host. Founders overwhelmingly link
       their own site in an announcement, and a domain is unambiguous.
    2. A capitalised run after a cue word like "building" or "at".
    3. The same cue search over the author's bio.

    Returns None when none of these fire, which is the honest answer - a rules
    engine should decline rather than guess a company name that will end up in
    a cold email.
    """
    for raw_url in URL_RE.findall(text):
        host = (urlparse(raw_url).hostname or "").lower()
        if host and host not in IGNORED_HOSTS:
            return host[4:] if host.startswith("www.") else host

    m = NAME_AFTER_CUE.search(text)
    if m:
        candidate = m.group(1).strip()
        if candidate.lower() not in {"y combinator", "yc", "speedrun", "sf"}:
            return candidate

    if author_bio:
        m = NAME_AFTER_CUE.search(author_bio)
        if m:
            return m.group(1).strip()

    return None


def is_candidate(text: str) -> bool:
    """Cheap gate deciding whether a signal is worth a model call at all."""
    claim, _ = classify(text)
    return claim == ClaimType.ACCEPTED
