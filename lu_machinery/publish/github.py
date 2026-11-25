"""Closed-schema GitHub publishing; public bytes are scanned once and replayed.

Call publish(verb, ...typed fields...) or use python -m scripts.publish.
There is no argv, arbitrary endpoint, GraphQL document or payload passthrough.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

import yaml

from lu_machinery import cto_safe_mode
from lu_machinery.common import github_client, json_depth
from lu_machinery.common.repo_root import PRODUCT_ROOT_ENV, product_root
from lu_machinery.common.scratch import ensure_scratch_root
from lu_machinery.guardrails.worktree_containment import NotAGitRepositoryError, resolve_main_root_without_git
from lu_machinery.opsec import prepublish as gate
from lu_machinery.opsec.gh_snapshot import repository
from lu_machinery.publish import recovery

# The repository registry and the verdict recorder, by their checkout-relative paths. Those
# start with the machinery package's directory name, taken from this module's unresolved
# location: a symlinked package keeps its checkout name, and a short import spelling (the
# package directory on ``sys.path``) cannot shorten it.
_MACHINERY = Path(os.path.abspath(__file__)).parents[1].name
_FLEET_REPOS = f"{_MACHINERY}/config/fleet_repos.yaml"
_RECORDER = f"{_MACHINERY}/review/record_cf_verdict.py"
# This publisher's import name, as ``python -m`` runs it (never __package__).
_PUBLISHER = f"{_MACHINERY}.publish"

# Each field has one type and one meaning; strings never select gh flags.
COMMON = {"repo": "repo"}
TEXT = {"title": "text", "body": "text", "body_file": "file"}
EDIT = {**TEXT, "add_labels": "texts", "remove_labels": "texts", "milestone": "text"}
ASSETS = {"assets": "assets"}
# Documented merge_queue parameter bounds (GitHub REST, rulesets, API 2022-11-28).
# A value outside these bounds is refused before any request.
MERGE_QUEUE_INT_BOUNDS = {
    "check_response_timeout_minutes": (1, 360),
    "max_entries_to_build": (0, 100),
    "max_entries_to_merge": (0, 100),
    "min_entries_to_merge": (0, 100),
    "min_entries_to_merge_wait_minutes": (0, 360),
}
SCHEMAS = {
    "issue-create": (
        {"title", "body"},
        {**COMMON, **TEXT, "labels": "texts", "milestone": "text", "parent": "number", "epic": "bool"},
    ),
    "issue-edit": ({"number"}, {**COMMON, **EDIT, "number": "number", "remove_milestone": "bool"}),
    "issue-comment": ({"number", "body"}, {**COMMON, "number": "number", "body": "text", "body_file": "file"}),
    "issue-comment-json": ({"number", "body"}, {**COMMON, "number": "number", "body": "text", "body_file": "file"}),
    "comment-edit": ({"comment_id", "body"}, {**COMMON, "comment_id": "number", "body": "text", "body_file": "file"}),
    "issue-close": ({"number"}, {**COMMON, "number": "number", "comment": "text", "reason": "reason"}),
    "code-scanning-dismiss": (
        {"number", "reason", "comment"},
        {**COMMON, "number": "number", "reason": "dismissal_reason", "comment": "text"},
    ),
    # Owner-approved cleanup deletions: one obsolete merge-queue analysis, or one exact unused branch.
    "code-scanning-analysis-delete": ({"repo", "analysis_id"}, {**COMMON, "analysis_id": "number"}),
    "branch-delete": ({"repo", "branch"}, {**COMMON, "branch": "ref"}),
    "repo-fork": ({"repo"}, COMMON),
    "pr-create": (
        {"title", "body", "base", "head"},
        {**COMMON, **TEXT, "base": "text", "head": "text", "draft": "bool", "milestone": "text"},
    ),
    "pr-edit": ({"number"}, {**COMMON, **EDIT, "number": "number"}),
    "pr-comment": ({"number", "body"}, {**COMMON, "number": "number", "body": "text", "body_file": "file"}),
    "pr-review": (
        {"number", "verdict"},
        {**COMMON, "number": "number", "verdict": "verdict", "body": "text", "body_file": "file"},
    ),
    "pr-merge": (
        {"number"},
        {
            **COMMON,
            "number": "number",
            "subject": "text",
            "body": "text",
            "body_file": "file",
            "match_head": "sha",
            "expect_queue": "queue_mode",
            "recovery_run_id": "number",
        },
    ),
    "pr-update-branch": ({"number"}, {**COMMON, "number": "number"}),
    "pr-ready": ({"number"}, {**COMMON, "number": "number"}),
    "pr-close": ({"number"}, {**COMMON, "number": "number"}),
    "issue-reopen": ({"number"}, {**COMMON, "number": "number"}),
    "run-rerun": ({"number"}, {**COMMON, "number": "number"}),
    "run-cancel": ({"number"}, {**COMMON, "number": "number"}),
    "run-force-cancel": ({"number"}, {**COMMON, "number": "number"}),
    "workflow-run": (
        {"workflow", "ref"},
        {**COMMON, "workflow": "workflow", "ref": "ref", "inputs": "workflow_inputs"},
    ),
    "pr-disarm": ({"number"}, {**COMMON, "number": "number"}),
    "pr-dequeue": ({"node_id"}, {**COMMON, "node_id": "node"}),
    "release-create": (
        {"tag"},
        {
            **COMMON,
            "tag": "text",
            "title": "text",
            "notes": "text",
            "notes_file": "file",
            "target": "text",
            "draft": "bool",
            "prerelease": "bool",
            **ASSETS,
        },
    ),
    "release-edit": (
        {"tag"},
        {
            **COMMON,
            "tag": "text",
            "title": "text",
            "notes": "text",
            "notes_file": "file",
            "draft": "bool",
            "prerelease": "bool",
        },
    ),
    "release-upload": ({"tag", "assets"}, {**COMMON, "tag": "text", **ASSETS, "clobber": "bool"}),
    "gist-create": ({"files"}, {"files": "assets", "description": "text", "public": "bool"}),
    "label-create": (
        {"name", "color"},
        {**COMMON, "name": "text", "color": "color", "description": "text", "force": "bool"},
    ),
    "label-edit": ({"name"}, {**COMMON, "name": "text", "new_name": "text", "color": "color", "description": "text"}),
    "milestone-create": ({"title"}, {**COMMON, "title": "text", "description": "text", "state": "state"}),
    "milestone-edit": (
        {"number"},
        {**COMMON, "number": "number", "title": "text", "description": "text", "state": "state"},
    ),
    "commit-status": (
        {"sha", "state", "context", "description"},
        {**COMMON, "sha": "sha", "state": "status", "context": "text", "description": "text"},
    ),
    "issue-link": (
        {"parent_id", "child_id"},
        {**COMMON, "parent_id": "node", "child_id": "node", "replace_parent": "bool"},
    ),
    "issue-unlink": ({"parent_id", "child_id"}, {**COMMON, "parent_id": "node", "child_id": "node"}),
    # Named merge_queue parameters only. There is no generic ruleset editor.
    "ruleset-merge-queue": (
        {"ruleset"},
        {
            **COMMON,
            "ruleset": "number",
            **{name: "count" for name in MERGE_QUEUE_INT_BOUNDS},
            "grouping_strategy": "grouping_strategy",
            "merge_method": "merge_method",
        },
    ),
    # Both endpoints resolve through scripts/config/fleet_repos.yaml; the verb sends no text (C-4682).
    "issue-transfer": (
        {"repo", "number", "destination"},
        {"repo": "endpoint", "number": "number", "destination": "alias", "receipt_dir": "path", "dry_run": "bool"},
    ),
    # Operator-run only (GH-ADMIN-VERBS-1124): both organizations come from one dual-slug registry row
    # (repo-create stays private-only; repo-transfer admits registered public or private dual-slug rows).
    "repo-create": (
        {"repo", "name", "principal"}, {"repo": "alias", "name": "alias", "principal": "login", "dry_run": "bool"},
    ),
    "repo-transfer": (
        {"repo", "principal"},
        {"repo": "alias", "principal": "login", "create_receipt": "path", "dry_run": "bool"},
    ),
}
# Workflows this publisher may start. Product workflow_dispatch input names,
# required flags, booleans and choice options are read from each workflow file.
# There is no second copy of that schema. A string input is admitted only when its
# name is a PATTERNS key (coverage-ref's sha). Any other declared input makes
# that workflow refuse every dispatch.
WORKFLOWS = frozenset({
    "ci.yml",
    "deploy-pages.yml",
    "pytest-slow-nightly.yml",
    "security.yml",
    "stale-branch-cleanup.yml",
    "coverage-ref.yml",
})
# The merge-queue reader trusts review-verdict markers from the shared fleet login, so
# only scripts/review/record_cf_verdict.py may write them (#11127). Comment fields
# refuse the prefix unless the value is a CfVerdictRecord on the recorder's verb.
CF_VERDICT_MARKER_PREFIX = "<!-- cf-verdict"
COMMENT_FIELDS = {
    "comment-edit": "body",
    "issue-comment": "body",
    "issue-comment-json": "body",
    "pr-comment": "body",
    "pr-review": "body",
    "issue-close": "comment",
    "code-scanning-dismiss": "comment",
}
CF_VERDICT_RECORD_VERB = "issue-comment-json"

ENUMS = {
    "workflow": set(WORKFLOWS),
    "reason": {"completed", "not planned"},
    "dismissal_reason": {"false positive", "won't fix", "used in tests"},
    "verdict": {"approve", "comment", "request-changes"},
    "state": {"open", "closed"},
    "status": {"success", "failure", "error", "pending"},
    "grouping_strategy": {"ALLGREEN", "HEADGREEN"},
    "merge_method": {"MERGE", "SQUASH", "REBASE"},
    # The landing kind a caller checked its gate for: pr-merge refuses when the base differs.
    "queue_mode": {"true", "false"},
}
_MERGE_QUEUE_FIELDS = frozenset(MERGE_QUEUE_INT_BOUNDS) | frozenset({"grouping_strategy", "merge_method"})
# Keys a repository ruleset GET returns that PUT must not send back. A key in neither
# this set nor _RULESET_WRITABLE refuses the update, so a new GitHub field is not dropped.
_RULESET_READONLY = frozenset({
    "_links", "created_at", "current_user_can_bypass", "id", "node_id", "source", "source_type", "updated_at",
})
_RULESET_WRITABLE = frozenset({
    "name", "target", "enforcement", "bypass_actors", "conditions", "rules", "bypass_mode",
})
PATTERNS = {
    "repo": r"(?:[A-Za-z0-9.-]+/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    "ref": r"[A-Za-z0-9_][A-Za-z0-9_./-]*",
    "sha": r"[0-9a-fA-F]{40}",
    "node": r"[A-Za-z0-9_=-]+",
    "color": r"[0-9a-fA-F]{6}",
    # A registry alias, or owner/name; issue-transfer resolves either through the registry.
    "alias": r"[a-z0-9][a-z0-9-]*",
    "endpoint": r"[a-z0-9][a-z0-9-]*|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    # A GitHub login: letters, digits and single inner hyphens.
    "login": r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
}


def _declared_workflow_input(name: object, spec: object) -> tuple[object, bool] | None:
    """One workflow_dispatch input the publisher can constrain, or None."""
    if not isinstance(name, str) or not name or not isinstance(spec, dict):
        return None
    required = spec.get("required", False)
    if type(required) is not bool:
        return None
    kind = spec.get("type", "string")
    if kind == "boolean":
        return ("bool", required)
    if kind == "choice":
        options = spec.get("options")
        if (
            not isinstance(options, list)
            or not options
            or any(type(option) is not str or not option for option in options)
            or len(set(options)) != len(options)
        ):
            return None
        return (("choice", tuple(options)), required)
    if kind == "string" and name in PATTERNS:
        return (name, required)
    return None


def _workflow_yaml_loader() -> type[yaml.SafeLoader]:
    """Safe loader that keeps the trigger key as the string ``on``.

    PyYAML 1.1 treats ``on``, ``yes`` and ``true`` as the same boolean, and
    ``True == 1 == 1.0``, so a lookup of that boolean also matches numeric
    keys. Only ``true`` and ``false`` stay booleans. A file whose trigger is
    any other spelling does not match the string key ``on``.
    """

    class Loader(yaml.SafeLoader):
        pass

    resolvers = {
        key: [(tag, regexp) for tag, regexp in values if tag != "tag:yaml.org,2002:bool"]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.yaml_implicit_resolvers = resolvers
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        list("tTfF"),
    )
    return Loader


_WORKFLOW_YAML_LOADER = _workflow_yaml_loader()


def _workflow_input_schema(workflow: str, *, product_root: Path | None = None) -> dict[str, tuple[object, bool]] | None:
    """Input schema for an allowlisted workflow.

    Product workflows are read under ``product_root`` (an absolute path;
    default: the configured product checkout). An unavailable schema returns
    None and refuses every dispatch, including with an empty input object.
    A workflow that takes no inputs returns an empty schema.
    """
    if workflow not in WORKFLOWS or "/" in workflow or workflow.startswith("."):
        return None
    if product_root is not None and not (isinstance(product_root, Path) and product_root.is_absolute()):
        return None
    if workflow == "security.yml":
        # Fleet caller workflows live in the destination repositories and take no manual inputs.
        return {}
    from lu_machinery.common.repo_root import ProductRootError
    from lu_machinery.common.repo_root import product_root as resolve_product_root

    try:
        root = resolve_product_root() if product_root is None else product_root
    except ProductRootError:
        return None
    path = root / ".github" / "workflows" / workflow
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_WORKFLOW_YAML_LOADER)
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    if not isinstance(document, dict):
        return None
    triggers = document.get("on")
    if not isinstance(triggers, dict) or "workflow_dispatch" not in triggers:
        return None
    dispatch = triggers["workflow_dispatch"]
    if dispatch is None:
        return {}
    if not isinstance(dispatch, dict):
        return None
    declared = dispatch.get("inputs", {})
    if declared is None:
        return {}
    if not isinstance(declared, dict):
        return None
    schema: dict[str, tuple[object, bool]] = {}
    for name, spec in declared.items():
        parsed = _declared_workflow_input(name, spec)
        if parsed is None:
            return None
        schema[name] = parsed
    return schema


def _workflow_input_accepted(kind: object, raw: object) -> bool:
    if kind == "bool":
        return type(raw) is bool
    if isinstance(kind, tuple) and len(kind) == 2 and kind[0] == "choice" and isinstance(kind[1], tuple):
        return isinstance(raw, str) and raw in kind[1]
    return (
        isinstance(kind, str)
        and kind in PATTERNS
        and isinstance(raw, str)
        and re.fullmatch(PATTERNS[kind], raw) is not None
    )


def _validated_environment(env) -> dict[str, str]:
    """Copy the environment and refuse malformed GH_HOST even without a repo."""
    environment = dict(os.environ if env is None else env)
    if "GH_HOST" in environment:
        hostname = gate.normalize_hostname(environment["GH_HOST"])
        if hostname is None:
            raise gate.PublishBlocked("OPSEC: invalid publisher hostname.")
        environment["GH_HOST"] = hostname
    return environment


def _hostname_flag(dest: str) -> list[str]:
    """Select an API host from a destination already validated by the resolver."""
    if dest == "unknown":
        return []
    host = dest.split("/", 1)[0]
    return [] if host == "github.com" else ["--hostname", host]


def _validate(verb, fields):
    if verb not in SCHEMAS:
        raise gate.PublishBlocked("OPSEC: unknown publisher verb.")
    required, schema = SCHEMAS[verb]
    if set(fields) - set(schema):
        raise gate.PublishBlocked("OPSEC: unknown publisher field.")
    effective = (
        set(fields) | ({"body"} if "body_file" in fields else set()) | ({"notes"} if "notes_file" in fields else set())
    )
    if required - effective:
        raise gate.PublishBlocked("OPSEC: required publisher field missing.")
    if verb == "workflow-run":
        fields = {**fields, "inputs": fields.get("inputs", {})}
    for key, value in fields.items():
        kind = schema[key]
        valid = True
        if kind == "bool":
            valid = type(value) is bool
        elif kind == "count":
            valid = type(value) is int and value >= 0
        elif kind == "number":
            valid = type(value) is int and value > 0
        elif kind == "file":
            valid = isinstance(value, (str, Path))
        elif kind == "texts":
            valid = isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value)
        elif kind == "workflow_inputs":
            workflow = fields["workflow"]
            input_schema = (
                _workflow_input_schema(workflow) if isinstance(workflow, str) and workflow in WORKFLOWS else None
            )
            valid = isinstance(value, dict) and input_schema is not None and set(value) <= set(input_schema)
            if valid:
                required_inputs = {name for name, (_, is_required) in input_schema.items() if is_required}
                valid = required_inputs <= set(value) and all(
                    _workflow_input_accepted(input_kind, value[name])
                    for name, (input_kind, _) in input_schema.items()
                    if name in value
                )
        elif kind == "assets":
            valid = isinstance(value, (list, tuple)) and bool(value) and all(isinstance(x, Asset) for x in value)
        elif kind in ENUMS:
            valid = isinstance(value, str) and value in ENUMS[kind]
        elif kind in PATTERNS:
            valid = isinstance(value, str) and re.fullmatch(PATTERNS[kind], value) is not None
        else:
            valid = isinstance(value, str) and "\0" not in value
        if not valid:
            raise gate.PublishBlocked(f"OPSEC: invalid publisher field {key}.")
    if verb == "pr-create" and any(not fields.get(key) for key in ("base", "head")):
        raise gate.PublishBlocked("OPSEC: PR creation requires explicit base and head names.")
    if verb == "pr-create" and "milestone" in fields and not valid_milestone_text(fields["milestone"]):
        raise gate.PublishBlocked("MILESTONE_REFUSED: milestone must be a non-blank single-line value.")
    if verb == "pr-merge" and "subject" in fields and not "".join(
        gate.normalize_for_scan(fields["subject"]).splitlines()[:1]
    ).strip():
        raise gate.PublishBlocked("OPSEC: merge subject must not be blank.")
    for name in ("body", "notes"):
        if name in fields and name + "_file" in fields:
            raise gate.PublishBlocked("OPSEC: use one text input per field.")
    if verb == "ruleset-merge-queue":
        if not (set(fields) & _MERGE_QUEUE_FIELDS):
            raise gate.PublishBlocked("OPSEC: edit requires explicit merge queue fields.")
        for key, (low, high) in MERGE_QUEUE_INT_BOUNDS.items():
            if key in fields and not low <= fields[key] <= high:
                raise gate.PublishBlocked(f"OPSEC: invalid publisher field {key}.")


class CfVerdictRecord(str):
    """A review-verdict comment built by ``record_cf_verdict``; the only marker carrier.

    The CLI and ``body_file`` only ever produce plain ``str``, so a hand-written marker
    cannot reach a comment through the publisher. The record holds exactly one marker,
    on its last line, which is where the merge-queue reader looks for it.
    """

    def __new__(cls, text: str) -> CfVerdictRecord:
        if (
            not isinstance(text, str)
            or text.count(CF_VERDICT_MARKER_PREFIX) != 1
            or not text.rstrip("\n").split("\n")[-1].startswith(CF_VERDICT_MARKER_PREFIX)
        ):
            raise gate.PublishBlocked("CF_VERDICT_MARKER_REFUSED: malformed verdict record.")
        return super().__new__(cls, text)


def _refuse_verdict_markers(verb, fields):
    """Refuse a review-verdict marker in comment text unless the recorder built it."""
    key = COMMENT_FIELDS.get(verb)
    value = fields.get(key) if key else None
    if not isinstance(value, str) or CF_VERDICT_MARKER_PREFIX not in value:
        return
    if verb == CF_VERDICT_RECORD_VERB and type(value) is CfVerdictRecord:
        return
    raise gate.PublishBlocked(f"CF_VERDICT_MARKER_REFUSED: review-verdict markers are written only by {_RECORDER}.")


class Asset:
    """An explicit artifact source and public name; local paths are never public text."""

    def __init__(self, path: str | Path, name: str | None = None):
        self.path = Path(path)
        self.name = name if name is not None else self.path.name
        if (
            not isinstance(self.name, str)
            or not re.fullmatch(r"[^/\\\x00-\x1f\x7f#]+", self.name)
            or self.name in {".", ".."}
        ):
            raise gate.PublishBlocked("OPSEC: invalid artifact name.")


def _read_file(path, cwd, stdin):
    try:
        if str(path) == "-":
            value = stdin if stdin is not None else sys.stdin.buffer.read()
            return value.encode("utf-8") if isinstance(value, str) else bytes(value)
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = cwd / resolved
        return resolved.read_bytes()
    except Exception:
        raise gate.PublishBlocked("OPSEC: publisher file input unreadable.") from None


def _send(argv, *, environment, runner, cwd, **kwargs):
    """Send an admitted command through the shared client boundary."""
    environment = gate.internal_environment(environment)
    if runner is not None:
        return github_client.command(argv, runner=runner, env=environment, cwd=cwd, **kwargs)
    return _run_transport(argv, env=environment, cwd=cwd, **kwargs)


def _run_transport(command, **kwargs):
    """Execute an admitted command once; the client records and types limits."""
    return github_client.run(command, **kwargs)


def merge_readiness_runner(runner=None):
    """The read transport ``pr-merge`` gives its readiness reads; it performs no write itself.

    Reads are fresh and a stale observation or typed refusal is raised, never judged.
    """

    def readiness_runner(args, **kwargs):
        if runner is None:
            kwargs["fresh"] = True
        result = _send(args, environment=kwargs.pop("env"), runner=runner, cwd=kwargs.pop("cwd"), **kwargs)
        observation = getattr(result, "github_result", None)
        if observation is not None and observation.stale:
            raise github_client.GitHubRateLimited(observation.reset_at, observation.reason)
        refusal = github_client.refusal_of(result, injected=runner is not None)
        if refusal is not None:
            raise refusal
        return result

    return readiness_runner


SQUASH_TEXT_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){id headRefOid isMergeQueueEnabled "
    "viewerMergeHeadlineText(mergeType:SQUASH) viewerMergeBodyText(mergeType:SQUASH)}}}"
)


def _squash_text(gh_repo, fields, dest, temp, runner, cwd, environment):
    """Read GitHub's default squash subject and body for the pinned head."""
    owner, name = gh_repo.split("/", 1)
    query = temp / "squash-text.json"
    variables = {"owner": owner, "name": name, "number": fields["number"]}
    query.write_bytes(json.dumps({"query": SQUASH_TEXT_QUERY, "variables": variables}).encode("utf-8"))
    try:
        result = runner(
            ["gh", "api", "--method", "POST", "graphql", "--input", str(query), *_hostname_flag(dest)],
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            raise ValueError
        document = github_client.parse_graphql_envelope(result.stdout)
        pull = document["data"]["repository"]["pullRequest"]
        subject, body = pull["viewerMergeHeadlineText"], pull["viewerMergeBodyText"]
        if not (
            isinstance(subject, str)
            and isinstance(body, str)
            and isinstance(pull["id"], str) and bool(pull["id"])
            and type(pull["isMergeQueueEnabled"]) is bool
            and isinstance(pull["headRefOid"], str)
            and pull["headRefOid"].lower() == fields["match_head"].lower()
        ):
            raise ValueError
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        raise gate.PublishBlocked("OPSEC: merge refused: squash text unverifiable.") from None
    return {"subject": subject, "body": body, "queue": pull["isMergeQueueEnabled"], "id": pull["id"]}


QUEUE_ENTRY_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){headRefOid isInMergeQueue mergeQueueEntry{headCommit{oid}} "
    "autoMergeRequest{enabledAt}}}}"
)


def _confirm_enqueued(gh_repo, number, head, dest, temp, readers, cwd, environment):
    """Prove the reviewed head has a queue entry after a successful enqueue.

    A PR that GitHub left out of the queue, or armed for auto-merge instead, is a
    refusal naming the unmet requirement; armed auto-merge is disarmed first.
    ``readers`` is ``(readiness_runner, runner)``: fresh reads, then the write transport.
    """
    readiness_runner, runner = readers
    owner, name = gh_repo.split("/", 1)
    query = temp / "queue-entry.json"
    variables = {"owner": owner, "name": name, "number": number}
    query.write_bytes(json.dumps({"query": QUEUE_ENTRY_QUERY, "variables": variables}).encode("utf-8"))
    try:
        result = readiness_runner(
            ["gh", "api", "--method", "POST", "graphql", "--input", str(query), *_hostname_flag(dest)],
            cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=15,
        )
        if result.returncode != 0:
            raise ValueError
        document = github_client.parse_graphql_envelope(result.stdout)
        pull = document["data"]["repository"]["pullRequest"]
        if not isinstance(pull, dict) or type(pull["isInMergeQueue"]) is not bool or not isinstance(pull["headRefOid"], str):
            raise ValueError
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        raise gate.PublishBlocked(
            "ENQUEUE_UNVERIFIED: the enqueue was sent but queue membership is unreadable;"
            " read the PR's queue entry and auto-merge state before retrying."
        ) from None
    if pull["isInMergeQueue"] and pull["headRefOid"].lower() == head.lower():
        return
    if pull.get("autoMergeRequest") is not None:
        disarm = _send(
            ["gh", "pr", "merge", str(number), "--repo", gh_repo, "--disable-auto"],
            environment=environment, runner=runner, cwd=cwd, capture_output=True, text=True, check=False, timeout=30,
        )
        raise gate.PublishBlocked(
            "ENQUEUE_REFUSED: a merge requirement is unmet; GitHub armed auto-merge instead of queueing, "
            + ("and auto-merge was disarmed." if disarm.returncode == 0 else "and the disarm FAILED; run pr-disarm.")
        )
    raise gate.PublishBlocked(
        "ENQUEUE_REFUSED: a merge requirement is unmet; the reviewed head has no merge-queue entry after enqueue."
    )


def _refuse_incomplete_seats(dest, number, head, runner, cwd, environment):
    """Refuse a head the merge-queue keeper would not land on its recorded verdicts (#12020).

    An earlier recorder posted ``fleet/cross-family-review`` success after one seat,
    so the check rollup alone cannot prove the seat set, and a moved head carries no
    status at all. Every reading but an exact-head approval refuses: an incomplete seat
    set, a rejection, unverifiable evidence and reviews of other heads only. In a
    repository whose registry row sets ``verdict_recorder: true``, a head with no recorded
    approval is refused too (#12031). Where it is false, an ordinary PR with no recorded
    review is outside the recorder's regime and is not refused here, unless the head's own
    rollup carries the recorder's status: then its record was removed or rewritten, and the
    status alone is unverifiable. A High-tier or security-sensitive change still needs that
    one recorded approval where the recorder is off. A Trivial head (``review/tiers.py``)
    needs no recorded review in either regime; a recorded rejection or an unreadable
    record still refuses it. There is no second seat. A PR whose
    files change learner content takes its Ukrainian seats even with no Ukrainian verdict
    recorded yet (#12181). Review-contract canaries are release acceptance, not a PR gate.
    """
    from lu_machinery.fleet_comms.review_publication import DEFAULT_STATUS_CONTEXT
    from lu_machinery.orchestration.integration_sweep import (
        NEEDS_SEAT,
        changes_learner_content,
        lookup_verdict,
        pr_file_paths,
    )
    from lu_machinery.review.tiers import UNREVIEWED_STATES, classify_pr_files

    # The setting is explicit per repository; an absent, ambiguous or non-boolean one is unverifiable.
    entry = _fleet_catalog_entry(dest, environment)
    recorder = entry[1].get("verdict_recorder") if entry else None
    if type(recorder) is not bool:
        raise gate.PublishBlocked("OPSEC: merge refused: the repository's verdict_recorder setting is unverifiable.")
    configured_recorder_login = entry[1].get("verdict_recorder_login") if entry else None
    if configured_recorder_login is not None and (
        not isinstance(configured_recorder_login, str) or not re.fullmatch(r"\S+", configured_recorder_login)
    ):
        raise gate.PublishBlocked("OPSEC: merge refused: the repository's verdict_recorder_login is invalid.")
    try:
        documents = []
        for operation, selectors in (("comments", {"number": number}), ("pr-files", {"number": number}), ("identity", {})):
            paged = operation != "identity"
            result = read(
                operation, repo=dest, runner=runner, cwd=cwd, env=environment, capture_output=True,
                text=True, check=False, timeout=30, paginate=paged, slurp=paged, **selectors,
            )
            if result.returncode:
                raise ValueError
            documents.append(json_depth.loads(result.stdout, max_depth=512))
        pages, files, identity = documents
        login = identity.get("login") if isinstance(identity, dict) else None
        if not all(isinstance(paged, list) and all(isinstance(page, list) for page in paged) for paged in (pages, files)):
            raise ValueError
        if not isinstance(login, str) or not re.fullmatch(r"\S+", login):
            raise ValueError
        verdict_login = configured_recorder_login or login
        file_rows = [item for page in files for item in page]
        paths = pr_file_paths(file_rows)
        tier = classify_pr_files(file_rows)
        # A review run on a Trivial head is held to Low's non-author rule.
        low_tier = tier in {"trivial", "low"}
        content = changes_learner_content(paths)
        verdict = lookup_verdict(
            [item for page in pages for item in page], head, verdict_login, learner_content=content, low_tier=low_tier,
        )
        state = verdict.state
    except github_client.GitHubRateLimited:
        raise
    except Exception as exc:
        raise gate.PublishBlocked(
            f"OPSEC: merge refused: review seats unverifiable ({type(exc).__name__})."
        ) from None
    if state == NEEDS_SEAT:
        raise gate.PublishBlocked("OPSEC: merge refused: a Ukrainian review seat has not approved this head.")
    if tier == "trivial" and state in UNREVIEWED_STATES:
        # Trivial needs required CI only (review policy section 2): no review of this head is on record.
        state = "needs-CF"
    if state not in {"APPROVED", "needs-CF"}:
        raise gate.PublishBlocked(f"OPSEC: merge refused: recorded review verdict is {state} at this head.")
    needs_approval = (bool(recorder) or tier == "high") and tier != "trivial"
    if not needs_approval:
        try:
            from lu_machinery.review.security_paths import is_security_sensitive_change

            needs_approval = is_security_sensitive_change(paths)
        except Exception:
            needs_approval = True
    if state == "needs-CF" and needs_approval:
        requirement = "non-author" if low_tier else "cross-family"
        raise gate.PublishBlocked(
            f"OPSEC: merge refused: no {requirement} APPROVE from {_RECORDER} is recorded at {head}."
        )
    if state == "needs-CF":
        from lu_machinery.publish.merge_guard import _rollup_name

        argv = ["gh", "pr", "view", str(number), "--json", "headRefOid,statusCheckRollup", "--repo", dest.split("/", 1)[1]]
        # As in the readiness read: forced colour would wrap the JSON in escape codes.
        env = {key: value for key, value in environment.items() if key not in {"CLICOLOR_FORCE", "FORCE_COLOR"}}
        env.update(NO_COLOR="1", CLICOLOR="0")
        try:
            result = runner(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=30)
            bound = json_depth.loads(result.stdout, max_depth=512)
            rows = bound["statusCheckRollup"]
            # The rollup and its head arrive in one response, so these rows are the judged head's.
            if result.returncode or bound["headRefOid"].lower() != head.lower() or not isinstance(rows, list):
                raise ValueError
            if any(not isinstance(row, dict) or _rollup_name(row) is None for row in rows):
                raise ValueError
        except github_client.GitHubRateLimited:
            raise
        except Exception as exc:
            raise gate.PublishBlocked(
                f"OPSEC: merge refused: review seats unverifiable ({type(exc).__name__})."
            ) from None
        if any(DEFAULT_STATUS_CONTEXT in (row.get("name"), row.get("context")) for row in rows):
            raise gate.PublishBlocked("OPSEC: merge refused: this head's review status has no verdict record.")
    return None


def _keeper_custody_blocks_worker_recovery(dest: str, number: int, environment: dict[str, str]) -> None:
    """Give the keeper sole ownership of the recovery allowance for PRs under keeper custody.

    Dispatched workers (``LEARN_UKRAINIAN_DISPATCH_TASK_ID`` set) cannot consume the
    recovery allowance of a PR transferred to keeper custody. A missing ownership
    store means no keeper custody exists; an unreadable store fails closed.
    """
    caller_task = environment.get("LEARN_UKRAINIAN_DISPATCH_TASK_ID")
    if not caller_task:
        return
    from lu_machinery.orchestration import pr_ownership

    slug = dest.split("/", 1)[1] if "/" in dest else dest
    try:
        custody = pr_ownership.keeper_custody_for(None, slug, number)
    except Exception:
        raise gate.PublishBlocked(
            "RECOVERY_CUSTODY_UNKNOWN: ownership state unreadable; worker recovery refused"
        ) from None
    if custody is not None:
        raise gate.PublishBlocked(
            "RECOVERY_KEEPER_OWNED: PR is under keeper custody; only the keeper owns its recovery allowance"
        )


def _configured_verdict_login(dest: str, fallback: str, environment: dict[str, str]) -> str:
    entry = _fleet_catalog_entry(dest, environment)
    configured = entry[1].get("verdict_recorder_login") if entry else None
    if configured is not None:
        if not isinstance(configured, str) or not re.fullmatch(r"\S+", configured):
            raise gate.PublishBlocked("RECOVERY_LOOKUP_UNKNOWN: invalid verdict_recorder_login configuration")
        return configured
    return fallback


