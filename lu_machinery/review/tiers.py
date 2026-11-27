"""The shared path/status classifier for recorded review and landing admission.

The tiers and what each requires before merge are ``agents_extensions/shared/skills/
review-loop/references/policy.md`` section 2; the path lists are
``lu_machinery/config/review_tiers.yaml``. A pull request takes the highest tier any
of its files takes, so one non-Trivial file makes the whole change non-Trivial.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration.integration_sweep import pr_file_paths

ReviewTier = Literal["trivial", "low", "medium", "ukrainian-content", "high"]
_RANK: dict[ReviewTier, int] = {"trivial": 0, "low": 1, "medium": 2, "ukrainian-content": 3, "high": 4}
_MATCH_ORDER: tuple[ReviewTier, ...] = ("high", "ukrainian-content", "trivial", "low")
_STATUSES = {"added", "modified", "removed", "renamed", "copied", "changed", "unchanged"}
_TABLE_KEYS = {"schema_version", "default", "tiers", "release_version_files"}
_KINDS = {"machinery-lock"}

# The product's lu-machinery lock (pip requirements, line-based, no multiline strings):
# its first lines are the lu-machinery requirement and its hashes. A patch whose one
# hunk starts at line 1 and replaces only that block proves, from the file start, that
# nothing but the lu-machinery release changed. Version files whose scope a patch
# cannot prove (pyproject.toml's TOML, package.json's JSON) are not exempt.
_NUMERIC = r"([0-9]+(?:\.[0-9]+){1,3})"
_FIRST_HUNK = re.compile(r"@@ -1(,[0-9]+)? \+1(,[0-9]+)? @@.*")
_LOCK_REQUIREMENT = re.compile(rf"lu-machinery=={_NUMERIC} \\")
_LOCK_HASH = re.compile(r"    --hash=sha256:[0-9a-f]{64}( \\)?")


# Verdict states (``integration_sweep.lookup_verdict``) that mean no review is on record at
# the head. A Trivial head needs none, so it lands on these; a rejection or an unreadable
# record still stops it.
UNREVIEWED_STATES = frozenset({"needs-CF", "CF-stale", "CF-unrecorded"})


def review_satisfied(tier: ReviewTier, state: str) -> bool:
    """Whether a head at ``tier`` whose verdict reads ``state`` meets the review the policy asks for."""
    return state == "APPROVED" or (tier == "trivial" and state in UNREVIEWED_STATES)


class _NotTrivial(Exception):
    """A lock patch whose change is more than the lu-machinery release."""


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _lock_bump(patch: object) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The (old, new) lu-machinery version a lock patch changes, and nothing else.

    One hunk from line 1: the old requirement block, then the new one, then context.
    Lines GitHub or Git mark ``\\ No newline at end of file`` carry no content.
    """
    if not isinstance(patch, str):
        raise _NotTrivial
    header, *lines = [line for line in patch.split("\n") if not line.startswith("\\")]
    if not _FIRST_HUNK.fullmatch(header):
        raise _NotTrivial
    removed = [line[1:] for line in lines if line.startswith("-")]
    added = [line[1:] for line in lines if line.startswith("+")]
    context = lines[len(removed) + len(added):]
    if lines[:len(removed)] != ["-" + line for line in removed] or any(
        not line.startswith(" ") for line in context if line
    ):
        raise _NotTrivial  # changes after context, a second hunk, or interleaved lines
    sides = []
    for block in (removed, added):
        head = _LOCK_REQUIREMENT.fullmatch(block[0]) if block else None
        hashes = block[1:]
        if (
            head is None or not hashes
            or any(_LOCK_HASH.fullmatch(line) is None for line in hashes)
            or any(not line.endswith(" \\") for line in hashes[:-1])
            or hashes[-1].endswith(" \\")
        ):
            raise _NotTrivial
        sides.append(_version(head.group(1)))
    # A hash line left as context would keep an old hash on the new version.
    if context and _LOCK_HASH.fullmatch(context[0][1:]):
        raise _NotTrivial
    return sides[0], sides[1]


