"""One shared, content-addressed store for the site dependencies of every dispatch worktree.

``npm ci`` copies every package into each worktree (about 570 MB of ``site/node_modules``), so a
handful of live worktrees filled the disk. The store keeps each distinct file once and gives each
worktree its own real ``site/node_modules`` made of hard links to those files: the same mechanism
pnpm's store uses, but over the tree ``npm ci`` produces from ``site/package-lock.json``, so a
worktree runs exactly the packages CI installs. (``pnpm import`` does not reproduce that lockfile:
it ignores npm ``overrides`` and resolves different versions.)

Isolation, as in ``git.provision_links``: no worktree path is a link into the primary checkout or
into another worktree. Deleting or replacing a worktree install (``npm ci``, ``rm -r``) unlinks
names and never touches the store. Store files are read-only, so a tool that edits a package file
in place fails loudly instead of silently changing every other worktree.

A tree is addressed by a key over everything that can change the installed bytes: the lockfile and
``package.json``, project ``.npmrc`` files, local ``file:``/``link:`` inputs and ``patches/``, the
Node and npm versions and module ABI, platform, architecture and libc, and the effective npm
configuration. File identity is the SHA-256 of the content plus the executable bit, so two keys
that differ in a native build share only the files that really are identical.

``python -m scripts.dispatcher.site_deps {ensure|status|prune|detach}``.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# File-path and isolated callers use this release's helper, never an installed copy.
_json_loads = runpy.run_path(str(Path(__file__).resolve().parents[1] / "common" / "json_depth.py"))["loads"]
LOGGER = logging.getLogger(__name__)
LAYOUT = 1
SITE = "site"
INSTALL_INPUTS = ("package.json", "package-lock.json", ".npmrc")
INSTALL_TIMEOUT = 900
KEEP_TREES = 3
# The install runs in a controlled environment, so ambient variables an install script might read
# (a custom esbuild binary path, a locale, a compiler override) cannot change its bytes behind the key.
# The key covers the inputs; the empty scratch HOME is equivalent across runs.
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin")
PASSED_ENV = frozenset({
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "NODE_OPTIONS",
})
NPM_CONFIG_PREFIX = "npm_config_"
NPM_HOME_SETTINGS = frozenset({
    "npm_config_cache", "npm_config_userconfig", "npm_config_globalconfig", "npm_config_logs_dir",
})
# Native modules build with these when no prebuilt binary exists, so their identity is part of the key.
BUILD_TOOLS = ("python3", "make", "cc", "c++")
# Shim directories of any checkout, under the machinery package named by this file's location.
_MACHINERY = Path(os.path.abspath(__file__)).parents[1].name
SHIM_DIRS = ((_MACHINERY, "agent_runtime", "shims"), (_MACHINERY, "dispatcher", "npm_shims"))
# Tool caches npm scripts write under node_modules at run time; never part of an install.
RUNTIME_CACHES = frozenset({".cache", ".vite", ".vite-temp", ".astro"})
# Rewritten by npm itself, so it is copied rather than shared.
COPIED = frozenset({".package-lock.json"})
STORE_ENV = "LEARN_UKRAINIAN_DEP_STORE"
DISABLE_ENV = "LEARN_UKRAINIAN_SITE_DEPS"
NODE_TOOLCHAIN_DIRS_ENV = "LU_NODE_TOOLCHAIN_DIRS"
# Default toolchain directory layout; hosts override via NODE_TOOLCHAIN_DIRS_ENV.
DEFAULT_NODE_TOOLCHAIN_DIRS = ("/opt/lu-node/{major}/bin",)
_SHIM_TAILS = frozenset({("agent_runtime", "shims"), ("dispatcher", "npm_shims")})
_NVMRC_MAX_BYTES = 1024
_ENGINE_SPEC_MAX_CHARS = 256
_MAX_MAJOR_EXPANSION = 16
_NVMRC_RE = re.compile(r"^v?(0|[1-9]\d*)(?:\.(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?)?$")
_SEMVER_OP_SPACE_RE = re.compile(r"(>=|<=|>|<|=|\^|~)\s+")
_SEMVER_TOKEN_RE = re.compile(
    r"^(?P<op>>=|<=|>|<|=|\^|~)?v?"
    r"(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*|x|X|\*)(?:\.(?P<patch>0|[1-9]\d*|x|X|\*))?)?$"
)
_VERSION_LINE_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


class NotCacheable(Exception):
    """The installed tree cannot be shared without breaking worktree isolation."""


class NodeToolchainError(ValueError):
    """A worktree's pinned Node/npm toolchain is invalid, conflicting, or unavailable."""

    def __init__(self, message: str, *, code: str = "node_toolchain_unavailable") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class _SemverInterval:
    """Half-open version interval ``[low, high)`` over ``(major, minor, patch)`` triples."""

    low: tuple[int, int, int]
    high: tuple[int, int, int] | None

    def matches(self, version: tuple[int, int, int]) -> bool:
        return version >= self.low and (self.high is None or version < self.high)

    def intersect(self, other: _SemverInterval) -> _SemverInterval | None:
        low = max(self.low, other.low)
        if self.high is None:
            high = other.high
        elif other.high is None:
            high = self.high
        else:
            high = min(self.high, other.high)
        if high is not None and low >= high:
            return None
        return _SemverInterval(low=low, high=high)