def _prepare_recovery(verb, fields, dest, cwd, environment, runner):
    """Resolve live PR/run identities and posted evidence before any recovery write."""
    from lu_machinery.gh_merge_queue_status import extract_pr_number
    from lu_machinery.orchestration.integration_sweep import changes_learner_content, lookup_verdict, pr_file_paths

    # An inactive, ambiguous or selector-less spelling is refused before any evidence read.
    catalog_entry = _fleet_catalog_entry(dest, environment)

    def _single_required_workflow() -> str | None:
        if catalog_entry is None:
            return None
        raw = catalog_entry[1].get("required_workflow", recovery.DEFAULT_REQUIRED_WORKFLOW)
        if isinstance(raw, str):
            workflow = raw
        elif isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], str):
            workflow = raw[0]
        else:
            return None
        return workflow if recovery.is_valid_workflow_path(workflow) else None

    def _valid_artifact_listing(listing: object) -> bool:
        return (
            isinstance(listing, dict)
            and type(listing.get("total_count")) is int
            and listing["total_count"] >= 0
            and isinstance(listing.get("artifacts"), list)
            and listing["total_count"] == len(listing["artifacts"])
            and all(
                isinstance(row, dict) and isinstance(row.get("name"), str) and bool(row["name"].strip())
                for row in listing["artifacts"]
            )
        )

    artifacts_cache: dict[int, dict] = {}
    jobs_complete = False

    def fetch(name, **selectors):
        nonlocal jobs_complete
        unknown = "RECOVERY_LOOKUP_UNKNOWN: live evidence unavailable"
        if verb == "run-rerun" and name == "run":
            unknown += "; Actions run lookup failed; --number requires an Actions run ID, not a PR number"
        try:
            if name == "queue-candidate":
                sel_num = selectors.get("number")
                cached = artifacts_cache.get(sel_num) if set(selectors) == {"number"} and type(sel_num) is int else None
                listing = cached if cached is not None else fetch("run-artifacts", **selectors)
                if not _valid_artifact_listing(listing):
                    raise ValueError("partial or malformed artifact listing")
                artifacts = listing["artifacts"]
                matches = [row for row in artifacts if row.get("name") == "ci-queue-candidate"]
                if len(matches) != 1:
                    raise ValueError("missing or ambiguous queue candidate artifact")
                artifact = matches[0]
                if (artifact.get("expired") is not False or type(artifact.get("id")) is not int
                        or type(artifact.get("size_in_bytes")) is not int
                        or not 0 < artifact["size_in_bytes"] <= 65536):
                    raise ValueError("queue candidate artifact unavailable")
                with tempfile.TemporaryFile(dir=ensure_scratch_root()) as archive_file:
                    read("artifact-archive", repo=dest, number=artifact["id"], cwd=cwd, env=environment,
                         runner=runner, stdout=archive_file, check=True, timeout=30)
                    if archive_file.tell() > 65536:
                        raise ValueError("queue candidate archive too large")
                    archive_file.seek(0)
                    with zipfile.ZipFile(archive_file) as archive:
                        members = archive.infolist()
                        if len(members) != 1 or members[0].filename != "queue-candidate.json" or members[0].file_size > 16384:
                            raise ValueError("queue candidate archive malformed")
                        return json_depth.loads(archive.read(members[0]), max_depth=512)
            result = read(
                name,
                repo=dest,
                cwd=cwd,
                env=environment,
                runner=runner,
                capture_output=True,
                text=True,
                check=True,
                paginate=name in {"comments", "timeline", "jobs", "pr-files"},
                slurp=name in {"comments", "timeline", "jobs", "pr-files"},
                **selectors,
            )
            refusal = github_client.refusal_of(result, injected=runner is not None)
            if refusal is not None:
                raise refusal
            value = json_depth.loads(result.stdout, max_depth=512)
            if name in {"comments", "timeline"}:
                return [item for page in value for item in page]
            if name == "pr-files":
                if not isinstance(value, list) or not all(isinstance(page, list) for page in value):
                    raise ValueError("PR files pagination incomplete")
                return pr_file_paths([item for page in value for item in page])
            if name == "jobs":
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(isinstance(page, dict) and isinstance(page.get("jobs"), list) for page in value)
                ):
                    raise ValueError("jobs pagination incomplete")
                collected = [job for page in value for job in page["jobs"]]
                if not all(isinstance(job, dict) for job in collected):
                    raise ValueError("jobs row malformed")
                totals = [page["total_count"] for page in value if "total_count" in page]
                if totals:
                    if (
                        len(totals) != len(value)
                        or any(type(total) is not int or total < 0 for total in totals)
                        or len(set(totals)) != 1
                        or len(collected) != totals[0]
                    ):
                        raise ValueError("jobs pagination incomplete")
                    ids = [job.get("id") for job in collected]
                    if any(type(job_id) is not int or job_id <= 0 for job_id in ids) or len(set(ids)) != totals[0]:
                        raise ValueError("jobs pagination incomplete or duplicate job IDs")
                    jobs_complete = True
                else:
                    jobs_complete = False
                return collected
            return value
        except github_client.GitHubRateLimited:
            raise
        except subprocess.CalledProcessError as exc:
            refusal = github_client.refusal_of(exc, injected=runner is not None)
            if refusal is not None:
                raise refusal from None
            raise gate.PublishBlocked(unknown) from None
        except Exception:
            raise gate.PublishBlocked(unknown) from None

    def _merge_group_associations_consistent(associations: object, pr_number: int) -> bool:
        return associations in (None, []) or (
            isinstance(associations, list)
            and len(associations) == 1
            and isinstance(associations[0], dict)
            and type(associations[0].get("number")) is int
            and associations[0]["number"] == pr_number
        )

    failed_run_conclusions = {"failure", "cancelled", "timed_out", "action_required"}
    incoherent_candidate = False
    if verb == "run-rerun":
        run_id = fields["number"]
        run = fetch("run", number=run_id)
        if not isinstance(run, dict) or run.get("id") != run_id:
            raise gate.PublishBlocked("RECOVERY_RUN_INVALID: run identity required")
        associations = run.get("pull_requests")
        event = run.get("event")
        if associations == [] and isinstance(event, str) and event and event not in {"pull_request", "pull_request_target", "merge_group"}:
            return None
        if run.get("status") != "completed" or run.get("conclusion") not in (
            failed_run_conclusions | recovery.PASSING_RUN_CONCLUSIONS
        ):
            raise gate.PublishBlocked(
                "RECOVERY_RUN_INVALID: completed failed or incoherent all-green run required"
            )
        incoherent_candidate = run.get("conclusion") in recovery.PASSING_RUN_CONCLUSIONS
        if run.get("event") == "merge_group":
            branch_pr = extract_pr_number(run.get("head_branch", ""))
            number = (
                branch_pr
                if type(branch_pr) is int and branch_pr > 0 and _merge_group_associations_consistent(associations, branch_pr)
                else None
            )
        elif isinstance(associations, list) and len(associations) == 1 and isinstance(associations[0], dict):
            number = associations[0].get("number")
        else:
            number = None
        if type(number) is not int or number <= 0:
            raise gate.PublishBlocked("RECOVERY_PR_UNKNOWN: run must identify one PR")
        pull = fetch("pull", number=number)
        head = pull.get("head", {}).get("sha")
        if pull.get("state") != "open" or not isinstance(head, str) or not recovery.SHA.fullmatch(head):
            raise gate.PublishBlocked("RECOVERY_HEAD_UNKNOWN: open PR head required")
        if run.get("event") != "merge_group" and associations[0].get("head", {}).get("sha") != head:
            raise gate.PublishBlocked("RECOVERY_HEAD_MOVED: run belongs to another PR head")
        action = "run-rerun"
        require_infra_diagnosis = False
    elif verb == "pr-merge":
        run_id = fields["recovery_run_id"]
        number = fields["number"]
        head = fields["match_head"]
        run = fetch("run", number=run_id)
        if not isinstance(run, dict) or run.get("id") != run_id:
            raise gate.PublishBlocked("RECOVERY_RUN_INVALID: run identity required")
        if run.get("event") != "merge_group":
            raise gate.PublishBlocked("RECOVERY_RUN_INVALID: merge_group failed run required for re-enqueue")
        if run.get("status") != "completed" or run.get("conclusion") not in failed_run_conclusions:
            raise gate.PublishBlocked("RECOVERY_RUN_INVALID: completed failed run required")
        if extract_pr_number(run.get("head_branch", "")) != number:
            raise gate.PublishBlocked("RECOVERY_PR_UNKNOWN: merge_group run does not identify this PR")
        if not _merge_group_associations_consistent(run.get("pull_requests"), number):
            raise gate.PublishBlocked("RECOVERY_PR_UNKNOWN: merge_group run has conflicting PR associations")
        if not isinstance(head, str) or not recovery.SHA.fullmatch(head):
            raise gate.PublishBlocked("RECOVERY_HEAD_UNKNOWN: open PR head required")
        action = "re-enqueue"
        require_infra_diagnosis = True
    else:
        raise gate.PublishBlocked("RECOVERY_ACTION_INVALID: unsupported recovery verb")
    _keeper_custody_blocks_worker_recovery(dest, number, environment)
    path = recovery.ledger_path(cwd)
    prior = recovery.first_attempt(path, dest, number, head)
    if prior:
        raise gate.PublishBlocked(recovery.spent_reason(prior))
    comments = fetch("comments", number=number)
    files = fetch("pr-files", number=number)
    content = changes_learner_content(files)
    login = fetch("identity").get("login")
    if not isinstance(login, str) or not login:
        raise gate.PublishBlocked("RECOVERY_APPROVAL_MISSING: exact-head recorded approval required")
    verdict_login = _configured_verdict_login(dest, login, environment)
    if lookup_verdict(
        comments, head, verdict_login, learner_content=content,
    ).state != "APPROVED":
        raise gate.PublishBlocked("RECOVERY_APPROVAL_MISSING: exact-head recorded approval required")
    failed_run = run
    live_jobs: list[dict] | None = None
    cancelled_no_artifact = False
    incoherent_ids: set[int] | None = None
    required_workflow = _single_required_workflow()
    failure_conclusions = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}

    def load_jobs() -> list[dict]:
        nonlocal live_jobs
        if live_jobs is None:
            live_jobs = fetch("jobs", number=run_id)
        return live_jobs

    def load_artifacts_listing() -> dict:
        if run_id not in artifacts_cache:
            listing = fetch("run-artifacts", number=run_id)
            if not _valid_artifact_listing(listing):
                raise gate.PublishBlocked("RECOVERY_LOOKUP_UNKNOWN: live evidence unavailable")
            artifacts_cache[run_id] = listing
        return artifacts_cache[run_id]

    if incoherent_candidate:
        if type(failed_run.get("run_attempt")) is not int or failed_run["run_attempt"] < 1:
            raise gate.PublishBlocked("RECOVERY_RUN_INVALID: completed run and attempt metadata required")
        if failed_run["run_attempt"] > 1:
            raise gate.PublishBlocked("RECOVERY_ALLOWANCE_SPENT: first=prior workflow run attempt")
        jobs_check = load_jobs()
        if not jobs_complete:
            raise gate.PublishBlocked("RECOVERY_LOOKUP_UNKNOWN: live evidence unavailable")
        incoherent_ids = recovery.incoherent_completed_run_job_ids(failed_run, jobs_check)
        if incoherent_ids is None:
            raise gate.PublishBlocked(
                "RECOVERY_RUN_INVALID: completed all-green run with incoherent check row required"
            )
        if not incoherent_ids:
            raise gate.PublishBlocked("RECOVERY_RUN_INVALID: coherent all-green run has nothing to recover")

    try:
        evidence = recovery.evidence_from_comments(
            comments,
            number,
            head,
            authenticated_login=login,
            run_id=run_id,
            require_infra_diagnosis=require_infra_diagnosis,
        )
    except gate.PublishBlocked as exc:
        if (
            not str(exc).startswith("RECOVERY_EVIDENCE_MISSING:")
            or failed_run.get("event") != "merge_group"
            or failed_run.get("conclusion") != "cancelled"
        ):
            raise
        jobs_check = load_jobs()
        if (
            not jobs_complete
            or not recovery.cancelled_merge_group_without_failure(
                failed_run, jobs_check, required_workflow=required_workflow, pr_number=number
            )
            or any(
                isinstance(job, dict) and job.get("conclusion") in failure_conclusions
                for job in jobs_check
            )
        ):
            raise
        listing = load_artifacts_listing()
        if any(row.get("name") == "ci-queue-candidate" for row in listing["artifacts"]):
            raise
        evidence = recovery.evidence_from_comments(
            comments,
            number,
            head,
            authenticated_login=login,
            run_id=run_id,
            require_infra_diagnosis=True,
            allow_empty_jobs=True,
        )
        cancelled_no_artifact = True
    allowed_conclusions = (
        recovery.PASSING_RUN_CONCLUSIONS if incoherent_candidate else failed_run_conclusions
    )
    if (type(failed_run.get("run_attempt")) is not int or failed_run["run_attempt"] < 1
            or failed_run.get("status") != "completed"
            or failed_run.get("conclusion") not in allowed_conclusions):
        raise gate.PublishBlocked("RECOVERY_RUN_INVALID: completed failed run and attempt metadata required")
    if failed_run["run_attempt"] > 1:
        raise gate.PublishBlocked("RECOVERY_ALLOWANCE_SPENT: first=prior workflow run attempt")
    if failed_run.get("head_sha") != evidence["tested_sha"]:
        raise gate.PublishBlocked("RECOVERY_EVIDENCE_MISMATCH: tested SHA does not match run")
    jobs = load_jobs()
    if incoherent_candidate:
        assert incoherent_ids is not None
        if incoherent_ids != set(evidence["job_ids"]):
            raise gate.PublishBlocked("RECOVERY_EVIDENCE_MISMATCH: every incoherent job must be diagnosed")
        evidence = {**evidence, "recovery_mode": recovery.INCOHERENT_CHECK_RECOVERY_MODE}
    else:
        failed_ids = {
            job.get("id")
            for job in jobs
            if isinstance(job, dict) and job.get("conclusion") in failure_conclusions
        }
        if failed_ids != set(evidence["job_ids"]) or (not failed_ids and not cancelled_no_artifact):
            raise gate.PublishBlocked("RECOVERY_EVIDENCE_MISMATCH: every failing job must be diagnosed")
        if failed_run.get("event") == "merge_group" and not cancelled_no_artifact:
            listing = load_artifacts_listing()
            candidate_matches = [row for row in listing["artifacts"] if row.get("name") == "ci-queue-candidate"]
            if not candidate_matches:
                if jobs_complete and recovery.cancelled_merge_group_without_failure(
                    failed_run, jobs, required_workflow=required_workflow, pr_number=number
                ):
                    cancelled_no_artifact = True
                else:
                    raise gate.PublishBlocked("RECOVERY_LOOKUP_UNKNOWN: live evidence unavailable")
    if cancelled_no_artifact:
        recovery.require_infra_diagnosis_evidence(evidence)
        action = "re-enqueue"
        evidence = {**evidence, "recovery_mode": recovery.CANCELLED_MERGE_GROUP_RECOVERY_MODE}
    elif failed_run.get("event") == "merge_group":
        recovery.require_queue_diff(evidence, failed_run, fetch)
    current = fetch("pull", number=number)
    if current.get("state") != "open" or current.get("head", {}).get("sha") != head:
        raise gate.PublishBlocked("RECOVERY_HEAD_MOVED: PR changed during preflight")
    return path, number, head, action, evidence


def _refuse_unreadable_criteria(body):
    """Refuse an issue body whose acceptance criteria the task lifecycle would reject.

    The lifecycle's own parser decides, so a body that publishes here also initializes
    there. A body with no criteria is not a task card and passes.
    """
    from lu_machinery.orchestration import task_lifecycle

    try:
        task_lifecycle.parse_issue_acceptance_criteria(body)
    except task_lifecycle.MalformedAcceptanceCriterion as exc:
        raise gate.PublishBlocked(
            f"ACCEPTANCE_CRITERIA_REFUSED: line {exc.line_number}: the task lifecycle rejects it ({exc}; "
            f"use `- [ ] **AC-01** — text`): {exc.line[:80]}"
        ) from None
    except task_lifecycle.LifecycleError as exc:
        if str(exc) != task_lifecycle.NO_ACCEPTANCE_CRITERIA:
            raise gate.PublishBlocked(f"ACCEPTANCE_CRITERIA_REFUSED: {exc}") from None


def _issue_node(read_runner, gh_repo, number, cwd, environment, *, allowed_slugs=None):
    """Return the node id of an open issue matching *gh_repo* and *number*, or None."""
    from urllib.parse import urlsplit

    expected_slug = _github_slug_or_none(gh_repo)
    if expected_slug is None or type(number) is not int or number < 1:
        return None
    result = read(
        "issue", repo=expected_slug, number=number, runner=read_runner, cwd=cwd, env=environment,
        capture_output=True, text=True, check=False, timeout=15,
    )
    if result.returncode:
        return None
    try:
        document = json_depth.loads(result.stdout, max_depth=512)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    if document.get("state") != "open" or "pull_request" in document:
        return None
    doc_number = document.get("number")
    if type(doc_number) is not int or doc_number != number:
        return None
    node_id = document.get("node_id")
    if not isinstance(node_id, str) or not node_id.strip():
        return None
    observed_slugs = []
    from lu_machinery.orchestration.fleet_repos import parse_ascii_positive_int

    repo_url = document.get("repository_url")
    if repo_url is not None:
        if not isinstance(repo_url, str) or not repo_url.isascii():
            return None
        parsed = urlsplit(repo_url.strip())
        if parsed.scheme != "https" or parsed.netloc.lower() != "api.github.com" or not parsed.path.startswith("/repos/"):
            return None
        repo_slug = _github_slug_or_none(parsed.path.removeprefix("/repos/"))
        if repo_slug is None:
            return None
        observed_slugs.append(repo_slug)
    html_url = document.get("html_url")
    if html_url is not None:
        if not isinstance(html_url, str) or not html_url.isascii():
            return None
        parsed_html = urlsplit(html_url.strip())
        if parsed_html.scheme != "https" or parsed_html.netloc.lower() != "github.com":
            return None
        parts = [p for p in parsed_html.path.split("/") if p]
        if len(parts) != 4 or parts[2] != "issues" or parse_ascii_positive_int(parts[3]) != number:
            return None
        html_slug = _github_slug_or_none(f"{parts[0]}/{parts[1]}")
        if html_slug is None:
            return None
        observed_slugs.append(html_slug)
    if not observed_slugs:
        return None
    if allowed_slugs is not None:
        accepted = {s.casefold() for s in allowed_slugs if isinstance(s, str) and s.isascii()}
        if not all(slug.isascii() and slug.casefold() in accepted for slug in observed_slugs):
            return None
    elif not all(_same_repository(slug, expected_slug) for slug in observed_slugs):
        return None
    return node_id


def _github_slug_or_none(value):
    """Return lowercase ``owner/name`` when *value* normalizes to host ``github.com``, else None."""
    normalized = gate.normalize_repository(value)
    host, sep, slug = normalized.partition("/")
    if host != "github.com" or not sep or not slug:
        return None
    return slug


def _registry_slug(value):
    """The registry's own owner/name spelling, case kept, when *value* names a github.com repository, else None.

    The host selector names a dual-slug row's active slug as the registry spells it, so the
    resolver compares against that spelling, never a lowercased copy.
    """
    slug = _github_slug_or_none(value)
    if slug is None:
        return None
    spelled = value.strip()
    return spelled if spelled.casefold() == slug else slug


def _catalog_fleet_repos(raw_catalog=None):
    """Convert ``gate.catalog()`` rows (or *raw_catalog*, one such read) into validated ``FleetRepo`` objects.

    Fails closed on any malformed key, row, ``github``, or ``alternate_github`` entry so
    broken dual-slug configuration can never downgrade into selector-free acceptance.
    """
    from lu_machinery.orchestration import fleet_repos

    if raw_catalog is None:
        raw_catalog = gate.catalog()
    if not isinstance(raw_catalog, dict) or not raw_catalog:
        raise gate.PublishBlocked("OPSEC: repository allowlist unavailable; write refused.")
    repos = {}
    defaults = 0
    for key, row in raw_catalog.items():
        if not isinstance(key, str) or not key.isascii() or not key.strip() or not isinstance(row, dict):
            raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        github = row.get("github")
        if not isinstance(github, str):
            raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        slug = _registry_slug(github)
        if slug is None:
            raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        default_raw = row.get("default", False)
        if type(default_raw) is not bool:
            raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        if default_raw:
            defaults += 1
        repo_id = row.get("repository_id")
        if repo_id is not None and (type(repo_id) is not int or repo_id <= 0):
            raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        visibility = row.get("visibility")
        if visibility is not None and (not isinstance(visibility, str) or visibility not in {"public", "private"}):
            raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        alt_raw = row.get("alternate_github")
        alt_slug = None
        if alt_raw is not None:
            if not isinstance(alt_raw, str):
                raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
            alt_slug = _registry_slug(alt_raw)
            if alt_slug is None:
                raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
            if alt_slug.casefold() == slug.casefold() or repo_id is None or visibility is None:
                raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
        repos[key] = fleet_repos.FleetRepo(
            key=key,
            github=slug,
            local_name=str(row.get("local_name") or key),
            role=str(row.get("role") or ""),
            default=default_raw,
            repository_id=repo_id,
            visibility=visibility,
            alternate_github=alt_slug,
        )
    if defaults != 1:
        raise gate.PublishBlocked("OPSEC: repository allowlist malformed; write refused.")
    return repos


def _fleet_catalog_row(gh_repo, *, env=None, repos=None, bind_inactive=False):
    """Resolve *gh_repo* through the A01 active-slug resolver, or None when unregistered.

    A dual-slug row's inactive spelling is refused, as every write gate requires; with
    *bind_inactive* (a configuration read of a stored spelling) either baked slug binds to the row.
    A dual-slug row with a missing or untrusted selector is refused either way.
    """
    from lu_machinery.orchestration import fleet_repos

    slug = _github_slug_or_none(gh_repo)
    if slug is None:
        return None
    if repos is None:
        repos = _catalog_fleet_repos()
    try:
        fleet_repos.resolve_fleet_repo_by_slug(slug, repos=repos)
    except fleet_repos.FleetRepoError:
        # A spelling that several rows claim is ambiguous, never unregistered.
        if sum(slug.casefold() in {item.casefold() for item in repo.slugs} for repo in repos.values()) > 1:
            raise gate.PublishBlocked(
                f"REPO_REFUSED: {slug} is registered more than once; nothing was created."
            ) from None
        return None
    try:
        active = fleet_repos.resolve_active_fleet_repo(slug, repos=repos, env=env)
    except fleet_repos.FleetRepoError as exc:
        raise gate.PublishBlocked(f"REPO_REFUSED: {exc}; nothing was created.") from None
    if not bind_inactive and active.active_github.casefold() != slug.casefold():
        raise gate.PublishBlocked(
            f"REPO_REFUSED: {slug} is not the active slug for {active.repo.key} "
            f"(active: {active.active_github}); nothing was created."
        )
    return active


def _fleet_catalog_entry(gh_repo, environment, *, bind_inactive=False):
    """The one lookup from a repository spelling to its fleet_repos.yaml row, through ``_fleet_catalog_row``.

    Returns ``(active, row)``: the resolved row bound to its active slug, and its raw catalog
    mapping from the same catalog read; None for an unregistered spelling.
    """
    raw_catalog = gate.catalog()
    active = _fleet_catalog_row(
        gh_repo, env=environment, repos=_catalog_fleet_repos(raw_catalog), bind_inactive=bind_inactive,
    )
    return None if active is None else (active, raw_catalog[active.repo.key])


def _owns_stream_registry(active, *, default_key=None):
    """True for any repository with a readable, valid section in issue_streams.yaml.

    Validates the registry for the default repository as well as non-default
    repositories so a missing or malformed ``issue_streams.yaml`` fails closed
    with ``REGISTRY_REFUSED``. Accepts a declared per-repo section with
    ``epics: []`` before its bootstrap root exists.
    """
    from lu_machinery.orchestration import issue_stream_audit

    effective_default = active.repo.key if active.repo.default else default_key
    try:
        has_registry = issue_stream_audit.repo_has_registry(
            active.repo.key,
            issue_stream_audit.default_registry_path(),
            default_key=effective_default,
        )
    except (OSError, ValueError) as exc:
        raise gate.PublishBlocked(f"REGISTRY_REFUSED: issue_streams.yaml is unreadable ({exc}); nothing was created.") from None
    if active.repo.default and not has_registry:
        raise gate.PublishBlocked(
            "REGISTRY_REFUSED: issue_streams.yaml defines no default-repository streams; nothing was created."
        )
    return has_registry


def _parent_epic_node(read_runner, active, repos, parent, cwd, environment):
    """Return the repository and node ID of the registered open parent before issue creation."""
    from lu_machinery.orchestration import fleet_repos, issue_stream_audit

    default_key = fleet_repos.default_repo_key(repos)
    target = active
    try:
        if not issue_stream_audit.repo_has_roots(
            active.repo.key,
            default_key=default_key,
            path=issue_stream_audit.default_registry_path(),
        ):
            target = fleet_repos.resolve_active_fleet_repo(default_key, repos=repos, env=environment)
            if not _same_slug(active.active_github.split("/", 1)[0], target.active_github.split("/", 1)[0]) or not all(
                item.repo.role.startswith("public-") and item.repo.visibility in (None, "public")
                for item in (active, target)
            ):
                raise gate.PublishBlocked(
                    "PARENT_REFUSED: cross-repository parents require two public repositories with the same "
                    "GitHub owner. Use a registered open stream epic in the target repository once it has one. "
                    "Nothing was created."
                )
        registry = issue_stream_audit.load_registry(
            issue_stream_audit.default_registry_path(),
            audit_only=True,
            repo=None if target.repo.default else target.repo.key,
        )
    except fleet_repos.FleetRepoError as exc:
        raise gate.PublishBlocked(f"REPO_REFUSED: {exc}; nothing was created.") from None
    except (OSError, ValueError) as exc:
        raise gate.PublishBlocked(f"REGISTRY_REFUSED: issue_streams.yaml is unreadable ({exc}); nothing was created.") from None
    requirement = f"--parent <number> of a registered, open stream epic in {target.active_github}"
    if parent is None:
        raise gate.PublishBlocked(f"PARENT_REQUIRED: issue-create requires {requirement}. Nothing was created.")
    epics = {number for numbers in registry.values() for number in numbers}
    node = (
        _issue_node(
            read_runner,
            target.active_github,
            parent,
            cwd,
            environment,
            allowed_slugs=target.repo.slugs,
        )
        if parent in epics
        else None
    )
    if node is None:
        raise gate.PublishBlocked(f"PARENT_REFUSED: use {requirement}. Nothing was created.")
    return target.active_github, node


def _created_resource_number(created, *, kind):
    """Extract a created issue/PR number only from pure-ASCII stdout before any strip()."""
    if not isinstance(created, str) or not created.isascii():
        return None
    match = re.search(rf"/{kind}/([1-9][0-9]*)[ \t\r\n]*$", created)
    if not match:
        return None
    from lu_machinery.orchestration.fleet_repos import parse_ascii_positive_int

    return parse_ascii_positive_int(match.group(1))


def _link_created_issue(created, parent_ref, parent_node, gh_repo, read_runner, runner, cwd, environment):
    """Link the issue ``created`` printed to the stream epic through the ``issue-link`` verb."""
    number = _created_resource_number(created, kind="issues")
    limited = link = None
    try:
        child_node = _issue_node(read_runner, gh_repo, number, cwd, environment) if number else None
        if child_node:
            link = publish(
                "issue-link", repo=gh_repo, parent_id=parent_node, child_id=child_node, runner=runner,
                cwd=cwd, env=environment, capture_output=True, text=True,
            )
            limited = github_client.refusal_of(link, injected=runner is not None)
    except github_client.GitHubRateLimited as exc:
        limited = exc
    except Exception:
        link = None
    which = f"Issue #{number}" if number else "The new issue"
    if limited is not None:
        # Keep the typed rate-limit exit and reset time, and still say which issue is unlinked.
        print(
            f"LINK_PENDING: {which} was created but not linked to parent {parent_ref}; "
            "link it with the issue-link verb after the rate limit resets.",
            file=sys.stderr,
        )
        raise limited
    if link is None or link.returncode:
        raise gate.PublishBlocked(
            f"LINK_FAILED: {which} was created but not linked to parent {parent_ref}; "
            "link it with the issue-link verb before doing anything else."
        )


# --- pr-create milestone resolution and application -----------------------------------------

_FENCED_CODE_RE = re.compile(r"(?ms)^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*$|\Z)")
_INLINE_CODE_RE = re.compile(r"(`+)([^`]*?)\1")
_HTML_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_CLOSING_ISSUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?ai:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t\r\n]*:?[ \t\r\n]+"
    r"(?:(?P<url>(?ai:https://github\.com)/(?P<url_repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?ai:issues)/(?P<url_num>[1-9][0-9]*))"
    r"|(?:(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(?P<num>[1-9][0-9]*)))(?!\w)"
)
_MAX_PR_CLOSING_ISSUES = 20


valid_milestone_text = github_client.valid_milestone_text


def _strip_markdown_code(text: str) -> str:
    """Drop fenced/inline code spans and HTML comments so examples/templates do not link issues."""
    without_fences = _FENCED_CODE_RE.sub("", text)
    without_comments = _HTML_COMMENT_RE.sub("", without_fences)
    return _INLINE_CODE_RE.sub("", without_comments)


def _pr_closing_issues(body: str, gh_repo: str) -> list[int]:
    """Same-repository issue numbers referenced by GitHub closing keywords in *body*."""
    from lu_machinery.orchestration.fleet_repos import parse_ascii_positive_int

    cleaned = _strip_markdown_code(body)
    found: list[int] = []
    for match in _CLOSING_ISSUE_RE.finditer(cleaned):
        if match.group("url"):
            ref_repo = match.group("url_repo")
            if not same_repository(ref_repo, gh_repo):
                continue
            number = parse_ascii_positive_int(match.group("url_num"))
        else:
            ref_repo = match.group("slug")
            if ref_repo is not None and not same_repository(ref_repo, gh_repo):
                continue
            number = parse_ascii_positive_int(match.group("num"))
        if number is not None and number not in found:
            found.append(number)
    return found


def match_milestone_pages(pages: object, raw_milestone: str, *, repo: str) -> tuple[int, str]:
    """Match *raw_milestone* (title or positive integer) to one open milestone in *pages*."""
    try:
        return github_client.match_milestone_pages(pages, raw_milestone, require_open=True)
    except github_client.GitHubMilestoneError as exc:
        prefix = "MILESTONE_AMBIGUOUS" if exc.reason == "milestone_ambiguous" else "MILESTONE_REFUSED"
        if exc.reason == "invalid_milestone":
            raise gate.PublishBlocked(f"{prefix}: {exc}.") from None
        raise gate.PublishBlocked(f"{prefix}: {exc} in {repo}; nothing was created.") from None


def extract_issue_milestone(document: object, *, number: int, repo: str) -> tuple[int, str] | None:
    """Return ``(milestone_number, title)`` for one open-milestoned issue payload, or ``None``."""
    if not isinstance(document, dict) or type(document.get("number")) is not int or document["number"] != number:
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: linked issue #{number} in {repo} returned malformed metadata; nothing was created."
        )
    if "pull_request" in document:
        return None
    raw = document.get("milestone")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: linked issue #{number} in {repo} has malformed milestone metadata; nothing was created."
        )
    ms_num = raw.get("number")
    ms_title = raw.get("title")
    ms_state = raw.get("state")
    if (
        type(ms_num) is not int
        or ms_num <= 0
        or not isinstance(ms_title, str)
        or not valid_milestone_text(ms_title)
        or not isinstance(ms_state, str)
        or ms_state not in {"open", "closed"}
    ):
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: linked issue #{number} in {repo} has malformed milestone metadata; nothing was created."
        )
    if ms_state != "open":
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: linked issue #{number} milestone {ms_title!r} (#{ms_num}) in {repo} "
            f"is {ms_state}, not open; nothing was created."
        )
    return ms_num, ms_title


def resolve_repo_milestone(
    repo: str,
    raw_milestone: str,
    *,
    runner=None,
    cwd=None,
    env=None,
) -> tuple[int, str]:
    """Validate *raw_milestone* against the live milestones of *repo*."""
    if not valid_milestone_text(raw_milestone):
        raise gate.PublishBlocked("MILESTONE_REFUSED: milestone must be a non-blank single-line value.")
    environment = _validated_environment(env)
    work = Path(cwd or Path.cwd())
    dest = repository(work, environment, repo)
    if dest == "unknown":
        raise gate.PublishBlocked("OPSEC: publisher repository unresolved; provide --repo.")
    gh_repo = dest.split("/", 1)[1]
    return _resolve_explicit_milestone(
        merge_readiness_runner(runner),
        dest,
        gh_repo,
        raw_milestone,
        work,
        environment,
        injected=runner is not None,
    )


def _resolve_explicit_milestone(
    read_runner, dest, gh_repo, raw_milestone, cwd, environment, *, injected: bool = False,
) -> tuple[int, str]:
    try:
        result = read(
            "milestones", repo=dest, runner=read_runner, cwd=cwd, env=environment,
            capture_output=True, text=True, check=False, timeout=15, paginate=True, slurp=True,
        )
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: repository milestones for {gh_repo} are unreadable; nothing was created."
        ) from None
    limited = github_client.refusal_of(result, injected=injected)
    if limited is not None:
        raise limited
    if result.returncode:
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: repository milestones for {gh_repo} are unreadable; nothing was created."
        )
    try:
        pages = json_depth.loads(result.stdout, max_depth=512)
    except (ValueError, TypeError):
        raise gate.PublishBlocked(
            f"MILESTONE_REFUSED: repository milestones payload for {gh_repo} is malformed; nothing was created."
        ) from None
    return match_milestone_pages(pages, raw_milestone, repo=gh_repo)


_HTTP_404_RE = re.compile(r"\bHTTP\s+404\b")


def is_not_found_result(result) -> bool:
    """True only when *result* carries a structured HTTP 404 observation or gh/GitHub 404 response."""
    observation = getattr(result, "github_result", None)
    if observation is not None:
        return bool(observation.status == 404)
    stderr = str(getattr(result, "stderr", "") or "")
    if _HTTP_404_RE.search(stderr):
        return True
    stdout = str(getattr(result, "stdout", "") or "").strip()
    if stdout:
        try:
            doc = json_depth.loads(stdout, max_depth=64)
        except (ValueError, TypeError):
            doc = None
        if isinstance(doc, dict):
            status = doc.get("status")
            if status in (404, "404"):
                return True
            if status is None and doc.get("message") == "Not Found":
                return True
    return False


def _resolve_linked_issue_milestone(
    read_runner, dest, gh_repo, body, cwd, environment, *, injected: bool = False,
) -> tuple[int, str] | None:
    issue_numbers = _pr_closing_issues(body, gh_repo)
    if not issue_numbers:
        return None
    if len(issue_numbers) > _MAX_PR_CLOSING_ISSUES:
        raise gate.PublishBlocked(
            f"MILESTONE_AMBIGUOUS: PR body references more than {_MAX_PR_CLOSING_ISSUES} closing issues; "
            "specify --milestone explicitly; nothing was created."
        )
    resolved: dict[int, str] = {}
    for number in issue_numbers:
        try:
            result = read(
                "issue", repo=dest, number=number, runner=read_runner, cwd=cwd, env=environment,
                capture_output=True, text=True, check=False, timeout=15,
            )
        except github_client.GitHubRateLimited:
            raise
        except Exception:
            raise gate.PublishBlocked(
                f"MILESTONE_REFUSED: linked issue #{number} in {gh_repo} is unreadable; nothing was created."
            ) from None
        limited = github_client.refusal_of(result, injected=injected)
        if limited is not None:
            raise limited
        if result.returncode:
            if is_not_found_result(result):
                continue
            raise gate.PublishBlocked(
                f"MILESTONE_REFUSED: linked issue #{number} in {gh_repo} is unreadable; nothing was created."
            )
        try:
            document = json_depth.loads(result.stdout, max_depth=512)
        except (ValueError, TypeError):
            raise gate.PublishBlocked(
                f"MILESTONE_REFUSED: linked issue #{number} in {gh_repo} returned malformed JSON; nothing was created."
            ) from None
        milestone = extract_issue_milestone(document, number=number, repo=gh_repo)
        if milestone is not None:
            resolved[milestone[0]] = milestone[1]
    if not resolved:
        return None
    if len(resolved) > 1:
        titles = ", ".join(sorted(resolved.values()))
        raise gate.PublishBlocked(
            f"MILESTONE_AMBIGUOUS: linked issues {issue_numbers} have conflicting milestones ({titles}); "
            "specify --milestone explicitly; nothing was created."
        )
    ((ms_number, ms_title),) = resolved.items()
    return ms_number, ms_title


