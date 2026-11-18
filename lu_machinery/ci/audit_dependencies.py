"""Audit Python and Node.js dependencies for security vulnerabilities.

Enforces blocking gates on Critical and High vulnerabilities while allowing
audited suppressions via .pip-audit-ignore.yaml.

Reference issue: learn-ukrainian-infra-private#703
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import product_root


def load_pip_audit_ignores(ignore_file: Path) -> list[str]:
    """Load list of suppressed vulnerability IDs from YAML config."""
    if not ignore_file.exists():
        return []
    with open(ignore_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    vulns = data.get("vulnerabilities", [])
    ignore_ids: list[str] = []
    for item in vulns:
        if isinstance(item, dict) and "id" in item:
            ignore_ids.append(str(item["id"]).strip())
        elif isinstance(item, str):
            ignore_ids.append(item.strip())
    return [i for i in ignore_ids if i]


# PEP 503: lowercase, and collapse runs of "-", "_" and "." to a single "-".
_LU_MACHINERY_NAME = "lu-machinery"
_WHEELHOUSE_ENV = "LU_MACHINERY_WHEELHOUSE"
_MACHINERY_LOCK = "requirements-machinery.lock"
# pip's comment rule: "#" at the start of a line or after whitespace.
_COMMENT = re.compile(r"(^|\s+)#.*$")
_LEADING_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_EXACT_PIN = re.compile(r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==(?P<version>\S+)(?P<rest>.*)$")
_HASH_TOKEN = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})\Z")
_SAFE_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
# The one accepted local path form: ``./dir`` or ``../dir``, optionally with extras.
_LOCAL_PATH = re.compile(r"(?P<path>\.\.?/[A-Za-z0-9._/-]+)(?:\[[A-Za-z0-9._,-]+\])?\Z")
# The one accepted direct reference: a git URL pinned to a full commit, with no user info.
_PINNED_GIT_URL = re.compile(r"git\+https://[A-Za-z0-9.-]+/[A-Za-z0-9._~/-]+@[0-9a-f]{40}\Z")
# pip's ``strip_extras``: a trailing ``[...]`` is removed before the path check.
_PIP_EXTRAS_SUFFIX = re.compile(r"^(.+)(\[[^\]]+\])$")
# pip installs a requirement ending in an archive suffix as a file (pip's ARCHIVE_EXTENSIONS).
_ARCHIVE_SUFFIXES = (
    ".zip",
    ".whl",
    ".tar.bz2",
    ".tbz",
    ".tar.xz",
    ".txz",
    ".tlz",
    ".tar.lz",
    ".tar.lzma",
    ".tar.gz",
    ".tgz",
    ".tar",
)


class LuMachineryAuditWheelError(RuntimeError):
    """The audit cannot bind ``lu-machinery`` to the checked wheel, so it must not run."""


def _pep503_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first line number, line)`` pairs, joined and stripped the way pip does.

    A trailing backslash continues a line unless the line is a comment; comments
    are removed after joining; blank results are dropped.
    """
    logical: list[tuple[int, str]] = []
    pending: list[str] = []
    first = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not pending:
            first = number
        if line.endswith("\\") and not _COMMENT.match(line):
            pending.append(line.strip("\\"))
            continue
        if _COMMENT.match(line):
            line = " " + line
        pending.append(line)
        joined = _COMMENT.sub("", "".join(pending)).strip()
        pending = []
        if joined:
            logical.append((first, joined))
    if pending:
        joined = _COMMENT.sub("", "".join(pending)).strip()
        if joined:
            logical.append((first, joined))
    return logical


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LuMachineryAuditWheelError(f"cannot read {path.name}: {exc}") from exc


def _split_options(line: str) -> tuple[str, list[str]]:
    """pip's split: the requirement ends at the first space-separated token starting with ``-``."""
    tokens = line.split(" ")
    for index, token in enumerate(tokens):
        if token.startswith("-"):
            return " ".join(tokens[:index]).strip(), " ".join(tokens[index:]).split()
    return line.strip(), []


def _dependency_names(source: Path, specifiers: object, field: str) -> set[str]:
    if not isinstance(specifiers, list) or not all(isinstance(item, str) for item in specifiers):
        raise LuMachineryAuditWheelError(f"{source} {field} is not a list of strings")
    names: set[str] = set()
    for specifier in specifiers:
        try:
            names.add(_pep503_name(Requirement(specifier).name))
        except InvalidRequirement as exc:
            raise LuMachineryAuditWheelError(f"{source} {field} has an unreadable entry {specifier!r}") from exc
    return names


