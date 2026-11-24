"""Allowlisted fleet repository resolution for multi-repo dispatch (#672 P2.1).

The public primary that owns ``scripts/dispatch.py`` remains the control-plane
root. ``--repo`` selects which sibling git checkout receives worktree creation
and GitHub targeting.

Dual-slug transfer bridge (PLAN NEW-ORG-1122 A01):
Moved rows declare ``alternate_github``, ``repository_id``, and explicit
``visibility``. Both baked slugs resolve to the same ``FleetRepo`` row. Active
slug selection comes from the host-owned, read-only selector file named by
``LU_FLEET_REPO_SELECTOR_FILE`` (failing closed on missing, unreadable, unknown,
or worker-writable selectors and never defaulting to the old slug).

``_repository_catalog`` and ``repository_registry`` are ``@cache``d in-process:
restart long-running processes after a registry or selector flip.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lu_machinery.agent_runtime.sources_read_only import SERVER_RELATIVE_PATH, printable_text
from lu_machinery.common.git_credentials import (
    SELECTOR_ENV,
    SelectorError,
    SelectorState,
    active_slug,
    load_selector,
    repository_registry,
    verify_authenticated_repository,
)
from lu_machinery.common.repo_root import PRODUCT_MARKER
from lu_machinery.orchestration.product_policy import POLICY_REL

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "fleet_repos.yaml"
_VALID_VISIBILITIES = frozenset({"public", "private"})
# A row carrying the Learn Ukrainian slug must declare both product files: it never gets consumer defaults.
_LEARN_UKRAINIAN_SLUG = "learn-ukrainian/learn-ukrainian.github.io"
_PRODUCT_FILE_CANONICAL: dict[str, str] = {
    "sources_server": SERVER_RELATIVE_PATH.as_posix(),
    "admission_policy": POLICY_REL,
}
# Local config keys read to identify a checkout. An include or a URL rewrite can make Git use an
# origin other than the literal remote.origin.url, so their presence refuses identification.
_CHECKOUT_IDENTITY_KEYS = r"^(remote\.origin\.url|include\..*|includeif\..*|url\..*)$"
_CHECKOUT_GIT_TIMEOUT_S = 10


@dataclass(frozen=True)
class FleetRepo:
    key: str
    github: str
    local_name: str
    role: str
    default: bool = False
    security_alerts: bool = True
    repository_id: int | None = None
    visibility: str | None = None
    alternate_github: str | None = None
    sources_server: str | None = None
    admission_policy: str | None = None

    @property
    def owner(self) -> str:
        return self.github.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.github.split("/", 1)[1]

    @property
    def slugs(self) -> tuple[str, ...]:
        if self.alternate_github is not None:
            return (self.github, self.alternate_github)
        return (self.github,)

    @property
    def has_alternate_slug(self) -> bool:
        return self.alternate_github is not None


@dataclass(frozen=True)
class ActiveFleetRepo:
    """A resolved registry row bound to its one active slug and selector receipt."""

    repo: FleetRepo
    active_github: str
    selector_value: str | None = None
    selector_digest: str | None = None

    @property
    def owner(self) -> str:
        return self.active_github.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.active_github.split("/", 1)[1]

    def selector_receipt(self) -> dict[str, str] | None:
        if self.selector_value is None or self.selector_digest is None:
            return None
        return {
            "selector_value": self.selector_value,
            "selector_sha256": self.selector_digest,
        }


class FleetRepoError(ValueError):
    """Unknown key, bad or unreadable catalog, invalid selector, or missing sibling checkout."""


class MissingFleetCheckout(FleetRepoError):
    """The catalog checkout is not present.

    Review resolution turns this into the neutral private refusal. Other
    catalog errors stay ``FleetRepoError`` so their text is preserved.
    """


def load_fleet_repos(path: Path | None = None) -> dict[str, FleetRepo]:
    cfg_path = path or _CONFIG_PATH
    try:
        repos_raw = repository_registry(cfg_path.resolve())
    except (ValueError, OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop on Python 3.12
        raise FleetRepoError(f"fleet_repos catalog unreadable: {cfg_path} ({type(exc).__name__}: {exc})") from exc
    out: dict[str, FleetRepo] = {}
    defaults = 0
    for key, body in repos_raw.items():
        github = body.get("github")
        local_name = body.get("local_name")
        role = body.get("role") or ""
        default = bool(body.get("default", False))
        security_alerts = body.get("security_alerts", True)
        repository_id = body.get("repository_id")
        visibility = body.get("visibility")
        alternate_github = body.get("alternate_github")
        product_files = {field: body[field] for field in _PRODUCT_FILE_CANONICAL if field in body}
        for field, value in product_files.items():
            if not isinstance(value, str) or not value.strip():
                raise FleetRepoError(f"fleet_repos[{key!r}].{field} must be a repo-relative file path")
            canonical = _PRODUCT_FILE_CANONICAL[field]
            if value != canonical:
                raise FleetRepoError(f"fleet_repos[{key!r}].{field} must be {canonical!r}")
        if not isinstance(github, str) or "/" not in github:
            raise FleetRepoError(f"fleet_repos[{key!r}].github must be owner/name")
        if not isinstance(local_name, str) or not local_name.strip():
            raise FleetRepoError(f"fleet_repos[{key!r}].local_name must be a non-empty string")
        if type(security_alerts) is not bool:
            raise FleetRepoError(f"fleet_repos[{key!r}].security_alerts must be a boolean")
        if repository_id is not None and (type(repository_id) is not int or repository_id <= 0):
            raise FleetRepoError(f"fleet_repos[{key!r}].repository_id must be a positive integer")
        if visibility is not None and (not isinstance(visibility, str) or visibility not in _VALID_VISIBILITIES):
            raise FleetRepoError(f"fleet_repos[{key!r}].visibility must be 'public' or 'private'")
        if alternate_github is not None:
            if not isinstance(alternate_github, str) or "/" not in alternate_github:
                raise FleetRepoError(f"fleet_repos[{key!r}].alternate_github must be owner/name")
            if repository_id is None or visibility is None:
                raise FleetRepoError(
                    f"fleet_repos[{key!r}] with alternate_github requires repository_id and visibility"
                )
        if _LEARN_UKRAINIAN_SLUG in {github.lower(), str(alternate_github).lower()}:
            for field in _PRODUCT_FILE_CANONICAL:
                if field not in product_files:
                    raise FleetRepoError(f"fleet_repos[{key!r}].{field} is required for Learn Ukrainian")
        if default:
            defaults += 1
        out[key] = FleetRepo(
            key=key,
            github=github,
            local_name=local_name.strip(),
            role=str(role),
            default=default,
            security_alerts=security_alerts,
            repository_id=repository_id,
            visibility=visibility,
            alternate_github=alternate_github,
            **product_files,
        )
    if defaults != 1:
        raise FleetRepoError("fleet_repos must mark exactly one default repo")
    return out


def default_repo_key(repos: Mapping[str, FleetRepo] | None = None) -> str:
    catalog = repos or load_fleet_repos()
    for key, repo in catalog.items():
        if repo.default:
            return key
    raise FleetRepoError("no default fleet repo")


def canonical_repo_key(key: str, repos: Mapping[str, FleetRepo] | None = None) -> str:
    """Return the one catalog key *key* spells.

    Surrounding whitespace and case are not distinct keys. An empty spelling
    is not the default. The error is raised before any checkout path exists.
    """
    catalog = repos if repos is not None else load_fleet_repos()
    known = ", ".join(sorted(catalog))
    if not isinstance(key, str) or not key.strip():
        raise FleetRepoError(f"unknown --repo ''; allowlisted: {known}")
    if key in catalog:
        return key
    stripped = key.strip()
    if stripped in catalog:
        return stripped
    matches = [candidate for candidate in catalog if candidate.casefold() == stripped.casefold()]
    if len(matches) == 1:
        return matches[0]
    raise FleetRepoError(f"unknown --repo {stripped!r}; allowlisted: {known}")


def resolve_fleet_repo_by_slug(
    identifier: str,
    *,
    repos: Mapping[str, FleetRepo] | None = None,
    expected_repository_id: int | None = None,
    expected_visibility: str | None = None,
) -> FleetRepo:
    """Resolve a registry key or either baked slug of a row to its one ``FleetRepo``.

    When *expected_repository_id* or *expected_visibility* is given, mismatches
    fail closed before any network call.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise FleetRepoError("unknown fleet repository identifier")
    catalog = repos if repos is not None else load_fleet_repos()
    spelling = identifier.strip().casefold()
    matches = [
        repo
        for repo in catalog.values()
        if spelling == repo.key.casefold() or any(spelling == slug.casefold() for slug in repo.slugs)
    ]
    if len(matches) != 1:
        raise FleetRepoError(f"unknown fleet repository {identifier.strip()!r}")
    repo = matches[0]
    if expected_repository_id is not None:
        if type(expected_repository_id) is not int or expected_repository_id <= 0:
            raise FleetRepoError("expected_repository_id must be a positive integer")
        if repo.repository_id is None or repo.repository_id != expected_repository_id:
            raise FleetRepoError(
                f"repository id mismatch for {repo.key!r}: expected {expected_repository_id}, "
                f"registered {repo.repository_id}"
            )
    if expected_visibility is not None:
        if expected_visibility not in _VALID_VISIBILITIES:
            raise FleetRepoError("expected_visibility must be 'public' or 'private'")
        if repo.visibility is None or repo.visibility != expected_visibility:
            raise FleetRepoError(
                f"repository visibility mismatch for {repo.key!r}: expected {expected_visibility!r}, "
                f"registered {repo.visibility!r}"
            )
    return repo