def _apply_pr_milestone(created, milestone_number, milestone_title, dest, gh_repo, runner, cwd, environment, temp):
    """Apply the pre-validated milestone to the newly created PR via Issues PATCH."""
    number = _created_resource_number(created, kind="pull")
    limited = applied = None
    try:
        if number:
            frozen = temp / "pr-milestone.json"
            frozen.write_bytes(json.dumps({"milestone": milestone_number}).encode("utf-8"))
            applied = _send(
                [
                    "gh", "api", "--method", "PATCH", f"repos/{gh_repo}/issues/{number}",
                    "--input", str(frozen), *_hostname_flag(dest),
                ],
                environment=environment, runner=runner, cwd=cwd, capture_output=True, text=True,
                check=False, timeout=30,
            )
            limited = github_client.refusal_of(applied, injected=runner is not None)
    except github_client.GitHubRateLimited as exc:
        limited = exc
    except Exception:
        applied = None
    created_pr = github_client.created_pr_detail({"number": number, "html_url": created.strip() if isinstance(created, str) and created.isascii() else ""}) or None
    pr_url = created_pr.get("pr_url") if created_pr else None
    which = (
        f"PR #{number} ({pr_url})"
        if number and pr_url
        else f"PR #{number}"
        if number
        else f"The new PR ({pr_url})"
        if pr_url
        else "The new PR"
    )
    if limited is not None:
        print(
            f"MILESTONE_PENDING: {which} was created but milestone {milestone_title!r} (#{milestone_number}) "
            "was not applied; apply it with pr-edit after the rate limit resets.",
            file=sys.stderr,
        )
        raise github_client.GitHubRateLimited(limited.reset_at, limited.reason, created_pr=created_pr)
    if applied is None or applied.returncode:
        raise gate.PublishBlocked(
            f"MILESTONE_FAILED: {which} was created but milestone {milestone_title!r} (#{milestone_number}) "
            "was not applied; apply it with pr-edit."
        )
    raw_out = str(applied.stdout or "").strip()
    updated = None
    if raw_out:
        try:
            updated = json_depth.loads(raw_out, max_depth=512)
        except (ValueError, TypeError):
            updated = None
    if not github_client.confirm_pr_milestone(updated, milestone_number, pr_number=number):
        raise gate.PublishBlocked(
            f"MILESTONE_FAILED: {which} was created but milestone {milestone_title!r} (#{milestone_number}) "
            "was not confirmed on the PR; verify with pr-edit."
        )


# --- issue-transfer (C-4682) ---------------------------------------------------------------
# One open issue moves between two registered repositories of one owner. Static refusals
# come first, then fresh reads, then exactly one transferIssue mutation. Nothing after the
# mutation can send a second one; once it is sent, every failure exits TRANSFER_UNSETTLED.

TRANSFER_REPOSITORY_QUERY = (
    "query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){"
    "id nameWithOwner visibility viewerPermission isArchived "
    "labels(first:100,after:$cursor){totalCount pageInfo{hasNextPage endCursor} nodes{name}}}}"
)
TRANSFER_ISSUE_LABELS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){"
    "issue(number:$number){id labels(first:100,after:$cursor){totalCount pageInfo{hasNextPage endCursor} "
    "nodes{name}}}}}"
)
TRANSFER_MUTATION = (
    "mutation($issue:ID!,$repository:ID!){transferIssue(input:{issueId:$issue,repositoryId:$repository,"
    "createLabelsIfMissing:false}){issue{id number url repository{nameWithOwner}}}}"
)
TRANSFER_WRITE_PERMISSIONS = frozenset({"ADMIN", "MAINTAIN", "WRITE"})
TRANSFER_VISIBILITY = {"private": "PRIVATE", "public": "PUBLIC"}
TRANSFER_UNSETTLED = 3
TRANSFER_LABEL_PAGES = 50
# GitHub serves the source redirect and the moved issue's labels some time after the mutation
# returns (one issue took about 85 s on 2026-11-04), so verification re-reads with a bounded
# backoff: eight fresh reads over 180 seconds.
TRANSFER_VERIFY_DELAYS = (0, 2, 4, 8, 16, 30, 60, 60)
_transfer_sleep = time.sleep
TRANSFER_PROBED = frozenset({"transferred", "already-transferred", "unverified", "ambiguous"})
TRANSFER_PROBE_NOTE = (
    "An anonymous HTTP status alone does not prove confidentiality: GitHub serves a transfer "
    "banner at the old URL to readers without access to the destination."
)
_API_REPOSITORY = re.compile(r"https://api\.github\.com/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


def _transfer_refused(reason):
    return gate.PublishBlocked(f"TRANSFER_REFUSED: {reason}; nothing was transferred.")


def _transfer_endpoint(value, environment):
    """Resolve a registry alias or owner/name to exactly one registered github.com repository at its active slug.

    An owner/name must be the row's active slug; an alias binds to it.
    """
    rows = gate.catalog()
    if not isinstance(rows, dict):
        raise _transfer_refused("the repository registry is unreadable")
    if "/" in value:
        wanted = gate.normalize_repository(value, environment.get("GH_HOST", "github.com"))
        entry = _fleet_catalog_entry(wanted, environment)
    else:
        row = rows.get(value)
        github = row.get("github") if isinstance(row, dict) else None
        entry = _fleet_catalog_entry(github, environment, bind_inactive=True) if isinstance(github, str) else None
        if entry is not None and entry[0].repo.key != value:
            entry = None
    if entry is None:
        raise _transfer_refused(f"{value} is not exactly one repository registered in {_FLEET_REPOS}")
    active, row = entry
    alias, role = active.repo.key, row.get("role")
    canonical = gate.normalize_repository(active.active_github)
    privacy = None
    if isinstance(role, str):
        privacy = "private" if role.startswith("private-") else "public" if role.startswith("public-") else None
    if not canonical.startswith("github.com/") or privacy is None:
        raise _transfer_refused(f"the registry row for {alias} has no github.com repository or no public-/private- role")
    repository_name = canonical.split("/", 1)[1]
    return {"alias": alias, "repository": repository_name, "owner": repository_name.split("/", 1)[0], "privacy": privacy}


def _transfer_checkouts():
    checkouts = [gate.ROOT.resolve()]
    with contextlib.suppress(NotAGitRepositoryError):
        checkouts.append(resolve_main_root_without_git(gate.ROOT).resolve())
    return checkouts


def _product_checkouts(environment, refused=_transfer_refused):
    """The product checkout and its primary, which a transfer receipt must also stay out of.

    The package's own tree is no product checkout on an installed release (it is
    site-packages), so ``product_root()`` under the publication's environment names it;
    an unresolved product root refuses the transfer. The root is canonical before its
    primary is looked up, so a symlinked root still bounds its primary checkout.
    """
    from lu_machinery.common.repo_root import ProductRootError, product_root

    try:
        product = Path(product_root(env=environment)).resolve()
    except ProductRootError:
        raise refused(
            "the product checkout is unresolved (set LU_PRODUCT_ROOT to its absolute path), so the receipt "
            "boundary is unknown"
        ) from None
    checkouts = [product]
    with contextlib.suppress(NotAGitRepositoryError):
        checkouts.append(resolve_main_root_without_git(product).resolve())
    return checkouts


def _private_directory(value, subdirectory, environment, refused):
    """Open a private 0700 directory outside every checkout: (descriptor, real path); the caller closes it.

    The default is ``$XDG_STATE_HOME/learn-ukrainian/<subdirectory>`` (else ``~/.local/state/...``).
    The directory is opened once and checked through that descriptor (owner, mode, real path),
    so files created relative to it cannot be moved into a checkout by a parent swapped later.
    """
    if value is None:
        state = os.environ.get("XDG_STATE_HOME", "")
        base = Path(state) if os.path.isabs(state) else Path.home() / ".local" / "state"
        directory = base / "learn-ukrainian" / subdirectory
    else:
        directory = Path(value).expanduser()
        if not directory.is_absolute():
            raise refused("the receipt directory must be an absolute path")
    directory = Path(os.path.abspath(directory))
    # Lexical check first, so nothing is ever created inside a checkout.
    checkouts = [*_product_checkouts(environment, refused), *_transfer_checkouts()]
    if any(directory.resolve().is_relative_to(root) for root in checkouts):
        raise refused("the receipt directory must be outside the repository")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise refused("the receipt directory cannot be created or opened") from None
    try:
        info = os.fstat(directory_fd)
        real = Path(os.readlink(f"/proc/self/fd/{directory_fd}"))
        if not real.is_absolute() or any(
            real.is_relative_to(root) for root in (*_product_checkouts(environment, refused), *_transfer_checkouts())
        ):
            raise refused("the receipt directory must be outside the repository")
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise refused("the receipt directory must be a mode 0700 directory owned by this user")
    except OSError:
        os.close(directory_fd)
        raise refused("the receipt directory cannot be created or opened") from None
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd, real


def _reserve_receipt(value, subdirectory, stem, environment, refused):
    """Create a new 0600 receipt ``<stem>-<UTC time>.json`` in the private directory."""
    from datetime import UTC, datetime

    directory_fd, real = _private_directory(value, subdirectory, environment, refused)
    try:
        name = f"{stem}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        return {"path": real / name, "fd": os.open(name, flags, 0o600, dir_fd=directory_fd)}
    except OSError:
        raise refused("the receipt file cannot be created") from None
    finally:
        os.close(directory_fd)


def _reserve_transfer_receipt(value, source, number, environment):
    """Create a new 0600 issue-transfer receipt in a private directory outside every checkout."""
    stem = f"{source['repository'].replace('/', '-')}-{number}"
    return _reserve_receipt(value, "issue-transfer", stem, environment, _transfer_refused)


def _write_transfer_receipt(sink, receipt):
    """Write the receipt once into its reserved file; False when it cannot be written."""
    fd, sink["fd"] = sink["fd"], None
    if fd is None:
        return False
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except OSError:
        os.close(fd)
        return False
    try:
        with handle:
            encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
            return handle.write(encoded) == len(encoded)
    except OSError:
        return False


def _anonymous_probe(url):
    """One credential-free GET: no token, cookie, netrc or proxy; record the final URL and status."""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "learn-ukrainian-issue-transfer-probe"})
    try:
        with opener.open(request, timeout=20) as response:
            return {"url": url, "final_url": response.geturl(), "status": response.status, "error": None}
    except urllib.error.HTTPError as exc:
        return {"url": url, "final_url": exc.geturl(), "status": exc.code, "error": None}
    except Exception as exc:
        return {"url": url, "final_url": None, "status": None, "error": type(exc).__name__}


def _probe_failed(probe):
    status = probe.get("status")
    return probe.get("error") is not None or type(status) is not int or status >= 500


def _transfer_resolution(endpoint, number, reader, cwd, environment):
    """What source/number resolves to now; the REST read follows a transferred issue's redirect."""
    result = read(
        "issue", repo=endpoint["repository"], runner=reader, cwd=cwd, env=environment, capture_output=True,
        text=True, check=False, timeout=30, number=number,
    )
    if result.returncode:
        raise ValueError("issue unreadable")
    document = json_depth.loads(result.stdout, max_depth=512)
    resolved = {
        "repository": None, "number": document["number"], "node_id": document["node_id"],
        "state": document["state"], "url": document["html_url"], "pull_request": "pull_request" in document,
    }
    match = _API_REPOSITORY.fullmatch(document["repository_url"]) if isinstance(document["repository_url"], str) else None
    if (
        match is None or type(resolved["number"]) is not int or resolved["number"] <= 0
        or not isinstance(resolved["node_id"], str) or not re.fullmatch(PATTERNS["node"], resolved["node_id"])
        or resolved["state"] not in {"open", "closed"} or not isinstance(resolved["url"], str)
    ):
        raise ValueError("malformed issue")
    resolved["repository"] = match[1]
    if not resolved["pull_request"] and not _same_repository_url(
        resolved["url"], match[1], f"issues/{resolved['number']}"
    ):
        raise ValueError("issue URL does not match its repository")
    return resolved


def _same_transfer_identity(current, identity, repository):
    """Whether two GraphQL label pages belong to the same object.

    Node id and metadata must stay equal across pages; a repository's
    ``nameWithOwner`` may switch between the requested row's two baked slugs.
    """
    if not isinstance(current, dict) or not isinstance(identity, dict) or set(current) != set(identity):
        return False
    for key, value in current.items():
        if key == "nameWithOwner" and repository is not None:
            if value == identity[key]:
                continue
            if not (_same_repository(value, repository) and _same_repository(identity[key], repository)):
                return False
        elif value != identity[key]:
            return False
    return True


def _transfer_label_connection(query, variables, select, reader, cwd, environment, temp, *, repository=None):
    """Every label of one GraphQL label connection: paginated, count-checked and distinct.

    ``select`` maps the repository object to (identity fields, label page); the identity must
    stay the same on every page (allowing ``nameWithOwner`` to vary across the requested row's
    two baked slugs when *repository* is given).
    """
    labels, cursor, identity, total = [], None, None, None
    for _ in range(TRANSFER_LABEL_PAGES):
        frozen = temp / "query.json"
        frozen.write_bytes(json.dumps({"query": query, "variables": {**variables, "cursor": cursor}}).encode("utf-8"))
        result = reader(
            ["gh", "api", "--method", "POST", "graphql", "--input", str(frozen)],
            cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode:
            raise ValueError("label read unreadable")
        document = github_client.parse_graphql_envelope(result.stdout)
        current, page = select(document["data"]["repository"])
        if identity is None:
            identity, total = current, page["totalCount"]
        if (
            not _same_transfer_identity(current, identity, repository)
            or page["totalCount"] != total
            or type(total) is not int
        ):
            raise ValueError("labels changed while reading")
        names = [node["name"] for node in page["nodes"]]
        if not all(isinstance(label, str) and label for label in names):
            raise ValueError("malformed label")
        labels += names
        more = page["pageInfo"]["hasNextPage"]
        if more is False:
            break
        cursor = page["pageInfo"]["endCursor"]
        if more is not True or not isinstance(cursor, str):
            raise ValueError("malformed label page")
    else:
        raise ValueError("too many label pages")
    # GitHub label names are unique without regard to case, so a repeat means a misread page.
    if len(labels) != total or len({label.casefold() for label in labels}) != len(labels):
        raise ValueError("incomplete or repeated labels")
    return identity, labels


def _transfer_repository(endpoint, reader, cwd, environment, temp):
    """Fresh identity, visibility, permission and complete label names of one repository."""
    owner, name = endpoint["repository"].split("/", 1)
    fields = ("id", "nameWithOwner", "visibility", "viewerPermission", "isArchived")
    identity, labels = _transfer_label_connection(
        TRANSFER_REPOSITORY_QUERY, {"owner": owner, "name": name},
        lambda repo: ({key: repo[key] for key in fields}, repo["labels"]), reader, cwd, environment, temp,
        repository=endpoint["repository"],
    )
    if not isinstance(identity["id"], str) or not identity["id"]:
        raise ValueError("repository id unreadable")
    return {**identity, "labels": labels}


def _transfer_issue_labels(repository_name, number, node_id, reader, cwd, environment, temp):
    """Complete label names of one issue, bound to the expected issue node id."""
    owner, name = repository_name.split("/", 1)
    identity, labels = _transfer_label_connection(
        TRANSFER_ISSUE_LABELS_QUERY, {"owner": owner, "name": name, "number": number},
        lambda repo: ({"id": repo["issue"]["id"]}, repo["issue"]["labels"]), reader, cwd, environment, temp,
    )
    if identity["id"] != node_id:
        raise ValueError("the labels belong to another issue")
    return labels


def _missing_labels(labels, present):
    kept = {name.casefold() for name in present}
    return [name for name in labels if name.casefold() not in kept]


def _repository_slugs(repository):
    """Both baked slugs of a transferred fleet row, else the spelling itself; an unreadable catalog names none."""
    # Imported here: the fleet catalog stays out of the publisher's import closure.
    from lu_machinery.orchestration.fleet_repos import FleetRepoError, repository_slugs

    try:
        return repository_slugs(repository)
    except FleetRepoError:
        return ()


def same_repository(left, right):
    """Whether two owner/name spellings name one repository, ignoring case; either baked slug of a row counts."""
    # Slugs are ASCII; casefold would map look-alikes such as U+212A onto them.
    if not isinstance(left, str) or not isinstance(right, str) or not left.isascii() or not right.isascii():
        return False
    return left.casefold() == right.casefold() or any(
        left.casefold() == slug.casefold() for slug in _repository_slugs(right)
    )


_same_repository = same_repository


def _repository_urls(repository, path):
    """``https://github.com/<slug>/<path>`` for each baked slug of *repository*."""
    return tuple(f"https://github.com/{slug}/{path}" for slug in _repository_slugs(repository))


def _same_repository_url(url, repository, path):
    return isinstance(url, str) and url.isascii() and any(
        url.casefold() == known.casefold() for known in _repository_urls(repository, path)
    )


def _transfer_verify(source, destination, number, moved, receipt, reader, cwd, environment, temp):
    """Bind the source URL and the moved issue's labels to the returned issue; nothing here writes.

    Each attempt is a fresh read. The last failure's detail is raised once the attempts run out.
    """
    detail = "the transfer could not be verified"
    for attempt, delay in enumerate(TRANSFER_VERIFY_DELAYS, start=1):
        _transfer_sleep(delay)
        receipt["verification_reads"] = attempt
        try:
            now = _transfer_resolution(source, number, reader, cwd, environment)
            if now["node_id"] != moved["id"] or now["number"] != moved["number"] or not (
                _same_repository(now["repository"], destination["repository"])
            ):
                raise ValueError("the source URL does not resolve to the returned issue")
            return _transfer_issue_labels(
                destination["repository"], moved["number"], moved["id"], reader, cwd, environment, temp
            )
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ValueError) and str(exc) else "the transfer could not be verified"
    raise ValueError(detail)


def _transfer_result(receipt, code, capture_output, text):
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if code == TRANSFER_UNSETTLED:
        with contextlib.suppress(OSError, ValueError):
            print(
                f"TRANSFER_UNSETTLED: {receipt['detail'] or 'the receipt is incomplete'}; a mutation was sent, so do "
                "not retry. Read the state only with --dry-run, which sends no mutation: an already transferred issue "
                "exits 0 as already-transferred.",
                file=sys.stderr,
            )
    return subprocess.CompletedProcess(
        ["github", "issue-transfer"], code,
        (encoded if text else encoded.encode("utf-8")) if capture_output else None,
        ("" if text else b"") if capture_output else None,
    )


def _issue_transfer(fields, *, runner, cwd, environment, capture_output, text):
    """Transfer one open issue, or refuse before the mutation; leave a private receipt after the static checks."""
    source = _transfer_endpoint(fields["repo"], environment)
    destination = _transfer_endpoint(fields["destination"], environment)
    if source["owner"] != destination["owner"]:
        raise _transfer_refused("GitHub transfers issues only between repositories of the same owner")
    if source["repository"] == destination["repository"]:
        raise _transfer_refused("the destination is the source repository")
    if source["privacy"] == "private" and destination["privacy"] != "private":
        raise _transfer_refused("an issue in a private-role repository never moves to a public-role repository")
    # The result checks bind both endpoints through the fleet registry; prove it binds them
    # now, before the mutation, so an unreadable registry cannot strand a sent transfer.
    if not _repository_slugs(source["repository"]) or not _repository_slugs(destination["repository"]):
        raise _transfer_refused("the fleet registry cannot bind the source or destination repository")
    number = fields["number"]
    environment["GH_HOST"] = "github.com"
    receipt = {
        "schema": "issue-transfer-receipt/v1",
        "outcome": None,
        "mutations_sent": 0,
        "dry_run": bool(fields.get("dry_run")),
        "source": {"alias": source["alias"], "repository": source["repository"], "number": number,
                   "url": f"https://github.com/{source['repository']}/issues/{number}", "node_id": None},
        "destination": {"alias": destination["alias"], "repository": destination["repository"],
                        "number": None, "url": None, "node_id": None},
        "labels": {"source": None, "destination_issue": None, "dropped": None, "predicted_dropped": None},
        "probes": [],
        "probe_note": TRANSFER_PROBE_NOTE,
        "verification_reads": 0,
        "detail": None,
    }
    sink = {"fd": None, "path": None}
    try:
        with tempfile.TemporaryDirectory(
            prefix="lu-transfer-", dir=ensure_scratch_root(), ignore_cleanup_errors=True
        ) as directory:
            sink.update(_reserve_transfer_receipt(fields.get("receipt_dir"), source, number, environment))
            return _transfer_checked(
                fields, source, destination, receipt, sink, Path(directory),
                runner=runner, cwd=cwd, environment=environment, capture_output=capture_output, text=text,
            )
    except Exception:
        if not receipt["mutations_sent"]:
            raise
        # Every local failure after the mutation (receipt output, stdout, cleanup) stays unsettled.
        if receipt["outcome"] in {None, "transferred"}:
            receipt["outcome"] = "unverified"
            receipt["detail"] = "a local step failed after the mutation was sent"
        _write_transfer_receipt(sink, receipt)
        return _transfer_result(receipt, TRANSFER_UNSETTLED, capture_output, text)
    finally:
        if sink["fd"] is not None:
            os.close(sink["fd"])


def _transfer_checked(fields, source, destination, receipt, sink, temp, *, runner, cwd, environment,
                      capture_output, text):
    """The fresh checks, the one mutation and its verification, with the receipt reserved."""
    reader = merge_readiness_runner(runner)
    number = fields["number"]

    def settle(outcome, code, detail=None):
        receipt["outcome"], receipt["detail"] = outcome, detail
        if outcome in TRANSFER_PROBED and not receipt["dry_run"]:
            urls = [receipt["source"]["url"], receipt["destination"]["url"]]
            receipt["probes"] = [_anonymous_probe(url) for url in urls if url]
            if receipt["mutations_sent"] and any(_probe_failed(probe) for probe in receipt["probes"]):
                code = TRANSFER_UNSETTLED
                if outcome == "transferred":
                    receipt["outcome"], receipt["detail"] = "unverified", "an anonymous probe failed"
        if not _write_transfer_receipt(sink, receipt):
            print("TRANSFER_RECEIPT_UNWRITTEN: the receipt is on stdout only.", file=sys.stderr)
            code = code or (TRANSFER_UNSETTLED if receipt["mutations_sent"] else 2)
        print(f"issue-transfer: receipt {sink['path']}", file=sys.stderr)
        result = _transfer_result(receipt, code, capture_output, text)
        if not capture_output:
            print(json.dumps(receipt, indent=2, sort_keys=True))
        return result

    try:
        try:
            repositories = {
                side: _transfer_repository(endpoint, reader, cwd, environment, temp)
                for side, endpoint in (("source", source), ("destination", destination))
            }
        except (gate.PublishBlocked, github_client.GitHubRateLimited):
            raise
        except Exception:
            raise _transfer_refused("a repository read was unreadable, malformed or unknown") from None
        for side, endpoint in (("source", source), ("destination", destination)):
            live = repositories[side]
            if not _same_repository(live["nameWithOwner"], endpoint["repository"]):
                raise _transfer_refused(f"the {side} repository's live identity differs from the registry")
            if live["visibility"] != TRANSFER_VISIBILITY[endpoint["privacy"]]:
                raise _transfer_refused(f"the {side} repository's live visibility differs from its registry role")
            if live["viewerPermission"] not in TRANSFER_WRITE_PERMISSIONS:
                raise _transfer_refused(f"this login cannot write to the {side} repository")
            if live["isArchived"] is not False:
                raise _transfer_refused(f"the {side} repository is archived or its state is unknown")
        try:
            resolved = _transfer_resolution(source, number, reader, cwd, environment)
        except (gate.PublishBlocked, github_client.GitHubRateLimited):
            raise
        except Exception:
            raise _transfer_refused("the source issue is missing, unreadable or malformed") from None
        if _same_repository(resolved["repository"], destination["repository"]) and not resolved["pull_request"]:
            # Already moved: bind the redirect to the destination's own record of that issue.
            try:
                bound = _transfer_resolution(destination, resolved["number"], reader, cwd, environment)
                if bound["node_id"] != resolved["node_id"] or bound["number"] != resolved["number"] or not (
                    _same_repository(bound["repository"], destination["repository"])
                ):
                    raise _transfer_refused("the redirect does not match the destination's issue")
                after = _transfer_issue_labels(
                    destination["repository"], bound["number"], bound["node_id"], reader, cwd, environment, temp
                )
            except (gate.PublishBlocked, github_client.GitHubRateLimited):
                raise
            except Exception:
                raise _transfer_refused("the transferred issue's destination record is unreadable") from None
            receipt["destination"].update(number=bound["number"], url=bound["url"], node_id=bound["node_id"])
            receipt["labels"]["destination_issue"] = after
            return settle("already-transferred", 0, "source labels are no longer readable; drops unknown")
        if not _same_repository(resolved["repository"], source["repository"]) or resolved["number"] != number:
            raise _transfer_refused("the source issue redirects to a repository other than the destination")
        if resolved["pull_request"]:
            raise _transfer_refused("the source number is a pull request")
        if resolved["state"] != "open":
            raise _transfer_refused("the source issue is closed")
        receipt["source"]["node_id"] = resolved["node_id"]
        try:
            labels = _transfer_issue_labels(
                source["repository"], number, resolved["node_id"], reader, cwd, environment, temp
            )
        except (gate.PublishBlocked, github_client.GitHubRateLimited):
            raise
        except Exception:
            raise _transfer_refused("the source issue's labels are unreadable or incomplete") from None
        receipt["labels"]["source"] = labels
        receipt["labels"]["predicted_dropped"] = _missing_labels(labels, repositories["destination"]["labels"])
        if receipt["dry_run"]:
            return settle("dry-run", 0, "every check passed; no mutation was sent")
    except gate.PublishBlocked as exc:
        settle("refused", 2, str(exc))
        raise
    except github_client.GitHubRateLimited:
        settle("refused", 2, "a fresh read was refused by the GitHub rate limit")
        raise
    except Exception:
        settle("refused", 2, "an unexpected error stopped the checks before any mutation")
        raise

    frozen = temp / "request.json"
    variables = {"issue": resolved["node_id"], "repository": repositories["destination"]["id"]}
    frozen.write_bytes(json.dumps({"query": TRANSFER_MUTATION, "variables": variables}).encode("utf-8"))
    receipt["mutations_sent"] = 1
    try:
        result = _send(
            ["gh", "api", "--method", "POST", "graphql", "--input", str(frozen)],
            environment=environment, runner=runner, cwd=cwd, capture_output=True, text=True, check=False,
            timeout=60,
        )
        if result.returncode != 0:
            raise ValueError
        document = github_client.parse_graphql_envelope(result.stdout)
        moved = document["data"]["transferIssue"]["issue"]
        if not isinstance(moved, dict):
            raise ValueError
    except Exception:
        return settle("ambiguous", TRANSFER_UNSETTLED, "the transfer mutation's result is unknown")
    try:
        moved_number = moved["number"]
        if (
            type(moved_number) is not int or moved_number <= 0
            or not isinstance(moved["id"], str) or not re.fullmatch(PATTERNS["node"], moved["id"])
            or not _same_repository(moved["repository"]["nameWithOwner"], destination["repository"])
            or not _same_repository_url(moved["url"], destination["repository"], f"issues/{moved_number}")
        ):
            raise ValueError("the mutation returned another issue or repository")
        # Only a validated URL is recorded, so only a validated URL is probed.
        receipt["destination"].update(number=moved_number, url=moved["url"], node_id=moved["id"])
        after = _transfer_verify(source, destination, number, moved, receipt, reader, cwd, environment, temp)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) and str(exc) else "the transfer could not be verified"
        return settle("unverified", TRANSFER_UNSETTLED, detail)
    receipt["labels"]["destination_issue"] = after
    receipt["labels"]["dropped"] = _missing_labels(labels, after)
    return settle("transferred", 0)


# --- repo-create and repo-transfer (GH-ADMIN-VERBS-1124) -------------------------------------
# The move of a dual-slug registry row to its alternate organization, and its rehearsal
# on one private fixture. No protected executor holds a GitHub principal that workers cannot
# read (docs/runbooks/write-worker-boundary.md), so these verbs run only from an operator-run
# script: worker markers and every caller credential override are refused, and the principal
# the operator names is verified through the same gh credential before and after the mutation.
# Static and fresh checks come first, then exactly one mutation; once it is sent, every
# failure exits REPO_ADMIN_UNSETTLED and a transfer journal refuses a second send. Only GitHub's
# own client-error answer to that POST (``_repo_admin_refusal``) proves nothing changed: exit 2.

REPO_ADMIN_UNSETTLED = 3
REPO_ADMIN_VERBS = frozenset({"repo-create", "repo-transfer"})
# gh reads its credential from these; a caller value would replace the operator's own login.
REPO_ADMIN_CREDENTIAL_OVERRIDES = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR", "XDG_CONFIG_HOME",
)
REPO_ADMIN_DIRECTORY = "repo-admin"
_GITHUB_REPOSITORY_NAME_LIMIT = 100
_REHEARSAL_SUFFIX = "-moved"
_DESTINATION_OWNER_PRECONDITION = (
    "Operator precondition: run as the destination organization owner with a stored gh token that can read\n"
    "  every repository in that organization. The destination lookup checks for visible collisions only.\n"
    "  Do not change the gh login while this command runs.\n"
)


def _account_home():
    """The running account's home directory from the password database, never ``$HOME``."""
    import pwd

    try:
        home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        raise gate.PublishBlocked("REPO_ADMIN_REFUSED: the account has no home directory; nothing was sent.") from None
    if not os.path.isabs(home):
        raise gate.PublishBlocked("REPO_ADMIN_REFUSED: the account's home directory is not absolute; nothing was sent.")
    return home


def _repo_admin_directory():
    """``~/.local/state/learn-ukrainian/repo-admin`` of the running account, from the password database.

    Not ``$HOME`` or ``$XDG_STATE_HOME``: every run of the same account must find the same journal,
    whatever its environment says, or a changed variable would hide an unsettled transfer.
    """
    return os.path.join(_account_home(), ".local", "state", "learn-ukrainian", REPO_ADMIN_DIRECTORY)
REPO_CREATE_SCHEMA = "repo-create-receipt/v1"
REPO_TRANSFER_SCHEMA = "repo-transfer-receipt/v1"
_REPO_ADMIN_RECEIPT_BYTES = 64 * 1024


def _repo_refused(verb, reason):
    action = "created" if verb == "repo-create" else "transferred"
    return gate.PublishBlocked(f"{verb.upper().replace('-', '_')}_REFUSED: {reason}; nothing was {action}.")


def _same_slug(left, right):
    """Exact owner/name equality without regard to ASCII case (GitHub slugs are case-insensitive)."""
    return (
        isinstance(left, str) and isinstance(right, str) and left.isascii() and right.isascii()
        and left.casefold() == right.casefold()
    )


def _repo_admin_authority(verb, environment):
    """Refuse a dispatched worker and every caller credential override before any read."""
    if cto_safe_mode.is_worker(os.environ) or cto_safe_mode.is_worker(environment):
        raise _repo_refused(verb, "dispatched workers cannot run it; it runs only from an operator-run script")
    # The environment the gh transport receives is the one that selects its credential.
    overrides = sorted(name for name in REPO_ADMIN_CREDENTIAL_OVERRIDES if name in environment)
    if overrides:
        raise _repo_refused(
            verb, f"credential overrides are refused ({', '.join(overrides)}); use the operator's own gh login"
        )
    if environment.get("GH_HOST", "github.com") != "github.com":
        raise _repo_refused(verb, "only github.com is supported")
    environment["GH_HOST"] = "github.com"
    # With GH_CONFIG_DIR and XDG_CONFIG_HOME refused, gh finds its stored login under the transport's HOME,
    # and the shared client keys its rate-limit budget on that login through this process's home. Both must
    # be the account's own home (the journal's too), so a caller HOME selects no other login or budget.
    home = _account_home()
    if environment.get("HOME") != home or os.environ.get("HOME") != home:
        raise _repo_refused(verb, f"HOME must be exactly the account's home directory {home}, where gh keeps its login")


def _repo_admin_row(verb, key):
    """The dual-slug registry row *key*: its two organizations and its pinned identity.

    ``repo-create`` remains restricted to private rows (it creates private rehearsal fixtures);
    ``repo-transfer`` admits registered public or private dual-slug rows whose declared
    ``visibility`` matches their ``role`` prefix.
    """
    repos = _catalog_fleet_repos()
    row = repos.get(key)
    if row is None:
        raise _repo_refused(verb, f"{key} is not a repository key in {_FLEET_REPOS}")
    if not row.has_alternate_slug or row.repository_id is None:
        raise _repo_refused(verb, f"the registry row {key} names no alternate_github and repository_id")
    if verb == "repo-create":
        if row.visibility != "private" or not row.role.startswith("private-"):
            raise _repo_refused(verb, f"the registry row {key} is not a private repository")
    elif row.visibility not in {"public", "private"} or not row.role.startswith(f"{row.visibility}-"):
        raise _repo_refused(
            verb, f"the registry row {key} must declare public or private visibility matching its role",
        )
    if _same_slug(row.owner, row.alternate_github.split("/", 1)[0]):
        raise _repo_refused(verb, f"the registry row {key} names one organization twice")
    return repos, row


def _registered_names(repos):
    """Every baked slug and every repository name of the registry, case-folded."""
    slugs = {slug.casefold() for repo in repos.values() for slug in repo.slugs}
    return slugs, {slug.split("/", 1)[1] for slug in slugs}


