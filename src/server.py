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
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

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
        target=_run_scan_task, args=(run.run_id, task_id, prompt), daemon=True
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


def _run_scan_task(run_id: str, task_id: str, prompt: str) -> None:
    store.put_run(run_id, "running", task_id=task_id)
    try:
        summary = agent.run_cycle(prompt or None)
        response = {
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "output": [{"type": "text", "text": summary}],
            "usage": {"unit_of_measurement": "result", "quantity": 1},
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


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "sources": store.health(),
        "slack_configured": notifier.configured,
        "model_configured": agent.client is not None,
        "spend_today_usd": round(store.spend_today(), 4),
    }


@app.post("/admin/scan")
def admin_scan() -> dict[str, Any]:
    """Trigger a cycle by hand. Used for the demo recording."""
    return {"summary": agent.run_cycle()}


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
