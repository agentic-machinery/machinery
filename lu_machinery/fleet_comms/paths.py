"""Canonical durable paths for fleet communications state."""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path

from lu_machinery.common.repo_root import (
    MachineryConfigError,
    configured_interpreter,
    configured_primary_root,
    is_configured_state_root,
    resolve_path,
    state_root,
    state_root_for_checkout,
)
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


def _installed_release_venv(state: Path) -> Path | None:
    """Resolve the operator-configured virtual environment authorized to write the live store.

    In today's layout (``state`` is a primary Git checkout), the authorized release is
    always ``state / ".venv"`` so an ambient ``LEARN_UKRAINIAN_PYTHON`` (such as a test
    environment) can never authorize writes against a primary checkout's live store. When
    ``state`` is a dedicated non-Git ``LU_STATE_ROOT`` without a ``.venv``, an explicit
    ``LU_PYTHON`` / ``LEARN_UKRAINIAN_PYTHON`` inside a real PEP 405 virtual environment
    (regular non-symlink ``pyvenv.cfg``) identifies the installed runtime, falling back to
    the configured primary checkout's ``.venv``. Never trusts ``sys.prefix`` or a
    worktree-local environment.
    """
    if (state / ".git").is_dir():
        return state / ".venv"
    interpreter = configured_interpreter()
    if (
        interpreter is not None
        and interpreter.parent.name == "bin"
        and interpreter.is_file()
        and os.access(interpreter, os.X_OK)
    ):
        candidate_venv = interpreter.parent.parent
        pyvenv = candidate_venv / "pyvenv.cfg"
        if pyvenv.is_file() and not pyvenv.is_symlink():
            return candidate_venv
    primary = configured_primary_root()
    if primary is not None:
        return resolve_path(primary, strict=True) / ".venv"
    return None


def assert_store_write_allowed(store: Path, *, package_file: str) -> None:
    """Only the installed release package may write the configured live store.

    An override or explicit argument naming the live root is still live. Resolve
    aliases before comparing; unknown shared-state identity never grants a write.
    The venv's site directory may be symlinked, but a package symlink pointing
    back to source is not an installed release. No subprocess or store open is
    needed to establish this boundary.
    """
    package = Path(package_file).parent.parent
    try:
        package = resolve_path(package_file).parents[1]
        state = resolve_path(state_root(), strict=True)
        live_store = resolve_path(state / DEFAULT_ROOT_REL / "comms.sqlite3")
        store = resolve_path(store)
        if store != live_store and not (
            store.exists() and live_store.exists() and store.samefile(live_store)
        ):
            return
        venv_path = _installed_release_venv(state)
        if venv_path is None:
            raise LiveStoreReleaseError("live_store_requires_installed_release", package, store)
        venv = str(venv_path)
        site = resolve_path(sysconfig.get_path(
            "purelib", scheme="venv", vars={"base": venv, "platbase": venv}
        ))
    except LiveStoreReleaseError:
        raise
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


def _resolve_repo_plane_base(repo_root: Path, *, allow_non_git: bool) -> Path:
    """Resolve the state base for an explicit ``repo_root`` argument.

    Accepts the explicitly configured non-Git ``LU_STATE_ROOT`` (converging Monitor and
    other callers passing ``ctx.roots.state_root``), and converges configured fleet
    checkouts onto ``LU_STATE_ROOT`` when detached. Refuses unconfigured non-Git
    directories unless ``allow_non_git=True`` (#6863).
    """
    configured_state = is_configured_state_root(repo_root)
    if configured_state is not None:
        return configured_state
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
        return repo_root.resolve()
    return state_root_for_checkout(base)


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
        base = _resolve_repo_plane_base(repo_root, allow_non_git=False) if repo_root is not None else state_root()
    except (PlaneRootAnchorError, NotAGitRepositoryError, MachineryConfigError, OSError, RuntimeError):
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
       root, a fixture): the configured non-Git state root when ``repo_root`` names
       it, else the primary checkout that owns its shared ``.git`` store (converged
       onto ``LU_STATE_ROOT`` when configured for this plane).
    3. Otherwise the shared-state anchor, :func:`lu_machinery.common.repo_root.state_root`
       (``LU_STATE_ROOT`` or ``LU_PRIMARY_REPO_ROOT`` / aliases). Without one the
       installed package raises ``StateRootError``, which names the variable.

    The working directory is never consulted: a command run from another
    repository's checkout (a machinery worktree) once anchored on that
    repository's primary and created a second, unread store there.

    An unconfigured named ``repo_root`` outside any git repository is a HARD ERROR
    (#6863), never a silent fallback to the raw candidate. Isolated non-git callers
    and test fixtures must opt in explicitly with ``allow_non_git=True``.
    """
    try:
        env = os.environ.get(ENV_ROOT)
        if env:
            root = validate_plane_root(Path(env))
        elif repo_root is None:
            root = validate_plane_root(state_root() / DEFAULT_ROOT_REL)
        else:
            base = _resolve_repo_plane_base(repo_root, allow_non_git=allow_non_git)
            root = validate_plane_root(base / DEFAULT_ROOT_REL)
        _refuse_retired_local_plane(root)
    except PlaneRootAnchorError:
        raise
    except (OSError, RuntimeError):
        raise PlaneRootAnchorError("plane_root_unresolvable") from None
    return root