def _repo_admin_get(reader, endpoint, cwd, environment):
    """The JSON object GitHub returns for *endpoint*, None for a 404; any other answer raises ValueError."""
    result = reader(
        ["gh", "api", "--method", "GET", endpoint],
        cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode:
        if is_not_found_result(result):
            return None
        raise ValueError(f"the read of {endpoint} failed")
    document = json_depth.loads(result.stdout, max_depth=64)
    if not isinstance(document, dict):
        raise ValueError(f"the read of {endpoint} is malformed")
    return document


def _repo_admin_whoami(reader, cwd, environment):
    """The login and id the gh credential authenticates as (GET /user through the mutation's transport)."""
    document = _repo_admin_get(reader, "user", cwd, environment)
    login, user_id = (document or {}).get("login"), (document or {}).get("id")
    if not isinstance(login, str) or not re.fullmatch(PATTERNS["login"], login) or type(user_id) is not int or user_id <= 0:
        raise ValueError("the authenticated user is unreadable")
    return {"login": login, "id": user_id}


def _repo_admin_principal(verb, principal, reader, cwd, environment):
    """The authenticated principal; refuses when it is not the one the operator named."""
    identity = _repo_admin_whoami(reader, cwd, environment)
    if not _same_slug(identity["login"], principal):
        raise _repo_refused(
            verb, f"the gh credential authenticates as {identity['login']}, not the named principal {principal}",
        )
    return identity


def _repo_admin_owner(verb, organization, reader, cwd, environment):
    """Refuse unless GitHub reports an active admin membership for *organization*."""
    membership = _repo_admin_get(reader, f"user/memberships/orgs/{organization}", cwd, environment)
    if (
        membership is None or membership.get("state") != "active" or membership.get("role") != "admin"
        or not _same_slug((membership.get("organization") or {}).get("login"), organization)
    ):
        raise _repo_refused(verb, f"the principal is not an active admin of {organization}")
    return {"organization": organization, "state": "active", "role": "admin"}


def _repo_identity(document):
    """The identity fields of a repository document; malformed fields raise ValueError."""
    identity = {key: document.get(key) for key in ("id", "full_name", "private", "visibility", "archived")}
    if (
        type(identity["id"]) is not int or identity["id"] <= 0 or not isinstance(identity["full_name"], str)
        or type(identity["private"]) is not bool or not isinstance(identity["visibility"], str)
        or type(identity["archived"]) is not bool
    ):
        raise ValueError("the repository document is malformed")
    return identity


def _repo_matches(document, slug, repository_id, visibility="private"):
    """Whether a repository response proves the expected slug, id and visibility."""
    if visibility not in {"public", "private"}:
        return False
    return (
        _same_slug(document.get("full_name"), slug)
        and type(document.get("id")) is int and document["id"] == repository_id
        and document.get("private") is (visibility == "private")
        and document.get("visibility") == visibility
    )


def _repo_transfer_acknowledged(document, source, destination, repository_id, visibility):
    """Whether a POST /repos/{source}/transfer 202 answer names the pinned repository with *visibility*.

    GitHub's transfer endpoint returns 202 Accepted with ``minimal-repository``, where ``id``,
    ``full_name`` and ``private`` are required and ``visibility`` is optional; the owner transfer
    and ``new_name`` rename continue asynchronously, so ``full_name`` may hold either endpoint's
    owner paired with either endpoint's name. Exact destination slug and ``visibility``
    are verified afterwards by the bounded ``GET /repos/{destination}`` readback loop.
    """
    if not isinstance(document, dict) or visibility not in {"public", "private"}:
        return False
    source_owner, source_name = source.split("/", 1)
    dest_owner, dest_name = destination.split("/", 1)
    slugs = (source, destination, f"{source_owner}/{dest_name}", f"{dest_owner}/{source_name}")
    if "visibility" in document and document["visibility"] != visibility:
        return False
    return (
        any(_same_slug(document.get("full_name"), slug) for slug in slugs)
        and type(document.get("id")) is int and document["id"] == repository_id
        and document.get("private") is (visibility == "private")
    )


_REPO_ADMIN_RUN: contextvars.ContextVar[dict[str, bool]] = contextvars.ContextVar("repo_admin_run")


@contextlib.contextmanager
def _repo_admin_run():
    state = {"sent": False, "failed": False}
    token = _REPO_ADMIN_RUN.set(state)
    try:
        yield state
    finally:
        _REPO_ADMIN_RUN.reset(token)


def _stream_write(name, text):
    """Write and flush to the caller's stream, recording a failure in this publication."""
    try:
        stream = getattr(sys, name)
        written = stream.write(text)
        stream.flush()
        if type(written) is int and written == len(text):
            return True
    except Exception:
        pass
    _REPO_ADMIN_RUN.get()["failed"] = True
    return False


def _stderr_note(message):
    """One stderr line; a failed write is recorded for the exit code, never raised."""
    return _stream_write("stderr", message + "\n")


def _repo_admin_limited(verb, exc, capture_output, text):
    """A rate limit before any mutation: exit 75 with the shared client's typed refusal on stdout and stderr.

    It is written here through the guarded writer, not by the shared ``rate_limited_command`` wrapper,
    whose ``stream.buffer`` write fails on a stream without one and would turn 75 into 2.
    """
    limited = github_client._result_process(
        ["github", verb],
        github_client.Result(
            error=exc.code, reset_at=exc.reset_at, status=429, reason=exc.reason, created_pr=exc.created_pr,
        ),
        {}, {"capture_output": True, "text": text or not capture_output},
    )
    if not capture_output:
        _stream_write("stdout", limited.stdout)
        _stream_write("stderr", limited.stderr)
        limited.stdout = limited.stderr = None
    return limited


def _repo_admin_result(verb, receipt, code, capture_output, text):
    """Send the receipt and record whether this run sent a mutation."""
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    state = _REPO_ADMIN_RUN.get()
    state["sent"] = bool(receipt["mutations_sent"])
    if not capture_output and not _stream_write("stdout", encoded):
        _stderr_note(f"{verb.upper().replace('-', '_')}_STDOUT_UNWRITTEN: the receipt is only in its file.")
    if code == REPO_ADMIN_UNSETTLED or (state["sent"] and state["failed"]):
        _stderr_note(
            f"{verb.upper().replace('-', '_')}_UNSETTLED: {receipt['detail'] or 'the result is unknown'}; "
            "do not retry. Reconcile the state on GitHub first (see --help)."
        )
    return subprocess.CompletedProcess(
        ["github", verb], code,
        (encoded if text else encoded.encode("utf-8")) if capture_output else None,
        ("" if text else b"") if capture_output else None,
    )


def _repo_admin_settle(verb, receipt, sink, code, outcome, detail, capture_output, text):
    receipt["outcome"], receipt["detail"] = outcome, detail
    if not _write_transfer_receipt(sink, receipt):
        _stderr_note(f"{verb.upper().replace('-', '_')}_RECEIPT_UNWRITTEN: the receipt is on stdout only.")
        if receipt["mutations_sent"]:
            code = REPO_ADMIN_UNSETTLED
    _stderr_note(f"{verb}: receipt {sink['path']}")
    return _repo_admin_result(verb, receipt, code, capture_output, text)


def _repo_admin_journal_possible(repository_id):
    """Whether a transfer journal for *repository_id* exists or its state cannot be read (fail closed)."""
    try:
        os.lstat(os.path.join(_repo_admin_directory(), f"repo-transfer-{repository_id}.pending"))
    except FileNotFoundError:
        return False
    except Exception:
        return True
    return True


def _repo_admin_lock(directory_fd, name):
    """An exclusive flock on *name* in the private directory, held until its descriptor closes; None when held elsewhere."""
    import fcntl

    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _repo_admin(verb, fields, *, runner, cwd, environment, capture_output, text):
    """Run repo-create or repo-transfer: static refusals, a private receipt and lock, then the checked verb.

    One lock per fixture name or repository id is held from the first read to settlement, so a
    second run never acts on reads taken before the first run's mutation.
    """
    _repo_admin_authority(verb, environment)
    repos, row = _repo_admin_row(verb, fields["repo"])
    plan = (_repo_create_plan if verb == "repo-create" else _repo_transfer_plan)(verb, fields, repos, row, environment)
    receipt = {
        "schema": REPO_CREATE_SCHEMA if verb == "repo-create" else REPO_TRANSFER_SCHEMA,
        "outcome": None,
        "mutations_sent": 0,
        "dry_run": bool(fields.get("dry_run")),
        "registry_row": row.key,
        "principal": {"named": fields["principal"], "before": None, "after": None},
        **plan["receipt"],
        "detail": None,
    }
    sink = {"fd": None, "path": None}
    held = {"directory": None, "lock": None}
    try:
        state = _repo_admin_directory()
        sink.update(_reserve_receipt(state, REPO_ADMIN_DIRECTORY, plan["stem"], environment, plan["refused"]))
        held["directory"], directory = _private_directory(state, REPO_ADMIN_DIRECTORY, environment, plan["refused"])
        try:
            held["lock"] = _repo_admin_lock(held["directory"], f"{plan['stem']}.lock")
        except OSError:
            raise plan["refused"]("the run lock cannot be opened") from None
        if held["lock"] is None:
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "in-progress",
                f"another {verb} run for {plan['stem']} holds its lock and may be sending; nothing was sent by this "
                "run. Wait for it to finish, then rerun to reconcile",
                capture_output, text,
            )
        return plan["run"](
            fields, plan, receipt, sink, held["directory"], directory, runner=runner, cwd=cwd,
            environment=environment, capture_output=capture_output, text=text,
        )
    except Exception as exc:
        if not receipt["mutations_sent"] and verb == "repo-transfer" and not isinstance(
            exc, github_client.GitHubRateLimited
        ) and _repo_admin_journal_possible(plan["repository_id"]):
            # The receipt, directory or lock step failed while a journal for this id exists or cannot be read.
            reason = str(exc) if isinstance(exc, gate.PublishBlocked) else "a local step failed before any read"
            receipt["outcome"], receipt["detail"] = "unreconciled", (
                f"a transfer journal for repository {plan['repository_id']} exists or cannot be read, and this run "
                f"could not check it ({reason}); no mutation was sent by this run. Check on GitHub, then fix the "
                "local state before a new attempt"
            )
            if sink["fd"] is not None:
                _write_transfer_receipt(sink, receipt)
            return _repo_admin_result(verb, receipt, REPO_ADMIN_UNSETTLED, capture_output, text)
        if not receipt["mutations_sent"]:
            if sink["fd"] is not None:
                # A refusal outside the checks still leaves a complete receipt, never an empty file.
                receipt["outcome"] = "refused"
                receipt["detail"] = str(exc) if isinstance(exc, gate.PublishBlocked) else (
                    "an unexpected error stopped the checks before any mutation"
                )
                _write_transfer_receipt(sink, receipt)
            if isinstance(exc, github_client.GitHubRateLimited):
                return _repo_admin_limited(verb, exc, capture_output, text)
            raise
        if receipt["outcome"] in {None, "created", "transferred"}:
            receipt["outcome"], receipt["detail"] = "unverified", "a local step failed after the mutation was sent"
        _write_transfer_receipt(sink, receipt)
        return _repo_admin_result(verb, receipt, REPO_ADMIN_UNSETTLED, capture_output, text)
    finally:
        for descriptor in (sink["fd"], held["lock"], held["directory"]):
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


def _repo_admin_checks(verb, receipt, sink, capture_output, text, checks, pending=None):
    """Run the pre-mutation *checks*; a refusal settles the receipt as refused and is raised.

    With a *pending* journal message, any failure instead settles as unreconciled (exit 3) and
    that result is returned: nothing short of a verified destination match settles a pending transfer.
    """
    try:
        checks()
        return None
    except Exception as exc:
        if pending is None:
            return _repo_admin_refuse_checks(verb, receipt, sink, exc)
        reason = str(exc) if isinstance(exc, gate.PublishBlocked) else (
            "a fresh read was refused by the GitHub rate limit" if isinstance(exc, github_client.GitHubRateLimited)
            else "a fresh read was unreadable, malformed or unknown"
        )
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unreconciled", f"{pending} ({reason})", capture_output, text,
        )


def _repo_admin_refuse_checks(verb, receipt, sink, exc):
    """Settle a failed check as refused and raise its typed refusal (exit 2, or 75 for a rate limit)."""
    if isinstance(exc, gate.PublishBlocked):
        _repo_admin_settle(verb, receipt, sink, 2, "refused", str(exc), True, True)
        raise exc
    if isinstance(exc, github_client.GitHubRateLimited):
        _repo_admin_settle(verb, receipt, sink, 2, "refused", "a fresh read was refused by the GitHub rate limit", True, True)
        raise exc
    _repo_admin_settle(verb, receipt, sink, 2, "refused", "a fresh read was unreadable, malformed or unknown", True, True)
    raise _repo_refused(verb, "a fresh read was unreadable, malformed or unknown") from None


# GitHub's client errors for a request it validated and did not carry out (documented for the create and
# transfer endpoints, plus authentication, not-found and conflict answers). Every other status, among them
# 408, 413 (also this client's own response-size refusal) and 429, and 5xx, leaves the result unknown.
_REPO_ADMIN_REFUSAL_STATUSES = frozenset({400, 401, 403, 404, 409, 410, 422})
_REPO_ADMIN_MESSAGE_LIMIT = 500


class _RepoAdminRefused(Exception):
    """GitHub answered the one POST with a client-error refusal: the mutation was not carried out."""

    def __init__(self, status, message):
        self.status, self.message = status, message
        # json.dumps quotes GitHub's text and escapes control and non-ASCII characters for the terminal.
        super().__init__(f"GitHub refused the request with HTTP {status} {json.dumps(message)}")


def _repo_admin_refusal(result):
    """GitHub's typed refusal of a failed POST, else None (the result stays unknown).

    Only the shared client's own observation counts: an HTTP error answer whose status is a refusal
    status and whose JSON body names a non-empty message. A timeout, a transport failure, a rate limit,
    a 5xx or an answer without a readable message is not a refusal.
    """
    observation = getattr(result, "github_result", None)
    if not isinstance(observation, github_client.Result) or observation.error != "github_http_error":
        return None
    status, document = observation.status, observation.value
    if type(status) is not int or status not in _REPO_ADMIN_REFUSAL_STATUSES or not isinstance(document, dict):
        return None
    message = document.get("message")
    if not isinstance(message, str) or not message:
        return None
    return _RepoAdminRefused(status, message[:_REPO_ADMIN_MESSAGE_LIMIT])


def _repo_admin_refused_post(receipt, refused):
    """Record GitHub's refusal: the refused POST changed nothing, so it is not counted as a mutation."""
    receipt["mutations_sent"] = 0
    receipt["github_refusal"] = {"status": refused.status, "message": refused.message}


def _repo_admin_post_refusal(verb, receipt, sink, refused, capture_output, text):
    """Settle GitHub's refusal of the POST: exit 2, the receipt on stdout and in its file, the refusal on stderr."""
    _stderr_note(str(_repo_refused(verb, str(refused))))
    return _repo_admin_settle(verb, receipt, sink, 2, "refused", str(refused), capture_output, text)


def _repo_admin_post(endpoint, frozen, runner, cwd, environment):
    """Send one POST of the prepared *frozen* body; any failure or malformed answer raises.

    GitHub's refusal raises ``_RepoAdminRefused``; every other failure raises ValueError (result unknown).
    The caller prepares the body and counts the mutation as sent before calling.
    """
    result = _send(
        ["gh", "api", "--method", "POST", endpoint, "--input", str(frozen)],
        environment=environment, runner=runner, cwd=cwd, capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode:
        refusal = _repo_admin_refusal(result)
        if refusal is not None:
            raise refusal
        raise ValueError("the mutation failed or its result is unknown")
    document = json_depth.loads(result.stdout, max_depth=64)
    if not isinstance(document, dict):
        raise ValueError("the mutation's answer is malformed")
    return document


def _repo_admin_readback(
    slug, repository_id, principal_before, reader, cwd, environment, *,
    visibility="private", fail_on_visibility_mismatch=False,
):
    """Re-read *slug* within ``TRANSFER_VERIFY_DELAYS`` (8 reads over 180 s) until it matches, then re-read the principal.

    GitHub runs repository transfers asynchronously (202 Accepted), so the destination slug may not appear
    immediately after the POST. When *fail_on_visibility_mismatch* is True (transfer destination readback),
    observing the pinned *repository_id* at *slug* with mismatched visibility stops polling immediately so
    the caller exits unsettled and retains the journal; ``repo-create`` keeps bounded polling for the
    expected private match. Nothing here writes; every failure raises ValueError with its reason and
    the caller reports it as unsettled while keeping any transfer journal.
    """
    detail = "the readback did not show the expected repository"
    for delay in TRANSFER_VERIFY_DELAYS:
        _transfer_sleep(delay)
        try:
            document = _repo_admin_get(reader, f"repos/{slug}", cwd, environment)
        except Exception:
            detail = "the readback was unreadable or rate limited"
            continue
        try:
            identity = _repo_identity(document) if document is not None else None
        except ValueError:
            if (
                fail_on_visibility_mismatch
                and isinstance(document, dict)
                and type(document.get("id")) is int and document["id"] == repository_id
                and _same_slug(document.get("full_name"), slug)
                and (
                    (type(document.get("private")) is bool and document["private"] is not (visibility == "private"))
                    or (isinstance(document.get("visibility"), str) and document["visibility"] != visibility)
                )
            ):
                raise ValueError(
                    f"the readback found repository {repository_id} at {slug} with unexpected visibility "
                    f"(private={document.get('private')!r}, visibility={document.get('visibility')!r}); "
                    f"expected {visibility}"
                ) from None
            identity = None
        if identity is not None:
            if _repo_matches(identity, slug, repository_id, visibility):
                break
            if fail_on_visibility_mismatch and _same_slug(identity["full_name"], slug) and identity["id"] == repository_id:
                raise ValueError(
                    f"the readback found repository {repository_id} at {slug} with unexpected visibility "
                    f"(private={identity['private']}, visibility={identity['visibility']!r}); expected {visibility}"
                )
        detail = (
            "the readback found no repository at the expected slug" if document is None
            else "the readback found another repository, slug or visibility"
        )
    else:
        raise ValueError(f"{detail} after {len(TRANSFER_VERIFY_DELAYS)} reads over {sum(TRANSFER_VERIFY_DELAYS)} s")
    try:
        identity = _repo_admin_whoami(reader, cwd, environment)
    except Exception:
        raise ValueError("the principal could not be re-read after the mutation") from None
    if not _same_slug(identity["login"], principal_before["login"]):
        raise ValueError(
            f"the gh credential authenticated as {identity['login']} after the mutation, not {principal_before['login']}"
        )
    if identity["id"] != principal_before["id"]:
        raise ValueError("the authenticated principal id changed after the mutation")
    return identity


def _rehearsal_slugs(verb, row, name):
    """The fixture's slug in the row's organization, and its exact rehearsal destination.

    Like the move itself, the rehearsal changes both owner and name: ``<name>`` in the row's
    organization becomes ``<name>-moved`` in the alternate organization.
    """
    destination_name = name + _REHEARSAL_SUFFIX
    if len(destination_name) > _GITHUB_REPOSITORY_NAME_LIMIT:
        raise _repo_refused(verb, f"the rehearsal destination name exceeds {_GITHUB_REPOSITORY_NAME_LIMIT} characters")
    return f"{row.owner}/{name}", f"{row.alternate_github.split('/', 1)[0]}/{destination_name}"


def _repo_create_plan(verb, fields, repos, row, environment):
    """Derive the fixture's slug and its exact rehearsal destination from the registry row."""
    name = fields["name"]
    slugs, names = _registered_names(repos)
    source, destination = _rehearsal_slugs(verb, row, name)
    if {source.casefold(), destination.casefold()} & slugs or {
        slug.split("/", 1)[1].casefold() for slug in (source, destination)
    } & names:
        raise _repo_refused(verb, f"{name} or its rehearsal destination is the name of a registered repository")
    return {
        "stem": f"repo-create-{name}",
        "refused": lambda reason: _repo_refused(verb, reason),
        "source": source,
        "destination": destination,
        "receipt": {
            "repository": {"slug": source, "id": None, "private": None, "visibility": None},
            "rehearsal_destination": destination,
            "preflight": {},
        },
        "run": _repo_create_checked,
    }


def _repo_create_checked(fields, plan, receipt, sink, _directory_fd, _directory, *, runner, cwd, environment,
                         capture_output, text):
    verb, reader = "repo-create", merge_readiness_runner(runner)
    source, destination = plan["source"], plan["destination"]

    def checks():
        receipt["principal"]["before"] = _repo_admin_principal(verb, fields["principal"], reader, cwd, environment)
        receipt["preflight"]["source_organization"] = _repo_admin_owner(
            verb, source.split("/", 1)[0], reader, cwd, environment,
        )
        for side, slug in (("source", source), ("rehearsal destination", destination)):
            if _repo_admin_get(reader, f"repos/{slug}", cwd, environment) is not None:
                raise _repo_refused(verb, f"the {side} name {slug} is taken")
        receipt["preflight"]["no_visible_collisions"] = [source, destination]

    _repo_admin_checks(verb, receipt, sink, capture_output, text, checks)
    if receipt["dry_run"]:
        # The exit 0 rests on a principal read after the collision reads, as the created path's readback does.
        reason = _principal_reread(receipt["principal"]["before"], receipt, reader, cwd, environment)
        if reason:
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unverified",
                f"{reason}, so this run cannot confirm its checks under the named principal; no mutation was sent. "
                "Rerun under one unchanged gh login",
                capture_output, text,
            )
        return _repo_admin_settle(
            verb, receipt, sink, 0, "dry-run", "every check passed; no mutation was sent", capture_output, text,
        )
    with tempfile.TemporaryDirectory(prefix="lu-repo-create-", dir=ensure_scratch_root(), ignore_cleanup_errors=True) as temp:
        frozen = Path(temp) / "request.json"
        payload = json.dumps({"name": fields["name"], "private": True, "auto_init": True}).encode("utf-8")
        if frozen.write_bytes(payload) != len(payload):
            raise plan["refused"]("the creation request could not be written in full")
        receipt["mutations_sent"] = 1
        try:
            created = _repo_identity(_repo_admin_post(
                f"orgs/{source.split('/', 1)[0]}/repos", frozen, runner, cwd, environment,
            ))
        except _RepoAdminRefused as refused:
            # Nothing was created, and no journal exists: a refusal (exit 2) naming GitHub's status and message.
            _repo_admin_refused_post(receipt, refused)
            return _repo_admin_post_refusal(verb, receipt, sink, refused, capture_output, text)
        except Exception:
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "ambiguous", "the creation's result is unknown",
                capture_output, text,
            )
    receipt["repository"].update(id=created["id"])
    if not _repo_matches(created, source, created["id"]):
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unverified",
            f"GitHub answered with {created['full_name']} (private {created['private']}), not the private {source}",
            capture_output, text,
        )
    try:
        receipt["principal"]["after"] = _repo_admin_readback(
            source, created["id"], receipt["principal"]["before"], reader, cwd, environment,
        )
    except Exception as exc:
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unverified", str(exc) or "the readback failed",
            capture_output, text,
        )
    receipt["repository"].update(private=True, visibility="private")
    return _repo_admin_settle(verb, receipt, sink, 0, "created", None, capture_output, text)


def _load_create_receipt(verb, value, environment):
    """The created fixture's repo-create receipt, read without following links from the private directory."""
    refused = lambda reason: _repo_refused(verb, reason)  # noqa: E731
    path = Path(value)
    if not path.is_absolute():
        raise refused("--create-receipt must be an absolute path")
    directory_fd, real = _private_directory(_repo_admin_directory(), REPO_ADMIN_DIRECTORY, environment, refused)
    try:
        if os.path.realpath(path.parent) != str(real):
            raise refused(f"--create-receipt must name a receipt in {real}")
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except OSError:
            raise refused("the creation receipt cannot be opened") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise refused("the creation receipt must be a mode 0600 file owned by this user")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                raw = handle.read(_REPO_ADMIN_RECEIPT_BYTES + 1)
        except OSError:
            raise refused("the creation receipt cannot be read") from None
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(directory_fd)
    if len(raw) > _REPO_ADMIN_RECEIPT_BYTES:
        raise refused("the creation receipt is too large")
    try:
        document = json_depth.loads(raw.decode("utf-8"), max_depth=16)
    except (UnicodeError, ValueError, RecursionError):
        raise refused("the creation receipt is not JSON") from None
    if not isinstance(document, dict):
        raise refused("the creation receipt is not a JSON object")
    return document


def _repo_transfer_plan(verb, fields, repos, row, environment):
    """Source, destination and pinned id: from the selector-backed row, or from a fixture's creation receipt."""
    from lu_machinery.orchestration import fleet_repos

    selector = None
    if "create_receipt" in fields:
        if row.visibility != "private" or not row.role.startswith("private-"):
            raise _repo_refused(verb, f"rehearsal --create-receipt requires a private registry row, not {row.key}")
        created = _load_create_receipt(verb, fields["create_receipt"], environment)
        repository = created.get("repository") if isinstance(created.get("repository"), dict) else {}
        slug, repository_id = repository.get("slug"), repository.get("id")
        name = slug.split("/", 1)[1] if isinstance(slug, str) and slug.count("/") == 1 else ""
        slugs, names = _registered_names(repos)
        registered_ids = {repo.repository_id for repo in repos.values() if repo.repository_id is not None}
        if not re.fullmatch(PATTERNS["alias"], name):
            raise _repo_refused(verb, "the creation receipt names no fixture repository")
        source, destination = _rehearsal_slugs(verb, row, name)
        if (
            created.get("schema") != REPO_CREATE_SCHEMA or created.get("outcome") != "created"
            or created.get("dry_run") is not False or type(created.get("mutations_sent")) is not int
            or created.get("mutations_sent") != 1
            or created.get("registry_row") != row.key
            or type(repository_id) is not int or repository_id <= 0
            or not _same_slug(slug, source)
            or repository.get("private") is not True or repository.get("visibility") != "private"
            or not _same_slug(created.get("rehearsal_destination"), destination)
        ):
            raise _repo_refused(verb, "the creation receipt is not a created fixture of this registry row")
        if repository_id in registered_ids or {source.casefold(), destination.casefold()} & slugs or {
            slug.split("/", 1)[1].casefold() for slug in (source, destination)
        } & names:
            raise _repo_refused(verb, "the creation receipt names a registered repository")
        mode = "rehearsal"
    else:
        # Selector problems are judged after the destination and journal reads, so a pending transfer still settles.
        try:
            active = fleet_repos.resolve_active_fleet_repo(row.key, repos=repos, env=environment)
        except Exception as exc:
            # Any selector failure, the parser's RecursionError included, is judged after the pending journal.
            detail = str(exc) if isinstance(exc, fleet_repos.FleetRepoError) else type(exc).__name__
            selector = f"the fleet repository selector cannot bind {row.key}: {detail}"
        else:
            if not _same_slug(active.active_github, row.github):
                selector = (
                    f"the selector already names {active.active_github} for {row.key}, but repository "
                    f"{row.repository_id} is not at {row.alternate_github}; the move is not pending"
                )
        mode, source, destination, repository_id = "move", row.github, row.alternate_github, row.repository_id
    return {
        "selector_refusal": selector,
        "stem": f"repo-transfer-{repository_id}",
        "refused": lambda reason: _repo_refused(verb, reason),
        "source": source,
        "destination": destination,
        "repository_id": repository_id,
        "visibility": row.visibility,
        "receipt": {
            "mode": mode,
            "repository_id": repository_id,
            "source": source,
            "destination": destination,
            "visibility": row.visibility,
            "create_receipt": str(fields["create_receipt"]) if mode == "rehearsal" else None,
            "journal": None,
            "preflight": {},
        },
        "run": _repo_transfer_checked,
    }


