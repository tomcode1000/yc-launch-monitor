"""Configuration: config.yml for behaviour, environment for secrets."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Use the OS certificate store rather than certifi's bundle. Machines running
# TLS-inspecting antivirus or a corporate proxy present a private root CA that
# certifi does not carry, which otherwise fails every outbound HTTPS call.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # optional; certifi's bundle is fine without interception
    pass

ROOT = Path(__file__).resolve().parent.parent

# Secrets live in .env next to config.yml. Load it before anything reads
# os.environ; real environment variables always win over the file.
load_dotenv(ROOT / ".env", override=False)

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.yml"))


class Config:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def source(self, name: str) -> dict[str, Any]:
        return self._data.get("sources", {}).get(name, {})

    # --- secrets -------------------------------------------------------
    @property
    def anthropic_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def slack_token(self) -> str | None:
        return os.environ.get("SLACK_BOT_TOKEN")

    @property
    def slack_channel(self) -> str:
        return os.environ.get("SLACK_CHANNEL", "#yc-alerts")

    @property
    def pond_access_key(self) -> str | None:
        return os.environ.get("POND_ACCESS_KEY")

    @property
    def apify_token(self) -> str | None:
        return os.environ.get("APIFY_TOKEN") or None

    @property
    def x_keyword_tier(self) -> str:
        return os.environ.get("X_KEYWORD_TIER", "off").lower()

    @property
    def db_path(self) -> Path:
        p = Path(os.environ.get("DATABASE_PATH", ROOT / "data" / "monitor.db"))
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def daily_spend_cap(self) -> float:
        return float(os.environ.get("DAILY_SPEND_CAP_USD", "2.00"))


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    with open(path, encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh))
