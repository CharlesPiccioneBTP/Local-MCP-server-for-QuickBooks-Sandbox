"""OAuth 2.0 against Intuit: one-time setup, then unattended token refresh.

This module owns the only POST in the package, and it is pinned to
`TOKEN_ENDPOINT`. That endpoint is Intuit's OAuth service, not the accounting
API, so no write to QuickBooks data is reachable from here. `client.py` holds
the accounting-API client and is GET-only. Neither can do the other's job, and
`tests/test_readonly.py` checks that this stays true.

The hard part is not getting a token, it is keeping one. Intuit rotates the
refresh token roughly every 24 hours and invalidates the previous value the
instant a new one is issued. So a refresh that succeeds upstream but fails to
persist locally does not cause a retryable error -- it permanently disconnects
the integration. Everything below is arranged around not losing that write:

* refresh happens under a cross-process lock;
* credentials are re-read inside the lock, so a refresh another process just
  completed is adopted rather than overwritten;
* the new token pair is written atomically before being returned to callers.
"""

from __future__ import annotations

import base64
import http.server
import logging
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from .config import (
    ConfigError,
    Credentials,
    credentials_lock,
    credentials_path,
    load_credentials,
    save_credentials,
)

logger = logging.getLogger(__name__)

# Confirmed against Intuit's OpenID discovery document. These are identical for
# sandbox and production; only the accounting API host differs.
AUTHORIZE_ENDPOINT = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Intuit publishes no read-only accounting scope: this grants read AND write.
# The read-only guarantee is therefore enforced by our code, not by the token.
# See the README section "What read-only does and does not mean".
SCOPE = "com.intuit.quickbooks.accounting"

REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8000
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

# Refresh this long before nominal expiry, so a request never races the clock.
EXPIRY_MARGIN_SECONDS = 300
# Warn once the refresh token (max 5 years, per Intuit's 2025 policy change) is
# close enough that a silent failure would be disruptive.
REFRESH_EXPIRY_WARN_SECONDS = 30 * 24 * 3600


