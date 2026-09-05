"""SQLite state.

Five tables carry everything:

  companies    one row per company we have ever decided to alert on. The
               uniqueness constraint here is what makes duplicate alerts
               structurally impossible rather than merely unlikely.
  signals      every raw post we classified, kept so lead time stays
               computable and any false positive traces back to its source.
  pond_runs    Pond Protocol requires idempotent responses to be persisted: a
               retried run_id must return the original response, not re-run.
  spend        per-day ledger for metered sources, backing the cost governor.
  source_state per-source health, surfaced in the Slack digest and /health.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    key           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    program       TEXT NOT NULL,
    batch         TEXT,
    website       TEXT,
    first_seen_at TEXT NOT NULL,
    alerted_at    TEXT,
    alert_status  TEXT,
    source        TEXT,
    confirmed_at  TEXT,
    founder       TEXT,
    post_url      TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    company_key  TEXT,
    text         TEXT,
    url          TEXT,
    author       TEXT,
    created_at   TEXT,
    seen_at      TEXT NOT NULL,
    claim_type   TEXT,
    confidence   REAL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS pond_runs (
    run_id     TEXT PRIMARY KEY,
    task_id    TEXT,
    status     TEXT NOT NULL,
    response   TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spend (
    day       TEXT NOT NULL,
    source    TEXT NOT NULL,
    usd       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
);

CREATE TABLE IF NOT EXISTS source_state (
    source      TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_error  TEXT,
    healthy     INTEGER NOT NULL DEFAULT 1
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Databases created before founder-first leads existed predate these
        # columns. Add them rather than making the client wipe state.
        for table, column in (("pond_runs", "request_hash TEXT"),):
            try:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        for column in ("founder TEXT", "post_url TEXT"):
            try:
                self._conn.execute(f"ALTER TABLE companies ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        self._conn.commit()

    # --- companies / dedupe -------------------------------------------
    def company_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()
        return int(row["n"])

    def has_alerted(self, key: str) -> bool:
        row = self._conn.execute(
            "SELECT alerted_at FROM companies WHERE key = ?", (key,)
        ).fetchone()
        return bool(row and row["alerted_at"])

    def claim_alert_slot(
        self,
        key: str,
        name: str,
        program: str,
        batch: str | None,
        source: str,
        status: str,
        website: str | None = None,
        founder: str | None = None,
        post_url: str | None = None,
    ) -> bool:
        """Reserve the right to alert on a company. Returns False if taken.

        This is the guardrail that makes the agent unable to double-post. It is
        an atomic check-and-set, not a suggestion the model is asked to honour:
        if the model calls it twice for the same company the second call
        returns False, and post_slack_alert refuses to send without a slot.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT alerted_at FROM companies WHERE key = ?", (key,)
            ).fetchone()
            if cur and cur["alerted_at"]:
                return False
            now = _now()
            self._conn.execute(
                """INSERT INTO companies
                     (key, name, program, batch, website, first_seen_at,
                      alerted_at, alert_status, source, founder, post_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     alerted_at=excluded.alerted_at,
                     alert_status=excluded.alert_status,
                     batch=COALESCE(excluded.batch, companies.batch),
                     founder=COALESCE(excluded.founder, companies.founder),
                     post_url=COALESCE(excluded.post_url, companies.post_url)""",
                (key, name, program, batch, website, now, now, status, source,
                 founder, post_url),
            )
            self._conn.commit()
            return True

    def release_alert_slot(self, key: str) -> None:
        """Hand back a claimed slot when delivery failed.

        Slots are claimed before sending so two cycles cannot race to alert the
        same lead. If the send then fails, holding the claim would retire the
        lead permanently: it counts as delivered and is never retried. Clearing
        alerted_at puts it back in play for the next cycle.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE companies SET alerted_at = NULL WHERE key = ?", (key,))
            self._conn.commit()

    def note_company(self, key: str, name: str, program: str, batch: str | None) -> None:
        """Record a company without alerting - used for directory backfill."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO companies
                     (key, name, program, batch, first_seen_at)
                   VALUES (?,?,?,?,?)""",
                (key, name, program, batch, _now()),
            )
            self._conn.commit()

    def mark_confirmed(self, key: str) -> None:
        """Called when a previously-early company appears in the directory.

        The gap between alerted_at and confirmed_at is the lead time - the
        number the whole submission is arguing for.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE companies SET confirmed_at = ? WHERE key = ? AND confirmed_at IS NULL",
                (_now(), key),
            )
            self._conn.commit()

    def lead_times_hours(self) -> list[float]:
        rows = self._conn.execute(
            """SELECT alerted_at, confirmed_at FROM companies
               WHERE alert_status IN ('early','early_unverified')
                 AND confirmed_at IS NOT NULL"""
        ).fetchall()
        out = []
        for r in rows:
            a = datetime.fromisoformat(r["alerted_at"])
            c = datetime.fromisoformat(r["confirmed_at"])
            out.append((c - a).total_seconds() / 3600)
        return out

    def alerted_lead_count(self) -> int:
        """Leads actually delivered to Slack.

        Counts social leads only. Directory listings are recorded as the
        verification baseline and never alerted, so including them would
        report hundreds of alerts nobody ever received - and older databases
        still carry rows from before the baseline stopped claiming alert
        slots, which this definition ignores by construction.
        """
        row = self._conn.execute(
            """SELECT COUNT(*) AS n FROM companies
               WHERE alerted_at IS NOT NULL
                 AND source NOT IN ('yc_directory', 'yc_speedrun')"""
        ).fetchone()
        return int(row["n"]) if row else 0

    def count_alerts_since(self, since_iso: str) -> int:
        """How many leads were actually delivered since `since_iso`.

        Counted from the store rather than tallied per code path, because a
        cycle can alert from the directory pass, the rules pass or the model
        pass, and Pond bills on what this number says. A count that only some
        paths increment would bill the caller for leads they never got.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM companies WHERE alerted_at >= ?",
            (since_iso,),
        ).fetchone()
        return int(row["n"] if row else 0)

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT name, batch, program, alert_status, alerted_at, source,
                      founder, post_url
               FROM companies WHERE alerted_at IS NOT NULL
               ORDER BY alerted_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- signals -------------------------------------------------------
    def seen_signal(self, source: str, external_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM signals WHERE source=? AND external_id=?",
                (source, external_id),
            ).fetchone()
            is not None
        )

    def record_signal(
        self, sig, company_key=None, claim_type=None, confidence=None
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO signals
                   (source, external_id, company_key, text, url, author,
                    created_at, seen_at, claim_type, confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    sig.source,
                    sig.external_id,
                    company_key,
                    sig.text,
                    sig.url,
                    sig.author,
                    sig.created_at.isoformat(),
                    _now(),
                    claim_type,
                    confidence,
                ),
            )
            self._conn.commit()

    # --- pond idempotency ---------------------------------------------
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM pond_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def put_run(self, run_id, status, response=None, task_id=None,
                request_hash=None) -> None:
        with self._lock:
            now = _now()
            self._conn.execute(
                """INSERT INTO pond_runs
                     (run_id, task_id, status, response, created_at, updated_at,
                      request_hash)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status=excluded.status,
                     response=COALESCE(excluded.response, pond_runs.response),
                     task_id=COALESCE(excluded.task_id, pond_runs.task_id),
                     request_hash=COALESCE(pond_runs.request_hash,
                                           excluded.request_hash),
                     updated_at=excluded.updated_at""",
                (
                    run_id,
                    task_id,
                    status,
                    json.dumps(response) if response is not None else None,
                    now,
                    now,
                    request_hash,
                ),
            )
            self._conn.commit()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM pond_runs WHERE task_id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    # --- spend governor ------------------------------------------------
    def add_spend(self, source: str, usd: float) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO spend (day, source, usd) VALUES (?,?,?)
                   ON CONFLICT(day, source) DO UPDATE SET
                     usd = spend.usd + excluded.usd""",
                (date.today().isoformat(), source, usd),
            )
            self._conn.commit()

    def spend_today(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(usd),0) AS t FROM spend WHERE day=?",
            (date.today().isoformat(),),
        ).fetchone()
        return float(row["t"])

    # --- source health -------------------------------------------------
    def set_health(self, source: str, healthy: bool, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO source_state (source, last_run_at, last_error, healthy)
                   VALUES (?,?,?,?)
                   ON CONFLICT(source) DO UPDATE SET
                     last_run_at=excluded.last_run_at,
                     last_error=excluded.last_error,
                     healthy=excluded.healthy""",
                (source, _now(), error, 1 if healthy else 0),
            )
            self._conn.commit()

    def health(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM source_state").fetchall()
        return [dict(r) for r in rows]
