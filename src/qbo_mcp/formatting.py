"""Turning QuickBooks payloads into something an LLM answers well from.

Two jobs:

* aging arithmetic for receivables/payables, computed here from raw invoice
  rows rather than parsed out of Intuit's aging report (whose shape varies and
  whose bucket boundaries we would not control);
* flattening the Reports API's arbitrarily nested Rows/ColData structure into
  indented label/value lines that survive being read by a model.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterator

AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def days_overdue(due_date: str, today: dt.date | None = None) -> int:
    """Days past due; zero or negative means not yet due."""
    today = today or dt.date.today()
    return (today - dt.date.fromisoformat(due_date)).days


def aging_bucket(days: int) -> str:
    """Standard AR aging bucket for a days-overdue count."""
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def build_aging(
    rows: list[dict[str, Any]],
    *,
    party_field: str,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Aggregate open transactions into an aging summary.

    `rows` are raw Invoice or Bill objects with a positive Balance.
    `party_field` is "CustomerRef" for invoices, "VendorRef" for bills.

    Output is shaped for answering "who owes us / whom do we owe, how overdue":
    a flat per-document list (worst first), per-party totals, and per-bucket
    totals that always include every bucket so absence reads as zero, not as
    missing data.
    """
    today = today or dt.date.today()
    documents: list[dict[str, Any]] = []
    by_bucket: dict[str, float] = {bucket: 0.0 for bucket in AGING_BUCKETS}
    by_party: dict[str, dict[str, Any]] = {}

    for row in rows:
        balance = float(row.get("Balance") or 0)
        if balance <= 0:
            continue

        party = (row.get(party_field) or {}).get("name") or "(unknown)"

        # A transaction with no due date is due on receipt, so it ages from its
        # transaction date. Treating it as "current" instead would understate
        # the aging of genuinely old debt. The fallback is flagged so a reader
        # can tell an inferred due date from a recorded one.
        due = row.get("DueDate")
        due_inferred = False
        if not due:
            due = row.get("TxnDate")
            due_inferred = bool(due)

        if due:
            overdue = days_overdue(due, today)
            bucket = aging_bucket(overdue)
        else:
            # Neither date present: nothing to age from.
            overdue = 0
            bucket = "current"

        documents.append(
            {
                "party": party,
                "doc_number": row.get("DocNumber"),
                "txn_date": row.get("TxnDate"),
                "due_date": due,
                "due_date_inferred": due_inferred or None,
                "days_overdue": max(overdue, 0),
                "balance": round(balance, 2),
                "bucket": bucket,
            }
        )

        by_bucket[bucket] = round(by_bucket[bucket] + balance, 2)
        party_entry = by_party.setdefault(
            party, {"total_due": 0.0, "max_days_overdue": 0, "documents": 0}
        )
        party_entry["total_due"] = round(party_entry["total_due"] + balance, 2)
        party_entry["max_days_overdue"] = max(party_entry["max_days_overdue"], overdue, 0)
        party_entry["documents"] += 1

    documents.sort(key=lambda d: (-d["days_overdue"], -d["balance"]))
    parties_ranked = sorted(
        ({"party": name, **data} for name, data in by_party.items()),
        key=lambda p: (-p["max_days_overdue"], -p["total_due"]),
    )

    return {
        "as_of": today.isoformat(),
        "invoice_count": len(documents),
        "totals": {
            "total_due": round(sum(d["balance"] for d in documents), 2),
            "by_bucket": by_bucket,
        },
        "parties": parties_ranked,
        "documents": documents,
    }


# --------------------------------------------------------------------------
# Reports API flattening
# --------------------------------------------------------------------------


def flatten_report(report: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Reports API payload to header info plus indented text lines.

    The native structure nests Rows inside Rows inside Sections, with values
    split across ColData entries. Models mis-read that reliably. Indented
    plain-text lines - the shape of a printed financial statement - they read
    correctly.
    """
    header = report.get("Header") or {}
    columns = _column_titles(report)
    lines = list(_walk_rows((report.get("Rows") or {}).get("Row") or [], depth=0, columns=columns))

    return {
        "report": header.get("ReportName", ""),
        "period": {
            "start": header.get("StartPeriod"),
            "end": header.get("EndPeriod"),
        },
        "currency": header.get("Currency"),
        "accounting_method": _header_option(header, "AccountingMethod")
        or header.get("SummarizeColumnsBy"),
        "no_data": _header_option(header, "NoReportData") == "true",
        "columns": columns,
        "lines": lines,
    }


def _column_titles(report: dict[str, Any]) -> list[str]:
    cols = (report.get("Columns") or {}).get("Column") or []
    return [col.get("ColTitle") or col.get("ColType") or "" for col in cols]


def _header_option(header: dict[str, Any], name: str) -> str | None:
    for option in header.get("Option") or []:
        if option.get("Name") == name:
            return option.get("Value")
    return None


def _walk_rows(rows: list[dict[str, Any]], *, depth: int, columns: list[str]) -> Iterator[str]:
    indent = "  " * depth
    for row in rows:
        # A section: header line, recursive body, then its summary line.
        if "Rows" in row or "Header" in row:
            header_cells = _cells(row.get("Header") or {})
            if header_cells and any(header_cells):
                yield f"{indent}{_join_cells(header_cells)}"
            inner = (row.get("Rows") or {}).get("Row") or []
            yield from _walk_rows(inner, depth=depth + 1, columns=columns)
            summary_cells = _cells(row.get("Summary") or {})
            if summary_cells and any(summary_cells):
                yield f"{indent}{_join_cells(summary_cells)}"
        else:
            cells = _cells(row)
            if cells and any(cells):
                yield f"{indent}{_join_cells(cells)}"


def _cells(row: dict[str, Any]) -> list[str]:
    return [cell.get("value") or "" for cell in row.get("ColData") or []]


def _join_cells(cells: list[str]) -> str:
    label, *values = cells
    if not values or not any(values):
        return label
    return f"{label}: " + "  ".join(v for v in values if v)


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def compact(obj: Any) -> Any:
    """Recursively drop None values and empty containers from tool output."""
    if isinstance(obj, dict):
        cleaned = {k: compact(v) for k, v in obj.items()}
        return {k: v for k, v in cleaned.items() if v is not None and v != {} and v != []}
    if isinstance(obj, list):
        return [compact(item) for item in obj]
    return obj