@dataclass(frozen=True)
class _SemverRange:
    """Union of half-open semver intervals parsed from an engine spec or ``.nvmrc``."""

    raw: str
    intervals: tuple[_SemverInterval, ...]

    def matches(self, version: tuple[int, int, int]) -> bool:
        return any(interval.matches(version) for interval in self.intervals)

    def intersect(self, other: _SemverRange) -> tuple[_SemverInterval, ...]:
        combined: list[_SemverInterval] = []
        for left in self.intervals:
            for right in other.intervals:
                overlap = left.intersect(right)
                if overlap is not None:
                    combined.append(overlap)
        return tuple(combined)


@dataclass(frozen=True)
class _WorktreeNodePin:
    """Effective Node and npm version constraints read from a worktree."""

    node_intervals: tuple[_SemverInterval, ...] | None
    npm_range: _SemverRange | None
    majors: tuple[int, ...] | None
    description: str

    def matches_node(self, version: tuple[int, int, int]) -> bool:
        if self.node_intervals is None:
            return True
        return any(interval.matches(version) for interval in self.node_intervals)

    def matches_npm(self, version: tuple[int, int, int]) -> bool:
        if self.npm_range is None:
            return True
        return self.npm_range.matches(version)


def _has_unsafe_chars(text: str) -> bool:
    return any(unicodedata.category(ch)[0] == "C" or ch in "\u2028\u2029" for ch in text)


def _parse_comparator(token: str, *, field: str) -> _SemverInterval:
    if token in {"*", "x", "X"}:
        return _SemverInterval((0, 0, 0), None)
    match = _SEMVER_TOKEN_RE.fullmatch(token)
    if match is None:
        raise NodeToolchainError(f"{field} has unsupported version specifier {token!r}", code="node_pin_invalid")
    op = match.group("op") or "="
    major = int(match.group("major"))
    raw_minor = match.group("minor")
    raw_patch = match.group("patch")
    minor_wild = raw_minor in {"x", "X", "*"}
    patch_wild = raw_patch in {"x", "X", "*"}
    if minor_wild and raw_patch is not None and not patch_wild:
        raise NodeToolchainError(f"{field} has invalid wildcard specifier {token!r}", code="node_pin_invalid")
    minor = None if raw_minor is None or minor_wild else int(raw_minor)
    patch = None if raw_patch is None or patch_wild else int(raw_patch)

    if op == "=":
        if minor is None:
            return _SemverInterval((major, 0, 0), (major + 1, 0, 0))
        if patch is None:
            return _SemverInterval((major, minor, 0), (major, minor + 1, 0))
        return _SemverInterval((major, minor, patch), (major, minor, patch + 1))
    if op == "^":
        if minor is None:
            return _SemverInterval((major, 0, 0), (major + 1, 0, 0))
        if patch is None:
            high = (major + 1, 0, 0) if major > 0 else (0, minor + 1, 0)
            return _SemverInterval((major, minor, 0), high)
        if major > 0:
            high = (major + 1, 0, 0)
        elif minor > 0:
            high = (0, minor + 1, 0)
        else:
            high = (0, 0, patch + 1)
        return _SemverInterval((major, minor, patch), high)
    if op == "~":
        if minor is None:
            return _SemverInterval((major, 0, 0), (major + 1, 0, 0))
        return _SemverInterval((major, minor, patch or 0), (major, minor + 1, 0))
    if op == ">=":
        return _SemverInterval((major, minor or 0, patch or 0), None)
    if op == ">":
        if minor is None:
            return _SemverInterval((major + 1, 0, 0), None)
        if patch is None:
            return _SemverInterval((major, minor + 1, 0), None)
        return _SemverInterval((major, minor, patch + 1), None)
    if op == "<":
        return _SemverInterval((0, 0, 0), (major, minor or 0, patch or 0))
    # op == "<="
    if minor is None:
        return _SemverInterval((0, 0, 0), (major + 1, 0, 0))
    if patch is None:
        return _SemverInterval((0, 0, 0), (major, minor + 1, 0))
    return _SemverInterval((0, 0, 0), (major, minor, patch + 1))