def _journal_exists(directory_fd, name):
    """Whether the journal exists; a state that cannot be read counts as pending (fail closed)."""
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _journal_bindings(directory_fd, name):
    """Read the original principal and visibility. Each missing or invalid binding is None."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
    except OSError:
        return None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return None, None
        with os.fdopen(fd, "rb") as handle:
            fd = None
            raw = handle.read(_REPO_ADMIN_RECEIPT_BYTES + 1)
        if len(raw) > _REPO_ADMIN_RECEIPT_BYTES:
            return None, None
        document = json_depth.loads(raw.decode("utf-8"), max_depth=16)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, None
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(document, dict):
        return None, None
    visibility = document.get("visibility")
    if visibility not in ("public", "private"):
        visibility = None
    principal = document.get("principal")
    login, user_id = (principal.get("login"), principal.get("id")) if isinstance(principal, dict) else (None, None)
    if not isinstance(login, str) or not re.fullmatch(PATTERNS["login"], login) or type(user_id) is not int or user_id <= 0:
        return None, visibility
    return {"login": login, "id": user_id}, visibility


def _same_principal(bound, current):
    """Whether the *bound* principal (or None) is the *current* login and account id."""
    return (
        bound is not None and current is not None and _same_slug(bound["login"], current["login"])
        and bound["id"] == current["id"]
    )


def _principal_reread(bound, receipt, reader, cwd, environment):
    """Re-read GET /user after the run's last GitHub read; why it is not *bound* (login and id), or None.

    An exit 0 or a journal removal never rests on a principal read older than the run's last GitHub
    read; the post-send readbacks keep the same order in ``_repo_admin_readback``.
    """
    try:
        current = _repo_admin_whoami(reader, cwd, environment)
    except Exception:
        receipt["principal"]["after"] = None
        return "the principal could not be re-read after the last GitHub read"
    receipt["principal"]["after"] = current
    if not _same_principal(bound, current):
        return (
            f"the gh credential authenticated as {current['login']} (id {current['id']}) at the re-read after the "
            f"last GitHub read, not {bound['login']} (id {bound['id']})"
        )
    return None


def _repo_transfer_checked(fields, plan, receipt, sink, directory_fd, directory, *, runner, cwd, environment,
                           capture_output, text):
    """Reconcile from the exact destination first, then the fresh source checks, then one journaled transfer.

    Exit contract: 0 verified (or already at the destination, or a passed dry run), always after a principal
    read that follows the last GitHub read; 2 refused with no journal left, either before the POST or by
    GitHub's client-error answer to it (status and message in ``github_refusal``); 75 a rate limit
    before any POST with no journal; 3 everything else: a POST sent and unsettled, a pending journal not
    settled by the destination under the visibility, login and id it binds, a failed or mismatched final principal
    re-read before an exit 0 or a journal removal (the journal is kept), or a journal that could not be removed.
    """
    verb, reader = "repo-transfer", merge_readiness_runner(runner)
    source, destination, repository_id, visibility = (
        plan["source"], plan["destination"], plan["repository_id"], plan["visibility"]
    )
    destination_owner = destination.split("/", 1)[0]
    journal_name = f"repo-transfer-{repository_id}.pending"
    receipt["journal"] = str(directory / journal_name)
    # Read once under the lock: only the lock holder creates or removes the journal.
    pending = _journal_exists(directory_fd, journal_name)
    receipt["journal_pending"] = pending
    # The principal that started the pending transfer; only that login and id can settle it (exit 0) or clear it.
    started_by = None
    if pending:
        started_by, journal_visibility = _journal_bindings(directory_fd, journal_name)
        receipt["journal_principal"] = started_by
        receipt["journal_visibility"] = journal_visibility
        if journal_visibility != visibility:
            binding = (
                "has no readable visibility binding" if journal_visibility is None
                else f"binds {journal_visibility} visibility but the registry now expects {visibility}"
            )
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unreconciled",
                f"the transfer journal {receipt['journal']} {binding}. Check the repository on GitHub before "
                "resolving the discrepancy. The journal is kept and no mutation was sent by this run",
                capture_output, text,
            )
    unreconciled = (
        f"an earlier run left the transfer journal {receipt['journal']}, so a transfer of repository "
        f"{repository_id} may have been sent, and {destination} does not show it {visibility}. Check on GitHub that no "
        "transfer is pending, then remove the journal before a new attempt; no mutation was sent by this run"
    )
    state = {}

    def checks():
        receipt["principal"]["before"] = _repo_admin_principal(verb, fields["principal"], reader, cwd, environment)
        # The exact destination decides first: a pending transfer settles from it whatever the old slug shows.
        moved = _repo_admin_get(reader, f"repos/{destination}", cwd, environment)
        observed = _repo_identity(moved) if moved is not None else None
        receipt["preflight"]["destination_observed"] = observed and {
            key: observed[key] for key in ("id", "full_name", "private", "visibility")
        }
        if observed is not None and _repo_matches(observed, destination, repository_id, visibility):
            state["moved"] = True
            return
        if pending:
            state["unreconciled"] = True
            return
        if plan["selector_refusal"]:
            raise _repo_refused(verb, plan["selector_refusal"])
        if observed is not None:
            raise _repo_refused(verb, f"the destination name {destination} holds repository {observed['id']}")
        found = _repo_admin_get(reader, f"repos/{source}", cwd, environment)
        identity = _repo_identity(found) if found is not None else None
        if identity is None or not _same_slug(identity["full_name"], source):
            raise _repo_refused(verb, f"repository {repository_id} is neither at {source} nor at {destination}")
        if identity["id"] != repository_id:
            raise _repo_refused(verb, f"{source} is repository {identity['id']}, not the pinned {repository_id}")
        if identity["private"] is not (visibility == "private") or identity["visibility"] != visibility:
            raise _repo_refused(verb, f"{source} is not {visibility}")
        if identity["archived"]:
            raise _repo_refused(verb, f"{source} is archived")
        if (found.get("permissions") or {}).get("admin") is not True:
            raise _repo_refused(verb, f"the principal is not an admin of {source}")
        receipt["preflight"]["destination_organization"] = _repo_admin_owner(
            verb, destination_owner, reader, cwd, environment,
        )
        receipt["preflight"]["no_visible_collisions"] = [destination]

    settled = _repo_admin_checks(
        verb, receipt, sink, capture_output, text, checks, pending=unreconciled if pending else None,
    )
    if settled is not None:
        return settled
    if state.get("unreconciled"):
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unreconciled", unreconciled, capture_output, text,
        )
    if state.get("moved") or receipt["dry_run"]:
        # Both exit 0 with no POST: the principal is re-read after the last GitHub read and must be the bound
        # one (the journal's starter, or with no journal this run's preflight principal), as after a send.
        before = receipt["principal"]["before"]
        bound = started_by if pending else before
        if not _same_principal(bound, before):
            reason = f"this run authenticates as {before['login']} (id {before['id']})"
        else:
            reason = _principal_reread(bound, receipt, reader, cwd, environment)
        if reason and pending:
            started = (
                "has no readable principal binding" if started_by is None
                else f"was started by {started_by['login']} (id {started_by['id']})"
            )
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unreconciled",
                f"repository {repository_id} is at {destination}, {visibility}, but the transfer journal "
                f"{receipt['journal']} {started}, and {reason}. Only the principal that started the transfer "
                "settles it: check the account on GitHub, then rerun as that principal or remove the journal by "
                "hand; the journal is kept and no mutation was sent by this run",
                capture_output, text,
            )
        if reason:
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unverified",
                f"{reason}, so this run cannot confirm its checks under the named principal; no mutation was sent "
                "by this run. Rerun under one unchanged gh login",
                capture_output, text,
            )
    if state.get("moved"):
        detail = f"repository {repository_id} is at {destination}, {visibility}; no mutation was sent"
        if pending and receipt["dry_run"]:
            detail += f"; the dry run leaves the journal {receipt['journal']} for a plain run to clear"
        elif pending and not _repo_admin_unjournal(directory_fd, journal_name, receipt):
            detail += f"; the journal {receipt['journal']} could not be removed: remove it by hand"
        return _repo_admin_settle(verb, receipt, sink, 0, "already-transferred", detail, capture_output, text)
    if receipt["dry_run"]:
        return _repo_admin_settle(
            verb, receipt, sink, 0, "dry-run", "every check passed; no mutation was sent", capture_output, text,
        )
    # The request body is prepared before the journal, so a local failure here leaves nothing to reconcile.
    with tempfile.TemporaryDirectory(prefix="lu-repo-transfer-", dir=ensure_scratch_root(), ignore_cleanup_errors=True) as temp:
        frozen = Path(temp) / "request.json"
        payload = json.dumps({
            "new_owner": destination_owner, "new_name": destination.split("/", 1)[1],
        }).encode("utf-8")
        if frozen.write_bytes(payload) != len(payload):
            raise plan["refused"]("the transfer request could not be written in full")
        return _repo_transfer_send(
            plan, receipt, sink, directory_fd, journal_name, frozen, reader,
            runner=runner, cwd=cwd, environment=environment, capture_output=capture_output, text=text,
        )


def _repo_admin_unjournal(directory_fd, journal_name, receipt):
    """Remove the transfer journal; False (and ``journal_retained``) when it exists but cannot be removed."""
    try:
        os.unlink(journal_name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    except OSError:
        receipt["journal_retained"] = True
        return False
    receipt["journal_reconciled"] = True
    return True


def _repo_transfer_send(plan, receipt, sink, directory_fd, journal_name, frozen, reader, *, runner, cwd,
                        environment, capture_output, text):
    """Create the journal, re-read the source, send the one transfer and verify it.

    A refusal before the POST removes the journal (exit 2, or 75 for a rate limit) only after the principal,
    re-read after the last GitHub read, is still the journal's starter; a failed or mismatched re-read, or a
    journal that cannot be removed, keeps it and exits 3. GitHub's client-error refusal of the POST is settled
    the same way; every other failure after the POST is unsettled (exit 3).
    """
    verb = "repo-transfer"
    source, destination, repository_id, visibility = (
        plan["source"], plan["destination"], plan["repository_id"], plan["visibility"]
    )

    def abandon(reason, limited=None, unsent="no POST was sent", refused=None):
        # Like every journal removal, this one rests on a principal read after the last GitHub read.
        reread = _principal_reread(receipt["principal"]["before"], receipt, reader, cwd, environment)
        if reread:
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unreconciled",
                f"{reason}; {unsent}, but {reread}, so the journal {receipt['journal']} is kept: check the "
                "account on GitHub, then remove the journal by hand before a new attempt",
                capture_output, text,
            )
        if not _repo_admin_unjournal(directory_fd, journal_name, receipt):
            return _repo_admin_settle(
                verb, receipt, sink, REPO_ADMIN_UNSETTLED, "journal-retained",
                f"{reason}; {unsent}, but the journal {receipt['journal']} could not be removed: remove it by "
                "hand before a new attempt",
                capture_output, text,
            )
        receipt.pop("journal_reconciled", None)
        if refused is not None:
            # GitHub's answer: the receipt is returned on the caller's stdout like every other settled POST.
            return _repo_admin_post_refusal(verb, receipt, sink, refused, capture_output, text)
        _repo_admin_settle(verb, receipt, sink, 2, "refused", reason, True, True)
        if limited is not None:
            raise limited
        raise _repo_refused(verb, reason)

    try:
        journal = os.open(
            journal_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd,
        )
    except OSError:
        # The lock holder saw no journal, so nothing of this run's exists to remove.
        _repo_admin_settle(verb, receipt, sink, 2, "refused", "the transfer journal cannot be created", True, True)
        raise _repo_refused(verb, "the transfer journal cannot be created") from None
    try:
        with os.fdopen(journal, "w", encoding="utf-8") as handle:
            # A later run needs the original visibility, login and account id to settle the journal.
            principal = receipt["principal"]["before"]
            entry = json.dumps({
                "receipt": str(sink["path"]), "source": source, "destination": destination,
                "principal": {"login": principal["login"], "id": principal["id"]},
                "visibility": visibility,
            }) + "\n"
            if handle.write(entry) != len(entry):
                raise OSError("short transfer journal write")
    except OSError:
        return abandon("the transfer journal cannot be written")
    # GitHub addresses the transfer by slug only: re-read it just before the POST to keep the window to one request.
    try:
        current = _repo_admin_get(reader, f"repos/{source}", cwd, environment)
        bound = current is not None and _repo_matches(_repo_identity(current), source, repository_id, visibility)
    except github_client.GitHubRateLimited as limited:
        return abandon("the source re-read before the transfer was refused by the GitHub rate limit", limited)
    except Exception:
        return abandon("the source re-read before the transfer was unreadable or malformed")
    if not bound:
        return abandon(f"{source} no longer holds the pinned {visibility} repository {repository_id}")
    receipt["mutations_sent"] = 1
    try:
        acknowledged = _repo_admin_post(f"repos/{source}/transfer", frozen, runner, cwd, environment)
    except _RepoAdminRefused as refused:
        # GitHub refused this POST, so it transferred nothing: the journal goes as for a refusal before the POST.
        _repo_admin_refused_post(receipt, refused)
        return abandon(str(refused), unsent="the refused POST transferred nothing", refused=refused)
    except Exception:
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "ambiguous", "the transfer's result is unknown",
            capture_output, text,
        )
    if not _repo_transfer_acknowledged(acknowledged, source, destination, repository_id, visibility):
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unverified",
            f"GitHub's answer does not name the pinned {visibility} repository {repository_id}", capture_output, text,
        )
    try:
        receipt["principal"]["after"] = _repo_admin_readback(
            destination, repository_id, receipt["principal"]["before"], reader, cwd, environment,
            visibility=visibility, fail_on_visibility_mismatch=True,
        )
    except Exception as exc:
        return _repo_admin_settle(
            verb, receipt, sink, REPO_ADMIN_UNSETTLED, "unverified", str(exc) or "the readback failed",
            capture_output, text,
        )
    detail = None
    if not _repo_admin_unjournal(directory_fd, journal_name, receipt):
        detail = f"verified; the journal {receipt['journal']} could not be removed: remove it by hand"
    receipt.pop("journal_reconciled", None)
    return _repo_admin_settle(verb, receipt, sink, 0, "transferred", detail, capture_output, text)


def _ruleset_get(runner, endpoint, cwd, environment):
    """Read one repository ruleset. A failed or unparsable read never continues into a write."""
    result = runner(
        ["gh", "api", "--method", "GET", endpoint],
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise gate.PublishBlocked("OPSEC: ruleset read failed.")
    try:
        document = json_depth.loads(result.stdout, max_depth=512)
    except (TypeError, json.JSONDecodeError):
        raise gate.PublishBlocked("OPSEC: ruleset read failed.") from None
    if not isinstance(document, dict):
        raise gate.PublishBlocked("OPSEC: ruleset read failed.")
    return document


def _merge_queue_rule(rules):
    """The single merge_queue rule, or a refusal when the ruleset has none or several."""
    if not isinstance(rules, list):
        raise gate.PublishBlocked("OPSEC: ruleset has no single merge_queue rule.")
    found = [index for index, rule in enumerate(rules) if isinstance(rule, dict) and rule.get("type") == "merge_queue"]
    if len(found) != 1:
        raise gate.PublishBlocked("OPSEC: ruleset has no single merge_queue rule.")
    rule = rules[found[0]]
    parameters = rule.get("parameters")
    if not isinstance(parameters, dict):
        raise gate.PublishBlocked("OPSEC: ruleset has no single merge_queue rule.")
    return found[0], parameters


def _ruleset_body(before, rules):
    """Writable ruleset fields only. An unrecognized key refuses, so it is not dropped on PUT."""
    unknown = set(before) - _RULESET_READONLY - _RULESET_WRITABLE
    if unknown or any(not isinstance(before.get(key), str) or not before[key] for key in ("name", "target", "enforcement")):
        raise gate.PublishBlocked("OPSEC: ruleset has a field this verb does not preserve.")
    if "conditions" not in before or "bypass_actors" not in before:
        raise gate.PublishBlocked("OPSEC: ruleset has a field this verb does not preserve.")
    body = {key: before[key] for key in ("name", "target", "enforcement", "bypass_actors", "conditions")}
    if before.get("bypass_mode") is not None:
        body["bypass_mode"] = before["bypass_mode"]
    body["rules"] = rules
    return body


def _ruleset_unchanged(before, after, original_parameters):
    """True when the read-back differs only by the merge_queue parameters already checked."""
    if any(before.get(key) != after.get(key) for key in ("name", "target", "enforcement", "conditions", "bypass_actors")):
        return False
    if ("bypass_mode" in before or "bypass_mode" in after) and before.get("bypass_mode") != after.get("bypass_mode"):
        return False
    restored = []
    seen = 0
    for rule in after.get("rules", []):
        if isinstance(rule, dict) and rule.get("type") == "merge_queue":
            seen += 1
            restored.append({**rule, "parameters": original_parameters})
        else:
            restored.append(rule)
    return seen == 1 and restored == before.get("rules")


def _publish_ruleset_merge_queue(
    fields, *, gh_repo, dest, runner, cwd, environment, temp, capture_output, text,
):
    """Replace named merge_queue parameters on an existing ruleset and require the read-back to match.

    PUT replaces the whole ruleset, so the body echoes GitHub's writable fields and edits
    only parameters that already exist on its single merge_queue rule. A read-back that
    does not match exits 3 and must not be retried: a retry would echo whatever the failed
    write left behind.
    """
    ruleset_id = fields["ruleset"]
    changes = {key: value for key, value in fields.items() if key in _MERGE_QUEUE_FIELDS}
    endpoint = f"repos/{gh_repo}/rulesets/{ruleset_id}"
    before = _ruleset_get(runner, endpoint, cwd, environment)
    if type(before.get("id")) is not int or before["id"] != ruleset_id:
        raise gate.PublishBlocked("OPSEC: ruleset read failed.")
    index, parameters = _merge_queue_rule(before.get("rules"))
    if any(key not in parameters for key in changes):
        raise gate.PublishBlocked("OPSEC: merge queue parameter is not on this ruleset.")
    updated = {**parameters, **changes}
    rules = [
        {**rule, "parameters": updated} if rule_index == index else rule
        for rule_index, rule in enumerate(before["rules"])
    ]
    body = _ruleset_body(before, rules)
    try:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise gate.PublishBlocked("OPSEC: ruleset cannot be republished.") from None
    try:
        gate.check_texts(dest, [raw.decode("utf-8")], environment=environment, field_names=["ruleset"])
    except gate.PublishBlocked:
        raise
    except Exception:
        raise gate.PublishBlocked("OPSEC: checker unavailable; write refused.") from None
    frozen = temp / "request.json"
    frozen.write_bytes(raw)
    put_argv = ["gh", "api", "--method", "PUT", endpoint, "--input", str(frozen)]
    unsent_or_lost = (
        "OPSEC: ruleset update unverified: the write may have been applied and its answer was lost. Do not retry."
    )
    # The nosemgrep markers below are on CompletedProcess constructors: each builds this
    # verb's result object and runs nothing.
    try:
        put = runner(
            put_argv,
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (github_client.GitHubRateLimited, gate.PublishBlocked):
        # A rate-limited write is not applied by GitHub (exit 75), and a publisher
        # refusal is raised before anything is sent (exit 2).
        raise
    except Exception:
        # A timeout or transport fault can follow a PUT that GitHub already applied.
        print(unsent_or_lost, file=sys.stderr)
        return subprocess.CompletedProcess(put_argv, 3, "", "")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    if put.returncode:
        status = getattr(getattr(put, "github_result", None), "status", None)
        if type(status) is int and 400 <= status < 500:
            print("OPSEC: ruleset update was refused by GitHub.", file=sys.stderr)
            return put
        # A server error, a lost connection or an unframed answer does not prove that
        # nothing landed.
        print(unsent_or_lost, file=sys.stderr)
        return subprocess.CompletedProcess(put_argv, 3, "", "")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    try:
        after = _ruleset_get(runner, endpoint, cwd, environment)
        after_index, after_parameters = _merge_queue_rule(after.get("rules"))
    except Exception:
        # After the PUT, every read-back failure is unverified, including a rate
        # limit. Letting that exception reach the command wrapper reports exit 75
        # or 2, as if the public write had not been sent.
        print(
            "OPSEC: ruleset update unverified: the write was sent and the read-back failed. Do not retry.",
            file=sys.stderr,
        )
        return subprocess.CompletedProcess(put_argv, 3, "", "")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    if (
        type(after.get("id")) is not int
        or after["id"] != ruleset_id
        or after_index != index
        or after_parameters != updated
        or not _ruleset_unchanged(before, after, parameters)
    ):
        print(
            "OPSEC: ruleset update unverified: the write was sent and the read-back does not match. Do not retry.",
            file=sys.stderr,
        )
        return subprocess.CompletedProcess(put_argv, 3, "", "")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    line = json.dumps(
        {"merge_queue": after_parameters, "repo": gh_repo, "ruleset": ruleset_id},
        ensure_ascii=False,
        sort_keys=True,
    )
    if not capture_output:
        print(line)
    return subprocess.CompletedProcess(put_argv, 0, line if text else line.encode(), "")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit


_DISMISS_TRIAGE_PATH = "docs/runbooks/secret-scanning.md"
_DISMISS_ANALYSIS_PER_PAGE = 100
_DISMISS_ANALYSIS_MAX_PAGES = 20
_DISMISS_ANALYSIS_CLOCK_SKEW = timedelta(minutes=5)
_DISMISS_PR_BATCH_SIZE = 100


def _parse_utc_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC)


def _refuse_dispatched_dismissal(environment):
    """This authority belongs only to interactive seats, including through the Python API."""
    for source in (os.environ, environment or {}):
        if (
            any(source.get(key) for key in (
                "LEARN_UKRAINIAN_DISPATCH_TASK_ID", BROKER_ENV,
            ))
            or source.get("USER") == "review" or source.get("LOGNAME") == "review"
        ):
            raise gate.PublishBlocked("DISMISSAL_BOUNDARY_REFUSED: interactive seats only; nothing was sent.")


def _dismiss_get(runner, endpoint, cwd, environment, *, pages=False, raw=False, sarif=False, raw_http=False):
    if raw_http:
        client = github_client.GitHubClient(env=gate.internal_environment(environment), cwd=cwd)
        headers = (
            {"Accept": "application/sarif+json" if sarif else "application/vnd.github.raw+json"}
            if (raw or sarif)
            else None
        )
        if not pages:
            observation = client.request(
                "GET", endpoint, headers=headers, timeout=30, fresh=True, raw_response=True,
            )
            if observation.stale:
                raise github_client.GitHubRateLimited(observation.reset_at, observation.reason)
            refusal = github_client.refusal_of(observation)
            if refusal is not None:
                raise refusal
            if (
                observation.error
                or not (200 <= observation.status < 300)
                or not isinstance(observation.value, (bytes, bytearray))
            ):
                raise ValueError("dismissal evidence unavailable")
            if raw:
                return bytes(observation.value).decode("utf-8")
            return github_client.loads_strict_json(observation.value)
        rows = []
        next_endpoint = endpoint
        for _ in range(100):
            observation = client.request(
                "GET", next_endpoint, headers=headers, timeout=30, fresh=True, raw_response=True,
            )
            if observation.stale:
                raise github_client.GitHubRateLimited(observation.reset_at, observation.reason)
            refusal = github_client.refusal_of(observation)
            if refusal is not None:
                raise refusal
            if (
                observation.error
                or not (200 <= observation.status < 300)
                or not isinstance(observation.value, (bytes, bytearray))
            ):
                raise ValueError("dismissal evidence unavailable")
            page = github_client.loads_strict_json(observation.value)
            if not isinstance(page, list):
                raise ValueError("dismissal pagination unavailable")
            rows.extend(page)
            next_endpoint = github_client.next_link(observation.headers)
            if not next_endpoint:
                return rows
        raise ValueError("dismissal pagination unavailable")
    argv = ["gh", "api", "--method", "GET", endpoint]
    if pages:
        argv += ["--paginate", "--slurp"]
    if raw or sarif:
        argv += ["--header", "Accept: " + ("application/sarif+json" if sarif else "application/vnd.github.raw+json")]
    result = runner(argv, cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=30)
    if result.returncode:
        raise ValueError("dismissal evidence unavailable")
    if raw:
        if isinstance(result.stdout, (bytes, bytearray)):
            return bytes(result.stdout).decode("utf-8")
        if not isinstance(result.stdout, str):
            raise ValueError("dismissal evidence unavailable")
        return result.stdout
    value = github_client.loads_strict_json(result.stdout)
    if pages:
        if not isinstance(value, list) or not value or any(not isinstance(page, list) for page in value):
            raise ValueError("dismissal pagination unavailable")
        value = [row for page in value for row in page]
    return value


def _dismiss_sarif_result(document, number):
    """Compare semantic evidence, resolving analysis-local artifact and rule indexes."""
    matches = []
    for run in document["runs"]:
        for result in run["results"]:
            if result.get("properties", {}).get("github/alertNumber") != number:
                continue
            driver = run["tool"]["driver"]
            reference = result.get("rule", {})
            component = reference.get("toolComponent", {})
            rule_component = run["tool"]["extensions"][component["index"]] if "index" in component else driver
            rule = rule_component["rules"][reference["index"] if "index" in reference else result["ruleIndex"]]
            if rule["id"] != result["ruleId"]:
                raise ValueError("unknown rule")

            def resolve(value, run=run):
                if isinstance(value, list):
                    return [resolve(item) for item in value]
                if not isinstance(value, dict):
                    return value
                if "artifactLocation" in value:
                    artifact = value["artifactLocation"]
                    uri = artifact.get("uri") or run["artifacts"][artifact["index"]]["location"]["uri"]
                    # Never follow URLs or paths from SARIF. They are evidence only.
                    value = {**value, "artifactLocation": {key: item for key, item in artifact.items() if key != "index"}}
                    value["artifactLocation"]["uri"] = uri
                return {key: resolve(item) for key, item in value.items()}

            evidence = resolve({key: value for key, value in result.items() if key not in {
                "rule", "ruleIndex", "correlationGuid", "partialFingerprints", "fingerprints",
            }})
            if not evidence.get("message") or not evidence.get("locations"):
                raise ValueError("unknown location or flow")
            matches.append({
                "result": evidence, "rule": rule,
                "tool": {key: driver.get(key) for key in ("name", "version", "semanticVersion")},
                "rule_component": {key: rule_component.get(key) for key in ("name", "version", "semanticVersion")},
            })
    if len(matches) != 1:
        raise ValueError("unknown or ambiguous alert evidence")
    return matches[0]


def _dismiss_preflight(fields, gh_repo, dest, runner, cwd, environment, *, temp=None, raw_graphql=False):
    from html.parser import HTMLParser

    from markdown_it import MarkdownIt

    from lu_machinery.orchestration.fleet_repos import parse_ascii_positive_int

    comment, number = fields["comment"], fields["number"]
    # Require an explanation in addition to an exact alert identifier and PR URL.
    identifier = rf"(?i)\balert\s+#{number}\b"
    if not re.search(identifier, comment):
        raise ValueError("comment must identify its alert")
    known = _repository_slugs(gh_repo)
    if not known:
        raise ValueError("the repository is not an exact owner/name or the fleet registry is unreadable")

    links: list[str] = []
    prose: list[str] = []
    https_scheme_case = str.maketrans("HTPS", "htps")

    def record_text(content: str, *, bare_links: bool) -> None:
        for word in content.split():
            start = word.translate(https_scheme_case).find("https://")
            if start == -1:
                prose.append(word)
                continue
            # URL text cannot explain the dismissal, even when it cannot bind a PR.
            prose.append(word[:start])
            if bare_links and (start == 0 or word[start - 1] in "([{\"'“‘*_~"):
                candidate = word[start:].partition("<")[0]
                links.append(candidate.rstrip(".,;:!?*)]}_\"'”’~…"))

    class AnchorParser(HTMLParser):
        in_anchor = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "a":
                self.in_anchor = True
                href = next((value for name, value in attrs if name == "href"), None)
                if href is not None:
                    links.append(href)

        def handle_endtag(self, tag: str) -> None:
            if tag == "a":
                self.in_anchor = False

        def handle_data(self, data: str) -> None:
            record_text(data, bare_links=False)

    html = AnchorParser()
    for block in MarkdownIt("commonmark").parse(comment):
        if block.type == "html_block":
            html.feed(block.content)
        elif block.type in {"code_block", "fence"}:
            record_text(block.content, bare_links=False)
        in_markdown_link = False
        for token in block.children or ():
            if token.type == "link_open":
                in_markdown_link = True
                href = token.attrGet("href")
                if isinstance(href, str):
                    links.append(href)
            elif token.type == "link_close":
                in_markdown_link = False
            elif token.type == "html_inline":
                html.feed(token.content)
            elif token.type in {"text", "code_inline"}:
                record_text(
                    token.content,
                    bare_links=token.type == "text" and not in_markdown_link and not html.in_anchor,
                )
    html.close()

    invalid_comment = "comment needs an explanation and one same-repository merged PR"
    github_prefix = "https://github.com/"
    prs: set[int] = set()
    for link in links:
        if not link.lower().startswith(github_prefix):
            continue
        path = link[len(github_prefix):].partition("#")[0].partition("?")[0]
        parts = path.split("/", 4)
        if len(parts) < 3 or "/".join(parts[:2]) not in known or parts[2] != "pull":
            continue
        segment = parts[3] if len(parts) >= 4 else ""
        pr_number = parse_ascii_positive_int(segment)
        if pr_number is None:
            raise ValueError(invalid_comment)
        if len(parts) == 4 or parts[4] in {"files", "commits"}:
            prs.add(pr_number)

    explanation = re.sub(identifier, "", " ".join(prose))
    if dest.partition("/")[0] != "github.com" or len(prs) != 1 or not any(char.isalpha() for char in explanation):
        raise ValueError(invalid_comment)
    root = f"repos/{gh_repo}"

    def get(suffix, **options):
        return _dismiss_get(runner, f"{root}/{suffix}", cwd, environment, raw_http=raw_graphql, **options)

    pr_number = prs.pop()
    pr = get(f"pulls/{pr_number}")
    merge = pr["merge_commit_sha"]
    if pr.get("merged") is not True or not re.fullmatch(PATTERNS["sha"], merge):
        raise ValueError("PR is not merged")
    merge_cutoff = (
        _parse_utc_timestamp(pr["merged_at"]) - _DISMISS_ANALYSIS_CLOCK_SKEW
        if "merged_at" in pr else None
    )
    if pr["html_url"] not in _repository_urls(gh_repo, f"pull/{pr_number}"):
        raise ValueError("PR identity changed")
    files = get(f"pulls/{pr_number}/files?per_page=100", pages=True)
    triage_files = [item for item in files if item.get("filename") == _DISMISS_TRIAGE_PATH and item.get("status") in {"added", "modified"}]
    if len(triage_files) != 1:
        raise ValueError("PR does not contain triage")
    triage = get(f"contents/{_DISMISS_TRIAGE_PATH}?ref={merge}", raw=True)
    rows = [line.split("|")[1:-1] for line in triage.splitlines() if line.startswith("|") and line.endswith("|")]
    rows = [row for row in rows if len(row) == 4 and row[0].strip() == str(number)]
    if len(rows) != 1 or not rows[0][3].strip():
        raise ValueError("triage does not cover this alert")
    row = rows[0]
    triage_line = "|" + "|".join(row) + "|"
    patch = triage_files[0].get("patch", "").splitlines()
    if "+" + triage_line not in patch or "-" + triage_line in patch:
        raise ValueError("PR did not review this triage row")
    base_ref = pr["base"]["ref"]
    if not re.fullmatch(PATTERNS["ref"], base_ref):
        raise ValueError("invalid triage branch")
    head_endpoint = f"git/ref/heads/{quote(base_ref, safe='')}"
    head_reference = get(head_endpoint)
    if head_reference["ref"] != f"refs/heads/{base_ref}" or head_reference["object"]["type"] != "commit":
        raise ValueError("unknown current triage branch")
    current_head = head_reference["object"]["sha"]
    if not re.fullmatch(PATTERNS["sha"], current_head):
        raise ValueError("unknown current triage head")
    current_triage = (
        triage
        if current_head.lower() == merge.lower()
        else get(f"contents/{_DISMISS_TRIAGE_PATH}?ref={current_head}", raw=True)
    )
    current_rows = [line for line in current_triage.splitlines() if "|" in line
                    and line.strip().removeprefix("|").split("|", 1)[0].strip() == str(number)]
    if current_rows != [triage_line]:
        raise ValueError("triage has been superseded")
    disposition = row[3].strip().lower()
    if fields["reason"] == "won't fix":
        justified = disposition.startswith("won't fix.")
    else:
        justified = disposition.startswith("false positive.")
        if fields["reason"] == "used in tests":
            justified = justified and row[2].strip().startswith("`tests/")
    if not justified:
        raise ValueError("reason does not match written triage")
    alert = get(f"code-scanning/alerts/{number}")
    if type(alert.get("number")) is not int or alert["number"] != number or alert.get("state") != "open":
        raise ValueError("alert identity or state changed")
    if row[1].strip() != f"`{alert['rule']['id']}`":
        raise ValueError("rule changed")
    tool_query = ""
    if "tool" in alert:
        tool = alert["tool"]
        tool_name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(tool_name, str) or not tool_name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in tool_name):
            raise ValueError("invalid alert tool")
        tool_query = f"&tool_name={quote(tool_name, safe='')}"
    get_instances = get(f"code-scanning/alerts/{number}/instances?per_page=100", pages=True)
    instances = [item for item in get_instances if item["state"] == "open"]
    if not instances:
        raise ValueError("no open instances")
    analyses, results = {}, {}

    def match_analysis(rows, sha, instance):
        for analysis in rows:
            if (
                analysis["commit_sha"] == sha
                and all(analysis.get(key) == instance.get(key) for key in ("analysis_key", "environment", "category"))
            ):
                return analysis
        return None

    def evidence(ref, sha, instance, *, not_before=None):
        if not re.fullmatch(PATTERNS["sha"], sha) or not re.fullmatch(PATTERNS["ref"], ref):
            raise ValueError("invalid analysis identity")
        state = analyses.setdefault(ref, {"rows": [], "next_page": 1, "exhausted": False, "oldest_created_at": None})
        analysis = match_analysis(state["rows"], sha, instance)
        while analysis is None and not state["exhausted"] and state["next_page"] <= _DISMISS_ANALYSIS_MAX_PAGES:
            if not_before is not None and state["oldest_created_at"] is not None and state["oldest_created_at"] < not_before:
                break
            page_num = state["next_page"]
            page = get(
                f"code-scanning/analyses?ref={quote(ref, safe='')}{tool_query}"
                f"&per_page={_DISMISS_ANALYSIS_PER_PAGE}&page={page_num}"
            )
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise ValueError("dismissal pagination unavailable")
            state["next_page"] += 1
            if not page:
                state["exhausted"] = True
                break
            for item in page:
                row_sha = item.get("commit_sha")
                if (
                    not isinstance(row_sha, str)
                    or not re.fullmatch(PATTERNS["sha"], row_sha)
                    or not isinstance(item.get("analysis_key"), str)
                    or not isinstance(item.get("environment"), str)
                    or ("category" in item and item["category"] is not None and not isinstance(item["category"], str))
                ):
                    raise ValueError("invalid analysis identity")
                if "created_at" in item:
                    created = _parse_utc_timestamp(item["created_at"])
                    if state["oldest_created_at"] is None or created < state["oldest_created_at"]:
                        state["oldest_created_at"] = created
            state["rows"].extend(page)
            analysis = match_analysis(page, sha, instance)
        if analysis is None:
            raise ValueError("analysis evidence unavailable")
        # GitHub lists newest first; never use an older scan of this commit.
        aid = analysis.get("id")
        if type(aid) is not int or aid <= 0 or analysis.get("error"):
            raise ValueError("analysis failed")
        if aid not in results:
            results[aid] = _dismiss_sarif_result(get(f"code-scanning/analyses/{aid}", sarif=True), number)
        return results[aid]

    def pull_states(pr_numbers):
        owner, repo_name = gh_repo.split("/", 1)
        ordered = sorted(set(pr_numbers))
        states = {}
        client = (
            github_client.GitHubClient(
                env={**gate.internal_environment(environment), "GH_HOST": dest.partition("/")[0]},
                cwd=cwd,
            )
            if raw_graphql
            else None
        )
        with (
            contextlib.nullcontext(None)
            if raw_graphql
            else (
                contextlib.nullcontext(temp)
                if temp is not None
                else tempfile.TemporaryDirectory(prefix="lu-dismiss-prs-", dir=ensure_scratch_root())
            )
        ) as query_dir:
            query_file = None if query_dir is None else Path(query_dir) / "dismiss-pulls.json"
            for start in range(0, len(ordered), _DISMISS_PR_BATCH_SIZE):
                chunk = ordered[start:start + _DISMISS_PR_BATCH_SIZE]
                aliases = " ".join(f"p{n}: pullRequest(number:{n}){{number state merged url}}" for n in chunk)
                query = f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{aliases}}}}}"
                payload = json.dumps({"query": query, "variables": {"owner": owner, "name": repo_name}}).encode("utf-8")
                if client is not None:
                    observation = client.request(
                        "POST", "graphql", payload=payload, timeout=30, fresh=True, raw_response=True,
                    )
                    if observation.stale:
                        raise github_client.GitHubRateLimited(observation.reset_at, observation.reason)
                    refusal = github_client.refusal_of(observation)
                    if refusal is not None:
                        raise refusal
                    if observation.error or observation.status != 200 or not isinstance(observation.value, (bytes, bytearray)):
                        raise ValueError("dismissal PR evidence unavailable")
                    raw_body = bytes(observation.value).decode("utf-8")
                else:
                    if query_file is None:
                        raise ValueError("dismissal PR evidence unavailable")
                    query_file.write_bytes(payload)
                    result = runner(
                        ["gh", "api", "--method", "POST", "graphql", "--input", str(query_file), *_hostname_flag(dest)],
                        cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=30,
                    )
                    if result.returncode:
                        raise ValueError("dismissal PR evidence unavailable")
                    raw_body = result.stdout
                document = github_client.parse_graphql_envelope(raw_body)
                data = document["data"]
                if set(data) != {"repository"}:
                    raise ValueError("unknown instance PR identity or state")
                repo_data = data["repository"]
                expected_keys = {f"p{n}" for n in chunk}
                if not isinstance(repo_data, dict) or set(repo_data) != expected_keys:
                    raise ValueError("unknown instance PR identity or state")
                for n in chunk:
                    pull = repo_data[f"p{n}"]
                    if not isinstance(pull, dict) or set(pull) != {"number", "state", "merged", "url"}:
                        raise ValueError("unknown instance PR identity or state")
                    state = pull["state"]
                    merged = pull["merged"]
                    if (
                        type(pull["number"]) is not int or pull["number"] != n
                        or pull["url"] not in _repository_urls(gh_repo, f"pull/{n}")
                        or not isinstance(state, str) or state not in {"OPEN", "CLOSED", "MERGED"}
                        or type(merged) is not bool
                        or (state == "MERGED") != merged
                    ):
                        raise ValueError("unknown instance PR identity or state")
                    states[n] = (state, merged)
        return states

    baseline_ref = f"refs/heads/{base_ref}"
    if not any(instance["ref"] == baseline_ref for instance in instances):
        raise ValueError("no open triage-branch instance")
    pull_instance_nums = sorted({
        int(match[1])
        for instance in instances
        if (match := re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge", instance["ref"]))
    })
    pr_states = pull_states(pull_instance_nums) if pull_instance_nums else {}
    skipped_prs = {}
    for instance in instances:
        if match := re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge", instance["ref"]):
            pr_num = int(match[1])
            state, merged = pr_states[pr_num]
            if state in {"CLOSED", "MERGED"} or merged:
                skipped_prs[pr_num] = (state, merged)
                continue
        location = instance["location"]
        if (row[2].strip() != f"`{location['path']}:{location['start_line']}`"
                or not instance.get("message", {}).get("text")):
            raise ValueError("location or flow unknown or changed")
        before = evidence(baseline_ref, merge, instance, not_before=merge_cutoff)
        current = evidence(instance["ref"], instance["commit_sha"], instance)
        if before != current or current["result"]["ruleId"] != alert["rule"]["id"]:
            raise ValueError("rule, location or flow changed")
        physical = current["result"]["locations"][0]["physicalLocation"]
        if (physical["artifactLocation"]["uri"] != location["path"]
                or physical["region"]["startLine"] != location["start_line"]):
            raise ValueError("instance does not match SARIF")
    # Close the evidence/read race before the one write, without retrying.
    if (get(f"code-scanning/alerts/{number}") != alert
            or get(f"code-scanning/alerts/{number}/instances?per_page=100", pages=True) != get_instances
            or get(head_endpoint) != head_reference
            or (skipped_prs and pull_states(skipped_prs) != skipped_prs)):
        raise ValueError("alert or triage evidence changed during preflight")
    return alert


def _publish_code_scanning_dismiss(
    fields, *, gh_repo, dest, runner, cwd, environment, temp, capture_output, text, raw_graphql=False,
):
    try:
        _dismiss_preflight(fields, gh_repo, dest, runner, cwd, environment, temp=temp, raw_graphql=raw_graphql)
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        raise gate.PublishBlocked("DISMISSAL_TRIAGE_REFUSED: merged triage or unchanged rule, location and flow evidence unavailable; nothing was sent.") from None
    endpoint = f"repos/{gh_repo}/code-scanning/alerts/{fields['number']}"
    payload = {"state": "dismissed", "dismissed_reason": fields["reason"], "dismissed_comment": fields["comment"]}
    frozen = temp / "request.json"
    frozen.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    argv = ["gh", "api", "--method", "PATCH", endpoint, "--input", str(frozen)]
    unknown = "UNKNOWN: dismissal may have been sent; state, reason and comment are unverified. Do not retry."
    try:
        result = runner(argv, cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=30)
    except (github_client.GitHubRateLimited, gate.PublishBlocked):
        raise
    except Exception:
        result = None
    # CompletedProcess only constructs a result; these calls execute no subprocess.
    if result is not None and result.returncode:
        status = getattr(getattr(result, "github_result", None), "status", None)
        if type(status) is int and 400 <= status < 500:
            refusal = "GitHub refused the dismissal."
            if not capture_output:
                print(refusal, file=sys.stderr)
            return subprocess.CompletedProcess(argv, result.returncode, "" if text else b"", refusal if text else refusal.encode())  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    if result is not None and not result.returncode:
        try:
            after = _dismiss_get(runner, endpoint, cwd, environment, raw_http=raw_graphql)
            if (type(after.get("number")) is int and after["number"] == fields["number"]
                    and after.get("state") == "dismissed" and after.get("dismissed_reason") == fields["reason"]
                    and after.get("dismissed_comment") == fields["comment"]):
                line = json.dumps({"number": fields["number"], **payload}, ensure_ascii=False, sort_keys=True)
                if not capture_output:
                    print(line)
                return subprocess.CompletedProcess(argv, 0, line if text else line.encode(), "" if text else b"")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        except Exception:
            pass  # A read failure after a write is UNKNOWN, including a rate limit.
    print(unknown, file=sys.stderr)
    return subprocess.CompletedProcess(argv, 3, "" if text else b"", unknown if text else unknown.encode())  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit


# The only analysis ref the delete verb accepts: a main merge-queue ref, which GitHub names
# gh-readonly-queue/main/pr-<PR number>-<queued head SHA>. main and refs/pull/* never match.
_QUEUE_ANALYSIS_REF = re.compile(r"refs/heads/gh-readonly-queue/main/pr-[1-9][0-9]*-[0-9a-f]{40}")


def _absent(runner, endpoint, cwd, environment):
    """True only when GitHub answers 404 for the resource; any other answer is not absence."""
    result = runner(
        ["gh", "api", "--method", "GET", endpoint], cwd=cwd, env=dict(environment),
        capture_output=True, text=True, check=False, timeout=30,
    )
    status = getattr(getattr(result, "github_result", None), "status", None)
    if result.returncode and type(status) is int and status == 404:
        return True
    if result.returncode:
        raise ValueError("absence unverified")
    return False


def _delete_outcome(argv, result, *, what, readback, capture_output, text, receipt):
    """Report one sent DELETE: verified receipt, GitHub's 4xx refusal, or UNKNOWN (exit 3)."""
    unknown = f"UNKNOWN: the {what} deletion may have been applied and is unverified. Do not retry."
    if result is not None and result.returncode:
        status = getattr(getattr(result, "github_result", None), "status", None)
        if type(status) is int and 400 <= status < 500:
            refusal = f"GitHub refused the {what} deletion."
            if not capture_output:
                print(refusal, file=sys.stderr)
            return subprocess.CompletedProcess(argv, result.returncode, "" if text else b"", refusal if text else refusal.encode())  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    if result is not None and not result.returncode:
        try:
            if readback():
                line = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
                if not capture_output:
                    print(line)
                return subprocess.CompletedProcess(argv, 0, line if text else line.encode(), "" if text else b"")  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        except Exception:
            pass  # A read failure after a write is UNKNOWN, including a rate limit.
    print(unknown, file=sys.stderr)
    return subprocess.CompletedProcess(argv, 3, "" if text else b"", unknown if text else unknown.encode())  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit


