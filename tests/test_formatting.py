"""Tests for aging arithmetic and report flattening."""

from __future__ import annotations

import datetime as dt

import pytest

from qbo_mcp.formatting import (
    AGING_BUCKETS,
    aging_bucket,
    build_aging,
    compact,
    days_overdue,
    flatten_report,
)

TODAY = dt.date(2026, 8, 7)


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (-5, "current"), (0, "current"),      # not yet due
        (1, "1-30"), (30, "1-30"),            # boundary pair
        (31, "31-60"), (60, "31-60"),
        (61, "61-90"), (90, "61-90"),
        (91, "90+"), (400, "90+"),
    ],
)
def test_bucket_boundaries(days: int, expected: str) -> None:
    """Off-by-one here silently misstates every aging report."""
    assert aging_bucket(days) == expected


def test_days_overdue_counts_from_due_date() -> None:
    assert days_overdue("2026-08-01", TODAY) == 6
    assert days_overdue("2026-08-07", TODAY) == 0
    assert days_overdue("2026-08-20", TODAY) == -13


def _invoice(name: str, due: str, balance: float, doc: str = "1001") -> dict:
    return {
        "DocNumber": doc,
        "TxnDate": "2026-06-01",
        "DueDate": due,
        "Balance": balance,
        "TotalAmt": balance,
        "CustomerRef": {"value": "1", "name": name},
    }


def test_build_aging_totals_and_ranking() -> None:
    rows = [
        _invoice("Amy's Bird Sanctuary", "2026-08-01", 100.0, "1001"),   # 6 days
        _invoice("Bill's Windsurf Shop", "2026-01-01", 250.0, "1002"),   # 218 days
        _invoice("Cool Cars", "2026-09-01", 75.0, "1003"),               # not due
    ]
    result = build_aging(rows, party_field="CustomerRef", today=TODAY)

    assert result["invoice_count"] == 3
    assert result["totals"]["total_due"] == 425.0
    assert result["totals"]["by_bucket"]["current"] == 75.0
    assert result["totals"]["by_bucket"]["1-30"] == 100.0
    assert result["totals"]["by_bucket"]["90+"] == 250.0

    # Most overdue first - that is the answer to "how overdue is it".
    assert result["documents"][0]["party"] == "Bill's Windsurf Shop"
    assert result["documents"][0]["days_overdue"] == 218
    assert result["parties"][0]["party"] == "Bill's Windsurf Shop"


def test_every_bucket_is_present_even_when_empty() -> None:
    """A missing bucket reads as missing data; zero reads as zero."""
    result = build_aging([_invoice("Solo", "2026-08-01", 10.0)], party_field="CustomerRef",
                         today=TODAY)
    assert set(result["totals"]["by_bucket"]) == set(AGING_BUCKETS)
    assert result["totals"]["by_bucket"]["90+"] == 0.0


def test_paid_and_credit_rows_are_excluded() -> None:
    rows = [
        _invoice("Paid Co", "2026-01-01", 0.0),
        _invoice("Credit Co", "2026-01-01", -50.0),
        _invoice("Owes Co", "2026-01-01", 20.0),
    ]
    result = build_aging(rows, party_field="CustomerRef", today=TODAY)
    assert result["invoice_count"] == 1
    assert result["totals"]["total_due"] == 20.0


def test_party_totals_aggregate_across_documents() -> None:
    rows = [
        _invoice("Repeat Co", "2026-07-01", 100.0, "1"),   # 37 days
        _invoice("Repeat Co", "2026-08-05", 50.0, "2"),    # 2 days
    ]
    result = build_aging(rows, party_field="CustomerRef", today=TODAY)
    assert len(result["parties"]) == 1
    assert result["parties"][0]["total_due"] == 150.0
    assert result["parties"][0]["max_days_overdue"] == 37
    assert result["parties"][0]["documents"] == 2


def test_missing_due_date_ages_from_txn_date() -> None:
    """Due on receipt. Calling old debt "current" would understate the aging."""
    row = {"Balance": 10.0, "TxnDate": "2026-06-01", "CustomerRef": {"name": "No Due"}}
    doc = build_aging([row], party_field="CustomerRef", today=TODAY)["documents"][0]
    assert doc["days_overdue"] == 67
    assert doc["bucket"] == "61-90"
    assert doc["due_date_inferred"] is True


def test_recorded_due_date_is_not_flagged_as_inferred() -> None:
    doc = build_aging(
        [_invoice("Has Due", "2026-08-01", 10.0)], party_field="CustomerRef", today=TODAY
    )["documents"][0]
    assert doc["due_date_inferred"] is None


def test_no_dates_at_all_is_current() -> None:
    row = {"Balance": 10.0, "CustomerRef": {"name": "Dateless"}}
    doc = build_aging([row], party_field="CustomerRef", today=TODAY)["documents"][0]
    assert doc["bucket"] == "current"
    assert doc["days_overdue"] == 0


def test_vendor_field_drives_payables() -> None:
    row = {"Balance": 42.0, "DueDate": "2026-08-01", "VendorRef": {"name": "Acme Supply"}}
    result = build_aging([row], party_field="VendorRef", today=TODAY)
    assert result["documents"][0]["party"] == "Acme Supply"


# --- report flattening -----------------------------------------------------

NESTED_REPORT = {
    "Header": {
        "ReportName": "ProfitAndLoss",
        "StartPeriod": "2026-01-01",
        "EndPeriod": "2026-12-31",
        "Currency": "USD",
        "Option": [{"Name": "AccountingMethod", "Value": "Accrual"}],
    },
    "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Total"}]},
    "Rows": {
        "Row": [
            {
                "Header": {"ColData": [{"value": "Income"}, {"value": ""}]},
                "Rows": {
                    "Row": [
                        {"ColData": [{"value": "Sales"}, {"value": "1500.00"}]},
                        {"ColData": [{"value": "Services"}, {"value": "500.00"}]},
                    ]
                },
                "Summary": {"ColData": [{"value": "Total Income"}, {"value": "2000.00"}]},
            },
            {"ColData": [{"value": "Net Income"}, {"value": "2000.00"}]},
        ]
    },
}


def test_flatten_report_extracts_header_metadata() -> None:
    result = flatten_report(NESTED_REPORT)
    assert result["report"] == "ProfitAndLoss"
    assert result["period"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert result["accounting_method"] == "Accrual"
    assert result["no_data"] is False


def test_flatten_report_indents_nested_sections() -> None:
    lines = flatten_report(NESTED_REPORT)["lines"]
    assert lines == [
        "Income",
        "  Sales: 1500.00",
        "  Services: 500.00",
        "Total Income: 2000.00",
        "Net Income: 2000.00",
    ]


def test_flatten_report_handles_empty_report() -> None:
    empty = {
        "Header": {"ReportName": "BalanceSheet", "Option": [{"Name": "NoReportData", "Value": "true"}]},
        "Columns": {"Column": []},
        "Rows": {},
    }
    result = flatten_report(empty)
    assert result["no_data"] is True
    assert result["lines"] == []


def test_compact_drops_empties_but_keeps_zero() -> None:
    """Zero is a meaningful balance and must survive compaction."""
    assert compact({"a": None, "b": [], "c": {}, "d": 0, "e": 0.0, "f": "x"}) == {
        "d": 0,
        "e": 0.0,
        "f": "x",
    }
