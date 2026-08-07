"""Validation for the `run_query` escape hatch.

Perspective matters here: the /query endpoint cannot write no matter what is
sent to it, and the GET-only client cannot reach anything else. So this
validator is not a security boundary. Its jobs are:

* produce immediate, specific error messages instead of Intuit's often-opaque
  400s;
* teach the dialect's quirks (no JOIN/OR/GROUP BY) at the moment they bite;
* cap result sizes so a careless query cannot blow out the context window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_RESULTS_CAP = 500

# Entities the /query endpoint accepts (the queryable subset that matters for a
# read-only reporting server; kept in sync with schema.py).
QUERYABLE_ENTITIES = {
    "account", "bill", "billpayment", "budget", "class", "companyinfo",
    "creditmemo", "customer", "department", "deposit", "employee", "estimate",
    "invoice", "item", "journalentry", "payment", "paymentmethod", "preferences",
    "purchase", "purchaseorder", "refundreceipt", "salesreceipt", "taxcode",
    "taxrate", "term", "timeactivity", "transfer", "vendor", "vendorcredit",
}

_SELECT_RE = re.compile(r"^\s*select\s+", re.IGNORECASE)
_FROM_RE = re.compile(r"\bfrom\s+(\w+)", re.IGNORECASE)
_MAXRESULTS_RE = re.compile(r"\bmaxresults\s+(\d+)", re.IGNORECASE)

# Constructs QBO-SQL rejects, mapped to advice that actually helps.
_UNSUPPORTED = [
    (re.compile(r"\bjoin\b", re.IGNORECASE),
     "QuickBooks queries cannot JOIN. Query each entity separately; Invoice rows "
     "already embed CustomerRef.name, which covers the common case."),
    (re.compile(r"\bgroup\s+by\b", re.IGNORECASE),
     "QuickBooks queries cannot GROUP BY. Fetch the rows and aggregate yourself, "
     "or use a report tool (get_profit_and_loss etc.), which is pre-aggregated."),
    (re.compile(r"\bor\b", re.IGNORECASE),
     "QuickBooks queries do not support OR. Use IN ('a','b') for one field, or "
     "run two queries."),
    (re.compile(r"\bhaving\b", re.IGNORECASE), "HAVING is not supported."),
    (re.compile(r"\bunion\b", re.IGNORECASE), "UNION is not supported."),
    (re.compile(r"!=|<>", re.IGNORECASE),
     "Inequality (!= / <>) is not supported. Invert the condition or use IN."),
]

# Anything that even smells like a mutation gets a pointed message. The client
# could not deliver these anyway; failing here just explains why properly.
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|create|alter|truncate|merge|grant|exec)\b",
    re.IGNORECASE,
)


class QueryValidationError(ValueError):
    """A query that should be rewritten, with instructions for doing so."""


@dataclass
class ValidatedQuery:
    statement: str
    entity: str


def validate_select(statement: str) -> ValidatedQuery:
    """Check a QBO-SQL statement and normalise its result cap.

    Returns the statement to send (MAXRESULTS injected or clamped) and the
    entity it reads, or raises QueryValidationError with advice.
    """
    text = statement.strip().rstrip(";").strip()
    if not text:
        raise QueryValidationError("Empty query.")

    if ";" in text:
        raise QueryValidationError(
            "One statement per query - QuickBooks does not accept semicolons."
        )

    write_match = _WRITE_KEYWORDS.search(text)
    if write_match:
        raise QueryValidationError(
            f"{write_match.group(0).upper()} is not possible here: this server is "
            f"read-only by construction (it can only issue GET requests to the "
            f"query and report endpoints). Only SELECT statements are accepted."
        )

    if not _SELECT_RE.match(text):
        raise QueryValidationError(
            "Only SELECT statements are supported, e.g. "
            "SELECT * FROM Invoice WHERE Balance > '0'."
        )

    for pattern, advice in _UNSUPPORTED:
        if pattern.search(_strip_string_literals(text)):
            raise QueryValidationError(advice)

    from_match = _FROM_RE.search(text)
    if not from_match:
        raise QueryValidationError("Missing FROM clause.")
    entity = from_match.group(1)
    if entity.lower() not in QUERYABLE_ENTITIES:
        suggestions = ", ".join(sorted(QUERYABLE_ENTITIES))
        raise QueryValidationError(
            f"{entity!r} is not a queryable entity. Use describe_schema to see "
            f"fields; entities: {suggestions}."
        )

    # Clamp or inject MAXRESULTS so one query cannot flood the model context.
    cap_match = _MAXRESULTS_RE.search(text)
    if cap_match:
        requested = int(cap_match.group(1))
        if requested > MAX_RESULTS_CAP:
            text = _MAXRESULTS_RE.sub(f"MAXRESULTS {MAX_RESULTS_CAP}", text)
    else:
        text = f"{text} MAXRESULTS {MAX_RESULTS_CAP}"

    return ValidatedQuery(statement=text, entity=entity)


def _strip_string_literals(text: str) -> str:
    """Blank out quoted strings so keyword checks skip data values.

    Without this, a customer legitimately named "Orr" or "Grand Union Ltd"
    would trip the OR/UNION checks.
    """
    return re.sub(r"'[^']*'", "''", text)
