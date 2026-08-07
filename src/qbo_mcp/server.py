"""MCP server exposing a QuickBooks company read-only.

Tool descriptions are load-bearing: they are the only thing the model sees when
choosing between eight options, so each one states what the tool answers in the
words someone would actually use, and says when to prefer a different tool.

The QuickBooks client is built lazily rather than in the lifespan. If it were
built at startup, running setup after Claude Desktop had already launched would
leave a dead server until restart, and a missing-credentials error would surface
as "server failed to start" instead of a sentence telling you what to run.

On errors: tools here raise ordinary exceptions and let them propagate. In this
SDK a plain exception becomes a *tool error* -- the message lands in the tool
result, where the model reads it and can relay it to the user. `MCPError` would
instead produce a protocol error the model never sees, turning "no credentials
found, run qbo-mcp-setup" into a silent failure. Every failure mode in this
server is one the user needs told about, so none of them is an MCPError.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import schema, tools
from . import __version__
from .auth import TokenProvider
from .client import QuickBooksReadClient
from .config import load_credentials
from .formatting import compact
from .qbo_sql import validate_select

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    """Holds the one client, created on first use and reused after."""

    client: QuickBooksReadClient | None = field(default=None)

    async def quickbooks(self) -> QuickBooksReadClient:
        if self.client is None:
            creds = load_credentials()
            self.client = QuickBooksReadClient(
                TokenProvider(creds),
                environment=creds.environment,
                realm_id=creds.realm_id,
            )
        return self.client

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    context = AppContext()
    try:
        yield context
    finally:
        await context.aclose()


mcp = MCPServer(
    "quickbooks",
    title="QuickBooks (read-only)",
    version=__version__,
    instructions=(
        "Read-only access to one QuickBooks Online company. This server can only "
        "read: it has no ability to create, change or delete anything in "
        "QuickBooks, so never tell the user you have modified their books. "
        "For questions about unpaid customer invoices or overdue accounts use "
        "get_receivables_aging, which computes days overdue and aging buckets; "
        "for money owed to suppliers use get_payables_aging. Amounts are in the "
        "company's home currency (check get_company_info if it matters)."
    ),
    lifespan=lifespan,
)


async def _client(ctx: Context[AppContext]) -> QuickBooksReadClient:
    """Resolve the QuickBooks client for this request."""
    return await ctx.request_context.lifespan_context.quickbooks()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
async def get_receivables_aging(
    ctx: Context[AppContext],
    as_of: str | None = None,
    min_days_overdue: int = 0,
    customer: str | None = None,
) -> dict[str, Any]:
    """Who owes the business money, and how overdue each amount is.

    Use this for any question about unpaid customer invoices, accounts
    receivable, overdue accounts, collections, or who is behind on payment.
    Returns every open invoice with the customer name, due date, days overdue
    and outstanding balance; per-customer totals ranked worst-first; and totals
    per aging bucket (current, 1-30, 31-60, 61-90, 90+ days).

    Args:
        as_of: Date to age against as YYYY-MM-DD. Defaults to today.
        min_days_overdue: Only include invoices at least this many days late.
            0 (the default) includes invoices that are not yet due.
        customer: Case-insensitive substring to limit results to one customer.
    """
    client = await _client(ctx)
    return compact(
        await tools.receivables_aging_summary(
            client, as_of=as_of, min_days_overdue=min_days_overdue, customer=customer
        )
    )


@mcp.tool()
async def get_payables_aging(
    ctx: Context[AppContext],
    as_of: str | None = None,
    min_days_overdue: int = 0,
    vendor: str | None = None,
) -> dict[str, Any]:
    """Who the business owes money to, and how overdue each bill is.

    The accounts-payable mirror of get_receivables_aging: use it for unpaid
    bills, money owed to suppliers or vendors, and upcoming payment
    obligations. Same shape of answer, aged into the same buckets.

    Args:
        as_of: Date to age against as YYYY-MM-DD. Defaults to today.
        min_days_overdue: Only include bills at least this many days late.
        vendor: Case-insensitive substring to limit results to one vendor.
    """
    client = await _client(ctx)
    return compact(
        await tools.payables_aging_summary(
            client, as_of=as_of, min_days_overdue=min_days_overdue, vendor=vendor
        )
    )


@mcp.tool()
async def get_profit_and_loss(
    ctx: Context[AppContext],
    start_date: str | None = None,
    end_date: str | None = None,
    accounting_method: str | None = None,
) -> dict[str, Any]:
    """Income, expenses and net profit over a period.

    Use for questions about revenue, sales totals, expenses, spending, margins
    or whether the business is profitable. Defaults to the current fiscal year
    to date when no dates are given.

    Args:
        start_date: Period start, YYYY-MM-DD.
        end_date: Period end, YYYY-MM-DD.
        accounting_method: "Accrual" or "Cash". Defaults to the company setting.
    """
    client = await _client(ctx)
    return await tools.profit_and_loss(
        client,
        start_date=start_date,
        end_date=end_date,
        accounting_method=accounting_method,
    )


@mcp.tool()
async def get_balance_sheet(
    ctx: Context[AppContext],
    as_of: str | None = None,
    accounting_method: str | None = None,
) -> dict[str, Any]:
    """Assets, liabilities and equity at a point in time.

    Use for questions about what the business owns and owes overall, cash
    position, or financial standing. For "who owes us money" specifically,
    get_receivables_aging gives a far more useful breakdown than the single
    receivables line on this report.

    Args:
        as_of: Balance sheet date, YYYY-MM-DD. Defaults to today.
        accounting_method: "Accrual" or "Cash". Defaults to the company setting.
    """
    client = await _client(ctx)
    return await tools.balance_sheet(
        client, as_of=as_of, accounting_method=accounting_method
    )


@mcp.tool()
async def find_contacts(
    ctx: Context[AppContext],
    name_contains: str | None = None,
    kind: str = "customer",
    include_inactive: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Look up customers or vendors, with contact details and balances.

    Use to find someone by partial name, to list who the business deals with,
    or to get an email or phone number. The balance returned is that contact's
    total outstanding; for invoice-level detail and overdue days use
    get_receivables_aging.

    Args:
        name_contains: Case-insensitive partial name. Omit to list everyone.
        kind: "customer" or "vendor".
        include_inactive: Include archived records. Defaults to active only.
        limit: Maximum records to return (capped at 500).
    """
    client = await _client(ctx)
    return await tools.find_contacts(
        client,
        name_contains=name_contains,
        kind=kind,
        include_inactive=include_inactive,
        limit=limit,
    )


