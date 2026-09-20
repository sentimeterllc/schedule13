"""SEC Schedule 13D / 13G beneficial-ownership filing pipeline.

Pulls Schedule 13D and 13G filings (and their amendments) day by day from a
filings API, archives the raw JSON, loads a normalised relational model into
PostgreSQL and derives an analysis-ready flat table with explicit provenance for
every derived number.
"""

__version__ = "1.0.0"
__all__ = ["config", "db", "fetch", "normalize"]
