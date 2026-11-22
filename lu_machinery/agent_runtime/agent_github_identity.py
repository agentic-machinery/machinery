"""Resolve the least-privileged GitHub identity for dispatched agents.

The operator's interactive GitHub token must never be the normal credential
available to an agent shell.  A repository-scoped GitHub App installation
token is preferred, followed by an explicitly provisioned agent token.  The
legacy operator token route remains only to keep existing dispatches fluid
while App setup is rolled out.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt

from lu_machinery.common import json_depth
from lu_machinery.common.github_client import http_open
from lu_machinery.common.repo_root import ProductRootError, product_root

_GITHUB_API_URL = "https://api.github.com"
_GIT_TIMEOUT_SECONDS = 30
_GITHUB_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}$")
_INSTALLATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SCP_AUTHORITY_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_INSTALLATION_CACHE_TTL_SECONDS = 300.0
_INSTALLATION_CACHE_MAX_ENTRIES = 64
_INSTALLATION_CACHE_LOCK = threading.Lock()
_INSTALLATION_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}


class GitHubIdentityError(RuntimeError):
    """Raised when configured agent identity material cannot be used safely."""


class RescueIdentityError(GitHubIdentityError):
    """Fixed item/category diagnostics, with no host or credential values."""

    def __init__(self, *missing: str):
        self.missing = missing
        super().__init__("rescue identity unavailable: " + ", ".join(missing))


def _validate_rescue_item(info: os.stat_result, item: str, *, directory: bool = False) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise RescueIdentityError(f"{item}:symlink")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise RescueIdentityError(f"{item}:wrong_type")
    if info.st_uid != os.getuid():
        raise RescueIdentityError(f"{item}:wrong_owner")
    if stat.S_IMODE(info.st_mode) & (0o022 if directory else 0o077):
        raise RescueIdentityError(f"{item}:wrong_mode")


def _read_rescue_item(path: str | Path, item: str, *, dir_fd: int | None = None) -> str:
    """Read only a private, owned regular file through a checked descriptor."""
    try:
        info = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        _validate_rescue_item(info, item)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        try:
            opened = os.fstat(fd)
            _validate_rescue_item(opened, item)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise RescueIdentityError(f"{item}:changed")
            with os.fdopen(fd, encoding="utf-8") as stream:
                fd = None
                return stream.read()
        finally:
            if fd is not None:
                os.close(fd)
    except FileNotFoundError:
        raise RescueIdentityError(f"{item}:missing") from None
    except (OSError, UnicodeError):
        raise RescueIdentityError(f"{item}:unreadable") from None


def _rescue_app_fields(env: Mapping[str, str]) -> dict[str, str] | None:
    """Load rescue-only provisioning without changing the process environment."""
    fields = {
        "app-id": env.get("LU_AGENT_GITHUB_APP_ID", ""),
        "installation-id": env.get("LU_AGENT_GITHUB_APP_INSTALLATION_ID", ""),
        "private-key.pem": env.get("LU_AGENT_GITHUB_APP_PRIVATE_KEY", ""),
    }
    key_file = env.get("LU_AGENT_GITHUB_APP_PRIVATE_KEY_FILE")
    if any(fields.values()) or key_file:
        if key_file and not fields["private-key.pem"]:
            fields["private-key.pem"] = _read_rescue_item(key_file, "private-key.pem")
        missing = [f"{item}:missing" for item in ("app-id", "private-key.pem") if not fields[item]]
        if missing:
            raise RescueIdentityError(*missing)
        return fields

    config_home = Path(env.get("XDG_CONFIG_HOME") or Path(env.get("HOME") or Path.home()) / ".config")
    directory = config_home / "learn-ukrainian" / "agent-github-app"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise RescueIdentityError("app-directory:unreadable") from None
    _validate_rescue_item(info, "app-directory", directory=True)
    has_installation_id = False
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            opened = os.fstat(fd)
            _validate_rescue_item(opened, "app-directory", directory=True)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise RescueIdentityError("app-directory:changed")
            present = set(os.listdir(fd))
            if present - set(fields):
                raise RescueIdentityError("app-directory:unexpected_items")
            for item in ("app-id", "private-key.pem"):
                fields[item] = _read_rescue_item(item, item, dir_fd=fd)
            if "installation-id" in present:
                has_installation_id = True
                fields["installation-id"] = _read_rescue_item("installation-id", "installation-id", dir_fd=fd)
        finally:
            os.close(fd)
    except OSError:
        raise RescueIdentityError("app-directory:unreadable") from None
    items_to_validate = ("app-id", "installation-id") if has_installation_id else ("app-id",)
    for item in items_to_validate:
        value = fields[item]
        if not value.removesuffix("\n").isascii() or not value.removesuffix("\n").isdigit():
            raise RescueIdentityError(f"{item}:invalid")
        fields[item] = value.removesuffix("\n")
    if not fields["private-key.pem"]:
        raise RescueIdentityError("private-key.pem:invalid")
    return fields


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class GitHubIdentity:
    """A resolved token and its deliberately non-secret provenance."""

    token: str | None = field(repr=False)
    source: str | None


def _read_legacy_token(path: Path) -> str | None:
    """Read a legacy shell token without evaluating shell source."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines:
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if parts[:1] == ["export"]:
            parts = parts[1:]
        for part in parts:
            for name in ("GH_TOKEN", "GITHUB_TOKEN"):
                prefix = f"{name}="
                if part.startswith(prefix):
                    value = part.removeprefix(prefix)
                    if value:
                        return value
    return None