class AuthError(RuntimeError):
    """Authentication failed in a way the user needs to act on."""


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    refresh_token_expires_in: int | None = None

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> "TokenResponse":
        try:
            return cls(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                expires_in=int(payload.get("expires_in", 3600)),
                refresh_token_expires_in=(
                    int(payload["x_refresh_token_expires_in"])
                    if "x_refresh_token_expires_in" in payload
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthError(
                f"Intuit's token response was missing expected fields: {payload!r}"
            ) from exc


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _exchange(client_id: str, client_secret: str, form: dict[str, str]) -> TokenResponse:
    """POST to Intuit's token endpoint. The only POST in this package."""
    response = httpx.post(
        TOKEN_ENDPOINT,
        data=form,
        headers={
            "Authorization": _basic_auth_header(client_id, client_secret),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30.0,
    )

    if response.status_code >= 400:
        detail = response.text[:400]
        if "invalid_grant" in detail:
            raise AuthError(
                "Intuit rejected the refresh token (invalid_grant). This happens when "
                "the token expired, was revoked in the QuickBooks UI, or was superseded "
                "by another copy of this server refreshing it.\n"
                "Reconnect with:  uv run qbo-mcp-setup"
            )
        if response.status_code == 401:
            raise AuthError(
                "Intuit rejected the client credentials (401). Check the Client ID and "
                "Client Secret, and that they are the keys for the right environment "
                "(sandbox keys differ from production keys).\n"
                "Re-enter them with:  uv run qbo-mcp-setup"
            )
        raise AuthError(f"Token request failed ({response.status_code}): {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(f"Token endpoint returned non-JSON: {response.text[:200]!r}") from exc

    return TokenResponse.parse(payload)


class TokenProvider:
    """Supplies a valid access token, refreshing and persisting as needed."""

    def __init__(self, credentials: Credentials, *, path: Any = None):
        self._creds = credentials
        self._path = path or credentials_path()
        self._force_refresh = False

    @property
    def credentials(self) -> Credentials:
        return self._creds

    async def access_token(self) -> str:
        """Return a usable access token, refreshing it if needed.

        The refresh path is synchronous (it takes a blocking cross-process lock)
        so it runs in a worker thread rather than stalling the event loop.
        """
        if self._is_fresh():
            return self._creds.access_token
        await anyio.to_thread.run_sync(self._refresh_locked)
        return self._creds.access_token

    async def invalidate_access_token(self) -> None:
        """Force the next `access_token()` call to refresh.

        Called when QuickBooks rejects a token that we still believed was valid,
        which happens after a password change or a revoked connection.
        """
        self._force_refresh = True

    def _is_fresh(self) -> bool:
        if self._force_refresh or not self._creds.access_token:
            return False
        return time.time() < self._creds.access_token_expires_at - EXPIRY_MARGIN_SECONDS

    def _refresh_locked(self) -> None:
        with credentials_lock(self._path):
            # Another process may have refreshed while we waited for the lock.
            # Adopting its tokens avoids burning our now-stale refresh token,
            # which would invalidate theirs and break both of us.
            try:
                on_disk = load_credentials(self._path)
            except ConfigError:
                on_disk = None

            if on_disk is not None:
                self._creds = on_disk
                if not self._force_refresh and self._is_fresh():
                    logger.info("Another process refreshed the token; adopted it")
                    return

            self._creds = refresh_credentials(self._creds)
            save_credentials(self._creds, self._path)
            self._force_refresh = False
            _warn_if_refresh_token_expiring(self._creds)


def refresh_credentials(creds: Credentials) -> Credentials:
    """Exchange the refresh token for a new token pair.

    Returns updated credentials; the caller is responsible for persisting them
    before use. Always store what comes back, even when the refresh token looks
    unchanged -- Intuit rotates it on its own schedule.
    """
    now = time.time()
    tokens = _exchange(
        creds.client_id,
        creds.client_secret,
        {"grant_type": "refresh_token", "refresh_token": creds.refresh_token},
    )

    creds.access_token = tokens.access_token
    creds.refresh_token = tokens.refresh_token
    creds.access_token_expires_at = now + tokens.expires_in
    if tokens.refresh_token_expires_in is not None:
        creds.refresh_token_expires_at = now + tokens.refresh_token_expires_in
    return creds


def _warn_if_refresh_token_expiring(creds: Credentials) -> None:
    if not creds.refresh_token_expires_at:
        return
    remaining = creds.refresh_token_expires_at - time.time()
    if remaining < REFRESH_EXPIRY_WARN_SECONDS:
        logger.warning(
            "QuickBooks refresh token expires in %.0f days. Re-run 'uv run qbo-mcp-setup' "
            "before then or the server will stop working.",
            remaining / 86400,
        )


# --------------------------------------------------------------------------
# One-time interactive setup
# --------------------------------------------------------------------------


@dataclass
class _CallbackResult:
    code: str | None = None
    realm_id: str | None = None
    state: str | None = None
    error: str | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single redirect Intuit sends back after consent."""

    result: _CallbackResult
    expected_state: str
    done: threading.Event

    def do_GET(self) -> None:  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_error(404, "Not the callback path")
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [None])[0]

        # Reject a mismatched state before touching the code: this is what stops
        # an unrelated page in the browser from injecting its own auth code.
        if state != self.expected_state:
            self.result.error = "State mismatch - ignoring this callback."
            self._respond(400, "Authorisation failed", self.result.error)
            self.done.set()
            return

        if "error" in params:
            description = (params.get("error_description") or params.get("error"))[0]
            self.result.error = description
            self._respond(400, "Authorisation failed", description)
            self.done.set()
            return

        self.result.code = (params.get("code") or [None])[0]
        self.result.realm_id = (params.get("realmId") or [None])[0]
        self.result.state = state

        if not self.result.code or not self.result.realm_id:
            self.result.error = "Intuit's redirect did not include both code and realmId."
            self._respond(400, "Authorisation failed", self.result.error)
        else:
            self._respond(
                200,
                "QuickBooks connected",
                "You can close this tab and return to the terminal.",
            )
        self.done.set()

    def _respond(self, status: int, heading: str, message: str) -> None:
        body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{heading}</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; line-height: 1.5;">
<h1 style="font-size: 1.4rem;">{heading}</h1>
<p>{message}</p>
</body></html>"""
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: Any) -> None:
        pass  # Keep the setup transcript clean.


def _await_callback(expected_state: str, timeout: float = 300.0) -> _CallbackResult:
    """Serve exactly one OAuth redirect on the loopback interface."""
    result = _CallbackResult()
    done = threading.Event()

    handler = type(
        "_BoundCallbackHandler",
        (_CallbackHandler,),
        {"result": result, "expected_state": expected_state, "done": done},
    )

    try:
        # Bind loopback explicitly: this listener should never be reachable
        # from the network, only from the browser on this machine.
        server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), handler)
    except OSError as exc:
        raise AuthError(
            f"Could not listen on 127.0.0.1:{REDIRECT_PORT} ({exc}). Something else is "
            f"using that port. Stop it and retry -- the port must match the redirect URI "
            f"registered in your Intuit app ({REDIRECT_URI})."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise AuthError(
                f"Timed out after {timeout / 60:.0f} minutes waiting for the browser "
                f"redirect. Re-run setup and complete the consent screen."
            )
    finally:
        server.shutdown()
        server.server_close()

    return result


def _prompt(label: str, *, secret: bool = False, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            import getpass

            value = getpass.getpass(f"{label}{suffix}: ").strip()
        else:
            value = input(f"{label}{suffix}: ").strip()
        if not value and default:
            return default
        if value:
            return value
        print("  Required.")


def run_setup() -> Credentials:
    """Interactive first-time connection to a QuickBooks company."""
    print("QuickBooks MCP server - one-time setup")
    print("=" * 46)
    print()
    print("You will need the Client ID and Client Secret from your Intuit app.")
    print("Find them at https://developer.intuit.com -> your app -> Keys & credentials.")
    print(f"That app must list this exact redirect URI: {REDIRECT_URI}")
    print()

    environment = _prompt("Environment (sandbox/production)", default="sandbox").lower()
    if environment not in ("sandbox", "production"):
        raise AuthError(f"Environment must be 'sandbox' or 'production', not {environment!r}")
    if environment == "sandbox":
        print("  Using sandbox: enter the Development keys, not the Production ones.")

    client_id = _prompt("Client ID")
    client_secret = _prompt("Client Secret", secret=True)

    state = secrets.token_urlsafe(24)
    authorize_url = f"{AUTHORIZE_ENDPOINT}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
    )

    print()
    print("Opening your browser to authorise. Sign in and pick the company to connect.")
    print("If nothing opens, paste this URL yourself:")
    print()
    print(f"  {authorize_url}")
    print()

    _check_port_free()
    opened = webbrowser.open(authorize_url)
    if not opened:
        print("(Could not launch a browser automatically - use the URL above.)")

    result = _await_callback(state)
    if result.error:
        raise AuthError(f"Authorisation failed: {result.error}")
    assert result.code and result.realm_id

    print("Authorised. Exchanging the code for tokens...")
    now = time.time()
    tokens = _exchange(
        client_id,
        client_secret,
        {
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": REDIRECT_URI,
        },
    )

    creds = Credentials(
        client_id=client_id,
        client_secret=client_secret,
        realm_id=result.realm_id,
        refresh_token=tokens.refresh_token,
        environment=environment,
        access_token=tokens.access_token,
        access_token_expires_at=now + tokens.expires_in,
        refresh_token_expires_at=(
            now + tokens.refresh_token_expires_in if tokens.refresh_token_expires_in else 0.0
        ),
    )

    save_credentials(creds)
    print(f"Saved credentials to {credentials_path()}")
    print(f"Connected to company (realm) {creds.realm_id} in {environment}.")
    return creds


def _check_port_free() -> None:
    """Fail early with a clear message rather than after the browser opens."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", REDIRECT_PORT)) == 0:
            raise AuthError(
                f"Port {REDIRECT_PORT} is already in use, so the redirect could not be "
                f"received. Stop whatever is listening there and re-run setup."
            )


def main() -> int:
    """Entry point for `uv run qbo-mcp-setup`."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        creds = run_setup()
    except (AuthError, ConfigError) as exc:
        print(f"\nSetup failed.\n\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    # Prove the connection works now, rather than letting the first failure
    # surface later inside Claude Desktop where the error is hard to see.
    print("\nVerifying the connection...")
    try:
        from .doctor import smoke_test

        company = anyio.run(smoke_test, creds)
    except Exception as exc:  # noqa: BLE001 - setup should report, not traceback
        print(f"\nCredentials saved, but the test query failed: {exc}", file=sys.stderr)
        print("Run 'uv run qbo-mcp-doctor' for details.", file=sys.stderr)
        return 1

    print(f"Success - connected to \"{company}\".")
    print("\nNext: add the server to Claude Desktop (see README.md), then restart it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
