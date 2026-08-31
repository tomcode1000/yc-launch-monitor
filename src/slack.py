"""Slack delivery.

The card layout mirrors the two examples in the task brief field for field.
That is deliberate: matching the client's own mock costs nothing and is the
first thing a reviewer compares.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

EARLY_HEADER = ":rotating_light: EARLY YC SIGNAL - Founder Announced Before YC"
CONFIRMED_HEADER = ":white_check_mark: NEW YC COMPANY"

EARLY_STATUS = ":zap: Founder announced / not yet officially announced by YC"
CONFIRMED_STATUS = ":white_check_mark: Confirmed by YC"
# No company name was recoverable from the post, so the directory could not be
# checked. The lead is still actionable - the founder is reachable through the
# link - but the alert must not claim a verification that never happened.
UNVERIFIED_STATUS = (
    ":zap: Founder announced / company not named in post, "
    "so not checked against the YC directory"
)


def _format_detected(when: datetime) -> str:
    """Render the Detected line without platform-specific strftime flags.

    "%-d" strips leading zeros on glibc but raises ValueError on Windows, so
    build the string from the parts instead. The bot has to run on whatever the
    client deploys to.
    """
    hour = when.hour % 12 or 12
    meridiem = "AM" if when.hour < 12 else "PM"
    month = when.strftime("%b")
    return f"{month} {when.day}, {when.year}, {hour}:{when.minute:02d} {meridiem} UTC"


def _fields(pairs: list[tuple[str, Any]]) -> list[dict]:
    return [
        {"type": "mrkdwn", "text": f"*{label}:*\n{value}"}
        for label, value in pairs
        if value
    ]


def build_alert(
    *,
    company: str | None = None,
    status: str = "early",
    program: str = "yc",
    batch: str | None = None,
    founder: str | None = None,
    source: str | None = None,
    description: str | None = None,
    url: str | None = None,
    quote: str | None = None,
    detected_at: datetime | None = None,
) -> tuple[str, list[dict]]:
    """Return (fallback_text, blocks) for one alert."""
    early = status in ("early", "early_unverified")
    header = EARLY_HEADER if early else CONFIRMED_HEADER
    if program == "speedrun":
        header = header.replace("YC", "SPEEDRUN")

    if status == "early_unverified":
        status_line = UNVERIFIED_STATUS
    elif early:
        status_line = EARLY_STATUS
    else:
        status_line = CONFIRMED_STATUS

    # The client acts on the founder, not the company name. When a post does
    # not name a company the lead is still complete - who posted, and where -
    # so the Company row is simply omitted and Founder leads the card.
    detected = _format_detected(detected_at or datetime.now(timezone.utc))

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{header}*"}},
        {
            "type": "section",
            "fields": _fields(
                [
                    ("Company", company),
                    ("Founder", founder),
                    ("Batch", batch),
                    ("Source", source),
                ]
            ),
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Status:* {status_line}",
            },
        },
    ]

    if description:
        blocks.append(
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*Description:* {description}"}}
        )
    if quote:
        quoted = "\n".join(f"> {line}" for line in quote.strip().splitlines())
        blocks.append(
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*Original post:*\n{quoted}"}}
        )
    if url:
        label = "Post link" if early else "YC Profile"
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{label}:* {url}"}}
        )

    blocks.append(
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": f"Detected: {detected}"}]}
    )

    fallback = f"{header} - {company or founder or 'unknown'}" + (
        f" ({batch})" if batch else "")
    return fallback, blocks


class SlackNotifier:
    def __init__(self, token: str | None, channel: str):
        self.channel = channel
        self._client = None
        if token:
            from slack_sdk import WebClient

            self._client = WebClient(token=token)

    @property
    def configured(self) -> bool:
        return self._client is not None

    def send(self, **kwargs) -> bool:
        text, blocks = build_alert(**kwargs)
        if self._client is None:
            # Dry-run mode: no token configured. Log rather than crash so the
            # pipeline can be exercised end-to-end before Slack is wired up.
            log.info("[dry-run] %s", text)
            return False
        try:
            self._client.chat_postMessage(
                channel=self.channel, text=text, blocks=blocks
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("slack delivery failed: %s", exc)
            return False

    def send_digest(self, lines: list[str]) -> None:
        if not lines:
            return
        text = "*YC Monitor - cycle digest*\n" + "\n".join(f"- {ln}" for ln in lines)
        if self._client is None:
            log.info("[dry-run digest] %s", text)
            return
        try:
            self._client.chat_postMessage(channel=self.channel, text=text)
        except Exception as exc:  # noqa: BLE001
            log.error("slack digest failed: %s", exc)