def _resolved_registry_path(registry_path: Path | None) -> Path:
    """Resolve *registry_path* (default: the bundled catalog) inside the ``FleetRepoError`` boundary."""
    path = registry_path or _CONFIG_PATH
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop on Python 3.12
        raise FleetRepoError(f"fleet_repos catalog unreadable: {path} ({type(exc).__name__}: {exc})") from exc


def load_fleet_repo_selector(
    *,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SelectorState:
    """Load the validated host-owned selector state, normalizing failures to ``FleetRepoError``."""
    cfg_path = _resolved_registry_path(registry_path)
    try:
        return load_selector(registry_path=cfg_path, env=env)
    except (SelectorError, ValueError, OSError) as exc:
        raise FleetRepoError(str(exc)) from exc


def selector_receipt(
    key: str | None = None,
    *,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return selector value and sha256 digest for receipts (adopted by A01b consumers)."""
    state = load_fleet_repo_selector(registry_path=registry_path, env=env)
    try:
        return state.receipt(key)
    except SelectorError as exc:
        raise FleetRepoError(str(exc)) from exc


def resolve_active_fleet_repo(
    identifier: str,
    *,
    repos: Mapping[str, FleetRepo] | None = None,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    expected_repository_id: int | None = None,
    expected_visibility: str | None = None,
) -> ActiveFleetRepo:
    """Resolve *identifier* (key or either baked slug) and bind dual-slug rows to the active slug.

    Dual-slug rows fail closed when the host-owned selector (``LU_FLEET_REPO_SELECTOR_FILE``)
    is missing, unreadable, unknown, or worker-writable, and never default to the old slug.
    """
    cfg_path = _resolved_registry_path(registry_path)
    catalog = repos if repos is not None else load_fleet_repos(cfg_path)
    repo = resolve_fleet_repo_by_slug(
        identifier,
        repos=catalog,
        expected_repository_id=expected_repository_id,
        expected_visibility=expected_visibility,
    )
    if not repo.has_alternate_slug:
        return ActiveFleetRepo(repo=repo, active_github=repo.github)
    entry: dict[str, Any] = {
        "key": repo.key,
        "github": repo.github,
        "alternate_github": repo.alternate_github,
        "repository_id": repo.repository_id,
        "visibility": repo.visibility,
    }
    try:
        chosen, state = active_slug(entry, key=repo.key, registry_path=cfg_path, env=env)
    except (SelectorError, ValueError, OSError) as exc:
        raise FleetRepoError(str(exc)) from exc
    if state is None:
        raise FleetRepoError(f"fleet repository selector required for dual-slug row {repo.key!r}")
    return ActiveFleetRepo(
        repo=repo,
        active_github=chosen,
        selector_value=chosen,
        selector_digest=state.digest,
    )


def verify_repository_identity(
    repo: FleetRepo,
    payload: Mapping[str, Any],
    *,
    active_github: str | None = None,
) -> None:
    """Verify an authenticated GitHub repository payload against *repo*."""
    expected_slug = repo.github if active_github is None else active_github
    if expected_slug not in repo.slugs:
        raise FleetRepoError(f"active slug {expected_slug!r} is not baked on row {repo.key!r}")
    try:
        verify_authenticated_repository(
            payload,
            expected_slug=expected_slug,
            expected_repository_id=repo.repository_id,
            expected_visibility=repo.visibility,
        )
    except ValueError as exc:
        raise FleetRepoError(str(exc)) from exc


def _missing_checkout_error(repo: FleetRepo, supplied_key: str | None, detail: str) -> MissingFleetCheckout:
    """Missing-checkout error for *supplied_key*.

    A non-canonical spelling of the private key is still that key. Callers
    that rewrite only the exact spelling would otherwise print *detail*,
    which names the checkout path.
    """
    if repo.key == "infra-private" and supplied_key != repo.key:
        from lu_machinery.review.target_resolution import PRIVATE_REVIEW_TARGET_UNRESOLVED

        return MissingFleetCheckout(PRIVATE_REVIEW_TARGET_UNRESOLVED)
    return MissingFleetCheckout(detail)


def resolve_fleet_repo(
    key: str | None,
    *,
    primary_root: Path,
    repos: Mapping[str, FleetRepo] | None = None,
    require_checkout: bool = True,
) -> tuple[FleetRepo, Path]:
    """Return ``(FleetRepo, absolute checkout path)`` for *key*.

    ``primary_root`` is the public Learn Ukrainian checkout that owns delegate.
    Sibling checkouts live at ``primary_root.parent / local_name``. The public
    entry resolves to ``primary_root`` itself even when ``local_name`` differs
    from the directory basename (worktree / rename safety).
    """
    catalog = repos or load_fleet_repos()
    resolved_key = default_repo_key(catalog) if key is None else canonical_repo_key(key, catalog)
    repo = catalog[resolved_key]
    primary = primary_root.resolve()
    checkout = primary if repo.default else (primary.parent / repo.local_name).resolve()
    if require_checkout and not (checkout / ".git").exists() and not checkout.is_dir():
        raise _missing_checkout_error(
            repo,
            key,
            f"--repo {repo.key} expects sibling checkout at {checkout}; "
            f"clone {repo.github} next to {primary.name} first",
        )
    if require_checkout:
        git_dir = checkout / ".git"
        if not git_dir.exists():
            raise _missing_checkout_error(
                repo,
                key,
                f"--repo {repo.key} path {checkout} is not a git checkout "
                f"(clone {repo.github} as ../{repo.local_name} beside the public primary)",
            )
    return repo, checkout


@cache
def _repository_catalog(path: Path) -> dict[str, FleetRepo]:
    """Cached catalog loader; restart long-running processes after a registry/selector flip."""
    return load_fleet_repos(path)


def _bundled_catalog() -> dict[str, FleetRepo]:
    """The cached bundled catalog; any failure to locate or read it is a ``FleetRepoError``."""
    return _repository_catalog(_resolved_registry_path(None))


def clear_repository_cache() -> None:
    """Reload registry edits on the next canonical repository lookup."""
    repository_registry.cache_clear()
    _repository_catalog.cache_clear()


def canonical_repository(value: str) -> str:
    """Canonical owner/name, accepting registry keys and both baked slug spellings."""
    if not isinstance(value, str) or not value.isascii():
        # Slugs and keys are ASCII; casefold would map look-alikes such as U+212A onto them.
        raise FleetRepoError("PR repository must be owner/name")
    spelling = value.strip().casefold()
    for repo in _bundled_catalog().values():
        known = {repo.key.casefold(), *(slug.casefold() for slug in repo.slugs)}
        if spelling in known:
            return repo.github.casefold()
    if re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", spelling):
        return spelling
    raise FleetRepoError("PR repository must be owner/name")


_EXACT_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def repository_slugs(spelling: object) -> tuple[str, ...]:
    """Every baked slug of the repository an exact owner/name *spelling* names.

    A spelling of either baked slug of a registry row (case-insensitive, as
    GitHub slugs are) returns that row's baked slugs, so a payload written
    before or after a transfer binds to the same repository. An unregistered
    owner/name returns only itself. Anything else (a registry key, a URL,
    surrounding whitespace, a non-string) names no repository and returns
    ``()``. An unreadable catalog raises ``FleetRepoError``.
    """
    if not isinstance(spelling, str) or not _EXACT_SLUG.fullmatch(spelling):
        return ()
    folded = spelling.casefold()
    for repo in _bundled_catalog().values():
        if any(folded == slug.casefold() for slug in repo.slugs):
            return repo.slugs
    return (spelling,)


def pull_request_repository(payload: Any, *, scoped_repository: str | None = None) -> str | None:
    """Read the PR's base repository, never its possibly forked head repository.

    A repository-scoped response can omit attribution. Explicit attribution
    always wins, including when it disagrees with the requested repository.
    Conflicting or malformed attribution is unknown, never deletion proof.
    """
    if not isinstance(payload, dict):
        raise FleetRepoError("PR payload is not an object")
    names: list[Any] = []
    base = payload.get("base")
    if isinstance(base, dict) and isinstance(base.get("repo"), dict):
        names.append(base["repo"].get("full_name"))
    repository = payload.get("repository")
    if repository is not None:
        names.append(repository.get("nameWithOwner") if isinstance(repository, dict) else repository)
    for field in ("url", "html_url"):
        if field in payload:
            if not isinstance(payload[field], str):
                raise FleetRepoError("PR payload has unusable repository attribution")
            url = urlsplit(payload[field])
            parts = url.path.strip("/").split("/")
            if url.hostname == "github.com" and len(parts) == 4 and parts[2] == "pull":
                names.append("/".join(parts[:2]))
            elif (
                url.hostname == "api.github.com"
                and len(parts) == 5
                and parts[0] == "repos"
                and parts[3] == "pulls"
            ):
                names.append("/".join(parts[1:3]))
            else:
                raise FleetRepoError("PR payload has unusable repository attribution")
    if any(not isinstance(name, str) or not name for name in names):
        raise FleetRepoError("PR payload has unusable repository attribution")
    canonical = {canonical_repository(name) for name in names}
    if len(canonical) > 1:
        raise FleetRepoError("PR payload has conflicting repository attribution")
    return next(iter(canonical)) if canonical else scoped_repository


_FDINFO_DIR = Path("/proc/self/fdinfo")
_KERNEL_UINT_RE = re.compile(rb"^(?:0|[1-9][0-9]{0,9})$")
_KERNEL_INT32_MAX = 0x7FFFFFFF
_KERNEL_UINT32_MAX = 0xFFFFFFFF
# The octal escapes each kernel mountinfo writer emits (fs/proc_namespace.c): seq_dentry() and
# seq_path_root() for the root and mount point; mangle() for the fs type and the default source;
# seq_show_option() and filesystem writers for super options. Any other escape, and any raw
# backslash, is non-canonical.
_MOUNTINFO_ESCAPE_RE = re.compile(rb"\\([0-7]{3})")
_MOUNTINFO_PATH_ESCAPED = frozenset(b" \t\n\\")
_MOUNTINFO_MANGLE_ESCAPED = _MOUNTINFO_PATH_ESCAPED | frozenset(b"#")
_MOUNTINFO_SUPER_OPTS_ESCAPED = _MOUNTINFO_MANGLE_ESCAPED | frozenset(b",=")
# One fdinfo line: a canonical lowercase key, exactly ":\t", then printable non-space ASCII.
_FDINFO_LINE_RE = re.compile(rb"([a-z][a-z_]*):\t([\x21-\x7e]+)")


def _parse_kernel_int(token: bytes, *, max_value: int) -> int | None:
    """Parse a bounded non-negative decimal kernel integer without unbounded ``int()`` conversion."""
    if not _KERNEL_UINT_RE.fullmatch(token):
        return None
    value = int(token)
    return value if value <= max_value else None


def _decode_mountinfo_field(field: bytes, *, escaped: frozenset[int] = frozenset()) -> str | None:
    """Decode one kernel-escaped ``mountinfo`` text field, or ``None`` unless it is canonical and printable.

    Only the octal escapes the kernel writes for that field (*escaped*) are accepted; a raw
    backslash or any other escape is refused. The decoded text must be non-empty and
    :func:`~lu_machinery.agent_runtime.sources_read_only.printable_text`, so control bytes such as
    CR, escaped tab or newline, format and separator characters, code points unassigned in
    Unicode 15.0.0, and undecodable bytes (which :func:`os.fsdecode` maps to lone surrogates,
    category ``Cs``) all refuse the field, with the same result on every supported Python.
    """
    out = bytearray()
    pos = 0
    for match in _MOUNTINFO_ESCAPE_RE.finditer(field):
        literal = field[pos : match.start()]
        byte = int(match.group(1), 8)
        if b"\\" in literal or byte not in escaped:
            return None
        out += literal
        out.append(byte)
        pos = match.end()
    if b"\\" in field[pos:]:
        return None
    out += field[pos:]
    text = os.fsdecode(bytes(out))
    return text if text and printable_text(text) else None


def _canonical_mount_point(text: str | None) -> str | None:
    """*text* if it is an absolute, normalized path as ``d_path()`` prints it, else ``None``."""
    if text is None or not text.startswith("/") or text.startswith("//") or posixpath.normpath(text) != text:
        return None
    return text


def _read_mount_table() -> dict[int, str] | None:
    """Strictly parse ``/proc/self/mountinfo`` into ``{mount_id: mount_point}``, or ``None`` on any error.

    Fail closed by class: one malformed, non-canonical or non-printable field in any line, a
    duplicate mount ID, an empty line or a missing final newline refuses the whole table.
    """
    from lu_machinery.common import task_scratch

    try:
        raw = task_scratch._MOUNTINFO_PATH.read_bytes()
    except OSError:
        return None
    if not raw or not raw.endswith(b"\n"):
        return None
    table: dict[int, str] = {}
    for line in raw[:-1].split(b"\n"):
        fields = line.split(b" ")
        try:
            sep = fields.index(b"-")
        except ValueError:
            return None
        if sep < 6 or len(fields) != sep + 4:
            return None
        mount_id_b, parent_id_b, dev_b, root_b, mount_point_b = fields[:5]
        mount_id = _parse_kernel_int(mount_id_b, max_value=_KERNEL_INT32_MAX)
        major_b, colon, minor_b = dev_b.partition(b":")
        mount_point = _canonical_mount_point(_decode_mountinfo_field(mount_point_b, escaped=_MOUNTINFO_PATH_ESCAPED))
        fstype_b, source_b, super_opts_b = fields[sep + 1 :]
        if (
            mount_id is None
            or mount_id in table
            or mount_point is None
            or _parse_kernel_int(parent_id_b, max_value=_KERNEL_INT32_MAX) is None
            or not colon
            or _parse_kernel_int(major_b, max_value=_KERNEL_UINT32_MAX) is None
            or _parse_kernel_int(minor_b, max_value=_KERNEL_UINT32_MAX) is None
            # Mount options and optional fields are fixed kernel tokens: never escaped.
            or any(_decode_mountinfo_field(opt) is None for opt in fields[5:sep])
            or _decode_mountinfo_field(root_b, escaped=_MOUNTINFO_PATH_ESCAPED) is None
            or _decode_mountinfo_field(fstype_b, escaped=_MOUNTINFO_MANGLE_ESCAPED) is None
            or _decode_mountinfo_field(source_b, escaped=_MOUNTINFO_MANGLE_ESCAPED) is None
            or _decode_mountinfo_field(super_opts_b, escaped=_MOUNTINFO_SUPER_OPTS_ESCAPED) is None
        ):
            return None
        table[mount_id] = mount_point
    return table if "/" in table.values() else None


def _read_fd_mount_id(fd: int) -> int | None:
    """Strictly read the single ``mnt_id`` from ``/proc/self/fdinfo/<fd>``, or ``None`` on any error.

    Fail closed by class: every line must be ``<key>:\\t<value>`` with a canonical lowercase key
    (no padding or case variants) and a printable non-space ASCII value; a duplicate key of any
    name, an empty line, a missing final newline, or anything but exactly one bounded ``mnt_id``
    refuses the file.
    """
    try:
        raw = (_FDINFO_DIR / str(fd)).read_bytes()
    except OSError:
        return None
    if not raw.endswith(b"\n"):
        return None
    values: dict[bytes, bytes] = {}
    for line in raw[:-1].split(b"\n"):
        match = _FDINFO_LINE_RE.fullmatch(line)
        if match is None or match.group(1) in values:
            return None
        values[match.group(1)] = match.group(2)
    mount_id = values.get(b"mnt_id")
    return None if mount_id is None else _parse_kernel_int(mount_id, max_value=_KERNEL_INT32_MAX)


def _crosses_mount_boundary(resolved: Path) -> bool:
    """Whether *resolved* or any ancestor below ``/`` crosses a mount boundary, or mount data is unavailable."""
    table = _read_mount_table()
    if table is None:
        return True
    mounts = frozenset(table.values())
    if any(str(ancestor) in mounts for ancestor in (resolved, *resolved.parents) if ancestor.parent != ancestor):
        return True
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open("/", flags)
    except (OSError, ValueError):
        return True
    try:
        try:
            checkout_fd = os.open(resolved, flags)
        except (OSError, ValueError):
            return True
        try:
            root_mount_id = _read_fd_mount_id(root_fd)
            checkout_mount_id = _read_fd_mount_id(checkout_fd)
            if (
                root_mount_id is None
                or checkout_mount_id is None
                or checkout_mount_id != root_mount_id
                or table.get(root_mount_id) != "/"
            ):
                return True
            try:
                if os.fstat(checkout_fd).st_dev != os.lstat(resolved.parent).st_dev:
                    return True
            except (OSError, ValueError):
                return True
        finally:
            os.close(checkout_fd)
    finally:
        os.close(root_fd)
    return False


def fleet_repo_for_checkout(root: Path, *, repos: Mapping[str, FleetRepo] | None = None) -> FleetRepo | None:
    """The registered row whose GitHub ``origin`` the Git checkout at ``root`` declares, else ``None``.

    Git reads the checkout's metadata, never Python: ``git rev-parse`` and ``git config`` run
    through :func:`~lu_machinery.orchestration.execution_safe_git.run_git`, which strips every
    inherited ``GIT_*`` variable, disables global and system config, and turns off
    repository-configured hooks and filesystem monitors. Callers treat ``None`` as an
    unregistered root that keeps every Learn Ukrainian requirement. Only a standalone primary
    checkout is identified: Git must report ``root`` as the top of its work tree, ``root/.git``
    as its shared Git directory, no mount boundary (cross-device or same-filesystem bind mount)
    may lie at ``root`` or any ancestor below ``/`` (strictly verified against
    ``/proc/self/mountinfo`` and ``/proc/self/fdinfo/<fd>``, failing closed on unreadable or
    malformed mount data), and an
    execution-safe ``rev-parse --show-toplevel`` on ``root``'s parent must traverse all ancestors
    and fail with Git's exact ``not a git repository (or any of the parent directories): .git``
    diagnostic (reading no parent index or tree). A linked worktree, a
    separate Git directory, a bare repository's worktree, or any checkout nested inside another
    Git repository (including an embedded-Git submodule) is never identified, because Git cannot
    name the checkout whose curriculum marker would decide it. ``None`` is also returned when Git
    fails, times out or cannot run; ``root`` carries ``PRODUCT_MARKER``, has a non-directory or
    symlink marker ancestor, or cannot be checked for it; the config has an include or a URL
    rewrite; ``origin`` has no URL or several; the URL names Learn Ukrainian; or the URL is not
    exactly the HTTPS or SSH form of one registered slug (ASCII, case-insensitive, optional
    ``.git``). Trust boundary: Learn Ukrainian stays strict when either ``PRODUCT_MARKER`` or a
    Learn Ukrainian ``origin`` is present; simultaneous marker removal and ``origin`` rewriting
    is out of scope for checkout-local identification.
    """
    # Lazy: execution_safe_git imports PyYAML, and this module is reached from dependency-free hooks.
    from lu_machinery.orchestration.execution_safe_git import run_git

    catalog = repos if repos is not None else _bundled_catalog()
    # Pin C locale so Git's non-repository diagnostic is stable; run_git strips GIT_* and disables hooks.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "LANGUAGE": ""}
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.parent == resolved:
        # The process root has no parent to discover from and is itself a mount point: never a consumer.
        return None

    def git(*args: str, cwd: Path = resolved) -> subprocess.CompletedProcess[Any] | None:
        try:
            return run_git(
                list(args),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_CHECKOUT_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    # Two lstat-only refusals come before any Git subprocess, so dispatch admission's cheap precheck
    # spawns nothing for the curriculum product or a non-checkout product root. Neither changes a
    # result: a curriculum marker always gives None, and Git can report ``root/.git`` as the common
    # directory only when it is a real directory (it reports a symlinked ``.git`` by its realpath).
    marker = Path(PRODUCT_MARKER)
    for rel_marker in (*reversed(marker.parents[:-1]), marker):
        try:
            st = os.lstat(resolved / rel_marker)
        except FileNotFoundError:
            break
        except (OSError, ValueError):
            return None
        if rel_marker == marker or not stat.S_ISDIR(st.st_mode):
            return None
    try:
        if not stat.S_ISDIR(os.lstat(resolved / ".git").st_mode):
            return None
    except (OSError, ValueError):
        return None
    layout = git("rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
    lines = [] if layout is None or layout.returncode != 0 else layout.stdout.split(b"\n")
    if len(lines) != 3 or lines[2] or [Path(os.fsdecode(line)) for line in lines[:2]] != [resolved, resolved / ".git"]:
        return None
    if _crosses_mount_boundary(resolved):
        return None
    parent_top = git("rev-parse", "--path-format=absolute", "--show-toplevel", cwd=resolved.parent)
    if (
        parent_top is None
        or parent_top.returncode != 128
        or parent_top.stdout
        or parent_top.stderr != b"fatal: not a git repository (or any of the parent directories): .git\n"
    ):
        return None
    config = git("config", "--no-includes", "--null", "--get-regexp", _CHECKOUT_IDENTITY_KEYS)
    entries = [] if config is None or config.returncode != 0 else config.stdout.split(b"\0")[:-1]
    key = b"remote.origin.url\n"
    if len(entries) != 1 or not entries[0].startswith(key):
        return None
    url = entries[0][len(key) :].decode("ascii", errors="replace").lower()
    matches = [
        row
        for row in catalog.values()
        if any(
            url == f"{prefix}{slug.lower()}{suffix}"
            for slug in row.slugs
            for prefix in ("https://github.com/", "ssh://git@github.com/", "git@github.com:")
            for suffix in ("", ".git")
        )
    ]
    if len(matches) != 1 or _LEARN_UKRAINIAN_SLUG in {slug.lower() for slug in matches[0].slugs}:
        return None
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    """Non-network preflight CLI for fleet repository selector verification."""
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.orchestration.fleet_repos",
        description="Inspect or verify the host-owned dual-slug fleet repository selector without network calls.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser(
        "selector-check",
        help="Verify LU_FLEET_REPO_SELECTOR_FILE fails closed and print its receipt JSON.",
    )
    check.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Optional fleet_repos.yaml path (default: bundled catalog).",
    )
    args = parser.parse_args(argv)
    if args.command == "selector-check":
        try:
            receipt = selector_receipt(registry_path=args.registry)
        except FleetRepoError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, **receipt}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SELECTOR_ENV",
    "ActiveFleetRepo",
    "FleetRepo",
    "FleetRepoError",
    "MissingFleetCheckout",
    "SelectorError",
    "SelectorState",
    "canonical_repo_key",
    "canonical_repository",
    "clear_repository_cache",
    "default_repo_key",
    "fleet_repo_for_checkout",
    "load_fleet_repo_selector",
    "load_fleet_repos",
    "main",
    "pull_request_repository",
    "repository_slugs",
    "resolve_active_fleet_repo",
    "resolve_fleet_repo",
    "resolve_fleet_repo_by_slug",
    "selector_receipt",
    "verify_repository_identity",
]
