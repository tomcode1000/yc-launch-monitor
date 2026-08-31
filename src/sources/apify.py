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
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.apify.com/v2"


class ApifyError(RuntimeError):
    pass


TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def run_actor(
    actor: str,
    token: str | None,
    payload: dict[str, Any],
    timeout: int = 180,
    poll_seconds: float = 3.0,
) -> list[dict[str, Any]]:
    """Start an actor, poll until it finishes, then fetch its dataset items.

    Deliberately NOT run-sync-get-dataset-items. That endpoint holds a single
    connection open for the whole run, which TLS-inspecting antivirus and many
    corporate proxies reset partway through (WinError 10054). Three short
    requests survive environments where one long one does not.
    """
    if not token:
        raise ApifyError("APIFY_TOKEN is not set")

    slug = actor.replace("/", "~")
    # Bearer header, never ?token= - httpx and most proxies log full URLs, and
    # a token in the query string ends up in every log line and access record.
    auth = {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {}
    deadline = time.monotonic() + timeout

    with httpx.Client(timeout=30, headers=auth) as client:
        started = client.post(f"{BASE}/acts/{slug}/runs", json=payload)
        if started.status_code >= 400:
            raise ApifyError(
                f"{actor} returned {started.status_code}: {started.text[:200]}")
        run = started.json()["data"]
        run_id, dataset_id = run["id"], run.get("defaultDatasetId")

        status = run.get("status")
        while status not in TERMINAL:
            if time.monotonic() > deadline:
                client.post(f"{BASE}/actor-runs/{run_id}/abort")
                raise ApifyError(f"{actor} still {status} after {timeout}s; aborted")
            time.sleep(poll_seconds)
            polled = client.get(f"{BASE}/actor-runs/{run_id}")
            if polled.status_code >= 400:
                raise ApifyError(
                    f"{actor} status check returned {polled.status_code}")
            run = polled.json()["data"]
            status = run.get("status")
            dataset_id = run.get("defaultDatasetId", dataset_id)

        if status != "SUCCEEDED":
            raise ApifyError(f"{actor} finished as {status}")
        if not dataset_id:
            raise ApifyError(f"{actor} succeeded but exposed no dataset")

        items = client.get(
            f"{BASE}/datasets/{dataset_id}/items",
            params={"clean": "true"},
        )
        if items.status_code >= 400:
            raise ApifyError(f"{actor} dataset fetch returned {items.status_code}")
        data = items.json()

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