def _load_table(table_path: Path | None) -> dict[str, object] | None:
    table = yaml.safe_load((table_path or machinery_path("config/review_tiers.yaml")).read_text(encoding="utf-8"))
    if (
        not isinstance(table, dict) or set(table) != _TABLE_KEYS
        or type(table["schema_version"]) is not int or table["schema_version"] != 2
        or table["default"] != "medium"
    ):
        return None
    tiers = table["tiers"]
    if not isinstance(tiers, dict) or set(tiers) != set(_MATCH_ORDER):
        return None
    lists = list(tiers.values())
    if any(
        not isinstance(patterns, list) or not patterns
        or any(not isinstance(pattern, str) or not pattern for pattern in patterns)
        for patterns in lists
    ):
        return None
    versions = table["release_version_files"]
    if (
        not isinstance(versions, dict) or not versions
        or any(not isinstance(path, str) or kind not in _KINDS for path, kind in versions.items())
    ):
        return None
    return table


def classify_pr_files(rows: object, *, table_path: Path | None = None) -> ReviewTier:
    """Classify a complete REST PR-files response; unknown input/table is High.

    Use the sweep's validator, including its rename and 3,000-file protections.
    Patches are read only for the product's lu-machinery lock, which is Trivial
    when its change is the lu-machinery release, raised (``_lock_bump``); any
    other change keeps its path's tier. Comment-only source edits keep their
    file's tier: a line that looks like a comment can sit in a string.
    """
    try:
        paths = pr_file_paths(rows)
        if not paths or any(
            path.startswith("/") or "\\" in path or any(part in {".", "..", ""} for part in path.split("/"))
            or PurePosixPath(path).as_posix() != path
            for path in paths
        ):
            return "high"
        table = _load_table(table_path)
        if table is None:
            return "high"
        tiers: Mapping[str, list[str]] = table["tiers"]  # type: ignore[assignment]
        versions: Mapping[str, str] = table["release_version_files"]  # type: ignore[assignment]
        path_tiers: list[ReviewTier] = []
        # Release version files whose patch changes only their version: (version pair, path tier).
        bumps: list[tuple[tuple[tuple[int, ...], tuple[int, ...]], ReviewTier]] = []
        # pr_file_paths validated the shape; every status must also be explicit.
        assert isinstance(rows, list)
        for row in rows:
            status = row.get("status")
            if not isinstance(status, str) or status not in _STATUSES:
                return "high"
            for path in pr_file_paths([row]):
                tier: ReviewTier = next(
                    (tier for tier in _MATCH_ORDER if any(fnmatchcase(path, pattern) for pattern in tiers[tier])),
                    "medium",
                )
                if status == "removed" and tier == "medium":
                    tier = "low"
                if path in versions and status == "modified" and row.get("filename") == path:
                    try:
                        bumps.append((_lock_bump(row.get("patch")), tier))
                        continue
                    except _NotTrivial:
                        pass
                path_tiers.append(tier)
        pairs = {pair for pair, _tier in bumps}
        if len(pairs) == 1 and (pair := pairs.pop())[1] > pair[0]:
            # One version, raised, in every release version file: the release cut's own change.
            path_tiers.append("trivial")
        else:
            path_tiers.extend(tier for _pair, tier in bumps)
        result = max(path_tiers, key=_RANK.__getitem__)
        if result == "trivial":
            # Security paths and learner content (the product's deploy policy, which can name
            # more than the table) are never Trivial; an unreadable policy is unknown, so High.
            from lu_machinery.ci.deploy_paths import DeployPathsError
            from lu_machinery.orchestration.integration_sweep import (
                changes_learner_content,
            )
            from lu_machinery.review.security_paths import is_security_sensitive_change

            if is_security_sensitive_change(paths):
                return "medium"
            try:
                if changes_learner_content(paths):
                    return "ukrainian-content"
            except DeployPathsError:
                return "high"
        return result
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError):
        return "high"


