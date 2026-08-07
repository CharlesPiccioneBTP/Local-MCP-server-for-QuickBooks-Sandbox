"""Protocol-level tests: drive the server the way an MCP host does.

These exercise the real tool registrations and the error translation in
server.py, which the unit tests bypass by calling tools.py directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from qbo_mcp.server import mcp

EXPECTED_TOOLS = {
    "get_receivables_aging",
    "get_payables_aging",
    "get_profit_and_loss",
    "get_balance_sheet",
    "find_contacts",
    "get_company_info",
    "run_query",
    "describe_schema",
}


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test read or write the developer's real credentials."""
    monkeypatch.setenv("QBO_MCP_CONFIG_DIR", str(tmp_path / "config"))


async def test_server_exposes_the_expected_tools() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools.tools} == EXPECTED_TOOLS


async def test_every_tool_has_a_description() -> None:
    """The description is all the model has to choose between eight tools."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    for tool in tools.tools:
        assert tool.description and len(tool.description.strip()) > 40, tool.name


async def test_no_tool_advertises_a_write() -> None:
    """A read-only server must not describe itself as able to change anything."""
    forbidden = ("create ", "update ", "delete ", "modify ", "write ", "send ")
    async with Client(mcp) as client:
        tools = await client.list_tools()
    for tool in tools.tools:
        text = (tool.description or "").lower()
        for word in forbidden:
            # "cannot modify" / "no query can modify" are fine; bare claims are not.
            index = text.find(word)
            if index != -1:
                preceding = text[max(0, index - 30) : index]
                assert any(
                    neg in preceding for neg in ("cannot", "can only", "no ", "not ", "never")
                ), f"{tool.name} description suggests it can {word.strip()}"


async def test_describe_schema_needs_no_credentials() -> None:
    """The one tool that must work before setup, so run_query is learnable."""
    async with Client(mcp) as client:
        result = await client.call_tool("describe_schema", {})

    assert not result.is_error
    content = result.structured_content or {}
    assert "Invoice" in content["entities"]
    assert any("JOIN" in note for note in content["dialect_notes"])


async def test_describe_schema_for_one_entity() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("describe_schema", {"entity": "invoice"})

    content = result.structured_content or {}
    assert content["entity"] == "Invoice"
    assert any("Balance" in field for field in content["key_fields"])


async def test_unknown_entity_lists_the_known_ones() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("describe_schema", {"entity": "Sprockets"})

    content = result.structured_content or {}
    assert "error" in content
    assert "Invoice" in content["known_entities"]


async def test_missing_credentials_gives_actionable_error() -> None:
    """Without setup, every data tool should say what to run - not traceback."""
    async with Client(mcp) as client:
        result = await client.call_tool("get_receivables_aging", {})

    assert result.is_error
    message = str(result.content)
    assert "qbo-mcp-setup" in message


async def test_write_shaped_query_is_refused_before_any_network_call() -> None:
    """The refusal must not depend on credentials or on reaching Intuit."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "run_query", {"query": "UPDATE Invoice SET Balance = 0"}
        )

    assert result.is_error
    assert "read-only by construction" in str(result.content)