def _validate_slug_segment(segment: str) -> str:
    if segment in {".", ".."} or not _GITHUB_SEGMENT_RE.fullmatch(segment):
        raise GitHubIdentityError("invalid repository identity for the GitHub App token")
    return segment


def _split_repository(repository: str) -> tuple[str | None, str]:
    if not isinstance(repository, str) or not repository:
        raise GitHubIdentityError("invalid repository identity for the GitHub App token")
    if "/" not in repository:
        return None, _validate_slug_segment(repository)
    parts = repository.split("/")
    if len(parts) != 2:
        raise GitHubIdentityError("invalid repository identity for the GitHub App token")
    return _validate_slug_segment(parts[0]), _validate_slug_segment(parts[1])


def _parse_remote_slug(remote: str) -> str:
    if (
        not isinstance(remote, str)
        or not remote
        or not remote.isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in remote)
        or any(c in remote for c in ("?", "#", "\\", "%"))
    ):
        raise GitHubIdentityError("cannot determine the repository for the GitHub App token")
    if "://" in remote:
        try:
            parsed = urlsplit(remote)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            raise GitHubIdentityError("cannot determine the repository for the GitHub App token") from None
        if (
            parsed.scheme not in {"https", "ssh"}
            or not host
            or ".." in host
            or not _HOST_RE.fullmatch(host)
            or parsed.password is not None
            or (parsed.scheme == "https" and parsed.username is not None)
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
        ):
            raise GitHubIdentityError("cannot determine the repository for the GitHub App token")
        raw_path = parsed.path[1:]
    elif ":" in remote:
        authority, raw_path = remote.split(":", 1)
        if (
            not authority
            or "/" in authority
            or ".." in authority
            or not _SCP_AUTHORITY_RE.fullmatch(authority)
            or raw_path.startswith("/")
        ):
            raise GitHubIdentityError("cannot determine the repository for the GitHub App token")
    else:
        raise GitHubIdentityError("cannot determine the repository for the GitHub App token")
    if raw_path.endswith("/"):
        raw_path = raw_path[:-1]
    if not raw_path or raw_path.endswith("/") or "//" in raw_path:
        raise GitHubIdentityError("cannot determine the repository for the GitHub App token")
    if raw_path.endswith(".git"):
        raw_path = raw_path[:-4]
    parts = raw_path.split("/")
    if len(parts) != 2:
        raise GitHubIdentityError("cannot determine the repository for the GitHub App token")
    try:
        owner = _validate_slug_segment(parts[0])
        name = _validate_slug_segment(parts[1])
    except GitHubIdentityError:
        raise GitHubIdentityError("cannot determine the repository for the GitHub App token") from None
    return f"{owner}/{name}"