def diff_file_rows(repo_root: Path, base: str, head: str, *, timeout: float = 60) -> list[dict[str, object]]:
    """REST-shaped file rows for ``base..head`` from local Git, with the patches the classifier reads.

    Renames are split into a removal and an addition, so both names count. Raises
    ``ValueError`` when Git fails or prints an unexpected status.
    """
    def git(*args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args], capture_output=True, timeout=timeout, check=False,
        )
        if result.returncode:
            raise ValueError("git diff failed")
        return result.stdout

    fields = git("diff", "--no-renames", "--no-ext-diff", "--name-status", "-z", base, head, "--").decode("utf-8")
    parts = fields.split("\0")
    if parts.pop() != "" or len(parts) % 2:
        raise ValueError("git diff file statuses unavailable")
    statuses = {"A": "added", "D": "removed", "M": "modified", "T": "changed"}
    table = _load_table(None)
    versions = table["release_version_files"] if table is not None else {}
    rows: list[dict[str, object]] = []
    for status, path in zip(parts[::2], parts[1::2], strict=True):
        if status not in statuses:
            raise ValueError("git diff file status unexpected")
        row: dict[str, object] = {"filename": path, "status": statuses[status]}
        if status == "M" and isinstance(versions, dict) and path in versions:
            text = git("diff", "--no-ext-diff", "--no-color", "-U3", base, head, "--", path).decode("utf-8")
            hunk = text.find("\n@@")
            if hunk >= 0:
                row["patch"] = text[hunk + 1:].removesuffix("\n")
        rows.append(row)
    return rows


# What each tier asks for before merge; review policy section 2 is the text of record.
REVIEW_REQUIRED: dict[ReviewTier, str] = {
    "trivial": "none: required CI only; do not run review-loop",
    "low": "one quick red-team pass: one non-author reviewer, --risk low, at most one fix confirmation",
    "medium": "one cross-family review",
    "ukrainian-content": "the Ukrainian profile's seats",
    "high": "one cross-family review at --risk high (Sol, Opus, Neon or Grok; the resolver picks) or critical (Sol, Opus or Neon)",
}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Print a pull request's review tier and the review it needs before merge.\n"
        "Use before review-loop round 1; the merge gates compute the same tier themselves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -m lu_machinery.review.tiers --repository owner/name --pr 714\n"
            "  .venv/bin/python -m lu_machinery.review.tiers --rows-file pr-files.json\n"
            "Outputs: one JSON object on stdout: {\"tier\": ..., \"review\": ...}; no writes.\n"
            "Exit codes: 0 tier printed (an unreadable file list or table prints high); 2 invalid arguments.\n"
            "Related: lu_machinery/config/review_tiers.yaml; agents_extensions/shared/skills/review-loop/"
            "references/policy.md section 2."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr", type=int, help="pull request number to read through gh (needs --repository)")
    source.add_argument(
        "--rows-file", type=Path,
        help="JSON list of GitHub PR-files rows (filename, status, patch) instead of reading GitHub",
    )
    parser.add_argument("--repository", help="owner/name of the pull request's repository")
    args = parser.parse_args(argv)
    if args.pr is not None and not args.repository:
        parser.error("--pr needs --repository")
    rows: object
    try:
        if args.rows_file is not None:
            rows = json_depth.loads(args.rows_file.read_text(encoding="utf-8"), max_depth=512)
        else:
            from lu_machinery.orchestration.integration_sweep import (
                GitHubAdapter,
                SweepError,
            )

            try:
                rows = GitHubAdapter(Path.cwd()).pr_file_rows(args.repository, args.pr)
            except SweepError:
                rows = None
    except (OSError, ValueError):
        rows = None
    tier = classify_pr_files(rows)
    json.dump({"tier": tier, "review": REVIEW_REQUIRED[tier]}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