def _send_delete(runner, argv, cwd, environment):
    """Send one DELETE; a rate limit or publisher refusal propagates, any other fault is None.

    A rate limit is either this client's refusal before sending or GitHub's 403/429 limit
    answer; the client types both alike, and GitHub does not process a rate-limited request.
    Either way nothing was deleted, so it stays exit 75 (as for the dismissal PATCH).
    """
    try:
        return runner(argv, cwd=cwd, env=dict(environment), capture_output=True, text=True, check=False, timeout=30)
    except (github_client.GitHubRateLimited, gate.PublishBlocked):
        raise
    except Exception:
        return None


def _publish_analysis_delete(fields, *, gh_repo, runner, cwd, environment, capture_output, text, raw_http=False):
    """Delete one code-scanning analysis of an obsolete main merge-queue ref, newest of its set first.

    GitHub refuses the last analysis of a ref/tool/category set without ``confirm_delete``
    (400). Only then is the same analysis deleted again with that parameter. The URLs a
    successful delete returns name the NEXT analysis of the set; they are never followed.
    """
    aid = fields["analysis_id"]
    endpoint = f"repos/{gh_repo}/code-scanning/analyses/{aid}"

    def refuse(reason):
        raise gate.PublishBlocked(f"ANALYSIS_DELETE_REFUSED: {reason}; nothing was sent.")

    try:
        analysis = _dismiss_get(runner, endpoint, cwd, environment, raw_http=raw_http)
        ref = analysis["ref"]
        if type(analysis["id"]) is not int or analysis["id"] != aid or not isinstance(ref, str):
            raise ValueError("analysis identity")
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        refuse("the analysis is unreadable")
    if not _QUEUE_ANALYSIS_REF.fullmatch(ref):
        refuse("its ref is not a refs/heads/gh-readonly-queue/main/pr-<n>-<sha> merge-queue ref")
    if analysis.get("deletable") is not True:
        refuse("it is not the newest analysis of its ref, tool and category; delete the newer ones first")
    try:
        obsolete = _absent(
            runner, f"repos/{gh_repo}/git/ref/heads/{quote(ref.removeprefix('refs/heads/'), safe='/')}", cwd, environment,
        )
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        refuse("the queue ref's state is unreadable")
    if not obsolete:
        refuse("its merge-queue ref still exists")
    argv = ["gh", "api", "--method", "DELETE", endpoint]
    result = _send_delete(runner, argv, cwd, environment)
    confirmed = False
    status = getattr(getattr(result, "github_result", None), "status", None)
    message = getattr(getattr(result, "github_result", None), "value", None)
    message = message.get("message") if isinstance(message, dict) else None
    if (
        result is not None and result.returncode and type(status) is int and status == 400
        and isinstance(message, str) and "confirm_delete" in message
    ):
        # GitHub reported the last analysis of its set; the 400 deleted nothing.
        confirmed = True
        argv = ["gh", "api", "--method", "DELETE", endpoint + "?confirm_delete"]
        result = _send_delete(runner, argv, cwd, environment)
    tool = analysis.get("tool")
    receipt = {
        "repo": gh_repo, "analysis_id": aid, "ref": ref, "category": analysis.get("category"),
        "tool": tool.get("name") if isinstance(tool, dict) else None, "confirm_delete": confirmed, "deleted": True,
    }
    return _delete_outcome(
        argv, result, what="analysis", readback=lambda: _absent(runner, endpoint, cwd, environment),
        capture_output=capture_output, text=text, receipt=receipt,
    )


def _publish_branch_delete(fields, *, gh_repo, runner, cwd, environment, capture_output, text, raw_http=False):
    """Delete one exact branch that is not default, protected, ruleset-covered or used by an open PR."""
    branch = fields["branch"]
    name = quote(branch, safe="/")
    root = f"repos/{gh_repo}"

    def refuse(reason):
        raise gate.PublishBlocked(f"BRANCH_DELETE_REFUSED: {reason}; nothing was sent.")

    from lu_machinery.orchestration.fleet_repos import ascii_repo_slug

    expected_slug = ascii_repo_slug(gh_repo, strip=False)
    try:
        repo = _dismiss_get(runner, root, cwd, environment, raw_http=raw_http)
        default = repo["default_branch"]
        repo_slug = ascii_repo_slug(repo["full_name"], strip=False)
        if not isinstance(default, str) or not default or expected_slug is None or repo_slug != expected_slug:
            raise ValueError("repository identity")
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        refuse("the repository is unreadable")
    if branch == default:
        refuse("it is the default branch")
    try:
        info = _dismiss_get(runner, f"{root}/branches/{name}", cwd, environment, raw_http=raw_http)
        sha, protected = info["commit"]["sha"], info["protected"]
        if info["name"] != branch or type(protected) is not bool or not re.fullmatch(PATTERNS["sha"], sha):
            raise ValueError("branch identity")
        rules = _dismiss_get(runner, f"{root}/rules/branches/{name}?per_page=100", cwd, environment, pages=True, raw_http=raw_http)
        pulls = _dismiss_get(runner, f"{root}/pulls?state=open&per_page=100", cwd, environment, pages=True, raw_http=raw_http)
        users = []
        for pull in pulls:
            head, base = pull["head"], pull["base"]
            head_repo = head.get("repo")
            head_slug = ascii_repo_slug(head_repo.get("full_name"), strip=False) if isinstance(head_repo, dict) else None
            if (head["ref"] == branch and (
                head_slug is None or head_slug == expected_slug
            )) or base["ref"] == branch:
                users.append(pull["number"])
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        refuse("the branch, its rules or the open pull requests are unreadable")
    if protected or rules:
        refuse("it is protected")
    if users:
        refuse("an open pull request uses it as head or base")
    endpoint = f"{root}/git/refs/heads/{name}"
    argv = ["gh", "api", "--method", "DELETE", endpoint]
    result = _send_delete(runner, argv, cwd, environment)
    return _delete_outcome(
        argv, result, what="branch",
        readback=lambda: _absent(runner, f"{root}/git/ref/heads/{name}", cwd, environment),
        capture_output=capture_output, text=text,
        receipt={"repo": gh_repo, "branch": branch, "sha": sha, "deleted": True},
    )


# Non-completed GitHub Actions workflow run statuses (GitHub REST API).
_FORCE_CANCEL_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending", "cancelling"})
_FORCE_CANCEL_JOB_STATUSES = _FORCE_CANCEL_ACTIVE_STATUSES | {"completed"}


def _require_force_cancel_eligible(
    dest: str,
    gh_repo: str,
    run_id: int,
    runner,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Require a non-completed workflow run with prior cancellation evidence before force-cancel."""
    if any(part in {".", ".."} for part in gh_repo.split("/")):
        raise gate.PublishBlocked("OPSEC: invalid publisher repository.")

    def refuse(reason: str) -> NoReturn:
        raise gate.PublishBlocked(f"FORCE_CANCEL_REFUSED: {reason}; nothing was sent.")

    try:
        run_result = read(
            "run",
            repo=dest,
            number=run_id,
            runner=runner,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        refuse("Actions run lookup failed; --number requires an Actions run ID, not a PR number")
    if run_result.returncode:
        refuse("Actions run lookup failed; --number requires an Actions run ID, not a PR number")
    try:
        run = github_client.loads_strict_json(run_result.stdout)
    except Exception:
        refuse("Actions run response is malformed")
    if not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != run_id:
        refuse("Actions run identity is unreadable or mismatched")
    status = run.get("status")
    if status == "completed":
        refuse("workflow run is already completed")
    if not isinstance(status, str) or status not in _FORCE_CANCEL_ACTIVE_STATUSES:
        refuse("workflow run status is unknown or unsupported")
    conclusion = run.get("conclusion")
    if conclusion is not None and conclusion != "cancelled":
        refuse("workflow run already has a non-cancelled conclusion")
    if status == "cancelling" or conclusion == "cancelled":
        return

    try:
        jobs_result = read(
            "jobs",
            repo=dest,
            number=run_id,
            runner=runner,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            paginate=True,
            slurp=True,
        )
    except github_client.GitHubRateLimited:
        raise
    except Exception:
        refuse("workflow run jobs are unreadable")
    if jobs_result.returncode:
        refuse("workflow run jobs are unreadable")
    try:
        pages = github_client.loads_strict_json(jobs_result.stdout)
    except Exception:
        refuse("workflow run jobs response is malformed")
    if not isinstance(pages, list) or not pages:
        refuse("workflow run jobs response is malformed")
    jobs: list[dict] = []
    expected_total: int | None = None
    for page in pages:
        if not isinstance(page, dict):
            refuse("workflow run jobs response is malformed")
        total = page.get("total_count")
        page_jobs = page.get("jobs")
        if type(total) is not int or total < 0 or not isinstance(page_jobs, list):
            refuse("workflow run jobs response is malformed")
        expected_total = total
        for job in page_jobs:
            if not isinstance(job, dict):
                refuse("workflow run jobs response is malformed")
            if "run_id" in job and (type(job["run_id"]) is not int or job["run_id"] != run_id):
                refuse("workflow run job identity is mismatched")
            job_status = job.get("status")
            if not isinstance(job_status, str) or job_status not in _FORCE_CANCEL_JOB_STATUSES:
                refuse("workflow run job status is unknown or unsupported")
            job_conclusion = job.get("conclusion")
            if job_conclusion is not None and (not isinstance(job_conclusion, str) or not job_conclusion):
                refuse("workflow run job conclusion is malformed")
            jobs.append(job)
    if expected_total is None or len(jobs) != expected_total:
        refuse("workflow run jobs pagination is incomplete")
    if not any(
        (job["status"] == "completed" and job.get("conclusion") == "cancelled")
        or job["status"] == "cancelling"
        for job in jobs
    ):
        refuse("run has no prior cancellation evidence; run run-cancel first")


# Native read-only reviews have no GitHub credential or direct route; named GETs use the host relay.
BROKER_ENV = "LU_WRITE_BOUNDARY_BROKER"


def _read_through_broker(operation, repo, fields, *, paginate, slurp, cwd, env, capture_output, text):
    """Send one named read to the write boundary's host broker (``github-read``); the host's login reads it.

    Inside the boundary there is no GitHub login or route, so a direct read can only fail. The broker reads only
    the launch's repository, with the same fixed templates; any other read is refused here, before anything is sent.
    """
    from lu_machinery.dispatcher import write_boundary

    names = sorted(write_boundary.GITHUB_READS)
    if operation not in write_boundary.GITHUB_READS or set(fields) - {"number", "sha"} or slurp:
        raise gate.PublishBlocked(
            "WRITE_BOUNDARY: inside the write boundary only these reads are relayed, by --number or --sha and "
            f"without --slurp: {', '.join(names)}; nothing was sent."
        )
    payload = {"name": operation, **fields, **({"paginate": True} if paginate else {})}
    # env=None inherits os.environ: an inherited GH_REPO is claimed like an explicit one, so the broker refuses a
    # foreign repository instead of reading the launch's.
    environment = _validated_environment(env)
    if repo or environment.get("GH_REPO"):
        host, _, slug = repository(cwd, environment, repo).partition("/")
        if host != "github.com" or not slug:
            raise gate.PublishBlocked("OPSEC: read repository unresolved.")
        payload["repo"] = slug
    reply = write_boundary.broker_call("github-read", payload)
    code = reply.get("returncode")
    code = code if type(code) is int and 0 <= code < 256 else 1
    out, err = str(reply.get("stdout", "")), str(reply.get("stderr", ""))
    if not capture_output:
        sys.stdout.write(out)
        sys.stderr.write(err)
        sys.stdout.flush()
        sys.stderr.flush()
    if not text:
        out, err = out.encode(), err.encode()
    return subprocess.CompletedProcess(["write_boundary", "call", "github-read"], code, out, err)


@github_client.rate_limited_command
def publish(
    verb: str,
    *,
    runner=None,
    cwd=None,
    env=None,
    stdin=None,
    capture_output=False,
    text=False,
    check=False,
    timeout=None,
    stdout=None,
    stderr=None,
    **fields,
):
    """Validate closed fields, freeze source bytes, scan, then send the same bytes.

    Transport options do not carry payloads. Injected runners exercise exactly
    the same validation, scanning and snapshots as the real gh transport.
    """
    if verb == "code-scanning-dismiss":
        _refuse_dispatched_dismissal(env)
    if verb == "pr-merge" and (refusal := cto_safe_mode.refusal("pr-merge", {**os.environ, **(env or {})})):
        # The CTO seat in safe mode lands nothing; dispatched workers keep landing their own PRs.
        raise gate.PublishBlocked(refusal)
    _validate(verb, fields)
    fields = {
        key: tuple(value) if SCHEMAS[verb][1][key] in {"texts", "assets"} else value for key, value in fields.items()
    }
    if verb in {"issue-edit", "pr-edit", "release-edit"} and not (set(fields) - {"number", "tag", "repo"}):
        raise gate.PublishBlocked("OPSEC: edit requires explicit fields; interactive publishing refused.")
    environment = _validated_environment(env)
    cwd = Path(cwd or Path.cwd())
    if verb == "issue-transfer":
        return _issue_transfer(
            fields, runner=runner, cwd=cwd, environment=environment, capture_output=capture_output, text=text
        )
    if verb in REPO_ADMIN_VERBS:
        with _repo_admin_run() as state:
            result = _repo_admin(
                verb, fields, runner=runner, cwd=cwd, environment=environment, capture_output=capture_output,
                text=text,
            )
            # Account for failures that first appear after the receipt was written.
            for name in ("stdout", "stderr"):
                _stream_write(name, "")
            if state["sent"] and state["failed"]:
                result.returncode = REPO_ADMIN_UNSETTLED
            return result
    dest = "unknown" if verb == "gist-create" else repository(cwd, environment, fields.get("repo"))
    if dest == "unknown" and verb != "gist-create":
        raise gate.PublishBlocked("OPSEC: publisher repository unresolved; provide --repo.")
    # Pin the proven destination in argv; never let gh resolve it differently.
    if dest != "unknown":
        environment["GH_HOST"] = dest.split("/", 1)[0]
    gh_repo = dest.split("/", 1)[1] if dest != "unknown" else ""
    if verb == "repo-fork" and (
        environment["GH_HOST"] != "github.com" or any(part in {".", ".."} for part in gh_repo.split("/"))
    ):
        raise gate.PublishBlocked("OPSEC: repo-fork requires a github.com repository owner/name.")
    texts, names = [], []

    def scan(name, value):
        texts.append(value)
        names.append(name)

    with tempfile.TemporaryDirectory(prefix="lu-publish-") as directory:
        temp = Path(directory)
        for field in ("body", "notes"):
            if field + "_file" in fields:
                raw = _read_file(fields.pop(field + "_file"), cwd, stdin)
                try:
                    fields[field] = raw.decode("utf-8")
                except UnicodeError:
                    raise gate.PublishBlocked("OPSEC: publisher text is not UTF-8.") from None
        if verb in {"issue-create", "issue-edit"} and "body" in fields:
            _refuse_unreadable_criteria(fields["body"])
        _refuse_verdict_markers(verb, fields)

        readiness_runner = merge_readiness_runner(runner)
        parent_epic = None
        if verb == "issue-create":
            from lu_machinery.orchestration import fleet_repos

            repos = _catalog_fleet_repos()
            active = _fleet_catalog_row(dest, env=environment, repos=repos)
            if active is None:
                raise gate.PublishBlocked(
                    "REPO_REFUSED: issue-create only targets a repository registered in "
                    f"{_FLEET_REPOS}; nothing was created."
                )
            owns_registry = _owns_stream_registry(active, default_key=fleet_repos.default_repo_key(repos))
            if fields.get("epic"):
                if not cto_safe_mode.is_cto_caller(os.environ):
                    raise gate.PublishBlocked("EPIC_REFUSED: --epic requires the CTO seat; nothing was created.")
                if not owns_registry or environment["GH_HOST"] != "github.com" or "parent" in fields:
                    raise gate.PublishBlocked(
                        "EPIC_REFUSED: use --epic without --parent in the stream registry repository on github.com; "
                        "nothing was created."
                    )
                if not re.fullmatch(r"\[EPIC\]\[[a-z0-9]+(?:-[a-z0-9]+)*\] \S[^\r\n]*", fields["title"]):
                    raise gate.PublishBlocked(
                        "EPIC_REFUSED: --epic requires a title in the form '[EPIC][stream-key] Title'; "
                        "nothing was created."
                    )
            elif owns_registry or "parent" in fields:
                parent_epic = _parent_epic_node(
                    readiness_runner, active, repos, fields.get("parent"), cwd, environment,
                )

        def queue_probe(pr_number=None):
            target_number = fields["number"] if pr_number is None else pr_number
            identity = readiness_runner(
                ["gh", "pr", "view", str(target_number), "--repo", gh_repo,
                 "--json", "id,headRefOid,isCrossRepository,baseRefName,labels"],
                cwd=cwd, env=environment, capture_output=True, text=True, check=False, timeout=15,
            )
            if identity.returncode:
                raise ValueError("queue identity unavailable")
            identity = json_depth.loads(identity.stdout, max_depth=512)
            result = read(
                "queue-status", repo=gh_repo, number=target_number, branch=identity["baseRefName"],
                runner=readiness_runner if runner is not None else None,
                cwd=cwd, env=environment, capture_output=True, text=True, check=False, timeout=15,
            )
            observation = getattr(result, "github_result", None)
            refusal = github_client.refusal_of(result, injected=runner is not None)
            if result.returncode or refusal is not None or (observation is not None and observation.stale):
                raise ValueError("queue state unavailable")
            document = github_client.parse_graphql_envelope(result.stdout)
            current = document["data"]["repository"]["pullRequest"]
            if not isinstance(current, dict) or current.get("headRefOid") != identity["headRefOid"]:
                raise ValueError("queue state unavailable or head changed")
            return {
                **current,
                "isCrossRepository": identity["isCrossRepository"],
                "id": identity.get("id"),
                "labels": identity.get("labels"),
            }

        def queue_jump(pull, pr_number=None, match_head=None):
            """Whether this enqueue jumps the queue; unreadable evidence never jumps."""
            from lu_machinery.orchestration.integration_sweep import PRIORITY_LABEL, priority_jump

            target_number = fields["number"] if pr_number is None else pr_number
            target_head = fields["match_head"] if match_head is None else match_head
            labels = pull.get("labels")
            if not isinstance(labels, list) or PRIORITY_LABEL not in {
                row.get("name") for row in labels if isinstance(row, dict)
            }:
                return False
            if not isinstance(pull.get("id"), str):
                raise ValueError("node id unavailable")
            documents = {}
            for name in ("identity", "comments", "timeline"):
                result = read(
                    name, repo=gh_repo, runner=readiness_runner, cwd=cwd, env=environment,
                    capture_output=True, text=True, check=False, timeout=15,
                    **({} if name == "identity" else {"number": target_number, "paginate": True, "slurp": True}),
                )
                if result.returncode:
                    raise ValueError(f"{name} unavailable")
                value = json_depth.loads(result.stdout, max_depth=512)
                documents[name] = value if name == "identity" else [row for page in value for row in page]
            jumped = priority_jump(
                labels, documents["timeline"], documents["comments"], target_head,
                documents["identity"].get("login"),
            )
            if not jumped:
                print(
                    f"queue-jump: refused; {PRIORITY_LABEL} was not applied by the fleet login"
                    " after the approval of this exact head",
                    file=sys.stderr,
                )
            return jumped

        recovery_queue = False
        already_queued = False
        jump = False
        if verb == "pr-merge":
            from lu_machinery.publish.merge_guard import ensure_merge_ready

            # Bind the merge target to its registry row before any GitHub read: an inactive,
            # ambiguous or selector-less spelling is refused here (the seat guard reads the row again).
            _fleet_catalog_entry(dest, environment)
            # Refuse a blocked supplied subject or body before any GitHub read. With an
            # override set, the one combined scan below decides, so the override is
            # offered once for the whole publish.
            supplied: list[str] = [key for key in ("subject", "body") if key in fields]
            if supplied and not environment.get(gate.OVERRIDE, "").strip():
                try:
                    gate.check_texts(
                        dest, [fields[key] for key in supplied], environment=dict(environment), field_names=supplied
                    )
                except gate.PublishBlocked:
                    raise
                except Exception:
                    raise gate.PublishBlocked("OPSEC: checker unavailable; write refused.") from None
            fields["match_head"] = ensure_merge_ready(
                gh_repo,
                fields["number"],
                runner=readiness_runner,
                cwd=cwd,
                environment=environment,
                match_head=fields.get("match_head"),
            )
            # GitHub's default squash text carries the PR title and commit messages.
            # Omitted fields are sent explicitly so the scanned text is the sent text;
            # a merge queue ignores explicit text, so its defaults are scanned too.
            default = _squash_text(gh_repo, fields, dest, temp, readiness_runner, cwd, environment)
            recovery_queue = default["queue"]
            if "expect_queue" in fields and (fields["expect_queue"] == "true") is not recovery_queue:
                raise gate.PublishBlocked(
                    "QUEUE_MODE_CHANGED: merge refused before publication: the base branch merge queue is "
                    + ("on" if recovery_queue else "off") + " but --expect-queue was " + fields["expect_queue"]
                    + "; re-read queue-status and re-check the landing gate for that kind."
                )
            if "recovery_run_id" in fields and not recovery_queue:
                raise gate.PublishBlocked(
                    "RECOVERY_QUEUE_REQUIRED: same-head recovery re-enqueue requires an active merge queue."
                )
            if recovery_queue:
                try:
                    before = queue_probe()
                    if before.get("headRefOid") != fields["match_head"]:
                        raise ValueError("head changed")
                    if type(before.get("isInMergeQueue")) is not bool:
                        raise ValueError("membership unknown")
                    already_queued = before["isInMergeQueue"]
                except Exception:
                    raise gate.PublishBlocked("QUEUE_STATE_UNKNOWN: enqueue refused before publication") from None
                if not already_queued:
                    try:
                        jump = queue_jump(before)
                    except github_client.GitHubRateLimited:
                        raise
                    except Exception:
                        print("queue-jump: priority evidence unreadable; enqueuing without jump", file=sys.stderr)
            for key in ("subject", "body"):
                if key not in fields:
                    fields[key] = default[key]
                elif default["queue"]:
                    scan("default_" + key, default[key])
            # Validate the subject GitHub publishes: an omitted one, or a queue's own default.
            _validate(verb, {**fields, "subject": default["subject"]} if default["queue"] else fields)
        schema = SCHEMAS[verb][1]
        for key, value in fields.items():
            if schema[key] == "text":
                scan(key, value)
            elif verb == "repo-fork":
                scan(key, gh_repo)
            elif schema[key] == "texts":
                for index, item in enumerate(value):
                    scan(f"{key}[{index}]", item)
        assets = []
        for index, asset in enumerate(fields.get("assets", fields.get("files", []))):
            public_name, source_path = asset.name, asset.path
            Asset(source_path, public_name)  # Revalidate mutable caller objects before writing a snapshot.
            scan(f"asset[{index}].name", public_name)
            raw = _read_file(source_path, cwd, None)
            try:
                decoded = raw.decode("utf-8")
            except UnicodeError:
                if verb == "gist-create":
                    raise gate.PublishBlocked("OPSEC: gist content must be UTF-8.") from None
            else:
                scan(f"asset[{index}].content", decoded)
            folder = temp / str(index)
            folder.mkdir()
            frozen = folder / public_name
            frozen.write_bytes(raw)
            assets.append(str(frozen))
        try:
            gate.check_texts(dest, texts, environment=environment, field_names=names)
        except gate.PublishBlocked:
            raise
        except Exception:
            raise gate.PublishBlocked("OPSEC: checker unavailable; write refused.") from None
        pr_milestone = None
        if verb == "pr-create":
            if "milestone" in fields:
                pr_milestone = _resolve_explicit_milestone(
                    readiness_runner, dest, gh_repo, fields["milestone"], cwd, environment,
                    injected=runner is not None,
                )
            elif "body" in fields:
                pr_milestone = _resolve_linked_issue_milestone(
                    readiness_runner, dest, gh_repo, fields["body"], cwd, environment,
                    injected=runner is not None,
                )
        if verb == "code-scanning-dismiss":
            return _publish_code_scanning_dismiss(
                fields, gh_repo=gh_repo, dest=dest, runner=readiness_runner, cwd=cwd,
                environment=environment, temp=temp, capture_output=capture_output, text=text,
                raw_graphql=runner is None,
            )
        if verb in {"code-scanning-analysis-delete", "branch-delete"}:
            return (_publish_analysis_delete if verb == "code-scanning-analysis-delete" else _publish_branch_delete)(
                fields, gh_repo=gh_repo, runner=readiness_runner, cwd=cwd, environment=environment,
                capture_output=capture_output, text=text, raw_http=runner is None,
            )
        if verb == "ruleset-merge-queue":
            return _publish_ruleset_merge_queue(
                fields,
                gh_repo=gh_repo,
                dest=dest,
                runner=readiness_runner,
                cwd=cwd,
                environment=environment,
                temp=temp,
                capture_output=capture_output,
                text=text,
            )
        if verb == "comment-edit":
            try:
                documents = []
                for operation, selectors in (("comment", {"number": fields["comment_id"]}), ("identity", {})):
                    result = read(
                        operation, repo=dest, runner=readiness_runner, cwd=cwd, env=environment,
                        capture_output=True, text=True, check=False, timeout=15, **selectors,
                    )
                    if result.returncode:
                        raise ValueError
                    documents.append(json_depth.loads(result.stdout, max_depth=512))
                comment, identity = documents
                body, login = comment["body"], identity["login"]
                if (
                    type(comment["id"]) is not int or comment["id"] != fields["comment_id"]
                    or not isinstance(body, str) or CF_VERDICT_MARKER_PREFIX in body
                    or not isinstance(login, str) or not login or comment["user"]["login"] != login
                ):
                    raise ValueError
            except github_client.GitHubRateLimited:
                raise
            except Exception:
                raise gate.PublishBlocked("OPSEC: comment edit requires a verified own comment without a verdict marker.") from None
        if verb in {"release-create", "release-edit"} and "notes" not in fields and verb == "release-create":
            fields["notes"] = ""
        if verb == "pr-review" and "body" not in fields:
            fields["body"] = ""
        argv = ["gh", *verb.split("-", 1)]
        if verb != "gist-create":
            argv += ["--repo", gh_repo]
        if "number" in fields:
            # Keep control operands positional; text fields are always =values.
            argv.insert(3, str(fields["number"]))

        def option(key, flag=None):
            if key in fields:
                argv.append((flag or "--" + key.replace("_", "-")) + "=" + str(fields[key]))

        def body_file(key):
            if key in fields:
                frozen = temp / (key + ".txt")
                frozen.write_bytes(fields[key].encode("utf-8"))
                argv.extend(["--" + key + "-file", str(frozen)])

        if verb in {"issue-create", "issue-edit", "pr-create", "pr-edit", "issue-comment", "pr-comment", "pr-review"}:
            option("title")
            body_file("body")
            for key, flag in (
                ("labels", "--label"),
                ("add_labels", "--add-label"),
                ("remove_labels", "--remove-label"),
            ):
                for item in fields.get(key, []):
                    argv.append(flag + "=" + item)
            if verb != "pr-create":
                option("milestone")
            if fields.get("remove_milestone"):
                argv.append("--remove-milestone")
            for key in ("base", "head"):
                option(key)
            if fields.get("draft"):
                argv.append("--draft")
            if verb == "pr-review":
                argv.append("--" + fields["verdict"])
        elif verb == "issue-close":
            option("comment")
            option("reason")
        elif verb == "pr-merge" and recovery_queue:
            # Queue repositories must enqueue without arming auto-merge.
            frozen = temp / "enqueue.json"
            frozen.write_bytes(json.dumps({
                "query": "mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:"
                "{pullRequestId:$id,expectedHeadOid:$head,jump:" + str(jump).lower()
                + "}){mergeQueueEntry{position jump}}}",
                "variables": {"id": default["id"], "head": fields["match_head"]},
            }).encode("utf-8"))
            argv = ["gh", "api", "--method", "POST", "graphql", "--input", str(frozen), *_hostname_flag(dest)]
        elif verb == "pr-merge":
            argv.append("--squash")
            option("subject")
            option("match_head", "--match-head-commit")
            body_file("body")
        elif verb in {"pr-update-branch", "pr-ready", "pr-close", "issue-reopen", "run-cancel"}:
            pass
        elif verb == "run-force-cancel":
            argv = [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{gh_repo}/actions/runs/{fields['number']}/force-cancel",
                *_hostname_flag(dest),
            ]
        elif verb == "run-rerun":
            pass
        elif verb == "workflow-run":
            argv.insert(3, fields["workflow"])
            option("ref")
            for name, value in fields.get("inputs", {}).items():
                # gh sends workflow_dispatch booleans as lowercase true/false.
                rendered = str(value).lower() if type(value) is bool else value
                argv.append(f"--raw-field={name}={rendered}")
        elif verb == "pr-disarm":
            argv = ["gh", "pr", "merge", str(fields["number"]), "--repo", gh_repo, "--disable-auto"]
        elif verb.startswith("release-"):
            # Tag after -- prevents it being interpreted as a flag.
            for key in ("title", "target"):
                option(key)
            body_file("notes")
            for key in ("draft", "prerelease"):
                if key in fields:
                    argv.append(f"--{key}={str(fields[key]).lower()}")
            if fields.get("clobber"):
                argv.append("--clobber")
            argv += ["--", fields["tag"], *assets]
        elif verb == "gist-create":
            option("description")
            if fields.get("public"):
                argv.append("--public")
            argv += ["--", *assets]
        elif verb in {"label-create", "label-edit"}:
            option("color")
            option("description")
            option("new_name", "--name")
            if fields.get("force"):
                argv.append("--force")
            argv += ["--", fields["name"]]
        else:
            payload: dict[str, object] = {}
            if verb == "repo-fork":
                endpoint, method = f"repos/{gh_repo}/forks", "POST"
                payload = {"default_branch_only": True}
            elif verb == "commit-status":
                endpoint = f"repos/{gh_repo}/statuses/{fields['sha']}"
                payload = {key: fields[key] for key in ("state", "context", "description")}
                method = "POST"
            elif verb == "issue-comment-json":
                endpoint = f"repos/{gh_repo}/issues/{fields['number']}/comments"
                payload, method = {"body": fields["body"]}, "POST"
            elif verb == "comment-edit":
                endpoint = f"repos/{gh_repo}/issues/comments/{fields['comment_id']}"
                payload, method = {"body": fields["body"]}, "PATCH"
            elif verb.startswith("milestone-"):
                endpoint = f"repos/{gh_repo}/milestones" + (f"/{fields['number']}" if verb.endswith("edit") else "")
                payload = {key: fields[key] for key in ("title", "description", "state") if key in fields}
                method = "PATCH" if verb.endswith("edit") else "POST"
            else:
                endpoint, method = "graphql", "POST"
                if verb == "issue-link" and fields.get("replace_parent"):
                    # Moving an issue between epics needs GitHub's explicit replaceParent.
                    payload = {
                        "query": "mutation($p:ID!,$c:ID!,$r:Boolean!){addSubIssue(input:{issueId:$p,subIssueId:$c,replaceParent:$r}){issue{number}}}",
                        "variables": {"p": fields["parent_id"], "c": fields["child_id"], "r": True},
                    }
                elif verb == "issue-link":
                    payload = {
                        "query": "mutation($p:ID!,$c:ID!){addSubIssue(input:{issueId:$p,subIssueId:$c}){issue{number}}}",
                        "variables": {"p": fields["parent_id"], "c": fields["child_id"]},
                    }
                elif verb == "issue-unlink":
                    payload = {
                        "query": "mutation($p:ID!,$c:ID!){removeSubIssue(input:{issueId:$p,subIssueId:$c}){issue{number}}}",
                        "variables": {"p": fields["parent_id"], "c": fields["child_id"]},
                    }
                elif verb == "pr-dequeue":
                    payload = {
                        "query": "mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}",
                        "variables": {"id": fields["node_id"]},
                    }
            frozen = temp / "request.json"
            frozen.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            argv = ["gh", "api", "--method", method, endpoint, "--input", str(frozen)]
            if endpoint == "graphql":
                argv += _hostname_flag(dest)
            if verb == "repo-fork":
                argv += ["--jq", ".html_url"]
        if verb == "pr-merge":
            _refuse_incomplete_seats(
                dest, fields["number"], fields["match_head"], readiness_runner, cwd, environment,
            )
        if verb == "run-force-cancel":
            _require_force_cancel_eligible(dest, gh_repo, fields["number"], readiness_runner, cwd, environment)
        if verb == "run-rerun" or (verb == "pr-merge" and "recovery_run_id" in fields and not already_queued):
            reservation = _prepare_recovery(verb, fields, dest, cwd, environment, readiness_runner)
            if reservation:
                path, number, head, action, evidence = reservation
                if verb == "run-rerun" and action == "re-enqueue":
                    from lu_machinery.publish.merge_guard import ensure_merge_ready

                    if refusal := cto_safe_mode.refusal("pr-merge", {**os.environ, **(env or {})}):
                        raise gate.PublishBlocked(refusal)
                    ensure_merge_ready(
                        gh_repo,
                        number,
                        runner=readiness_runner,
                        cwd=cwd,
                        environment=environment,
                        match_head=head,
                    )
                    _refuse_incomplete_seats(dest, number, head, readiness_runner, cwd, environment)
                    default = _squash_text(
                        gh_repo,
                        {"number": number, "match_head": head},
                        dest,
                        temp,
                        readiness_runner,
                        cwd,
                        environment,
                    )
                    if not default["queue"]:
                        raise gate.PublishBlocked(
                            "RECOVERY_QUEUE_REQUIRED: cancelled merge-group recovery requires an active merge queue."
                        )
                    if not "".join(gate.normalize_for_scan(default["subject"]).splitlines()[:1]).strip():
                        raise gate.PublishBlocked("OPSEC: merge subject must not be blank.")
                    try:
                        gate.check_texts(
                            dest,
                            [default["subject"], default["body"]],
                            environment=environment,
                            field_names=["default_subject", "default_body"],
                        )
                    except gate.PublishBlocked:
                        raise
                    except Exception:
                        raise gate.PublishBlocked("OPSEC: checker unavailable; write refused.") from None
                    try:
                        before = queue_probe(number)
                        probe_head = before.get("headRefOid")
                        if not isinstance(probe_head, str) or probe_head.lower() != head.lower():
                            raise ValueError("head changed")
                        if type(before.get("isInMergeQueue")) is not bool:
                            raise ValueError("membership unknown")
                        in_queue = before["isInMergeQueue"]
                    except github_client.GitHubRateLimited:
                        raise
                    except Exception:
                        raise gate.PublishBlocked("QUEUE_STATE_UNKNOWN: enqueue refused before publication") from None
                    if in_queue:
                        raise gate.PublishBlocked(
                            "RECOVERY_STILL_QUEUED: PR is still in the merge queue and GitHub offers no "
                            "candidate-conditional dequeue; verify the queue entry and run pr-dequeue manually "
                            "before retrying recovery"
                        )
                    try:
                        jump = queue_jump(before, number, head)
                    except github_client.GitHubRateLimited:
                        raise
                    except Exception:
                        print("queue-jump: priority evidence unreadable; enqueuing without jump", file=sys.stderr)
                    recovery.consume(path, dest, number, head, action, evidence)
                    enqueue_payload = temp / "enqueue.json"
                    enqueue_payload.write_bytes(json.dumps({
                        "query": "mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:"
                        "{pullRequestId:$id,expectedHeadOid:$head,jump:" + str(jump).lower()
                        + "}){mergeQueueEntry{position jump}}}",
                        "variables": {"id": default["id"], "head": head},
                    }).encode("utf-8"))
                    enqueue_argv = [
                        "gh", "api", "--method", "POST", "graphql", "--input", str(enqueue_payload),
                        *_hostname_flag(dest),
                    ]
                    result = _send(
                        enqueue_argv,
                        environment=environment,
                        runner=runner,
                        cwd=cwd,
                        capture_output=capture_output,
                        text=text,
                        check=check,
                        timeout=timeout,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    if result.returncode == 0:
                        _confirm_enqueued(
                            gh_repo, number, head, dest, temp, (readiness_runner, runner), cwd, environment,
                        )
                        if jump:
                            result.queue_jump = True
                            print(f"queue-jump: enqueued at the front of the merge queue at {head}", file=sys.stderr)
                    return result
                if verb == "run-rerun" and evidence.get("recovery_mode") != recovery.INCOHERENT_CHECK_RECOVERY_MODE:
                    argv.append("--failed")
                recovery.consume(path, dest, number, head, action, evidence)
            elif verb == "run-rerun":
                argv.append("--failed")
        applying_milestone = pr_milestone is not None
        capture_created = parent_epic is not None or applying_milestone
        result = subprocess.CompletedProcess(argv, 0, "" if text else b"", "" if text else b"") if already_queued else _send(
            argv,
            environment=environment,
            runner=runner,
            cwd=cwd,
            capture_output=capture_output or capture_created,
            text=text or capture_created,
            check=check,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
        )
        if parent_epic is not None and result.returncode == 0:
            parent_repo, parent_node = parent_epic
            _link_created_issue(
                str(result.stdout or ""), f"{parent_repo}#{fields['parent']}", parent_node, gh_repo,
                readiness_runner, runner,
                cwd, environment,
            )
            if not capture_output:
                print(str(result.stdout or "").strip())
        if pr_milestone is not None and result.returncode == 0:
            _apply_pr_milestone(
                str(result.stdout or ""), pr_milestone[0], pr_milestone[1], dest, gh_repo, runner,
                cwd, environment, temp,
            )
            if not capture_output:
                print(str(result.stdout or "").strip())
        if capture_created and not capture_output and result.returncode != 0 and result.stderr:
            sys.stderr.write(str(result.stderr))
        if capture_created and capture_output and not text:
            if isinstance(result.stdout, str):
                result.stdout = result.stdout.encode("utf-8")
            if isinstance(result.stderr, str):
                result.stderr = result.stderr.encode("utf-8")
        if verb == "pr-merge" and recovery_queue and not already_queued and result.returncode == 0:
            _confirm_enqueued(
                gh_repo, fields["number"], fields["match_head"], dest, temp, (readiness_runner, runner), cwd, environment,
            )
        if jump and result.returncode == 0:
            result.queue_jump = True
            print(f"queue-jump: enqueued at the front of the merge queue at {fields['match_head']}", file=sys.stderr)
        return result


class PublisherParser(argparse.ArgumentParser):
    """Invalid CLI input never echoes user values or transport paths."""

    def error(self, message):
        self.exit(2, "OPSEC: invalid publisher arguments; use --help.\n")


class StoreOnce(argparse.Action):
    """Reject repeated scalar options on comment edits before any transport."""

    def __call__(self, parser, namespace, values, option_string=None):
        if hasattr(namespace, self.dest):
            parser.error("repeated publisher field")
        setattr(namespace, self.dest, values)


def loads(value: str) -> object:
    """Bounded argparse JSON callback with the stdlib's stable diagnostic name."""
    try:
        return json_depth.loads(value, max_depth=512)
    except RecursionError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv=None, *, runner=None):
    return _run_args(_parse_args(argv), runner=runner)