def _repository_slug(repo_root: Path | None = None, *, env: Mapping[str, str] | None = None) -> str:
    """Return the origin owner/repository slug for an installation-token scope."""
    try:
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_root or product_root(env=env),
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout.removesuffix("\n")
    except (OSError, ProductRootError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise GitHubIdentityError("cannot determine the repository for the GitHub App token") from exc
    return _parse_remote_slug(remote)


def _repository_name(repo_root: Path | None = None) -> str:
    """Return the origin repository name for an installation-token scope."""
    return _repository_slug(repo_root).split("/", 1)[1]


def clear_installation_cache() -> None:
    """Clear the in-process per-owner GitHub App installation cache."""
    with _INSTALLATION_CACHE_LOCK:
        _INSTALLATION_CACHE.clear()


def _build_app_jwt(*, app_id: str, private_key: str, issued_at: int) -> str:
    signing_key = private_key.replace("\\n", "\n")
    return jwt.encode(
        {"iat": issued_at - 60, "exp": issued_at + 9 * 60, "iss": app_id},
        signing_key,
        algorithm="RS256",
    )


def _resolve_installation_id(
    *,
    app_id: str,
    assertion: str,
    owner: str,
    repo_name: str,
    api_base_url: str,
    ssl_context=None,
) -> str:
    """Resolve and cache the installation id covering ``owner/repo_name`` via App JWT."""
    base_url = api_base_url.rstrip("/")
    owner_key = owner.lower()
    cache_key = (app_id, base_url, owner_key)
    now_mono = time.monotonic()
    with _INSTALLATION_CACHE_LOCK:
        cached = _INSTALLATION_CACHE.get(cache_key)
        if cached is not None:
            cached_id, expires_at = cached
            if expires_at > now_mono:
                return cached_id
            _INSTALLATION_CACHE.pop(cache_key, None)

    request = urllib.request.Request(
        f"{base_url}/repos/{owner}/{repo_name}/installation",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {assertion}",
            "User-Agent": "learn-ukrainian-agent-runtime",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with http_open(request, opener=_restricted_opener(ssl_context).open, timeout=15) as response:
            if response.status != 200:
                raise ValueError("unexpected installation response")
            payload: dict[str, Any] = json_depth.loads(response.read().decode("utf-8"), max_depth=512)
        if not isinstance(payload, dict):
            raise ValueError("invalid installation response")
        raw_id = payload.get("id")
        if type(raw_id) is not int or raw_id <= 0:
            raise ValueError("invalid installation id")
        if "suspended_at" not in payload or payload["suspended_at"] is not None:
            raise ValueError("installation suspended")
        account = payload.get("account")
        login = account.get("login") if isinstance(account, dict) else None
        if not isinstance(login, str) or not login or login.lower() != owner_key:
            raise ValueError("installation owner mismatch")
        raw_app_id = payload.get("app_id")
        if type(raw_app_id) is not int or raw_app_id <= 0:
            raise ValueError("invalid installation app id")
        if "client_id" in payload:
            raw_client_id = payload["client_id"]
            if not isinstance(raw_client_id, str) or not raw_client_id:
                raise ValueError("invalid installation client id")
        if app_id.isascii() and app_id.isdigit():
            if str(raw_app_id) != app_id:
                raise ValueError("installation app mismatch")
        elif payload.get("client_id") != app_id:
            raise ValueError("installation client mismatch")
    except Exception:
        raise GitHubIdentityError("GitHub App installation lookup failed for repository owner") from None

    resolved_id = str(raw_id)
    expires_at = time.monotonic() + _INSTALLATION_CACHE_TTL_SECONDS
    with _INSTALLATION_CACHE_LOCK:
        now_store = time.monotonic()
        expired = [k for k, (_v, exp) in _INSTALLATION_CACHE.items() if exp <= now_store]
        for k in expired:
            _INSTALLATION_CACHE.pop(k, None)
        while len(_INSTALLATION_CACHE) >= _INSTALLATION_CACHE_MAX_ENTRIES:
            oldest = min(_INSTALLATION_CACHE.items(), key=lambda item: item[1][1])[0]
            _INSTALLATION_CACHE.pop(oldest, None)
        _INSTALLATION_CACHE[cache_key] = (resolved_id, expires_at)
    return resolved_id


def _restricted_opener(ssl_context=None):
    """Exclude ambient proxies, redirects and environment-selected TLS trust."""
    if ssl_context is None:
        trust = ssl.get_default_verify_paths()
        cafile = trust.openssl_cafile if Path(trust.openssl_cafile).is_file() else None
        capath = trust.openssl_capath if Path(trust.openssl_capath).is_dir() else None
        if not cafile and not capath:
            raise ValueError("default TLS trust unavailable")
        ssl_context = ssl.create_default_context(cafile=cafile, capath=capath)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl_context),
    )


def revoke_installation_token(
    token: str,
    *,
    api_base_url: str = _GITHUB_API_URL,
    ssl_context=None,
) -> None:
    """Revoke this installation credential without exposing response diagnostics."""
    try:
        request = urllib.request.Request(
            f"{api_base_url}/installation/token",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "learn-ukrainian-agent-runtime",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="DELETE",
        )
        with http_open(request, opener=_restricted_opener(ssl_context).open, timeout=15) as response:
            if response.status != 204:
                raise ValueError("unexpected revocation response")
    except Exception:
        raise GitHubIdentityError("GitHub App installation token revocation failed") from None


