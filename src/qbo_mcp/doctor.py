"""`uv run qbo-mcp-doctor` - prove the installation works, without Claude.

Checks, in dependency order:

1. credentials load from the config directory;
2. a *forced* token refresh succeeds AND the rotated refresh token is written
   back to disk -- the specific failure that would otherwise surface as a
   mysteriously dead server days later;
3. a real query returns the company name;
4. the receivables aging pipeline returns data.

Exit code 0 means a colleague can stop worrying. Anything else prints what to
do next.
"""

from __future__ import annotations

import logging
import sys

import anyio

from .auth import AuthError, TokenProvider
from .client import QuickBooksError, QuickBooksReadClient
from .config import ConfigError, Credentials, credentials_path, load_credentials


async def smoke_test(creds: Credentials) -> str:
    """Run one real query; return the company name. Used by setup and doctor."""
    tokens = TokenProvider(creds)
    async with QuickBooksReadClient(
        tokens, environment=creds.environment, realm_id=creds.realm_id
    ) as client:
        response = await client.query("SELECT * FROM CompanyInfo")
        info = (response.get("CompanyInfo") or [{}])[0]
        return info.get("CompanyName", "<unnamed company>")


async def _run_checks() -> int:
    print("qbo-mcp doctor")
    print("=" * 46)

    # 1. Credentials present and well-formed.
    path = credentials_path()
    try:
        creds = load_credentials()
    except ConfigError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print(f"[ ok ] Credentials loaded from {path}")
    print(f"       environment={creds.environment}  realm={creds.realm_id}")

    # 2. Forced refresh, and rotation actually persisted.
    old_refresh = creds.refresh_token
    tokens = TokenProvider(creds)
    await tokens.invalidate_access_token()
    try:
        await tokens.access_token()
    except AuthError as exc:
        print(f"\n[FAIL] Token refresh failed: {exc}")
        return 1
    print("[ ok ] Token refresh succeeded")

    on_disk = load_credentials()
    if not on_disk.access_token:
        print("\n[FAIL] Refresh succeeded but nothing was written back to disk.")
        return 1
    if on_disk.refresh_token == old_refresh:
        # Intuit rotates roughly daily, not on every refresh; same value is
        # normal. What matters is that the file reflects whatever came back.
        print("[ ok ] Refresh token unchanged this time (rotation is periodic); file is current")
    else:
        print("[ ok ] Refresh token rotated and the new value was persisted")

    if on_disk.refresh_token_expires_at:
        import time

        days = (on_disk.refresh_token_expires_at - time.time()) / 86400
        marker = "[warn]" if days < 30 else "[ ok ]"
        print(f"{marker} Refresh token expires in {days:.0f} days"
              + (" - re-run qbo-mcp-setup soon" if days < 30 else ""))

    # 3. Real data comes back.
    try:
        company = await smoke_test(on_disk)
    except (QuickBooksError, AuthError) as exc:
        print(f"\n[FAIL] Query failed: {exc}")
        return 1
    print(f'[ ok ] Connected to "{company}"')

    # 4. The demo pipeline end-to-end.
    from .tools import receivables_aging_summary

    tokens = TokenProvider(on_disk)
    async with QuickBooksReadClient(
        tokens, environment=on_disk.environment, realm_id=on_disk.realm_id
    ) as client:
        aging = await receivables_aging_summary(client)

    totals = aging["totals"]
    print(f"[ ok ] Receivables aging: {aging['invoice_count']} open invoice(s), "
          f"total outstanding {totals['total_due']:.2f}")
    for bucket, amount in totals["by_bucket"].items():
        print(f"         {bucket:<8} {amount:>12,.2f}")

    print("\nAll checks passed. The server is ready for Claude Desktop.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        return anyio.run(_run_checks)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
