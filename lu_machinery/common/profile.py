"""The consumer profile: one committed file that names a consumer's identity and configuration.

Machinery serves more than one consumer (learn-ukrainian, kube-dojo, ...). Each consumer runs its
own control plane, whose owner is the primary checkout that holds the shared state
(:func:`repo_root.state_root`). That checkout commits one public-safe file,
``<state root>/.lu-machinery/profile.v1.json``, which binds the plane to its consumer. It holds no
host paths or secrets:

.. code-block:: json

    {
      "schema": "lu-machinery.profile",
      "version": 1,
      "profile_id": "learn-ukrainian",
      "github_slug": "learn-ukrainian/learn-ukrainian.github.io",
      "config": {"fleet_repos": ".lu-machinery/fleet_repos.yaml"}
    }

Every field is required and no other field is accepted. ``config`` maps a consumer config name
(:data:`CONSUMER_CONFIG_NAMES`) to a path relative to the plane owner root. Deployment sources stay
in ``agents_extensions/deployment.v1.json`` and review hooks in their own descriptor; the profile
does not repeat them.

Selection. There is one candidate, the plane owner's profile, so a dispatch worktree, a product
checkout or the working directory can never supply another one: every process anchored on the same
plane reads the same file. An absent file means no profile. A present but invalid one (unreadable,
a symlink, a non-regular file, a file owned by another user, larger than
:data:`MAX_PROFILE_BYTES`, not strict UTF-8 JSON, a duplicate member, a non-finite number, a
missing or unknown field, or an invalid value) raises :class:`ProfileError` and never falls back
to the no-profile behaviour. Reads never follow a symlink below the plane owner root.

Identity. The profile's ``github_slug`` is the one authoritative slug
(:func:`repo_root.configured_github_slug`). ``LU_GITHUB_SLUG`` (or its alias) may also be set only
to the same value; a conflict raises :class:`ProfileError` when the profile is loaded, before any
caller acts on either value.

Configuration. :func:`config_path` resolves a consumer config name through the active profile.
Without ``default``, no plane owner, no profile, or no entry for the name raises
:class:`ProfileError` naming the name and the profile path. With ``default`` supplied by a
registry loader, those three absent-profile / omitted-entry cases return ``default`` so an
install without a profile reads packaged defaults unchanged; an unknown name, an invalid
profile, a slug conflict, or a missing/unsafe declared target still raises :class:`ProfileError`
and never falls back.
Machinery's own configuration keeps being read through :func:`repo_root.machinery_path`.

Like :mod:`.repo_root`, nothing here runs at import, and resolution only reads.
"""

from __future__ import annotations

import errno
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import json_depth, repo_root
from .safe_open import UnsafeEntryError, safe_open_below

PROFILE_DIR = repo_root.CONSUMER_PROFILE_DIR
PROFILE_NAME = "profile.v1.json"
PROFILE_REL = f"{PROFILE_DIR}/{PROFILE_NAME}"
PROFILE_PATH_ENV = repo_root.PROFILE_PATH_ENV
PROFILE_SCHEMA = "lu-machinery.profile"
PROFILE_VERSION = 1
MAX_PROFILE_BYTES = 64 * 1024
# The profile object holds the config object, and nothing nests deeper.
_MAX_DEPTH = 2
_MAX_CONFIG_PATH_CHARS = 1024
_FIELDS = frozenset({"schema", "version", "profile_id", "github_slug", "config"})
_PROFILE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
# Consumer and deployment data a consumer owns. Backup and retention deployment inputs are not
# runtime configuration and stay out of the profile.
CONSUMER_CONFIG_NAMES = frozenset({
    "area_assignments",
    "code_owners",
    "credit_lanes",
    "driver_lanes",
    "fleet_repos",
    "fleet_taxonomy",
    "issue_streams",
    "launcher_stream_aliases",
    "terminal_abandon_succession",
})
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


class ProfileError(repo_root.MachineryConfigError):
    """The consumer profile, or a consumer config entry it names, cannot be used; the message says why."""


