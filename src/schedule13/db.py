"""PostgreSQL access: schema management, relational load, normalised upsert.

The store is deliberately two-shaped:

* a **relational model** (``schedule13_issuers`` / ``schedule13_reporters`` /
  ``schedule13_filings`` / ``schedule13_positions``) that keeps one row per
  reporting person per filing, for questions about *who* holds *what*; and
* a **flat table** (``schedule13_filings_normalized``) that keeps one row per
  filing with the derived headline numbers, their provenance and the untouched
  source JSON, for screening and joins.

Both are keyed on the SEC accession number, so loads are idempotent: re-running
a day inserts nothing new and refreshes the normalised row in place.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Json

from . import config

log = logging.getLogger("schedule13.db")

_SCHEMA_SQL = Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


class Database:
    """Thin wrapper over a psycopg connection with this pipeline's statements."""

    def __init__(self, **kwargs: Any) -> None:
        self.conn = psycopg.connect(**(kwargs or config.pg_kwargs()))

    # ----------------------------------------------------------------- #
    # lifecycle
    # ----------------------------------------------------------------- #
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def ensure_schema(self) -> None:
        """Create the tables and indexes if they are missing. Safe to re-run."""
        if not _SCHEMA_SQL.is_file():
            raise FileNotFoundError(f"schema file not found: {_SCHEMA_SQL}")
        with self.conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))
        self.conn.commit()

    # ----------------------------------------------------------------- #
    # calendar / resume helpers
    # ----------------------------------------------------------------- #
    def last_filed_date(self) -> date | None:
        """Latest ``filed_at`` already loaded, used to resume an interrupted run."""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT MAX(filed_at)::date FROM schedule13_filings_normalized")
                row = cur.fetchone()
                return row[0] if row and row[0] else None
        except psycopg.Error:
            self.conn.rollback()
            return None

    def business_dates(self, start: date, end: date) -> list[date]:
        """Trading days in ``[start, end]``, oldest first.

        Filings are published on trading days, so the calendar table drives the
        loop instead of a naive date range that would waste a request per
        weekend and holiday.
        """
        table = config.bizcal_table()
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT bizdate::date FROM {table} "
                "WHERE bizdate >= %s AND bizdate <= %s ORDER BY 1",
                (start, end),
            )
            return [r[0] for r in cur.fetchall()]

    def resolve_ticker(self, cusip: str) -> str | None:
        """Best-effort CUSIP -> ticker lookup against the security master.

        Reference rows store several CUSIPs per security in one space-separated
        column, hence the containment match on the 9-character CUSIP.
        """
        if not cusip:
            return None
        table = config.ticker_lookup_table()
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    f"SELECT ticker FROM {table} WHERE cusip LIKE %s LIMIT 1",
                    (f"%{cusip[:9]}%",),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except psycopg.Error:
            self.conn.rollback()
            return None

    # ----------------------------------------------------------------- #
    # relational load
    # ----------------------------------------------------------------- #
    def _upsert_issuer(self, cik: str | None, name: str | None, ticker: str | None) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedule13_issuers (cik, name, ticker) VALUES (%s, %s, %s) "
                "ON CONFLICT (cik) DO NOTHING RETURNING issuer_id",
                (cik, name, ticker),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("SELECT issuer_id FROM schedule13_issuers WHERE cik = %s", (cik,))
            row = cur.fetchone()
            return row[0] if row else None

    def _upsert_reporter(self, cik: str | None, name: str | None) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedule13_reporters (cik, name) VALUES (%s, %s) "
                "ON CONFLICT (cik) DO NOTHING RETURNING reporter_id",
                (cik, name),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("SELECT reporter_id FROM schedule13_reporters WHERE cik = %s", (cik,))
            row = cur.fetchone()
            return row[0] if row else None

    def insert_filing(self, filing: dict[str, Any]) -> tuple[int | None, bool]:
        """Load one filing into the relational model.

        Returns ``(filing_id, created)``. An accession number already present is
        left untouched — filings are immutable once published, so re-reading a
        day is a no-op rather than a rewrite.
        """
        from .normalize import owner_display_name

        accession = filing.get("accessionNo")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT filing_id FROM schedule13_filings WHERE accession_number = %s",
                (accession,),
            )
            row = cur.fetchone()
        if row:
            return row[0], False

        filers = filing.get("filers") or [{}]
        # The issuer is the first filer block; the reporting party that signed
        # the filing is the second when both are present.
        issuer_cik = filers[0].get("cik")
        filed_by = filers[1] if len(filers) > 1 else filers[0]
        filed_by_cik = filed_by.get("cik")

        issuer_id = self._upsert_issuer(issuer_cik, filing.get("nameOfIssuer"), filing.get("ticker"))
        filed_by_id = self._upsert_reporter(filed_by_cik, filed_by.get("name"))

        filed_at = filing.get("filedAt")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule13_filings
                    (accession_number, filing_date, form_type, issuer_id, filed_by_id, url)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (accession_number) DO NOTHING
                RETURNING filing_id
                """,
                (
                    accession,
                    filed_at[:10] if filed_at else None,
                    filing.get("formType"),
                    issuer_id,
                    filed_by_id,
                    filing.get("linkToFilingDetails"),
                ),
            )
            row = cur.fetchone()
            if row is None:  # another worker inserted it between the check and here
                cur.execute(
                    "SELECT filing_id FROM schedule13_filings WHERE accession_number = %s",
                    (accession,),
                )
                row = cur.fetchone()
            filing_id = row[0] if row else None

        for owner in filing.get("owners", []) or []:
            reporter_id = self._upsert_reporter(owner.get("cik"), owner_display_name(owner))
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO schedule13_positions
                        (filing_id, reporting_owner_id, percent_outstanding, shares_owned,
                         position_dollar, is_filed_by, reporter_role)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (filing_id, reporting_owner_id) DO NOTHING
                    """,
                    (
                        filing_id,
                        reporter_id,
                        owner.get("amountAsPercent"),
                        owner.get("aggregateAmountOwned"),
                        None,
                        owner.get("cik") == filed_by_cik,
                        owner.get("typeOfReportingPerson") or "",
                    ),
                )

        self.conn.commit()
        return filing_id, True

    # ----------------------------------------------------------------- #
    # normalised upsert
    # ----------------------------------------------------------------- #
    def upsert_normalized(self, row: dict[str, Any]) -> None:
        """Insert or refresh the flat row for one filing.

        Unlike the relational load this always rewrites: the derivation rules
        evolve, and re-running the pipeline is how an improved rule reaches rows
        that were loaded under the old one. ``ticker`` is the exception — a
        previously resolved ticker is never overwritten with a NULL.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule13_filings_normalized (
                    accession_number, issuer_cusip, issuer_name, reporters, filers,
                    position_shares, amount_percent, total_shares_outstanding,
                    total_shares_source, total_shares_owner, position_source,
                    percent_source, filed_at, raw_json, form_type, purpose_text, ticker
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (accession_number) DO UPDATE SET
                    issuer_cusip             = EXCLUDED.issuer_cusip,
                    issuer_name              = EXCLUDED.issuer_name,
                    reporters                = EXCLUDED.reporters,
                    filers                   = EXCLUDED.filers,
                    position_shares          = EXCLUDED.position_shares,
                    amount_percent           = EXCLUDED.amount_percent,
                    total_shares_outstanding = EXCLUDED.total_shares_outstanding,
                    total_shares_source      = EXCLUDED.total_shares_source,
                    total_shares_owner       = EXCLUDED.total_shares_owner,
                    position_source          = EXCLUDED.position_source,
                    percent_source           = EXCLUDED.percent_source,
                    filed_at                 = EXCLUDED.filed_at,
                    raw_json                 = EXCLUDED.raw_json,
                    form_type                = EXCLUDED.form_type,
                    purpose_text             = EXCLUDED.purpose_text,
                    ticker                   = COALESCE(EXCLUDED.ticker,
                                                        schedule13_filings_normalized.ticker)
                """,
                (
                    row["accession_number"],
                    row["issuer_cusip"],
                    row["issuer_name"],
                    row["reporters"],
                    row["filers"],
                    row["position_shares"],
                    row["amount_percent"],
                    row["total_shares_outstanding"],
                    row["total_shares_source"],
                    row["total_shares_owner"],
                    row["position_source"],
                    row["percent_source"],
                    row["filed_at"],
                    Json(row["raw_json"]),
                    row.get("form_type"),
                    row.get("purpose_text"),
                    row.get("ticker"),
                ),
            )
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
