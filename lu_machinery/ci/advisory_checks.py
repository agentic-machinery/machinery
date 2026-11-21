"""Exact advisory check names from ci.yml; unknown declarations grant no exemption.

CI Gate's ``needs`` includes scheduling-only jobs. Its scored set is the
``needs.<job>.result`` references in the gate, plus those jobs' dependencies.
An ordinary gate without ``always()`` also depends on every scheduling result.
Unsupported expressions or matrix shapes fail closed rather than guessing names.

External checks are not ci.yml jobs. Only an operator-decision entry below can
make one advisory, and only for check runs proven to come from that exact
GitHub App on the exact head; the display name alone grants nothing.
"""

from __future__ import annotations

import base64
import itertools
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

import yaml

from lu_machinery.common.repo_root import product_root

_RESULT = re.compile(r"needs\.([\w-]+)\.result\b")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_MATRIX_VALUE = re.compile(r"matrix\.([\w.-]+)\Z")
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdvisoryChecks:
    """A parsed allowlist, or an empty allowlist with a diagnostic reason."""

    names: frozenset[str] = frozenset()
    workflow: str = ""
    reason: str | None = None


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    if not isinstance(needs, list) or any(not isinstance(key, str) for key in needs):
        raise ValueError("invalid job dependencies")
    return needs


def _required_jobs(jobs: dict) -> set[str]:
    gates = [key for key, job in jobs.items() if job.get("name", key) == "CI Gate"]
    if len(gates) != 1:
        raise ValueError("expected one CI Gate")
    gate_id = gates[0]
    gate = jobs[gate_id]
    gate_needs = _needs(gate)
    # Do not treat the scheduling list as a scored list when an always() gate
    # explicitly checks results (as today's ci.yml does).
    body = yaml.safe_dump({key: value for key, value in gate.items() if key != "needs"})
    required = set(_RESULT.findall(body))
    for expression in _EXPRESSION.findall(body):
        if expression.strip() == "toJSON(needs)":
            # The gate passes every dependency result to its parser.
            required.update(gate_needs)
            continue
        if "needs" in expression and not re.fullmatch(
            r"\s*needs\.[\w-]+\.(?:result|outputs\.[\w-]+)\s*", expression
        ):
            raise ValueError("unsupported CI Gate needs expression")
    if not required or str(gate.get("if", "")).strip() not in {"always()", "${{ always() }}"}:
        required.update(gate_needs)
    required.add(gate_id)
    todo = list(required - {gate_id})
    while todo:
        key = todo.pop()
        if key not in jobs:
            raise ValueError("unknown CI Gate dependency")
        for dependency in _needs(jobs[key]):
            if dependency not in required:
                required.add(dependency)
                todo.append(dependency)
    return required


def _matrix_rows(matrix: object) -> list[dict]:
    if not isinstance(matrix, dict) or not matrix:
        raise ValueError("matrix must be a static mapping")
    axes = {key: value for key, value in matrix.items() if key not in {"include", "exclude"}}
    total = 1
    for values in axes.values():
        if not isinstance(values, list) or not values:
            raise ValueError("matrix axis must be a non-empty static list")
        total *= len(values)
    if total > 256 or "${{" in yaml.safe_dump(matrix):
        raise ValueError("dynamic or oversized matrix")
    original = [dict(zip(axes, values, strict=True)) for values in itertools.product(*axes.values())] if axes else []
    exclude = matrix.get("exclude", [])
    include = matrix.get("include", [])
    if any(not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries)
           for entries in (include, exclude)):
        raise ValueError("invalid matrix include/exclude")
    original = [row for row in original if not any(
        all(row.get(key) == value for key, value in selector.items()) for selector in exclude
    )]
    rows = [dict(row) for row in original]
    extra = []
    for addition in include:
        matched = False
        for base, row in zip(original, rows, strict=True):
            if all(key not in base or base[key] == value for key, value in addition.items()):
                row.update(addition)
                matched = True
        if not matched:
            extra.append(dict(addition))
    rows.extend(extra)
    if not rows or len(rows) > 256:
        raise ValueError("empty or oversized matrix")
    return rows


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ValueError("unsupported matrix name value")


def _job_names(key: str, job: dict) -> set[str]:
    name = job.get("name", key)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("invalid job name")
    strategy = job.get("strategy", {})
    if not isinstance(strategy, dict):
        raise ValueError("invalid job strategy")
    matrix = strategy.get("matrix")
    if matrix is None:
        if "${{" in name:
            raise ValueError("dynamic job name")
        return {name}
    rows = _matrix_rows(matrix)
    if "name" not in job:
        return {f"{key} ({', '.join(_scalar(value) for value in row.values())})" for row in rows}
    if "${{" not in name:
        raise ValueError("matrix name must explicitly reference its values")

    def render(expression: re.Match, row: dict) -> str:
        match = _MATRIX_VALUE.fullmatch(expression[1].strip())
        if match is None:
            raise ValueError("unsupported matrix name expression")
        value = row
        for part in match[1].split("."):
            if not isinstance(value, dict) or part not in value:
                raise ValueError("missing matrix name value")
            value = value[part]
        return _scalar(value)

    return {_EXPRESSION.sub(lambda expression, row=row: render(expression, row), name) for row in rows}


