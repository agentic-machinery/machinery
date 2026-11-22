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
import re
import sys
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

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "fleet_repos.yaml"
_VALID_VISIBILITIES = frozenset({"public", "private"})


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
    except (ValueError, OSError) as exc:
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


def load_fleet_repo_selector(
    *,
    registry_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SelectorState:
    """Load the validated host-owned selector state, normalizing failures to ``FleetRepoError``."""
    cfg_path = (registry_path or _CONFIG_PATH).resolve()
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
    cfg_path = (registry_path or _CONFIG_PATH).resolve()
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


def clear_repository_cache() -> None:
    """Reload registry edits on the next canonical repository lookup."""
    repository_registry.cache_clear()
    _repository_catalog.cache_clear()


def canonical_repository(value: str) -> str:
    """Canonical owner/name, accepting registry keys and both baked slug spellings."""
    spelling = value.strip().casefold()
    for repo in _repository_catalog(_CONFIG_PATH.resolve()).values():
        known = {repo.key.casefold(), *(slug.casefold() for slug in repo.slugs)}
        if spelling in known:
            return repo.github.casefold()
    if re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", spelling):
        return spelling
    raise FleetRepoError("PR repository must be owner/name")


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
    "load_fleet_repo_selector",
    "load_fleet_repos",
    "main",
    "pull_request_repository",
    "resolve_active_fleet_repo",
    "resolve_fleet_repo",
    "resolve_fleet_repo_by_slug",
    "selector_receipt",
    "verify_repository_identity",
]