def _path_project_names(package_dir: Path) -> set[str]:
    """PEP 503 names a local project can install: runtime, optional and build requirements.

    Optional groups count whether or not the line asks for them; a project whose
    dependencies are dynamic or unreadable is refused, because they cannot be checked.
    """
    pyproject = package_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise LuMachineryAuditWheelError(f"{package_dir} has no pyproject.toml to read")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LuMachineryAuditWheelError(f"cannot read {pyproject}: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        raise LuMachineryAuditWheelError(f"{pyproject} has no [project] table")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or {"dependencies", "optional-dependencies"} & set(map(str, dynamic)):
        raise LuMachineryAuditWheelError(f"{pyproject} declares dynamic dependencies")
    names = _dependency_names(pyproject, project.get("dependencies", []), "[project].dependencies")
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise LuMachineryAuditWheelError(f"{pyproject} [project].optional-dependencies is not a table")
    for group, specifiers in optional.items():
        names |= _dependency_names(pyproject, specifiers, f"[project].optional-dependencies.{group}")
    build = data.get("build-system", {})
    if not isinstance(build, dict):
        raise LuMachineryAuditWheelError(f"{pyproject} [build-system] is not a table")
    names |= _dependency_names(pyproject, build.get("requires", []), "[build-system].requires")
    return names


def _line_names(repo_root: Path, line: str) -> set[str]:
    """PEP 503 names one requirements line can install; refuse every unrecognized form.

    Accepted: ``name[extras]==version`` (markers allowed), a
    ``name @ git+https://host/path@<40-hex commit>`` reference to anything but
    ``lu-machinery``, and a ``./dir`` or ``../dir`` project whose pyproject.toml
    is read. The only accepted option is ``--hash=sha256:``. A pin or git
    reference whose text names an existing local path is refused, because pip
    would install that path instead. Everything else pip would also accept
    (editables in any spelling, URLs, includes, constraints, index options,
    unpinned names, ``===``, environment variables) raises.
    """
    if "${" in line:
        raise LuMachineryAuditWheelError("environment variable reference")
    requirement, options = _split_options(line)
    if not requirement:
        raise LuMachineryAuditWheelError(f"unsupported option line {options[0]!r}")
    for option in options:
        if _HASH_TOKEN.fullmatch(option) is None:
            raise LuMachineryAuditWheelError(f"unsupported option {option!r}")
    path = _LOCAL_PATH.fullmatch(requirement)
    if path is not None:
        if options:
            raise LuMachineryAuditWheelError("hash options on a local path")
        return _path_project_names(repo_root / path.group("path"))
    if requirement.startswith((".", "/", "~")):
        raise LuMachineryAuditWheelError("local path outside the ./dir or ../dir form")
    # pip tries the text before ";" as a local path first: an existing directory
    # (extras stripped) or an archive name wins over the requirement it spells.
    pip_name = requirement.split(";", 1)[0].strip()
    if pip_name.lower().endswith(_ARCHIVE_SUFFIXES):
        raise LuMachineryAuditWheelError("pip reads a requirement with an archive suffix as a file")
    pip_path = os.path.normpath(os.path.join(repo_root, pip_name))
    stripped = _PIP_EXTRAS_SUFFIX.match(pip_path)
    if os.path.lexists(pip_path) or (stripped is not None and os.path.lexists(stripped.group(1))):
        raise LuMachineryAuditWheelError("names an existing local path, which pip would install instead")
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement as exc:
        raise LuMachineryAuditWheelError("not a pinned requirement or a ./dir path") from exc
    name = _pep503_name(parsed.name)
    if parsed.url is not None:
        if name == _LU_MACHINERY_NAME:
            raise LuMachineryAuditWheelError("lu-machinery by direct reference")
        if options or parsed.extras or _PINNED_GIT_URL.fullmatch(parsed.url) is None:
            raise LuMachineryAuditWheelError("direct reference that is not a git URL pinned to a full commit")
        return {name}
    specifiers = list(parsed.specifier)
    # Not ``===``: arbitrary equality admits any text, including path separators.
    if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
        raise LuMachineryAuditWheelError("requirement not pinned to one exact == version")
    return {name}


def _requirements_need_lu_machinery(repo_root: Path, requirements_file: Path) -> bool:
    """True when a line, or a local project it installs, names exactly ``lu-machinery``.

    Every line must be a recognized form (:func:`_line_names`); any other line
    raises, naming the file, line number and reason. ``lu-machinery-extras`` is a
    different distribution and does not count.
    """
    need = False
    for number, line in _logical_lines(_read_text(requirements_file)):
        try:
            need |= _LU_MACHINERY_NAME in _line_names(repo_root, line)
        except LuMachineryAuditWheelError as exc:
            raise LuMachineryAuditWheelError(
                f"{requirements_file.name} line {number}: {exc}; refusing to audit {line!r}"
            ) from exc
    return need


def _lock_names_lu_machinery(lock: Path) -> bool:
    """True when any line of the consumer's machinery lock starts with the name ``lu-machinery``."""
    if not lock.is_file():
        return False
    for _, line in _logical_lines(_read_text(lock)):
        match = _LEADING_NAME.match(line)
        if match is not None and _pep503_name(match.group(1)) == _LU_MACHINERY_NAME:
            return True
    return False


class _LuMachineryPin:
    def __init__(self, version: str, hashes: frozenset[str]) -> None:
        self.version = version
        self.hashes = hashes


def _lu_machinery_pin(lock: Path) -> _LuMachineryPin:
    """The single exact ``lu-machinery`` pin and its sha256 hashes."""
    if not lock.is_file():
        raise LuMachineryAuditWheelError(f"{lock.name} does not pin lu-machinery")
    pins: list[_LuMachineryPin] = []
    named = 0
    for _, line in _logical_lines(_read_text(lock)):
        leading = _LEADING_NAME.match(line)
        if leading is None or _pep503_name(leading.group(1)) != _LU_MACHINERY_NAME:
            continue
        named += 1
        match = _EXACT_PIN.match(line)
        if match is None or _pep503_name(match.group("name")) != _LU_MACHINERY_NAME:
            continue
        version = match.group("version")
        if _SAFE_VERSION.fullmatch(version) is None:
            raise LuMachineryAuditWheelError(
                f"{lock.name} pins lu-machinery=={version}, which is not an exact release version"
            )
        hashes: set[str] = set()
        rest = match.group("rest").strip()
        for token in rest.split():
            hashed = _HASH_TOKEN.fullmatch(token)
            if hashed is None:
                raise LuMachineryAuditWheelError(f"{lock.name} lu-machinery pin has an unsupported token {token!r}")
            hashes.add(hashed.group(1).lower())
        if not hashes:
            raise LuMachineryAuditWheelError(f"{lock.name} pins lu-machinery without a sha256 hash")
        pins.append(_LuMachineryPin(version, frozenset(hashes)))
    if len(pins) != 1 or named != 1:
        raise LuMachineryAuditWheelError(
            f"{lock.name} must pin exactly one lu-machinery version; found {named} lu-machinery line(s)"
        )
    return pins[0]


def _hash_checked_wheel(pin: _LuMachineryPin, lock_name: str = _MACHINERY_LOCK) -> Path:
    """Wheel from ``LU_MACHINERY_WHEELHOUSE`` whose sha256 is one of the pin hashes."""
    raw = os.environ.get(_WHEELHOUSE_ENV, "").strip()
    filename = f"lu_machinery-{pin.version}-py3-none-any.whl"
    if not raw:
        raise LuMachineryAuditWheelError("LU_MACHINERY_WHEELHOUSE is unset; refusing to fetch lu-machinery from PyPI")
    wheel = Path(raw) / filename
    if not wheel.is_file():
        raise LuMachineryAuditWheelError(f"LU_MACHINERY_WHEELHOUSE has no {filename}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest not in pin.hashes:
        raise LuMachineryAuditWheelError(f"{filename} sha256 {digest} does not match {lock_name}")
    return wheel


def _pip_audit_process_env(extra: Mapping[str, str]) -> dict[str, str] | None:
    """Process environment for pip-audit, or None when no extra variables apply."""
    if not extra:
        return None
    return {**os.environ, **extra}


@contextmanager
def pip_audit_requirement_env(repo_root: Path, requirements_file: Path) -> Iterator[dict[str, str]]:
    """Environment that lets pip-audit resolve ``lu-machinery`` without PyPI.

    Fails closed first: every line of ``requirements_file`` must be a form
    :func:`_line_names` recognizes, or this raises before pip-audit starts.

    When the consumer's ``requirements-machinery.lock`` names ``lu-machinery``,
    this always yields ``PIP_FIND_LINKS`` (a directory containing only the
    hash-checked wheel) and ``PIP_CONSTRAINT`` (an unhashed ``lu-machinery @
    file:`` URL of that staged wheel), plus the same file as
    ``PIP_BUILD_CONSTRAINT``, because since pip 26.2 isolated builds ignore
    ``PIP_CONSTRAINT``. pip applies a constraint wherever the name appears in
    the resolution, so ``lu-machinery`` required by a pinned package, a git
    reference or a local project, directly, transitively or as a build
    dependency, resolves only from that wheel. When the lock does not name it but a line or
    a local project does, this raises. Otherwise it yields an empty mapping and
    the audit command stays exactly as before.

    A version pin alone does not bind the candidate: pip may download another
    build of the same version from an index. A direct file URL does, and because
    it has no ``--hash`` it does not make pip require hashes on
    ``requirements-lock.txt``. The wheel comes from ``LU_MACHINERY_WHEELHOUSE``;
    this does not download it. A missing pin, a hash mismatch, or a missing wheel
    raises :class:`LuMachineryAuditWheelError` before the caller starts pip-audit.
    """
    lock = repo_root / _MACHINERY_LOCK
    need = _requirements_need_lu_machinery(repo_root, requirements_file)
    if not need and not _lock_names_lu_machinery(lock):
        yield {}
        return
    pin = _lu_machinery_pin(lock)
    wheel = _hash_checked_wheel(pin)
    with tempfile.TemporaryDirectory(prefix="lu-machinery-audit-") as tmp:
        root = Path(tmp)
        links = root / "find-links"
        links.mkdir()
        staged = links / wheel.name
        shutil.copyfile(wheel, staged)
        staged_digest = hashlib.sha256(staged.read_bytes()).hexdigest()
        if staged_digest not in pin.hashes:
            raise LuMachineryAuditWheelError(
                f"staged {staged.name} sha256 {staged_digest} does not match requirements-machinery.lock"
            )
        constraint = root / "lu-machinery-constraint.txt"
        # File URL, not ``==version``: an index can offer another build of the
        # same version. No ``--hash``, so the unhashed consumer lock still resolves.
        constraint.write_text(f"lu-machinery @ {staged.resolve().as_uri()}\n", encoding="utf-8")
        # Since pip 26.2 an isolated build ignores PIP_CONSTRAINT and takes only
        # build constraints, so the same file binds build dependencies too.
        yield {
            "PIP_FIND_LINKS": str(links),
            "PIP_CONSTRAINT": str(constraint),
            "PIP_BUILD_CONSTRAINT": str(constraint),
        }


def _verify_uv_machinery_wheel(package: dict) -> None:
    version = package.get("version", "")
    url = package["source"].get("url", "")
    wheels = package.get("wheels", [])
    if not isinstance(version, str) or _SAFE_VERSION.fullmatch(version) is None:
        raise LuMachineryAuditWheelError("uv.lock must pin an exact lu-machinery release version")
    filename = f"lu_machinery-{version}-py3-none-any.whl"
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or parsed.path.rsplit("/", 1)[-1] != filename
            or len(wheels) != 1 or wheels[0].get("url") != url):
        raise LuMachineryAuditWheelError("uv.lock must pin one lu-machinery HTTPS release wheel")
    match = _HASH_TOKEN.fullmatch(f"--hash={wheels[0].get('hash')}")
    if match is None:
        raise LuMachineryAuditWheelError("uv.lock pins lu-machinery without a SHA-256 wheel hash")
    _hash_checked_wheel(_LuMachineryPin(version, frozenset({match[1].lower()})), "uv.lock")


def _resolve_audit_path(path: Path, description: str) -> Path:
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LuMachineryAuditWheelError(f"cannot resolve {description}") from exc


def _uv_audit_export(repo_root: Path, lock: Path) -> dict[str, dict[str, set[str]]]:
    """Export every locked registry version, checking direct sources before excluding them."""
    uv = shutil.which("uv")
    if uv is None:
        raise LuMachineryAuditWheelError("uv is not on PATH; cannot export uv.lock for audit")
    excluded: dict[str, str] = {}
    registry: set[tuple[str, str]] = set()
    cpu_versions: set[tuple[str, str]] = set()
    name: str
    try:
        packages = tomllib.loads(_read_text(lock))["package"]
        for package in packages:
            name = canonicalize_name(package["name"], validate=True)
            source = package["source"]
            if len(source) != 1:
                raise LuMachineryAuditWheelError(f"uv.lock has ambiguous source for {name}")
            kind, origin = next(iter(source.items()))
            if name == _LU_MACHINERY_NAME:
                _verify_uv_machinery_wheel(package)
                excluded[name] = "hash-verified release wheel; published outside PyPI"
            elif kind == "registry":
                version = str(Version(package["version"]))
                registry.add((name, version))
                if name in {"torch", "torchvision"} and origin.rstrip("/") == "https://download.pytorch.org/whl/cpu":
                    cpu_versions.add((name, version))
            elif kind in {"directory", "editable", "virtual"}:
                local = _resolve_audit_path(repo_root / origin, f"uv.lock local source for {name}")
                if not local.is_relative_to(repo_root):
                    raise LuMachineryAuditWheelError(f"uv.lock local source for {name} escapes the checkout")
                excluded[name] = "local project; registry dependencies are audited"
            elif kind == "git" and re.fullmatch(r"https://[^/@?#]+/[^@#]+#[0-9a-f]{40}", origin):
                excluded[name] = "Git commit; registry dependencies are audited"
            else:
                raise LuMachineryAuditWheelError(f"uv.lock has unsupported {kind} source for {name}")
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
        raise LuMachineryAuditWheelError(f"invalid uv.lock audit input ({type(exc).__name__})") from exc
    if any(name in excluded for name, _ in registry):
        raise LuMachineryAuditWheelError("uv.lock mixes registry and direct sources for the same package")
    cmd = [uv, "export", "--locked", "--offline", "--no-build", "--no-python-downloads", "--format", "requirements.txt",
           "--all-groups", "--all-extras", "--no-emit-project", "--no-header", "--no-annotate"]
    for name in sorted(excluded):
        cmd.extend(["--no-emit-package", name])
    # Fixed uv export argv and validated distribution names, without a shell.
    try:
        result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            cmd, cwd=repo_root, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LuMachineryAuditWheelError(f"uv export failed ({type(exc).__name__})") from exc
    if result.returncode:
        raise LuMachineryAuditWheelError(f"uv export --locked refused the audit input (exit {result.returncode})")
    requirements: dict[str, dict[str, set[str]]] = {}
    exported = set()
    for _, line in _logical_lines(result.stdout):
        spec, hashes = _split_options(line)
        _line_names(repo_root, line)
        req = Requirement(spec)
        name = canonicalize_name(req.name)
        version = str(Version(next(iter(req.specifier)).version))
        if not hashes or (name, version) not in registry:
            raise LuMachineryAuditWheelError(f"uv export emitted an unhashed or non-registry requirement: {name}=={version}")
        exported.add((name, version))
        if (name, version) in cpu_versions:
            # PyPI advisories apply to the public version; the CPU build has no PyPI version endpoint.
            version = Version(version).public
        # Markers select installations. An advisory audit covers every version in the universal lock.
        requirements.setdefault(name, {}).setdefault(version, set()).update(hashes)
    if not exported or exported != registry:
        raise LuMachineryAuditWheelError(f"uv export covered {len(exported)} of {len(registry)} locked registry versions")
    for name, reason in sorted(excluded.items()):
        print(f"[audit_dependencies] {name}: {reason}", file=sys.stderr)
    return requirements


@contextmanager
def legacy_python_audit_input(repo_root: Path) -> Iterator[tuple[str, dict[str, str] | None]]:
    """Shared legacy input and checked-wheel environment for both audit callers."""
    repo_root = _resolve_audit_path(repo_root, "the audit checkout")
    requirements = repo_root / "requirements-lock.txt"
    if not requirements.is_file():
        raise LuMachineryAuditWheelError(f"{requirements} not found; missing required inputs fail closed")
    with pip_audit_requirement_env(repo_root, requirements) as extra:
        yield str(requirements), _pip_audit_process_env(extra)


def _checked_audit_report(result: subprocess.CompletedProcess[str], expected: set[tuple[str, str]]) -> list[dict]:
    try:
        dependencies = json_depth.loads(result.stdout, max_depth=512)["dependencies"]
        if not isinstance(dependencies, list):
            raise TypeError("dependencies must be a list")
        audited = set()
        for row in dependencies:
            if "skip_reason" in row or not isinstance(row["vulns"], list):
                raise ValueError("unverified dependency result")
            if any(not isinstance(vuln.get("id"), str) or not vuln["id"] for vuln in row["vulns"]):
                raise ValueError("vulnerability without an advisory ID")
            audited.add((canonicalize_name(row["name"], validate=True), str(Version(row["version"]))))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise LuMachineryAuditWheelError(f"pip-audit returned an invalid uv audit report (exit {result.returncode})") from exc
    if audited != expected or len(dependencies) != len(expected):
        raise LuMachineryAuditWheelError(
            f"pip-audit reported {len(audited & expected)} of {len(expected)} expected uv pins with exact versions"
        )
    if result.returncode != int(any(row["vulns"] for row in dependencies)):
        raise LuMachineryAuditWheelError(f"pip-audit exited {result.returncode} with an inconsistent uv audit report")
    return dependencies


def audit_uv_python(repo_root: Path, ignored_ids: list[str]) -> dict[str, list[dict]] | None:
    """Audit all locked registry versions. Return None only for a legacy checkout."""
    repo_root = _resolve_audit_path(repo_root, "the audit checkout")
    lock = repo_root / "uv.lock"
    if not os.path.lexists(lock):
        return None
    # pip-audit accepts one version per name in a run. Split forks across the fewest invocations.
    batches: list[dict[tuple[str, str], set[str]]] = []
    for name, versions in sorted(_uv_audit_export(repo_root, lock).items()):
        for index, (version, hashes) in enumerate(sorted(versions.items())):
            if index == len(batches):
                batches.append({})
            batches[index][name, version] = hashes
    dependencies = []
    with tempfile.TemporaryDirectory(prefix="uv-dependency-audit-") as tmp:
        requirements = Path(tmp) / "requirements.txt"
        cmd = [sys.executable, "-m", "pip_audit", "-r", str(requirements), "--require-hashes",
               "--disable-pip", "--strict", "--format", "json", "--progress-spinner=off"]
        for vuln_id in ignored_ids:
            cmd.extend(["--ignore-vuln", vuln_id])
        for batch in batches:
            requirements.write_text("".join(
                f"{name}=={version} {' '.join(sorted(hashes))}\n" for (name, version), hashes in batch.items()
            ), encoding="utf-8")
            # Fixed pip-audit argv with a validated, resolver-free requirements input.
            try:
                result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                    cmd, cwd=repo_root, capture_output=True, text=True, timeout=300
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LuMachineryAuditWheelError(f"pip-audit failed ({type(exc).__name__})") from exc
            dependencies.extend(_checked_audit_report(result, set(batch)))
    return {"dependencies": dependencies}


def audit_python(repo_root: Path, ignore_file: Path) -> int:
    """Run pip-audit on the product dependency lock with suppressions."""
    ignored_ids = load_pip_audit_ignores(ignore_file)
    options = ["--desc", "--progress-spinner=off"]
    for vuln_id in ignored_ids:
        options.extend(["--ignore-vuln", vuln_id])

    try:
        uv_report = audit_uv_python(repo_root, ignored_ids)
        if uv_report is not None:
            dependencies = uv_report["dependencies"]
            findings = 0
            for dependency in dependencies:
                for vuln in dependency["vulns"]:
                    print(f"{dependency['name']} {dependency['version']}: {vuln['id']}")
                    findings += 1
            print(f"[audit_dependencies] Audited {len(dependencies)} uv registry pins; {findings} findings.")
            return int(findings != 0)
        with legacy_python_audit_input(repo_root) as (requirements, process_env):
            cmd = [sys.executable, "-m", "pip_audit", "-r", requirements, *options]
            print(f"[audit_dependencies] Running pip-audit with {len(ignored_ids)} suppressed vulnerability ID(s)...")
            if process_env is None:
                res = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=300)
            else:
                # Fixed pip-audit argv. env only adds the checked wheel's find-links and constraint.
                res = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                    cmd, cwd=repo_root, capture_output=True, text=True, timeout=300, env=process_env
                )
    except LuMachineryAuditWheelError as exc:
        print(f"[audit_dependencies] Error: {exc}", file=sys.stderr)
        return 1
    if res.stdout:
        print(res.stdout)
    if res.stderr:
        print(res.stderr, file=sys.stderr)

    return res.returncode


