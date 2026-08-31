"""Thin Apify client.

Actor IDs are configuration, never hard-coded call sites. Actors break and get
deprecated; when one does, the fix should be one line in config.yml rather than
a code change and redeploy.

Verified against Apify's live store API:
    apidojo/tweet-scraper                                 exists, ~159M runs
    apimaestro/linkedin-posts-search-scraper-no-cookies   exists, $0.005/item
    harvestapi/linkedin-post-search                       exists (fallback)
    apify/google-search-scraper                           exists, ~114M runs
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.apify.com/v2"


class ApifyError(RuntimeError):
    pass


def run_actor(
    actor: str,
    token: str | None,
    payload: dict[str, Any],
    timeout: int = 180,
) -> list[dict[str, Any]]:
    """Run an actor synchronously and return its dataset items.

    Uses run-sync-get-dataset-items so one call covers start, wait, and fetch.
    """
    if not token:
        raise ApifyError("APIFY_TOKEN is not set")

    slug = actor.replace("/", "~")
    url = f"{BASE}/acts/{slug}/run-sync-get-dataset-items"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, params={"token": token}, json=payload)
        if resp.status_code >= 400:
            raise ApifyError(f"{actor} returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
    if not isinstance(data, list):
        raise ApifyError(f"{actor} returned {type(data).__name__}, expected a list")
    return data


def run_with_fallback(
    primary: str,
    fallback: str | None,
    token: str | None,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Try the configured actor, fall back to the alternate if it breaks.

    Scraper actors are the least durable part of this system. Having a second
    one already wired means an outage degrades throughput instead of stopping
    the source entirely.
    """
    try:
        return run_actor(primary, token, payload)
    except Exception as exc:  # noqa: BLE001
        if not fallback:
            raise
        log.warning("actor %s failed (%s); trying fallback %s", primary, exc, fallback)
        return run_actor(fallback, token, payload)
