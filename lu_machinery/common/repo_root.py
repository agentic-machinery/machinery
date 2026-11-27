"""Repository root and project-interpreter resolvers, and the one machinery root configuration.

Execution modes (C-4843, root contract step 1). This module runs either as the
**in-tree source copy** (the package directory that sits inside a product checkout
under the product's own package name) or as **lu_machinery** (the installed package,
or a standalone machinery checkout, whose package directory is named
``INSTALLED_PACKAGE_NAME``). :func:`in_tree_source` tells them apart from this file's
unresolved package directory name alone: no working directory, ancestor, sibling, home
directory or ``site-packages`` lookup takes part, so a decoy file cannot change the mode.

- :func:`machinery_root` and :func:`machinery_path`: the package directory, in both
  modes; never configured, and resource paths stay in the package or its shipped shared resource tree.
- :func:`product_root`: an explicit argument, else ``LU_PRODUCT_ROOT``; with neither,
  lu_machinery refuses, and only the in-tree copy derives the checkout holding it.
- :func:`state_root`: the host context's state directory or the explicit legacy
  ``LU_STATE_ROOT`` setting. If neither is configured, it refuses.
- :func:`primary_root`: ``LU_PRIMARY_REPO_ROOT`` (alias
  ``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``); only the in-tree copy can derive its own checkout.

Refusals raise :class:`ProductRootError` or :class:`StateRootError` (both
:class:`MachineryConfigError`, a ``ValueError``) at the call, never at import, so
package-only imports and ``--help`` work with nothing configured. Resolution only
reads: it creates no directory, file, database or connection. An invalid selection
never falls through to a later candidate.

Machinery roots (#12681). Five roots are kept distinct, and the four that can live
outside this checkout come from one configuration: environment variables read at the
call (a systemd ``EnvironmentFile=``, a launcher or a test can supply them). In the
in-tree source copy an unset or empty variable keeps today's value, so the defaults
are the behaviour before the configuration existed.

========================  ====================================  =================================================
Root                      Variable (compatibility alias)        Default (today's behaviour)
========================  ====================================  =================================================
runtime (machinery code)  none: the checkout this file is in    ``Path(__file__).parents[2]``
primary checkout          ``LU_PRIMARY_REPO_ROOT``              ``main_checkout_root`` of the runtime checkout
                          (``LEARN_UKRAINIAN_PRIMARY_REPO_ROOT``)
GitHub slug               the plane owner's consumer profile,   ``learn-ukrainian/learn-ukrainian.github.io``
                          ``LU_GITHUB_SLUG``                    when the plane owner has no profile
                          (``LEARN_UKRAINIAN_GITHUB_SLUG``)
state root                host context or ``LU_STATE_ROOT``     required (holds ``batch_state/``)
                          (``LEARN_UKRAINIAN_STATE_ROOT``)
rules directory (policy)  none: installed release                ``agents_extensions/shared/rules`` beside the package
interpreter               ``LU_PYTHON``                         :func:`project_interpreter`'s resolution order
                          (``LEARN_UKRAINIAN_PYTHON``)
========================  ====================================  =================================================

Each ``LU_*`` name is the primary name; its ``LEARN_UKRAINIAN_*`` alias is still read, so a
host configured before the rename keeps working unchanged. Both set to different values
refuses (:class:`MachineryConfigError`, naming both), so the two can never select different
roots. Messages name the variable that was actually read. The project-state reporter wrapper,
child-process environments and the shell libraries still read and export the alias only.

The GitHub slug has one authority. The plane owner's consumer profile
(``<state root>/.lu-machinery/profile.v1.json``, read by :mod:`.profile`) and the slug
variable may both name it only when they agree; a conflict refuses. The default slug is the
compatibility path for a plane owner without a profile (learn-ukrainian until it commits one).

Path settings must be absolute. The durable state root can live outside all checkouts.
The host context validates any supplied legacy state setting against that binding.

The runtime root is never configured. It is the code that is running, and a re-exec
from a linked worktree (``scripts/dispatcher/primary.py``) still resolves that code's own
primary from Git metadata, not from this configuration.

Machinery code finds its own files through :func:`machinery_root` (the package directory
this module is in, one level below the runtime root) and :func:`machinery_path` (a
contained, package-relative code or shipped resource path), never by spelling the package's name.

Machinery code finds the product's files (curriculum data, product configuration, product
modules named by product data) through :func:`product_root` (C-4746), never through its
own location: once the machinery is its own repository, the checkout it runs from is not
the product. The selection order is an explicit argument, then ``LU_PRODUCT_ROOT``, then,
in the in-tree source copy only, the checkout that holds this package (from the unresolved
module location) when it is a product checkout. The first candidate that is given is the
only one considered: an invalid one raises :class:`ProductRootError` and never falls
through to the next. Every candidate must be absolute. An explicit or configured product
must be an existing directory and need not be the Ukrainian curriculum (kube-dojo is a
product too): the operation that reads it validates the files it needs. Only the derived
in-tree default must carry the product marker ``PRODUCT_MARKER``, which proves the
layout, not authorization or completeness: loaders keep their own origin checks, and a
reader whose required file is missing fails through its own error. Machinery-owned
resources (shared hooks, guard executables) never come from this root. The installed
package has no implicit product: every service, timer, hook or command that reads
product files names the product checkout with ``LU_PRODUCT_ROOT`` (a systemd
``EnvironmentFile=``, a launcher) or an explicit root; without one it refuses. The Monitor
API selects it at startup (``scripts.api.config.PRODUCT_ROOT``): served from a release
snapshot, that is the snapshot itself, never the live checkout.

Shared runtime state requires a valid host context or an explicit ``LU_STATE_ROOT``.
Product selection and Git checkout selection are independent of state placement.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _install_redacted_environ() -> None:
    """Hide env values without importing the scripts package.

    Callers load this module as ``common.repo_root`` with ``scripts/`` on
    ``sys.path``. A package import of ``scripts`` fails on that path.
    A partial checkout that copied this module without ``scripts/lib`` still
    imports: the formatter is optional there.
    """
    name = "lu_machinery.lib.redacted_environ"
    module = sys.modules.get(name)
    if module is None:
        path = Path(__file__).resolve().parents[1] / "lib" / "redacted_environ.py"
        if not path.is_file():
            return
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    module.install_redacted_environ_repr()


_install_redacted_environ()

# The configuration names. ``LU_*`` are the primary names; each ``LEARN_UKRAINIAN_*`` name is its
# compatibility alias and is still read. The constants below keep naming the alias because their
# importers export it to child processes and shell readers, which read only that name until the
# propagation step of the consumer profile work (A08.2).
LU_PRIMARY_ROOT_ENV = "LU_PRIMARY_REPO_ROOT"
LU_GITHUB_SLUG_ENV = "LU_GITHUB_SLUG"
LU_STATE_ROOT_ENV = "LU_STATE_ROOT"
LU_INTERPRETER_ENV = "LU_PYTHON"
REPOS_ROOT_ENV = "LU_REPOS_ROOT"
PROFILE_PATH_ENV = "LU_PROFILE_PATH"
PRIMARY_ROOT_ENV = "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"
GITHUB_SLUG_ENV = "LEARN_UKRAINIAN_GITHUB_SLUG"
STATE_ROOT_ENV = "LEARN_UKRAINIAN_STATE_ROOT"
INTERPRETER_ENV = "LEARN_UKRAINIAN_PYTHON"
# Primary name -> compatibility alias. Both set to different values refuses.
ENV_ALIASES = {
    LU_PRIMARY_ROOT_ENV: PRIMARY_ROOT_ENV,
    LU_GITHUB_SLUG_ENV: GITHUB_SLUG_ENV,
    LU_STATE_ROOT_ENV: STATE_ROOT_ENV,
    LU_INTERPRETER_ENV: INTERPRETER_ENV,
}
MACHINERY_ENV_VARS = (
    PRIMARY_ROOT_ENV,
    GITHUB_SLUG_ENV,
    STATE_ROOT_ENV,
    INTERPRETER_ENV,
    REPOS_ROOT_ENV,
    PROFILE_PATH_ENV,
    *ENV_ALIASES,
)
DEFAULT_GITHUB_SLUG = "learn-ukrainian/learn-ukrainian.github.io"
# The plane owner's consumer profile directory (``common/profile.py`` reads the profile inside it).
CONSUMER_PROFILE_DIR = ".lu-machinery"
RULES_DIR_REL = "agents_extensions/shared/rules"
RUNTIME_ROOT = Path(__file__).resolve().parents[2]
_MACHINERY_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_ROOT_ENV = "LU_PRODUCT_ROOT"
# Product Python code and CI census data keep this tree after extraction.
_PRODUCT_TREE = "scripts"
# A file only the product repository has (Sol, C-4746): it proves a checkout's layout.
PRODUCT_MARKER = "curriculum/l2-uk-en/curriculum.yaml"
# The checkout that holds this package, from the unresolved module location (design-v1
# amendment 1): with the package directory symlinked into a product checkout, this names
# the product checkout, not the machinery checkout the link points at.
_PACKAGE_CHECKOUT = Path(os.path.abspath(__file__)).parents[2]
# The import name of the installed (and standalone-checkout) machinery package. A package
# directory with any other name is the in-tree source copy inside a product checkout.
INSTALLED_PACKAGE_NAME = "lu_machinery"
# Decided from the unresolved module location once, at import, without touching the file
# system: a symlinked in-tree package directory keeps the in-tree name.
_IN_TREE_SOURCE = Path(os.path.abspath(__file__)).parents[1].name != INSTALLED_PACKAGE_NAME
# GitHub owner (alphanumerics and inner hyphens) / repository name.
# Git's object-directory override, which its repository test honours in place of objects/.
_GIT_OBJECT_DIRECTORY_ENV = "GIT_OBJECT_DIRECTORY"
# A SHA-1 or SHA-256 object ID, as a detached HEAD holds it.
_GIT_OBJECT_ID_RE = re.compile(r"[0-9a-fA-F]{64}|[0-9a-fA-F]{40}")
_GITHUB_SLUG_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}")


class MachineryConfigError(ValueError):
    """A machinery root variable is set to a value that cannot be used; the message names it."""


class StateRootError(MachineryConfigError):
    """No shared-state anchor is configured where one is required; the message names the variable."""


def in_tree_source() -> bool:
    """True when this module runs as the in-tree source copy inside a product checkout.

    False for the installed ``lu_machinery`` package and for a standalone machinery
    checkout: there no product and no state anchor is ever derived from the code's own
    location (C-4843).
    """
    return _IN_TREE_SOURCE


def _require_in_tree_default(error: type[MachineryConfigError], what: str, remedy: str) -> None:
    """Refuse the location-derived default of ``what`` outside the in-tree source copy."""
    if not _IN_TREE_SOURCE:
        raise error(f"no {what} is configured, and the {INSTALLED_PACKAGE_NAME} package derives none: {remedy}")


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _setting(primary: str, env: Mapping[str, str] | None) -> tuple[str, str] | None:
    """``(name, value)`` of the setting ``primary`` or its alias, ``None`` when both are unset or empty.

    An empty value counts as unset, as before the alias existed. Both set to different values
    refuses, naming both, so the two names can never select different roots.
    """
    alias = ENV_ALIASES[primary]
    values = _env(env)
    new, old = values.get(primary, ""), values.get(alias, "")
    if new and old and new != old:
        raise MachineryConfigError(
            f"{primary} and its compatibility alias {alias} are both set to different values; "
            f"set one, or set both to the same value"
        )
    if new:
        return primary, new
    if old:
        return alias, old
    return None


def _configured_path(primary: str, env: Mapping[str, str] | None) -> tuple[str, Path] | None:
    """``(name, path)`` of the absolute path setting ``primary`` or its alias; refuse a relative path."""
    setting = _setting(primary, env)
    if setting is None:
        return None
    name, value = setting
    if "\x00" in value or not os.path.isabs(value):
        raise MachineryConfigError(f"{name} must be an absolute path, got {value!r}")
    return name, Path(value)


def _configured_single_path(name: str, env: Mapping[str, str] | None) -> tuple[str, Path] | None:
    """``(name, path)`` of a single-name absolute path setting; refuse relative or NUL-bearing values."""
    value = _env(env).get(name, "")
    if not value:
        return None
    if "\x00" in value or not os.path.isabs(value):
        raise MachineryConfigError(f"{name} must be an absolute path, got {value!r}")
    return name, Path(value)


def _validate_existing_directory(name: str, path: Path) -> Path:
    """Verify ``path`` is resolvable (no symlink loop) and an existing directory."""
    try:
        resolve_path(path)
    except (OSError, RuntimeError) as exc:
        raise MachineryConfigError(f"{name} ({path}) cannot be resolved: {exc}") from None
    if not os.path.isdir(path):
        raise MachineryConfigError(f"{name} ({path}) must be an existing directory")
    return path


def configured_primary_root(env: Mapping[str, str] | None = None) -> Path | None:
    """The primary checkout named by the configuration, or ``None`` for the Git-derived default.

    Only a checkout whose ``.git`` is a real Git directory (not a symlink) holding no
    ``commondir`` file is accepted: that is the one shape whose Git common directory is
    its own ``.git``. Anything else (a gitfile, a ``.git`` symlink such as one to a
    linked worktree's administrative directory, a copied worktree gitdir, an empty or
    stray ``.git`` directory, or no ``.git``) is refused, since anchoring state there
    would split task records from the locks closeout reads (#5171 class).
    """
    setting = _configured_path(LU_PRIMARY_ROOT_ENV, env)
    if setting is None:
        return None
    name, configured = setting
    if not _is_primary_git_layout(configured, env):
        raise MachineryConfigError(
            f"{name} must name a primary checkout, not a linked worktree or a stray .git: "
            f"{configured}/.git is not a real Git directory of its own"
        )
    return configured


def _is_primary_git_layout(checkout: Path, env: Mapping[str, str] | None = None) -> bool:
    """``checkout/.git`` is a non-symlink Git directory with no ``commondir`` file.

    A Git directory passes Git's own test (``is_git_directory`` in Git's ``setup.c``): a
    valid ``HEAD`` (:func:`_valid_git_head`), an object directory (``objects``, or
    ``GIT_OBJECT_DIRECTORY`` when the environment names an absolute one) and ``refs``. An
    empty or stray ``.git`` is not one. A relative ``GIT_OBJECT_DIRECTORY`` names a directory
    below whatever the caller's working directory is, not a property of this checkout, so it
    is ignored and the checkout's own ``objects`` decides: the answer never depends on the
    working directory.
    """
    git_dir = checkout / ".git"
    try:
        if not stat.S_ISDIR(os.lstat(git_dir).st_mode):
            return False
    except OSError:
        return False
    if os.path.lexists(git_dir / "commondir"):
        return False
    override = _env(env).get(_GIT_OBJECT_DIRECTORY_ENV, "")
    objects = override if os.path.isabs(override) else git_dir / "objects"
    return _valid_git_head(git_dir / "HEAD") and os.path.isdir(objects) and os.path.isdir(git_dir / "refs")


def _valid_git_head(head: Path) -> bool:
    """Git's ``validate_headref``: a symlink into ``refs/`` (its target need not exist yet),
    a ``ref: refs/...`` symbolic ref, or an object ID (a detached ``HEAD``)."""
    try:
        if stat.S_ISLNK(os.lstat(head).st_mode):
            return os.readlink(head).startswith("refs/")
        with open(head, "rb") as handle:
            text = handle.read(255).decode("ascii", "replace")
    except OSError:
        return False
    if text.startswith("ref:"):
        return text[4:].lstrip().startswith("refs/")
    return _GIT_OBJECT_ID_RE.match(text) is not None


def runtime_primary_root(start: Path | None = None) -> Path:
    """The running code's own primary checkout, from its Git metadata; never configured.

    ``start`` is the caller's own checkout (``Path(__file__).resolve().parents[2]``),
    default this module's. Only the in-tree source copy has runtime Git provenance;
    installed machinery raises StateRootError even with a configured product primary.
    A product checkout cannot identify the code of the installed distribution.
    """
    _require_in_tree_default(
        StateRootError, "runtime source checkout", "installed machinery has no Git source checkout",
    )
    return main_checkout_root(RUNTIME_ROOT if start is None else start)


def primary_root(start: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The configured primary checkout, else the primary that owns ``start`` (default: the runtime).

    The fallback exists only in the in-tree source copy. The installed package requires
    ``LU_PRIMARY_REPO_ROOT`` (or its alias) and raises :class:`StateRootError` without it,
    whatever ``start`` is: callers pass their own install location there, and a
    ``site-packages`` directory is never a state anchor.
    """
    configured = configured_primary_root(env)
    if configured is not None:
        return configured
    _require_in_tree_default(
        StateRootError,
        "shared-state anchor",
        f"set {LU_PRIMARY_ROOT_ENV} (or its alias {PRIMARY_ROOT_ENV}) to the primary checkout that holds batch_state/",
    )
    return main_checkout_root(RUNTIME_ROOT if start is None else start)


