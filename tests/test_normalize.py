"""Tests for the derivation rules — the part of the pipeline worth pinning down.

No network, no database: every rule under test is a pure function over a filing
document, so the fixtures below are small hand-written filings.
"""

from __future__ import annotations

import pytest

from schedule13.normalize import (
    clean_cusip,
    derive_total_shares,
    extract_purpose_text,
    names_from_owners,
    normalize_filing,
    owners_max_values,
    to_number,
)


# --------------------------------------------------------------------------- #
# scalars
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value, expected",
    [(None, None), (5, 5.0), ("1,234,567", 1234567.0), ("", None), ("n/a", None), (" 8.58 ", 8.58)],
)
def test_to_number(value, expected):
    assert to_number(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("037833-10-0", "037833100"),
        (["  88160R 10 1 ", "other"], "88160R101"),
        (None, ""),
        ([], ""),
    ],
)
def test_clean_cusip(value, expected):
    assert clean_cusip(value) == expected


def test_names_from_owners_dedupes_case_insensitively_and_keeps_order():
    owners = [
        {"name": ["Blue Harbour Group, LP", "BLUE HARBOUR GROUP, LP"]},
        {"name": "Clifton S. Robbins"},
        {"name": "blue harbour group, lp"},
    ]
    assert names_from_owners(owners) == ["Blue Harbour Group, LP", "Clifton S. Robbins"]


def test_owners_max_takes_the_group_row_not_the_sum():
    owners = [
        {"aggregateAmountOwned": 1_000_000, "amountAsPercent": 6.0},   # fund A
        {"aggregateAmountOwned": 500_000, "amountAsPercent": 3.0},     # fund B
        {"aggregateAmountOwned": 1_500_000, "amountAsPercent": 9.0},   # the group
    ]
    assert owners_max_values(owners) == (1_500_000.0, 9.0)


# --------------------------------------------------------------------------- #
# shares outstanding
# --------------------------------------------------------------------------- #
def test_total_shares_from_consistent_owner_rows():
    owners = [
        {"name": "Fund A", "aggregateAmountOwned": 1_000_000, "amountAsPercent": 10.0},
        {"name": "Fund B", "aggregateAmountOwned": 500_000, "amountAsPercent": 5.0},
    ]
    total, source, owner = derive_total_shares(owners, {})
    assert (total, source) == (10_000_000, "owners_row")
    assert owner in {"Fund A", "Fund B"}


def test_disagreeing_owner_rows_are_flagged_but_still_returned():
    owners = [
        {"name": "Fund A", "aggregateAmountOwned": 1_000_000, "amountAsPercent": 10.0},  # 10m
        {"name": "Fund B", "aggregateAmountOwned": 1_000_000, "amountAsPercent": 5.0},   # 20m
    ]
    total, source, _ = derive_total_shares(owners, {})
    assert source == "inconsistent"
    assert total in (10_000_000, 20_000_000)  # a real row's value, never an average


def test_blocker_rows_are_dropped_when_a_clean_row_exists():
    """A 9.99% conversion blocker must not set the share count."""
    owners = [
        {"name": "Note holder", "aggregateAmountOwned": 1_460_000_000, "amountAsPercent": 9.99},
        {"name": "Fund", "aggregateAmountOwned": 80_000, "amountAsPercent": 10.0},
    ]
    total, source, owner = derive_total_shares(owners, {})
    assert (total, source, owner) == (800_000, "owners_row", "Fund")


def test_blocker_only_filing_is_marked_inconsistent():
    owners = [{"name": "Note holder", "aggregateAmountOwned": 1_460_000_000, "amountAsPercent": 9.99}]
    total, source, _ = derive_total_shares(owners, {})
    assert source == "inconsistent"
    assert total == 14_614_614_615


def test_falls_back_to_item4_when_no_owner_rows_qualify():
    item4 = {"amountBeneficiallyOwned": 2_000_000, "classPercent": 8.0}
    assert derive_total_shares([], item4) == (25_000_000, "item4", None)


def test_unknown_when_the_filing_says_nothing_usable():
    assert derive_total_shares([{"name": "X"}], {}) == (None, "unknown", None)


# --------------------------------------------------------------------------- #
# narrative
# --------------------------------------------------------------------------- #
def test_purpose_text_is_labelled_per_source_item():
    filing = {
        "item3": {"fundsSource": "Working capital."},
        "item4": {"transactionPurpose": "Seek board representation."},
        "item6": {"contractDescription": "   "},
    }
    assert extract_purpose_text(filing) == (
        "[Source of Funds] Working capital.\n\n[Purpose] Seek board representation."
    )


def test_purpose_text_is_none_when_every_item_is_empty():
    assert extract_purpose_text({"item4": {"transactionPurpose": ""}}) is None


# --------------------------------------------------------------------------- #
# the whole row
# --------------------------------------------------------------------------- #
def test_item4_wins_when_populated():
    filing = {
        "accessionNo": "0000000000-26-000001",
        "formType": "SC 13D",
        "nameOfIssuer": "Example Corp",
        "cusip": "30049 A 10 9",
        "filedAt": "2026-03-04T16:31:00-05:00",
        "filers": [{"cik": "0000001", "name": "Example Corp"}, {"cik": "0000002", "name": "Alpha Capital"}],
        "owners": [{"name": "Alpha Capital", "aggregateAmountOwned": 900_000, "amountAsPercent": 7.0}],
        "item4": {"amountBeneficiallyOwned": 1_000_000, "classPercent": 8.58},
    }
    row = normalize_filing(filing, ticker="EXMP")
    assert row["position_shares"] == 1_000_000
    assert row["position_source"] == "item4.amountBeneficiallyOwned"
    assert row["amount_percent"] == 8.58
    assert row["percent_source"] == "item4.classPercent"
    assert row["issuer_cusip"] == "30049A109"
    assert row["ticker"] == "EXMP"
    assert row["form_type"] == "SC 13D"
    assert row["raw_json"] is filing


def test_13g_placeholder_zero_falls_back_to_owner_rows_per_field():
    """A 13G cover page often carries shares=0 next to a real percent."""
    filing = {
        "accessionNo": "0000000000-26-000002",
        "formType": "SC 13G",
        "nameOfIssuer": "Example Corp",
        "cusip": "30049A109",
        "filers": [{"cik": "0000003", "name": "Index Manager LLC"}],
        "owners": [{"name": "Index Manager LLC", "aggregateAmountOwned": 640_000, "amountAsPercent": 5.4}],
        "item4": {"amountBeneficiallyOwned": 0, "classPercent": 5.4},
    }
    row = normalize_filing(filing)
    assert row["position_shares"] == 640_000
    assert row["position_source"] == "owners.aggregateAmountOwned"
    assert row["amount_percent"] == 5.4
    assert row["percent_source"] == "item4.classPercent"
    assert row["ticker"] is None
