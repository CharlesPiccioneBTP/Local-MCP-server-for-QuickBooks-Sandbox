"""Tool-layer tests against a stub client - no network, no credentials.

Covers the wiring the unit tests miss: that the right query is built, that
pagination terminates, and that filters narrow results without corrupting the
company-wide totals.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from qbo_mcp import tools
from qbo_mcp.tools import PAGE_SIZE


class StubClient:
    """Stands in for QuickBooksReadClient, recording what was asked of it."""

    environment = "sandbox"
    realm_id = "1234567890"

    def __init__(self, pages: list[list[dict[str, Any]]] | None = None, entity: str = "Invoice"):
        self._pages = pages if pages is not None else [[]]
        self._entity = entity
        self.queries: list[str] = []
        self.reports: list[tuple[str, dict[str, Any]]] = []

    async def query(self, statement: str) -> dict[str, Any]:
        self.queries.append(statement)
        index = len(self.queries) - 1
        page = self._pages[index] if index < len(self._pages) else []
        return {self._entity: page}

    async def report(self, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.reports.append((name, dict(params or {})))
        return {
            "Header": {"ReportName": name},
            "Columns": {"Column": [{"ColTitle": ""}, {"ColTitle": "Total"}]},
            "Rows": {"Row": [{"ColData": [{"value": "Net Income"}, {"value": "10.00"}]}]},
        }


def _invoice(name: str, due: str, balance: float, doc: str = "1") -> dict[str, Any]:
    return {
        "DocNumber": doc,
        "TxnDate": "2026-01-01",
        "DueDate": due,
        "Balance": balance,
        "CustomerRef": {"value": "1", "name": name},
    }


async def test_receivables_query_filters_to_open_invoices() -> None:
    client = StubClient([[_invoice("Acme", "2026-08-01", 100.0)]])
    await tools.receivables_aging_summary(client, as_of="2026-08-07")

    assert "FROM Invoice" in client.queries[0]
    # Paid invoices carry Balance 0, so this predicate is what makes it "open".
    assert "Balance > '0'" in client.queries[0]
    assert "STARTPOSITION 1" in client.queries[0]


async def test_pagination_stops_on_short_page() -> None:
    """A full page triggers another fetch; a short one ends the loop."""
    full = [_invoice(f"C{i}", "2026-08-01", 10.0, str(i)) for i in range(PAGE_SIZE)]
    client = StubClient([full, [_invoice("Last", "2026-08-01", 5.0, "x")]])

    result = await tools.receivables_aging_summary(client, as_of="2026-08-07")

    assert len(client.queries) == 2
    assert f"STARTPOSITION {PAGE_SIZE + 1}" in client.queries[1]
    assert result["invoice_count"] == PAGE_SIZE + 1


async def test_pagination_stops_immediately_when_empty() -> None:
    client = StubClient([[]])
    result = await tools.receivables_aging_summary(client, as_of="2026-08-07")
    assert len(client.queries) == 1
    assert result["invoice_count"] == 0
    assert result["totals"]["total_due"] == 0


async def test_min_days_overdue_filter_preserves_company_totals() -> None:
    """Filtering must not make the model think the filtered sum is the total."""
    client = StubClient(
        [[
            _invoice("Old Debt", "2026-01-01", 300.0, "1"),   # 218 days
            _invoice("Recent", "2026-08-05", 50.0, "2"),      # 2 days
        ]]
    )
    result = await tools.receivables_aging_summary(
        client, as_of="2026-08-07", min_days_overdue=60
    )

    assert result["invoice_count"] == 1
    assert result["documents"][0]["party"] == "Old Debt"
    assert result["totals"]["matching_filter"] == 300.0
    assert result["totals"]["total_due"] == 350.0  # unfiltered, still reported


async def test_customer_filter_is_case_insensitive_substring() -> None:
    client = StubClient(
        [[_invoice("Amy's Bird Sanctuary", "2026-08-01", 100.0, "1"),
          _invoice("Cool Cars", "2026-08-01", 50.0, "2")]]
    )
    result = await tools.receivables_aging_summary(
        client, as_of="2026-08-07", customer="amy"
    )
    assert result["invoice_count"] == 1
    assert result["documents"][0]["party"] == "Amy's Bird Sanctuary"


async def test_payables_uses_bill_entity_and_vendor_ref() -> None:
    client = StubClient(
        [[{"DocNumber": "B1", "DueDate": "2026-08-01", "Balance": 75.0,
           "VendorRef": {"name": "Acme Supply"}}]],
        entity="Bill",
    )
    result = await tools.payables_aging_summary(client, as_of="2026-08-07")

    assert "FROM Bill" in client.queries[0]
    assert result["documents"][0]["party"] == "Acme Supply"


async def test_find_contacts_escapes_quotes_in_names() -> None:
    """A name like O'Brien must not break out of the string literal."""
    client = StubClient([[]], entity="Customer")
    await tools.find_contacts(client, name_contains="O'Brien")

    assert "LIKE '%O''Brien%'" in client.queries[0]


async def test_find_contacts_defaults_to_active_only() -> None:
    client = StubClient([[]], entity="Customer")
    await tools.find_contacts(client)
    assert "Active = true" in client.queries[0]

    client = StubClient([[]], entity="Customer")
    await tools.find_contacts(client, include_inactive=True)
    assert "Active = true" not in client.queries[0]


async def test_find_contacts_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="customer.*vendor"):
        await tools.find_contacts(StubClient(), kind="supplier")


async def test_find_contacts_caps_limit() -> None:
    client = StubClient([[]], entity="Customer")
    await tools.find_contacts(client, limit=99999)
    assert f"MAXRESULTS {PAGE_SIZE}" in client.queries[0]


async def test_profit_and_loss_defaults_to_year_to_date() -> None:
    """Without a period QuickBooks would default to the current month."""
    client = StubClient()
    await tools.profit_and_loss(client)

    name, params = client.reports[0]
    assert name == "ProfitAndLoss"
    assert params["date_macro"] == "This Fiscal Year-to-date"


async def test_profit_and_loss_passes_explicit_dates() -> None:
    client = StubClient()
    await tools.profit_and_loss(client, start_date="2026-01-01", end_date="2026-06-30")

    _, params = client.reports[0]
    assert params == {"start_date": "2026-01-01", "end_date": "2026-06-30"}
    assert "date_macro" not in params


async def test_balance_sheet_maps_as_of_to_end_date() -> None:
    client = StubClient()
    await tools.balance_sheet(client, as_of="2026-06-30", accounting_method="cash")

    name, params = client.reports[0]
    assert name == "BalanceSheet"
    assert params == {"end_date": "2026-06-30", "accounting_method": "Cash"}


async def test_company_info_reports_environment() -> None:
    client = StubClient([[{"CompanyName": "Sandbox Co", "Country": "US"}]], entity="CompanyInfo")
    result = await tools.company_info(client)

    assert result["company_name"] == "Sandbox Co"
    assert result["environment"] == "sandbox"
    assert result["realm_id"] == "1234567890"


async def test_as_of_defaults_to_today() -> None:
    client = StubClient([[_invoice("Acme", "2026-01-01", 10.0)]])
    result = await tools.receivables_aging_summary(client)
    assert result["as_of"] == dt.date.today().isoformat()
