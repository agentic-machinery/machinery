"""Reviewer-grading runner: split stability, cross-family scoring and a dispatch-free dry run."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lu_machinery.bakeoff import review_eval
from tests.helpers import current_sol

CURRENT_SOL = current_sol()

SET_DIR = Path(__file__).resolve().parents[2] / "docs" / "review-eval"


def test_split_is_a_pinned_hash_so_tuning_never_reaches_the_held_out_split() -> None:
    """Defect class: a split that depends on file order, set size or runtime state moves cases
    between tune and test, so tuning would read held-out cases."""
    assert review_eval.split_of("learn-ukrainian/machinery", 349) == "test"
    assert review_eval.split_of("learn-ukrainian/machinery", 343) == "tune"
    assert review_eval.split_of("learn-ukrainian/learn-ukrainian.github.io", 13145) == "test"
    assert review_eval.split_of("learn-ukrainian/learn-ukrainian.github.io", 13135) == "tune"

    evalset = review_eval.load_set(SET_DIR)
    reversed_set = review_eval.EvalSet(evalset.cases[::-1], evalset.controls[::-1], evalset.protocol)
    for split in review_eval.SPLITS:
        forward = {i.entry["id"] for i in review_eval.plan(evalset, "neon", split, "t")}
        backward = {i.entry["id"] for i in review_eval.plan(reversed_set, "neon", split, "t")}
        assert forward == backward
    test_ids = {i.entry["id"] for i in review_eval.plan(evalset, "sol", "test", "t")}
    tune_ids = {i.entry["id"] for i in review_eval.plan(evalset, "sol", "tune", "t")}
    assert test_ids and tune_ids and not test_ids & tune_ids
    assert len(test_ids | tune_ids) == len(evalset.cases) + len(evalset.controls)


def test_a_finding_scores_only_when_a_cross_family_judge_matches_a_returned_finding() -> None:
    """Defect class: recall inflated by judge answers that name findings the reviewer never
    returned, by one finding credited to two defects, or by a same-family judge; and a BLOCKED
    or malformed reply scored as a completed review (a clean control or a review that found
    nothing)."""
    validator = review_eval._findings_validator()
    overall = {"explanation": "x", "confidence": 0.5}
    blocked = {"schema_version": "code-review-findings.v1", "overall": {**overall, "correctness": "uncertain"},
               "findings": [], "missing_capabilities": ["checkout-data"]}
    approve = {"schema_version": "code-review-findings.v1", "overall": {**overall, "correctness": "correct"},
               "findings": []}
    assert review_eval.valid_review(json.dumps(blocked), validator) is None
    assert review_eval.valid_review(json.dumps({"findings": []}), validator) is None
    assert review_eval.valid_review(json.dumps({**approve, "findings": [42]}), validator) is None
    assert review_eval.valid_review("APPROVE " + json.dumps(approve), validator) == approve
    assert review_eval.valid_matches('{"matches": [{"defect_id": "D1", "finding_id": 3}]}') is None
    assert review_eval.valid_matches('{"matches": [{"defect_id": "D1", "finding_id": null}]}') == {"D1": None}

    case = {
        "id": "machinery-1", "repository": "learn-ukrainian/machinery", "pr": 1,
        "source_review": {"family": "openai"},
        "defects": [{"id": "D1"}, {"id": "D2"}, {"id": "D3"}],
    }
    reviewer = {"family": "google"}
    findings = [{"id": "F1", "priority": "P1"}, {"id": "F2", "priority": "P3"}]

    def fake_judge(_case, _reviewer, _findings):
        return {"D1": "F1", "D2": "F1", "D3": "F9", "D4": "F2"}

    found = review_eval.grade_defect_case(case, reviewer, "anthropic", findings, fake_judge)
    assert found == {"D1": "F1"}
    assert review_eval.grade_defect_case(case, reviewer, "anthropic", [], fake_judge) == {}
    with pytest.raises(review_eval.EvalError):
        review_eval.grade_defect_case(case, reviewer, "google", findings, fake_judge)

    protocol = {"seats": {"carbon": {"family": "google"}}}
    evalset = review_eval.EvalSet([case], [{"id": "machinery-2"}, {"id": "machinery-3"}], protocol)
    items = [
        review_eval.Item("carbon", "defect", case, "t1"),
        review_eval.Item("carbon", "control", {"id": "machinery-2"}, "t2"),
        review_eval.Item("carbon", "control", {"id": "machinery-3"}, "t3"),
    ]
    rows = [
        {"seat": "carbon", "case_id": "machinery-1", "status": "done", "matched": found, "minutes": 4.0},
        {"seat": "carbon", "case_id": "machinery-2", "status": "done", "minutes": 2.0,
         "findings": [{"id": "F1", "priority": "P2"}]},
        {"seat": "carbon", "case_id": "machinery-3", "status": "failed", "minutes": 1.0},
    ]
    scores = review_eval.score(evalset, items, rows)["carbon"]
    assert (scores["defects_found"], scores["defects_total"], scores["recall"]) == (1, 3, 0.333)
    assert scores["recall_other_family_sources"] == 0.333
    assert (scores["controls_with_blocking_finding"], scores["controls_scored"]) == (1, 1)
    assert scores["reviews_failed"] == 1


def test_dry_run_prints_the_plan_and_dispatches_nothing(monkeypatch, capsys) -> None:
    """Defect class: a dry run that starts a provider dispatch spends paid review runs the CTO
    has not approved."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("dry run started a subprocess")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    code = review_eval.main(["--seat", "sol", "--seat", "neon", "--dry-run", "--set-dir", str(SET_DIR),
                             "--run-label", "t"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"lu_machinery.dispatch dispatch --agent codex --model {CURRENT_SOL}" in out
    assert "--agent agy --model gemini-4-neon-high" in out
    assert '"reviews": 33' in out
