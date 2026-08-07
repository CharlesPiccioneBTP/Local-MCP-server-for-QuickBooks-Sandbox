"""Credential storage.

Credentials live in the OS user-config directory, never in the project. That is
the actual mechanism behind "credentials never touch version control": the file
is not in the repo, so it cannot be committed. `refuse_if_in_git_work_tree` is
the guard that keeps it that way if someone later points CONFIG_DIR somewhere
convenient.

Two properties matter for unattended operation:

* Writes are atomic. Intuit rotates the refresh token on every refresh and kills
  the old value immediately, so a half-written credentials file is not a corrupt
  file -- it is a permanently disconnected integration.
* Writes are serialised across processes. Two servers refreshing concurrently
  would each invalidate the other's token.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

APP_NAME = "qbo-mcp"
CREDENTIALS_FILENAME = "credentials.json"

# How long to wait for another process to finish its refresh before giving up.
LOCK_TIMEOUT_SECONDS = 30.0
# A lock older than this is assumed to be from a process that died mid-refresh.
LOCK_STALE_SECONDS = 60.0


class ConfigError(RuntimeError):
    """Raised when credentials are missing, malformed, or unsafe to write."""


def config_dir() -> Path:
    """Directory holding credentials, outside any project checkout.

    Honours QBO_MCP_CONFIG_DIR so the test suite (and anyone with an unusual
    setup) can redirect it without touching real credentials.
    """
    override = os.environ.get("QBO_MCP_CONFIG_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")

    return Path(base) / APP_NAME


def credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILENAME


def refuse_if_in_git_work_tree(path: Path) -> None:
    """Abort if `path` sits inside a git repository.

    Credentials in a work tree are one `git add -A` away from being published.
    Rather than trusting .gitignore, refuse the write outright.
    """
    directory = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # No git available, or it misbehaved. Nothing to check against.
        return

    if result.returncode == 0:
        toplevel = result.stdout.strip()
        raise ConfigError(
            f"Refusing to write credentials to {path}: it is inside the git "
            f"repository at {toplevel}. Credentials must never live in a work "
            f"tree. Unset QBO_MCP_CONFIG_DIR to use the default location "
            f"({config_dir()}), or point it somewhere outside any repo."
        )


@dataclass
class Credentials:
    """Everything needed to reach one QuickBooks company, and nothing else."""

    client_id: str
    client_secret: str
    realm_id: str
    refresh_token: str
    environment: str = "sandbox"  # "sandbox" or "production"
    access_token: str = ""
    # Absolute unix timestamps; 0 means "unknown, refresh before use".
    access_token_expires_at: float = 0.0
    refresh_token_expires_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "Credentials":
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        missing = {"client_id", "client_secret", "realm_id", "refresh_token"} - data.keys()
        if missing:
            raise ConfigError(
                f"Credentials file is missing required field(s): "
                f"{', '.join(sorted(missing))}. Re-run: uv run qbo-mcp-setup"
            )
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["extra"] = {k: v for k, v in data.items() if k not in known and k != "extra"}
        return cls(**kwargs)


def load_credentials(path: Path | None = None) -> Credentials:
    path = path or credentials_path()
    if not path.exists():
        raise ConfigError(
            f"No credentials found at {path}.\n"
            f"Run this once to connect a QuickBooks company:\n"
            f"    uv run qbo-mcp-setup"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Credentials file at {path} is not valid JSON ({exc}). "
            f"Delete it and re-run: uv run qbo-mcp-setup"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Credentials file at {path} should contain a JSON object.")
    return Credentials.from_mapping(data)


def save_credentials(creds: Credentials, path: Path | None = None) -> None:
    """Write credentials atomically, then restrict them to the current user.

    The temp file is created in the destination directory so that os.replace is
    a same-filesystem rename, which is atomic on both Windows and POSIX.
    """
    path = path or credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    refuse_if_in_git_work_tree(path)

    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        # 0o600 from creation, so the secret is never briefly world-readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        # fdopen takes ownership of fd: on success the `with` closes it, and if
        # fdopen itself raises, fd would leak, so close it explicitly there.
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(creds.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

    _restrict_permissions(path)


def _restrict_permissions(path: Path) -> None:
    """Best-effort lockdown of the credentials file to the current user."""
    if sys.platform == "win32":
        # POSIX mode bits are near-meaningless on NTFS; rewrite the ACL so the
        # file is readable only by its owner.
        user = os.environ.get("USERNAME")
        if not user:
            return
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # Not fatal: the file is still outside the repo.
    else:
        try:
            path.chmod(0o600)
        except OSError:
            pass


@contextmanager
def credentials_lock(path: Path | None = None) -> Iterator[None]:
    """Serialise token refreshes across processes.

    Intuit invalidates the previous refresh token the moment a new one is
    issued, so two processes refreshing at once will leave one of them holding a
    dead token. Callers should re-read credentials *inside* this lock so they
    pick up a refresh another process just completed.
    """
    path = path or credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _lock_is_stale(lock_path):
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"Timed out waiting for the credentials lock at {lock_path}. "
                    f"If no other qbo-mcp process is running, delete that file."
                )
            time.sleep(0.1)

    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        lock_path.unlink(missing_ok=True)


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age > LOCK_STALE_SECONDS
