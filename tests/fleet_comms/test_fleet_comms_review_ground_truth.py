"""Tests for PR three-dot ground truth (#5802 Layer 3)."""

from __future__ import annotations

import json
import subprocess

import pytest

from lu_machinery.fleet_comms.review_ground_truth import (
    ReviewGroundTruthError,
    fetch_pr_change_inventory,
    format_ground_truth_brief,
    inventory_from_path_status,
    parse_pr_change_inventory,
    validate_findings_against_inventory,
)
from lu_machinery.fleet_comms.review_publication import (
    ReviewEvidence,
    ReviewFinding,
    ReviewPublicationError,
)

_SHA = "a" * 40
_BASE = "b" * 40
_REPO = "learn-ukrainian/learn-ukrainian.github.io"


def _finding(*, path: str, finding_id: str = "F001") -> ReviewFinding:
    return ReviewFinding(
        finding_id=finding_id,
        title="Claimed deletion",
        body="File was deleted.",
        priority="P0",
        confidence=0.9,
        category="regression",
        path=path,
        start_line=1,
        end_line=1,
        claim_type="missing",
        verbatim="sentinel",
        why_wrong="Would remove a regression guard.",
        smallest_fix="Do not delete the file.",
        sources=("none",),
    )


def _evidence(*paths: str) -> ReviewEvidence:
    return ReviewEvidence(
        correctness="incorrect",
        explanation="Blocking deletions.",
        confidence=0.9,
        findings=tuple(
            _finding(path=path, finding_id=f"F{index:03d}")
            for index, path in enumerate(paths, start=1)
        ),
    )


def test_parse_inventory_and_brief_marks_three_dot_trap() -> None:
    inventory = parse_pr_change_inventory(
        {
            "headRefOid": _SHA,
            "baseRefOid": _BASE,
            "files": [
                {"path": "scripts/a.py", "changeType": "MODIFIED"},
                {"path": "tests/b.py", "changeType": "DELETED"},
            ],
        },
        repository=_REPO,
        pr_number=5802,
    )
    assert inventory.paths == frozenset({"scripts/a.py", "tests/b.py"})
    assert inventory.deleted_paths == frozenset({"tests/b.py"})
    brief = format_ground_truth_brief(inventory)
    assert "three-dot" in brief
    assert "two-dot" in brief
    assert "**Changed paths:** 2 (1 deleted)" in brief
    assert "gh pr view 5802 --json files" in brief
    assert "merge-base" in brief
    # Path samples are optional and budget-gated; request them explicitly.
    with_paths = format_ground_truth_brief(inventory, max_files=12)
    assert "`M` `scripts/a.py`" in with_paths
    assert "`D` `tests/b.py`" in with_paths


def test_validate_findings_refuses_two_dot_artifact_paths() -> None:
    inventory = inventory_from_path_status(
        repository=_REPO,
        pr_number=5799,
        head_sha=_SHA,
        base_ref_oid=_BASE,
        entries=(
            ("scripts/fleet_comms/review_publication.py", "MODIFIED"),
            ("tests/test_fleet_comms_review_publication.py", "MODIFIED"),
        ),
    )
    # Layer 3 incident shape: review cites files that only "disappear" in a
    # two-dot diff against a moved main tip — not on the PR surface.
    with pytest.raises(
        ReviewPublicationError, match="review_finding_path_outside_pr_surface"
    ):
        validate_findings_against_inventory(
            _evidence(
                "tests/test_pytest_worker_rlimit_isolation.py",
                "scripts/fleet_comms/review_publication.py",
            ),
            inventory,
            expected_head_sha=_SHA,
        )


def test_validate_findings_allows_in_surface_paths() -> None:
    inventory = inventory_from_path_status(
        repository=_REPO,
        pr_number=5799,
        head_sha=_SHA,
        base_ref_oid=_BASE,
        entries=(("scripts/fleet_comms/review_publication.py", "MODIFIED"),),
    )
    validate_findings_against_inventory(
        _evidence("scripts/fleet_comms/review_publication.py"),
        inventory,
        expected_head_sha=_SHA,
    )


def test_validate_rejects_mismatched_head_sha() -> None:
    inventory = inventory_from_path_status(
        repository=_REPO,
        pr_number=1,
        head_sha=_SHA,
        base_ref_oid=_BASE,
        entries=(("a.py", "MODIFIED"),),
    )
    with pytest.raises(
        ReviewPublicationError, match="review_ground_truth_head_mismatch"
    ):
        validate_findings_against_inventory(
            _evidence("a.py"),
            inventory,
            expected_head_sha="c" * 40,
        )


def test_fetch_pr_change_inventory_uses_gh_json() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        payload = {
            "headRefOid": _SHA,
            "baseRefOid": _BASE,
            "files": [{"path": "a.py", "changeType": "ADDED"}],
        }
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    inventory = fetch_pr_change_inventory(
        repository=_REPO, pr_number=5802, runner=runner
    )
    assert inventory.files[0].path == "a.py"
    assert inventory.files[0].change_type == "ADDED"
    assert any("--json" in call and "files" in ",".join(call) for call in calls)


def test_fetch_pr_change_inventory_fails_closed_on_gh_error() -> None:
    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    with pytest.raises(ReviewGroundTruthError, match="gh_pr_files_lookup_failed"):
        fetch_pr_change_inventory(repository=_REPO, pr_number=5802, runner=runner)