def valid_github_slug(value: object) -> bool:
    """``value`` is an ``owner/name`` GitHub slug: owner alphanumerics with inner hyphens, a plain name."""
    return (
        isinstance(value, str)
        and _GITHUB_SLUG_RE.fullmatch(value) is not None
        and value.split("/")[1] not in (".", "..")
    )


def env_github_slug(env: Mapping[str, str] | None = None) -> str | None:
    """The ``owner/name`` slug set by ``LU_GITHUB_SLUG`` or its alias, ``None`` when unset or empty."""
    setting = _setting(LU_GITHUB_SLUG_ENV, env)
    if setting is None:
        return None
    name, value = setting
    if not valid_github_slug(value):
        raise MachineryConfigError(f"{name} must be an owner/name GitHub slug, got {value!r}")
    return value


def _profile_reader():
    """The consumer profile module beside this resolver, imported only when a plane owner may hold a profile.

    It is a sibling in the same package, whatever the package is called. A by-file copy of this
    resolver outside a package has no profile reader and refuses, rather than ignoring a profile.
    """
    try:
        if not __package__:
            raise ImportError("loaded outside its package")
        return importlib.import_module(".profile", __package__)
    except ImportError as exc:
        raise MachineryConfigError(f"the consumer profile reader beside this resolver is unavailable: {exc}") from None


