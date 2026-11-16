"""Audit Python and Node.js dependencies for security vulnerabilities.

Enforces blocking gates on Critical and High vulnerabilities while allowing
audited suppressions via .pip-audit-ignore.yaml.

Reference issue: learn-ukrainian-infra-private#703
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
_DIST_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_VERSION_OPERATOR = re.compile(r"^(===|==|~=|!=|<=|>=|<|>|\s|\()")
_EXACT_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==(?P<version>\S+)(?P<rest>.*)$"
)
_HASH_TOKEN = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})\Z")
_SAFE_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")


class LuMachineryAuditWheelError(RuntimeError):
    """The consumer requires lu-machinery and the checked wheel cannot be staged."""


def _pep503_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations and drop comments and blank lines."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        without_comment = raw.split(" #", 1)[0].rstrip()
        if without_comment.endswith("\\"):
            pending += without_comment[:-1].strip() + " "
            continue
        pending += without_comment.strip()
        if pending.strip():
            logical.append(" ".join(pending.split()))
        pending = ""
    if pending.strip():
        logical.append(" ".join(pending.split()))
    return logical


def _regex_distribution_name(body: str) -> str | None:
    """PEP 503 name from a specifier the standards parser did not accept."""
    if "@" in body:
        body = body.split("@", 1)[0].strip()
    body = body.split("[", 1)[0].strip()
    match = _DIST_NAME.match(body)
    if match is None:
        return None
    rest = body[match.end() :]
    if rest and _VERSION_OPERATOR.match(rest) is None:
        return None
    return _pep503_name(match.group(0))


def _normalized_distribution_name(specifier: str) -> str | None:
    """PEP 503 name of a requirement specifier, or None when it is not one.

    Parentheses (``name(>=1)``) and direct URLs are requirement forms. A local
    path is not a name; the caller resolves those separately.
    """
    body = specifier.split(";", 1)[0].strip()
    if not body or body.startswith(("-", ".", "/")):
        return None
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        return _regex_distribution_name(body)
    try:
        return _pep503_name(Requirement(body).name)
    except InvalidRequirement:
        return _regex_distribution_name(body)


def _strip_path_extras(token: str) -> str:
    if token.endswith("]") and "[" in token and "/" not in token[token.rfind("[") :]:
        return token.rpartition("[")[0]
    return token


def _path_requirement(repo_root: Path, line: str) -> Path | None:
    """Local path a requirements line installs, relative to the consumer root.

    Pip treats a separator or a leading dot as a path, including ``packages/runtime``
    and ``./packages/runtime[extra]``. Editable forms include ``-e ./path``,
    ``-e./path`` and ``-epath`` (the argument may be a relative path, an extra
    or a ``file:`` URL). ``name @ file://`` and a bare ``file:`` URL point at the
    same tree.
    """
    body = line.split(";", 1)[0].strip()
    if body.startswith("--editable="):
        body = body.split("=", 1)[1].strip()
    elif body.startswith("--editable "):
        body = body[len("--editable ") :].strip()
    elif body.startswith("-e "):
        body = body[3:].strip()
    elif body.startswith("-e") and len(body) > 2 and body[2] != "-":
        body = body[2:].strip()
    if not body or body.startswith("-"):
        return None
    url = body
    if " @ " in body:
        url = body.split(" @ ", 1)[1].strip()
    elif "@" in body:
        left, right = body.split("@", 1)
        if not (left.strip().startswith(".") or "/" in left):
            url = right.strip()
    url = _strip_path_extras(url.strip())
    if url.startswith("file:"):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "file":
            return None
        target = Path(urllib.parse.unquote(parsed.path))
    elif "://" in url:
        return None
    elif url.startswith(".") or "/" in url or url.startswith("~"):
        target = Path(url)
        if not target.is_absolute():
            target = repo_root / target
    else:
        return None
    return target


def _project_dependency_names(package_dir: Path) -> list[str]:
    """PEP 503 names from ``[project].dependencies``, excluding optional extras."""
    pyproject = package_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LuMachineryAuditWheelError(f"cannot read {pyproject}: {exc}") from exc
    project = data.get("project")
    if not isinstance(project, dict):
        return []
    dependencies = project.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise LuMachineryAuditWheelError(f"{pyproject} [project].dependencies is not a list")
    names: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        name = _normalized_distribution_name(dependency)
        if name is not None:
            names.append(name)
    return names


def _requirements_name_lu_machinery(repo_root: Path, requirements_file: Path) -> bool:
    """True when the lock or a path requirement names exactly ``lu-machinery``.

    ``lu-machinery-extras`` is a different distribution and does not count.
    """
    for line in _logical_lines(requirements_file.read_text(encoding="utf-8")):
        if _normalized_distribution_name(line) == _LU_MACHINERY_NAME:
            return True
        path = _path_requirement(repo_root, line)
        if path is not None and _LU_MACHINERY_NAME in _project_dependency_names(path):
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
    for line in _logical_lines(lock.read_text(encoding="utf-8")):
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
                raise LuMachineryAuditWheelError(
                    f"{lock.name} lu-machinery pin has an unsupported token {token!r}"
                )
            hashes.add(hashed.group(1).lower())
        if not hashes:
            raise LuMachineryAuditWheelError(f"{lock.name} pins lu-machinery without a sha256 hash")
        pins.append(_LuMachineryPin(version, frozenset(hashes)))
    if len(pins) != 1:
        raise LuMachineryAuditWheelError(
            f"{lock.name} must pin exactly one lu-machinery version; found {len(pins)}"
        )
    return pins[0]


def _hash_checked_wheel(pin: _LuMachineryPin) -> Path:
    """Wheel from ``LU_MACHINERY_WHEELHOUSE`` whose sha256 is one of the pin hashes."""
    raw = os.environ.get(_WHEELHOUSE_ENV, "").strip()
    filename = f"lu_machinery-{pin.version}-py3-none-any.whl"
    if not raw:
        raise LuMachineryAuditWheelError(
            "LU_MACHINERY_WHEELHOUSE is unset; refusing to fetch lu-machinery from PyPI"
        )
    wheel = Path(raw) / filename
    if not wheel.is_file():
        raise LuMachineryAuditWheelError(f"LU_MACHINERY_WHEELHOUSE has no {filename}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if digest not in pin.hashes:
        raise LuMachineryAuditWheelError(
            f"{filename} sha256 {digest} does not match requirements-machinery.lock"
        )
    return wheel


def _pip_audit_process_env(extra: Mapping[str, str]) -> dict[str, str] | None:
    """Process environment for pip-audit, or None when no extra variables apply."""
    if not extra:
        return None
    return {**os.environ, **extra}


@contextmanager
def pip_audit_requirement_env(repo_root: Path, requirements_file: Path) -> Iterator[dict[str, str]]:
    """Environment that lets pip-audit resolve ``lu-machinery`` without PyPI.

    Yields an empty mapping when the requirements do not name ``lu-machinery``,
    so the audit command stays exactly as it is today. Otherwise yields
    ``PIP_FIND_LINKS`` (a directory containing only the hash-checked wheel) and
    ``PIP_CONSTRAINT`` (an unhashed ``lu-machinery @ file:`` URL of that staged wheel).

    A version pin alone does not bind the candidate: pip may download another
    build of the same version from an index. A direct file URL does, and because
    it has no ``--hash`` it does not make pip require hashes on
    ``requirements-lock.txt``. The wheel comes from ``LU_MACHINERY_WHEELHOUSE``;
    this does not download it. A missing pin, a hash mismatch, or a missing wheel
    raises :class:`LuMachineryAuditWheelError` before the caller starts pip-audit.
    """
    if not _requirements_name_lu_machinery(repo_root, requirements_file):
        yield {}
        return
    pin = _lu_machinery_pin(repo_root / "requirements-machinery.lock")
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
        yield {"PIP_FIND_LINKS": str(links), "PIP_CONSTRAINT": str(constraint)}


def audit_python(repo_root: Path, ignore_file: Path) -> int:
    """Run pip-audit on requirements-lock.txt with suppressions."""
    req_file = repo_root / "requirements-lock.txt"
    if not req_file.exists():
        print(
            f"[audit_dependencies] Error: {req_file} not found; missing required inputs fail closed.", file=sys.stderr
        )
        return 1

    ignored_ids = load_pip_audit_ignores(ignore_file)
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(req_file),
        "--desc",
        "--progress-spinner=off",
    ]
    for vuln_id in ignored_ids:
        cmd.extend(["--ignore-vuln", vuln_id])

    try:
        with pip_audit_requirement_env(repo_root, req_file) as extra:
            print(f"[audit_dependencies] Running pip-audit with {len(ignored_ids)} suppressed vulnerability ID(s)...")
            process_env = _pip_audit_process_env(extra)
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
            data = json.loads(resp.read().decode("utf-8"))
            primary_id = str(data.get("id", ""))
            if primary_id.upper().startswith("GHSA-"):
                return primary_id.upper()
            for alias in data.get("aliases", []):
                if str(alias).upper().startswith("GHSA-"):
                    return str(alias).upper()
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
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
            audit_data = json.loads(res.stdout)
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
