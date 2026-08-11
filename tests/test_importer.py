"""The import path exists so a colleague can connect without an Intuit account.

Its whole risk is that people paste the wrong value: the access token is the
one that visibly works in curl, so it is the one that gets sent. These tests
pin the guardrails that turn that into an immediate, explanatory error rather
than a failed API call several steps later.
"""

from __future__ import annotations

import json

import pytest

from qbo_mcp.config import Credentials, credentials_path, load_credentials, save_credentials
from qbo_mcp.importer import (
    ImportError_,
    validate_environment,
    validate_refresh_token,
)

# Shapes only - none of these are live values.
ACCESS_TOKEN = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMTI4Q0JDLUhTMjU2In0." + "x" * 300 + ".sig"
AUTH_CODE = "XAB11786469757l3IV12lXSgdZSPZwYjCxl1I54muDaZ3VOND5"
REFRESH_TOKEN = "RT1-34-H0-1795195592gsg1cvdq732evbbln7xt"


def test_accepts_a_well_formed_refresh_token() -> None:
    assert validate_refresh_token(REFRESH_TOKEN) == REFRESH_TOKEN


def test_strips_surrounding_whitespace() -> None:
    """Values arrive pasted out of chat, often with a stray newline."""
    assert validate_refresh_token(f"  {REFRESH_TOKEN}\n") == REFRESH_TOKEN


def test_rejects_an_access_token_by_name() -> None:
    with pytest.raises(ImportError_) as exc:
        validate_refresh_token(ACCESS_TOKEN)
    message = str(exc.value)
    assert "access token" in message.lower()
    assert "RT1-" in message, "the error must say what to ask for instead"


def test_rejects_an_authorization_code_by_name() -> None:
    with pytest.raises(ImportError_) as exc:
        validate_refresh_token(AUTH_CODE)
    message = str(exc.value)
    assert "authorization code" in message.lower()
    assert "RT1-" in message


def test_unrecognised_prefix_warns_but_is_allowed(caplog: pytest.LogCaptureFixture) -> None:
    """Intuit has never promised the prefix is stable; refusing outright would
    strand someone whose token is simply newer than this code."""
    value = "SOMETHING-NEW-1234567890"
    with caplog.at_level("WARNING"):
        assert validate_refresh_token(value) == value
    assert "RT1-" in caplog.text


@pytest.mark.parametrize("value", ["sandbox", "SANDBOX", " Production ", "production"])
def test_environment_is_normalised(value: str) -> None:
    assert validate_environment(value) in ("sandbox", "production")


def test_rejects_an_unknown_environment() -> None:
    with pytest.raises(ImportError_):
        validate_environment("staging")


def test_imported_credentials_load_back(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An imported file must satisfy the same loader as a setup-written one.

    This is the actual claim being made to the user: no code change was needed
    to support borrowed tokens, because the file format is identical.
    """
    monkeypatch.setenv("QBO_MCP_CONFIG_DIR", str(tmp_path))

    creds = Credentials(
        client_id="client-id",
        client_secret="client-secret",
        realm_id="9341457654564284",
        refresh_token=REFRESH_TOKEN,
        environment="sandbox",
    )
    save_credentials(creds)

    loaded = load_credentials()
    assert loaded.refresh_token == REFRESH_TOKEN
    assert loaded.realm_id == "9341457654564284"
    assert loaded.environment == "sandbox"
    # Left empty on purpose: the first refresh proves the token is live.
    assert loaded.access_token == ""


def test_imported_file_has_no_access_token_on_disk(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_MCP_CONFIG_DIR", str(tmp_path))
    save_credentials(
        Credentials(
            client_id="client-id",
            client_secret="client-secret",
            realm_id="realm",
            refresh_token=REFRESH_TOKEN,
        )
    )
    on_disk = json.loads(credentials_path().read_text(encoding="utf-8"))
    assert on_disk["access_token"] == ""
