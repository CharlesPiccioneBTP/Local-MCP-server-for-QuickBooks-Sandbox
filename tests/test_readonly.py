"""Mechanical enforcement of "read-only by construction".

These tests parse the package source (AST, not regex) and fail if:

* any module other than client.py imports or constructs an httpx client,
  except auth.py's single POST to Intuit's OAuth token endpoint;
* client.py issues any HTTP verb other than GET, by literal or by helper;
* auth.py's POST targets anything but the TOKEN_ENDPOINT constant;
* client.py's path allowlist loses either read prefix or gains a new one.

The point is that a future "small write helper" cannot arrive quietly: adding
one means editing this file, which is a visible, reviewable act.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "qbo_mcp"

# Methods on httpx clients/module that issue non-GET requests.
FORBIDDEN_HTTP_METHODS = {"post", "put", "patch", "delete", "send", "stream"}
HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _modules() -> dict[str, ast.Module]:
    sources = {}
    for path in PACKAGE.glob("*.py"):
        sources[path.name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert "client.py" in sources, "client.py is missing - the guarantee has no home"
    assert "auth.py" in sources, "auth.py is missing"
    return sources


def _imports_httpx(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "httpx" or alias.name.startswith("httpx.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "httpx" or node.module.startswith("httpx.")):
                return True
    return False


def test_only_client_and_auth_touch_http() -> None:
    """No third module may grow its own HTTP capability."""
    offenders = [
        name
        for name, tree in _modules().items()
        if name not in ("client.py", "auth.py") and _imports_httpx(tree)
    ]
    assert not offenders, (
        f"{offenders} import httpx. All QuickBooks traffic must flow through "
        f"client.py (GET-only) or auth.py (token endpoint only)."
    )


def test_client_issues_only_get() -> None:
    """client.py may not contain any way to issue a non-GET request."""
    tree = _modules()["client.py"]

    for node in ast.walk(tree):
        # x.post(...), x.send(...), httpx.put(...) etc.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in FORBIDDEN_HTTP_METHODS, (
                f"client.py line {node.lineno}: call to .{node.func.attr}() - "
                f"this module must be incapable of writing."
            )
        # .request(<verb>, ...) with anything but the literal "GET".
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "request"
        ):
            assert node.args, f"client.py line {node.lineno}: .request() with no verb argument"
            verb = node.args[0]
            assert isinstance(verb, ast.Constant) and verb.value == "GET", (
                f"client.py line {node.lineno}: .request() verb must be the literal "
                f'"GET", found {ast.dump(verb)}'
            )

    # No non-GET verb may even appear as a string literal, which also catches
    # someone threading a verb through a variable to dodge the check above.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.upper() not in (HTTP_VERBS - {"GET"}), (
                f"client.py line {node.lineno}: HTTP verb literal {node.value!r} "
                f"has no business in the read-only client."
            )


def test_auth_posts_only_to_token_endpoint() -> None:
    """auth.py's POST must target the TOKEN_ENDPOINT constant, nothing else."""
    tree = _modules()["auth.py"]
    post_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN_HTTP_METHODS
    ]
    assert len(post_calls) == 1, (
        f"auth.py should contain exactly one POST (the token exchange), "
        f"found {len(post_calls)}"
    )
    call = post_calls[0]
    assert call.func.attr == "post"
    target = call.args[0] if call.args else None
    assert isinstance(target, ast.Name) and target.id == "TOKEN_ENDPOINT", (
        "auth.py's POST must target the TOKEN_ENDPOINT constant by name, so the "
        "destination is auditable at a glance."
    )

    # And that constant must still be Intuit's OAuth service, not the API host.
    endpoint = next(
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "TOKEN_ENDPOINT" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    )
    assert endpoint == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    assert "api.intuit.com" not in endpoint


def test_path_allowlist_is_reads_only() -> None:
    """The allowlist must be exactly the two read prefixes."""
    from qbo_mcp import client

    assert client._ALLOWED_PATH_PREFIXES == ("/query", "/reports/")


def test_client_rejects_write_shaped_paths() -> None:
    """Entity paths (where QuickBooks writes live) must be refused up front."""
    from qbo_mcp.client import QuickBooksReadClient, ReadOnlyViolation

    import pytest

    for path in (
        "/invoice",
        "/customer",
        "/bill?operation=delete",
        "https://sandbox-quickbooks.api.intuit.com/v3/company/1/query",
        "//evil.example/query",
    ):
        with pytest.raises(ReadOnlyViolation):
            QuickBooksReadClient._assert_readable_path(path)

    # And the reads must pass, or the server does nothing at all.
    QuickBooksReadClient._assert_readable_path("/query")
    QuickBooksReadClient._assert_readable_path("/reports/ProfitAndLoss")


def test_public_surface_has_no_write_methods() -> None:
    """The client's public API is get/query/report and nothing verb-shaped."""
    from qbo_mcp.client import QuickBooksReadClient

    public = {name for name in dir(QuickBooksReadClient) if not name.startswith("_")}
    assert public == {"get", "query", "report", "aclose", "realm_id", "environment"}
