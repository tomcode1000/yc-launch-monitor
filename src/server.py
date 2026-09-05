"""Pond Protocol server + the autonomous scheduler, in one process.

One agent, two triggers:

  The scheduler   fires monitoring cycles on each source's own interval and
                  pushes Slack alerts with nobody asking. This is the product.
  POST /runs      lets Pond drive the same agent on demand against the same
                  state. This is what Pond reviews and health-monitors.

Running both here is also the strictest reading of "the bot runs continuously"
in the task's eligibility list - and Pond needs a public URL to call anyway, so
one always-on service satisfies both requirements at once.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import agent as agent_module
from .agent import MonitorAgent
from .config import load_config
from .manifest import PROTOCOL_VERSION, build_manifest
from .scheduler import MonitorScheduler, build_sources
from . import status_page
from .slack import SlackNotifier
from .store import Store

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yc-monitor")

REPO_URL = "https://github.com/tomcode1000/yc-launch-monitor"

# Upper bound on a single request's ask. The per-source ceilings in config.yml
# still bound what any one run can buy; this stops a caller naming a number so
# large the request is obviously not a real ask.
MAX_RESULTS_CEILING = 25

config = load_config()
store = Store(config.db_path)
notifier = SlackNotifier(config.slack_token, config.slack_channel)
sources = build_sources(config, store)
agent = MonitorAgent(config, store, sources, notifier)
scheduler = MonitorScheduler(config, sources, agent, store, notifier)

app = FastAPI(title="YC Launch Monitor")


@app.on_event("startup")
def _start() -> None:
    scheduler.start()
    log.info("monitoring %d sources; slack configured: %s",
             len(sources), notifier.configured)


# ---------------------------------------------------------------------------
# Pond Protocol
# ---------------------------------------------------------------------------

def fail(status_code: int, code: str, message: str):
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def authenticate_pond(
    authorization: str | None = Header(default=None),
    pond_version: str | None = Header(default=None, alias="X-Agent-Protocol-Version"),
) -> None:
    """Access Key on runtime calls only. /manifest stays public."""
    access_key = config.pond_access_key
    if not access_key:
        fail(500, "internal_error", "POND_ACCESS_KEY is not configured.")
    if authorization != f"Bearer {access_key}":
        fail(401, "unauthorized", "The Access Key is missing or invalid.")
    if pond_version is None or not _valid_version(pond_version):
        fail(400, "invalid_request", "The protocol version must be Major.Minor.")
    if pond_version != PROTOCOL_VERSION:
        fail(400, "unsupported_protocol_version",
             f"Protocol version {pond_version} is not supported.")


def _valid_version(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 2 and all(p.isdigit() for p in parts)


class RunRequest(BaseModel):
    run_id: str
    agent_id: str | None = None
    conversation_id: str | None = None
    history_truncated: bool = False
    action_id: str | None = None
    user: dict = {}
    messages: list[dict] = []
    parameters: dict = {}
    execution: dict = {}


@app.get("/manifest")
def manifest() -> dict[str, Any]:
    return build_manifest(config)


@app.post("/runs", dependencies=[Depends(authenticate_pond)])
def create_run(
    run: RunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_key != run.run_id:
        fail(400, "invalid_request", "Idempotency-Key must match run_id.")

    # Pond requires persisted idempotency: a retried run_id returns the original
    # response instead of running the scan again.
    existing = store.get_run(run.run_id)
    if existing and existing.get("response"):
        import json

        return json.loads(existing["response"])

    if run.action_id not in (None, "scan_now", "query_state"):
        fail(400, "unsupported_operation", "The action is not supported.")

    prompt = (run.parameters or {}).get("prompt") or ""

    # How many leads the caller wants. This is the billing unit, and it also
    # sizes the sweep - a run that is asked for two leads should not buy the
    # volume of posts a run asked for twenty would. Defaults to the pricing
    # plan's own quantity so an unspecified request costs what the listing says.
    default_results = int(
        (config.get("pond", {}) or {}).get("usage_quantity", 2))
    max_results = (run.parameters or {}).get("max_results", default_results)
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        fail(400, "invalid_request", "max_results must be an integer.")
    if not 1 <= max_results <= MAX_RESULTS_CEILING:
        fail(400, "invalid_request",
             f"max_results must be between 1 and {MAX_RESULTS_CEILING}.")

    # execution.deadline_ms is the caller's ceiling, measured from acceptance.
    # The protocol does not oblige us to enforce it - Pond just stops polling
    # and records a timeout - but a deadline longer than the max_run_seconds
    # we advertise is a contradiction we should reject rather than accept and
    # quietly under-deliver on.
    deadline_ms = (run.execution or {}).get("deadline_ms")
    max_run_ms = build_manifest(config)["limits"]["max_run_seconds"] * 1000
    if deadline_ms is not None:
        if not isinstance(deadline_ms, int) or deadline_ms <= 0:
            fail(400, "invalid_request", "deadline_ms must be a positive integer.")
        if deadline_ms > max_run_ms:
            fail(400, "invalid_request",
                 f"deadline_ms exceeds the advertised max_run_seconds "
                 f"({max_run_ms // 1000}s).")

    # A full scan cannot be guaranteed to finish inside the deadline, so accept
    # it as a task and let Pond poll. A state query answers immediately.
    if run.action_id == "query_state":
        text = _answer_state_query(prompt)
        response = {
            "run_id": run.run_id,
            "status": "completed",
            "output": [{"type": "text", "text": text}],
            "usage": {"unit_of_measurement": "result", "quantity": 1},
        }
        store.put_run(run.run_id, "completed", response)
        return response

    task_id = f"task_{uuid.uuid4().hex[:16]}"
    store.put_run(run.run_id, "queued", task_id=task_id)
    threading.Thread(
        target=_run_scan_task,
        args=(run.run_id, task_id, prompt, deadline_ms or max_run_ms,
              max_results),
        daemon=True,
    ).start()
    return JSONResponse(
        status_code=202,
        content={
            "run_id": run.run_id,
            "task_id": task_id,
            "status": "queued",
            "poll_after_ms": 3000,
        },
    )


@app.get("/tasks/{task_id}", dependencies=[Depends(authenticate_pond)])
def get_task(task_id: str):
    record = store.get_task(task_id)
    if not record:
        fail(404, "not_found", "Unknown task.")
    if record["status"] in ("completed", "failed") and record.get("response"):
        import json

        return json.loads(record["response"])
    return {
        "run_id": record["run_id"],
        "task_id": task_id,
        "status": record["status"],
        "updated_at": record["updated_at"],
    }


def _run_scan_task(run_id: str, task_id: str, prompt: str,
                   deadline_ms: int, max_results: int) -> None:
    store.put_run(run_id, "running", task_id=task_id)
    started = time.monotonic()
    try:
        # This is the paid path. On an on-request-only deployment the metered
        # sources have been idle since the last request, so a run has to both
        # allow the spend and clear the interval gates - otherwise the sweep is
        # skipped and the caller is told "no social candidates", which reads
        # exactly like a scan that ran and found nothing.
        capped = store.spend_today() >= config.daily_spend_cap
        if capped:
            log.warning(
                "daily spend cap $%.2f reached; run %s scans free sources only",
                config.daily_spend_cap, run_id,
            )
        outcome = agent.run_cycle(prompt or None,
                                  include_metered=not capped,
                                  force_metered=not capped,
                                  max_results=max_results)
        summary = outcome.summary

        # The scan is not interruptible mid-flight, so overrunning is reported
        # rather than prevented. Saying so is the point: a caller that already
        # timed out should not be told the run was delivered on time, and the
        # work itself is not wasted - alerts have already gone to Slack.
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if elapsed_ms > deadline_ms:
            log.warning("run %s took %dms, past its %dms deadline",
                        run_id, elapsed_ms, deadline_ms)
            note = (f"_(Completed in {elapsed_ms // 1000}s, past the "
                    f"{deadline_ms // 1000}s deadline.)_")
            summary = "\n\n".join([summary, note])

        response = {
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "output": [{"type": "text", "text": summary}],
            # Bill for the leads actually delivered. Reporting a flat 1 charged
            # the caller for a scan that found nothing and undercharged for one
            # that found several - the spec bills on what this reports.
            "usage": {"unit_of_measurement": "result",
                      "quantity": outcome.results},
        }
        store.put_run(run_id, "completed", response, task_id=task_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("scan task failed")
        response = {
            "run_id": run_id,
            "task_id": task_id,
            "status": "failed",
            "error": {"code": "internal_error", "message": str(exc)[:300]},
            "usage": {"unit_of_measurement": "result", "quantity": 0},
        }
        store.put_run(run_id, "failed", response, task_id=task_id)


def _answer_state_query(prompt: str) -> str:
    alerts = store.recent_alerts(10)
    leads = store.lead_times_hours()
    health = store.health()
    lines = ["## YC Launch Monitor", ""]
    if leads:
        leads_sorted = sorted(leads)
        median = leads_sorted[len(leads_sorted) // 2]
        lines.append(
            f"**Lead time:** median {median:.1f}h ahead of the official listing "
            f"across {len(leads)} confirmed early detections."
        )
    else:
        lines.append("**Lead time:** no early detections confirmed yet.")
    lines.append("")
    lines.append(f"**Recent alerts ({len(alerts)}):**")
    for a in alerts:
        lines.append(
            f"- {a['name']} ({a.get('batch') or '?'}) - {a['alert_status']} "
            f"via {a.get('source')} at {a['alerted_at'][:16]}"
        )
    lines.append("")
    lines.append("**Sources:**")
    for h in health:
        state = "healthy" if h["healthy"] else f"degraded: {h.get('last_error')}"
        lines.append(f"- {h['source']}: {state}")
    lines.append("")
    lines.append(f"**Spend today:** ${store.spend_today():.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Operator endpoints (not part of Pond Protocol)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Human status page. Every other route is machine-facing, so following
    the deployed link would otherwise return a bare 404 and give a reader no
    way to tell a running agent from a broken one."""
    return status_page.render(store, notifier, config, REPO_URL)