@dataclass(frozen=True)
class Profile:
    """A validated consumer profile, bound to the plane owner root it was read from."""

    path: Path
    owner_root: Path
    profile_id: str
    github_slug: str
    config: Mapping[str, str]


def _configured_profile_path(env: Mapping[str, str] | None) -> Path | None:
    """Validated explicit host profile path from ``LU_PROFILE_PATH``, else ``None``."""
    raw = (os.environ if env is None else env).get(PROFILE_PATH_ENV, "")
    if not raw:
        return None
    if (
        "\x00" in raw
        or "\\" in raw
        or raw.endswith("/")
        or any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in raw)
        or not os.path.isabs(raw)
    ):
        raise ProfileError(f"{PROFILE_PATH_ENV} must be a clean absolute file path, got {raw!r}")
    path = Path(raw)
    if ".." in path.parts or not path.name or path.parent == path:
        raise ProfileError(f"{PROFILE_PATH_ENV} must name a profile file without parent traversal, got {raw!r}")
    return path


def load_profile(*, env: Mapping[str, str] | None = None) -> Profile | None:
    """The configured host profile (``LU_PROFILE_PATH``) or the plane owner's validated profile.

    When ``LU_PROFILE_PATH`` is set, that file is the sole candidate and a missing or invalid
    file fails closed (:class:`ProfileError`). Otherwise reads
    ``<state_root>/.lu-machinery/profile.v1.json``, returning ``None`` when absent or when no
    plane owner is configured.
    """
    explicit = _configured_profile_path(env)
    if explicit is not None:
        return _load_explicit(explicit, env)
    try:
        owner = repo_root.state_root(env=env)
    except repo_root.StateRootError:
        return None
    return _load(owner, env)


def config_path(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    default: Path | None = None,
) -> Path:
    """The consumer config file ``name`` as the active profile names it.

    The returned path is below the profile's ``owner_root`` (``LU_PROFILE_PATH.parent`` for a
    host profile, else ``state_root()``) and was, when checked, a regular file owned by
    the current user reached without a symlink. Raises :class:`ProfileError` naming ``name`` when it
    is not a consumer config name, when the active profile is invalid or conflicts on slug, or when
    a declared target entry is missing, a symlink or not a regular file. When ``default`` is given,
    an absent plane owner, absent profile, or valid profile omitting ``name`` returns ``default``;
    otherwise those cases raise :class:`ProfileError` too.
    """
    if name not in CONSUMER_CONFIG_NAMES:
        raise ProfileError(
            f"{name!r} is not a consumer config name; machinery's own configuration is read through machinery_path"
        )
    explicit = _configured_profile_path(env)
    if explicit is not None:
        profile: Profile | None = _load_explicit(explicit, env)
        owner = explicit.parent
        where = explicit
    else:
        try:
            owner = repo_root.state_root(env=env)
        except repo_root.StateRootError as exc:
            if default is not None:
                return default
            raise ProfileError(f"consumer config {name!r} needs a consumer profile, and no plane owner is configured: {exc}") from None
        profile = _load(owner, env)
        where = owner / PROFILE_REL
    if profile is None:
        if default is not None:
            return default
        raise ProfileError(f"consumer config {name!r} needs a consumer profile, and the plane owner has none at {where}")
    relative = profile.config.get(name)
    if relative is None:
        if default is not None:
            return default
        raise ProfileError(f"the consumer profile {where} names no {name!r} config")
    _check_regular_below(owner, relative, f"consumer config {name!r} ({relative}, named by {where})")
    return owner.joinpath(*relative.split("/"))


def _check_slug_agreement(profile: Profile, env: Mapping[str, str] | None) -> Profile:
    configured = repo_root.env_github_slug(env)
    if configured is not None and configured != profile.github_slug:
        raise ProfileError(
            f"the consumer profile {profile.path} names GitHub slug {profile.github_slug!r}, but "
            f"{repo_root.LU_GITHUB_SLUG_ENV} (or its alias {repo_root.GITHUB_SLUG_ENV}) names {configured!r}; "
            "the profile is the one authority: unset the variable or make it agree"
        )
    return profile


