"""The adapter contract.

Adding a platform is one subclass plus a config entry. Nothing in the
scheduler, the agent, or the alerting path knows how any individual source
works - they all speak RawSignal.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..models import RawSignal

log = logging.getLogger(__name__)


@dataclass
class SourceHealth:
    name: str
    healthy: bool
    detail: str = ""
    last_run_at: float | None = None


class Source(ABC):
    """One monitored platform.

    Subclasses set `name`, declare their own polling interval, and flag
    themselves `metered` if a call costs money. The scheduler reads those
    attributes; it never special-cases a particular source.
    """

    name: str = "base"
    metered: bool = False

    def __init__(self, config: dict, interval_seconds: int | None = None):
        self.config = config or {}
        self.interval_seconds = int(
            interval_seconds or self.config.get("interval_seconds", 3600)
        )
        self._last_run: float | None = None
        self._health = SourceHealth(self.name, True)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def due(self, now: float | None = None) -> bool:
        now = now or time.time()
        if self._last_run is None:
            return True
        return (now - self._last_run) >= self.interval_seconds

    @abstractmethod
    def fetch(self, since: datetime) -> list[RawSignal]:
        """Collect signals newer than `since`. Must not raise."""

    def collect(self, since: datetime) -> list[RawSignal]:
        """fetch() wrapped so one broken source never stops a cycle.

        A degraded source is reported, not fatal: the free tiers keep producing
        alerts while a fragile one is down.
        """
        self._last_run = time.time()
        try:
            signals = self.fetch(since)
            self._health = SourceHealth(self.name, True, f"{len(signals)} signals", self._last_run)
            return signals
        except Exception as exc:  # noqa: BLE001 - deliberate: degrade, don't crash
            log.warning("source %s degraded: %s", self.name, exc)
            self._health = SourceHealth(self.name, False, str(exc)[:200], self._last_run)
            return []

    def health(self) -> SourceHealth:
        return self._health

    def estimated_cost(self, item_count: int) -> float:
        """USD estimate for a run of this size. Free sources return 0."""
        return 0.0