def _may_hold_profile(env: Mapping[str, str] | None) -> bool:
    """False only when no host profile is configured and the plane owner provably has no profile.

    Anything else, an unreadable owner included, goes to the profile reader, which decides or refuses.
    """
    if _env(env).get(PROFILE_PATH_ENV, ""):
        return True
    try:
        owner = state_root(env=env)
    except StateRootError:
        return False
    try:
        os.lstat(owner / CONSUMER_PROFILE_DIR)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def configured_github_slug(env: Mapping[str, str] | None = None) -> str | None:
    """The one authoritative ``owner/name`` slug: the plane owner's profile, else the environment.

    The profile (:mod:`.profile`) and ``LU_GITHUB_SLUG`` (or its alias) may both name the slug only
    when they agree; a conflict refuses before any caller acts on either. ``None`` when neither
    names one. A plane owner without a ``.lu-machinery`` entry has no profile, so the profile
    reader is not needed for it.
    """
    if _may_hold_profile(env):
        selected = _profile_reader().load_profile(env=env)
        if selected is not None:
            slug: str = selected.github_slug
            return slug
    return env_github_slug(env)


def github_slug(env: Mapping[str, str] | None = None) -> str:
    """The configured ``owner/name`` GitHub slug, else this repository's.

    ``DEFAULT_GITHUB_SLUG`` is the compatibility path for a plane owner with no consumer profile
    and no configured slug (learn-ukrainian before its profile lands); a consumer with a profile
    never reaches it.
    """
    return configured_github_slug(env) or DEFAULT_GITHUB_SLUG


