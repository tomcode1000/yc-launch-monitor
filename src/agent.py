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
import os
from datetime import datetime, timedelta, timezone

# The Anthropic SDK is optional. This bot ships rules-only by default, so a
# deployment with no API key should not even need the package installed. The
# import is guarded rather than assumed - a missing optional dependency must
# not stop the server from booting.
try:
    import anthropic
    from anthropic import beta_tool
except ImportError:  # pragma: no cover - exercised by the no-SDK deployment
    anthropic = None

    def beta_tool(fn):  # type: ignore[misc]
        return fn

from . import classify_rules
from .models import AlertStatus, lead_key, normalize_batch, normalize_company

log = logging.getLogger(__name__)

# How far back each cycle looks. Wide enough to cover a first run and any
# missed cycles; dedupe, not the window, is what prevents repeat alerts.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "5"))

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
3. Identify the company if you can, but understand what the lead actually is. \
The person reading these alerts acts by MESSAGING THE FOUNDER through their \
post link. So the founder and the link are the lead; the company name is \
useful metadata, not a requirement. A post saying only "we got into YC F26!!" \
is a complete and valuable lead. Never discard a genuine acceptance because no \
company is named - pass company_name="" and the handle instead.
4. If you have a company name, call `lookup_company` to check the directory.
   - Not listed  -> the scoop. Status "early".
   - Listed, and the signal came from the directory itself -> "confirmed".
   - Listed, and the signal came from social -> already public. Suppress it.
   With no company name there is nothing to look up, so use status \
