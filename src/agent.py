"""The agent.

Claude drives the tools and makes the judgment calls. The things that must
never be wrong stay in deterministic code.

That split is the whole design. Deciding whether "we're joining the batch" is
an acceptance or an interview announcement, whether two posts describe the same
company, and whether the evidence justifies an alert - those are judgment
calls, and they are what the model is for. Deduplication, state and delivery
are not judgment calls. If the model *can* send a duplicate, eventually it
will, so `record_alert` is an atomic check-and-set and `post_slack_alert`
refuses to fire without a reserved slot.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import anthropic
from anthropic import beta_tool

from .models import AlertStatus, Program, normalize_batch, normalize_company

log = logging.getLogger(__name__)

CYCLE_PROMPT = """\
You are monitoring for companies newly accepted into Y Combinator or a16z \
SPEEDRUN, with one priority above all others: find founders who have announced \
their acceptance publicly BEFORE the official directory lists them.

Work through this cycle:

1. Call `collect_signals` to get the posts and directory entries gathered since \
the last run.
2. For each candidate, decide what it actually claims. Only a genuine \
ACCEPTANCE is alertable. Reject: interview announcements ("our YC interview is \
next week"), applications, rejection post-mortems, and alumni reminiscing about \
an old batch. These are the most common false positives and they matter - the \
person reading these alerts will cold-email the founder, so a wrong alert costs \
more than a missed one.
3. For anything that looks like a real acceptance, call `lookup_company` to \
check whether the directory already lists it.
   - Not listed  -> this is the scoop. Status "early".
   - Listed, and the signal came from the directory itself -> status "confirmed".
   - Listed, and the signal came from social -> already public. Suppress it.
4. Before alerting, call `check_already_alerted`. Two cofounders posting the \
same news is one company, not two alerts.
5. Call `record_alert` to reserve the company, then `post_slack_alert`. If \
`record_alert` returns already_claimed, do not alert - another signal in this \
same cycle got there first.