def configured_host_state_root(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the validated host binding's state root when the binding exists."""
    from lu_machinery import host_context

    settings = _env(env)
    try:
        return host_context.load_host_context(role="state", env=settings).state_root
    except host_context.HostContextError as exc:
        if exc.code == "missing_context" and host_context.CONTEXT_ENV not in settings:
            return None
        raise StateRootError(str(exc)) from exc


def configured_state_root(env: Mapping[str, str] | None = None) -> Path | None:
    """The host context state root or explicit legacy state root, else ``None``.

    A legacy setting may name a dedicated non-Git directory or a primary checkout.
    Invalid settings raise ``MachineryConfigError``.
    """
    host_state = configured_host_state_root(env)
    if host_state is not None:
        return host_state

    setting = _configured_path(LU_STATE_ROOT_ENV, env)
    if setting is None:
        return None
    name, configured = setting
    _validate_existing_directory(name, configured)
    if os.path.lexists(configured / ".git") and not _is_primary_git_layout(configured, env):
        raise MachineryConfigError(
            f"{name} must name a dedicated non-Git directory or a primary checkout, "
            f"not a linked worktree or a stray .git: {configured}/.git is not a real Git directory of its own"
        )
    return configured


def state_root(start: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Resolve durable state from the host binding or explicit legacy setting.

    The retained `start` argument has no effect on state placement.
    """
    configured = configured_state_root(env)
    if configured is None:
        raise StateRootError(
            f"no durable state root configured: provide a host context or set {LU_STATE_ROOT_ENV}"
        )
    return configured


def configured_repos_root(env: Mapping[str, str] | None = None) -> Path | None:
    """The explicitly configured repository checkouts root (``LU_REPOS_ROOT``), else ``None``.

    Must name an existing non-Git directory holding sibling repository checkouts.
    """
    setting = _configured_single_path(REPOS_ROOT_ENV, env)
    if setting is None:
        return None
    name, configured = setting
    _validate_existing_directory(name, configured)
    if os.path.lexists(configured / ".git"):
        raise MachineryConfigError(
            f"{name} ({configured}) must name the parent directory holding repository checkouts, "
            "not a Git checkout itself"
        )
    return configured


def repos_root(start: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Directory holding fleet repository checkouts: ``LU_REPOS_ROOT``, else ``primary_root().parent``."""
    configured = configured_repos_root(env)
    if configured is not None:
        return configured
    try:
        return primary_root(start, env).parent
    except StateRootError as exc:
        raise StateRootError(
            f"no repository checkouts root is configured: set {REPOS_ROOT_ENV} "
            f"(or {LU_PRIMARY_ROOT_ENV} / {PRIMARY_ROOT_ENV}): {exc}"
        ) from None


def configured_product_root(env: Mapping[str, str] | None = None) -> Path | None:
    """Validated ``LU_PRODUCT_ROOT`` when set, else ``None`` (never raises when unset)."""
    if not _env(env).get(PRODUCT_ROOT_ENV, ""):
        return None
    return product_root(env=env)


def is_configured_state_root(path: Path, env: Mapping[str, str] | None = None) -> Path | None:
    """Resolved configured state root when ``path`` names it, else ``None``."""
    configured = configured_state_root(env)
    if configured is None:
        return None
    resolved_configured = resolve_path(configured)
    if resolve_path(path) == resolved_configured:
        return resolved_configured
    return None


def state_root_for_checkout(checkout_primary: Path, env: Mapping[str, str] | None = None) -> Path:
    """Resolve the shared state for a caller operating on a checkout."""
    return state_root(env=env)


def rules_dir() -> Path:
    """Fleet rules shipped beside this machinery package; never configured."""
    return machinery_path(RULES_DIR_REL)


def configured_interpreter(env: Mapping[str, str] | None = None) -> Path | None:
    """The interpreter named by the configuration, or ``None`` for :func:`project_interpreter`'s order."""
    setting = _configured_path(LU_INTERPRETER_ENV, env)
    return None if setting is None else setting[1]


@dataclass(frozen=True)
class MachineryRoots:
    """The distinct roots and the GitHub slug, as one resolved view."""

    runtime: Path
    primary: Path | None
    github_slug: str
    state: Path
    rules_dir: Path
    interpreter: Path
    repos: Path | None = None


def machinery_roots(env: Mapping[str, str] | None = None) -> MachineryRoots:
    """Resolve every machinery root from the configuration and its defaults.

    Raises :class:`MachineryConfigError` for an unusable or (installed package) missing
    state anchor, :class:`StateRootError` being the missing case, and ``FileNotFoundError``
    when no interpreter can be resolved.
    """
    state = state_root(env=env)
    primary = configured_primary_root(env)
    if primary is None and in_tree_source():
        primary = primary_root(env=env)
    repos = configured_repos_root(env)
    if repos is None and primary is not None:
        repos = primary.parent
    return MachineryRoots(
        runtime=RUNTIME_ROOT,
        primary=primary,
        github_slug=github_slug(env),
        state=state,
        rules_dir=rules_dir(),
        interpreter=project_interpreter(env=env),
        repos=repos,
    )


def machinery_root() -> Path:
    """The machinery package directory: the resolved directory that holds ``common/``.

    It is found from this file's own location, so it names the package wherever the code
    runs from and whatever the package is called. It is never configured, and it is not
    the checkout-level runtime root of :func:`machinery_roots`.
    """
    return _MACHINERY_ROOT


def resolve_path(path: Path | str, *, strict: bool = False) -> Path:
    """``Path(path).resolve(strict=strict)`` that refuses a symlink loop on every supported Python.

    Python 3.12's ``Path.resolve`` raises ``RuntimeError("Symlink loop from ...")`` for a loop in
    either mode. Python 3.13 changed that: non-strict resolution returns the looping path
    unchanged and strict resolution raises ``OSError(ELOOP)``
    (https://docs.python.org/3/library/pathlib.html#pathlib.Path.resolve). A guard that treats
    ``RuntimeError`` as "cannot be resolved" would then accept the loop. This restores the 3.12
    behaviour with the same two steps 3.12 used: a loop reported by the resolution itself, then
    an ``ELOOP`` from a stat of the non-strict result. Every other stat failure is ignored, as a
    non-strict resolution of a missing leaf must still succeed.
    """
    try:
        resolved = Path(path).resolve(strict=strict)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RuntimeError(f"Symlink loop from {exc.filename!r}") from None
        raise
    if not strict:
        try:
            os.stat(resolved)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(f"Symlink loop from {exc.filename!r}") from None
    return resolved


def machinery_path(relative: str) -> Path:
    """The machinery file or directory at package-relative ``relative``, which need not exist for code; shared resources must exist.

    ``relative`` is a trusted, package-relative path such as ``"config/model_catalog.yaml"``.
    The checks are ``path_safety.safe_join``'s, made stricter: an empty, absolute or
    NUL-bearing value, and any empty, ``.``, ``..`` or ``:``/``\\``-bearing component, raise
    ``ValueError``; so does a path whose resolved location leaves :func:`machinery_root`,
    such as one through a symlink that points outside the package, or one that cannot be
    resolved (a symlink loop). Unlike ``safe_join`` there is no release live-data exception:
    a machinery file never resolves outside the package. The returned path is logical
    (symlinks inside the package are not expanded). This module must load without the
    package (hooks load it by file; the AGY store contract projects it without
    ``path_safety.py``), so the checks are inlined rather than imported.
    """
    if not isinstance(relative, str):
        raise TypeError(f"machinery_path takes a str, got {type(relative).__name__}")
    if not relative or "\x00" in relative:
        raise ValueError("machinery path must be a non-empty relative path without NUL")
    if os.path.isabs(relative):
        raise ValueError(f"machinery path must be package-relative, got absolute {relative!r}")
    components = relative.split("/")
    if relative.endswith("/"):
        components.pop()
    for component in components:
        if component in ("", ".", "..") or ":" in component or "\\" in component:
            raise ValueError(f"machinery path has an invalid component {component!r}: {relative!r}")
    root = machinery_root()
    if components[:2] == ["agents_extensions", "shared"]:
        # The wheel preserves this sibling layout. No source/install branch or
        # import search participates, even when product namespaces overlap.
        root = root.parent / "agents_extensions" / "shared"
        if resolve_path(root) != root:
            raise ValueError("machinery resource tree must not be a symlink")
        components = components[2:]
    logical = root.joinpath(*components)
    try:
        resolved = resolve_path(logical)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"machinery path {relative!r} cannot be resolved: {exc}") from exc
    if os.path.commonpath([root, resolved]) != str(root):
        raise ValueError(f"machinery path {relative!r} resolves outside the machinery package: {resolved}")
    if (relative == "agents_extensions/shared" or relative.startswith("agents_extensions/shared/")) and not logical.exists():
        raise FileNotFoundError(f"machinery release resource unavailable: {relative}")
    return logical


class ProductRootError(MachineryConfigError):
    """No product checkout can be selected; the message names the candidate and the rule it broke."""


def product_tree_path(*parts: str, root: Path | None = None) -> Path:
    """A CI input or Python tree in the selected checkout, never in the wheel.

    Products keep ``scripts``. Machinery's own checkout uses its package name
    when that directory exists there. Missing product inputs keep their product
    spelling and fail at the reader; they never select a packaged copy.
    """
    selected = product_root(root)
    tree = _PRODUCT_TREE
    if not (selected / tree).is_dir() and (selected / _MACHINERY_ROOT.name).is_dir():
        tree = _MACHINERY_ROOT.name
    return selected.joinpath(tree, *parts)


def _carries_product_marker(checkout: Path, *, through_primary: bool) -> bool:
    """``checkout`` has ``PRODUCT_MARKER``; with ``through_primary``, or its primary checkout has it.

    Dispatch worktrees are sparse checkouts without ``curriculum/``. For the derived default only,
    a linked worktree whose primary checkout (found from Git metadata alone, by
    :func:`main_checkout_root`, never from the configured primary root) has the marker qualifies;
    the worktree itself stays the selected root, and nothing is read from the primary. A release
    snapshot has no Git metadata; its marker comes through its declared ``curriculum`` live-data
    link (``scripts/common/release_layout.py``). Qualifying proves the layout, not that every
    product file is present: a reader whose required file is missing fails through its own
    missing-input error.
    """
    # An unreadable candidate, or unreadable, malformed or looping Git metadata, does not qualify:
    # OSError covers permissions, ValueError covers NUL bytes and UnicodeDecodeError, and
    # RuntimeError is what resolve_path() raises for a symlink loop.
    try:
        if (checkout / PRODUCT_MARKER).is_file():
            return True
        if not through_primary:
            return False
        primary = main_checkout_root(checkout)
        return primary != checkout and (primary / PRODUCT_MARKER).is_file()
    except (OSError, ValueError, RuntimeError):
        return False


def product_root(explicit: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """The product checkout whose files machinery reads, or raise :class:`ProductRootError`.

    The candidate is ``explicit`` when given, else ``LU_PRODUCT_ROOT`` when set and not
    empty. An explicit or configured candidate must be an absolute path to an existing
    directory; it need not carry ``PRODUCT_MARKER``, since a product other than the
    Ukrainian curriculum is still a product, and the reading operation validates its own
    inputs. With neither given, the installed package refuses: it has no implicit product.
    Only the in-tree source copy then derives the checkout that holds this package from the
    unresolved module location; that derived candidate must carry ``PRODUCT_MARKER``, or be
    a linked worktree of a checkout that carries it (see :func:`_carries_product_marker`).
    Only the one selected candidate is checked: one that fails raises at once instead of
    falling through to a later one. The path is returned as given (symlinks are not
    resolved), so a release snapshot stays the snapshot.

    Call it when a feature runs or an application starts, never at import, and resolve it
    before a cache lookup so the cache is keyed by the selected root.
    """
    derived = False
    if explicit is not None:
        source, value = "the explicit product root", os.fspath(explicit)
    elif _env(env).get(PRODUCT_ROOT_ENV, ""):
        source, value = PRODUCT_ROOT_ENV, _env(env)[PRODUCT_ROOT_ENV]
    else:
        _require_in_tree_default(
            ProductRootError,
            "product",
            f"name the product checkout with {PRODUCT_ROOT_ENV} or an explicit product root",
        )
        source, value, derived = "the checkout holding the machinery package", os.fspath(_PACKAGE_CHECKOUT), True
    if not isinstance(value, str) or "\x00" in value or not os.path.isabs(value):
        raise ProductRootError(f"{source} must be an absolute path, got {value!r}")
    candidate = Path(value)
    if not derived:
        if not os.path.isdir(candidate):
            raise ProductRootError(f"{source} ({candidate}) is not an existing directory")
        return candidate
    if not _carries_product_marker(candidate, through_primary=True):
        raise ProductRootError(
            f"{source} ({candidate}) is not a product checkout: it has no {PRODUCT_MARKER}; "
            f"name the product checkout with {PRODUCT_ROOT_ENV} or an explicit product root"
        )
    return candidate


def main_checkout_root(repo_root: Path) -> Path:
    """Return the primary checkout root that owns the shared ``.git`` dir.

    Only a ``gitdir:`` target with Git's exact ``.git/worktrees/<name>`` shape
    redirects a path. Primary checkouts, release snapshots, and every other
    malformed or non-Git root remain anchored to themselves.
    """
    git_path = repo_root / ".git"
    if git_path.is_dir():
        return repo_root
    if not git_path.is_file():
        return repo_root

    try:
        first_line = git_path.read_text().splitlines()[0]
    except (IndexError, OSError):
        return repo_root
    prefix = "gitdir:"
    if not first_line.startswith(prefix):
        return repo_root

    git_dir = Path(first_line[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    git_dir = resolve_path(git_dir)
    if git_dir.parent.name != "worktrees":
        return repo_root
    common_git_dir = git_dir.parent.parent
    if common_git_dir.name != ".git":
        return repo_root
    return common_git_dir.parent


def _absolute_text(path: Path) -> str:
    text = os.fspath(path)
    if os.path.isabs(text):
        return text
    return os.path.join(os.getcwd(), text)


def _realpath(path: Path) -> Path:
    """``os.path.realpath`` of ``path``, or a normalized absolute path if that fails."""
    text = _absolute_text(path)
    try:
        resolved = os.path.realpath(text)
    except OSError:
        resolved = os.path.normpath(text)
    return Path(resolved)


def _venv_python(checkout: Path) -> Path:
    """Path of ``checkout``'s project interpreter, whether or not the file exists."""
    # Resolver definition: this join names that checkout's interpreter.
    return checkout / ".venv" / "bin" / "python"


def _executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def project_interpreter(root: Path | None = None, *, env: Mapping[str, str] | None = None) -> Path:
    """Return the project interpreter for ``root`` or this checkout.

    Called when a spawn needs the interpreter, not while this module is
    imported. Import therefore succeeds when no project interpreter can be
    found; this function raises ``FileNotFoundError`` at the call.

    A configured ``LU_PYTHON`` (or its alias ``LEARN_UKRAINIAN_PYTHON``) wins for every checkout. It must be an
    absolute path to an executable file. A relative path raises
    ``MachineryConfigError``. A missing or non-executable file raises
    ``FileNotFoundError``. There is no fallback. The operator who sets it
    vouches for it, as for ``PATH``. Unset, the interpreter is the validated
    primary checkout's ``.venv/bin/python`` when that file is executable. Without an
    explicit checkout, the configured primary supplies that root; installed code
    never searches beside the package for an interpreter. The
    primary is the git common-dir checkout ``main_checkout_root`` finds for a
    linked worktree, and the checkout itself when ``.git`` is a directory.
    A gitfile that is not Git's ``.git/worktrees/<name>`` shape is not a
    primary, so its ``.venv`` is refused. A worktree-local ``.venv`` and
    ``sys.executable`` are not used. Anything else raises
    ``FileNotFoundError``.
    """
    setting = _configured_path(LU_INTERPRETER_ENV, env)
    if setting is not None:
        name, configured = setting
        if not _executable_file(configured):
            raise FileNotFoundError(
                f"project interpreter not found: {name}={configured} is not an executable file"
            )
        return configured
    repo = primary_root(env=env) if root is None else Path(root)
    primary = main_checkout_root(repo)
    # main_checkout_root anchors a malformed gitfile to the directory itself.
    # That directory is a linked worktree, not the primary, so its .venv is refused.
    if (repo / ".git").is_file() and resolve_path(primary) == resolve_path(repo):
        refused = repo / ".venv" / "bin" / "python"
        raise FileNotFoundError(
            f"project interpreter not found: {refused} (a linked worktree .venv is not used)"
        )
    executable = _venv_python(primary)
    if _executable_file(executable):
        return executable
    raise FileNotFoundError(f"project interpreter not found: {executable} is not an executable file")


def resolve_repo_root(script_path: Path, parents: int) -> Path:
    """Compatibility spelling for the configured durable state root."""
    return state_root()


# Hook-boundary bridge (machinery #100, E3a). Fixed texts every native hook entry
# uses for a context that cannot resolve; they hold no payload or environment value.
HOOK_BOOTSTRAP_DIAGNOSTIC = "HOOK-BOOTSTRAP-FAILED: product root, shared state, interpreter or machinery unavailable."
HOOK_BOOTSTRAP_REASON = "HOOK-BOOTSTRAP-FAILED: hook context unavailable."
# Sources the packaged library ($1) and prints the two roots NUL-terminated.
_HOOK_ROOTS_SCRIPT = (
    'source "$1" || exit 1; project_hook_roots_resolve || exit 1; '
    'printf "%s\\0%s\\0" "$LU_PRODUCT_ROOT" "$LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"'
)
# The only environment the shell rule reads (PATH for dirname/basename).
_HOOK_ROOTS_ENV = ("PATH", PRODUCT_ROOT_ENV, "CLAUDE_PROJECT_DIR", PRIMARY_ROOT_ENV)


def _has_git_marker(directory: Path) -> bool:
    """Return True if ``directory/.git`` exists; only ``FileNotFoundError`` proves absence."""
    try:
        os.lstat(directory / ".git")
    except FileNotFoundError:
        return False
    return True


def configured_hook_state_root(primary: Path) -> Path | None:
    """Resolve the explicit state directory for a hook with a validated primary checkout.

    The native hook command's compatibility alias can name the primary. A
    detached root is configured with ``LU_STATE_ROOT`` and lives outside Git,
    including when reached through a directory alias.
    """
    try:
        configured = configured_state_root()
        if configured is None:
            return None
        configured_primary_root()
        resolved = resolve_path(configured)
        if "\n" in os.fspath(resolved):
            raise MachineryConfigError("the physical hook state root contains a newline")
        if resolved == resolve_path(primary):
            return resolved
        if os.environ.get(STATE_ROOT_ENV):
            raise MachineryConfigError(f"{STATE_ROOT_ENV} must name the primary checkout; use {LU_STATE_ROOT_ENV} for detached state")
        ancestors = (configured, *configured.parents, resolved, *resolved.parents)
        if any(_has_git_marker(ancestor) for ancestor in ancestors):
            raise MachineryConfigError(f"{LU_STATE_ROOT_ENV} must name the primary checkout or a directory outside Git")
        return resolved
    except (OSError, RuntimeError) as exc:
        raise MachineryConfigError(f"the hook state root could not be verified: {exc}") from None


def native_hook_roots(invocation_dir: Path) -> tuple[Path, Path]:
    """The product and primary checkout for ``invocation_dir`` by the one hook rule; read-only.

    Run ``project_hook_roots_resolve`` from the packaged ``lib/project_interpreter.sh``
    with stdin disconnected and ``invocation_dir`` as the working directory. The
    rule checks the nearest ``.git`` entry even when a product is configured, so
    a symlinked or malformed ``.git`` there refuses. Check the explicit state
    setting against the validated primary checkout. Leave the environment intact.
    A refusal, a timeout or a malformed answer raises :class:`ProductRootError`.
    """
    import subprocess

    library = machinery_path("lib/project_interpreter.sh")
    env = {name: os.environ[name] for name in _HOOK_ROOTS_ENV if name in os.environ}
    # The shell rule supplies the primary checkout. Validate explicit state
    # once the primary is known, as the shell lifecycle hooks do.
    try:
        primary_setting = _setting(LU_PRIMARY_ROOT_ENV, None)
        if primary_setting is not None:
            env[PRIMARY_ROOT_ENV] = primary_setting[1]
    except MachineryConfigError as exc:
        raise ProductRootError(f"no hook context resolves from {invocation_dir}: {exc}") from None
    try:
        # Fixed /bin/bash argv; the library path and directory are arguments, never shell text.
        result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            ["/bin/bash", "-c", _HOOK_ROOTS_SCRIPT, "lu-hook-roots", os.fspath(library)],
            cwd=invocation_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ProductRootError(f"no hook context resolves from {invocation_dir}: the root resolver did not run") from None
    # Bytes from run(); a str only from a caller that replaced subprocess.run (fsencode takes both).
    fields = os.fsencode(result.stdout).split(b"\0")
    if result.returncode != 0 or len(fields) != 3 or fields[2] or not all(fields[:2]):
        raise ProductRootError(
            f"no hook context resolves from {invocation_dir}: set {PRODUCT_ROOT_ENV} to the product checkout "
            f"and {PRIMARY_ROOT_ENV} to its primary checkout"
        )
    product, primary = (Path(os.fsdecode(field)) for field in fields[:2])
    if not product.is_absolute() or not primary.is_absolute():
        raise ProductRootError(f"no hook context resolves from {invocation_dir}: the resolver answered a relative root")
    try:
        configured_hook_state_root(primary)
    except MachineryConfigError as exc:
        raise ProductRootError(f"no hook context resolves from {invocation_dir}: {exc}") from None
    return product, primary


def configure_native_hook_roots(invocation_dir: Path) -> None:
    """At a direct hook or provider boundary, validate the hook context and set missing roots.

    Every call applies the shared shell rule (:func:`native_hook_roots` for
    ``invocation_dir``, the caller's explicit invocation directory, never a payload's
    working directory), so configured roots get the same Git-metadata checks as the
    generated hook command; a refusal raises :class:`ProductRootError`. Configured
    ``LU_PRODUCT_ROOT`` and primary-checkout values (``LU_PRIMARY_REPO_ROOT`` or its alias)
    are kept; only missing ones are set, the primary under its alias name. The in-process validators then run as well. Call it only from
    a hook or launch boundary, never at import.
    """
    product, primary = native_hook_roots(invocation_dir)
    if not os.environ.get(PRODUCT_ROOT_ENV):
        os.environ[PRODUCT_ROOT_ENV] = os.fspath(product)
    if _setting(LU_PRIMARY_ROOT_ENV, None) is None:
        os.environ[PRIMARY_ROOT_ENV] = os.fspath(primary)
    product_root()
    state_root()