def _parse_semver_range(raw: object, *, field: str) -> _SemverRange:
    if not isinstance(raw, str):
        raise NodeToolchainError(f"{field} must be a non-empty string", code="node_pin_invalid")
    if len(raw) > _ENGINE_SPEC_MAX_CHARS or _has_unsafe_chars(raw):
        raise NodeToolchainError(f"{field} contains invalid or oversized characters", code="node_pin_invalid")
    cleaned = raw.strip()
    if not cleaned:
        raise NodeToolchainError(f"{field} must not be empty", code="node_pin_invalid")
    intervals: list[_SemverInterval] = []
    for raw_clause in cleaned.split("||"):
        clause = _SEMVER_OP_SPACE_RE.sub(r"\1", raw_clause.strip())
        if not clause:
            raise NodeToolchainError(f"{field} has an empty version clause in {raw!r}", code="node_pin_invalid")
        current: _SemverInterval | None = _SemverInterval((0, 0, 0), None)
        for token in clause.split():
            assert current is not None
            current = current.intersect(_parse_comparator(token, field=field))
            if current is None:
                break
        if current is not None:
            intervals.append(current)
    if not intervals:
        raise NodeToolchainError(f"{field} ({cleaned!r}) matches no version", code="node_pin_conflict")
    return _SemverRange(raw=cleaned, intervals=tuple(intervals))


def _read_nvmrc(path: Path, *, label: str) -> _SemverRange | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NodeToolchainError(f"cannot inspect {label} ({path}): {exc}", code="node_pin_invalid") from exc
    if not stat.S_ISREG(info.st_mode):
        raise NodeToolchainError(f"{label} ({path}) must be a regular file, not a symlink or directory",
                                 code="node_pin_invalid")
    if info.st_size > _NVMRC_MAX_BYTES:
        raise NodeToolchainError(f"{label} ({path}) exceeds {_NVMRC_MAX_BYTES} bytes", code="node_pin_invalid")
    try:
        data = path.read_bytes()
        text = data.decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise NodeToolchainError(f"{label} ({path}) is not readable ASCII: {exc}", code="node_pin_invalid") from exc
    cleaned = text.strip(" \t\r\n")
    if not cleaned or "\n" in cleaned or "\r" in cleaned or _has_unsafe_chars(cleaned):
        raise NodeToolchainError(f"{label} ({path}) must contain a single Node version line", code="node_pin_invalid")
    match = _NVMRC_RE.fullmatch(cleaned)
    if match is None:
        raise NodeToolchainError(f"{label} ({path}) has unsupported Node pin {cleaned!r}", code="node_pin_invalid")
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else None
    patch = int(match.group(3)) if match.group(3) is not None else None
    if minor is None:
        interval = _SemverInterval((major, 0, 0), (major + 1, 0, 0))
    elif patch is None:
        interval = _SemverInterval((major, minor, 0), (major, minor + 1, 0))
    else:
        interval = _SemverInterval((major, minor, patch), (major, minor, patch + 1))
    return _SemverRange(raw=cleaned, intervals=(interval,))


def _bounded_majors(intervals: tuple[_SemverInterval, ...]) -> tuple[int, ...] | None:
    majors: set[int] = set()
    for interval in intervals:
        if interval.high is None:
            return None
        max_major = interval.high[0] if (interval.high[1] > 0 or interval.high[2] > 0) else interval.high[0] - 1
        if max_major - interval.low[0] + 1 > _MAX_MAJOR_EXPANSION:
            return None
        majors.update(range(interval.low[0], max_major + 1))
    return tuple(sorted(majors, reverse=True)) if majors else None


def _read_worktree_pins(worktree: Path, site: Path) -> _WorktreeNodePin | None:
    root_nvmrc = _read_nvmrc(worktree / ".nvmrc", label=".nvmrc")
    site_nvmrc = _read_nvmrc(site / ".nvmrc", label=f"{SITE}/.nvmrc")
    nvmrc: _SemverRange | None = root_nvmrc
    if root_nvmrc is not None and site_nvmrc is not None:
        overlap = root_nvmrc.intersect(site_nvmrc)
        if not overlap:
            raise NodeToolchainError(
                f".nvmrc ({root_nvmrc.raw!r}) conflicts with {SITE}/.nvmrc ({site_nvmrc.raw!r})",
                code="node_pin_conflict",
            )
        nvmrc = _SemverRange(raw=f"{root_nvmrc.raw} & {site_nvmrc.raw}", intervals=overlap)
    elif site_nvmrc is not None:
        nvmrc = site_nvmrc

    package_path = site / "package.json"
    try:
        package = _json_loads(package_path.read_text(encoding="utf-8"), max_depth=512)
    except (OSError, ValueError, RecursionError) as exc:
        raise NodeToolchainError(f"cannot read {SITE}/package.json: {exc}", code="node_pin_invalid") from exc
    if not isinstance(package, dict):
        raise NodeToolchainError(f"{SITE}/package.json must be a JSON object", code="node_pin_invalid")
    engines = package.get("engines")
    node_engine: _SemverRange | None = None
    npm_engine: _SemverRange | None = None
    if engines is not None:
        if not isinstance(engines, dict):
            raise NodeToolchainError(f"{SITE}/package.json engines must be a JSON object", code="node_pin_invalid")
        if "node" in engines:
            node_engine = _parse_semver_range(engines["node"], field=f"{SITE}/package.json engines.node")
        if "npm" in engines:
            npm_engine = _parse_semver_range(engines["npm"], field=f"{SITE}/package.json engines.npm")

    node_intervals: tuple[_SemverInterval, ...] | None = None
    if nvmrc is not None and node_engine is not None:
        overlap = nvmrc.intersect(node_engine)
        if not overlap:
            raise NodeToolchainError(
                f"worktree .nvmrc ({nvmrc.raw!r}) conflicts with {SITE}/package.json engines.node ({node_engine.raw!r})",
                code="node_pin_conflict",
            )
        node_intervals = overlap
    elif nvmrc is not None:
        node_intervals = nvmrc.intervals
    elif node_engine is not None:
        node_intervals = node_engine.intervals

    if node_intervals is None and npm_engine is None:
        return None

    parts: list[str] = []
    if nvmrc is not None:
        parts.append(f".nvmrc={nvmrc.raw!r}")
    if node_engine is not None:
        parts.append(f"{SITE}/package.json engines.node={node_engine.raw!r}")
    if npm_engine is not None:
        parts.append(f"{SITE}/package.json engines.npm={npm_engine.raw!r}")
    majors = _bounded_majors(node_intervals) if node_intervals is not None else None
    return _WorktreeNodePin(
        node_intervals=node_intervals,
        npm_range=npm_engine,
        majors=majors,
        description=", ".join(parts),
    )