def _parse_args(argv):
    parser = PublisherParser(
        description="Publish GitHub changes through closed, scanned fields.\nUse for public writes; raw gh reads use native CLI arguments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Examples:\n  .venv/bin/python -m {_PUBLISHER} issue-comment --repo unit/public --number 1 --body-file reply.md\n  .venv/bin/python -m {_PUBLISHER} read code-scanning-alerts --number 9781\n  .venv/bin/python -m {_PUBLISHER} read check-annotations --number 112001533549\nOutputs: GitHub mutation for write verbs; JSON on stdout for reads; temporary snapshots removed on exit.\nExit codes: 0 success; 1 gh failure; 2 schema or publishing refusal.\nRelated: docs/dev/agent-public-text.md; #9297; #9792",
        allow_abbrev=False,
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb, (_, schema) in SCHEMAS.items():
        sub = verbs.add_parser(verb, help=f"Publish {verb} with scanned fields", allow_abbrev=False)
        if verb == "issue-create":
            sub.description = (
                "Create an issue in a registered repository.\n"
                "Link ordinary issues with --parent. "
                "Use --epic to create an approved stream root."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Example for the CTO seat:\n"
                f"  .venv/bin/python -m {_PUBLISHER} issue-create --epic --repo unit/public "
                "--title '[EPIC][my-stream] Coordination' --body-file epic.md\n"
                "Outputs: the issue URL. Add the new root to the stream registry with the same delivery.\n"
                "Exit codes: 0 success; 1 GitHub failure; 2 invalid input, publishing refusal or transport error.\n"
                "If LINK_FAILED appears, the ordinary issue already exists and needs a parent link.\n"
                "Related: workflow.md, Work intake."
            )
        if verb == "code-scanning-dismiss":
            sub.description = (
                "Dismiss one reviewed code-scanning alert from an interactive seat.\n"
                "Workers and reviewers are refused, including direct Python API calls."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "The merged same-repository PR linked in the comment must change "
                "docs/runbooks/secret-scanning.md and contain this alert's triage row.\n"
                "Rule, location and SARIF flow evidence must match the analysis at that merge, across open branches.\n"
                "Example:\n"
                f"  .venv/bin/python -m {_PUBLISHER} code-scanning-dismiss --repo unit/public --number 1 "
                "--reason 'false positive' --comment 'Alert #1 stores public metadata. https://github.com/unit/public/pull/2'\n"
                "Outputs: one fixed PATCH with state=dismissed, then verified state/reason/comment JSON.\n"
                "Exit codes: 0 verified dismissal; 1 GitHub refusal; 2 schema, boundary or triage refusal; "
                "3 UNKNOWN after an ambiguous write or read-back, do not retry; "
                "75 rate-limit refusal before a dismissal is processed, including at PATCH.\n"
                "Related: docs/runbooks/secret-scanning.md; GitHub REST code-scanning alert update."
            )
        if verb == "repo-fork":
            sub.description = (
                "Fork one GitHub repository into the authenticated user's account.\n"
                "Use when a change needs a fork. The publisher creates no local checkout."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Example:\n"
                f"  .venv/bin/python -m {_PUBLISHER} repo-fork --repo kube-dojo/kubedojo-labs\n"
                "Outputs: the fork URL after GitHub accepts the default-branch fork request.\n"
                "GitHub creates the fork asynchronously. Wait for it to be available before pushing.\n"
                "Exit codes: 0 accepted, 1 GitHub failure, 2 invalid input or publishing refusal, 75 rate limit.\n"
                "Related: pr-create --head owner:branch."
            )
        if verb == "code-scanning-analysis-delete":
            sub.description = (
                "Delete one code-scanning analysis of an obsolete main merge-queue ref.\n"
                "Any other ref, including refs/heads/main and refs/pull/*, is refused before a DELETE is sent."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Checks before the DELETE: the analysis is read; its ref must be\n"
                "  refs/heads/gh-readonly-queue/main/pr-<n>-<sha>, that ref must no longer exist, and GitHub must\n"
                "  mark the analysis deletable (the newest of its ref/tool/category set; delete newest first).\n"
                "If GitHub answers that it is the last analysis of its set (400), the same analysis is deleted\n"
                "  again with ?confirm_delete. The next-analysis URLs GitHub returns are never followed.\n"
                "Example:\n"
                f"  .venv/bin/python -m {_PUBLISHER} code-scanning-analysis-delete --repo unit/public --analysis-id 41\n"
                "Outputs: JSON {repo, analysis_id, ref, tool, category, confirm_delete, deleted} after a read-back 404.\n"
                "Exit codes: 0 deleted and verified; 1 GitHub refused the deletion (nothing deleted); 2 invalid input\n"
                "  or a refusal before any DELETE; 3 UNKNOWN after an ambiguous DELETE or read-back, do not retry;\n"
                "  75 a rate limit refused a request, locally or by GitHub at the DELETE; nothing was deleted,\n"
                "  and a rerun after the reset repeats every check.\n"
                "Related: docs/runbooks/secret-scanning.md; GitHub REST delete a code scanning analysis."
            )
        if verb == "branch-delete":
            sub.description = (
                "Delete one exact remote branch that nothing uses.\n"
                "The default branch, a protected or ruleset-covered branch, and a branch that is the head or base\n"
                "of an open pull request are refused before a DELETE is sent."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "The name is used exactly as given: no pattern, no prefix, no issue number derived from it.\n"
                "Example:\n"
                f"  .venv/bin/python -m {_PUBLISHER} branch-delete --repo unit/public --branch salvage/old-attempt\n"
                "Outputs: JSON {repo, branch, sha, deleted} after a read-back 404; restore with\n"
                "  git push origin <sha>:refs/heads/<branch>.\n"
                "Exit codes: 0 deleted and verified; 1 GitHub refused the deletion (nothing deleted); 2 invalid input\n"
                "  or a refusal before any DELETE; 3 UNKNOWN after an ambiguous DELETE or read-back, do not retry;\n"
                "  75 a rate limit refused a request, locally or by GitHub at the DELETE; nothing was deleted,\n"
                "  and a rerun after the reset repeats every check.\n"
                "Related: docs/runbooks/remote-branch-cleanup.md; GitHub REST delete a reference."
            )
        if verb == "comment-edit":
            sub.description = (
                "Edit an existing issue or PR conversation comment through the common scanner.\n"
                "Use the comment ID from its API response or #issuecomment-ID URL fragment.\n"
                "An issue or PR number is not a comment ID; inline review comments are unsupported."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} comment-edit --repo unit/public --comment-id 123456 --body-file reply.md\n"
                f"  .venv/bin/python -m {_PUBLISHER} comment-edit --repo unit/public --comment-id 123456 --body-file -\n"
                "Outputs: PATCH the same comment ID with the scanned body; temporary payload removed on exit.\n"
                "Exit codes: 0 success; 1 GitHub failure; 2 invalid input, publishing refusal or transport error.\n"
                "Related: docs/dev/agent-public-text.md; read comment."
            )
        if verb == "workflow-run":
            sub.description = (
                "Start one allowlisted workflow on an explicit ref.\n"
                "Inputs are that workflow file's workflow_dispatch inputs, passed as one JSON object."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} workflow-run --repo unit/public --workflow ci.yml --ref main "
                "--inputs '{\"secret_scan_mode\": \"full-history-audit\"}'\n"
                "Outputs: a workflow_dispatch request. A choice value must be one of the options that input declares.\n"
                "  Other workflows and other values are refused before transport.\n"
                "Exit codes: 0 request accepted; 1 gh failure; 2 invalid input or publishing refusal.\n"
                "Related: docs/dev/agent-public-text.md."
            )
        if verb == "run-cancel":
            sub.description = (
                "Request ordinary cancellation of one GitHub Actions workflow run.\n"
                "Use only for an explicitly authorized run after checking its identity and current state."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} read run --repo unit/public --number 12345678901\n"
                f"  .venv/bin/python -m {_PUBLISHER} run-cancel --repo unit/public --number 12345678901\n"
                "Outputs: requests ordinary cancellation; can interrupt work and eject a merge-group run from the queue.\n"
                "  Request acceptance does not prove terminal cancellation; read back the run state.\n"
                "  Cancellation does not satisfy CI or merge gates and does not consume or reset recovery allowances.\n"
                "  When a cancelled run stays non-completed (for example, an if: always() job stays queued), use run-force-cancel.\n"
                "Exit codes: 0 request accepted, terminal state unverified; 1 GitHub API failure;\n"
                "  2 invalid input, publishing refusal or transport error; 75 rate-limit refusal.\n"
                "Related: docs/dev/agent-public-text.md; read run; run-force-cancel; run-rerun."
            )
        if verb == "run-force-cancel":
            sub.description = (
                "Force-cancel one GitHub Actions workflow run that stayed non-completed after ordinary cancellation.\n"
                "Use only after run-cancel; refuses completed runs and runs with no cancellation evidence."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} read run --repo unit/public --number 12345678901\n"
                f"  .venv/bin/python -m {_PUBLISHER} run-force-cancel --repo unit/public --number 12345678901\n"
                "Outputs: verifies the run is not completed and shows prior cancellation (status cancelling,\n"
                "  conclusion cancelled, or at least one completed/cancelled or cancelling job), then POSTs\n"
                "  repos/<owner>/<name>/actions/runs/<number>/force-cancel.\n"
                "  Request acceptance does not prove terminal cancellation; read back the run state.\n"
                "  Cancellation does not satisfy CI or merge gates and does not consume or reset recovery allowances.\n"
                "Exit codes: 0 request accepted, terminal state unverified; 1 GitHub API failure;\n"
                "  2 invalid input, preflight refusal, publishing refusal or transport error; 75 rate-limit refusal.\n"
                "Related: docs/dev/agent-public-text.md; read run; read jobs; run-cancel."
            )
        if verb == "run-rerun":
            sub.description = (
                "Rerun only failed jobs in an existing GitHub Actions run (or the whole run when a\n"
                "completed all-green PR run has an incoherent check row).\n"
                "Use an Actions run ID, not a PR number. After a base-branch fix, use pr-update-branch instead."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} read run --repo unit/public --number 12345678901\n"
                f"  .venv/bin/python -m {_PUBLISHER} run-rerun --repo unit/public --number 12345678901\n"
                "Outputs: requests a GitHub rerun with --failed on the original pinned run, not the latest PR head.\n"
                "  PR recovery requires an eligible failed run, current-head approval and posted failure evidence.\n"
                "  It consumes the one PR-head recovery allowance for reruns before sending the write.\n"
                "  When a completed all-green PR run has every job and step completed with passing conclusions and\n"
                "  valid completed_at, and at least one check row is incoherent (non-completed status such as\n"
                "  in_progress while conclusion and completed_at are set), and posted evidence names those exact\n"
                "  incoherent job IDs, run-rerun omits --failed to rerun all jobs on the same head using the same\n"
                "  one-per-head allowance. Any failing job keeps --failed; a coherent all-green run is refused with\n"
                "  RECOVERY_RUN_INVALID (nothing to recover).\n"
                "  A completed cancelled merge_group run of the configured CI workflow with no failing job or step and\n"
                "  no ci-queue-candidate artifact recovers instead through the same one-per-head allowance by enqueueing\n"
                "  once at the approved PR head after the merge-readiness gates pass and the posted evidence carries\n"
                "  diagnosis=\"infrastructure\" and undermines_approval=false (when still queued, it refuses with\n"
                "  RECOVERY_STILL_QUEUED without consuming the allowance so the owner can run pr-dequeue manually).\n"
                "  An unclassified run lookup failure stays RECOVERY_LOOKUP_UNKNOWN, sends no write and consumes no allowance.\n"
                "Exit codes: 0 request accepted, job results unverified; 1 gh failure;\n"
                "  2 invalid input, unknown lookup, policy refusal or transport error.\n"
                "  The project GitHub client returns 75 for a rate-limit refusal at either stage:\n"
                "    During any preflight lookup: no rerun request; no allowance consumed.\n"
                "    At the rerun request: PR recovery has already consumed its allowance; no refund.\n"
                "    A later retry for that PR head exits 2 with RECOVERY_ALLOWANCE_SPENT.\n"
                "  Other gh exit codes propagate.\n"
                "Related: docs/dev/agent-public-text.md; read run; pr-update-branch."
            )
        if verb == "issue-transfer":
            sub.description = (
                f"Move one open issue between two repositories registered in {_FLEET_REPOS}\n"
                "that share an owner, through one GitHub transferIssue mutation. Raw gh issue transfer stays refused.\n"
                "Use for approved confidentiality or ownership moves only; never for bulk moves or private-to-public."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} issue-transfer --repo public --number 123 "
                "--destination infra-private --dry-run\n"
                f"  .venv/bin/python -m {_PUBLISHER} issue-transfer --repo learn-ukrainian/learn-ukrainian.github.io "
                "--number 123 --destination infra-private --receipt-dir /abs/private/dir\n"
                "Checks, in order, before any mutation: both endpoints registered (alias or owner/name), same owner,\n"
                "  different repositories, never a private- role to a public- role; then fresh reads: each\n"
                "  repository's live name, visibility (PRIVATE or PUBLIC as its role says), write permission and\n"
                "  archive state; the source number resolves to an open issue, not a pull request, in the source.\n"
                "  If the source number already resolves to an issue in the destination, the verb reports it and\n"
                "  exits 0 without a mutation; a redirect to any other repository is refused.\n"
                "Outputs: one transferIssue mutation with createLabelsIfMissing false (labels missing at the\n"
                "  destination are dropped; create them first with label-create). The receipt JSON goes to stdout\n"
                "  and to a new 0600 file in the receipt directory: outcome, source and destination URLs, node ids,\n"
                "  source labels, labels dropped (from complete label reads; predicted_dropped on a dry run), and\n"
                "  credential-free anonymous HTTP probes of both URLs with final URL and status or error (none on\n"
                "  a dry run). A status alone does not prove confidentiality: GitHub shows a transfer banner at\n"
                "  the old URL. Label reads are paginated, count-checked and bound to the issue's node id.\n"
                "  After the mutation, the source redirect and labels are re-read up to eight times over about\n"
                "  180 s, because GitHub can take over a minute to serve them; verification_reads counts them.\n"
                "  A refusal after the static checks leaves a receipt with outcome refused.\n"
                "Exit codes: 0 transferred and verified, already transferred, or dry run passed;\n"
                "  2 invalid input or refusal, no mutation sent; 3 mutation sent but its result is ambiguous or\n"
                "  unverified (including a failed anonymous probe or any later local error): never retry; read the\n"
                "  state only with --dry-run, because a plain re-run sends a new mutation if the issue did not move;\n"
                "  75 rate-limit refusal before the mutation.\n"
                "Related: decision C-4682; machinery-move contract v3 step 10; label-create; read issue."
            )
        if verb == "repo-create":
            sub.description = (
                "Create one auto-initialized private fixture repository for the repository-move rehearsal.\n"
                "Use only from an operator-run script; dispatched workers and credential overrides are refused.\n"
                "Never for real repositories: names of registered repositories are refused."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                "  export LU_PRODUCT_ROOT=/abs/path/to/product-checkout\n"
                f"  .venv/bin/python -m {_PUBLISHER} repo-create --repo infra-private --name move-rehearsal-1 "
                "--principal octo-admin --dry-run\n"
                f"  .venv/bin/python -m {_PUBLISHER} repo-create --repo infra-private --name move-rehearsal-1 "
                "--principal octo-admin\n"
                f"The --repo row of {_FLEET_REPOS} (alternate_github, repository_id, private) names both\n"
                "  organizations: the fixture is <github owner>/<name>, and its exact rehearsal destination is\n"
                "  <alternate_github owner>/<name>-moved (owner and name change, like the move itself).\n"
                "Checks before the mutation: LU_PRODUCT_ROOT names the product checkout, which the private\n"
                "  directory below must stay outside (unset: exit 2); no worker marker; none of GH_TOKEN,\n"
                "  GITHUB_TOKEN, GH_ENTERPRISE_TOKEN, GITHUB_ENTERPRISE_TOKEN, GH_CONFIG_DIR, XDG_CONFIG_HOME set;\n"
                "  GH_HOST unset or github.com; HOME exactly matches the account's password-database home (gh reads its stored\n"
                "  login, and the client its rate-limit budget, through it); GET /user is --principal; the principal\n"
                "  is an active admin of the source organization. Both name lookups must return no visible collision.\n"
                "  One run per fixture name: a lock in the private directory is held from the first read to settlement.\n"
                + _DESTINATION_OWNER_PRECONDITION
                + "Outputs: one POST orgs/<owner>/repos (private, auto_init), a readback of the same private repository,\n"
                "  and GET /user to verify the same login and account id (a dry run re-reads GET /user after the name\n"
                "  lookups the same way). Receipt JSON on stdout and in a new 0600 file under\n"
                "  ~/.local/state/learn-ukrainian/repo-admin/ of the running account (its password-database home;\n"
                "  $HOME and $XDG_STATE_HOME are ignored, so every run finds the same journal): repository id and slug,\n"
                "  rehearsal_destination, principal before and after. repo-transfer --create-receipt takes that file.\n"
                "Exit codes: 0 created and verified, or dry run passed (no mutation); 2 invalid input or refusal, no\n"
                "  mutation, GitHub's client-error answer to the POST included (HTTP 400, 401, 403, 404, 409, 410\n"
                "  or 422 with a message: github_refusal in the receipt names its status and message, and\n"
                "  mutations_sent stays 0); 3 UNSETTLED: the POST was sent and its result is unknown (a timeout,\n"
                "  a 5xx or any other answer) or unverified (an output\n"
                "  failure after the POST included), or another run holds the lock (in-progress); never\n"
                "  retry with the same name: check the organization on GitHub and use a new name; a rate limit at\n"
                "  the POST is 3; a dry run whose final GET /user fails or names another login or id is 3\n"
                "  (unverified, no mutation);\n"
                "  75 a rate limit refused a read before the POST.\n"
                "Related: decision GH-ADMIN-VERBS-1124; repo-transfer; docs/runbooks/write-worker-boundary.md."
            )
        if verb == "repo-transfer":
            sub.description = (
                "Transfer one registered dual-slug repository to its alternate organization, exactly once.\n"
                "Use only from an operator-run script for the approved move or its rehearsal; workers are refused."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                "  export LU_PRODUCT_ROOT=/abs/path/to/product-checkout\n"
                f"  .venv/bin/python -m {_PUBLISHER} repo-transfer --repo infra-private --principal octo-admin "
                "--create-receipt /abs/state/learn-ukrainian/repo-admin/repo-create-move-rehearsal-1-<time>.json\n"
                f"  .venv/bin/python -m {_PUBLISHER} repo-transfer --repo infra-private --principal octo-admin --dry-run\n"
                f"  .venv/bin/python -m {_PUBLISHER} repo-transfer --repo machinery --principal octo-admin\n"
                "Move: source and destination are the --repo row's github and alternate_github slugs, the id is its\n"
                "  repository_id, its visibility matches the row (public or private) and must stay unchanged, and\n"
                "  LU_FLEET_REPO_SELECTOR_FILE must still name the github slug (else refused).\n"
                "Rehearsal (--create-receipt, private rows only): the repo-create receipt from the private state\n"
                "  directory binds the fixture's id and its exact <name>-moved destination; registered ids and names\n"
                "  are refused.\n"
                "Checks before the mutation, under one lock per repository id held to settlement: the repo-create\n"
                "  caller checks; first the exact destination: the id there, with the row's visibility, exits 0 as\n"
                "  already-transferred with no mutation and clears the journal (a dry run leaves it), but a journal\n"
                "  can settle only when its original visibility matches the row and destination and its principal\n"
                "  matches this run's login and account id. Missing or mismatched bindings exit 3 and keep the\n"
                "  journal for reconciliation, as does any failed check while the journal exists.\n"
                "  Every exit 0 with no POST (already transferred, journal or not, and a dry run) and every journal\n"
                "  removal (a refusal before the POST included) re-reads GET /user after its last GitHub read and\n"
                "  requires the bound login and account id; a failed or mismatched re-read exits 3 and keeps the journal.\n"
                "  A new send requires no visible destination collision, an unarchived source with the pinned id and\n"
                "  row visibility, source admin permission, and active destination admin membership. The source slug\n"
                "  and visibility are re-read just before the POST.\n"
                + _DESTINATION_OWNER_PRECONDITION
                + "GitHub addresses a transfer by slug only, so nobody else may rename, transfer\n"
                "  or create repositories in the source organization during the run.\n"
                "Outputs: one POST repos/<source>/transfer {new_owner, new_name}. GitHub executes transfers\n"
                "  asynchronously (202 Accepted with minimal-repository, where visibility may be omitted and\n"
                "  full_name may still name the source or an intermediate owner/name pair): the POST answer must\n"
                "  name the pinned id and the row's visibility (private matching the row, and visibility matching if\n"
                "  present). repo-transfer then polls GET repos/<destination> for the exact pinned repository (id,\n"
                "  full_name, private, visibility matching the row) across 8 fresh reads over a bounded 180 s wait\n"
                "  (backoff 0, 2, 4, 8, 16, 30, 60, 60 s), stopping immediately if the pinned repository is observed\n"
                "  at the destination with mismatched visibility, then re-reads GET /user after the last GitHub read to\n"
                "  verify the same login and account id. Only after that bound (or on a mismatched POST answer, observed\n"
                "  destination visibility mismatch, or final GET /user) does it exit 3 (unverified) and keep the\n"
                "  journal. Receipt JSON on stdout and in a new 0600 file; journal repo-transfer-<id>.pending there\n"
                "  from just before the POST until verified.\n"
                "Exit codes: 0 transferred and verified, already transferred, or dry run passed (a journal that could\n"
                "  not be removed is named in the receipt); 2 invalid input, a caller or registry refusal, or a\n"
                "  --create-receipt that cannot be loaded (the id is then unknown), or a refusal with no mutation sent\n"
                "  and no journal for this repository id, GitHub's client-error answer to the POST included (HTTP\n"
                "  400, 401, 403, 404, 409, 410 or 422 with a message: github_refusal in the receipt names its status\n"
                "  and message, mutations_sent stays 0, and the journal is removed after the same GET /user\n"
                "  re-read as a refusal before the POST); 3 UNSETTLED: the POST was sent and the result is unknown\n"
                "  (a timeout, a 5xx or any other answer) or\n"
                "  unverified (an output failure after the POST included); a journal for this repository id exists or\n"
                "  cannot be read and the destination does not settle it, or it binds another login or account id,\n"
                "  whichever later step fails (unreconciled: this run sent no request; an earlier one may have, and\n"
                "  the journal is kept for reconciliation). A failed or mismatched GET /user before an exit 0 that\n"
                "  sends no POST, or before removing a journal written when no POST was sent or GitHub refused it,\n"
                "  means no mutation was carried out (unverified when there is no journal, else unreconciled and that\n"
                "  journal is kept). The same\n"
                "  GET /user after a POST is exit 3 unverified: a request was sent and could not be confirmed,\n"
                "  and the journal is kept for reconciliation;\n"
                "  a refused run could not remove its own journal (journal-retained); or another run holds the lock\n"
                "  (in-progress). Journals are per repository id: the rehearsal's never blocks the move.\n"
                "  Never retry: rerun only to reconcile, as the principal that started the transfer (it exits 0 once\n"
                "  the id is at the destination); if GitHub still shows it at the source, confirm no transfer is\n"
                "  pending and remove the journal by hand. Do not flip the selector until reconciled. A rate limit at\n"
                "  the POST is 3;\n"
                "  75 a rate limit refused a read before the POST.\n"
                "Related: decision GH-ADMIN-VERBS-1124; repo-create; lu_machinery/config/fleet_repos.yaml."
            )
        if verb == "ruleset-merge-queue":
            sub.description = (
                "Change named merge_queue parameters on one existing repository ruleset.\n"
                "Use only for those parameters. This is not a ruleset editor: a name, enforcement,\n"
                "condition, bypass actor, other rule, or unknown field is refused."
            )
            sub.formatter_class = argparse.RawDescriptionHelpFormatter
            sub.epilog = (
                "Examples:\n"
                f"  .venv/bin/python -m {_PUBLISHER} ruleset-merge-queue --repo unit/public --ruleset 20977023 "
                "--min-entries-to-merge-wait-minutes 10\n"
                "Outputs: reads the ruleset, replaces only the named merge_queue parameters that already exist\n"
                "  on its single merge_queue rule, PUTs the writable ruleset fields, reads the ruleset again,\n"
                "  and prints JSON {repo, ruleset, merge_queue} from that read-back. Other ruleset fields are\n"
                "  echoed, not edited. An unrecognized ruleset field refuses before the PUT.\n"
                "Exit codes: 0 update sent and the read-back matches; 1 GitHub refused the update with a 4xx\n"
                "  answer (nothing landed); 2 invalid input or a refusal before the update; 3 the update may have\n"
                "  landed and is unverified (a timeout, transport fault or server error on the update, or a failed\n"
                "  or mismatched read-back): do not retry. A rate limit on that read-back is exit 3, not 75;\n"
                "  75 is a rate-limit refusal of the update itself (nothing landed).\n"
                "Related: GitHub REST update a repository ruleset; decision C-5345."
            )
        for key, kind in schema.items():
            kwargs = {"default": argparse.SUPPRESS, "help": f"{key.replace('_', ' ')} ({kind}); omitted by default"}
            if verb == "repo-fork":
                kwargs["action"] = StoreOnce
                kwargs["help"] = "Required GitHub owner/name, such as kube-dojo/kubedojo-labs. No default."
            if verb == "issue-create" and key == "parent":
                kwargs["help"] = (
                    "Open registered epic number, such as 123. Use a local epic when the repository has roots. "
                    "Otherwise use a default-repository epic when both repositories are public and share a GitHub owner. "
                    "Required for ordinary issues in repositories that declare a registry section."
                )
            if verb == "pr-create" and key == "milestone":
                kwargs["help"] = (
                    "Open repository milestone title or number (e.g. 'v0.11.0 - CI and GitHub budget reliability' or '13'); "
                    "omitted inherits the linked closing issue's milestone if resolved"
                )
            if verb == "issue-create" and key == "epic":
                kwargs["help"] = (
                    "CTO seat only. Create a root in the stream registry repository, with no --parent "
                    "and a title like '[EPIC][my-stream] Coordination'. Default off."
                )
            if verb == "issue-transfer":
                kwargs["help"] = {
                    "repo": "Required source: registry alias (e.g. public) or owner/name; no GH_REPO or origin default",
                    "number": "Required source issue number (e.g. 123); a pull request number is refused",
                    "destination": "Required destination registry alias (e.g. infra-private); owner/name is refused",
                    "receipt_dir": (
                        "Absolute private directory for the receipt, created 0700 and outside the repository; "
                        "default $XDG_STATE_HOME/learn-ukrainian/issue-transfer (else ~/.local/state/...)"
                    ),
                    "dry_run": "Run every check and the label analysis, write the receipt; no mutation, no probe; default off",
                }[key]
            if verb in {"repo-create", "repo-transfer"}:
                if kind != "bool":
                    kwargs["action"] = StoreOnce
                kwargs["help"] = {
                    "repo": (
                        "Required registry key of a dual-slug private row (e.g. infra-private); no default"
                        if verb == "repo-create"
                        else "Required registry key of a dual-slug row (e.g. infra-private or machinery); no default"
                    ),
                    "name": (
                        "Required fixture name, lowercase letters, digits and hyphens (e.g. move-rehearsal-1). "
                        f"At most {_GITHUB_REPOSITORY_NAME_LIMIT - len(_REHEARSAL_SUFFIX)} characters."
                    ),
                    "principal": "Required GitHub login the gh credential must authenticate as (e.g. octo-admin)",
                    "create_receipt": (
                        "Absolute path of a repo-create receipt for the rehearsal (private rows only); "
                        "omitted transfers the row itself"
                    ),
                    "dry_run": "Run every check and write the receipt; no mutation; default off",
                }[key]
            if verb == "comment-edit":
                kwargs["action"] = StoreOnce
                kwargs["help"] = {
                    "repo": "Repository owner/name (e.g. unit/public); defaults to GH_REPO or Git origin",
                    "comment_id": "Required positive comment ID (e.g. 123456), not an issue or PR number; no default",
                    "body": "Exact UTF-8 body text; use exactly one of --body or --body-file; no default",
                    "body_file": "UTF-8 body file (e.g. reply.md), or - for stdin; no default",
                }[key]
            if verb == "ruleset-merge-queue":
                kwargs["help"] = {
                    "repo": "Repository owner/name (e.g. unit/public); defaults to GH_REPO or Git origin",
                    "ruleset": "Required existing ruleset ID (e.g. 20977023); no default",
                    "check_response_timeout_minutes": "Minutes for a required check to finish, from 1 to 360; omitted keeps the current value",
                    "grouping_strategy": "ALLGREEN or HEADGREEN; omitted keeps the current value",
                    "max_entries_to_build": "Queued pull requests built at once, from 0 to 100; omitted keeps the current value",
                    "max_entries_to_merge": "Maximum pull requests merged as one group, from 0 to 100; omitted keeps the current value",
                    "merge_method": "MERGE, SQUASH or REBASE; omitted keeps the current value",
                    "min_entries_to_merge": "Minimum group size, from 0 to 100; omitted keeps the current value",
                    "min_entries_to_merge_wait_minutes": "Minutes to wait for that minimum, from 0 to 360; omitted keeps the current value",
                }[key]
            if verb == "code-scanning-dismiss":
                kwargs["action"] = StoreOnce
                kwargs["help"] = {
                    "repo": "Repository owner/name, e.g. unit/public; defaults to GH_REPO or origin",
                    "number": "Required positive alert number, e.g. 1; no default",
                    "reason": "Required reviewed dismissal reason; separate from issue-close reasons",
                    "comment": "Required explanation identifying Alert #N and linking one same-repository merged PR; scanned verbatim",
                }[key]
            if verb in {"code-scanning-analysis-delete", "branch-delete"}:
                kwargs["action"] = StoreOnce
                kwargs["help"] = {
                    "repo": "Required repository owner/name, e.g. unit/public; no GH_REPO or origin default",
                    "analysis_id": "Required positive code-scanning analysis ID, e.g. 41; no default",
                    "branch": "Required exact branch name without refs/heads/, e.g. salvage/old-attempt; no default",
                }[key]
            if verb in {"run-rerun", "run-cancel", "run-force-cancel"}:
                if verb == "run-force-cancel":
                    kwargs["action"] = StoreOnce
                kwargs["help"] = {
                    "repo": "Repository owner/name (e.g. unit/public); defaults to GH_REPO or Git origin",
                    "number": (
                        "Required Actions run ID from /actions/runs/<RUN_ID> (e.g. 12345678901), "
                        "not a PR number; no default"
                    ),
                }[key]
            if verb == "pr-merge" and key == "expect_queue":
                kwargs["help"] = (
                    "true or false: the merge queue state the landing gate was checked for; "
                    "refused before any write when the base branch differs; omitted skips the check"
                )
            if verb == "pr-merge" and key == "recovery_run_id":
                kwargs["help"] = (
                    "Positive failed merge_group Actions run ID for same-head infrastructure re-enqueue; "
                    "atomically reserves the PR head's one recovery allowance; omitted for ordinary landing"
                )
            if kind == "bool":
                kwargs["action"] = argparse.BooleanOptionalAction
            elif kind in {"texts", "assets"}:
                kwargs["action"] = "append"
                if kind == "assets":

                    def asset_arg(value):
                        try:
                            return Asset(value)
                        except gate.PublishBlocked:
                            raise argparse.ArgumentTypeError("invalid artifact") from None

                    kwargs["type"] = asset_arg
            elif kind == "number":

                def _parse_verb_number(raw: str) -> int:
                    from lu_machinery.orchestration.fleet_repos import parse_ascii_positive_int

                    parsed = parse_ascii_positive_int(raw)
                    if parsed is None:
                        raise argparse.ArgumentTypeError(f"invalid positive ASCII number: {raw!r}")
                    return parsed

                kwargs["type"] = _parse_verb_number
            elif kind == "count":
                kwargs["type"] = int
            if kind == "workflow_inputs":
                kwargs["type"] = loads
            if kind in ENUMS:
                kwargs["choices"] = sorted(ENUMS[kind])
            sub.add_argument("--" + key.replace("_", "-"), **kwargs)
    read_parser = verbs.add_parser(
        "read",
        help="Named REST and GraphQL reads",
        allow_abbrev=False,
        description=(
            "Read named GitHub resources. Diagnostic reads return JSON arrays per page; "
            "code-scanning-alerts lists open alerts for a PR (--number) or Git ref (--ref). "
            "dependabot-alerts reads all open alerts from the physical checkout's approved origin on github.com, "
            "takes no selectors, and emits one complete JSON array with only number, state, package, "
            "ecosystem, manifest, GHSA, severity, affected range and fixed version (possibly null). "
            "It rejects withdrawn advisories and malformed responses; failures emit no alert rows. "
            f"Example: .venv/bin/python -m {_PUBLISHER} read dependabot-alerts"
        ),
    )
    read_parser.add_argument(
        "name",
        choices=sorted(
            set(REST_READS) | set(GQL_READS) | set(REST_COMPAT_READS) | {"queue-snapshot", "issue-count"}
        ),
    )
    read_parser.add_argument(
        "--repo",
        help=(
            "Repository owner/name (e.g. unit/public); defaults to GH_REPO or Git origin. "
            "For code-scanning-alerts and check-annotations, admission allows only the checkout's origin repository."
            " dependabot-alerts requires a physically proved catalog checkout and its origin on github.com; "
            "repository labels only confirm that identity, and inherited Git overrides cannot select it."
        ),
    )
    def _parse_positive_number_arg(raw: str) -> int:
        from lu_machinery.orchestration.fleet_repos import parse_ascii_positive_int

        parsed = parse_ascii_positive_int(raw)
        if parsed is None:
            raise argparse.ArgumentTypeError(f"invalid positive ASCII number: {raw!r}")
        return parsed

    for key in ("number",):
        read_parser.add_argument(
            "--" + key,
            type=_parse_positive_number_arg,
            default=argparse.SUPPRESS,
            help="Positive issue, PR, run or check-run ID; omitted by default",
        )
    for key in ("sha", "base", "head", "start", "end", "branch", "cursor", "ref"):
        read_parser.add_argument(
            "--" + key,
            default=argparse.SUPPRESS,
            help=(
                f"{key.capitalize()} commit SHA for compare (40 hexadecimal characters); omitted by default"
                if key in {"base", "head"}
                else "Git ref for code-scanning-alerts (e.g. refs/heads/main); use either --ref or --number"
                if key == "ref"
                else f"{key} selector; omitted by default"
            ),
        )
    for key in ("branches", "cursors", "body_roots", "numbers", "batch"):
        read_parser.add_argument(
            "--" + key.replace("_", "-"),
            type=loads,
            default=argparse.SUPPRESS,
            help=f"{key} as JSON; omitted by default",
        )
    read_parser.add_argument(
        "--paginate", action="store_true", help="Read all pages (default off; diagnostic reads always paginate)"
    )
    read_parser.add_argument(
        "--slurp",
        action="store_true",
        help="Wrap paginated pages in an array (default off; unsupported for shaped diagnostic reads)",
    )
    return vars(parser.parse_args(argv))


