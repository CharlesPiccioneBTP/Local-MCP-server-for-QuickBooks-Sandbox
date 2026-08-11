"""`uv run qbo-mcp-import` - connect using tokens someone else obtained.

The normal path is `qbo-mcp-setup`, which walks the OAuth flow and produces the
four values below itself. This module exists for the case where a colleague has
already done that and hands the values over: the person running this needs no
Intuit account and never sees a sign-in page.

The trade-off is real and is stated to the user rather than buried here. Intuit
rotates the refresh token roughly daily and invalidates the previous value the
instant a new one is issued, so a token pair works on exactly one machine. If
the person who supplied it keeps using it too, whichever machine refreshes
second is disconnected permanently. And because the recipient has no Intuit
account, they cannot re-run setup to recover - they have to ask for a new token.

Nothing here talks to QuickBooks directly. It writes the same credentials file
`qbo-mcp-setup` writes, through the same atomic, locked, git-refusing path, and
verifies it through the same smoke test.
"""

from __future__ import annotations

import getpass
import logging
import sys

import anyio

from .config import (
    ConfigError,
    Credentials,
    credentials_path,
    load_credentials,
    save_credentials,
)

logger = logging.getLogger(__name__)

VALID_ENVIRONMENTS = ("sandbox", "production")

# Intuit's refresh tokens are opaque, but every one observed starts with this
# and runs to roughly 40 characters. Catching a pasted *access* token here saves
# a confusing failure three steps later: access tokens are JWTs (three
# dot-separated segments, hundreds of characters) and are the value people
# reach for first, because it is the one that visibly "works" in curl.
REFRESH_TOKEN_PREFIX = "RT1-"


class ImportError_(Exception):
    """Raised when the supplied values cannot form a usable credentials file."""


def _prompt(label: str, *, secret: bool = False, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            value = getpass.getpass(f"{label}{suffix}: ").strip()
        else:
            value = input(f"{label}{suffix}: ").strip()
        if not value and default:
            return default
        if value:
            return value
        print("  Required.")


def _masked(value: str) -> str:
    """Enough to spot a truncated paste, not enough to be worth shoulder-surfing."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{'*' * (len(value) - 4)}{value[-4:]}  ({len(value)} characters)"


def validate_refresh_token(value: str) -> str:
    """Reject the values people paste instead of a refresh token."""
    value = value.strip()
    if value.count(".") >= 2 and value.startswith("ey"):
        raise ImportError_(
            "That looks like an access token (a JWT), not a refresh token.\n"
            "Access tokens expire after 1 hour and cannot be renewed, so the\n"
            "server cannot use one. Ask for the refresh token - it is a shorter\n"
            "value beginning 'RT1-' and it lasts about 100 days."
        )
    if value.startswith("XAB") or (len(value) > 40 and value.startswith("XAB1")):
        raise ImportError_(
            "That looks like an authorization code, not a refresh token.\n"
            "Authorization codes are single-use and expire within minutes.\n"
            "Ask for the refresh token - it begins 'RT1-'."
        )
    if not value.startswith(REFRESH_TOKEN_PREFIX):
        # Not fatal: Intuit has never documented the prefix as stable, so refuse
        # to hard-fail on a value that might simply be newer than this code.
        logger.warning(
            "Refresh token does not start with %s. Continuing, but if the "
            "verification below fails, check you were sent the right value.",
            REFRESH_TOKEN_PREFIX,
        )
    return value


def validate_environment(value: str) -> str:
    value = value.strip().lower()
    if value not in VALID_ENVIRONMENTS:
        raise ImportError_(
            f"Environment must be one of {', '.join(VALID_ENVIRONMENTS)}, not {value!r}."
        )
    return value


def _confirm_overwrite() -> None:
    """Never silently replace a working connection.

    Whoever is running this may already have their own OAuth grant. Overwriting
    it with a borrowed token would cost them a re-run of setup to undo, and the
    symptom - suddenly looking at someone else's company - is confusing.
    """
    path = credentials_path()
    if not path.exists():
        return

    try:
        existing = load_credentials(path)
    except ConfigError:
        print(f"\nThere is an unreadable credentials file at {path}.")
    else:
        print(f"\nThis machine is already connected:")
        print(f"  file         {path}")
        print(f"  environment  {existing.environment}")
        print(f"  company      {existing.realm_id}")
        print("\nImporting will replace it. If that connection is your own OAuth")
        print("grant, you can restore it later with 'uv run qbo-mcp-setup'.")

    answer = input("\nReplace it? Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        raise ImportError_("Cancelled - nothing was changed.")


def run_import() -> Credentials:
    """Interactive import of a token pair obtained by someone else."""
    print("QuickBooks MCP server - import an existing connection")
    print("=" * 52)
    print()
    print("Use this when a colleague has already connected a QuickBooks company")
    print("and has sent you the values below. You will not need an Intuit account")
    print("and you will not be asked to sign in.")
    print()
    print("You need four values from them:")
    print("  Client ID, Client Secret, Realm ID, and the Refresh Token.")
    print()
    print("The refresh token is the one that matters and the one most often sent")
    print("by mistake. It begins 'RT1-'. An access token (a long value beginning")
    print("'ey', with dots in it) will not work - it expires after an hour.")
    print()

    _confirm_overwrite()

    print()
    environment = validate_environment(
        _prompt("Environment (sandbox/production)", default="sandbox")
    )

    client_id = _prompt("Client ID")
    client_secret = _prompt("Client Secret", secret=True)
    print(f"  Client Secret: {_masked(client_secret)}")

    realm_id = _prompt("Realm ID (the company number)")

    refresh_token = validate_refresh_token(_prompt("Refresh Token", secret=True))
    print(f"  Refresh Token: {_masked(refresh_token)}")

    creds = Credentials(
        client_id=client_id,
        client_secret=client_secret,
        realm_id=realm_id,
        refresh_token=refresh_token,
        environment=environment,
        # Deliberately left empty: the first request refreshes, which also
        # proves the refresh token is live rather than merely well-formed.
        access_token="",
        access_token_expires_at=0.0,
        refresh_token_expires_at=0.0,
    )

    save_credentials(creds)
    print()
    print(f"Saved credentials to {credentials_path()}")
    return creds


def main() -> int:
    """Entry point for `uv run qbo-mcp-import`."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        creds = run_import()
    except (ImportError_, ConfigError) as exc:
        print(f"\nImport failed.\n\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    print("\nVerifying the connection...")
    try:
        from .doctor import smoke_test

        company = anyio.run(smoke_test, creds)
    except Exception as exc:  # noqa: BLE001 - report, don't traceback at a user
        print(f"\nCredentials saved, but the test query failed: {exc}", file=sys.stderr)
        print(
            "\nThe most likely causes, in order:\n"
            "  1. The refresh token has already been used by another machine.\n"
            "     Intuit invalidates the previous value each time it rotates.\n"
            "  2. An access token was pasted instead of the refresh token.\n"
            "  3. The Client ID/Secret belong to a different app than the token.\n"
            "\nAsk whoever sent the values for a freshly generated refresh token.\n"
            "Run 'uv run qbo-mcp-doctor' for the full diagnosis.",
            file=sys.stderr,
        )
        return 1

    print(f'Success - connected to "{company}".')
    print()
    print("Note: this connection uses a token someone else generated. Intuit")
    print("rotates it, so it works on one machine at a time. If it stops working,")
    print("you cannot re-run setup yourself - ask them for a new refresh token.")
    print()
    print("Next: add the server to Claude Desktop (see README.md), then restart it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