def _source_rows() -> list[dict[str, Any]]:
    """Every configured source, whether or not it has ever run.

    Reporting only the sources with a stored row means a freshly deployed
    on-request agent shows no LinkedIn at all until someone calls it, and an
    absent source reads worse than a described one - the first rejection of
    this listing was for a source that looked broken. So each source is listed
    with an explicit state, and one that is merely waiting to be asked says so.
    """
    stored = {r["source"]: r for r in store.health()}
    rows = []
    for name, source in sources.items():
        row = dict(stored.get(name) or {
            "source": name, "last_run_at": None, "last_error": None,
            "healthy": 1,
        })
        # Only a source with nothing free to do waits for a caller. X is mixed:
        # its timeline tiers run on the timer and just its keyword sweep waits,
        # so calling the whole source request-only would misreport it.
        on_request = bool(source.metered and config.metered_on_request_only
                          and not source.has_free_tier)
        if not source.enabled:
            row["state"] = "disabled"
        elif row["last_run_at"] is None:
            row["state"] = "awaiting_request" if on_request else "pending"
        elif not row["healthy"]:
            row["state"] = "degraded"
        else:
            row["state"] = "healthy"
        # Says plainly why a paid source's last run can look old: it only runs
        # when a caller asks, so staleness here is the design, not a fault.
        row["runs_on_request_only"] = on_request
        if (source.metered and config.metered_on_request_only
                and source.has_free_tier):
            row["paid_tier_on_request_only"] = True
        rows.append(row)
    return rows


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "sources": _source_rows(),
        "slack_configured": notifier.configured,
        "model_configured": agent.client is not None,
        "spend_today_usd": round(store.spend_today(), 4),
        # Which commit is actually serving. Hosts can keep rebuilding a pinned
        # or cached revision while the repo moves on, and without this the only
        # way to tell is inferring it from behaviour.
        "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA")
                   or os.environ.get("GIT_COMMIT_SHA") or "unknown")[:7],
        "lookback_days": agent_module.LOOKBACK_DAYS,
        "x_actor": config.source("x").get("apify_actor"),
        # The paid keyword tier is off unless this reads "apify" AND a token is
        # present. Both are environment values, so a dashboard that mangles one
        # silently disables the only source that finds founders we don't follow.
        "x_keyword_tier": config.x_keyword_tier,
        "apify_token_set": bool(config.apify_token),
        "linkedin_actor": config.source("linkedin").get("apify_actor"),
        # True means the paid sources never run on the timer - they wait for a
        # Pond run. A reviewer seeing linkedin with an old last_run_at should
        # read this before concluding the source is broken.
        "metered_on_request_only": config.metered_on_request_only,
        "daily_spend_cap_usd": config.daily_spend_cap,
    }