def _run_args(args, *, runner=None):
    verb = args.pop("verb")
    try:
        if verb == "read":
            return read(args.pop("name"), runner=runner, **args).returncode
        return publish(verb, runner=runner, **args).returncode
    except gate.PublishBlocked as exc:
        message = str(exc)
    except Exception:
        message = "OPSEC: publisher transport unavailable."
    if verb in REPO_ADMIN_VERBS:
        with _repo_admin_run():
            _stderr_note(message)
    else:
        print(message, file=sys.stderr)
    return 2


class Request:
    """A named closed operation for existing adapters with injected transports."""

    def __init__(self, verb, **fields):
        self.verb, self.fields = verb, fields


def request_run(request, *, runner=None, input=None, **kwargs):
    """Adapt typed requests; raw argv retains the write and exposure controls."""
    if not isinstance(request, Request):
        return gate.checked_run(request, runner=runner, input=input, **kwargs)
    if input is not None:
        raise gate.PublishBlocked("OPSEC: typed requests cannot take arbitrary transport input.")
    if request.verb.startswith("read-"):
        return read(request.verb[5:], runner=runner, **kwargs, **request.fields)
    return publish(request.verb, runner=runner, **kwargs, **request.fields)


REST_READS = {
    "identity": ("user", {}),
    "issue": ("repos/{repo}/issues/{number}", {"number": "number"}),
    "comments": ("repos/{repo}/issues/{number}/comments", {"number": "number"}),
    "labels": ("repos/{repo}/issues/{number}/labels", {"number": "number"}),
    "timeline": ("repos/{repo}/issues/{number}/timeline", {"number": "number"}),
    "reviews": ("repos/{repo}/pulls/{number}/reviews", {"number": "number"}),
    "commits": ("repos/{repo}/pulls/{number}/commits", {"number": "number"}),
    "commit": ("repos/{repo}/commits/{sha}", {"sha": "sha"}),
    "branch": ("repos/{repo}/branches/{branch}", {"branch": "ref"}),
    "compare": ("repos/{repo}/compare/{base}...{head}", {"base": "sha", "head": "sha"}),
    "pull": ("repos/{repo}/pulls/{number}", {"number": "number"}),
    "pr-files": ("repos/{repo}/pulls/{number}/files", {"number": "number"}),
    "comment": ("repos/{repo}/issues/comments/{number}", {"number": "number"}),
    "checks": ("repos/{repo}/commits/{sha}/check-runs", {"sha": "sha"}),
    "code-scanning-alerts": ("repos/{repo}/code-scanning/alerts?state=open&ref={ref}", {"ref": "ref"}),
    "check-annotations": ("repos/{repo}/check-runs/{number}/annotations", {"number": "number"}),
    "dependabot-alerts": ("repos/{repo}/dependabot/alerts?state=open", {}),
    "jobs": ("repos/{repo}/actions/runs/{number}/jobs", {"number": "number"}),
    "run": ("repos/{repo}/actions/runs/{number}", {"number": "number"}),
    "run-artifacts": ("repos/{repo}/actions/runs/{number}/artifacts", {"number": "number"}),
    "artifact-archive": ("repos/{repo}/actions/artifacts/{number}/zip", {"number": "number"}),
    "issues": ("repos/{repo}/issues?state=open", {}),
    "milestones": ("repos/{repo}/milestones?state=all", {}),
    "runs": ("repos/{repo}/actions/runs?event=merge_group&created={start}..{end}", {"start": "timestamp", "end": "timestamp"}),
    "head-runs": ("repos/{repo}/actions/runs?head_sha={sha}", {"sha": "sha"}),
    "deployments": ("repos/{repo}/deployments?sha={sha}", {"sha": "sha"}),
    "deployment-statuses": ("repos/{repo}/deployments/{number}/statuses", {"number": "number"}),
}
# Fixed projections omit account/host metadata and refuse non-relative file paths.
# gh applies --jq to each page; diagnostic reads emit one JSON array per page.
_RELATIVE_PATH_JQ = r"""def relative_path:
    if type == "string" and length > 0
       and (test("(^/|[\\\\:\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}]|(^|/)\\.\\.?(/|$)|//)") | not)
    then . else error("OPSEC: diagnostic file path is not repository-relative") end;
"""
DIAGNOSTIC_READS = {
    "code-scanning-alerts": _RELATIVE_PATH_JQ
    + """
        map({rule_id: .rule.id,
            severity: (.rule.security_severity_level // .rule.severity),
            state: (.most_recent_instance.state // .state),
            file: (.most_recent_instance.location.path | relative_path),
            start_line: .most_recent_instance.location.start_line,
            message: .most_recent_instance.message.text,
            html_path: (.html_url |
                if test("^https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(security/)?code-scanning/[0-9]+$")
                then split("/")[5:] | join("/") | relative_path
                else error("OPSEC: invalid diagnostic html path") end)})""",
    "check-annotations": _RELATIVE_PATH_JQ
    + """
        map({path: (.path | relative_path), line: .start_line,
            level: .annotation_level, message: .message})""",
    "dependabot-alerts": _RELATIVE_PATH_JQ
    + r"""
        def token: type == "string" and length > 0
            and test("^[A-Za-z0-9@][A-Za-z0-9._+:/~!-]*$")
            and (contains("://") | not);
        def version: type == "string" and length > 0
            and test("^[A-Za-z0-9*<>=~^|, ._+!:/-]+$")
            and (contains("://") | not);
        if type != "array" then error("OPSEC: invalid Dependabot alert page") else
        map(
            if type != "object" then error("OPSEC: invalid Dependabot alert") else . end |
            .security_advisory as $advisory | .security_vulnerability as $vulnerability |
            if (.number | type == "number" and . > 0 and floor == .)
                and .state == "open"
                and (.dependency.package.name | token)
                and (.dependency.package.ecosystem | token)
                and .dependency.package == $vulnerability.package
                and ($advisory | type == "object" and has("withdrawn_at"))
                and $advisory.withdrawn_at == null
                and ($advisory.ghsa_id | type == "string" and test("^GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}$"))
                and ($vulnerability.severity | IN("low", "medium", "high", "critical"))
                and ($vulnerability.vulnerable_version_range | version)
                and ($vulnerability | has("first_patched_version"))
                and ($vulnerability.first_patched_version == null
                    or ($vulnerability.first_patched_version.identifier | version))
            then {
                number: .number, state: .state,
                package: .dependency.package.name, ecosystem: .dependency.package.ecosystem,
                manifest: (.dependency.manifest_path | relative_path),
                ghsa: $advisory.ghsa_id, severity: $vulnerability.severity,
                affected_version_range: $vulnerability.vulnerable_version_range,
                fixed_version: $vulnerability.first_patched_version.identifier
            } else error("OPSEC: invalid or withdrawn Dependabot alert") end
        ) end""",
}
REST_COMPAT_READS = {
    "budget": set(),
    "default-head": set(),
    "pr-bases": {"cursor"},
    "issue-states": {"numbers"},
    "merge-facts": {"batch"},
    "issue-parent": {"number"},
    "issue-scope": {"number"},
    "subissues": {"number"},
    "subissues-next": {"number", "cursor"},
    "subissue-batch": {"cursors", "body_roots"},
}
GQL_READS = {
    "fork-public-head": (
        "query($owner:String!,$name:String!){viewer{login}"
        "repository(owner:$owner,name:$name,followRenames:false){nameWithOwner isFork"
        " parent{nameWithOwner visibility defaultBranchRef{target{... on Commit{oid}}}}}}"
    ),
    "queue-removal": "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid removals:timelineItems(first:100,after:$cursor,itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){totalCount pageInfo{hasNextPage endCursor} nodes{... on RemovedFromMergeQueueEvent{beforeCommit{oid} createdAt reason}}} pushes:timelineItems(last:1,itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT]){nodes{... on HeadRefForcePushedEvent{afterCommit{oid} createdAt}}}}}}",
    "queue-status": "\nquery($owner: String!, $name: String!, $number: Int!, $branch: String!) {\n  repository(owner: $owner, name: $name) {\n    pullRequest(number: $number) {\n      number\n      title\n      state\n      merged\n      mergeable\n      mergeStateStatus\n      isInMergeQueue\n      isMergeQueueEnabled\n      headRefName\n      headRefOid\n      baseRefName\n      mergeQueueEntry {\n        id\n        position\n        state\n        enqueuedAt\n        estimatedTimeToMerge\n        jump\n        solo\n        headCommit {\n          oid\n          checkSuites(first: 20) {\n            nodes {\n              status\n              conclusion\n              createdAt\n              updatedAt\n              workflowRun {\n                id\n                url\n                event\n                createdAt\n                updatedAt\n                workflow {\n                  name\n                }\n              }\n            }\n          }\n        }\n      }\n    }\n    mergeQueue(branch: $branch) {\n      url\n      nextEntryEstimatedTimeToMerge\n      entries(first: 50) {\n        totalCount\n        nodes {\n          position\n          state\n          enqueuedAt\n          estimatedTimeToMerge\n          pullRequest {\n            number\n          }\n        }\n      }\n    }\n  }\n}\n",
    "membership-head": "query($owner:String!,$name:String!,$number:Int!,$branch:String!){repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid isInMergeQueue} mergeQueue(branch:$branch){url}}}",
    "membership": "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){isInMergeQueue}}}",
    "squash-text": "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid viewerMergeHeadlineText(mergeType:SQUASH) viewerMergeBodyText(mergeType:SQUASH) mergeQueueEntry{headCommit{oid message}}}}}",
}


def _dependabot_repository(cwd, environment, explicit):
    """Bind the nearest physical checkout to the existing repository catalog.

    Git links prove checkout membership; catalog locations supply authority.
    The local origin is only a consistency check, never an authority source.
    Return an identity snapshot to recheck before sending and exposing rows.
    """
    try:
        if ".." in Path(cwd).parts:
            raise ValueError
        folder = Path(os.path.abspath(cwd))
        paths = {folder, *folder.parents}
        if any(not stat.S_ISDIR(path.lstat().st_mode) for path in paths):
            raise ValueError
        checkout = next(path for path in (folder, *folder.parents) if os.path.lexists(path / ".git"))
        primary = resolve_main_root_without_git(checkout)
        # Installed code lives in site-packages, outside the catalog checkouts.
        # Use the declared product, or the caller's physical checkout, as the anchor.
        public = resolve_main_root_without_git(product_root(environment.get(PRODUCT_ROOT_ENV) or checkout))
        matches = [row for row in gate.catalog().values() if public.parent / row["local_name"] == primary]
        if len(matches) != 1:
            raise ValueError
        # The checkout's row names its repository at the row's active slug.
        entry = _fleet_catalog_entry(matches[0]["github"], environment, bind_inactive=True)
        if entry is None:
            raise ValueError
        destination = gate.normalize_repository(entry[0].active_github)
        if destination.split("/", 1)[0] != "github.com":
            raise ValueError
        if environment.get("GH_HOST", "github.com") != "github.com":
            raise ValueError
        for selected in (explicit, environment.get("GH_REPO")):
            if selected is not None and gate.normalize_repository(selected) != destination:
                raise ValueError
        common = primary / ".git"
        metadata = {checkout / ".git", common / "config"}
        if (checkout / ".git").is_file():
            fd = os.open(checkout / ".git", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError
                link = handle.read(65537)
            if len(link) > 65536:
                raise ValueError
            gitdir = link.decode().strip().removeprefix("gitdir:").strip()
            gitdir = Path(os.path.abspath(checkout / gitdir))
            paths.update((gitdir, *gitdir.parents))
            metadata.update((gitdir / "gitdir", gitdir / "commondir"))
        paths.update((common, *common.parents))
        snapshot = []
        for path in sorted(paths | metadata):
            info = path.lstat()
            identity = (str(path), info.st_dev, info.st_ino)
            if stat.S_ISDIR(info.st_mode):
                snapshot.append(identity)
            elif path in metadata and stat.S_ISREG(info.st_mode) and info.st_size <= 1048576:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity[1:]:
                        raise ValueError
                    raw = handle.read(1048577)
                if len(raw) > 1048576:
                    raise ValueError
                snapshot.append((*identity, info.st_mtime_ns, info.st_ctime_ns, hashlib.sha256(raw).hexdigest()))
            else:
                raise ValueError

        def origin_reader(_argv, **_kwargs):
            # Read only the physical repository's own config. Includes, global
            # config, URL rewrites and inherited GIT_CONFIG_* cannot select it.
            return subprocess.run(
                ["git", "config", "--file", str(common / "config"), "--no-includes",
                 "--get-all", "remote.origin.url"],
                cwd=folder, env=environment, capture_output=True, text=True, check=False, timeout=5,
            )

        if repository(folder, {}, reader=origin_reader) != destination:
            raise ValueError
        return destination, tuple(snapshot)
    except (OSError, ValueError, StopIteration, KeyError, TypeError, NotAGitRepositoryError):
        raise gate.PublishBlocked("DEPENDABOT_REPOSITORY_REFUSED: physical checkout identity unproved.") from None


def read(
    operation,
    *,
    repo=None,
    runner=None,
    cwd=None,
    env=None,
    paginate=False,
    slurp=False,
    capture_output=False,
    text=False,
    check=False,
    timeout=None,
    stdout=None,
    stderr=None,
    **fields,
):
    """Specific API reads with fixed GET endpoints or internally built query documents."""
    cwd = Path(cwd or Path.cwd())
    if os.environ.get(BROKER_ENV) and runner is None:
        return _read_through_broker(operation, repo, fields, paginate=paginate, slurp=slurp, cwd=cwd, env=env,
                                    capture_output=capture_output, text=text)
    environment = _validated_environment(env)
    if operation == "dependabot-alerts":
        # All inherited Git state is untrusted, including config injection.
        environment = {key: value for key, value in environment.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        dest, physical_identity = _dependabot_repository(cwd, environment, repo)
        environment.pop("GH_REPO", None)
    else:
        dest = "unknown" if operation == "budget" else repository(cwd, environment, repo)
    if dest == "unknown" and operation not in {"identity", "budget", "merge-facts"}:
        raise gate.PublishBlocked("OPSEC: read repository unresolved.")
    if dest != "unknown":
        environment["GH_HOST"] = dest.split("/", 1)[0]
    gh_repo = dest.split("/", 1)[-1]
    if operation == "dependabot-alerts":
        if timeout is not None and (type(timeout) not in {int, float} or not 0 < timeout <= 30):
            raise gate.PublishBlocked("OPSEC: invalid Dependabot read timeout.")
        timeout = timeout or 30
    if operation == "issue-count":
        # One fixed aggregate read. No selectors, arbitrary queries or text leave this path.
        # A registered row by either baked slug (callers pass the stored one), read at its active slug.
        entry = None if fields else _fleet_catalog_entry(dest, environment, bind_inactive=True)
        if entry is None:
            raise gate.PublishBlocked("OPSEC: invalid issue count repository or fields.")
        dest = gate.normalize_repository(entry[0].active_github)
        owner, name = dest.split("/")[1:]
        query = "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){issues(states:OPEN){totalCount}}}"
        with tempfile.TemporaryDirectory(prefix="lu-issue-count-", dir=ensure_scratch_root()) as directory:
            frozen = Path(directory) / "query.json"
            frozen.write_text(json.dumps({"query": query, "variables": {"owner": owner, "name": name}}))
            result = _send(
                ["gh", "api", "--method", "POST", "graphql", "--input", str(frozen), *_hostname_flag(dest)],
                environment=environment, runner=runner, cwd=cwd, capture_output=True, text=True,
                check=False, timeout=timeout,
            )
        observation = getattr(result, "github_result", None)
        if observation is not None and observation.error == "github_graphql_error":
            raise gate.PublishBlocked("OPSEC: malformed issue count response.")
        if result.returncode == 0:
            try:
                document = github_client.parse_graphql_envelope(result.stdout)
                count = document["data"]["repository"]["issues"]["totalCount"]
                if type(count) is not int or count < 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError, RecursionError):
                raise gate.PublishBlocked("OPSEC: malformed issue count response.") from None
            return github_client._result_process(
                ["github", operation], github_client.Result(value=document), {},
                {"capture_output": capture_output, "text": text, "stdout": stdout, "stderr": stderr, "check": check},
            )
        if check:
            failure = subprocess.CalledProcessError(
                result.returncode, result.args, output=result.stdout, stderr=result.stderr,
            )
            if observation is not None:
                failure.github_result = observation
            raise failure
        return result
    for key in ("number",):
        if key in fields and (type(fields[key]) is not int or fields[key] <= 0):
            raise gate.PublishBlocked("OPSEC: invalid read identifier.")
    if operation in REST_COMPAT_READS:
        if set(fields) != REST_COMPAT_READS[operation]:
            raise gate.PublishBlocked("OPSEC: invalid read fields.")
        if operation == "issue-states" and (not isinstance(fields["numbers"], (list, tuple)) or not 1 <= len(fields["numbers"]) <= 100 or any(type(n) is not int or n <= 0 for n in fields["numbers"])):
            raise gate.PublishBlocked("OPSEC: invalid issue batch.")
        if operation == "merge-facts" and (not isinstance(fields["batch"], (list, tuple)) or not 1 <= len(fields["batch"]) <= 50 or any(not isinstance(item, (list, tuple)) or len(item) != 2 or not isinstance(item[0], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item[0]) or type(item[1]) is not int or item[1] <= 0 for item in fields["batch"])):
            raise gate.PublishBlocked("OPSEC: invalid merge batch.")
        if operation == "subissues-next" and not isinstance(fields["cursor"], str):
            raise gate.PublishBlocked("OPSEC: invalid read cursor.")
        if operation == "subissue-batch":
            cursors, roots = fields["cursors"], fields["body_roots"]
            if not isinstance(cursors, dict) or not 1 <= len(cursors) <= 20 or not isinstance(roots, (set,list,tuple)) or any(type(n) is not int or n <= 0 or (c is not None and not isinstance(c,str)) for n,c in cursors.items()):
                raise gate.PublishBlocked("OPSEC: invalid read batch.")
        return github_client.rest_read(operation, gh_repo, fields, runner=runner, env=environment, cwd=cwd, capture_output=capture_output, text=text, check=check, timeout=timeout, stdout=stdout, stderr=stderr)
    if operation in {"queue-snapshot", "queue-status"} and runner is None:
        expected = {"branches"} if operation == "queue-snapshot" else {"number", "branch"}
        if set(fields) != expected or (operation == "queue-snapshot" and (not isinstance(fields["branches"], (list, set, tuple)) or not all(isinstance(b, str) for b in fields["branches"]))):
            raise gate.PublishBlocked("OPSEC: invalid queue fields.")
        return github_client.queue_read(operation, gh_repo, fields, env=environment, cwd=cwd, capture_output=capture_output, text=text, check=check, timeout=timeout, stdout=stdout, stderr=stderr)
    if operation in REST_READS:
        endpoint, schema = REST_READS[operation]
        if operation == "runs" and "sha" in fields:
            endpoint += "&head_sha={sha}"
            schema = {**schema, "sha": "sha"}
        if operation == "code-scanning-alerts" and set(fields) == {"number"}:
            # PR filtering covers analyses uploaded to either head or merge refs.
            endpoint = "repos/{repo}/code-scanning/alerts?state=open&pr={number}"
            schema = {"number": "number"}
        if set(fields) != set(schema):
            raise gate.PublishBlocked("OPSEC: invalid read fields.")
        for key, kind in schema.items():
            pattern = (
                r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?"
                if kind == "timestamp" else PATTERNS.get(kind)
            )
            if pattern and (not isinstance(fields[key], str) or not re.fullmatch(pattern, fields[key])):
                raise gate.PublishBlocked("OPSEC: invalid read selector.")
        if operation == "code-scanning-alerts" and "ref" in fields:
            if any(segment in {"", ".", ".."} for segment in fields["ref"].split("/")):
                raise gate.PublishBlocked("OPSEC: invalid read selector.")
            fields = {"ref": quote(fields["ref"], safe="")}
        if operation == "branch" and "branch" in fields:
            if any(segment in {"", ".", ".."} for segment in fields["branch"].split("/")):
                raise gate.PublishBlocked("OPSEC: invalid read selector.")
            fields = {"branch": quote(fields["branch"], safe="")}
        endpoint = endpoint.format(repo=gh_repo, **fields)
        endpoint += ("&" if "?" in endpoint else "?") + "per_page=100"
        argv = ["gh", "api", "--method", "GET", endpoint]
        if operation in DIAGNOSTIC_READS:
            if slurp:
                raise gate.PublishBlocked("OPSEC: diagnostic reads return JSON arrays per page; slurp is unsupported.")
            argv += ["--paginate", "--jq", DIAGNOSTIC_READS[operation]]
        elif paginate:
            argv.append("--paginate")
        if slurp and operation not in DIAGNOSTIC_READS:
            if not paginate:
                raise gate.PublishBlocked("OPSEC: slurp requires pagination.")
            argv.append("--slurp")
    else:
        variables = dict(zip(("owner", "name"), gh_repo.split("/", 1), strict=True)) if "/" in gh_repo else {}
        if operation in GQL_READS:
            expected = (
                {"number", "branch"}
                if operation in {"queue-status", "membership-head"}
                else {"number"}
            )
            if operation == "fork-public-head":
                expected = set()
                if dest.split("/", 1)[0] != "github.com" or any(part in {".", ".."} for part in variables.values()):
                    raise gate.PublishBlocked("OPSEC: fork history read requires a GitHub.com repository.")
                if paginate or slurp:
                    raise gate.PublishBlocked("OPSEC: fork history read does not support pagination.")
            if operation == "queue-removal" and "cursor" in fields:
                expected.add("cursor")
            if (
                set(fields) != expected
                or (
                    "cursor" in fields
                    and not isinstance(fields["cursor"], str)
                )
                or ("branch" in fields and not isinstance(fields["branch"], str))
            ):
                raise gate.PublishBlocked("OPSEC: invalid query fields.")
            query = GQL_READS[operation]
            variables.update(fields)
        elif operation == "queue-snapshot" and set(fields) == {"branches"}:
            branches = fields["branches"]
            if not isinstance(branches, (list, tuple, set)) or not all(isinstance(b, str) for b in branches):
                raise gate.PublishBlocked("OPSEC: invalid query branches.")
            aliases = "\n".join(
                f"q{i}: mergeQueue(branch: {json.dumps(branch)}) {{ url }}" for i, branch in enumerate(sorted(branches))
            )
            query = (
                "query($owner:String!,$name:String!){ rateLimit { remaining cost } repository(owner:$owner,name:$name){ pullRequests(first:100,states:OPEN){ totalCount pageInfo{hasNextPage} nodes{ id number title isDraft headRefOid baseRefName isInMergeQueue mergeStateStatus autoMergeRequest{ enabledAt } labels(first:100){totalCount pageInfo{hasNextPage} nodes{name}} } } "
                + aliases
                + "\n}}"
            )
        else:
            raise gate.PublishBlocked("OPSEC: unknown typed read.")
        # Only our query documents reach the API. Variable values are JSON data.
        with tempfile.TemporaryDirectory(prefix="lu-read-") as directory:
            frozen = Path(directory) / "query.json"
            frozen.write_bytes(json.dumps({"query": query, "variables": variables}).encode("utf-8"))
            argv = ["gh", "api", "--method", "POST", "graphql", "--input", str(frozen)]
            argv += _hostname_flag(dest)
            return _send(
                argv,
                environment=environment,
                runner=runner,
                cwd=cwd,
                capture_output=capture_output,
                text=text,
                check=check,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
                **({"fresh": True} if operation == "fork-public-head" and runner is None else {}),
            )
    argv += _hostname_flag(dest)
    if operation == "dependabot-alerts":
        # Buffer every projected page before exposing rows: a later failure must
        # never look like a complete alert inventory. Raw responses stay in gh.
        if _dependabot_repository(cwd, environment, repo) != (dest, physical_identity):
            raise gate.PublishBlocked("DEPENDABOT_IDENTITY_CHANGED: physical checkout changed during read.")
        try:
            directory_fd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise gate.PublishBlocked("DEPENDABOT_REPOSITORY_REFUSED: physical checkout pin unavailable.") from None
        try:
            pinned = Path(f"/proc/self/fd/{directory_fd}")
            opened = os.fstat(directory_fd)
            expected = next(item for item in physical_identity if item[0] == os.path.abspath(cwd))
            if (opened.st_dev, opened.st_ino) != expected[1:] or not pinned.is_dir():
                raise gate.PublishBlocked("DEPENDABOT_IDENTITY_CHANGED: physical checkout pin unproved.")
            # The child changes directory through this open handle before exec;
            # renaming or replacing the caller's pathname cannot redirect it.
            result = _send(
                argv, environment=environment, runner=runner, cwd=pinned,
                capture_output=True, text=True, check=False, timeout=timeout,
            )
        finally:
            os.close(directory_fd)
        if _dependabot_repository(cwd, environment, repo) != (dest, physical_identity):
            raise gate.PublishBlocked("DEPENDABOT_IDENTITY_CHANGED: physical checkout changed during read.")
        if result.returncode:
            refusal = f"OPSEC: API read refused; use python -m {_PUBLISHER} read <name>."
            if (result.stderr or "").strip() == refusal:
                raise gate.PublishBlocked(refusal)
            raise gate.PublishBlocked("OPSEC: Dependabot read failed; no complete alert inventory.")
        try:
            remaining = result.stdout.strip()
            rows, pages = [], 0
            while remaining:
                page, end = json_depth.raw_decode(remaining, max_depth=512, decoder=json.JSONDecoder())
                if not isinstance(page, list):
                    raise ValueError
                rows.extend(page)
                pages += 1
                remaining = remaining[end:].lstrip()
            if not pages:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise gate.PublishBlocked("OPSEC: malformed Dependabot projection.") from None
        return github_client._result_process(
            ["github", operation], github_client.Result(value=rows), {},
            {"capture_output": capture_output, "text": text, "stdout": stdout, "stderr": stderr, "check": check},
        )
    return _send(
        argv,
        environment=environment,
        runner=runner,
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=timeout,
        stdout=stdout,
        stderr=stderr,
    )
