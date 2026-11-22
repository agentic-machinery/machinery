"""Explicit Git credentials for exact registry HTTPS destinations.

Git URL credential scopes match path prefixes. The helper additionally checks
the full credential context before delegating to gh's existing authenticated
store. Only gh and Git handle credential bytes, over their private pipe.

Dual-slug transfer bridge (PLAN NEW-ORG-1122 A01):
Rows declaring ``alternate_github`` also declare immutable ``repository_id`` and
explicit ``visibility``. The one active slug for each dual-slug row comes from
the host-owned, read-only selector file named by ``LU_FLEET_REPO_SELECTOR_FILE``
(outside any git checkout or writable worktree). Resolution fails closed on a
missing, unreadable, unknown, or worker-writable selector and never defaults to
the old slug. Only the active slug receives credentials.

``repository_registry`` is ``@cache``d in-process: restart long-running
processes after flipping a selector or updating ``fleet_repos.yaml``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shlex
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from lu_machinery.opsec.prepublish import PublishBlocked, real_gh

CONFIG = Path(__file__).resolve().parents[1] / "config" / "fleet_repos.yaml"
SELECTOR_ENV = "LU_FLEET_REPO_SELECTOR_FILE"
_MAX_SELECTOR_BYTES = 65536
_SLUG_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*")
_VALID_VISIBILITIES = frozenset({"public", "private"})
_WORKTREE_ROOT_ENVS = (
    "LU_PRODUCT_ROOT",
    "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT",
    "LEARN_UKRAINIAN_STATE_ROOT",
)


class SelectorError(ValueError):
    """Fail-closed refusal when the host-owned dual-slug selector cannot be trusted."""


@dataclass(frozen=True)
class SelectorState:
    """Validated host-owned active slug selections and their raw-bytes SHA-256 digest."""

    active_slugs: Mapping[str, str]
    digest: str

    def value_for(self, key: str) -> str:
        if key not in self.active_slugs:
            raise SelectorError(f"fleet repository selector missing active slug for {key!r}")
        return self.active_slugs[key]

    def receipt(self, key: str | None = None) -> dict[str, Any]:
        """Return receipt metadata (value and sha256 digest), never exposing host paths."""
        if key is not None:
            return {
                "selector_value": self.value_for(key),
                "selector_sha256": self.digest,
            }
        return {
            "selector_values": dict(sorted(self.active_slugs.items())),
            "selector_sha256": self.digest,
        }


class _UniqueSafeLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently keeping the last value."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
            seen.add(key)
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "unhashable key", node.start_mark) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(None, None, "duplicate key", node.start_mark)
    return dict(loader.construct_mapping(node, deep=deep))


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _is_valid_slug(value: Any) -> bool:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        return False
    owner, name = value.split("/", 1)
    return owner not in {".", ".."} and name not in {".", ".."}


def _is_valid_repository_id(value: Any) -> bool:
    return type(value) is int and value > 0


def _is_valid_visibility(value: Any) -> bool:
    return isinstance(value, str) and value in _VALID_VISIBILITIES


def _current_euid() -> int:
    return os.geteuid()


def _is_readonly_mount(fd: int) -> bool:
    st_rdonly = getattr(os, "ST_RDONLY", 0)
    if not st_rdonly:
        return False
    return bool(os.fstatvfs(fd).f_flag & st_rdonly)


def _is_writable_by_current_user(info: os.stat_result, path: Path) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        return True
    euid = _current_euid()
    if info.st_uid == euid and (mode & stat.S_IWUSR):
        return True
    if euid == 0:
        return False
    if os.access in os.supports_effective_ids:
        return bool(os.access(path, os.W_OK, effective_ids=True))
    return bool(os.access(path, os.W_OK))


def _assert_ancestors_not_worker_controlled(parent: Path, *, euid: int) -> None:
    """Ensure no ancestor above a distinct-owner selector can be swapped by the worker."""
    for ancestor in (parent, *parent.parents):
        try:
            anc_st = os.lstat(ancestor)
        except OSError:
            raise SelectorError("fleet repository selector ancestor directory is unreadable") from None
        if stat.S_ISLNK(anc_st.st_mode) or not stat.S_ISDIR(anc_st.st_mode):
            raise SelectorError("fleet repository selector ancestor must be a real directory")
        mode = stat.S_IMODE(anc_st.st_mode)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SelectorError("fleet repository selector ancestor directory is group- or world-writable")
        if euid != 0 and anc_st.st_uid == euid:
            raise SelectorError("fleet repository selector ancestor directory is owned by the worker uid")
        if euid != 0 and _is_writable_by_current_user(anc_st, ancestor):
            raise SelectorError("fleet repository selector ancestor directory is writable")


def _validate_selector_path_shape(raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise SelectorError(
            f"fleet repository selector unavailable: set {SELECTOR_ENV} to a host-owned read-only file"
        )
    if raw_path != raw_path.strip():
        raise SelectorError("fleet repository selector path has leading or trailing whitespace")
    if "\x00" in raw_path or any(unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"} for ch in raw_path):
        raise SelectorError("fleet repository selector path contains control or formatting characters")
    if not os.path.isabs(raw_path):
        raise SelectorError("fleet repository selector path must be absolute")
    path = Path(raw_path)
    if ".." in path.parts or not path.name or path.parent == path:
        raise SelectorError("fleet repository selector path must name a file without parent traversal")
    for part in (path, *path.parents):
        try:
            if part.is_symlink():
                raise SelectorError("fleet repository selector must not traverse or be a symlink")
        except OSError:
            raise SelectorError("fleet repository selector path is unreadable") from None
    return path


def _assert_outside_worktrees(selector_path: Path, env: Mapping[str, str]) -> None:
    try:
        resolved = selector_path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise SelectorError("fleet repository selector path cannot be resolved") from None
    for ancestor in resolved.parents:
        if ancestor.name == ".worktrees" or os.path.lexists(ancestor / ".git"):
            raise SelectorError("fleet repository selector must live outside any git checkout or worktree")
    for var in _WORKTREE_ROOT_ENVS:
        raw_root = env.get(var)
        if not raw_root:
            continue
        if not isinstance(raw_root, str) or not os.path.isabs(raw_root) or "\x00" in raw_root:
            raise SelectorError("configured repository root is invalid for selector containment check")
        try:
            root_path = Path(raw_root).resolve(strict=False)
            with contextlib.suppress(FileNotFoundError):
                os.stat(root_path)
        except (OSError, RuntimeError):
            raise SelectorError("configured repository root cannot be resolved for selector containment check") from None
        if resolved == root_path or resolved.is_relative_to(root_path):
            raise SelectorError("fleet repository selector must live outside configured repository roots")


def _read_host_selector_bytes(selector_path: Path) -> bytes:
    parent = selector_path.parent
    euid = _current_euid()
    try:
        parent_lstat = os.lstat(parent)
    except FileNotFoundError:
        raise SelectorError("fleet repository selector directory is missing") from None
    except OSError:
        raise SelectorError("fleet repository selector directory is unreadable") from None
    if stat.S_ISLNK(parent_lstat.st_mode) or not stat.S_ISDIR(parent_lstat.st_mode):
        raise SelectorError("fleet repository selector parent must be a real directory")

    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        dir_fd = os.open(parent, dir_flags)
    except FileNotFoundError:
        raise SelectorError("fleet repository selector directory is missing") from None
    except OSError:
        raise SelectorError("fleet repository selector directory is unreadable") from None

    try:
        dir_st = os.fstat(dir_fd)
        if (dir_st.st_dev, dir_st.st_ino) != (parent_lstat.st_dev, parent_lstat.st_ino):
            raise SelectorError("fleet repository selector directory changed during validation")
        if not stat.S_ISDIR(dir_st.st_mode):
            raise SelectorError("fleet repository selector parent must be a real directory")
        if dir_st.st_uid not in (0, euid):
            raise SelectorError("fleet repository selector directory has untrusted owner")
        if _is_writable_by_current_user(dir_st, parent):
            raise SelectorError("fleet repository selector directory is writable")

        try:
            file_lstat = os.lstat(selector_path.name, dir_fd=dir_fd)
        except FileNotFoundError:
            raise SelectorError("fleet repository selector is missing") from None
        except OSError:
            raise SelectorError("fleet repository selector is unreadable") from None
        if stat.S_ISLNK(file_lstat.st_mode) or not stat.S_ISREG(file_lstat.st_mode):
            raise SelectorError("fleet repository selector must be a regular file")

        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            file_fd = os.open(selector_path.name, file_flags, dir_fd=dir_fd)
        except FileNotFoundError:
            raise SelectorError("fleet repository selector is missing") from None
        except OSError:
            raise SelectorError("fleet repository selector is unreadable") from None

        try:
            file_st = os.fstat(file_fd)
            if (file_st.st_dev, file_st.st_ino) != (file_lstat.st_dev, file_lstat.st_ino):
                raise SelectorError("fleet repository selector changed during validation")
            if not stat.S_ISREG(file_st.st_mode):
                raise SelectorError("fleet repository selector must be a regular file")
            if file_st.st_uid not in (0, euid):
                raise SelectorError("fleet repository selector has untrusted owner")
            if _is_writable_by_current_user(file_st, selector_path):
                raise SelectorError("fleet repository selector is writable")
            if euid != 0 and (file_st.st_uid == euid or dir_st.st_uid == euid):
                if not (_is_readonly_mount(file_fd) and _is_readonly_mount(dir_fd)):
                    raise SelectorError(
                        "fleet repository selector is owned by the worker uid on a writable filesystem"
                    )
            else:
                _assert_ancestors_not_worker_controlled(parent, euid=euid)
            if file_st.st_size > _MAX_SELECTOR_BYTES:
                raise SelectorError("fleet repository selector exceeds size limit")
            os.set_blocking(file_fd, True)
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = -1
                raw = handle.read(_MAX_SELECTOR_BYTES + 1)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
    finally:
        os.close(dir_fd)

    if len(raw) > _MAX_SELECTOR_BYTES:
        raise SelectorError("fleet repository selector exceeds size limit")
    if b"\x00" in raw:
        raise SelectorError("fleet repository selector contains NUL bytes")
    return raw


@cache
def repository_registry(path: Path) -> dict[str, dict[str, Any]]:
    """Cache registry row types; each reader requires the fields it uses.

    Cached in-process with ``@cache``: long-running processes must restart after
    a registry update or dual-slug selector flip.
    """
    try:
        registry = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueSafeLoader)
    except yaml.YAMLError:
        raise ValueError("invalid fleet repository registry") from None
    repos = registry.get("repos") if isinstance(registry, dict) else None
    if not isinstance(repos, dict) or not repos:
        raise ValueError("invalid fleet repository registry")
    seen_slugs: set[str] = set()
    seen_ids: set[int] = set()
    for key, entry in repos.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(entry, dict)
            or not isinstance(entry.get("github", ""), str)
            or entry.get("transport", "ssh") not in ("file", "https", "ssh")
            or not isinstance(entry.get("local_name", ""), str)
            or type(entry.get("default", False)) is not bool
        ):
            raise ValueError("invalid fleet repository registry")

        role = entry.get("role", "")
        if role is not None and not isinstance(role, str):
            raise ValueError("invalid fleet repository registry")
        role_str = role or ""

        repo_id = entry.get("repository_id")
        if repo_id is not None:
            if not _is_valid_repository_id(repo_id) or repo_id in seen_ids:
                raise ValueError("invalid fleet repository registry")
            seen_ids.add(repo_id)

        visibility = entry.get("visibility")
        if visibility is not None:
            if not _is_valid_visibility(visibility):
                raise ValueError("invalid fleet repository registry")
            if role_str.startswith("private-") and visibility != "private":
                raise ValueError("invalid fleet repository registry")
            if role_str.startswith("public-") and visibility != "public":
                raise ValueError("invalid fleet repository registry")
            if entry.get("default", False) and visibility != "public":
                raise ValueError("invalid fleet repository registry")

        github = entry.get("github", "")
        if github:
            folded = github.casefold()
            if folded in seen_slugs:
                raise ValueError("invalid fleet repository registry")
            seen_slugs.add(folded)

        alternate = entry.get("alternate_github")
        if alternate is not None:
            if (
                not _is_valid_slug(github)
                or not _is_valid_slug(alternate)
                or alternate.casefold() == github.casefold()
                or entry.get("transport", "ssh") not in ("https", "ssh")
                or not _is_valid_repository_id(repo_id)
                or not _is_valid_visibility(visibility)
            ):
                raise ValueError("invalid fleet repository registry")
            alt_folded = alternate.casefold()
            if alt_folded in seen_slugs:
                raise ValueError("invalid fleet repository registry")
            seen_slugs.add(alt_folded)
    return repos


def baked_slugs(repository: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the one or two baked owner/name slugs declared on *repository*."""
    github = repository.get("github")
    if not isinstance(github, str):
        raise ValueError("invalid registered repository identity")
    alternate = repository.get("alternate_github")
    if alternate is None:
        return (github,)
    if not _is_valid_slug(github) or not _is_valid_slug(alternate) or alternate.casefold() == github.casefold():
        raise ValueError("invalid registered repository identity")
    return (github, alternate)