def _parse_workflow(workflow: object, path: str = ".github/workflows/ci.yml") -> AdvisoryChecks:
    """The advisory allowlist one parsed workflow declares; ValueError for an unsupported shape.

    ``path`` is the workflow file, whose repository-relative path GitHub shows as the
    workflow name when the file sets none.
    """
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("workflow jobs must be a mapping")
    jobs = workflow["jobs"]
    if any(not isinstance(key, str) or not isinstance(job, dict) for key, job in jobs.items()):
        raise ValueError("invalid workflow job")
    if any(dependency not in jobs for job in jobs.values() for dependency in _needs(job)):
        raise ValueError("unknown workflow dependency")
    workflow_name = workflow.get("name", path)
    if not isinstance(workflow_name, str) or not workflow_name or "${{" in workflow_name:
        raise ValueError("invalid workflow name")
    if not any(job.get("name", key) == "CI Gate" for key, job in jobs.items()) and all(
        job.get("continue-on-error", False) is False for job in jobs.values()
    ):
        # Advisory needs continue-on-error, so a workflow whose every job omits it or sets it to
        # false, and that has no CI Gate, declares none. Any other value still needs the gate.
        return AdvisoryChecks(workflow=workflow_name)
    required = _required_jobs(jobs)
    blocking_names: set[str] = set()
    advisory_names: set[str] = set()
    for key, job in jobs.items():
        names = _job_names(key, job)
        target = advisory_names if job.get("continue-on-error") is True and key not in required else blocking_names
        target.update(names)
    return AdvisoryChecks(frozenset(advisory_names - blocking_names), workflow_name)


class _WorkflowLoader(yaml.SafeLoader):
    """SafeLoader with the YAML 1.2 core schema's scalars, as GitHub Actions reads a workflow.

    PyYAML's YAML 1.1 schema also reads yes, no, on, off, y and n as booleans (so an
    unsupported ``continue-on-error: no`` would read as false), 010 as octal, 1_000 and
    1:20 as numbers, dates as timestamps, ``<<`` as a merge key and ``=`` as a value.
    Here those stay strings, as in YAML 1.2; an
    explicit ``!!bool`` or ``!!int`` tag on another spelling is a construction error.
    """

    bool_values: ClassVar[dict[str, bool]] = {"true": True, "false": False}


_YAML_1_1_TYPES = {f"tag:yaml.org,2002:{kind}" for kind in ("bool", "int", "float", "timestamp", "merge", "value")}
_WorkflowLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in _YAML_1_1_TYPES]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)
_YAML_1_2_INT = re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$")
_WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:int", _YAML_1_2_INT, list("-+0123456789"))
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
    list("-+.0123456789"),
)