SEVERITY_ORDER: dict[str, int] = {
    "info": 1,
    "low": 2,
    "moderate": 3,
    "medium": 3,
    "high": 4,
    "critical": 5,
}
SEVERITY_BY_RANK: dict[int, str] = {
    1: "info",
    2: "low",
    3: "moderate",
    4: "high",
    5: "critical",
}

KNOWN_CVE_ALIASES: dict[str, str] = {
    "CVE-2026-93748": "GHSA-CH52-4W7C-C8XP",
}


def resolve_cve_to_ghsa(cve_id: str, custom_aliases: dict[str, str] | None = None) -> str | None:
    """Resolve a CVE identifier to its canonical GHSA advisory ID.

    Checks:
    1. Caller/YAML custom aliases
    2. Static/offline known aliases table
    3. OSV vulnerability API (with short timeout; fails gracefully if offline)
    """
    clean = cve_id.strip().upper()
    if custom_aliases and clean in custom_aliases:
        return custom_aliases[clean].upper()
    if clean in KNOWN_CVE_ALIASES:
        return KNOWN_CVE_ALIASES[clean].upper()

    try:
        url = f"https://api.osv.dev/v1/vulns/{clean}"
        req = urllib.request.Request(url, headers={"User-Agent": "learn-ukrainian-ci"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json_depth.loads(resp.read().decode("utf-8"), max_depth=512)
            primary_id = str(data.get("id", ""))
            if primary_id.upper().startswith("GHSA-"):
                return primary_id.upper()
            for alias in data.get("aliases", []):
                if str(alias).upper().startswith("GHSA-"):
                    return str(alias).upper()
    except urllib.error.HTTPError as e:
        try:
            body = json_depth.loads(e.read().decode("utf-8"), max_depth=512)
            msg = str(body.get("message", ""))
            ghsa_match = re.search(r"GHSA-[a-z0-9-]+", msg, re.IGNORECASE)
            if ghsa_match:
                return ghsa_match.group(0).upper()
        except Exception:
            pass
    except Exception:
        pass

    return None


def load_npm_audit_ignores(ignore_file: Path | None) -> list[str]:
    """Load list of suppressed vulnerability IDs / package names from YAML config."""
    if not ignore_file or not ignore_file.exists():
        return []
    with open(ignore_file, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    custom_aliases: dict[str, str] = {}
    if isinstance(data.get("aliases"), dict):
        for k, v in data["aliases"].items():
            if str(k).strip() and str(v).strip():
                custom_aliases[str(k).strip().upper()] = str(v).strip().upper()

    vulns = data.get("vulnerabilities", [])

    # Collect intra-entry aliases where both id and cve are present
    for item in vulns:
        if isinstance(item, dict):
            entry_id = str(item.get("id", "")).strip().upper()
            entry_cve = str(item.get("cve", "")).strip().upper()
            if entry_id.startswith("GHSA-") and entry_cve.startswith("CVE-"):
                custom_aliases[entry_cve] = entry_id
            elif entry_id.startswith("CVE-") and entry_cve.startswith("GHSA-"):
                custom_aliases[entry_id] = entry_cve

    ignore_ids: list[str] = []
    for item in vulns:
        if isinstance(item, dict):
            has_id = bool(item.get("id") and str(item["id"]).strip())
            has_cve = bool(item.get("cve") and str(item["cve"]).strip())
            if has_id:
                raw_id = str(item["id"]).strip().upper()
                ignore_ids.append(raw_id)
                if raw_id.startswith("CVE-"):
                    resolved = resolve_cve_to_ghsa(raw_id, custom_aliases)
                    if resolved:
                        ignore_ids.append(resolved)
            if has_cve:
                raw_cve = str(item["cve"]).strip().upper()
                ignore_ids.append(raw_cve)
                resolved = resolve_cve_to_ghsa(raw_cve, custom_aliases)
                if resolved:
                    ignore_ids.append(resolved)
            if not has_id and not has_cve and item.get("package") and str(item["package"]).strip():
                ignore_ids.append(str(item["package"]).strip().lower())
        elif isinstance(item, str):
            val = item.strip()
            if val:
                val_upper = val.upper()
                ignore_ids.append(val_upper)
                if val_upper.startswith("CVE-"):
                    resolved = resolve_cve_to_ghsa(val_upper, custom_aliases)
                    if resolved:
                        ignore_ids.append(resolved)
    return [i for i in ignore_ids if i]


def is_advisory_suppressed(
    item: dict,
    ignored_set: set[str],
    ignored_pkgs: set[str],
) -> bool:
    """Check if an advisory dict is suppressed by ID, CVE, or package fallback."""
    for field in ("url", "title", "id", "name", "source"):
        val = str(item.get(field, ""))
        if not val:
            continue
        ghsa = re.search(r"GHSA-[a-z0-9-]+", val, re.IGNORECASE)
        if ghsa and ghsa.group(0).upper() in ignored_set:
            return True
        cve = re.search(r"CVE-\d{4}-\d+", val, re.IGNORECASE)
        if cve and cve.group(0).upper() in ignored_set:
            return True
        if val.upper() in ignored_set:
            return True

    dep = str(item.get("dependency", item.get("name", ""))).lower()
    return bool(dep and dep in ignored_pkgs)


def filter_npm_audit_vulnerabilities(vulns: dict, ignored_list: list[str]) -> tuple[set[str], dict]:
    """Filter npm audit v2 vulnerabilities against audited suppression IDs.

    Returns (suppressed_packages, unsuppressed_vulnerabilities).
    """
    expanded_ignored: list[str] = list(ignored_list)
    for item in ignored_list:
        if isinstance(item, str) and item.strip().upper().startswith("CVE-"):
            resolved = resolve_cve_to_ghsa(item.strip().upper())
            if resolved and resolved not in expanded_ignored:
                expanded_ignored.append(resolved)

    ignored_set = {i.upper() for i in expanded_ignored}
    ignored_pkgs = {
        i.lower() for i in expanded_ignored if not (i.upper().startswith(("GHSA-", "CVE-", "PYSEC-")) or i.isdigit())
    }

    direct_rank: dict[str, int] = {}
    unsuppressed_direct: dict[str, list[dict]] = {}

    for pkg_name, details in vulns.items():
        via_items = details.get("via", [])
        direct_advisories = [v for v in via_items if isinstance(v, dict)]
        transitive_deps = [v for v in via_items if isinstance(v, str)]

        unsuppressed_adv: list[dict] = []
        max_rank = 0

        for adv in direct_advisories:
            if is_advisory_suppressed(adv, ignored_set, ignored_pkgs):
                continue
            unsuppressed_adv.append(adv)
            sev = SEVERITY_ORDER.get(str(adv.get("severity", "")).lower(), 0)
            if sev == 0:
                sev = SEVERITY_ORDER.get(str(details.get("severity", "")).lower(), SEVERITY_ORDER["high"])
            if sev > max_rank:
                max_rank = sev

        if not direct_advisories and not transitive_deps:
            if pkg_name.lower() in ignored_pkgs:
                max_rank = 0
            else:
                max_rank = SEVERITY_ORDER.get(str(details.get("severity", "")).lower(), 0)

        direct_rank[pkg_name] = max_rank
        unsuppressed_direct[pkg_name] = unsuppressed_adv

    effective_rank = dict(direct_rank)
    changed = True
    while changed:
        changed = False
        for pkg_name, details in vulns.items():
            transitive_deps = [v for v in details.get("via", []) if isinstance(v, str)]
            current_rank = effective_rank[pkg_name]
            for dep in transitive_deps:
                dep_rank = effective_rank.get(dep, 0)
                if dep_rank > current_rank:
                    current_rank = dep_rank
            if current_rank > effective_rank[pkg_name]:
                effective_rank[pkg_name] = current_rank
                changed = True

    suppressed_pkgs: set[str] = set()
    unsuppressed: dict[str, dict] = {}

    for pkg_name, details in vulns.items():
        if effective_rank[pkg_name] == 0:
            suppressed_pkgs.add(pkg_name)
        else:
            transitive_deps = [v for v in details.get("via", []) if isinstance(v, str)]
            remaining_via = list(unsuppressed_direct[pkg_name]) + [
                dep for dep in transitive_deps if effective_rank.get(dep, 0) > 0
            ]
            info = dict(details)
            info["severity"] = SEVERITY_BY_RANK.get(effective_rank[pkg_name], details.get("severity", "high"))
            info["via"] = remaining_via
            unsuppressed[pkg_name] = info

    return suppressed_pkgs, unsuppressed


def audit_node(repo_root: Path, ignore_file: Path | None = None) -> int:
    """Run npm audit on production dependencies in repository root and site/."""
    targets = [
        ("root", repo_root),
        ("site", repo_root / "site"),
    ]
    ignored_ids = load_npm_audit_ignores(ignore_file) if ignore_file else []
    print(f"[audit_dependencies] Running npm audit with {len(ignored_ids)} suppressed vulnerability ID(s)...")

    exit_code = 0
    for label, target_dir in targets:
        pkg_file = target_dir / "package.json"
        lock_file = target_dir / "package-lock.json"
        if not pkg_file.exists() or not lock_file.exists():
            print(
                f"[audit_dependencies] Error: {pkg_file} or {lock_file} not found; missing required inputs fail closed.",
                file=sys.stderr,
            )
            return 1

        cmd = ["npm", "audit", "--omit=dev", "--audit-level=high", "--json"]
        print(f"[audit_dependencies] Running npm audit in {label} ({target_dir})...")
        res = subprocess.run(cmd, cwd=target_dir, capture_output=True, text=True, timeout=120)

        if res.returncode == 0:
            print(f"[audit_dependencies] npm audit ({label}): found 0 high/critical vulnerabilities.")
            continue

        try:
            audit_data = json_depth.loads(res.stdout, max_depth=512)
        except (ValueError, TypeError):
            print(f"[audit_dependencies] ERROR: Failed to parse npm audit JSON output in {label}:", file=sys.stderr)
            if res.stderr:
                print(res.stderr, file=sys.stderr)
            return 1

        if not isinstance(audit_data, dict):
            print(
                f"[audit_dependencies] ERROR: Unexpected npm audit report format (expected JSON object) in {label}.",
                file=sys.stderr,
            )
            return 1

        if "error" in audit_data:
            err = audit_data["error"]
            err_msg = err.get("summary") or err.get("detail") or err.get("code") or str(err)
            print(f"[audit_dependencies] ERROR: npm audit reported an error in {label}: {err_msg}", file=sys.stderr)
            return 1

        if "vulnerabilities" not in audit_data:
            print(
                f"[audit_dependencies] ERROR: Invalid npm audit report (missing 'vulnerabilities') in {label}.",
                file=sys.stderr,
            )
            return 1

        vulns = audit_data["vulnerabilities"]
        if not isinstance(vulns, dict):
            print(
                f"[audit_dependencies] ERROR: Invalid npm audit report ('vulnerabilities' is not an object) in {label}.",
                file=sys.stderr,
            )
            return 1

        if not vulns and res.returncode != 0:
            print(
                f"[audit_dependencies] ERROR: npm audit exited with code {res.returncode} but reported no vulnerabilities in {label}.",
                file=sys.stderr,
            )
            return 1

        suppressed, unsuppressed = filter_npm_audit_vulnerabilities(vulns, ignored_ids)
        high_critical_unsuppressed = {
            pkg: info for pkg, info in unsuppressed.items() if info.get("severity") in {"high", "critical"}
        }

        if high_critical_unsuppressed:
            print(
                f"[audit_dependencies] ERROR: Found {len(high_critical_unsuppressed)} unsuppressed High/Critical vulnerable package(s) in {label}:",
                file=sys.stderr,
            )
            for pkg, info in high_critical_unsuppressed.items():
                via_desc = []
                for v in info.get("via", []):
                    if isinstance(v, dict):
                        via_desc.append(f"{v.get('title', v.get('name'))} ({v.get('url', '')})")
                    else:
                        via_desc.append(str(v))
                print(
                    f"  - {pkg} ({info.get('severity', 'unknown')} severity): via {', '.join(via_desc)}",
                    file=sys.stderr,
                )
            exit_code = 1
        elif suppressed:
            print(
                f"[audit_dependencies] npm audit ({label}): all High/Critical vulnerable package(s) match audited suppressions."
            )
        else:
            print(f"[audit_dependencies] npm audit ({label}): no unsuppressed High/Critical vulnerabilities.")

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit production dependencies for vulnerabilities.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: the selected product checkout)",
    )
    parser.add_argument(
        "--ignore-file",
        type=Path,
        default=None,
        help="Path to .pip-audit-ignore.yaml",
    )
    parser.add_argument(
        "--npm-ignore-file",
        type=Path,
        default=None,
        help="Path to npm-audit-ignore.yaml",
    )
    parser.add_argument("--skip-python", action="store_true", help="Skip Python pip-audit")
    parser.add_argument("--skip-node", action="store_true", help="Skip Node.js npm audit")
    args = parser.parse_args(argv)

    repo_root = (args.repo_root or product_root()).resolve()
    if args.ignore_file:
        ignore_file = args.ignore_file.resolve()
    else:
        config_path = repo_root / "scripts/config/pip-audit-ignore.yaml"
        ignore_file = config_path if config_path.exists() else repo_root / ".pip-audit-ignore.yaml"

    if args.npm_ignore_file:
        npm_ignore_file = args.npm_ignore_file.resolve()
    else:
        npm_config_path = repo_root / "scripts/config/npm-audit-ignore.yaml"
        npm_ignore_file = npm_config_path if npm_config_path.exists() else repo_root / ".npm-audit-ignore.yaml"

    exit_code = 0

    if not args.skip_python:
        py_code = audit_python(repo_root, ignore_file)
        if py_code != 0:
            print(f"[audit_dependencies] ERROR: pip-audit failed with exit code {py_code}", file=sys.stderr)
            exit_code = py_code

    if not args.skip_node:
        node_code = audit_node(repo_root, npm_ignore_file)
        if node_code != 0:
            print(f"[audit_dependencies] ERROR: npm audit failed with exit code {node_code}", file=sys.stderr)
            if exit_code == 0:
                exit_code = node_code

    if exit_code == 0:
        print("[audit_dependencies] All dependency security audits passed.")
    else:
        print("[audit_dependencies] One or more dependency audits failed. Action required.", file=sys.stderr)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
