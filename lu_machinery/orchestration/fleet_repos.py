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
import errno
import json
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lu_machinery.common.git_credentials import (
    SELECTOR_ENV,
    SelectorError,
    SelectorState,
    active_slug,
    load_selector,
    repository_registry,
    verify_authenticated_repository,
)
from lu_machinery.common.repo_root import PRODUCT_MARKER, resolve_path

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "fleet_repos.yaml"
_VALID_VISIBILITIES = frozenset({"public", "private"})
_MAX_GIT_CONFIG_BYTES = 256 * 1024
_MAX_GITFILE_BYTES = 4096
_LEARN_UKRAINIAN_SLUG = "learn-ukrainian/learn-ukrainian.github.io"
_GITHUB_ORIGIN_RE = re.compile(
    r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}?)(?:\.git)?$"
)
_GIT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_GIT_SECTION_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
_GIT_FALSE_VALUES = frozenset({"false", "no", "off", "0", ""})


def _validate_repo_relpath(key: str, field: str, value: Any) -> str:
    """Validate an explicit canonical repo-relative POSIX file path from ``fleet_repos.yaml``."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or "\x00" in value
        or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise FleetRepoError(f"fleet_repos[{key!r}].{field} must be a canonical repo-relative file path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise FleetRepoError(f"fleet_repos[{key!r}].{field} must be a canonical repo-relative file path") from None
    return value


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
        sources_server = (
            _validate_repo_relpath(key, "sources_server", body["sources_server"])
            if "sources_server" in body
            else None
        )
        admission_policy = (
            _validate_repo_relpath(key, "admission_policy", body["admission_policy"])
            if "admission_policy" in body
            else None
        )
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
        if github.casefold() == _LEARN_UKRAINIAN_SLUG or (
            isinstance(alternate_github, str) and alternate_github.casefold() == _LEARN_UKRAINIAN_SLUG
        ):
            if sources_server is None:
                raise FleetRepoError(f"fleet_repos[{key!r}].sources_server is required for Learn Ukrainian")
            if admission_policy is None:
                raise FleetRepoError(f"fleet_repos[{key!r}].admission_policy is required for Learn Ukrainian")
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
            sources_server=sources_server,
            admission_policy=admission_policy,
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


def _marker_state(checkout: Path) -> str:
    """Return ``'present'``, ``'absent'``, or ``'unreadable'`` for ``PRODUCT_MARKER`` in *checkout*."""
    try:
        os.lstat(checkout / PRODUCT_MARKER)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return "absent"
        return "unreadable"
    return "present"


def _github_slug_from_origin(url: str) -> str | None:
    """Extract ``owner/name`` from a supported GitHub remote URL, or ``None``."""
    match = _GITHUB_ORIGIN_RE.fullmatch(url)
    if match is None:
        return None
    slug = match.group(1)
    if slug.split("/", 1)[1] in (".", ".."):
        return None
    return slug


def _read_bounded_regular_utf8(path: Path, max_bytes: int) -> str | None:
    """Safely read a regular, non-symlink UTF-8 file up to ``max_bytes`` with no NUL bytes, or ``None``."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes or b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _parse_git_section_header(line: str) -> tuple[str, str | None] | None:
    """Parse one Git config ``[section]``, ``[section "sub"]``, or legacy ``[section.sub]`` header.

    Returns ``(lowered_section, subsection_or_None)``, or ``None`` for any malformed header.
    """
    if not line.startswith("["):
        return None
    index = 1
    length = len(line)
    while index < length and line[index] in " \t":
        index += 1
    start = index
    while index < length and line[index] not in ' \t."]':
        index += 1
    section = line[start:index]
    if not section or _GIT_SECTION_NAME_RE.fullmatch(section) is None:
        return None
    lowered_section = section.lower()

    subsection: str | None = None
    if index < length and line[index] == ".":
        # Legacy Git syntax: [section.subsection] (subsection is lowercased per git-config(1)).
        index += 1
        sub_start = index
        while index < length and line[index] != "]":
            index += 1
        raw_sub = line[sub_start:index]
        if not raw_sub or any(ch in " \t" for ch in raw_sub):
            return None
        subsection = raw_sub.lower()
    else:
        ws_start = index
        while index < length and line[index] in " \t":
            index += 1
        if index < length and line[index] == '"':
            if index == ws_start:
                return None
            index += 1
            chars: list[str] = []
            closed = False
            while index < length:
                ch = line[index]
                if ch == "\\":
                    if index + 1 >= length:
                        return None
                    chars.append(line[index + 1])
                    index += 2
                elif ch == '"':
                    closed = True
                    index += 1
                    break
                else:
                    chars.append(ch)
                    index += 1
            if not closed:
                return None
            subsection = "".join(chars)
            while index < length and line[index] in " \t":
                index += 1

    if index >= length or line[index] != "]":
        return None
    tail = line[index + 1 :].strip()
    if tail and not tail.startswith(("#", ";")):
        return None
    return lowered_section, subsection


