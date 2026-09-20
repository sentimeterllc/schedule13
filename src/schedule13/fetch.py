"""Client for the Schedule 13D/13G filings API.

One day at a time, paged. A day is the natural unit here: the SEC publishes on
trading days, the API's date filter is inclusive on both ends, and a day-sized
request is small enough to retry cheaply and re-run idempotently.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from . import config

log = logging.getLogger("schedule13.fetch")

#: The query sent to the provider. ``13D`` and ``13G`` cover the originals and
#: their amendments (``SC 13D/A``, ``SC 13G/A``) because the provider matches on
#: the form-type prefix.
QUERY_TEMPLATE = "formType:(13D OR 13G) AND filedAt:[{day} TO {day}]"


class FilingsApiError(RuntimeError):
    """Raised when a day cannot be retrieved after the configured retries."""


def fetch_day(day_iso: str) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages of filings for one calendar day.

    Transient failures are retried with a fixed backoff; a page that keeps
    failing aborts the day rather than looping forever, so a scheduled run can
    surface the problem instead of silently spinning.
    """
    url = config.api_url()
    headers = {"Authorization": config.api_key()}
    size = config.page_size()
    payload = {
        "query": QUERY_TEMPLATE.format(day=day_iso),
        "from": "0",
        "size": str(size),
        "sort": [{"filedAt": {"order": "desc"}}],
    }

    offset = 0
    failures = 0
    while True:
        payload["from"] = str(offset)
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=config.http_timeout()
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            failures += 1
            if failures >= config.max_retries():
                raise FilingsApiError(
                    f"{day_iso}: giving up after {failures} consecutive failures at offset {offset}"
                ) from exc
            log.warning(
                "[%s] request failed at offset %d (%s); retry %d/%d",
                day_iso, offset, exc, failures, config.max_retries(),
            )
            time.sleep(config.retry_delay_seconds())
            continue

        failures = 0
        filings = data.get("filings", []) or []
        total = (data.get("total") or {}).get("value")
        log.info(
            "[%s] page at offset %d: %d filings%s",
            day_iso, offset, len(filings), f" (reported total {total})" if total is not None else "",
        )
        if filings:
            yield filings

        if len(filings) < size:
            return
        offset += size
        time.sleep(config.query_pause_seconds())


def archive_filing(filing: dict[str, Any], download_path: Path) -> Path:
    """Write the untouched filing JSON to ``<accession number>.json``.

    The raw archive is written before anything is parsed, so the derivation
    rules can be changed later and replayed over local files without re-paying
    for the API.
    """
    path = download_path / f"{filing.get('accessionNo')}.json"
    path.write_text(json.dumps(filing, indent=2), encoding="utf-8")
    return path
