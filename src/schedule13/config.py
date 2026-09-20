"""Configuration, loaded from the environment (optionally via a .env file)."""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional at runtime
        return
    env_file = Path(os.getenv("SCHEDULE13_ENV", _REPO_ROOT / ".env"))
    if env_file.is_file():
        load_dotenv(env_file)


_load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or export {name} in the environment."
        )
    return value


# --------------------------------------------------------------------------- #
# Filings API
# --------------------------------------------------------------------------- #
def api_key() -> str:
    """API key for the SEC filings provider."""
    return _require("SECAPI_KEY")


def api_url() -> str:
    """Endpoint serving structured Schedule 13D/13G filings."""
    return os.getenv("SECAPI_13DG_URL", "https://api.sec-api.io/form-13d-13g")


def page_size() -> int:
    """Filings requested per page. The provider caps this at 50."""
    return int(os.getenv("SCHEDULE13_PAGE_SIZE", "50"))


def query_pause_seconds() -> float:
    """Pause between pages, to stay inside the provider's rate limit."""
    return float(os.getenv("SCHEDULE13_QUERY_PAUSE_SEC", "2"))


def filing_pause_seconds() -> float:
    """Pause every few filings, to keep the database write rate civil."""
    return float(os.getenv("SCHEDULE13_FILING_PAUSE_SEC", "0.4"))


def retry_delay_seconds() -> float:
    """Backoff after a failed page request."""
    return float(os.getenv("SCHEDULE13_RETRY_DELAY_SEC", "5"))


def max_retries() -> int:
    """Consecutive failures tolerated on one page before the day is abandoned."""
    return int(os.getenv("SCHEDULE13_MAX_RETRIES", "5"))


def http_timeout() -> float:
    return float(os.getenv("SCHEDULE13_HTTP_TIMEOUT", "60"))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def download_path() -> Path:
    """Archive directory for raw filing JSON, one file per accession number."""
    path = Path(os.getenv("DOWNLOAD_PATH", "./data/schedule13/downloads"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_mode() -> str:
    """Pipeline mode when none is given on the command line."""
    return os.getenv("MODE", "download_only").lower()


def start_date() -> str | None:
    """Optional ``YYYYMMDD`` start date override."""
    return os.getenv("SCHEDULE13_START_DATE") or None


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #
def pg_kwargs() -> dict:
    """Keyword arguments for :func:`psycopg.connect`."""
    return {
        "host": _require("PGHOST"),
        "port": os.getenv("PGPORT", "5432"),
        "user": _require("PGUSER"),
        "password": os.getenv("PGPASSWORD"),
        "dbname": _require("PGDATABASE"),
    }


def bizcal_table() -> str:
    """Trading-calendar table used to enumerate filing days."""
    return os.getenv("SCHEDULE13_BIZCAL_TABLE", "bizcal")


def ticker_lookup_table() -> str:
    """Security-reference table used to resolve a CUSIP to a ticker."""
    return os.getenv("SCHEDULE13_TICKER_TABLE", "sec_info")