def _construct_int(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> int:
    """A YAML 1.2 core integer: decimal (a leading zero is still decimal), 0o octal or 0x hex."""
    value = loader.construct_scalar(node)
    if not _YAML_1_2_INT.match(value):
        raise ValueError("not a YAML 1.2 integer")
    if value.startswith(("0o", "0x")):
        return int(value[2:], 8 if value[1] == "o" else 16)
    return int(value, 10)


_WorkflowLoader.add_constructor("tag:yaml.org,2002:int", _construct_int)


def _workflow_policy(text: str, path: str) -> AdvisoryChecks:
    """Parse one workflow file's text; every failure is a ValueError that quotes none of the file."""
    try:
        loader = _WorkflowLoader(text)
        try:
            workflow = loader.get_single_data()
        finally:
            loader.dispose()
    except Exception:
        # Parser and constructor messages (and their KeyError or ValueError) can carry file text.
        raise ValueError(f"{path} is not valid workflow YAML") from None
    try:
        return _parse_workflow(workflow, path)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{path} has an unsupported workflow shape ({type(exc).__name__})") from None


def load_advisory_checks(path: Path | None = None) -> AdvisoryChecks:
    """Read current ci.yml, reporting failures without leaking file contents."""
    try:
        return _workflow_policy(
            (path or (product_root() / ".github/workflows/ci.yml")).read_text(encoding="utf-8"),
            ".github/workflows/ci.yml",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        reason = f"ci.yml advisory checks unavailable: {detail}; all checks treated as blocking"
        _LOG.warning(reason)
        return AdvisoryChecks(reason=reason)


_REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
_WORKFLOW_DIRECTORY = ".github/workflows"
# The contents API returns at most this many entries of a directory, so a listing that long
# may be truncated and cannot prove a file absent.
_LISTING_CAP = 1000


def _listing_entry(entry: object) -> bool:
    """Whether a directory-listing entry names one immediate child of the workflow directory."""
    if not isinstance(entry, dict):
        return False
    name, path = entry.get("name"), entry.get("path")
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and "/" not in name
        and path == f"{_WORKFLOW_DIRECTORY}/{name}"
        and isinstance(entry.get("type"), str)
    )


def _workflow_text(document: object, entry: dict) -> str:
    """The decoded default-branch file the directory listing named; ValueError otherwise."""
    if (
        not isinstance(document, dict)
        or document.get("type") != "file"
        or document.get("path") != entry["path"]
        or not isinstance(entry.get("sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", entry["sha"])
        or document.get("sha") != entry["sha"]
        or document.get("encoding") != "base64"
        or not isinstance(document.get("content"), str)
    ):
        raise ValueError(f"{entry['path']} unreadable or changed since the directory listing")
    return base64.b64decode("".join(document["content"].split()), validate=True).decode("utf-8")


def load_repo_advisory_checks(repo: str, get_json: Callable[[str], object]) -> AdvisoryChecks:
    """The destination repository's policy, from its default-branch CI workflow, or a reason.

    The workflows are the repository's ``required_workflow`` setting (lu_machinery/config/
    fleet_repos.yaml, default ci.yml), read at the default branch, never the PR head, through
    ``get_json(endpoint)`` (the caller's GitHub client; None on failure). The directory
    listing proves which of them exist; one a complete, well-formed listing proves absent
    declares no advisory job. A listing at the API's entry cap proves nothing absent. An
    unreadable listing or file, a file that changed between the two reads, an unsupported
    workflow shape, or advisory jobs in more than one workflow is ``reason``: the caller refuses.
    Errors the caller's reads raise (a refusal, a rate limit) propagate.
    """
    try:
        if not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo) or {".", ".."} & set(repo.split("/")):
            raise ValueError("invalid repository")
        from lu_machinery.review.repair_round import RepairRefused, required_workflows

        try:
            paths = required_workflows(repo)
        except RepairRefused as exc:
            raise ValueError(exc.detail) from None
        listing = get_json(f"repos/{repo}/contents/{_WORKFLOW_DIRECTORY}")
        if not isinstance(listing, list) or not all(_listing_entry(entry) for entry in listing):
            raise ValueError("workflow directory unreadable")
        if len({entry["path"] for entry in listing}) != len(listing):
            raise ValueError("workflow directory lists a path twice")
        policies = []
        for path in paths:
            entries = [entry for entry in listing if entry["path"] == path]
            if not entries:
                if len(listing) >= _LISTING_CAP:
                    raise ValueError(f"{path} missing from a workflow directory listing that may be truncated")
                continue
            if entries[0]["type"] != "file":
                raise ValueError(f"{path} is not a file")
            document = get_json(f"repos/{repo}/contents/{quote(path, safe='/')}")
            policies.append(_workflow_policy(_workflow_text(document, entries[0]), path))
        named = [policy for policy in policies if policy.names]
        if len(named) > 1:
            raise ValueError("advisory jobs in more than one workflow")
        return named[0] if named else AdvisoryChecks()
    except ValueError as exc:
        # Only this module's own messages are quoted; a decoder's text could echo file bytes.
        detail = str(exc) if type(exc) is ValueError else type(exc).__name__
        reason = f"destination advisory policy unavailable: {detail}"
        _LOG.warning(reason)
        return AdvisoryChecks(reason=reason)


def is_advisory(name: str, *, workflow: str | None = None, policy: AdvisoryChecks | None = None) -> bool:
    """Exact name and workflow match only; missing workflow identity blocks."""
    policy = policy if policy is not None else load_advisory_checks()
    return name in policy.names and workflow is not None and workflow == policy.workflow


@dataclass(frozen=True)
class ExternalAdvisoryCheck:
    """A third-party check run the operator switched off, keyed by its GitHub App."""

    name: str
    app_id: int
    app_slug: str
    decision: str


# Operator decision OO-48 (#11184): the Entire integration is switched off (and removed from
# the repository, C-2928), so a "Entire Gates" run its GitHub App may still post is
# informational in every state. No other check shares the exemption.
OO48_ENTIRE_GATES = ExternalAdvisoryCheck(name="Entire Gates", app_id=2626939, app_slug="entire", decision="OO-48")
EXTERNAL_ADVISORY_CHECKS: tuple[ExternalAdvisoryCheck, ...] = (OO48_ENTIRE_GATES,)


def external_advisory_check(name: object) -> ExternalAdvisoryCheck | None:
    """The operator entry for an exact check name, which still needs provenance proof."""
    return next((entry for entry in EXTERNAL_ADVISORY_CHECKS if entry.name == name), None)


def is_external_advisory_run(entry: ExternalAdvisoryCheck, run: object, head: str) -> bool:
    """Whether a REST check run is ``entry``'s exact name, exact head and exact GitHub App."""
    if not isinstance(run, dict) or not isinstance(run.get("app"), dict):
        return False
    app_id = run["app"].get("id")
    return (
        run.get("name") == entry.name
        and run.get("head_sha") == head
        and type(app_id) is int
        and app_id == entry.app_id
        and run["app"].get("slug") == entry.app_slug
    )