Be decisive. If evidence is thin, say so and skip rather than guessing. \
Finish by summarising what you alerted on and what you deliberately skipped."""


class MonitorAgent:
    def __init__(self, config, store, sources, notifier):
        self.config = config
        self.store = store
        self.sources = sources
        self.notifier = notifier
        self.client = anthropic.Anthropic() if config.anthropic_key else None
        agent_cfg = config.get("agent", {}) or {}
        self.model = agent_cfg.get("model", "claude-opus-5")
        self.max_tokens = int(agent_cfg.get("max_tokens", 16000))
        self.max_iterations = int(agent_cfg.get("max_iterations", 40))
        self.threshold = float(agent_cfg.get("confidence_threshold", 0.75))
        self._pending: list = []

    # ------------------------------------------------------------------
    # Tools. Each is a plain function; the SDK builds the schema from the
    # signature and docstring.
    # ------------------------------------------------------------------
    def _build_tools(self):
        store = self.store
        sources = self.sources
        notifier = self.notifier
        agent = self

        @beta_tool
        def collect_signals() -> str:
            """Gather new candidate posts and directory entries from every source.

            Returns the signals collected since the last cycle, already filtered
            to those mentioning YC or SPEEDRUN with a plausible batch code.
            """
            since = datetime.now(timezone.utc) - timedelta(days=3)
            collected = []
            for source in sources.values():
                if not source.enabled or not source.due():
                    continue
                for sig in source.collect(since):
                    if source.name in ("yc_directory", "yc_speedrun") or sig.looks_relevant():
                        collected.append(sig)
            agent._pending = collected
            payload = [
                {
                    "source": s.source,
                    "id": s.external_id,
                    "author": s.author,
                    "text": s.text[:600],
                    "url": s.url,
                    "created_at": s.created_at.isoformat(),
                }
                for s in collected[:60]
            ]
            return json.dumps({"count": len(collected), "signals": payload})

        @beta_tool
        def lookup_company(company_name: str) -> str:
            """Check whether a company is already listed in the official directory.

            This is the verification oracle. Absent from the directory means the
            founder's announcement is still ahead of the official listing.

            Args:
                company_name: The company name as it appears in the post.
            """
            yc = sources.get("yc_directory")
            speedrun = sources.get("yc_speedrun")
            hit = yc.lookup(company_name) if yc else None
            if hit:
                return json.dumps({
                    "listed": True, "program": "yc",
                    "batch": normalize_batch(hit.get("batch")),
                    "description": hit.get("oneLiner"), "url": hit.get("url"),
                })
            hit = speedrun.lookup(company_name) if speedrun else None
            if hit:
                return json.dumps({"listed": True, "program": "speedrun",
                                   "url": hit.get("url")})
            return json.dumps({"listed": False})

        @beta_tool
        def check_already_alerted(company_name: str) -> str:
            """Check whether an alert has already been sent for this company.

            Args:
                company_name: The company name as it appears in the post.
            """
            key = normalize_company(company_name)
            return json.dumps({"already_alerted": store.has_alerted(key), "key": key})

        @beta_tool
        def record_alert(
            company_name: str,
            status: str,
            program: str = "yc",
            batch: str = "",
            website: str = "",
            source: str = "",
        ) -> str:
            """Reserve the right to alert on a company. Call before posting.

            This is an atomic check-and-set. If it returns already_claimed, some
            other signal reserved this company first and you must not alert.

            Args:
                company_name: Company name.
                status: Either "early" or "confirmed".
                program: Either "yc" or "speedrun".
                batch: Batch code such as F26, W27, P26, S26.
                website: Company website if known.
                source: Which source produced the signal.
            """
            key = normalize_company(company_name)
            ok = store.claim_alert_slot(
                key, company_name, program, normalize_batch(batch) or None,
                source, status, website or None,
            )
            return json.dumps({"claimed": ok, "key": key,
                               "reason": None if ok else "already_claimed"})

        @beta_tool
        def post_slack_alert(
            company_name: str,
            status: str,
            batch: str = "",
            founder: str = "",
            source: str = "",
            description: str = "",
            url: str = "",
            quote: str = "",
            program: str = "yc",
        ) -> str:
            """Send the alert to Slack. Only works for a company you reserved.

            Args:
                company_name: Company name.
                status: Either "early" or "confirmed".
                batch: Batch code such as F26.
                founder: Founder name and handle, if known.
                source: Where the signal came from, e.g. "X" or "YC Directory".
                description: One-line description of the company.
                url: Link to the original post, or the directory profile.
                quote: The founder's own words, for the Original post block.
                program: Either "yc" or "speedrun".
            """
            key = normalize_company(company_name)
            if not store.has_alerted(key):
                return json.dumps({
                    "sent": False,
                    "error": "No reservation for this company. Call record_alert first.",
                })
            sent = notifier.send(
                company=company_name, status=status, program=program,
                batch=normalize_batch(batch) or None, founder=founder or None,
                source=source or None, description=description or None,
                url=url or None, quote=quote or None,
            )
            return json.dumps({"sent": sent, "dry_run": not notifier.configured})

        return [
            collect_signals, lookup_company, check_already_alerted,
            record_alert, post_slack_alert,
        ]

    # ------------------------------------------------------------------
    def bootstrap(self) -> int:
        """Seed state on first boot without alerting.

        Without this the very first cycle treats every company already in the
        configured batches as a new discovery and fires hundreds of Slack
        messages - measured at 420 on a cold start across four batches. The
        client's first experience of the bot would be a flooded channel.

        So on an empty database we record what already exists as known-and-
        alerted, silently. Alerts then begin from the next arrival, which is
        what "only push incremental updates" means in the brief.
        """
        if self.store.company_count() > 0:
            return 0

        since = datetime.now(timezone.utc) - timedelta(days=3)
        seeded = 0
        for name in ("yc_directory", "yc_speedrun"):
            source = self.sources.get(name)
            if not source or not source.enabled:
                continue
            for sig in source.collect(since):
                fields = source.to_alert_fields(sig.raw)
                key = normalize_company(fields.get("company") or "")
                if not key:
                    continue
                if self.store.claim_alert_slot(
                    key, fields["company"], fields["program"], fields["batch"],
                    name, "seeded", fields.get("website"),
                ):
                    self.store.record_signal(sig, company_key=key)
                    seeded += 1
        log.info("bootstrap seeded %d existing companies (no alerts sent)", seeded)
        return seeded

    def run_cycle(self, instruction: str | None = None) -> str:
        """One monitoring pass. Returns the agent's own summary."""
        if self.client is None:
            return self._run_without_model()

        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            tools=self._build_tools(),
            messages=[{"role": "user", "content": instruction or CYCLE_PROMPT}],
        )

        summary, turns = "", 0
        for message in runner:
            turns += 1
            if turns > self.max_iterations:
                log.warning("iteration cap hit at %s turns; stopping cycle",
                            self.max_iterations)
                break
            for block in message.content:
                if block.type == "text" and block.text.strip():
                    summary = block.text
        return summary or "Cycle completed with no summary."

    def _run_without_model(self) -> str:
        """Deterministic fallback when no Anthropic key is configured.

        Directory arrivals are unambiguous - a company appearing in YC's own
        API needs no judgment - so the confirmed-alert path still works and the
        pipeline stays demonstrable before any key is added.
        """
        since = datetime.now(timezone.utc) - timedelta(days=3)
        sent = 0
        for name in ("yc_directory", "yc_speedrun"):
            source = self.sources.get(name)
            if not source or not source.enabled:
                continue
            for sig in source.collect(since):
                fields = source.to_alert_fields(sig.raw)
                key = normalize_company(fields["company"] or "")
                if not key or not self.store.claim_alert_slot(
                    key, fields["company"], fields["program"], fields["batch"],
                    name, AlertStatus.CONFIRMED.value, fields.get("website"),
                ):
                    continue
                self.store.record_signal(sig, company_key=key)
                self.notifier.send(
                    company=fields["company"], status="confirmed",
                    program=fields["program"], batch=fields["batch"],
                    source="YC Directory" if name == "yc_directory" else "Speedrun",
                    description=fields.get("description"), url=fields.get("url"),
                )
                sent += 1
        return f"No ANTHROPIC_API_KEY set - ran directory-only pass. {sent} alert(s)."
