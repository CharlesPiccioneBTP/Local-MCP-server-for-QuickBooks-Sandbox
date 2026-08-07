"""Tests for the run_query validator."""

from __future__ import annotations

import pytest

from qbo_mcp.qbo_sql import MAX_RESULTS_CAP, QueryValidationError, validate_select


def test_accepts_plain_select() -> None:
    result = validate_select("SELECT * FROM Invoice")
    assert result.entity == "Invoice"
    assert f"MAXRESULTS {MAX_RESULTS_CAP}" in result.statement


def test_injects_result_cap_when_absent() -> None:
    assert validate_select("SELECT * FROM Customer").statement.endswith(
        f"MAXRESULTS {MAX_RESULTS_CAP}"
    )


def test_clamps_oversized_result_cap() -> None:
    result = validate_select("SELECT * FROM Invoice MAXRESULTS 1000")
    assert f"MAXRESULTS {MAX_RESULTS_CAP}" in result.statement
    assert "1000" not in result.statement


def test_preserves_smaller_result_cap() -> None:
    assert "MAXRESULTS 10" in validate_select("SELECT * FROM Invoice MAXRESULTS 10").statement


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE Invoice SET Balance = 0",
        "DELETE FROM Customer",
        "INSERT INTO Invoice (Id) VALUES (1)",
        "DROP TABLE Invoice",
    ],
)
def test_rejects_mutations_with_read_only_explanation(statement: str) -> None:
    with pytest.raises(QueryValidationError, match="read-only by construction"):
        validate_select(statement)


def test_rejects_statement_chaining() -> None:
    with pytest.raises(QueryValidationError, match="One statement per query"):
        validate_select("SELECT * FROM Invoice; SELECT * FROM Customer")


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("SELECT * FROM Invoice JOIN Customer ON x", "cannot JOIN"),
        ("SELECT * FROM Invoice GROUP BY CustomerRef", "cannot GROUP BY"),
        ("SELECT * FROM Invoice WHERE a = '1' OR b = '2'", "do not support OR"),
        ("SELECT * FROM Invoice WHERE Balance != '0'", "Inequality"),
    ],
)
def test_explains_unsupported_dialect_constructs(statement: str, expected: str) -> None:
    with pytest.raises(QueryValidationError, match=expected):
        validate_select(statement)


def test_keyword_check_ignores_string_literals() -> None:
    """A customer named 'Orr' must not trip the OR check."""
    # Would raise if quoted values were scanned for keywords.
    validate_select("SELECT * FROM Customer WHERE DisplayName = 'Orr and Sons'")
    validate_select("SELECT * FROM Customer WHERE DisplayName LIKE 'Grand Union%'")


def test_rejects_unknown_entity() -> None:
    with pytest.raises(QueryValidationError, match="not a queryable entity"):
        validate_select("SELECT * FROM Sprockets")


def test_rejects_missing_from() -> None:
    with pytest.raises(QueryValidationError, match="Missing FROM"):
        validate_select("SELECT 1")


def test_rejects_non_select() -> None:
    with pytest.raises(QueryValidationError, match="Only SELECT"):
        validate_select("SHOW TABLES")


def test_tolerates_trailing_semicolon_and_whitespace() -> None:
    assert validate_select("  SELECT * FROM Invoice ;  ").entity == "Invoice"
