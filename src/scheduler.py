"""The autonomous side: per-source polling, plus the cost governor.

Cadence is per source, not global, because the four sources have wildly
different cost and fragility profiles:

  yc_directory   60s     free public JSON API
  yc_speedrun    5min    free, static page
  x              3min    free, but the endpoint rate-limits by IP, so each
                         tick polls a slice of the watchlist round-robin
  linkedin       6h      metered - about $22/month at this rate, versus
                         roughly $360/day if it ran every minute

The governor exists because of that last row: metered sources stop when the
daily cap trips, free sources carry on, and the next digest says so. A client
who learns about their bill from Slack rather than an invoice stays a client.
"""

from __future__ import annotations

import logging
import threading
import time

from .sources.linkedin import LinkedInSource
from .sources.x_source import XSource
from .sources.yc_directory import YCDirectorySource
from .sources.yc_speedrun import SpeedrunSource

log = logging.getLogger(__name__)


def build_sources(config, store) -> dict:
    """Instantiate every enabled adapter from config."""
    out: dict = {}

    yc_cfg = config.source("yc_directory")
    if yc_cfg.get("enabled", True):
        out["yc_directory"] = YCDirectorySource(
            yc_cfg, store=store, interval_seconds=yc_cfg.get("interval_seconds", 60)
        )

    sr_cfg = config.source("yc_speedrun")
    if sr_cfg.get("enabled", True):
        out["yc_speedrun"] = SpeedrunSource(
            sr_cfg, store=store, interval_seconds=sr_cfg.get("interval_seconds", 300)
        )

    x_cfg = config.source("x")
    if x_cfg.get("enabled", True):
        out["x"] = XSource(
            x_cfg,
            apify_token=config.apify_token,
            keyword_tier=config.x_keyword_tier,
            interval_seconds=x_cfg.get("interval_seconds", 180),
        )

    li_cfg = config.source("linkedin")
    if li_cfg.get("enabled", True):
        out["linkedin"] = LinkedInSource(
            li_cfg,
            apify_token=config.apify_token,
            interval_seconds=li_cfg.get("interval_seconds", 21600),
        )

    return out


class MonitorScheduler:
    """A single background loop that ticks every source on its own interval."""

    TICK_SECONDS = 30

    def __init__(self, config, sources, agent, store, notifier):
        self.config = config
        self.sources = sources
        self.agent = agent
        self.store = store
        self.notifier = notifier
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Seed on first boot so the client's first experience is not a channel
        # flooded with every company already in the directory.
        seeded = self.agent.bootstrap()
        if seeded:
            log.info("seeded %d existing companies; alerting starts from here", seeded)
            self.notifier.send_digest([
                f"Started up. Seeded {seeded} companies already listed - no alerts "
                "sent for these. You will be alerted on new arrivals from now on."
            ])
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("scheduler started (tick=%ss)", self.TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("scheduler tick failed")
            self._stop.wait(self.TICK_SECONDS)

    def tick(self) -> None:
        due = [s for s in self.sources.values() if s.enabled and s.due()]
        if not due:
            return

        # Cost governor: metered sources go dormant once the cap trips.
        capped = self.store.spend_today() >= self.config.daily_spend_cap
        runnable = [s for s in due if not (s.metered and capped)]
        if capped and len(runnable) < len(due):
            log.warning(
                "daily spend cap $%.2f reached; metered sources dormant",
                self.config.daily_spend_cap,
            )

        if not runnable:
            return

        log.info("cycle: %s", ", ".join(s.name for s in runnable))
        summary = self.agent.run_cycle()

        for source in runnable:
            health = source.health()
            self.store.set_health(
                source.name, health.healthy, None if health.healthy else health.detail
            )
        log.info("cycle done: %s", (summary or "")[:200])

    def run_once(self) -> str:
        """Single synchronous pass. Used by /admin/scan and the CLI."""
        return self.agent.run_cycle()