@app.post("/admin/scan")
def admin_scan(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Trigger a cycle by hand. Used for the demo recording.

    Guarded: the service is on a public URL, and an open trigger lets anyone
    spend the Apify budget and post into the client's Slack channel. With no
    ADMIN_TOKEN configured the route does not exist at all, so a deployment
    that forgets to set one is closed rather than open.
    """
    expected = config.admin_token
    if not expected:
        fail(404, "not_found", "This route is not enabled.")
    if x_admin_token != expected:
        fail(401, "unauthorized", "A valid X-Admin-Token header is required.")
    # A manual scan should actually scan: without this the time-gated keyword
    # tier is skipped and the response says "no social candidates", which is
    # indistinguishable from a sweep that ran and found nothing.
    outcome = agent.run_cycle(include_metered=True, force_metered=True)
    return {"summary": outcome.summary, "results": outcome.results}


# ---------------------------------------------------------------------------
# Pond error envelope
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def pond_error(_request: Request, error: HTTPException):
    detail = error.detail
    if not isinstance(detail, dict):
        detail = {"code": "invalid_request", "message": str(detail)}
    return JSONResponse(status_code=error.status_code, content={"error": detail})


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _error: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"error": {"code": "invalid_request",
                           "message": "The request does not match Pond Protocol V1."}},
    )