@mcp.tool()
async def get_company_info(ctx: Context[AppContext]) -> dict[str, Any]:
    """Name, address and fiscal year of the connected QuickBooks company.

    Useful for confirming which company the answers refer to, and for finding
    the fiscal year start before asking for period reports.
    """
    client = await _client(ctx)
    return await tools.company_info(client)


@mcp.tool()
async def run_query(ctx: Context[AppContext], query: str) -> dict[str, Any]:
    """Run a read-only QuickBooks query for anything the other tools miss.

    Only SELECT statements are accepted, and this server can only issue read
    requests, so no query can modify data. The language is a restricted subset
    of SQL: no JOIN, GROUP BY, OR, or aggregate functions. Call describe_schema
    first to check entity and field names, and prefer the purpose-built tools
    where one fits -- they do arithmetic this cannot.

    Example: SELECT * FROM Invoice WHERE TxnDate >= '2026-01-01'

    Args:
        query: A QuickBooks SELECT statement.
    """
    # Validate before resolving the client. A malformed or write-shaped query is
    # rejectable without credentials, and the answer should not depend on
    # whether setup has been run.
    validated = validate_select(query)

    client = await _client(ctx)
    response = await client.query(validated.statement)
    rows = response.get(validated.entity) or []
    return {
        "entity": validated.entity,
        "query": validated.statement,
        "count": len(rows),
        "rows": rows,
    }


@mcp.tool()
async def describe_schema(entity: str | None = None) -> dict[str, Any]:
    """Which QuickBooks entities and fields can be queried, and the dialect rules.

    Call this before writing a run_query statement. With no argument it lists
    every queryable entity and available report; with an entity name it returns
    that entity's key fields. Costs no API call.

    Args:
        entity: Entity name such as "Invoice" or "Customer". Omit for the overview.
    """
    return schema.describe(entity)


def main() -> int:
    """Entry point for `uv run qbo-mcp`."""
    # stdio carries the protocol, so logs must go to stderr or they corrupt it.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
