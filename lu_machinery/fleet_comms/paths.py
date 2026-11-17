"""Canonical durable paths for fleet communications state."""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path

from lu_machinery.common.repo_root import MachineryConfigError, resolve_path, state_root
from lu_machinery.guardrails.worktree_containment import (
    NotAGitRepositoryError,
    canonicalize,
    resolve_main_root,
)

ENV_ROOT = "FLEET_COMMS_ROOT"
ENV_ALLOW_LOCAL_SHADOW = "FLEET_COMMS_ALLOW_LOCAL_SHADOW"
DEFAULT_ROOT_REL = Path("batch_state") / "fleet-comms" / "v1"
RETIRED_LOCAL_MARKER = "READ_ME_CANONICAL_ON_JOB_HOST.txt"
RETIRED_LOCAL_PLANE_MESSAGE = (
    "local fleet-comms sqlite is retired; the job-host Monitor owns "
    "the canonical plane. Do not chmod the stub directory. Observe "
    "via the tunneled API, or set FLEET_COMMS_ALLOW_LOCAL_SHADOW=1 "
    "only for an isolated fixture."
)


class PlaneRootAnchorError(RuntimeError):
    """Raised when the plane root cannot be anchored to the primary checkout."""


class LiveStoreReleaseError(PlaneRootAnchorError):
    """A writer cannot establish installed-release authority for the live store."""

    def __init__(self, reason_code: str, package_path: Path, store: Path) -> None:
        self.reason_code = reason_code
        self.package_path = package_path
        self.store = store
        super().__init__(f"{reason_code}: package={package_path}; store={store}")


def assert_store_write_allowed(store: Path, *, package_file: str) -> None:
    """Only the primary venv's installed package may write the default store.

    An override or explicit argument naming the live root is still live. Resolve
    aliases before comparing; unknown shared-state identity never grants a write.
    The venv's site directory may be symlinked, but a package symlink pointing
    back to source is not an installed release. No subprocess or store open is
    needed to establish this boundary.
    """
    package = Path(package_file).parent.parent
    try:
        package = resolve_path(package_file).parents[1]
        primary = resolve_path(state_root(), strict=True)
        live_store = resolve_path(primary / DEFAULT_ROOT_REL / "comms.sqlite3")
        store = resolve_path(store)
        if store != live_store and not (
            store.exists() and live_store.exists() and store.samefile(live_store)
        ):
            return
        venv = str(primary / ".venv")
        site = resolve_path(sysconfig.get_path(
            "purelib", scheme="venv", vars={"base": venv, "platbase": venv}
        ))
    except (MachineryConfigError, OSError, RuntimeError, KeyError, ValueError) as exc:
        raise LiveStoreReleaseError("live_store_identity_unknown", package, store) from exc
    if package != site / "lu_machinery":
        raise LiveStoreReleaseError("live_store_requires_installed_release", package, store)


def validate_plane_root(root: Path) -> Path:
    """Canonicalize storage roots and refuse checkout roots without writing.

    Both primary checkouts and linked worktrees carry a .git entry. Refuse
    that entry even when damaged; validation must not need a working Git
    executable or a writable backend. Dedicated directories beneath a checkout
    remain valid, including the default plane directory.
    """
    try:
        resolved = canonicalize(root)
        marker = resolved / ".git"
        is_checkout = marker.exists() or marker.is_symlink()
    except (OSError, RuntimeError):
        raise PlaneRootAnchorError("plane_root_unresolvable") from None
    if is_checkout:
        raise PlaneRootAnchorError("plane_root_is_git_checkout")
    return resolved


def _retired_marker_present(root: Path) -> bool:
    """True when the notebook plane root (or its parent) carries the retire marker."""
    return (root / RETIRED_LOCAL_MARKER).is_file() or (
        root.parent / RETIRED_LOCAL_MARKER
    ).is_file()


def local_plane_is_retired(
    *,
    repo_root: Path | None = None,
    root: Path | None = None,
) -> bool:
    """Return True when this checkout's local plane has been retired to the job host.

    Does not raise. Honors ``FLEET_COMMS_ALLOW_LOCAL_SHADOW=1`` (fixtures) as
    not-retired so isolated tests can open a local sqlite. Never creates or
    chmods the stub directory.
    """
    if os.environ.get(ENV_ALLOW_LOCAL_SHADOW, "").strip() == "1":
        return False
    if root is not None:
        return _retired_marker_present(Path(root).expanduser().resolve())
    env = os.environ.get(ENV_ROOT)
    if env:
        return _retired_marker_present(Path(env).expanduser().resolve())
    try:
        base = resolve_main_root(repo_root) if repo_root is not None else state_root()
    except (NotAGitRepositoryError, MachineryConfigError):
        return False
    return _retired_marker_present((base / DEFAULT_ROOT_REL).resolve())


def _refuse_retired_local_plane(root: Path) -> None:
    """Fail closed when the notebook copy has been retired to the job host.

    The marker is an ordinary file in the plane root (gitignored). Tests and
    explicit recovery set ``FLEET_COMMS_ALLOW_LOCAL_SHADOW=1``.
    """
    if os.environ.get(ENV_ALLOW_LOCAL_SHADOW, "").strip() == "1":
        return
    if _retired_marker_present(root):
        raise PlaneRootAnchorError(RETIRED_LOCAL_PLANE_MESSAGE)


def default_plane_root(
    *,
    repo_root: Path | None = None,
    allow_non_git: bool = False,
) -> Path:
    """Resolve the one Fleet Comms authority store, unless explicitly overridden.

    Anchoring order:

    1. ``FLEET_COMMS_ROOT`` env override, validated as a dedicated storage root.
    2. ``repo_root`` when the caller names one (the Monitor API's configured state
       root, a fixture): the primary checkout that owns its shared ``.git`` store.
    3. Otherwise the shared-state anchor, :func:`lu_machinery.common.repo_root.state_root`
       (``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``). Without it the installed package raises
       ``StateRootError``, which names the variable.

    The working directory is never consulted: a command run from another
    repository's checkout (a machinery worktree) once anchored on that
    repository's primary and created a second, unread store there.

    A named ``repo_root`` outside any git repository is a HARD ERROR (#6863),
    never a silent fallback to the raw candidate. The old fallback let a process
    running with a garbage cwd (a transport-leak directory fragment at the repo
    root) materialize a shadow ``batch_state/fleet-comms/v1`` tree there,
    converting a quoting bug into silent message loss. Isolated non-git callers
    and test fixtures must opt in explicitly with ``allow_non_git=True``.
    """
    try:
        env = os.environ.get(ENV_ROOT)
        if env:
            root = validate_plane_root(Path(env))
        elif repo_root is None:
            root = validate_plane_root(state_root() / DEFAULT_ROOT_REL)
        else:
            try:
                base = resolve_main_root(repo_root)
            except NotAGitRepositoryError as exc:
                if not allow_non_git:
                    raise PlaneRootAnchorError(
                        f"refusing to anchor the fleet-comms plane root at {repo_root}: "
                        "not inside a git repository, so the primary checkout cannot be "
                        "verified. A silent fallback here once materialized a shadow "
                        "comms DB under a garbage cwd (#6863). Set FLEET_COMMS_ROOT for an "
                        "explicit override, or pass allow_non_git=True for an isolated "
                        "non-git fixture."
                    ) from exc
                base = repo_root.resolve()
            root = validate_plane_root(base / DEFAULT_ROOT_REL)
        _refuse_retired_local_plane(root)
    except PlaneRootAnchorError:
        raise
    except (OSError, RuntimeError):
        raise PlaneRootAnchorError("plane_root_unresolvable") from None
    return root
