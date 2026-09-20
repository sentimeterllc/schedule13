-- ---------------------------------------------------------------------------
-- Schedule 13D/13G store.
--
-- Two shapes over the same filings, both keyed on the SEC accession number:
--   * a relational model, one row per reporting person per filing;
--   * a flat table, one row per filing with derived headline numbers,
--     their provenance, and the untouched source JSON.
--
-- Every statement is idempotent: this file is executed on every run.
-- ---------------------------------------------------------------------------

-- Issuer: the company whose shares are being reported on.
CREATE TABLE IF NOT EXISTS schedule13_issuers (
    issuer_id   SERIAL PRIMARY KEY,
    cik         VARCHAR       NOT NULL,   -- SEC Central Index Key of the issuer
    name        TEXT          NOT NULL,   -- issuer name as printed on the filing
    ticker      VARCHAR                   -- trading symbol, when the filing carries one
);
-- Natural key: one row per issuer, so repeated loads do not duplicate issuers.
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule13_issuers_cik
    ON schedule13_issuers (cik);

-- Reporting person: an institution, fund or individual that reports a position.
-- The same entity appears across thousands of filings, hence its own table.
CREATE TABLE IF NOT EXISTS schedule13_reporters (
    reporter_id SERIAL PRIMARY KEY,
    cik         VARCHAR,                  -- SEC CIK; NULL for parties filing without one
    name        TEXT          NOT NULL
);
-- NULLs are distinct in PostgreSQL, so CIK-less parties are never collapsed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule13_reporters_cik
    ON schedule13_reporters (cik);
CREATE INDEX IF NOT EXISTS idx_schedule13_reporters_name
    ON schedule13_reporters (name);

-- Filing: one submitted Schedule 13D/13G or amendment.
CREATE TABLE IF NOT EXISTS schedule13_filings (
    filing_id        SERIAL PRIMARY KEY,
    accession_number VARCHAR NOT NULL UNIQUE,  -- SEC accession number, the natural key
    filing_date      DATE    NOT NULL,         -- date the filing was accepted
    form_type        VARCHAR NOT NULL,         -- SC 13D | SC 13D/A | SC 13G | SC 13G/A
    issuer_id        INTEGER REFERENCES schedule13_issuers (issuer_id),
    filed_by_id      INTEGER REFERENCES schedule13_reporters (reporter_id),
    url              TEXT                      -- link to the filing on EDGAR
);
CREATE INDEX IF NOT EXISTS idx_schedule13_filings_issuer_date
    ON schedule13_filings (issuer_id, filing_date DESC);
CREATE INDEX IF NOT EXISTS idx_schedule13_filings_form_date
    ON schedule13_filings (form_type, filing_date DESC);

-- Position: what one reporting person reported in one filing.
-- A group files a single Schedule with a row per member plus, usually, a row
-- for the group as a whole, so these rows overlap by design and must not be
-- summed without deduplication.
CREATE TABLE IF NOT EXISTS schedule13_positions (
    position_id              SERIAL PRIMARY KEY,
    filing_id                INTEGER NOT NULL REFERENCES schedule13_filings (filing_id),
    reporting_owner_id       INTEGER NOT NULL REFERENCES schedule13_reporters (reporter_id),
    percent_outstanding      NUMERIC,   -- percent of the class, as reported
    shares_owned             BIGINT,    -- aggregate beneficial ownership, as reported
    position_dollar          NUMERIC,   -- notional value, when priced by a downstream job
    is_filed_by              BOOLEAN DEFAULT FALSE,  -- this person signed the filing
    ownership_type           VARCHAR,   -- sole / shared, when stated
    reporter_role            TEXT,      -- item 2 type of reporting person (IA, CO, IN, ...)
    notes                    TEXT,
    sole_voting_power        BIGINT,
    shared_voting_power      BIGINT,
    sole_dispositive_power   BIGINT,
    shared_dispositive_power BIGINT
);
-- One position per person per filing: makes the load safely re-runnable.
CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule13_positions_filing_owner
    ON schedule13_positions (filing_id, reporting_owner_id);
CREATE INDEX IF NOT EXISTS idx_schedule13_positions_owner
    ON schedule13_positions (reporting_owner_id);

-- Flat, analysis-ready view of a filing: one row, headline numbers already
-- resolved, each with the field it was taken from. raw_json keeps the original
-- document so a rule change can be replayed without re-fetching.
CREATE TABLE IF NOT EXISTS schedule13_filings_normalized (
    accession_number         TEXT PRIMARY KEY,
    issuer_cusip             TEXT NOT NULL,          -- alphanumeric-only CUSIP
    issuer_name              TEXT NOT NULL,
    ticker                   TEXT,                   -- resolved from the CUSIP, best effort
    form_type                TEXT,
    reporters                TEXT NOT NULL,          -- comma-separated reporting persons
    filers                   TEXT NOT NULL,          -- comma-separated filer entities
    position_shares          NUMERIC,                -- headline position, shares
    position_source          TEXT DEFAULT 'unknown', -- which field it came from
    amount_percent           NUMERIC,                -- headline position, percent of class
    percent_source           TEXT DEFAULT 'unknown', -- which field it came from
    total_shares_outstanding BIGINT,                 -- inferred from shares / percent
    total_shares_source      TEXT DEFAULT 'unknown', -- owners_row | item4 | inconsistent | unknown
    total_shares_owner       TEXT,                   -- the row the inference came from
    purpose_text             TEXT,                   -- concatenated narrative items
    filed_at                 TIMESTAMPTZ,
    raw_json                 JSONB                   -- the filing exactly as received
);
CREATE INDEX IF NOT EXISTS idx_sc13_filings_norm_cusip
    ON schedule13_filings_normalized (issuer_cusip);
CREATE INDEX IF NOT EXISTS idx_sc13_filings_norm_issuer
    ON schedule13_filings_normalized ((lower(issuer_name)));
CREATE INDEX IF NOT EXISTS idx_sc13_filings_norm_ticker
    ON schedule13_filings_normalized (ticker);
CREATE INDEX IF NOT EXISTS idx_sc13_filings_norm_filed_at
    ON schedule13_filings_normalized (filed_at DESC);