def mint_installation_token(
    *,
    app_id: str,
    private_key: str,
    installation_id: str | None = None,
    repository: str | None = None,
    now: int | None = None,
    permissions: Mapping[str, str] | None = None,
    api_base_url: str = _GITHUB_API_URL,
    ssl_context=None,
) -> str:
    """Mint a repository-scoped GitHub App installation token.

    The App's installation permissions define the ceiling.  This request adds
    no workflow, admin, or organization-secret permissions.  When
    ``installation_id`` is omitted, the installation covering the target
    ``owner/repository`` is resolved via the App JWT and cached per owner.
    """
    issued_at = int(time.time()) if now is None else now
    if (
        not isinstance(app_id, str)
        or not app_id
        or not app_id.isascii()
        or any(ord(c) <= 32 or ord(c) == 127 for c in app_id)
    ):
        raise GitHubIdentityError("invalid GitHub App id")
    selected = _repository_slug() if repository is None else repository
    owner, repo_name = _split_repository(selected)
    if permissions is not None and owner is None:
        raise GitHubIdentityError("GitHub App installation token scope or expiry refused")
    assertion = _build_app_jwt(app_id=app_id, private_key=private_key, issued_at=issued_at)
    base_url = api_base_url.rstrip("/")
    if installation_id is None:
        if owner is None:
            raise GitHubIdentityError("cannot determine repository owner for GitHub App installation")
        resolved_installation_id = _resolve_installation_id(
            app_id=app_id,
            assertion=assertion,
            owner=owner,
            repo_name=repo_name,
            api_base_url=base_url,
            ssl_context=ssl_context,
        )
    else:
        if not isinstance(installation_id, str) or not _INSTALLATION_ID_RE.fullmatch(installation_id):
            raise GitHubIdentityError("invalid GitHub App installation id")
        resolved_installation_id = installation_id
    request_body: dict[str, Any] = {"repositories": [repo_name]}
    if permissions is not None:
        request_body["permissions"] = dict(permissions)
    body = json.dumps(request_body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/app/installations/{resolved_installation_id}/access_tokens",
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {assertion}",
            "Content-Type": "application/json",
            "User-Agent": "learn-ukrainian-agent-runtime",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        open_request = (
            _restricted_opener(ssl_context).open
            if permissions is not None or ssl_context is not None or installation_id is None
            else urllib.request.urlopen
        )
        with http_open(request, opener=open_request, timeout=15) as response:
            payload: dict[str, Any] = json_depth.loads(response.read().decode("utf-8"), max_depth=512)
        if not isinstance(payload, dict):
            raise ValueError("invalid response")
    except Exception as exc:  # HTTP errors deliberately contain no token in the message.
        raise GitHubIdentityError("GitHub App installation token minting failed") from exc

    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise GitHubIdentityError("GitHub App installation token response did not contain a token")
    if permissions is not None:
        try:
            repositories = payload["repositories"]
            if not isinstance(repositories, list) or len(repositories) != 1:
                raise ValueError("invalid repository scope")
            if "/" not in selected or repositories[0]["full_name"] != selected:
                raise ValueError("invalid repository identity")
            granted = payload["permissions"]
            levels = {"read": 1, "write": 2, "admin": 3}
            ceilings = {"metadata": "read", **permissions}
            if (
                not isinstance(granted, dict)
                or any(
                    value not in levels or levels[value] > levels.get(ceilings.get(key), 0)
                    for key, value in granted.items()
                )
                or any(granted.get(key) != value for key, value in permissions.items())
            ):
                raise ValueError("invalid permissions")
            expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None or expires <= datetime.fromtimestamp(
                time.time() if now is None else issued_at, UTC
            ):
                raise ValueError("invalid expiry")
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise GitHubIdentityError("GitHub App installation token scope or expiry refused") from exc
    return token


def resolve_agent_github_identity(
    *,
    environment: Mapping[str, str] | None = None,
    bash_secrets_path: Path | None = None,
    repository: str | None = None,
    repo_root: Path | None = None,
    permissions: Mapping[str, str] | None = None,
    api_base_url: str = _GITHUB_API_URL,
    ssl_context=None,
    rescue: bool = False,
) -> GitHubIdentity:
    """Resolve App, dedicated-token, then temporary legacy identity.

    A partial App configuration is an operator setup error, rather than a
    reason to silently fall through to a broader credential.
    """
    env = os.environ if environment is None else environment
    if rescue:
        try:
            fields = _rescue_app_fields(env)
            if fields is not None:
                try:
                    if repository is not None:
                        selected_repository: str | None = repository
                    elif repo_root is not None:
                        selected_repository = _repository_slug(repo_root)
                    elif env.get("LU_PRODUCT_ROOT"):
                        selected_repository = _repository_slug(product_root(env=env))
                    else:
                        selected_repository = None
                    token = mint_installation_token(
                        app_id=fields["app-id"],
                        private_key=fields["private-key.pem"],
                        installation_id=None,
                        repository=selected_repository,
                        permissions={"contents": "write"},
                        api_base_url=api_base_url,
                        ssl_context=ssl_context,
                    )
                except Exception:
                    raise RescueIdentityError("app-token:mint_failed") from None
                return GitHubIdentity(token=token, source="app")
            token = env.get("LU_AGENT_GITHUB_TOKEN")
            if token:
                return GitHubIdentity(token=token, source="token")
            raise RescueIdentityError("app-directory:missing", "agent-token:missing")
        except RescueIdentityError as exc:
            missing = exc.missing
        # Raising outside the handler discards credential-bearing exception context.
        raise RescueIdentityError(*missing) from None
    private_key = env.get("LU_AGENT_GITHUB_APP_PRIVATE_KEY")
    key_file = env.get("LU_AGENT_GITHUB_APP_PRIVATE_KEY_FILE")
    if key_file and not private_key:
        try:
            info = os.stat(key_file, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise OSError("key file is not private and regular")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(key_file, flags)
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) & 0o077
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise OSError("key file changed or is not private and regular")
                with os.fdopen(fd, encoding="utf-8") as stream:
                    fd = None
                    private_key = stream.read()
            finally:
                if fd is not None:
                    os.close(fd)
        except (OSError, UnicodeError):
            raise GitHubIdentityError("cannot read the configured key file") from None
    raw_app_id = env.get("LU_AGENT_GITHUB_APP_ID")
    app_id = raw_app_id.strip() if isinstance(raw_app_id, str) else ""
    legacy_installation_id = env.get("LU_AGENT_GITHUB_APP_INSTALLATION_ID")
    if any((raw_app_id, private_key, legacy_installation_id)) or key_file:
        if not (app_id and private_key):
            raise GitHubIdentityError("incomplete LU_AGENT_GITHUB_APP_* configuration")
        if repository is not None:
            selected_repository = repository
        elif repo_root is not None:
            selected_repository = _repository_slug(repo_root)
        elif env.get("LU_PRODUCT_ROOT"):
            try:
                selected_repository = _repository_slug(product_root(env=env))
            except (GitHubIdentityError, ProductRootError):
                raise GitHubIdentityError("cannot determine the repository for the GitHub App token") from None
        else:
            selected_repository = None
        # Resolve per-owner via App JWT whenever the target names an owner (or
        # will be read from origin). Only bare legacy repository names without an
        # owner use LU_AGENT_GITHUB_APP_INSTALLATION_ID.
        installation_id: str | None = (
            legacy_installation_id
            if (
                permissions is None
                and isinstance(selected_repository, str)
                and selected_repository
                and "/" not in selected_repository
                and legacy_installation_id
            )
            else None
        )
        mint_kwargs: dict[str, Any] = {}
        if permissions is not None:
            mint_kwargs["permissions"] = permissions
            mint_kwargs["api_base_url"] = api_base_url
            mint_kwargs["ssl_context"] = ssl_context
        else:
            if api_base_url != _GITHUB_API_URL:
                mint_kwargs["api_base_url"] = api_base_url
            if ssl_context is not None:
                mint_kwargs["ssl_context"] = ssl_context
        return GitHubIdentity(
            token=mint_installation_token(
                app_id=app_id,
                private_key=private_key,
                installation_id=installation_id,
                repository=selected_repository,
                **mint_kwargs,
            ),
            source="app",
        )

    token = env.get("LU_AGENT_GITHUB_TOKEN")
    if token:
        return GitHubIdentity(token=token, source="token")

    legacy_token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not legacy_token:
        legacy_token = _read_legacy_token(bash_secrets_path or Path.home() / ".bash_secrets")
    if legacy_token:
        print(
            "WARNING: agent dispatch is using the operator GitHub identity; GitHub App setup is required.",
            file=sys.stderr,
        )
        return GitHubIdentity(token=legacy_token, source="legacy")
    return GitHubIdentity(token=None, source=None)
