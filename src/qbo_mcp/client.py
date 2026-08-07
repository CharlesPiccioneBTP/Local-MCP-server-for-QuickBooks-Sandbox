"""The only module that talks to the QuickBooks accounting API.

This is where "read-only by construction" is actually constructed, so the rules
are worth stating plainly:

* `QuickBooksReadClient` exposes exactly one public request method, `get`.
  There is no post/put/patch/delete, and none can be reached through it.
* The HTTP verb is the literal `"GET"` written into `_request`. It is not a
  parameter, so no caller can influence it.
* Every path is checked against `_ALLOWED_PATH_PREFIXES` -- `/query` and
  `/reports/`. QuickBooks writes are POSTs to `/v3/company/{realm}/{entity}`,
  which is neither of those, so even a hypothetical verb slip would have
  nowhere to land.
* The underlying httpx client is pinned to the accounting API host via
  `base_url`, and absolute URLs are rejected before they reach it.

`tests/test_readonly.py` parses this file and fails the build if a verb other
than GET appears, or if any other module constructs a client of its own.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .auth import TokenProvider

logger = logging.getLogger(__name__)

# Pin the minor version rather than tracking "latest": Intuit changes response
# shapes between minor versions, and an unattended server should not have its
# output change underneath it.
MINOR_VERSION = "75"

API_HOSTS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

# Reads only. `/query` runs the SQL-subset endpoint; `/reports/` returns
# pre-aggregated financial statements. Writes live at /v3/company/{realm}/{entity}
# and are unreachable from here.
_ALLOWED_PATH_PREFIXES = ("/query", "/reports/")

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_RETRIES = 3


class QuickBooksError(RuntimeError):
    """An error returned by the QuickBooks API, with the detail preserved."""

    def __init__(self, message: str, *, status_code: int | None = None, fault: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.fault = fault


class ReadOnlyViolation(AssertionError):
    """Raised when a caller attempts a path outside the read-only allowlist.

    Reaching this exception means a bug got past code review and the tests; it
    is deliberately not a subclass of QuickBooksError so it cannot be mistaken
    for an upstream failure and swallowed by retry logic.
    """


class QuickBooksReadClient:
    """A GET-only view of one QuickBooks company."""

    def __init__(self, tokens: TokenProvider, *, environment: str, realm_id: str):
        if environment not in API_HOSTS:
            raise ValueError(
                f"Unknown environment {environment!r}; expected one of {sorted(API_HOSTS)}"
            )
        self._tokens = tokens
        self._realm_id = realm_id
        self._environment = environment
        self._http = httpx.AsyncClient(
            base_url=f"{API_HOSTS[environment]}/v3/company/{realm_id}",
            timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json"},
        )

    @property
    def realm_id(self) -> str:
        return self._realm_id

    @property
    def environment(self) -> str:
        return self._environment

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "QuickBooksReadClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---- the only way out of this process to the accounting API ----

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Fetch a read-only QuickBooks resource.

        `path` is relative to /v3/company/{realm_id} and must begin with one of
        the allowed prefixes.
        """
        self._assert_readable_path(path)
        return await self._request(path, params)

    async def query(self, statement: str) -> dict[str, Any]:
        """Run a QuickBooks SQL-subset statement.

        Callers should validate the statement with `qbo_sql.validate_select`
        first; this method does not parse it. That is safe because the /query
        endpoint has no write capability regardless of what is sent to it.
        """
        response = await self.get("/query", {"query": statement})
        return response.get("QueryResponse", {}) or {}

    async def report(self, name: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Fetch a named report, e.g. ProfitAndLoss or BalanceSheet."""
        if not name.isalnum():
            raise ReadOnlyViolation(
                f"Report name {name!r} must be alphanumeric; refusing to build a path from it."
            )
        return await self.get(f"/reports/{name}", params)

    # ---- internals ----

    @staticmethod
    def _assert_readable_path(path: str) -> None:
        # An absolute URL would bypass base_url and could target any host,
        # including the write endpoints. Reject before httpx ever sees it.
        split = urlsplit(path)
        if split.scheme or split.netloc:
            raise ReadOnlyViolation(
                f"Refusing absolute URL {path!r}: this client may only address "
                f"paths under its pinned company base URL."
            )
        if not path.startswith(_ALLOWED_PATH_PREFIXES):
            raise ReadOnlyViolation(
                f"Refusing path {path!r}: this client is read-only and may only "
                f"reach {' or '.join(_ALLOWED_PATH_PREFIXES)}."
            )

    async def _request(self, path: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        merged = dict(params or {})
        merged["minorversion"] = MINOR_VERSION

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            token = await self._tokens.access_token()
            try:
                # The verb is a literal. This is the single place in the package
                # where a request to the accounting API is issued.
                response = await self._http.request(
                    "GET",
                    path,
                    params=merged,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == MAX_RETRIES - 1:
                    break
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 401:
                # The access token was rejected. Force a refresh and retry once;
                # if a fresh token is also rejected, the grant itself is broken.
                if attempt == MAX_RETRIES - 1:
                    raise QuickBooksError(
                        "QuickBooks rejected the access token even after refreshing it. "
                        "The connection likely needs re-authorising: uv run qbo-mcp-setup",
                        status_code=401,
                    )
                logger.info("Access token rejected; forcing refresh and retrying")
                await self._tokens.invalidate_access_token()
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                last_error = QuickBooksError(
                    f"QuickBooks returned {response.status_code}", status_code=response.status_code
                )
                if attempt == MAX_RETRIES - 1:
                    break
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                raise self._error_from(response)

            try:
                return response.json()
            except ValueError as exc:
                raise QuickBooksError(
                    f"QuickBooks returned a non-JSON response ({response.status_code}). "
                    f"First 200 characters: {response.text[:200]!r}"
                ) from exc

        assert last_error is not None
        if isinstance(last_error, QuickBooksError):
            raise last_error
        raise QuickBooksError(
            f"Could not reach QuickBooks after {MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return float(2**attempt)

    @staticmethod
    def _error_from(response: httpx.Response) -> QuickBooksError:
        """Surface Intuit's Fault detail instead of a bare status code.

        Their 400s carry the actual reason (bad field name, malformed query) and
        losing it turns every mistake into an unhelpful "400 Bad Request".
        """
        fault: Any = None
        detail = response.text[:500]
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            fault = payload.get("Fault") or payload.get("fault")
            errors = (fault or {}).get("Error") or []
            if errors:
                parts = []
                for err in errors:
                    message = err.get("Message", "")
                    extra = err.get("Detail", "")
                    parts.append(f"{message}: {extra}" if extra else message)
                detail = " | ".join(p for p in parts if p) or detail

        return QuickBooksError(
            f"QuickBooks returned {response.status_code}: {detail}",
            status_code=response.status_code,
            fault=fault,
        )