def _parse_git_value(raw: str) -> str | None:
    """Parse one Git config right-hand value (quotes, escapes, inline comments), or ``None`` if invalid."""
    chars: list[str] = []
    in_quotes = False
    index = 0
    length = len(raw)
    while index < length:
        ch = raw[index]
        if ch == "\\":
            if index + 1 >= length:
                return None
            nxt = raw[index + 1]
            if nxt == "n":
                chars.append("\n")
            elif nxt == "t":
                chars.append("\t")
            elif nxt == "b":
                chars.append("\b")
            elif nxt in ('"', "\\"):
                chars.append(nxt)
            else:
                return None
            index += 2
        elif ch == '"':
            in_quotes = not in_quotes
            index += 1
        elif not in_quotes and ch in ("#", ";"):
            break
        else:
            chars.append(ch)
            index += 1
    if in_quotes:
        return None
    return "".join(chars).strip()


def _unfold_git_config_lines(text: str) -> list[str] | None:
    """Join Git backslash-continued lines; refuse dangling EOF continuation."""
    if text.startswith("\ufeff"):
        text = text[1:]
    logical: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        combined = pending + raw_line
        # Count trailing backslashes: an odd count continues onto the next line.
        trailing = len(combined) - len(combined.rstrip("\\"))
        if trailing % 2 == 1:
            pending = combined[:-1]
        else:
            logical.append(combined)
            pending = ""
    if pending:
        return None
    return logical


def _parse_single_git_config(path: Path) -> tuple[list[str], bool, bool] | None:
    """Strictly parse one Git config file.

    Returns ``(origin_urls, unsafe_directives, worktree_config_enabled)`` or ``None`` if missing,
    unreadable, or syntactically invalid.
    """
    text = _read_bounded_regular_utf8(path, _MAX_GIT_CONFIG_BYTES)
    if text is None:
        return None
    lines = _unfold_git_config_lines(text)
    if lines is None:
        return None

    section: str | None = None
    subsection: str | None = None
    unsafe = False
    worktree_config = False
    urls: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            parsed_sec = _parse_git_section_header(line)
            if parsed_sec is None:
                return None
            section, subsection = parsed_sec
            if section in ("include", "includeif", "url"):
                unsafe = True
            continue
        if section is None:
            return None
        if "=" in line:
            raw_key, raw_val = line.split("=", 1)
            key = raw_key.strip().lower()
            if _GIT_KEY_RE.fullmatch(key) is None:
                return None
            val = _parse_git_value(raw_val)
            if val is None:
                return None
        else:
            key = line.split("#", 1)[0].split(";", 1)[0].strip().lower()
            if _GIT_KEY_RE.fullmatch(key) is None:
                return None
            val = "true"

        if key in ("insteadof", "pushinsteadof"):
            unsafe = True
        if section == "extensions" and key == "worktreeconfig":
            worktree_config = val.lower() not in _GIT_FALSE_VALUES
        elif section == "remote" and subsection == "origin" and key == "url":
            urls.append(val)

    return urls, unsafe, worktree_config


