"""Static reference for `describe_schema`.

Deliberately a tool rather than an MCP resource: Claude Desktop requires
resources to be attached by hand, so a resource would sit unread and `run_query`
would be a guessing game. This costs no API call -- it is a constant.

Only the fields that matter for read-only reporting are listed. A complete field
dump would be longer than it is useful and would crowd out the model's context.
"""

from __future__ import annotations

DIALECT_NOTES = [
    "The language is a small subset of SQL, not SQL. Unsupported: JOIN, GROUP BY, "
    "OR, HAVING, UNION, aggregate functions, and != / <>.",
    "Operators: = < > <= >= IN LIKE. LIKE takes % as the only wildcard.",
    "String and date literals need single quotes, including numeric-looking ones: "
    "WHERE Balance > '0', WHERE TxnDate >= '2024-01-01'.",
    "Reference fields are objects. Filter on the id (WHERE CustomerRef = '58') but "
    "read the name from CustomerRef.name in the response -- no join needed.",
    "Paging: STARTPOSITION is 1-based, MAXRESULTS caps at 1000 upstream and is "
    "clamped lower here to protect the context window.",
    "SELECT * is usually right. Selecting specific columns is allowed but the "
    "response still nests reference objects.",
    "Deleted records are absent, not flagged. Customers and vendors have an Active "
    "boolean; inactive ones are still returned unless you filter on it.",
]

ENTITIES: dict[str, dict[str, object]] = {
    "Invoice": {
        "what": "Money owed TO the business by a customer (accounts receivable).",
        "key_fields": [
            "Id", "DocNumber", "TxnDate (issued)", "DueDate", "TotalAmt (invoiced)",
            "Balance (still outstanding - 0 means paid)", "CustomerRef {value, name}",
            "CustomerMemo.value", "PrivateNote", "Line[] (detail)",
        ],
        "notes": "Open invoices are Balance > '0'. Prefer get_receivables_aging, "
                 "which does the overdue arithmetic and bucketing for you.",
    },
    "Bill": {
        "what": "Money the business owes a vendor (accounts payable).",
        "key_fields": [
            "Id", "DocNumber", "TxnDate", "DueDate", "TotalAmt", "Balance",
            "VendorRef {value, name}", "APAccountRef",
        ],
        "notes": "Mirror of Invoice. Prefer get_payables_aging.",
    },
    "Customer": {
        "what": "A person or organisation the business sells to.",
        "key_fields": [
            "Id", "DisplayName", "CompanyName", "Balance (total owed across invoices)",
            "Active", "PrimaryEmailAddr.Address", "PrimaryPhone.FreeFormNumber",
            "BillAddr", "Notes",
        ],
        "notes": "Balance here is the customer-level total; per-invoice detail lives "
                 "on Invoice.",
    },
    "Vendor": {
        "what": "A person or organisation the business buys from.",
        "key_fields": [
            "Id", "DisplayName", "CompanyName", "Balance", "Active",
            "PrimaryEmailAddr.Address", "AcctNum",
        ],
    },
    "Payment": {
        "what": "A customer payment received, optionally applied to invoices.",
        "key_fields": [
            "Id", "TxnDate", "TotalAmt", "UnappliedAmt", "CustomerRef",
            "Line[].LinkedTxn[] (which invoices it paid)",
        ],
    },
    "Account": {
        "what": "A chart-of-accounts entry.",
        "key_fields": [
            "Id", "Name", "AccountType", "AccountSubType", "CurrentBalance",
            "Active", "Classification (Asset/Liability/Equity/Revenue/Expense)",
        ],
    },
    "Item": {
        "what": "A product or service that can appear on a transaction line.",
        "key_fields": [
            "Id", "Name", "Type (Inventory/Service/NonInventory)", "UnitPrice",
            "QtyOnHand", "IncomeAccountRef", "Active",
        ],
    },
    "Estimate": {
        "what": "A quote issued to a customer; may convert to an invoice.",
        "key_fields": ["Id", "DocNumber", "TxnDate", "TotalAmt", "TxnStatus", "CustomerRef"],
    },
    "SalesReceipt": {
        "what": "A sale paid at the point of sale (no receivable is created).",
        "key_fields": ["Id", "DocNumber", "TxnDate", "TotalAmt", "CustomerRef"],
    },
    "Purchase": {
        "what": "An expense paid directly by card, cheque or cash.",
        "key_fields": ["Id", "TxnDate", "TotalAmt", "PaymentType", "EntityRef", "AccountRef"],
    },
    "CreditMemo": {
        "what": "A credit issued to a customer, reducing what they owe.",
        "key_fields": ["Id", "DocNumber", "TxnDate", "TotalAmt", "Balance", "CustomerRef"],
    },
    "JournalEntry": {
        "what": "A manual double-entry adjustment.",
        "key_fields": ["Id", "DocNumber", "TxnDate", "Line[].JournalEntryLineDetail"],
    },
    "CompanyInfo": {
        "what": "The company itself: name, address, fiscal year start.",
        "key_fields": [
            "CompanyName", "LegalName", "CompanyAddr", "FiscalYearStartMonth", "Country",
        ],
        "notes": "Always exactly one row: SELECT * FROM CompanyInfo.",
    },
}

REPORTS: dict[str, str] = {
    "ProfitAndLoss": "Income and expenses over a period. Takes start_date/end_date.",
    "BalanceSheet": "Assets, liabilities and equity at a point in time.",
    "CashFlow": "Cash movement over a period.",
    "TrialBalance": "Debit/credit balance per account.",
    "GeneralLedger": "Every transaction per account. Large - always date-bound it.",
    "AgedReceivables": "Receivables summary by aging bucket, per customer.",
    "AgedReceivableDetail": "Per-invoice receivables detail.",
    "AgedPayables": "Payables summary by aging bucket, per vendor.",
    "AgedPayableDetail": "Per-bill payables detail. Note: takes start_duedate/end_duedate.",
    "CustomerBalance": "Outstanding balance per customer.",
    "CustomerSales": "Sales totals per customer.",
    "VendorBalance": "Outstanding balance per vendor.",
    "VendorExpenses": "Spend per vendor.",
    "ItemSales": "Sales totals per product or service.",
    "TransactionList": "Filterable list of transactions.",
    "InventoryValuationSummary": "Quantity and value on hand per inventory item.",
}


def describe(entity: str | None = None) -> dict[str, object]:
    """Reference for one entity, or the full overview when entity is None."""
    if entity:
        for name, spec in ENTITIES.items():
            if name.lower() == entity.lower():
                return {"entity": name, **spec, "dialect_notes": DIALECT_NOTES}
        return {
            "error": f"No reference for {entity!r}.",
            "known_entities": sorted(ENTITIES),
        }

    return {
        "entities": {name: spec["what"] for name, spec in ENTITIES.items()},
        "reports": REPORTS,
        "dialect_notes": DIALECT_NOTES,
        "hint": "Call describe_schema with an entity name for its fields. "
                "For receivables or payables ageing, use the dedicated tools - "
                "they compute days overdue and buckets that a raw query will not.",
    }
