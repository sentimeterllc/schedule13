"""Turning a filing's JSON into one analysis-ready row.

Schedule 13D/G filings are free-form enough that the same fact appears in two
places with two different values. The rules below are the interesting part of
this project: each derived number is chosen deliberately and shipped alongside a
``*_source`` column, so a consumer can always see *where* a number came from and
filter on that rather than trusting a silent average.

Every function here is pure — no network, no database — which is what makes the
rules testable.
"""

from __future__ import annotations

from typing import Any

#: Conversion-blocker percentages.
#:
#: Convertible notes and warrants routinely carry a contractual cap ("the holder
#: may not convert to the extent it would own more than 9.99% of the class").
#: Filers then report an ``aggregateAmountOwned`` that *includes* the underlying
#: convertible shares, while the percentage is computed on a denominator that
#: excludes them under Rule 13d-3. Dividing one by the other fabricates a share
#: count — one observed filing implies 14.8bn shares outstanding against a real
#: count near 800k. Rows at these percentages are therefore dropped whenever a
#: non-blocker row is available, and flagged as inconsistent when they are all
#: that exists.
BLOCKER_PERCENTS = {4.9, 4.99, 9.9, 9.99}

#: Derived share counts that disagree by more than this ratio are untrustworthy.
TOTAL_SHARES_DISAGREEMENT_RATIO = 1.25


def to_number(value: Any) -> float | None:
    """Coerce a filing value to a float, tolerating ``"1,234,567"`` strings."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).replace(",", "").strip()
        return float(text) if text else None
    except ValueError:
        return None


def clean_cusip(value: Any) -> str:
    """Normalise a CUSIP to bare uppercase alphanumerics.

    Filings carry CUSIPs with spaces, dashes and occasionally as a list of
    several classes; the first non-empty entry wins.
    """
    code = None
    if isinstance(value, list) and value:
        code = next((v for v in value if isinstance(v, str) and v.strip()), None)
    elif isinstance(value, str):
        code = value
    if not code:
        return ""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def owner_display_name(owner: dict[str, Any]) -> str | None:
    """``owners[].name`` is sometimes a string, sometimes a list of aliases."""
    name = owner.get("name")
    if isinstance(name, list) and name:
        return str(name[0])
    if isinstance(name, str):
        return name
    return None


def names_from_owners(owners: list[dict[str, Any]]) -> list[str]:
    """Distinct reporting-person names, in filing order, case-insensitively deduped."""
    out, seen = [], set()
    for owner in owners or []:
        name = owner.get("name")
        candidates = (
            [str(n).strip() for n in name if str(n).strip()]
            if isinstance(name, list)
            else ([name.strip()] if isinstance(name, str) and name.strip() else [])
        )
        for candidate in candidates:
            key = candidate.lower()
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def names_from_filers(filers: list[dict[str, Any]]) -> list[str]:
    """Distinct filer names, in filing order, case-insensitively deduped."""
    out, seen = [], set()
    for filer in filers or []:
        name = filer.get("name")
        if isinstance(name, str) and name.strip():
            key = name.strip().lower()
            if key not in seen:
                seen.add(key)
                out.append(name.strip())
    return out


def owners_max_values(owners: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Largest share count and largest percentage across the reporting persons.

    A group files one Schedule with a row per member plus, usually, a row for the
    group as a whole. The maximum is the group-level position; summing would
    double-count members against their own fund.
    """
    max_shares, max_percent = None, None
    for owner in owners or []:
        shares = to_number(owner.get("aggregateAmountOwned"))
        percent = to_number(owner.get("amountAsPercent"))
        if shares is not None:
            max_shares = shares if max_shares is None else max(max_shares, shares)
        if percent is not None:
            max_percent = percent if max_percent is None else max(max_percent, percent)
    return max_shares, max_percent


