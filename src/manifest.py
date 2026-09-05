"""Pond Protocol v1.0 manifest.

Two details taken from the full protocol spec rather than the quickstart,
because the two pages disagree:

  * `limits` requires THREE fields - max_request_bytes, max_attachment_bytes
    and max_run_seconds. The quickstart example omits max_attachment_bytes,
    so copying it verbatim produces a manifest that fails schema validation
    at publish time.

  * max_run_seconds has no upper bound in the schema (minimum: 1). The
    quickstart shows 60, the full spec's own example shows 300. You choose the
    ceiling and must then honour it: Pond validates each request's deadline_ms
    against max_run_seconds * 1000.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = "1.0"


def build_manifest(config) -> dict[str, Any]:
    pond = config.get("pond", {}) or {}
    return {
        "protocol": "marketplace-agent",
        "protocol_version": PROTOCOL_VERSION,
        "agent_version": "1.0.0",
        "metadata": {
            "name": "YC Launch Monitor",
            "short_description": (
                "Alerts on new YC and SPEEDRUN companies, including founders "
                "who announce before the official directory lists them."
            ),
            "description": (
                "Monitors the YC company directory, the a16z SPEEDRUN "
                "portfolio, X and LinkedIn. Detects founders announcing an "
                "acceptance before it is officially listed, verifies each "
                "claim against YC's own API, and pushes a Slack alert. "
                "Maintains state so a lead is never alerted twice. Runs on a "
                "deterministic rules engine - no LLM required."
            ),
            "category": "research",
            "key_features": (
                "Early detection ahead of official announcement; verification "
                "against the official directory; per-source polling cadence; "
                "duplicate-proof state; Slack delivery."
            ),
            "use_cases": (
                "Sales and GTM teams who want to reach new-batch founders "
                "before the rest of the market sees the announcement."
            ),
            # Optional, and it only PREFILLS the publishing page - it cannot
            # change a price that is already published. It is here so the
            # manifest describes its own commercial terms rather than leaving
            # them to be retyped by hand on every resubmission.
            #
            # usage_unit must match what /runs and /tasks report in
            # `usage.unit_of_measurement`; the spec requires the reported unit
            # to align with the saved pricing plan, and we report "result".
            "pricing_plans": [
                {
                    "name": pond.get("plan_name", "Per result"),
                    "pricing_model": "pay_as_you_go",
                    # Minor units: 600 = $6.00.
                    "amount_minor": int(pond.get("amount_minor", 600)),
                    "usage_quantity": int(pond.get("usage_quantity", 2)),
                    "usage_unit": "result",
                    "description": pond.get(
                        "plan_description", "$6 per 2 results"),
                    "sort_order": 1,
                }
            ],
        },
        "actions": [
            {
                "id": "scan_now",
                "name": "Scan for new companies",
                "description": (
                    "Run a full monitoring pass across all sources and alert "
                    "on anything new. Use for any request to check, scan, or "
                    "look for new YC or SPEEDRUN companies."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Optional focus for this scan.",
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "id": "query_state",
                "name": "Ask about findings",
                "description": (
                    "Answer questions about what has already been found - "
                    "recent alerts, lead times, source health. Fast, no scan."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The question to answer.",
                            "minLength": 1,
                        }
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            },
        ],
        "capabilities": {
            "sync": True,
            "streaming": False,
            # A full scan will not finish inside a short deadline, so it is
            # accepted as a task and polled. Note these flags are only hints -
            # Pond accepts a valid 200 or 202 per request regardless.
            "async_tasks": True,
            "cancellation": False,
            "attachments": False,
            "feedback": False,
        },
        "input_modes": ["text/plain"],
        "output_modes": ["text/markdown"],
        "limits": {
            "max_request_bytes": int(pond.get("max_request_bytes", 1_048_576)),
            "max_attachment_bytes": int(pond.get("max_attachment_bytes", 1_048_576)),
            "max_run_seconds": int(pond.get("max_run_seconds", 300)),
        },
    }