def _is_shim_dir(entry: str) -> bool:
    path = Path(entry)
    if path.parts[-2:] in _SHIM_TAILS or path.parts[-3:] in SHIM_DIRS:
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True
    return resolved.parts[-2:] in _SHIM_TAILS or resolved.parts[-3:] in SHIM_DIRS


def _configured_toolchain_templates(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if env is None else env
    if NODE_TOOLCHAIN_DIRS_ENV not in source:
        return DEFAULT_NODE_TOOLCHAIN_DIRS
    raw = source[NODE_TOOLCHAIN_DIRS_ENV]
    if not raw.strip():
        raise NodeToolchainError(
            f"{NODE_TOOLCHAIN_DIRS_ENV} must not be empty when set",
            code="node_toolchain_config_invalid",
        )
    templates: list[str] = []
    for entry in raw.split(os.pathsep):
        if not entry or _has_unsafe_chars(entry):
            raise NodeToolchainError(
                f"{NODE_TOOLCHAIN_DIRS_ENV} contains an empty or unsafe path entry {entry!r}",
                code="node_toolchain_config_invalid",
            )
        substituted = entry.replace("{major}", "0")
        if "{" in substituted or "}" in substituted:
            raise NodeToolchainError(
                f"{NODE_TOOLCHAIN_DIRS_ENV} entry {entry!r} may only use the '{{major}}' placeholder",
                code="node_toolchain_config_invalid",
            )
        probe_path = Path(substituted)
        if not probe_path.is_absolute() or ".." in probe_path.parts:
            raise NodeToolchainError(
                f"{NODE_TOOLCHAIN_DIRS_ENV} entry {entry!r} must be an absolute path without '..'",
                code="node_toolchain_config_invalid",
            )
        templates.append(entry)
    return tuple(templates)


def _non_shim_path_dirs(env: Mapping[str, str] | None = None) -> list[Path]:
    source = os.environ if env is None else env
    search = source.get("AGENT_ORIGINAL_PATH") or source.get("PATH", os.defpath)
    dirs: list[Path] = []
    for entry in search.split(os.pathsep):
        if not entry or not Path(entry).is_absolute() or _is_shim_dir(entry):
            continue
        dirs.append(Path(entry))
    return dirs


def _companion_npm(candidate_dir: Path, resolved_node: Path) -> tuple[Path, Path] | None:
    """Locate an executable ``npm`` paired with ``resolved_node``, preferring the Node install's bin dir."""
    for bin_dir in (resolved_node.parent, candidate_dir):
        node_path = bin_dir / "node"
        npm_path = bin_dir / "npm"
        try:
            if node_path.resolve(strict=True) != resolved_node:
                continue
            resolved_npm = npm_path.resolve(strict=True)
            resolved_dir = bin_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved_npm.is_file() and os.access(npm_path, os.X_OK):
            return resolved_dir, npm_path
    return None


def _probe_version(binary: Path, *, env: Mapping[str, str]) -> tuple[int, int, int] | None:
    try:
        proc = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    observed = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not observed:
        return None
    match = _VERSION_LINE_RE.fullmatch(observed.splitlines()[0].strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _probe_npm_semver(npm_bin: Path, bin_dir: Path) -> tuple[int, int, int] | None:
    probe_env = {
        "PATH": os.pathsep.join(dict.fromkeys([str(bin_dir), *SYSTEM_PATH])),
        "LANG": "C.UTF-8",
    }
    return _probe_version(npm_bin, env=probe_env)


def _resolve_node_binary(
    *,
    search_dirs: Sequence[Path],
    matches_version: Callable[[tuple[int, int, int]], bool],
    probe_env: Mapping[str, str],
    candidate_filter: Callable[[Path, Path, tuple[int, int, int]], bool],
) -> tuple[Path, Path, tuple[int, int, int]] | None:
    """Select the highest matching-semver ``node`` across ``search_dirs`` (stdlib-only for file-path callers)."""
    seen: set[tuple[str, str]] = set()
    best: tuple[Path, Path, tuple[int, int, int]] | None = None
    for directory in search_dirs:
        candidate = directory / "node"
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            # Python 3.12 raises RuntimeError on symlink loops; 3.13+ uses OSError(ELOOP).
            continue
        key = (str(resolved), str(directory))
        if key in seen:
            continue
        seen.add(key)
        if not (resolved.is_file() and os.access(resolved, os.X_OK)):
            continue
        version = _probe_version(resolved, env=probe_env)
        if version is None or not matches_version(version):
            continue
        if not candidate_filter(candidate, resolved, version):
            continue
        if best is None or version > best[2]:
            best = (candidate, resolved, version)
    return best


def resolve_toolchain_dir(
    worktree: Path,
    site: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve the pinned Node/npm bin directory for ``worktree``, or ``None`` when unpinned."""
    pin = _read_worktree_pins(worktree, site)
    if pin is None:
        return None
    templates = _configured_toolchain_templates(env)
    search_dirs: list[Path] = []
    seen_dirs: set[str] = set()
    skipped_templates: list[str] = []
    for template in templates:
        if "{major}" in template:
            if pin.majors is None:
                skipped_templates.append(template)
                continue
            for major in pin.majors:
                expanded = template.replace("{major}", str(major))
                if expanded not in seen_dirs:
                    seen_dirs.add(expanded)
                    search_dirs.append(Path(expanded))
        elif template not in seen_dirs:
            seen_dirs.add(template)
            search_dirs.append(Path(template))
    for path_dir in _non_shim_path_dirs(env):
        text = str(path_dir)
        if text not in seen_dirs:
            seen_dirs.add(text)
            search_dirs.append(path_dir)

    probe_env = {"PATH": os.pathsep.join(SYSTEM_PATH), "LANG": "C.UTF-8"}
    matched_dirs: dict[tuple[Path, Path], Path] = {}
    npm_rejections: list[str] = []

    def _check_npm(candidate: Path, resolved_node: Path, node_version: tuple[int, int, int]) -> bool:
        pair = _companion_npm(candidate.parent, resolved_node)
        if pair is None:
            npm_rejections.append(f"{candidate.parent} (node v{'.'.join(map(str, node_version))}, npm missing)")
            return False
        bin_dir, npm_bin = pair
        npm_version = _probe_npm_semver(npm_bin, bin_dir)
        if npm_version is None:
            npm_rejections.append(f"{bin_dir} (node v{'.'.join(map(str, node_version))}, npm --version unreadable)")
            return False
        if not pin.matches_npm(npm_version):
            npm_rejections.append(
                f"{bin_dir} (node v{'.'.join(map(str, node_version))}, npm {'.'.join(map(str, npm_version))})"
            )
            return False
        matched_dirs[(candidate, resolved_node)] = bin_dir
        return True

    found = _resolve_node_binary(
        search_dirs=search_dirs,
        matches_version=pin.matches_node,
        probe_env=probe_env,
        candidate_filter=_check_npm,
    )
    if found is None:
        searched = ", ".join(str(directory) for directory in search_dirs) or "(none)"
        skipped = (
            f"; skipped templated dirs (pin does not bound Node majors): {', '.join(skipped_templates)}"
            if skipped_templates
            else ""
        )
        extra = f"; rejected matching-node candidates: {'; '.join(npm_rejections)}" if npm_rejections else ""
        raise NodeToolchainError(
            f"no matching Node/npm toolchain for pin [{pin.description}]; "
            f"searched directories: {searched}{skipped}{extra}; "
            f"install the pinned toolchain or set {NODE_TOOLCHAIN_DIRS_ENV}",
            code="node_toolchain_unavailable",
        )
    return matched_dirs[(found[0], found[1])]


def store_root(repo: Path) -> Path:
    """The store lives beside the worktrees' repository so that hard links stay on one filesystem."""
    configured = os.environ.get(STORE_ENV)
    return Path(configured) if configured else Path(repo) / ".git" / "dispatch-dep-store" / f"v{LAYOUT}"


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        digest.update(f"{path.relative_to(root).as_posix()}\0".encode())
        digest.update((os.readlink(path) if path.is_symlink() else _sha256(path)).encode())
    return digest.hexdigest()


def _local_inputs(site: Path) -> dict[str, str]:
    """Digests of every non-registry input the install reads: file:/link: targets and patches."""
    specs: set[str] = set()
    package = _json_loads((site / "package.json").read_text(encoding="utf-8"), max_depth=512)
    for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        specs.update(v for v in package.get(field, {}).values() if v.startswith(("file:", "link:")))
    lock = _json_loads((site / "package-lock.json").read_text(encoding="utf-8"), max_depth=512)
    for entry in lock.get("packages", {}).values():
        if entry.get("link") and entry.get("resolved"):
            specs.add(f"link:{entry['resolved']}")
        elif str(entry.get("resolved", "")).startswith("file:"):
            specs.add(entry["resolved"])
    inputs = {}
    candidates = {spec: (site / spec.split(":", 1)[1]) for spec in specs}
    candidates["patches/"] = site / "patches"
    for name, path in sorted(candidates.items()):
        resolved = path.resolve()
        if not resolved.is_relative_to(site.resolve()) or not resolved.exists():
            if name != "patches/":
                raise NotCacheable(f"local install input {name} is missing or outside the site directory")
            continue
        inputs[name] = _tree_digest(resolved) if resolved.is_dir() else _sha256(resolved)
    return inputs


def _tool_dirs() -> list[str]:
    """Directories of the real node and npm, never a dispatch shim directory."""
    search = os.environ.get("AGENT_ORIGINAL_PATH") or os.environ.get("PATH", os.defpath)
    entries = [e for e in search.split(os.pathsep) if e and not _is_shim_dir(e)]
    found = (shutil.which(tool, path=os.pathsep.join(entries)) for tool in ("node", "npm"))
    return [str(Path(binary).parent) for binary in found if binary]


def _install_env(toolchain_dir: Path | None = None) -> dict[str, str]:
    """Build the allowlisted environment before assigning a scratch home."""
    tool_dirs = [str(toolchain_dir)] if toolchain_dir is not None else _tool_dirs()
    env = {"PATH": os.pathsep.join(dict.fromkeys([*tool_dirs, *SYSTEM_PATH])), "LANG": "C.UTF-8"}
    env.update({name: os.environ[name] for name in sorted(os.environ)
                if name in PASSED_ENV or (
                    name.lower().startswith(NPM_CONFIG_PREFIX) and name.lower() not in NPM_HOME_SETTINGS)})
    return env


def _toolchain(env: dict[str, str]) -> dict:
    tools = {}
    for tool in BUILD_TOOLS:
        binary = shutil.which(tool, path=env["PATH"])
        try:
            banner = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10,
                                    env=env).stdout.splitlines()[:1] if binary else []
        except (OSError, subprocess.SubprocessError):
            banner = []
        tools[tool] = [str(Path(binary).resolve()), *banner] if binary else None
    return tools


def _run(argv, cwd, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60),
                          env=kwargs.pop("env", None) or _install_env(), **kwargs)


def _runtime(site: Path, env: dict[str, str]) -> dict:
    probe = ("const r=process.report.getReport().header;console.log(JSON.stringify({node:process.version,"
             "abi:process.versions.modules,platform:process.platform,arch:process.arch,"
             "libc:r.glibcVersionRuntime?'glibc-'+r.glibcVersionRuntime:'other'}))")
    runtime = _json_loads(_run(["node", "-e", probe], site, env=env).stdout, max_depth=512)
    runtime["npm"] = _run(["npm", "--version"], site, env=env).stdout.strip()
    return runtime


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def install_key(site: Path, env: dict[str, str]) -> tuple[str, dict]:
    """The key and the human-readable facts it covers; any change to them is a different install."""
    # Hash effective config without making each random scratch directory a different cache key.
    config = _json_loads(_run(["npm", "config", "ls", "-l", "--json"], site, env=env).stdout, max_depth=512)
    normalized_config = json.dumps(config, sort_keys=True).replace(json.dumps(env["HOME"])[1:-1], "$HOME")
    facts = {
        "layout": LAYOUT,
        "runtime": _runtime(site, env),
        # Digests only: npm configuration can hold registry credentials, and facts are stored.
        "npm_config": hashlib.sha256(normalized_config.encode()).hexdigest(),
        "env": {name: _digest(value) for name, value in env.items()
                if name not in {"HOME", "npm_config_globalconfig", "npm_config_logs_dir"}},
        "toolchain": _toolchain(env),
        "inputs": {name: _sha256(site / name) for name in INSTALL_INPUTS if (site / name).is_file()},
        "local": _local_inputs(site),
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest(), facts


@contextmanager
def _locked(path: Path, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle, mode)
        yield


def _blob(store: Path, name: str) -> Path:
    return store / "blobs" / name[:2] / name


def _intact(blob: Path, name: str) -> bool:
    """True when the store file has the content and the read-only mode its name promises."""
    try:
        mode = stat.S_IMODE(blob.stat().st_mode)
        return mode == (0o555 if name.endswith("x") else 0o444) and _sha256(blob) == name[:64]
    except OSError:
        return False


def _ingest(store: Path, modules: Path) -> dict:
    """Move the files of a freshly installed tree into the store and describe the tree."""
    root = modules.resolve()
    entries: list[list] = []

    def link(here: Path, rel: str) -> None:
        target = os.readlink(here)
        if os.path.isabs(target) or not (here.parent / target).resolve().is_relative_to(root):
            raise NotCacheable(f"{rel} links outside the installed tree")
        entries.append(["l", rel, target])

    for directory, subdirs, files in os.walk(modules):
        here = Path(directory)
        relative = here.relative_to(modules)
        top = not relative.parts
        if top:
            subdirs[:] = [d for d in subdirs if d not in RUNTIME_CACHES]
        for name in sorted(subdirs):
            rel = (relative / name).as_posix()
            link(here / name, rel) if (here / name).is_symlink() else entries.append(["d", rel])
        for name in sorted(files):
            path, rel = here / name, (relative / name).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                link(path, rel)
            elif stat.S_ISREG(info.st_mode):
                executable = bool(info.st_mode & stat.S_IXUSR)
                digest = _sha256(path) + ("x" if executable else "")
                entries.append(["c" if top and name in COPIED else "f", rel, digest])
                _keep(store, path, digest, executable)
            else:
                raise NotCacheable(f"{rel} is neither a file, directory nor link")
    return {"entries": entries}


def _keep(store: Path, path: Path, digest: str, executable: bool) -> None:
    blob = _blob(store, digest)
    blob.parent.mkdir(parents=True, exist_ok=True)
    path.chmod(0o555 if executable else 0o444)
    try:
        os.link(path, blob)
    except FileExistsError:
        if not _intact(blob, digest):
            # Altered since it was stored: other worktrees keep the old inode, this tree gets a fresh one.
            blob.unlink()
            os.link(path, blob)


def _write_manifest(store: Path, key: str, facts: dict, tree: dict) -> None:
    target = store / "trees" / f"{key}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # A sibling file renamed into place: a reader never sees a half-written manifest.
    temporary = target.with_name(f".{key}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump({"facts": facts, **tree}, handle)
    os.replace(temporary, target)


def populate(repo: Path, site: Path, key: str, facts: dict, env: dict[str, str]) -> None:
    """Install once in a staging directory (never in a worktree) and move the result into the store."""
    store = store_root(repo)
    staging = store / "staging" / f"{key[:12]}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for name in (*INSTALL_INPUTS, "patches"):
            source = site / name
            if source.is_dir():
                shutil.copytree(source, staging / name)
            elif source.is_file():
                shutil.copy2(source, staging / name)
        for name in facts["local"]:
            if name != "patches/":
                target = name.split(":", 1)[1]
                (staging / target).parent.mkdir(parents=True, exist_ok=True)
                source = site / target
                shutil.copytree(source, staging / target) if source.is_dir() else shutil.copy2(source, staging / target)
        _run(["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund", f"--cache={env['npm_config_cache']}"],
             staging, env=env, timeout=INSTALL_TIMEOUT)
        tree = _ingest(store, staging / "node_modules")
        _write_manifest(store, key, facts, tree)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _altered(store: Path, manifest: Path) -> list[Path] | None:
    """None when the tree is stored and every store file still has the content its name says.

    Otherwise the store files that do not (an empty list when the tree was never stored).

    Hard links share one inode, so a tool that edits a package file in place changes it for every worktree
    and for the store. The check runs before each reuse, so such a file never reaches a new worktree.
    """
    if not manifest.is_file():
        return []
    altered = []
    for name in sorted({e[2] for e in _json_loads(manifest.read_text(encoding="utf-8"), max_depth=512)["entries"]
                        if e[0] in ("f", "c")}):
        if not _intact(_blob(store, name), name):
            altered.append(_blob(store, name))
    return altered or None


def materialize(repo: Path, site: Path, key: str) -> dict:
    """Give the worktree its own real node_modules of hard links; never replaces an existing install."""
    store = store_root(repo)
    destination = site / "node_modules"
    manifest = _json_loads((store / "trees" / f"{key}.json").read_text(encoding="utf-8"), max_depth=512)
    if destination.exists() or destination.is_symlink():
        return {"state": "present"}
    # A killed dispatcher must not leave unknown output inside its worktree.
    # Keep staging in the repository's local store even when blobs are configured on another device.
    # Cross-device blobs use the existing copy fallback; publication remains one local rename.
    staging = repo / ".git" / "dispatch-dep-store" / f"v{LAYOUT}" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    building = Path(tempfile.mkdtemp(prefix="materialize-", dir=staging))
    stats = {"state": "installed", "linked": 0, "copied": 0}
    try:
        for kind, rel, *rest in manifest["entries"]:
            path = building / rel
            if kind == "d":
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "l":
                os.symlink(rest[0], path)
                continue
            blob = _blob(store, rest[0])
            if kind == "f":
                try:
                    os.link(blob, path)
                    stats["linked"] += 1
                    continue
                except OSError:
                    LOGGER.warning("Cannot hard-link %s into the worktree; copying it", rel)
            shutil.copy2(blob, path)
            path.chmod(0o755 if rest[0].endswith("x") else 0o644)
            stats["copied"] += 1
        os.rename(building, destination)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return stats


def ensure(repo: Path, worktree: Path) -> dict:
    """Install the worktree's site dependencies from the store, filling the store first on a miss."""
    repo, worktree = Path(repo), Path(worktree)
    site = worktree / SITE
    if os.environ.get(DISABLE_ENV) == "off" or not all((site / name).is_file() for name in INSTALL_INPUTS[:2]):
        return {"state": "skipped"}
    if (site / "node_modules").exists() or (site / "node_modules").is_symlink():
        return {"state": "present"}
    toolchain_dir = resolve_toolchain_dir(worktree, site)
    store = store_root(repo)
    cache = Path(os.environ.get("NPM_CACHE", store.absolute() / "npm-cache"))
    if not cache.is_absolute():
        raise NotCacheable("NPM_CACHE must be an absolute release cache path")
    with tempfile.TemporaryDirectory(prefix="site-deps-home-") as home, _locked(store / "prune.lock", fcntl.LOCK_SH):
        # Keep npm's diagnostic logs in the scratch home even when sharing cached tarballs.
        env = {
            **_install_env(toolchain_dir=toolchain_dir),
            "HOME": home,
            "npm_config_globalconfig": str(Path(home) / "etc/npmrc"),
            "npm_config_cache": str(cache),
            "npm_config_logs_dir": str(Path(home) / ".npm/_logs"),
        }
        key, facts = install_key(site, env)
        manifest = store / "trees" / f"{key}.json"
        if _altered(store, manifest) is not None:
            with _locked(store / "locks" / f"{key}.lock", fcntl.LOCK_EX):
                altered = _altered(store, manifest)
                if altered is not None:
                    # Unlinking leaves worktrees that still hold the old inode as they are; only new ones differ.
                    for blob in altered:
                        blob.unlink(missing_ok=True)
                    manifest.unlink(missing_ok=True)
                    populate(repo, site, key, facts, env)
        result = materialize(repo, site, key)
    prune(repo, keep=KEEP_TREES, blocking=False)
    return {**result, "key": key}


def prune(repo: Path, keep: int = KEEP_TREES, *, blocking: bool = True) -> dict:
    """Drop all but the newest trees, then the store files no remaining tree and no worktree uses."""
    store = store_root(repo)
    if not store.is_dir():
        return {"trees": 0, "blobs": 0}
    path = store / "prune.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            return {"trees": 0, "blobs": 0}
        trees = sorted((store / "trees").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in trees[keep:]:
            stale.unlink()
        wanted = set()
        for tree in trees[:keep]:
            wanted.update(entry[2] for entry in _json_loads(tree.read_text(encoding="utf-8"), max_depth=512)["entries"]
                          if entry[0] in ("f", "c"))
        removed = 0
        for blob in (store / "blobs").glob("*/*"):
            if blob.name not in wanted and blob.stat().st_nlink == 1:
                blob.unlink()
                removed += 1
    return {"trees": len(trees[keep:]), "blobs": removed}


def detach(worktree: Path, paths=()) -> dict:
    """Replace hard-linked files of the worktree's install with private writable copies.

    Run this before any workflow that edits, patches or changes the permissions of package files in place:
    afterwards those files no longer share an inode with the store or with other worktrees. With no paths
    the whole install is detached; a path is relative to site/node_modules and may name a file or directory.
    """
    modules = (Path(worktree) / SITE / "node_modules").resolve()
    roots = [(modules / path).resolve() for path in paths] or [modules]
    detached = 0
    for root in roots:
        if not root.is_relative_to(modules) or not root.exists():
            raise ValueError(f"{root} is not inside {modules}")
        for directory, _subdirs, files in os.walk(root) if root.is_dir() else [(root.parent, [], [root.name])]:
            for name in files:
                path = Path(directory) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink == 1:
                    continue
                private = path.with_name(f".{name}.detach-{os.getpid()}")
                shutil.copy2(path, private)
                private.chmod(0o755 if info.st_mode & stat.S_IXUSR else 0o644)
                os.replace(private, path)
                detached += 1
    return {"detached": detached}


def status(repo: Path) -> dict:
    store = store_root(repo)
    blobs = [p for p in (store / "blobs").glob("*/*")] if store.is_dir() else []
    return {"store": str(store), "trees": len(list((store / "trees").glob("*.json"))) if store.is_dir() else 0,
            "blobs": len(blobs), "bytes": sum(p.stat().st_size for p in blobs)}


def provision(repo: Path, worktree: Path) -> None:
    """Best-effort hook for worktree creation: a task never fails because dependencies could not be shared."""
    try:
        result = ensure(repo, worktree)
    except (NotCacheable, OSError, ValueError, subprocess.SubprocessError) as exc:
        LOGGER.warning("Site dependencies were not provisioned for %s: %s", worktree, exc)
        return
    LOGGER.info("Site dependencies for %s: %s", worktree, result)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("ensure", "status", "prune", "detach"))
    parser.add_argument("--path", action="append", default=[], help="detach only this path under site/node_modules")
    parser.add_argument("--worktree", type=Path, default=Path.cwd(), help="the dispatch worktree (default: here)")
    args = parser.parse_args(argv)
    common = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], args.worktree).stdout.strip()
    repo = Path(common).parent
    if args.action == "ensure":
        result = ensure(repo, args.worktree)
    elif args.action == "detach":
        result = detach(args.worktree, args.path)
    else:
        result = status(repo) if args.action == "status" else prune(repo)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