def load_selector(
    *,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SelectorState:
    """Load and validate the host-owned dual-slug selector; always fails closed."""
    source_env = os.environ if env is None else env
    selector_path = _validate_selector_path_shape(source_env.get(SELECTOR_ENV))
    _assert_outside_worktrees(selector_path, source_env)
    raw_bytes = _read_host_selector_bytes(selector_path)
    digest = hashlib.sha256(raw_bytes).hexdigest()

    try:
        text = raw_bytes.decode("utf-8")
        document = yaml.load(text, Loader=_UniqueSafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError):
        raise SelectorError("fleet repository selector is malformed") from None

    if not isinstance(document, dict) or set(document.keys()) != {"version", "active_slugs"}:
        raise SelectorError("fleet repository selector must contain only version and active_slugs")
    if type(document["version"]) is not int or document["version"] != 1:
        raise SelectorError("fleet repository selector version must be 1")
    active_raw = document["active_slugs"]
    if not isinstance(active_raw, dict) or not all(isinstance(k, str) for k in active_raw):
        raise SelectorError("fleet repository selector active_slugs must be a string-keyed mapping")

    cfg_path = (registry_path or CONFIG).resolve()
    repos = repository_registry(cfg_path)
    dual_rows = {key: entry for key, entry in repos.items() if entry.get("alternate_github") is not None}

    unknown_keys = sorted(set(active_raw.keys()) - set(dual_rows.keys()))
    if unknown_keys:
        raise SelectorError(f"fleet repository selector contains unknown key(s): {', '.join(map(repr, unknown_keys))}")
    missing_keys = sorted(set(dual_rows.keys()) - set(active_raw.keys()))
    if missing_keys:
        raise SelectorError(
            f"fleet repository selector missing active slug for: {', '.join(map(repr, missing_keys))}"
        )

    validated: dict[str, str] = {}
    for key, entry in dual_rows.items():
        allowed = baked_slugs(entry)
        chosen = active_raw.get(key)
        if not isinstance(chosen, str) or chosen not in allowed:
            raise SelectorError(f"fleet repository selector chose unknown slug for {key!r}")
        validated[key] = chosen
    return SelectorState(active_slugs=MappingProxyType(validated), digest=digest)


def active_slug(
    repository: Mapping[str, Any],
    *,
    key: str | None = None,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, SelectorState | None]:
    """Return ``(active_slug, selector_state)`` for *repository*.

    Single-slug rows require no selector. Dual-slug rows require a valid
    host-owned selector and never default to the old slug.
    """
    slugs = baked_slugs(repository)
    if len(slugs) == 1:
        return slugs[0], None

    cfg_path = (registry_path or CONFIG).resolve()
    selector = load_selector(registry_path=cfg_path, env=env)
    row_key = key or (repository.get("key") if isinstance(repository.get("key"), str) else None)
    if row_key is None:
        repos = repository_registry(cfg_path)
        matches = [
            candidate_key
            for candidate_key, candidate_entry in repos.items()
            if candidate_entry.get("alternate_github") is not None and baked_slugs(candidate_entry) == slugs
        ]
        if len(matches) != 1:
            raise SelectorError("cannot identify dual-slug row in registry")
        row_key = matches[0]
    chosen = selector.value_for(row_key)
    if chosen not in slugs:
        raise SelectorError(f"fleet repository selector chose unknown slug for {row_key!r}")
    return chosen, selector


def verify_authenticated_repository(
    payload: Mapping[str, Any],
    *,
    expected_slug: str,
    expected_repository_id: int | None = None,
    expected_visibility: str | None = None,
) -> None:
    """Verify authenticated GitHub repository ``full_name``, ``id``, and ``visibility``."""
    if not isinstance(payload, Mapping):
        raise ValueError("authenticated repository payload must be a mapping")
    full_name = payload.get("full_name")
    if not isinstance(full_name, str) or not full_name or full_name != expected_slug:
        raise ValueError("authenticated repository full_name mismatch")
    if expected_repository_id is not None:
        if not _is_valid_repository_id(expected_repository_id):
            raise ValueError("invalid expected repository_id")
        actual_id = payload.get("id")
        if not _is_valid_repository_id(actual_id) or actual_id != expected_repository_id:
            raise ValueError("authenticated repository id mismatch")
    if expected_visibility is not None:
        if not _is_valid_visibility(expected_visibility):
            raise ValueError("invalid expected visibility")
        expected_private = expected_visibility == "private"
        has_visibility = "visibility" in payload and payload["visibility"] is not None
        has_private = "private" in payload and payload["private"] is not None
        if not has_visibility and not has_private:
            raise ValueError("authenticated repository visibility missing")
        if has_visibility:
            actual_visibility = payload["visibility"]
            if not isinstance(actual_visibility, str) or actual_visibility != expected_visibility:
                raise ValueError("authenticated repository visibility mismatch")
        if has_private:
            actual_private = payload["private"]
            if type(actual_private) is not bool or actual_private != expected_private:
                raise ValueError("authenticated repository visibility mismatch")


def _origins_for_slug(slug: str, transport: str) -> tuple[str, ...]:
    if transport == "file" and Path(slug).is_absolute():
        return (slug,)
    if not _is_valid_slug(slug):
        raise ValueError("invalid registered repository identity")
    if transport not in ("https", "ssh"):
        raise ValueError("unsupported registered transport")
    bases = (f"https://github.com/{slug}", f"git@github.com:{slug}", f"ssh://git@github.com/{slug}")
    return tuple(base + suffix for base in bases for suffix in (".git", ""))


def canonical_origins(
    repository: dict[str, Any],
    *,
    key: str | None = None,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Derive exact destinations from registry identity, not clone transport.

    For a dual-slug bridge row, only the host-selected active slug's origins
    are returned; missing or untrusted selectors fail closed.
    """
    github = repository.get("github")
    if not isinstance(github, str):
        raise ValueError("invalid registered repository identity")
    transport = repository.get("transport", "ssh")
    if repository.get("alternate_github") is not None:
        chosen, _ = active_slug(repository, key=key, registry_path=registry_path, env=env)
        return _origins_for_slug(chosen, transport)
    return _origins_for_slug(github, transport)


def _https_origins_for_slug(slug: str) -> tuple[str, str]:
    return (f"https://github.com/{slug}.git", f"https://github.com/{slug}")


def _build_https_credential_settings(remote: str) -> list[str]:
    try:
        gh = shlex.quote(real_gh(os.environ))
    except PublishBlocked:
        return []
    path = shlex.quote(remote.removeprefix("https://github.com/"))
    # Reconstruct only the validated context. Do not forward store/erase or
    # arbitrary helper input to gh, and do not expose its diagnostics to records.
    helper = (
        '!f() { [ "$1" = get ] || exit 0; protocol= host= path=; '
        'while IFS="=" read -r key value; do '
        'case "$key" in protocol) protocol=$value;; host) host=$value;; path) path=$value;; esac; done; '
        '[ "$protocol" = https ] && [ "$host" = github.com ] && '
        f'[ "$path" = {path} ] || exit 0; '
        f"printf '%s\\n' protocol=https host=github.com path={path} '' | {gh} auth git-credential get 2>/dev/null; "
        '}; f'
    )
    return [
        "credential.helper=",
        "credential.interactive=false",
        f"credential.{remote}.useHttpPath=true",
        f"credential.{remote}.helper=",
        f"credential.{remote}.helper={helper}",
        "http.followRedirects=false",
        f"http.{remote}.followRedirects=false",
    ]


def verification_credentials(remote: str) -> list[str]:
    """Return command-scope settings; never inspect, copy or persist a token.

    Non-HTTPS and unregistered URLs gain no helper. Dual-slug bridge rows
    resolve the host-owned selector (failing closed on missing, unreadable,
    unknown, or worker-writable selectors) and grant credentials only to the
    active slug. Redirects stay refused.
    """
    if not remote.startswith("https://github.com/"):
        return []
    repositories = repository_registry(CONFIG)
    remote_folded = remote.casefold()
    for row_key, repository in repositories.items():
        if repository.get("alternate_github") is None:
            if remote in canonical_origins(repository):
                return _build_https_credential_settings(remote)
            continue
        candidate_origins = {
            origin.casefold()
            for slug in baked_slugs(repository)
            for origin in _https_origins_for_slug(slug)
        }
        if remote_folded in candidate_origins:
            chosen, _ = active_slug(repository, key=row_key, registry_path=CONFIG)
            if remote in _https_origins_for_slug(chosen):
                return _build_https_credential_settings(remote)
            return []
    return []