def derive_total_shares(
    owners: list[dict[str, Any]], item4: dict[str, Any]
) -> tuple[int | None, str, str | None]:
    """Infer shares outstanding from the position/percentage pairs in the filing.

    A Schedule 13 does not state the issuer's share count, but every reporting
    person implicitly reveals it: ``shares / (percent / 100)``. Each qualifying
    row gives one estimate; the median of the surviving estimates is returned.

    Returns ``(total, source, owner_name)`` where ``source`` is one of:

    ``owners_row``
        Derived from the reporting-person rows and internally consistent.
    ``item4``
        No usable owner rows; derived from the cover-page item 4 totals.
    ``inconsistent``
        A value was derived but should not be trusted — the estimates disagree
        by more than 25%, or every contributing row sat at a conversion-blocker
        percentage. The number is still returned: filtering is the consumer's
        decision, made against this column.
    ``unknown``
        Nothing usable in the filing.
    """
    estimates: list[tuple[int, str | None, bool]] = []
    for owner in owners or []:
        shares = to_number(owner.get("aggregateAmountOwned"))
        percent = to_number(owner.get("amountAsPercent"))
        if shares and percent and shares > 0 and percent > 0:
            estimates.append(
                (
                    int(round(shares / (percent / 100.0))),
                    owner_display_name(owner),
                    percent in BLOCKER_PERCENTS,
                )
            )

    if estimates:
        source = "owners_row"
        non_blocker = [e for e in estimates if not e[2]]
        chosen = non_blocker or estimates
        if not non_blocker:
            source = "inconsistent"

        totals = sorted(total for total, _, _ in chosen)
        # Lower-middle median: always a value some row actually produced, never
        # an average of two disagreeing rows.
        median = totals[len(totals) // 2] if len(totals) % 2 else totals[len(totals) // 2 - 1]
        if max(totals) > min(totals) * TOTAL_SHARES_DISAGREEMENT_RATIO:
            source = "inconsistent"
        owner_name = next((name for total, name, _ in chosen if total == median), None)
        return median, source, owner_name

    if isinstance(item4, dict):
        shares = to_number(item4.get("amountBeneficiallyOwned"))
        percent = to_number(item4.get("classPercent"))
        if shares and percent and shares > 0 and percent > 0:
            source = "inconsistent" if percent in BLOCKER_PERCENTS else "item4"
            return int(round(shares / (percent / 100.0))), source, None

    return None, "unknown", None


def extract_purpose_text(filing: dict[str, Any]) -> str | None:
    """Collect the narrative items into one labelled block of text.

    Intent is the part of a Schedule 13D that matters and the part no numeric
    field captures: whether a holder intends to seek board seats, finance a
    transaction, or is simply passive. The narrative items are concatenated with
    a tag per source so the text stays attributable after extraction.
    """
    sections = [
        ("item1", "commentText", "Comment"),
        ("item3", "fundsSource", "Source of Funds"),
        ("item4", "transactionPurpose", "Purpose"),
        ("item5", "transactionDescription", "Transactions"),
        ("item6", "contractDescription", "Contracts"),
    ]
    parts = []
    for item_key, field, label in sections:
        item = filing.get(item_key) or {}
        value = item.get(field) if isinstance(item, dict) else None
        if isinstance(value, str) and value.strip():
            parts.append(f"[{label}] {value.strip()}")
    return "\n\n".join(parts) if parts else None


def normalize_filing(filing: dict[str, Any], ticker: str | None = None) -> dict[str, Any]:
    """Build the flat ``schedule13_filings_normalized`` row for one filing.

    Position and percentage are taken from the cover-page item 4 when that field
    is present and greater than zero, and from the reporting-person rows
    otherwise. The order matters: 13G cover pages frequently carry a placeholder
    ``amountBeneficiallyOwned`` of 0 next to a real ``classPercent``, so the two
    fields are resolved independently rather than as one block.
    """
    owners = filing.get("owners") or []
    filers = filing.get("filers") or []
    item4 = filing.get("item4") or {}

    owners_shares, owners_percent = owners_max_values(owners)
    item4_shares = to_number(item4.get("amountBeneficiallyOwned"))
    item4_percent = to_number(item4.get("classPercent"))

    if item4_shares is not None and item4_shares > 0:
        position_shares, position_source = item4_shares, "item4.amountBeneficiallyOwned"
    elif owners_shares is not None:
        position_shares, position_source = owners_shares, "owners.aggregateAmountOwned"
    elif item4_shares is not None:
        position_shares, position_source = item4_shares, "item4.amountBeneficiallyOwned"
    else:
        position_shares, position_source = None, "unknown"

    if item4_percent is not None and item4_percent > 0:
        amount_percent, percent_source = item4_percent, "item4.classPercent"
    elif owners_percent is not None:
        amount_percent, percent_source = owners_percent, "owners.amountAsPercent"
    elif item4_percent is not None:
        amount_percent, percent_source = item4_percent, "item4.classPercent"
    else:
        amount_percent, percent_source = None, "unknown"

    total_shares, total_source, total_owner = derive_total_shares(owners, item4)
    issuer_name = filing.get("nameOfIssuer") or (filing.get("item1") or {}).get("issuerName")

    return {
        "accession_number": filing.get("accessionNo"),
        "issuer_cusip": clean_cusip(filing.get("cusip")),
        "issuer_name": issuer_name or "",
        "reporters": ", ".join(names_from_owners(owners)),
        "filers": ", ".join(names_from_filers(filers)),
        "position_shares": position_shares,
        "amount_percent": amount_percent,
        "total_shares_outstanding": total_shares,
        "total_shares_source": total_source,
        "total_shares_owner": total_owner,
        "position_source": position_source,
        "percent_source": percent_source,
        "filed_at": filing.get("filedAt"),
        "raw_json": filing,
        "form_type": filing.get("formType"),
        "purpose_text": extract_purpose_text(filing),
        "ticker": ticker,
    }
