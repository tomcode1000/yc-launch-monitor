"""Pond Protocol V1 conformance.

Each of these was a real gap found by auditing the published spec against the
running service before resubmitting the listing. They are cheap to break again
by accident, and a reviewer tests exactly these.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_PATH",
                      str(Path(tempfile.mkdtemp()) / "protocol.db"))
os.environ["SLACK_BOT_TOKEN"] = ""
os.environ.setdefault("POND_ACCESS_KEY", "test-key-for-protocol-suite")

from fastapi.testclient import TestClient  # noqa: E402

import src.server as server  # noqa: E402

KEY = os.environ["POND_ACCESS_KEY"]
V = {"X-Agent-Protocol-Version": "1.0"}
AUTH = {"Authorization": f"Bearer {KEY}", **V}


@pytest.fixture()
def client(monkeypatch):
    # Never let a protocol test reach a paid actor or run a real scan.
    monkeypatch.setattr(server.agent, "run_cycle",
                        lambda *a, **k: server.agent_module.CycleOutcome("stub", 0))
    return TestClient(server.app)


def _body(run_id, **kw):
    return {"run_id": run_id, **kw}


def test_manifest_is_public_and_needs_no_version_header(client):
    r = client.get("/manifest")
    assert r.status_code == 200
    m = r.json()
    assert m["protocol"] == "marketplace-agent"
    assert m["protocol_version"] == "1.0"
    assert m["agent_version"]
    assert set(m["limits"]) == {
        "max_request_bytes", "max_attachment_bytes", "max_run_seconds"}
    assert len(m["input_modes"]) >= 1 and len(m["output_modes"]) >= 1
    for field in ("sync", "streaming", "async_tasks", "cancellation",
                  "attachments", "feedback"):
        assert field in m["capabilities"]
    assert len(json.dumps(m).encode()) < 256 * 1024


def test_every_declared_action_schema_is_well_formed(client):
    for action in client.get("/manifest").json()["actions"]:
        s = action["input_schema"]
        assert s["type"] == "object"
        assert s["additionalProperties"] is False
        assert "required" in s and "properties" in s
        for name, prop in s["properties"].items():
            assert "type" in prop, f"{action['id']}.{name} has no type"
            assert prop.get("description"), f"{action['id']}.{name} undescribed"


def test_missing_or_bad_key_is_401(client):
    assert client.post("/runs", json=_body("a")).status_code == 401
    r = client.post("/runs", json=_body("a"),
                    headers={"Authorization": "Bearer wrong", **V,
                             "Idempotency-Key": "a"})
    assert r.status_code == 401


@pytest.mark.parametrize("version,code", [
    ("1.0.1", "invalid_request"),          # patch versions are malformed
    ("nonsense", "invalid_request"),
    ("1.1", "unsupported_protocol_version"),
])
def test_protocol_version_handling(client, version, code):
    r = client.post("/runs", json=_body("v"),
                    headers={"Authorization": f"Bearer {KEY}",
                             "X-Agent-Protocol-Version": version,
                             "Idempotency-Key": "v"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code


def test_unknown_task_uses_the_spec_code(client):
    r = client.get("/tasks/task_missing", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"


def test_error_bodies_carry_run_id(client):
    r = client.post("/runs", json=_body("e1", action_id="nope"),
                    headers={**AUTH, "Idempotency-Key": "e1"})
    assert r.status_code == 400
    assert r.json()["run_id"] == "e1"
    assert r.json()["error"]["code"] == "unsupported_operation"


def test_schema_violation_is_422_invalid_input(client):
    r = client.post(
        "/runs",
        json=_body("i1", action_id="scan_now", parameters={"max_results": 999}),
        headers={**AUTH, "Idempotency-Key": "i1"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_input"


def test_output_mode_mismatch_is_415(client):
    r = client.post(
        "/runs",
        json=_body("o1", action_id="query_state", parameters={"prompt": "x"},
                   execution={"accepted_output_modes": ["image/png"]}),
        headers={**AUTH, "Idempotency-Key": "o1"})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "unsupported_content_type"


def test_same_key_different_request_is_409(client):
    first = _body("c1", action_id="query_state", parameters={"prompt": "one"})
    r1 = client.post("/runs", json=first, headers={**AUTH,
                                                   "Idempotency-Key": "c1"})
    assert r1.status_code == 200

    # A genuine retry replays the stored answer.
    r2 = client.post("/runs", json=first, headers={**AUTH,
                                                   "Idempotency-Key": "c1"})
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    # A different request wearing the same key is a conflict, not a replay.
    other = _body("c1", action_id="query_state", parameters={"prompt": "two"})
    r3 = client.post("/runs", json=other, headers={**AUTH,
                                                   "Idempotency-Key": "c1"})
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_conflict"


def test_idempotency_key_must_match_run_id(client):
    r = client.post("/runs", json=_body("m1"),
                    headers={**AUTH, "Idempotency-Key": "different"})
    assert r.status_code == 400


def test_a_body_over_the_declared_limit_is_refused(client):
    declared = client.get("/manifest").json()["limits"]["max_request_bytes"]
    oversized = _body("b1", action_id="query_state",
                      parameters={"prompt": "x" * (declared + 1000)})
    r = client.post("/runs", json=oversized,
                    headers={**AUTH, "Idempotency-Key": "b1"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_terminal_responses_carry_usage(client):
    r = client.post(
        "/runs",
        json=_body("u1", action_id="query_state", parameters={"prompt": "x"}),
        headers={**AUTH, "Idempotency-Key": "u1"})
    assert r.status_code == 200
    usage = r.json()["usage"]
    assert usage["unit_of_measurement"] in ("token", "result", "other")
    assert isinstance(usage["quantity"], int) and usage["quantity"] >= 0


def test_the_reported_unit_matches_the_pricing_plan(client):
    m = client.get("/manifest").json()
    plan_unit = m["metadata"]["pricing_plans"][0]["usage_unit"]
    r = client.post(
        "/runs",
        json=_body("u2", action_id="query_state", parameters={"prompt": "x"}),
        headers={**AUTH, "Idempotency-Key": "u2"})
    assert r.json()["usage"]["unit_of_measurement"] == plan_unit