def _load(owner: Path, env: Mapping[str, str] | None) -> Profile | None:
    path = owner / PROFILE_REL
    blob = _read(owner, path)
    if blob is None:
        return None
    return _check_slug_agreement(_parse(blob, path, owner), env)


def _load_explicit(path: Path, env: Mapping[str, str] | None) -> Profile:
    owner = path.parent
    blob = _read_explicit(owner, path)
    return _check_slug_agreement(_parse(blob, path, owner), env)


def _read_explicit(owner: Path, path: Path) -> bytes:
    """Read an explicitly configured host profile file; missing or unsafe fails closed."""
    owner_fd = _open_owner(owner)
    try:
        try:
            fd = safe_open_below(owner_fd, path.name, os.O_RDONLY)
        except FileNotFoundError:
            raise ProfileError(f"the configured consumer profile {path} does not exist") from None
        except UnsafeEntryError:
            raise ProfileError(
                f"the consumer profile {path} is a symlink, not a regular file, or not owned by the current user"
            ) from None
        except OSError as exc:
            raise ProfileError(f"the consumer profile {path} cannot be read: {exc.strerror}") from None
        with os.fdopen(fd, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_PROFILE_BYTES:
                raise ProfileError(f"the consumer profile {path} is larger than {MAX_PROFILE_BYTES} bytes")
            blob = handle.read(MAX_PROFILE_BYTES + 1)
        if len(blob) > MAX_PROFILE_BYTES:
            raise ProfileError(f"the consumer profile {path} is larger than {MAX_PROFILE_BYTES} bytes")
        return blob
    finally:
        os.close(owner_fd)


def _open_directory(name: str, dir_fd: int, what: str) -> int | None:
    """A no-follow descriptor for directory ``name`` below ``dir_fd``; ``None`` when it does not exist."""
    try:
        return os.open(name, _DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ProfileError(f"{what} is a symlink or not a directory") from None
        raise ProfileError(f"{what} cannot be opened: {exc.strerror}") from None


def _open_owner(owner: Path) -> int:
    try:
        return os.open(owner, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise ProfileError(f"the plane owner root {owner} cannot be opened: {exc.strerror}") from None


def _read(owner: Path, path: Path) -> bytes | None:
    """The profile's bytes, ``None`` when absent; refuse anything that is not a small regular file."""
    owner_fd = _open_owner(owner)
    try:
        directory = _open_directory(PROFILE_DIR, owner_fd, f"the consumer profile directory {owner / PROFILE_DIR}")
        if directory is None:
            return None
        try:
            try:
                fd = safe_open_below(directory, PROFILE_NAME, os.O_RDONLY)
            except FileNotFoundError:
                return None
            except UnsafeEntryError:
                raise ProfileError(
                    f"the consumer profile {path} is a symlink, not a regular file, or not owned by the current user"
                ) from None
            except OSError as exc:
                raise ProfileError(f"the consumer profile {path} cannot be read: {exc.strerror}") from None
            with os.fdopen(fd, "rb") as handle:
                if os.fstat(handle.fileno()).st_size > MAX_PROFILE_BYTES:
                    raise ProfileError(f"the consumer profile {path} is larger than {MAX_PROFILE_BYTES} bytes")
                blob = handle.read(MAX_PROFILE_BYTES + 1)
            if len(blob) > MAX_PROFILE_BYTES:
                raise ProfileError(f"the consumer profile {path} is larger than {MAX_PROFILE_BYTES} bytes")
            return blob
        finally:
            os.close(directory)
    finally:
        os.close(owner_fd)


class _DuplicateMember(ValueError):
    pass


def _members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember(f"duplicate member {key!r}")
        result[key] = value
    return result


def _non_finite(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def _parse(blob: bytes, path: Path, owner: Path) -> Profile:
    try:
        text = blob.decode("utf-8")
        data = json_depth.loads(
            text, max_depth=_MAX_DEPTH, object_pairs_hook=_members, parse_constant=_non_finite
        )
    except (ValueError, RecursionError) as exc:
        # UnicodeDecodeError and JSONDecodeError are ValueErrors; RecursionError is nesting.
        raise ProfileError(f"the consumer profile {path} is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ProfileError(f"the consumer profile {path} must be a JSON object")
    unknown = sorted(set(data) - _FIELDS)
    if unknown:
        raise ProfileError(f"the consumer profile {path} has unknown fields {unknown!r}")
    missing = sorted(_FIELDS - set(data))
    if missing:
        raise ProfileError(f"the consumer profile {path} lacks required fields {missing!r}")
    if data["schema"] != PROFILE_SCHEMA:
        raise ProfileError(f"the consumer profile {path} schema must be {PROFILE_SCHEMA!r}")
    if type(data["version"]) is not int or data["version"] != PROFILE_VERSION:
        raise ProfileError(f"the consumer profile {path} version must be {PROFILE_VERSION}")
    profile_id = data["profile_id"]
    if not isinstance(profile_id, str) or _PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise ProfileError(
            f"the consumer profile {path} profile_id must be lowercase letters, digits and inner hyphens, at most 64"
        )
    slug = data["github_slug"]
    if not repo_root.valid_github_slug(slug):
        raise ProfileError(f"the consumer profile {path} github_slug must be an owner/name GitHub slug")
    config = data["config"]
    if not isinstance(config, dict):
        raise ProfileError(f"the consumer profile {path} config must be a JSON object")
    for name, value in config.items():
        if name not in CONSUMER_CONFIG_NAMES:
            raise ProfileError(f"the consumer profile {path} config names an unknown consumer config {name!r}")
        reason = _relative_path_error(value)
        if reason is not None:
            raise ProfileError(f"the consumer profile {path} config {name!r} {reason}")
    return Profile(
        path=path,
        owner_root=owner,
        profile_id=profile_id,
        github_slug=slug,
        config=MappingProxyType(dict(config)),
    )


def _relative_path_error(value: object) -> str | None:
    """Why ``value`` is not a canonical relative POSIX path below the plane owner, or ``None``.

    The rule of ``agent_deploy.plan.relative``: no backslash, control, format or line-separator
    character, and no empty, ``.`` or ``..`` component (so no absolute path and no traversal).
    """
    if not isinstance(value, str) or not value:
        return "must be a nonempty relative path"
    if len(value) > _MAX_CONFIG_PATH_CHARS:
        return f"is longer than {_MAX_CONFIG_PATH_CHARS} characters"
    if "\\" in value or any(unicodedata.category(c).startswith("C") or c in "  " for c in value):
        return "has a backslash, control, format or line-separator character"
    if any(part in ("", ".", "..") for part in value.split("/")):
        return "must be a canonical relative path: no absolute path, empty, '.' or '..' component"
    return None


def _check_regular_below(owner: Path, relative: str, what: str) -> None:
    """``relative`` reaches, without a symlink, a regular file below ``owner`` owned by the current user."""
    *directories, leaf = relative.split("/")
    fd = _open_owner(owner)
    try:
        for part in directories:
            child = _open_directory(part, fd, f"a directory of {what}")
            if child is None:
                raise ProfileError(f"{what} does not exist")
            os.close(fd)
            fd = child
        try:
            os.close(safe_open_below(fd, leaf, os.O_RDONLY))
        except FileNotFoundError:
            raise ProfileError(f"{what} does not exist") from None
        except UnsafeEntryError:
            raise ProfileError(f"{what} is a symlink, not a regular file, or not owned by the current user") from None
        except OSError as exc:
            raise ProfileError(f"{what} cannot be read: {exc.strerror}") from None
    finally:
        os.close(fd)