"early_unverified". The lead is real; just do not claim a check you did not make.
5. Before alerting, call `check_already_alerted` with the company name if you \
have one, otherwise the author handle. Two cofounders posting the same news is \
one lead, not two alerts.
6. Call `record_alert` to reserve the lead, then `post_slack_alert`. If \
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
        agent_cfg = config.get("agent", {}) or {}
        self.model = agent_cfg.get("model", "claude-opus-5")
        self.max_tokens = int(agent_cfg.get("max_tokens", 16000))
        self.max_iterations = int(agent_cfg.get("max_iterations", 40))
        self.threshold = float(agent_cfg.get("confidence_threshold", 0.75))
        # Rules-only by default. The bot ships without an Anthropic dependency;
        # set LLM_MODE=auto to add model judgment on top (see README).
        #   off    - no model at all, no key, no SDK required
        #   auto   - rules first, model only on surviving social signals
        #   always - model every cycle
        self.llm_mode = os.environ.get("LLM_MODE", "off").lower()

        self.client = None
        if self.llm_mode != "off":
            if anthropic is None:
                log.warning(
                    "LLM_MODE=%s but the anthropic package is not installed. "
                    "Falling back to rules-only. Install with: pip install anthropic",
                    self.llm_mode,
                )
                self.llm_mode = "off"
            elif not config.anthropic_key:
                log.warning(
                    "LLM_MODE=%s but ANTHROPIC_API_KEY is unset. "
                    "Falling back to rules-only.", self.llm_mode,
                )
                self.llm_mode = "off"
            else:
                self.client = anthropic.Anthropic()

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
            """Get the candidate posts gathered for this cycle.

            These have already passed a cheap rules prefilter that removed
            interview announcements, applications, rejection post-mortems and
            alumni posts, so what remains is worth reading carefully.
            """
            collected = agent._pending
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
        def check_already_alerted(
            company_name: str = "", founder_handle: str = ""
        ) -> str:
            """Check whether this lead has already been alerted on.

            Pass whichever you have. Identity prefers the company name because
            it collapses cofounders posting the same news into one lead; it
            falls back to the author handle when no company is named.

            Args:
                company_name: Company name, if the post gives one.
                founder_handle: The author handle, e.g. @janedoe.
            """
            key = lead_key(company_name or None, founder_handle or None)
            if not key:
                return json.dumps({"error": "Provide company_name or founder_handle."})
            return json.dumps({"already_alerted": store.has_alerted(key), "key": key})

        @beta_tool
        def record_alert(
            status: str,
            company_name: str = "",
            founder_handle: str = "",
            post_url: str = "",
            program: str = "yc",
            batch: str = "",
            website: str = "",
            source: str = "",
        ) -> str:
            """Reserve the right to alert on a lead. Call before posting.

            Atomic check-and-set. If it returns already_claimed, another signal
            reserved this lead first and you must not alert.

            Args:
                status: "early", "early_unverified", or "confirmed".
                company_name: Company name if the post names one. May be empty.
                founder_handle: Author handle. Required when company_name is empty.
                post_url: Link to the original post.
                program: Either "yc" or "speedrun".
                batch: Batch code such as F26, W27, P26, S26.
                website: Company website if known.
                source: Which source produced the signal.
            """
            key = lead_key(company_name or None, founder_handle or None)
            if not key:
                return json.dumps({
                    "claimed": False,
                    "reason": "Provide company_name or founder_handle.",
                })
            ok = store.claim_alert_slot(
                key, company_name or founder_handle or "unknown", program,
                normalize_batch(batch) or None, source, status, website or None,
                founder=founder_handle or None, post_url=post_url or None,
            )
            return json.dumps({"claimed": ok, "key": key,
                               "reason": None if ok else "already_claimed"})

        @beta_tool
        def post_slack_alert(
            status: str,
            company_name: str = "",
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
            key = lead_key(company_name or None, founder or None)
            if not key or not store.has_alerted(key):
                return json.dumps({
                    "sent": False,
                    "error": "No reservation for this lead. Call record_alert first.",
                })
            sent = notifier.send(
                company=company_name or None, status=status, program=program,
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

        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        seeded = 0
        for name in ("yc_directory", "yc_speedrun"):
            source = self.sources.get(name)
            if not source or not source.enabled:
                continue
            # A listing here is already-public news. Record it so it can serve
            # as the verification baseline, but stay quiet unless explicitly
            # configured to alert - the product is the pre-announcement scoop.
            announce = bool(self.config.source(name).get("alert_on_new_listing", False))

            collected = source.collect(since)
            self._book_spend(source)
            for sig in collected:
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

    def run_cycle(self, instruction: str | None = None,
                  include_metered: bool | None = None) -> str:
        """One monitoring pass, routed by how much judgment it actually needs.

        Directory arrivals are unambiguous - a company appearing in YC's own
        API needs no interpretation - so they always take the deterministic
        path and cost nothing. The model is invoked only when social signals
        survive the free rules prefilter, which on most cycles is nothing.

        That gate is what stops a 60-second directory poll from firing an Opus
        tool loop every minute.

        `include_metered` decides whether the paid sources are consulted at
        all. Left as None it follows config: on-request-only deployments pass
        True from the request path and False from the background loop.
        """
        if include_metered is None:
            include_metered = not self.config.metered_on_request_only

        parts = [self.run_directory_pass()]

        candidates = self.gather_social_candidates(include_metered)
        if not candidates:
            parts.append("No social candidates; no model call made.")
            return " ".join(parts)

        if self.llm_mode == "off" or self.client is None:
            parts.append(self.run_rules_pass(candidates))
            return " ".join(parts)

        parts.append(self._run_model_pass(candidates, instruction))
        return " ".join(parts)

    def _book_spend(self, source) -> None:
        """Charge a source's run against today's budget.

        Without this the governor reads a spend of zero forever and the daily
        cap never trips, which matters only for the metered sources.
        """
        cost = source.estimated_cost(getattr(source, "last_item_count", 0))
        if cost:
            self.store.add_spend(source.name, cost)

    def force_keyword_sweep(self) -> None:
        """Clear the keyword tier's interval so the next cycle sweeps now.

        The paid sweep is time-gated, so a manual trigger inside the interval
        would skip it and report "no social candidates" - which reads exactly
        like a working scan that found nothing. Used by /admin/scan.
        """
        for name in ("x", "linkedin"):
            src = self.sources.get(name)
            if src is None:
                continue
            # Both gates have to open. gather_social_candidates checks the
            # source's own poll interval first, and the scheduler has usually
            # just run it, so clearing only the keyword timer left the source
            # skipped before the keyword tier was ever consulted.
            src._last_run = 0.0
            if hasattr(src, "_last_keyword_run"):
                src._last_keyword_run = 0.0
                src._cooldown_until = 0.0

    def gather_social_candidates(self, include_metered: bool = True) -> list:
        """Collect X/LinkedIn signals that survive the free rules prefilter.

        With `include_metered` False the paid sources are skipped without
        being touched, so no Apify credit is spent and their recorded health
        keeps whatever the last real run reported rather than being reset.
        """
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        out = []
        for name in ("x", "linkedin"):
            source = self.sources.get(name)
            if not source or not source.enabled or not source.due():
                continue
            source.allow_paid_calls(include_metered)
            if source.metered and not include_metered and not source.has_free_tier:
                # Nothing this source can contribute without spending, so it is
                # not touched at all - its recorded health stays whatever the
                # last real run reported instead of being overwritten.
                log.debug("%s is fully metered and this pass is unpaid; skipping", name)
                continue
            collected = source.collect(since)
            self._book_spend(source)
            for sig in collected:
                if self.store.seen_signal(sig.source, sig.external_id):
                    continue
                if not sig.looks_relevant():
                    continue
                if not classify_rules.is_candidate(sig.text):
                    # Rejected for free: interview announcements, applications,
                    # rejection post-mortems, alumni reminiscing.
                    self.store.record_signal(sig, claim_type="filtered")
                    continue
                out.append(sig)
        return out

    def run_rules_pass(self, candidates: list) -> str:
        """Rules-only handling. No model, no API cost.

        Used when LLM_MODE=off or no key is configured. Weaker than the model
        at naming companies, and it declines rather than guessing: a signal it
        cannot name is logged for review, never alerted. A wrong name here
        would become a cold email to a company that never got in.
        """
        alerted = skipped = 0
        for sig in candidates:
            claim, confidence = classify_rules.classify(sig.text)
            if confidence < self.threshold:
                self.store.record_signal(
                    sig, claim_type=claim.value, confidence=confidence)
                skipped += 1
                continue

            # The company name is metadata, not the lead. What the client acts
            # on is the founder and their post, both of which we always have.
            company = classify_rules.extract_company(sig.text)
            key = lead_key(company, sig.author)
            if not key:
                skipped += 1
                continue

            # Verification needs a name to look up. Without one, say so rather
            # than claiming a directory check that never happened.
            if company:
                if self._listed(company):
                    self.store.record_signal(
                        sig, company_key=key, claim_type=claim.value)
                    skipped += 1
                    continue
                status = AlertStatus.EARLY.value
            else:
                status = AlertStatus.EARLY_UNVERIFIED.value

            batch = classify_rules.extract_batch(sig.text)
            program = classify_rules.extract_program(sig.text)
            if not self.store.claim_alert_slot(
                key, company or (sig.author or "unknown"), program.value, batch,
                sig.source, status, founder=sig.author, post_url=sig.url,
            ):
                skipped += 1
                continue

            self.store.record_signal(
                sig, company_key=key, claim_type=claim.value, confidence=confidence)
            delivered = self.notifier.send(
                company=company, status=status, program=program.value, batch=batch,
                founder=sig.author, source="X" if sig.source == "x" else "LinkedIn",
                url=sig.url, quote=sig.text[:280],
            )
            if not delivered:
                # Slack rejected it or the network failed. Give the slot back so
                # the next cycle retries, rather than recording a lead as
                # delivered that nobody ever received.
                log.warning("delivery failed for %s; releasing slot for retry", key)
                self.store.release_alert_slot(key)
                skipped += 1
                continue
            alerted += 1
        return f"Rules pass: {alerted} alert(s), {skipped} skipped of {len(candidates)}."

    def _listed(self, company: str) -> bool:
        for name in ("yc_directory", "yc_speedrun"):
            source = self.sources.get(name)
            if source and source.lookup(company):
                return True
        return False

    def _run_model_pass(self, candidates: list, instruction: str | None) -> str:
        self._pending = candidates
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

    def run_directory_pass(self) -> str:
        """Deterministic handling of directory arrivals. Never calls a model.

        A company appearing in YC's own API needs no interpretation, so this
        path runs on every cycle regardless of LLM_MODE and costs nothing.
        """
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

        # First run against an empty store is a backfill, not news: every
        # company YC has ever listed looks "new" to us. Record them silently so
        # the store has a baseline, and alert only on what arrives after that.
        seeding = self.store.company_count() == 0
        if seeding:
            log.info("empty store: seeding baseline, suppressing alerts this pass")

        sent = seeded = 0
        for name in ("yc_directory", "yc_speedrun"):
            source = self.sources.get(name)
            if not source or not source.enabled:
                continue
            # A listing here is already-public news, so it stays silent unless
            # this source is explicitly configured to announce.
            announce = bool(
                self.config.source(name).get("alert_on_new_listing", False))

            collected = source.collect(since)
            self._book_spend(source)
            for sig in collected:
                fields = source.to_alert_fields(sig.raw)
                key = normalize_company(fields["company"] or "")
                if not key:
                    continue

                # Silent path: record the listing as verification baseline
                # WITHOUT claiming an alert slot. claim_alert_slot stamps
                # alerted_at, which is how the rest of the system counts
                # alerts actually sent - using it here would report hundreds
                # of alerts that were deliberately never delivered.
                if seeding or not announce:
                    if self.store.seen_signal(sig.source, sig.external_id):
                        continue
                    self.store.note_company(
                        key, fields["company"], fields["program"],
                        fields["batch"])
                    self.store.record_signal(sig, company_key=key)
                    seeded += 1
                    continue

                if not self.store.claim_alert_slot(
                    key, fields["company"], fields["program"], fields["batch"],
                    name, AlertStatus.CONFIRMED.value, fields.get("website"),
                ):
                    continue
                self.store.record_signal(sig, company_key=key)
                if not self.notifier.send(
                    company=fields["company"], status="confirmed",
                    program=fields["program"], batch=fields["batch"],
                    source="YC Directory" if name == "yc_directory" else "Speedrun",
                    description=fields.get("description"), url=fields.get("url"),
                ):
                    log.warning("delivery failed for %s; releasing slot", key)
                    self.store.release_alert_slot(key)
                    continue
                sent += 1
        if seeded and not sent:
            return (f"Directory pass: {seeded} listing(s) recorded as "
                    "verification baseline, no alerts sent.")
        return f"Directory pass: {sent} confirmed alert(s), {seeded} recorded silently."
