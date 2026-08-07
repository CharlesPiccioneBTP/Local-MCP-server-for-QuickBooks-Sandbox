"""Tests for credential storage.

The git-work-tree refusal is the mechanism behind "credentials never touch
version control", so it gets a test that actually creates a repo and checks the
write is refused -- not a mocked stand-in.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from qbo_mcp.config import (
    ConfigError,
    Credentials,
    credentials_lock,
    load_credentials,
    save_credentials,
)


def _creds(**overrides: object) -> Credentials:
    base = {
        "client_id": "id-123",
        "client_secret": "secret-456",
        "realm_id": "9130000000",
        "refresh_token": "refresh-abc",
    }
    base.update(overrides)
    return Credentials(**base)  # type: ignore[arg-type]


def test_round_trip_preserves_fields(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    original = _creds(access_token="tok", access_token_expires_at=1234.5)
    save_credentials(original, path)

    loaded = load_credentials(path)
    assert loaded.client_id == "id-123"
    assert loaded.refresh_token == "refresh-abc"
    assert loaded.access_token == "tok"
    assert loaded.access_token_expires_at == 1234.5


def test_refuses_to_write_inside_a_git_work_tree(tmp_path: Path) -> None:
    """The guard that keeps secrets out of version control."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)

    with pytest.raises(ConfigError, match="inside the git repository"):
        save_credentials(_creds(), repo / "credentials.json")

    assert not (repo / "credentials.json").exists()


def test_writes_outside_a_repo_are_allowed(tmp_path: Path) -> None:
    path = tmp_path / "elsewhere" / "credentials.json"
    save_credentials(_creds(), path)
    assert path.exists()


def test_save_leaves_no_temp_files(tmp_path: Path) -> None:
    """A stray .tmp beside the credentials would be an unprotected secret."""
    path = tmp_path / "credentials.json"
    save_credentials(_creds(), path)
    assert [p.name for p in tmp_path.iterdir()] == ["credentials.json"]


def test_overwrite_is_atomic_and_complete(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    save_credentials(_creds(refresh_token="old"), path)
    save_credentials(_creds(refresh_token="new-rotated"), path)

    # The whole file is replaced, not partially overwritten.
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["refresh_token"] == "new-rotated"
    assert load_credentials(path).refresh_token == "new-rotated"


def test_missing_file_explains_how_to_fix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="qbo-mcp-setup"):
        load_credentials(tmp_path / "nope.json")


def test_corrupt_file_explains_how_to_fix(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_credentials(path)


def test_incomplete_file_names_the_missing_fields(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"client_id": "x"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="client_secret, realm_id, refresh_token"):
        load_credentials(path)


def test_unknown_fields_survive_a_round_trip(tmp_path: Path) -> None:
    """Forward compatibility: don't silently drop fields we don't know yet."""
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "a", "client_secret": "b", "realm_id": "c",
                "refresh_token": "d", "future_field": "keep me",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_credentials(path)
    assert loaded.extra["future_field"] == "keep me"


def test_lock_is_released_after_use(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    with credentials_lock(path):
        assert path.with_suffix(".json.lock").exists()
    assert not path.with_suffix(".json.lock").exists()


def test_lock_is_released_even_if_the_body_raises(tmp_path: Path) -> None:
    """A crash mid-refresh must not wedge every future run."""
    path = tmp_path / "credentials.json"
    with pytest.raises(RuntimeError):
        with credentials_lock(path):
            raise RuntimeError("refresh blew up")
    assert not path.with_suffix(".json.lock").exists()


def test_stale_lock_is_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock from a process that died must not block forever."""
    from qbo_mcp import config

    path = tmp_path / "credentials.json"
    lock_path = path.with_suffix(".json.lock")
    lock_path.write_text("99999", encoding="utf-8")

    # Age the lock past the staleness threshold.
    old = time.time() - (config.LOCK_STALE_SECONDS + 10)
    import os

    os.utime(lock_path, (old, old))

    with credentials_lock(path):
        pass  # Acquired by breaking the stale lock rather than timing out.
    assert not lock_path.exists()


def test_fresh_lock_blocks_until_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two live processes must not both refresh and invalidate each other."""
    from qbo_mcp import config

    monkeypatch.setattr(config, "LOCK_TIMEOUT_SECONDS", 0.3)
    path = tmp_path / "credentials.json"
    path.with_suffix(".json.lock").write_text("1", encoding="utf-8")

    with pytest.raises(ConfigError, match="Timed out waiting"):
        with credentials_lock(path):
            pass
