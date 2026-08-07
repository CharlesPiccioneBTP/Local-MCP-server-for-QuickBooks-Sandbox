"""The QuickBooks operations behind the MCP tools.

Kept separate from server.py so each one is an ordinary async function that can
be called from doctor.py or a test without an MCP session. server.py adds the
descriptions and error translation.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .client import QuickBooksReadClient
from .formatting import build_aging, compact, flatten_report

# Upstream hard cap is 1000 per page; stay under it and page explicitly.
PAGE_SIZE = 500
# Ceiling on total rows pulled for one aging call. The sandbox is far smaller
# than this; the cap exists so a large real company cannot hang a tool call.
MAX_PAGES = 20


async def _query_all(client: QuickBooksReadClient, select: str, entity: str) -> list[dict[str, Any]]:
    """Page through a query until exhausted or the page ceiling is hit."""
    rows: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        start = page * PAGE_SIZE + 1  # STARTPOSITION is 1-based.
        statement = f"{select} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
        response = await client.query(statement)
        batch = response.get(entity) or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return rows


async def receivables_aging_summary(
    client: QuickBooksReadClient,
    *,
    as_of: str | None = None,
    min_days_overdue: int = 0,
    customer: str | None = None,
) -> dict[str, Any]:
    """Open invoices, aged. The answer to "who owes us money and how overdue"."""
    today = dt.date.fromisoformat(as_of) if as_of else dt.date.today()

    # Balance > '0' is what "still owed" means; paid invoices carry Balance 0.
    select = "SELECT * FROM Invoice WHERE Balance > '0'"
    rows = await _query_all(client, select, "Invoice")

    result = build_aging(rows, party_field="CustomerRef", today=today)
    return _filter_aging(result, min_days_overdue=min_days_overdue, party=customer)


async def payables_aging_summary(
    client: QuickBooksReadClient,
    *,
    as_of: str | None = None,
    min_days_overdue: int = 0,
    vendor: str | None = None,
) -> dict[str, Any]:
    """Unpaid bills, aged. The mirror of receivables."""
    today = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    rows = await _query_all(client, "SELECT * FROM Bill WHERE Balance > '0'", "Bill")

    result = build_aging(rows, party_field="VendorRef", today=today)
    return _filter_aging(result, min_days_overdue=min_days_overdue, party=vendor)


def _filter_aging(
    result: dict[str, Any], *, min_days_overdue: int, party: str | None
) -> dict[str, Any]:
    """Narrow an aging result, keeping unfiltered totals alongside.

    Both totals are reported: filtering to one customer should not make the
    model think the company's whole receivable is that customer's balance.
    """
    if not min_days_overdue and not party:
        return result

    needle = party.lower() if party else None
    kept = [
        doc
        for doc in result["documents"]
        if doc["days_overdue"] >= min_days_overdue
        and (needle is None or needle in doc["party"].lower())
    ]

    filtered_total = round(sum(doc["balance"] for doc in kept), 2)
    return {
        **result,
        "filter": compact(
            {
                "min_days_overdue": min_days_overdue or None,
                "party_contains": party,
            }
        ),
        "invoice_count": len(kept),
        "documents": kept,
        "parties": [
            p for p in result["parties"] if needle is None or needle in p["party"].lower()
        ],
        "totals": {
            **result["totals"],
            "matching_filter": filtered_total,
        },
    }


async def company_info(client: QuickBooksReadClient) -> dict[str, Any]:
    """Identify the connected company, so answers can be grounded."""
    response = await client.query("SELECT * FROM CompanyInfo")
    info = (response.get("CompanyInfo") or [{}])[0]
    address = info.get("CompanyAddr") or {}
    return compact(
        {
            "company_name": info.get("CompanyName"),
            "legal_name": info.get("LegalName"),
            "country": info.get("Country"),
            "fiscal_year_start_month": info.get("FiscalYearStartMonth"),
            "address": compact(
                {
                    "line1": address.get("Line1"),
                    "city": address.get("City"),
                    "region": address.get("CountrySubDivisionCode"),
                    "postal_code": address.get("PostalCode"),
                }
            ),
            "environment": client.environment,
            "realm_id": client.realm_id,
        }
    )


async def find_contacts(
    client: QuickBooksReadClient,
    *,
    name_contains: str | None = None,
    kind: str = "customer",
    include_inactive: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Look up customers or vendors with their outstanding balances."""
    entity = {"customer": "Customer", "vendor": "Vendor"}.get(kind.lower())
    if entity is None:
        raise ValueError(f"kind must be 'customer' or 'vendor', not {kind!r}")

    clauses = []
    if name_contains:
        # Single quotes are the string delimiter, so double them to escape.
        safe = name_contains.replace("'", "''")
        clauses.append(f"DisplayName LIKE '%{safe}%'")
    if not include_inactive:
        clauses.append("Active = true")

    select = f"SELECT * FROM {entity}"
    if clauses:
        # No OR in this dialect, so AND-joining is the only combination available.
        select += " WHERE " + " AND ".join(clauses)

    capped = max(1, min(limit, PAGE_SIZE))
    response = await client.query(f"{select} MAXRESULTS {capped}")
    rows = response.get(entity) or []

    return {
        "kind": kind.lower(),
        "count": len(rows),
        "contacts": [
            compact(
                {
                    "id": row.get("Id"),
                    "name": row.get("DisplayName"),
                    "company": row.get("CompanyName"),
                    "balance": round(float(row.get("Balance") or 0), 2),
                    "email": (row.get("PrimaryEmailAddr") or {}).get("Address"),
                    "phone": (row.get("PrimaryPhone") or {}).get("FreeFormNumber"),
                    "active": row.get("Active"),
                }
            )
            for row in rows
        ],
    }


async def profit_and_loss(
    client: QuickBooksReadClient,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    accounting_method: str | None = None,
) -> dict[str, Any]:
    """Profit and loss for a period, flattened to readable lines."""
    params: dict[str, Any] = {}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if accounting_method:
        params["accounting_method"] = accounting_method.capitalize()
    if not start_date and not end_date:
        # Without a period QuickBooks defaults to the current month, which is
        # rarely what someone means by "how are we doing".
        params["date_macro"] = "This Fiscal Year-to-date"

    return flatten_report(await client.report("ProfitAndLoss", params))


async def balance_sheet(
    client: QuickBooksReadClient,
    *,
    as_of: str | None = None,
    accounting_method: str | None = None,
) -> dict[str, Any]:
    """Balance sheet at a point in time, flattened to readable lines."""
    params: dict[str, Any] = {}
    if as_of:
        params["end_date"] = as_of
    if accounting_method:
        params["accounting_method"] = accounting_method.capitalize()
    return flatten_report(await client.report("BalanceSheet", params))


async def run_report(
    client: QuickBooksReadClient, *, name: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Fetch any named report, for cases the dedicated tools do not cover."""
    return flatten_report(await client.report(name, params or {}))