def _read_origin_urls(git_dir: Path, worktree_gitdir: Path | None = None) -> tuple[list[str], bool] | None:
    """Read effective local ``remote.origin.url`` entries from ``.git/config`` (and ``config.worktree``).

    Returns ``(urls, unsafe_directives)``, or ``None`` when the config is missing, unreadable,
    or malformed. Never falls back on missing or invalid config.
    """
    primary_parsed = _parse_single_git_config(git_dir / "config")
    if primary_parsed is None:
        return None
    urls, unsafe, wt_enabled = primary_parsed

    wt_config = (worktree_gitdir or git_dir) / "config.worktree"
    try:
        wt_stat: os.stat_result | None = os.lstat(wt_config)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            return None
        wt_stat = None

    if wt_enabled and wt_stat is not None:
        wt_parsed = _parse_single_git_config(wt_config)
        if wt_parsed is None:
            return None
        wt_urls, wt_unsafe, _ = wt_parsed
        urls.extend(wt_urls)
        unsafe = unsafe or wt_unsafe

    return urls, unsafe


def _resolve_verified_git_metadata(resolved_root: Path) -> tuple[Path, Path | None, Path | None] | None:
    """Verify ``resolved_root/.git`` and return ``(primary, common_git_dir, worktree_gitdir)``.

    - No ``.git`` entry (``ENOENT``): returns ``(resolved_root, None, None)``.
    - Real ``.git`` directory: returns ``(resolved_root, resolved_root / '.git', None)``.
    - Regular ``.git`` gitfile: validates bounded UTF-8 ``gitdir:`` pointing to an existing real
      ``<primary>/.git/worktrees/<name>`` directory whose ``gitdir`` back-pointer resolves back
      to ``resolved_root / '.git'`` and whose ``commondir`` resolves to ``<primary>/.git``.
    - Any symlink, missing/forged back-pointer, unreadable, oversized, or malformed metadata
      returns ``None`` (fail closed).
    """
    root_git = resolved_root / ".git"
    try:
        root_git_stat = os.lstat(root_git)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return resolved_root, None, None
        return None

    if stat.S_ISLNK(root_git_stat.st_mode):
        return None
    if stat.S_ISDIR(root_git_stat.st_mode):
        return resolved_root, root_git, None
    if not stat.S_ISREG(root_git_stat.st_mode):
        return None

    gitfile_text = _read_bounded_regular_utf8(root_git, _MAX_GITFILE_BYTES)
    if gitfile_text is None:
        return None
    non_empty = [line for line in gitfile_text.splitlines() if line.strip()]
    if len(non_empty) != 1 or not non_empty[0].startswith("gitdir:"):
        return None
    raw_gitdir = non_empty[0][len("gitdir:") :].strip()
    if not raw_gitdir or any(unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in raw_gitdir):
        return None
    candidate_gitdir = Path(raw_gitdir) if os.path.isabs(raw_gitdir) else (resolved_root / raw_gitdir)
    try:
        wt_gitdir = resolve_path(candidate_gitdir, strict=True)
        wt_stat = os.lstat(wt_gitdir)
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISDIR(wt_stat.st_mode):
        return None
    if wt_gitdir.parent.name != "worktrees" or wt_gitdir.parent.parent.name != ".git":
        return None
    common_git_dir = wt_gitdir.parent.parent
    try:
        common_stat = os.lstat(common_git_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(common_stat.st_mode):
        return None

    # Verify the worktree admin entry's commondir points to common_git_dir and its gitdir points back here.
    commondir_text = _read_bounded_regular_utf8(wt_gitdir / "commondir", _MAX_GITFILE_BYTES)
    backptr_text = _read_bounded_regular_utf8(wt_gitdir / "gitdir", _MAX_GITFILE_BYTES)
    if commondir_text is None or backptr_text is None:
        return None
    commondir_lines = [line.strip() for line in commondir_text.splitlines() if line.strip()]
    backptr_lines = [line.strip() for line in backptr_text.splitlines() if line.strip()]
    if len(commondir_lines) != 1 or len(backptr_lines) != 1:
        return None
    try:
        resolved_common = resolve_path(
            Path(commondir_lines[0]) if os.path.isabs(commondir_lines[0]) else (wt_gitdir / commondir_lines[0]),
            strict=True,
        )
        raw_backptr = Path(backptr_lines[0]) if os.path.isabs(backptr_lines[0]) else (wt_gitdir / backptr_lines[0])
        resolved_backptr_parent = resolve_path(raw_backptr.parent, strict=True)
        primary = resolve_path(common_git_dir.parent, strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved_common != common_git_dir:
        return None
    if raw_backptr.name != ".git" or resolved_backptr_parent != resolved_root:
        return None
    if not primary.is_dir():
        return None
    return primary, common_git_dir, wt_gitdir


def consumer_repo_for_root(
    root: Path,
    *,
    repos: Mapping[str, FleetRepo] | None = None,
) -> FleetRepo | None:
    """Resolve a product checkout ``root`` to its registered ``FleetRepo`` row, or ``None``.

    Fail-closed rules:
    - A symlinked ``root`` resolves to its real target first, and linked worktrees resolve to
      their primary checkout only after bounded verification of the gitfile, worktree admin
      directory, ``commondir``, and ``gitdir`` back-pointer. Unresolvable, dangling, or malformed
      metadata returns ``None``.
    - Any checkout carrying ``PRODUCT_MARKER`` (on ``root`` or its primary) always resolves to
      the default (``public``) Learn Ukrainian row and can never match a non-default consumer.
    - When the primary has a ``.git`` directory, ``.git/config`` (and ``config.worktree`` when
      ``extensions.worktreeConfig`` is enabled) is parsed strictly in Git-compatible syntax
      (including UTF-8 BOM, ``[remote "origin"]``, and legacy ``[remote.origin]``). Missing or
      malformed config, ``[include]``/``[includeIf]``/``[url]``/``insteadOf`` directives, or
      multiple/unregistered ``origin`` URLs return ``None`` rather than falling back to basename.
    - Only when no ``.git`` exists or a valid ``.git/config`` declares zero ``origin`` URLs and
      no unsafe directives does the primary checkout's directory name match a unique ``local_name``.
    - An unreadable or malformed ``fleet_repos.yaml`` raises :class:`FleetRepoError`.
    """
    catalog = repos if repos is not None else _bundled_catalog()
    try:
        resolved_root = resolve_path(root, strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved_root.is_dir():
        return None

    git_meta = _resolve_verified_git_metadata(resolved_root)
    if git_meta is None:
        return None
    primary, common_git_dir, worktree_gitdir = git_meta

    # Learn Ukrainian marker always pins to the default (public) row, never a non-LU consumer.
    for candidate in (resolved_root, primary):
        marker = _marker_state(candidate)
        if marker == "unreadable":
            return None
        if marker == "present":
            defaults = [row for row in catalog.values() if row.default]
            return defaults[0] if len(defaults) == 1 else None

    if common_git_dir is not None:
        parsed = _read_origin_urls(common_git_dir, worktree_gitdir)
        if parsed is None:
            return None
        urls, unsafe = parsed
        if unsafe:
            return None
        if urls:
            if len(urls) != 1:
                return None
            slug = _github_slug_from_origin(urls[0])
            if slug is None:
                return None
            folded = slug.casefold()
            matches = [row for row in catalog.values() if any(folded == s.casefold() for s in row.slugs)]
            return matches[0] if len(matches) == 1 else None

    name_matches = [row for row in catalog.values() if row.local_name == primary.name]
    return name_matches[0] if len(name_matches) == 1 else None


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
    "consumer_repo_for_root",
    "default_repo_key",
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
