"""Command-line entry point: ``python -m schedule13`` / ``schedule13-load``.

Three modes, matching the three things this pipeline is asked to do:

``download_only``
    Pull the filings for each trading day and archive the raw JSON. No database.
``download_parse_save2db``
    The nightly mode: archive, load the relational model, upsert the flat table.
``parse_and_save2db``
    Replay the local archive into the database. Used after a derivation rule
    changes, and to recover a database without touching the API.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime

from . import config
from .db import Database
from .fetch import FilingsApiError, archive_filing, fetch_day
from .normalize import clean_cusip, normalize_filing

log = logging.getLogger("schedule13")

MODES = ("download_only", "download_parse_save2db", "parse_and_save2db")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="schedule13-load",
        description="Download and load SEC Schedule 13D/13G beneficial-ownership filings.",
    )
    parser.add_argument("mode", nargs="?", choices=MODES, default=config.default_mode())
    parser.add_argument(
        "start_date",
        nargs="?",
        help="first day to process, YYYYMMDD. Defaults to the latest filing date already "
             "loaded (re-processed, since loads are idempotent), else today.",
    )
    parser.add_argument("--end-date", help="last day to process, YYYYMMDD (default: today)")
    return parser.parse_args(argv)


def _resolve_start_date(explicit: str | None, db: Database | None) -> date:
    """CLI argument, then environment, then resume point, then today."""
    candidate = explicit or config.start_date()
    if candidate:
        try:
            return datetime.strptime(candidate, "%Y%m%d").date()
        except ValueError as exc:
            raise SystemExit(
                f"invalid start date {candidate!r}; expected YYYYMMDD, e.g. 20260102"
            ) from exc
    if db is not None:
        last = db.last_filed_date()
        if last:
            log.info("resuming from the latest loaded filing date: %s", last)
            return last
    return date.today()


def _load_one(db: Database, filing: dict) -> bool:
    """Relational insert plus normalised upsert. Returns True when newly created."""
    _, created = db.insert_filing(filing)
    cusip = clean_cusip(filing.get("cusip"))
    db.upsert_normalized(normalize_filing(filing, ticker=db.resolve_ticker(cusip)))
    return created


def run_download(day: date, db: Database | None) -> None:
    """Fetch, archive and (in database modes) load one day of filings."""
    day_iso = day.isoformat()
    download_path = config.download_path()
    saved = created = updated = errors = 0

    for page in fetch_day(day_iso):
        for filing in page:
            archive_filing(filing, download_path)
            saved += 1
            if db is None:
                continue
            try:
                if _load_one(db, filing):
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors += 1
                db.rollback()
                log.error("[%s] load failed for %s: %s", day_iso, filing.get("accessionNo"), exc)
            if (created + updated) and (created + updated) % 4 == 0:
                time.sleep(config.filing_pause_seconds())

    log.info(
        "[%s] done — archived %d, inserted %d, refreshed %d, errors %d",
        day_iso, saved, created, updated, errors,
    )


def run_replay(db: Database) -> None:
    """Re-load every archived filing on disk into the database."""
    files = sorted(config.download_path().glob("*.json"))
    log.info("replaying %d archived filings", len(files))
    created = updated = errors = 0

    for index, path in enumerate(files, start=1):
        try:
            filing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors += 1
            log.error("could not read %s: %s", path.name, exc)
            continue
        try:
            if _load_one(db, filing):
                created += 1
            else:
                updated += 1
        except Exception as exc:
            errors += 1
            db.rollback()
            log.error("load failed for %s: %s", path.name, exc)
        if index % 4 == 0:
            time.sleep(config.filing_pause_seconds())

    log.info("replay done — inserted %d, refreshed %d, errors %d", created, updated, errors)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    needs_db = args.mode in ("download_parse_save2db", "parse_and_save2db")
    db = Database() if needs_db else None
    try:
        if db is not None:
            db.ensure_schema()

        if args.mode == "parse_and_save2db":
            run_replay(db)
            return 0

        start = _resolve_start_date(args.start_date, db)
        end = (
            datetime.strptime(args.end_date, "%Y%m%d").date() if args.end_date else date.today()
        )
        if start > end:
            log.info("start date %s is after end date %s — nothing to do", start, end)
            return 0

        log.info("mode=%s  range=%s..%s  archive=%s",
                 args.mode, start, end, config.download_path())

        # Trading days drive the loop; a calendarless deployment falls back to
        # every date in the range.
        days: list[date]
        if db is not None:
            days = db.business_dates(start, end)
            if not days:
                log.info("no trading days between %s and %s", start, end)
                return 0
        else:
            days = [
                date.fromordinal(o)
                for o in range(start.toordinal(), end.toordinal() + 1)
            ]

        failures = 0
        for day in days:
            log.info("=== %s ===", day.isoformat())
            try:
                run_download(day, db)
            except FilingsApiError as exc:
                failures += 1
                log.error("%s", exc)
        return 1 if failures else 0
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
