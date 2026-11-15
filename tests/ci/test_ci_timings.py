"""Unit tests for scripts/ci/ci_timings.py (issue #7174).

Hermetic, offline test suite verifying:
- Nearest-rank p95 percentile against hand-computed values.
- ISO and relative --since window parsing and boundary filtering.
- Queue timing and kick calculation from recorded API fixtures.
- Stable-ordered JSON serialization and Markdown table formatting.
- CLI argument parsing and error handling.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
import pytest
from lu_machinery.ci.ci_timings import DEFAULT_REPO, analyze_timings, extract_pr_number, fetch_run_jobs_from_api, fetch_workflow_runs_from_api, gh_api_get, load_runs_and_jobs_from_fixture, main
from lu_machinery.ci import delivery_baseline as db
from tests.helpers.product_tree import configured_roots as configured_roots

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "ci_timings" / "sample_ci_runs.json"




def test_extract_pr_number() -> None:
    """Test extracting PR numbers from branch names."""
    assert extract_pr_number("gh-readonly-queue/main/pr-7170-0224500326cbe490cd890784f67741ca1d8ef65b") == 7170
    assert extract_pr_number("issue/7139-monitor-dual-host-slice") == 7139
    assert extract_pr_number("pull/6863") == 6863
    assert extract_pr_number("pr-4811") == 4811
    assert extract_pr_number("main") is None
    assert extract_pr_number("") is None
    assert extract_pr_number(None) is None


def test_analyze_timings_on_recorded_fixture() -> None:
    """Verify complete timing analysis against the offline recorded fixture."""
    runs, jobs_by_run = load_runs_and_jobs_from_fixture(_FIXTURE_PATH)
    assert len(runs) == 15
    assert len(jobs_by_run) == 15

    report = analyze_timings(
        runs=runs,
        jobs_fetcher=jobs_by_run,
        workflow_name="CI",
        repo_name=DEFAULT_REPO,
        event_filter="all",
    )

    assert set(report.events.keys()) == {"pull_request", "merge_group", "push"}

    # Validate merge_group analysis: 5 total completed runs, 3 successful
    mg = report.events["merge_group"]
    assert mg.runs_count == 5
    assert mg.wall_clock_minutes["n"] == 3
    assert mg.wall_clock_minutes["avg"] == 13.0
    assert mg.wall_clock_minutes["median"] == 13.0
    assert mg.wall_clock_minutes["max"] == 15.0
    assert mg.wall_clock_all_minutes["n"] == 5
    assert mg.wall_clock_all_minutes["avg"] == 12.0
    assert mg.wall_clock_all_minutes["median"] == 11.1
    assert mg.wall_clock_all_minutes["max"] == 15.0

    job_names = {j.name for j in mg.jobs}
    assert "Contracts (schema, MDX, atlas, BIO)" in job_names
    assert "Python (pytest) [1/4]" in job_names
    assert "Python (pytest) [2/4]" in job_names
    assert "Python (pytest) [3/4]" in job_names
    assert "Python (pytest) [4/4]" in job_names
    assert "Frontend (build + vitest)" in job_names

    # Check queue timing details for merge_group
    assert mg.queue_timing is not None
    q = mg.queue_timing
    assert q["summary"]["prs_count"] == 4
    assert q["summary"]["kicks_total"] == 2

    pr_map = {p["pr_number"]: p for p in q["prs"]}
    assert set(pr_map.keys()) == {7165, 7167, 7169, 7170}

    # PR 7170: landed cleanly with 0 kicks
    assert pr_map[7170]["status"] == "landed"
    assert pr_map[7170]["kicks"] == 0
    assert pr_map[7170]["time_in_queue_minutes"] == 11.7

    # PR 7165: 1 kick and landed
    assert pr_map[7165]["status"] == "landed"
    assert pr_map[7165]["kicks"] == 1

    # PR 7169: failed / kicked
    assert pr_map[7169]["status"] == "failed"
    assert pr_map[7169]["kicks"] == 1


def test_run_jobs_fetch_error_is_not_dropped() -> None:
    runs, _ = load_runs_and_jobs_from_fixture(_FIXTURE_PATH)

    def fail_jobs(_run_id: int) -> list[dict[str, Any]]:
        raise RuntimeError("job fetch failed")

    with pytest.raises(RuntimeError, match="job fetch failed"):
        analyze_timings(runs, fail_jobs, event_filter="all", max_workers=8)


def test_fetch_workflow_runs_passes_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify fetch_workflow_runs_from_api constructs correct API query parameters."""
    captured_paths: list[str] = []

    def fake_gh_api_get(path: str, **kwargs: Any) -> dict[str, Any]:
        captured_paths.append(path)
        return {"workflow_runs": [{"id": 1}]}

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)

    # 1. Event + branch + default status
    fetch_workflow_runs_from_api(
        repo="learn-ukrainian/learn-ukrainian.github.io",
        workflow_file="ci.yml",
        event="push",
        branch="main",
        limit=5,
    )
    assert len(captured_paths) == 1
    assert "event=push" in captured_paths[0]
    assert "branch=main" in captured_paths[0]
    assert "status=completed" in captured_paths[0]
    assert "per_page=100" in captured_paths[0]

    captured_paths.clear()

    # 2. Event == 'all' should NOT pass event=all
    fetch_workflow_runs_from_api(
        repo="learn-ukrainian/learn-ukrainian.github.io",
        workflow_file="ci.yml",
        event="all",
    )
    assert len(captured_paths) == 1
    assert "event=" not in captured_paths[0]


def test_fetch_workflow_runs_out_of_order_page_does_not_stop_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale entry before newer runs must not hide later pages."""
    cutoff = datetime(2026, 9, 27, 12, tzinfo=UTC)
    paths: list[str] = []
    recent = [{"id": run_id, "created_at": "2026-09-28T00:00:00Z"} for run_id in range(1, 101)]
    pages = {
        1: [{"id": 0, "created_at": "2026-09-26T00:00:00Z"}, *recent[:99]],
        2: recent[99:],
    }

    def fake_gh_api_get(path: str, **_kwargs: Any) -> dict[str, Any]:
        paths.append(path)
        query = parse_qs(urlsplit(path).query)
        assert query["created"] == [">=2026-09-27T12:00:00Z"]
        assert "event" not in query
        return {"total_count": 101, "workflow_runs": pages[int(query["page"][0])]}

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)
    runs = fetch_workflow_runs_from_api(DEFAULT_REPO, "ci.yml", since_dt=cutoff, event="all")

    assert [run["id"] for run in runs] == list(range(1, 101))
    assert len(paths) == 2


def test_fetch_workflow_runs_stops_when_page_ends_before_since(monkeypatch: pytest.MonkeyPatch) -> None:
    cutoff = datetime(2026, 9, 27, 12, tzinfo=UTC)
    paths: list[str] = []

    def fake_gh_api_get(path: str, **_kwargs: Any) -> dict[str, Any]:
        paths.append(path)
        assert "page=1" in path
        return {
            "total_count": 300,
            "workflow_runs": [
                {"id": run_id, "created_at": "2026-09-28T00:00:00Z" if run_id < 50 else "2026-09-26T00:00:00Z"}
                for run_id in range(100)
            ],
        }

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)
    runs = fetch_workflow_runs_from_api(DEFAULT_REPO, "ci.yml", since_dt=cutoff, event="all")
    assert len(runs) == 50
    assert len(paths) == 1


def test_fetch_workflow_runs_stops_at_total_count_on_full_page(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def fake_gh_api_get(path: str, **_kwargs: Any) -> dict[str, Any]:
        paths.append(path)
        return {"total_count": 100, "workflow_runs": [{"id": i} for i in range(100)]}

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)
    assert len(fetch_workflow_runs_from_api(DEFAULT_REPO, "ci.yml")) == 100
    assert len(paths) == 1


def test_cli_main_api_event_limit_matching_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end test of main() with API fetch, verifying --limit N returns N matching runs.

    If the fix were missing (i.e. event filter omitted during fetch), fetch would return
    the newest runs which are non-push, resulting in 'push (0 completed runs)'.
    With the fix, main() produces 'push (2 completed runs)'.
    """
    runs, jobs_by_run = load_runs_and_jobs_from_fixture(_FIXTURE_PATH)

    def fake_gh_api_get(path: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        if "actions/runs/" in path and "/jobs" in path:
            run_id = path.split("actions/runs/")[1].split("/jobs")[0]
            return jobs_by_run.get(run_id, [])
        if "event=push" in path:
            matching = [r for r in runs if r.get("event") == "push" and r.get("status") == "completed"]
            return {"workflow_runs": matching}
        # Unfiltered returns all runs (the top ones are non-push)
        return {"workflow_runs": runs}

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)

    exit_code = main(["--event", "push", "--limit", "2"])
    assert exit_code == 0
    out, err = capsys.readouterr()
    assert not err
    assert "## push (2 completed runs)" in out
    assert "## push (0 completed runs)" not in out


def test_fetch_run_jobs_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify fetch_run_jobs_from_api paginates across multiple pages."""
    calls: list[str] = []

    def fake_gh_api_get(path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        m = re.search(r"[?&]page=(\d+)", path)
        page_num = int(m.group(1)) if m else 1
        if page_num == 1:
            return {
                "total_count": 125,
                "jobs": [{"id": i, "name": f"job-{i}", "status": "completed"} for i in range(100)],
            }
        if page_num == 2:
            return {
                "total_count": 125,
                "jobs": [{"id": 100 + i, "name": f"job-{100 + i}", "status": "completed"} for i in range(25)],
            }
        return {"total_count": 125, "jobs": []}

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)

    jobs = fetch_run_jobs_from_api(
        repo="learn-ukrainian/learn-ukrainian.github.io",
        run_id=12345,
    )
    assert len(jobs) == 125
    assert len(calls) == 2
    assert "page=1" in calls[0]
    assert "page=2" in calls[1]


def test_fetch_run_jobs_pagination_max_pages_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify fetch_run_jobs_from_api emits a warning when max_pages limit is reached."""

    def fake_gh_api_get(path: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "total_count": 500,
            "jobs": [{"id": i, "name": f"job-{i}", "status": "completed"} for i in range(100)],
        }

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_gh_api_get)

    jobs = fetch_run_jobs_from_api(
        repo="learn-ukrainian/learn-ukrainian.github.io",
        run_id=99999,
        max_pages=2,
    )
    assert len(jobs) == 200
    _, err = capsys.readouterr()
    assert "Warning: Reached maximum pagination limit (2 pages) for run 99999 jobs; retrieved 200 jobs." in err


def test_load_runs_and_jobs_from_fixture_non_list_non_dict(tmp_path: Path) -> None:
    """Verify load_runs_and_jobs_from_fixture raises ValueError on non-list/non-dict JSON payloads."""
    str_file = tmp_path / "string_fixture.json"
    str_file.write_text(json.dumps("plain string value"), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid fixture format: expected JSON object or array"):
        load_runs_and_jobs_from_fixture(str_file)

    int_file = tmp_path / "int_fixture.json"
    int_file.write_text(json.dumps(12345), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid fixture format: expected JSON object or array"):
        load_runs_and_jobs_from_fixture(int_file)


def test_gh_api_get_json_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify gh_api_get raises RuntimeError with documented message on malformed JSON from API."""

    def fake_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/test"],
            returncode=0,
            stdout="<html>502 Bad Gateway</html>",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    with pytest.raises(RuntimeError, match="Failed to parse JSON response from gh api repos/owner/repo/test"):
        gh_api_get("repos/owner/repo/test")


def test_empty_workflow_page_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def empty_page(_path: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"total_count": 80, "workflow_runs": []}

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", empty_page)
    with pytest.raises(RuntimeError, match="stayed empty after 3 attempts despite total_count=80"):
        fetch_workflow_runs_from_api(DEFAULT_REPO, "ci.yml")
    assert calls == 3

    assert main(["--since", "2026-09-27T11:00", "--event", "merge_group"]) == 1
    assert calls == 6


def test_empty_workflow_page_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = iter(
        [
            {"total_count": 1, "workflow_runs": []},
            {"total_count": 1, "workflow_runs": [{"id": 1, "created_at": "2026-09-28T00:00:00Z"}]},
        ]
    )
    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", lambda *_a, **_kw: next(pages))
    runs = fetch_workflow_runs_from_api(DEFAULT_REPO, "ci.yml")
    assert [run["id"] for run in runs] == [1]


def test_cli_all_limit_fetches_each_event_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def fake_api(path: str, **_kwargs: Any) -> dict[str, Any]:
        paths.append(path)
        if "/jobs?" in path:
            return {"jobs": [], "total_count": 0}
        event = next(ev for ev in ("pull_request", "merge_group", "push") if f"event={ev}" in path)
        return {
            "total_count": 1,
            "workflow_runs": [
                {"id": len(paths), "event": event, "status": "completed", "created_at": "2026-09-28T00:00:00Z"}
            ],
        }

    monkeypatch.setattr("lu_machinery.ci.ci_timings.gh_api_get", fake_api)
    assert main(["--event", "all", "--limit", "1", "--json"]) == 0
    run_paths = [path for path in paths if "/workflows/" in path]
    assert len(run_paths) == 3
    assert all(
        f"event={event}" in path for event, path in zip(("pull_request", "merge_group", "push"), run_paths, strict=True)
    )


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "delivery_baseline"


def _load(name: str) -> dict:
    return json.loads(_DELIVERY_FIXTURES[name])


def _record(report: dict, task_id: str) -> dict:
    return next(item for item in report["records"] if item["task_id"] == task_id)


@pytest.mark.usefixtures('configured_roots')
def test_canonical_task_and_linked_pr_check_spans_are_qualified() -> None:
    report = db.evaluate(_load("canonical_linked.json"))
    record = _record(report, "fixture-canonical-1")
    assert record["disposition"] == "delivered"
    assert {name: metric["status"] for name, metric in record["metrics"].items()} == {
        "intake.elapsed": "qualified",
        "authoring.worker_wall": "qualified",
        "ci_landing.check_wall": "qualified",
        "ci_landing.queue_delay": "qualified",
        "ci_landing.elapsed": "qualified",
    }
    assert record["metrics"]["authoring.worker_wall"]["union_s"] == 2700.0
    assert record["metrics"]["ci_landing.queue_delay"]["union_s"] == 60.0


@pytest.mark.usefixtures('configured_roots')
def test_missing_or_non_canonical_span_is_unknown_never_zero() -> None:
    record = _record(db.evaluate(_load("unknown_span.json")), "fixture-missing-1")
    authoring = record["metrics"]["authoring.worker_wall"]
    assert authoring == {"status": "unknown", "union_s": None, "spans_s": [None], "reasons": ["missing_endpoint"]}
    review = record["metrics"]["verification_review.worker_wall"]
    assert review["status"] == "unknown"
    assert review["reasons"] == ["non_canonical_source"]
    assert record["metrics"]["onboarding.elapsed"]["status"] == "unqualified"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("task_id", "reason"),
    [
        ("fixture-unlinked", "unlinked_outcome"),
        ("fixture-no-merge", "not_merged"),
        ("fixture-no-review", "no_review_of_record"),
        ("fixture-no-cleanup", "no_cleanup_evidence"),
        ("fixture-self-review", "review_not_independent"),
        ("fixture-stale-head", "review_not_exact_head"),
    ],
)
def test_finished_but_unproven_records_are_not_delivered(task_id: str, reason: str) -> None:
    record = _record(db.evaluate(_load("not_delivered.json")), task_id)
    assert record["disposition"] == "not_delivered"
    assert record["reasons"] == [reason]


@pytest.mark.usefixtures('configured_roots')
def test_negative_interval_and_naive_timestamp_are_unknown() -> None:
    negative = {
        "stage": "authoring",
        "kind": "worker_wall",
        "start_field": "task.started_at",
        "end_field": "task.finished_at",
    }
    assert db._span_value({**negative, "start": "2026-01-01T11:00:00Z", "end": "2026-01-01T10:00:00Z"}) == (
        None,
        "negative_interval",
    )
    assert db._span_value({**negative, "start": "2026-01-01T10:00:00", "end": "2026-01-01T11:00:00Z"}) == (
        None,
        "missing_endpoint",
    )


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("cohort", "message"),
    [
        ({"schema_version": "other", "records": []}, "schema_version"),
        ({"schema_version": db.SCHEMA_VERSION, "records": {}}, "records must be a list"),
        ({"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "during"}]}, "arm must be"),
        ({"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "before"}] * 2}, "duplicated"),
        (
            {
                "schema_version": db.SCHEMA_VERSION,
                "records": [{"task_id": "x", "arm": "before", "spans": [{"stage": "deploy", "kind": "elapsed"}]}],
            },
            "unknown stage",
        ),
    ],
)
def test_malformed_cohorts_are_refused(cohort: dict, message: str) -> None:
    with pytest.raises(db.CohortError, match=message):
        db.evaluate(cohort)


@pytest.mark.usefixtures('configured_roots')
def test_report_records_input_digest_and_is_deterministic() -> None:
    first = db.evaluate(_load("stratum_floor.json"))
    second = db.evaluate(_load("stratum_floor.json"))
    assert first == second
    assert len(first["input_sha256"]) == 64


@pytest.mark.usefixtures("delivery_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_cli_prints_report_and_refuses_bad_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert db.main([str(FIXTURES / "overlapping_spans.json")]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == "delivery-baseline-report.v1"
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert db.main([str(bad)]) == 2
    assert "schema_version" in capsys.readouterr().err


CLAIM = {"stratum": "architecture_change", "metric": "ci_landing.check_wall"}


def _check_run(start: str, end: str) -> dict:
    return {
        "stage": "ci_landing",
        "kind": "check_wall",
        "start_field": "github.check_run.startedAt",
        "end_field": "github.check_run.completedAt",
        "start": f"2026-03-01T{start}:00Z",
        "end": f"2026-03-01T{end}:00Z",
    }


def _floor_cohort() -> dict:
    records = []
    for index in range(2 * db.MIN_DELIVERED_PER_ARM):
        arm = db.ARMS[index % 2]
        head = f"{index:040x}"
        records.append(
            {
                "task_id": f"heldout-{arm}-{index}",
                "arm": arm,
                "stratum": "architecture_change",
                "matched_setup": {"required_check": "CI Gate", "review_profile": "code"},
                "outcome": {"issue": 900 + index, "pr": 950 + index},
                "review_of_record": {
                    "verdict": "APPROVE",
                    "head_sha": head,
                    "author_family": "anthropic",
                    "reviewer_family": "openai",
                },
                "merge": {"merged_at": "2026-03-01T13:00:00Z", "head_sha": head},
                "cleanup": {"state": "CLEANED_UP", "observed_at": "2026-03-01T14:00:00Z"},
                "spans": [_check_run("12:00", "12:04"), _check_run("12:02", "12:07"), _check_run("12:10", "12:11")],
            }
        )
    return {"schema_version": db.SCHEMA_VERSION, "records": records, "claims": [CLAIM]}


def _verdict(cohort: dict) -> tuple[dict, dict, dict | None]:
    report = db.evaluate(cohort)
    stratum = report["strata"]["architecture_change"]
    return report, stratum, stratum["metrics"].get("ci_landing.check_wall")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author_family", None),
        ("author_family", ""),
        ("author_family", "   "),
        ("author_family", 7),
        ("reviewer_family", None),
    ],
)
def test_unknown_author_or_reviewer_family_never_establishes_independence(field: str, value: object) -> None:
    cohort = _floor_cohort()
    for record in cohort["records"]:
        if value is None:
            record["review_of_record"].pop(field)
        else:
            record["review_of_record"][field] = value
    report, stratum, metric = _verdict(cohort)
    assert metric is None
    assert {tuple(item["reasons"]) for item in report["records"]} == {("review_identity_unknown",)}
    assert stratum["denominators"]["before"]["not_delivered_reasons"] == {"review_identity_unknown": 5}
    assert stratum["verdict"] == "inconclusive"
    assert report["claims"][0]["status"] == "rejected"


@pytest.mark.usefixtures('configured_roots')
def test_family_comparison_ignores_case_and_whitespace() -> None:
    cohort = _floor_cohort()
    cohort["records"][0]["review_of_record"].update(author_family="Anthropic ", reviewer_family="anthropic")
    assert _record(db.evaluate(cohort), "heldout-before-0")["reasons"] == ["review_not_independent"]


def _one_outcome_per_arm(cohort: dict) -> dict:
    for record in cohort["records"]:
        first = cohort["records"][db.ARMS.index(record["arm"])]
        for key in ("outcome", "review_of_record", "merge", "cleanup"):
            record[key] = json.loads(json.dumps(first[key]))
    return cohort


@pytest.mark.usefixtures('configured_roots')
def test_repeated_attempts_at_one_outcome_are_refused_not_counted(tmp_path: Path) -> None:
    cohort = _one_outcome_per_arm(_floor_cohort())
    with pytest.raises(db.CohortError, match="share outcome pr 950"):
        db.evaluate(cohort)
    path = tmp_path / "repeated.json"
    path.write_text(json.dumps(cohort), encoding="utf-8")
    assert db.main([str(path)]) == 2


@pytest.mark.usefixtures('configured_roots')
def test_shared_merged_head_under_different_pr_numbers_is_one_outcome() -> None:
    cohort = _floor_cohort()
    cohort["records"][1]["merge"]["head_sha"] = cohort["records"][0]["merge"]["head_sha"]
    with pytest.raises(db.CohortError, match="share outcome merged_head"):
        db.evaluate(cohort)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("setup", ["absent", None, {}, {"required_check": None, "review_profile": "code"}])
def test_missing_or_empty_setup_is_unknown_and_inconclusive(setup: object) -> None:
    cohort = _floor_cohort()
    for record in cohort["records"]:
        if setup == "absent":
            record.pop("matched_setup")
        else:
            record["matched_setup"] = setup
    report, stratum, metric = _verdict(cohort)
    assert metric == {"verdict": "inconclusive", "reasons": ["unknown_setup"]}
    assert stratum["verdict"] == "inconclusive"
    assert report["claims"][0] == {**CLAIM, "status": "rejected", "reason": "comparison_inconclusive"}


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "outcome",
    [
        {"issue": False, "pr": True},
        {"issue": True, "pr": 950},
        {"issue": 0, "pr": 950},
        {"issue": -1, "pr": 950},
        {"issue": "900", "pr": 950},
        {"issue": 900, "pr": 950.0},
    ],
)
def test_outcome_identifiers_must_be_positive_integers(outcome: dict) -> None:
    cohort = _floor_cohort()
    for index, record in enumerate(cohort["records"]):
        record["outcome"] = dict(outcome) if index == 0 else record["outcome"]
    report = db.evaluate(cohort)
    assert _record(report, "heldout-before-0")["reasons"] == ["unlinked_outcome"]
    assert report["claims"][0]["status"] == "rejected"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("cohort", "message"),
    [
        ([], "cohort must be a JSON object"),
        ({"schema_version": db.SCHEMA_VERSION, "records": [None]}, "each record must be an object"),
        (
            {"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "before", "spans": [False]}]},
            "each span must be an object",
        ),
        (
            {"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "before", "spans": {"a": 1}}]},
            "spans must be a list",
        ),
        (
            {"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "before", "outcome": [1]}]},
            "outcome must be an object",
        ),
        (
            {
                "schema_version": db.SCHEMA_VERSION,
                "records": [{"task_id": "x", "arm": "before", "exclusion": "dry_run", "merge": "yes"}],
            },
            "merge must be an object",
        ),
        (
            {"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "before", "matched_setup": "x"}]},
            "matched_setup must be an object",
        ),
        ({"schema_version": db.SCHEMA_VERSION, "records": [], "claims": {}}, "claims must be a list"),
        ({"schema_version": db.SCHEMA_VERSION, "records": [], "claims": [None]}, "each claim must be an object"),
        (
            {"schema_version": db.SCHEMA_VERSION, "records": [], "claims": [{"stratum": "x", "metric": ["m"]}]},
            "string metric",
        ),
    ],
)
def test_malformed_structures_raise_cohort_error(cohort: object, message: str) -> None:
    with pytest.raises(db.CohortError, match=message):
        db.evaluate(cohort)  # type: ignore[arg-type]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema_version": db.SCHEMA_VERSION, "records": [None]},
        {"schema_version": db.SCHEMA_VERSION, "records": [{"task_id": "x", "arm": "before", "spans": [False]}]},
    ],
)
def test_cli_exits_two_on_malformed_structures(
    document: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert db.main([str(path)]) == 2
    assert capsys.readouterr().err.startswith("delivery_baseline: ")


@pytest.mark.usefixtures('configured_roots')
def test_unhashable_span_fields_are_non_canonical_not_a_crash() -> None:
    span = {
        "stage": "authoring",
        "kind": "worker_wall",
        "start_field": ["task.started_at"],
        "end_field": "task.finished_at",
        "start": "2026-01-01T10:00:00Z",
        "end": "2026-01-01T11:00:00Z",
    }
    assert db._span_value(span) == (None, "non_canonical_source")


@pytest.mark.usefixtures('configured_roots')
def test_actions_job_span_is_a_qualified_check_wall() -> None:
    span = {
        "stage": "ci_landing",
        "kind": "check_wall",
        "start_field": "github.actions_job.started_at",
        "end_field": "github.actions_job.completed_at",
        "start": "2026-01-01T10:00:00Z",
        "end": "2026-01-01T10:07:00Z",
    }
    interval, reason = db._span_value(span)
    assert reason is None
    assert interval is not None
    assert (interval[1] - interval[0]).total_seconds() == 420.0


@pytest.mark.usefixtures('configured_roots')
def test_help_documents_examples_outputs_exit_codes_and_related(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        db.main(["--help"])
    assert exited.value.code == 0
    text = capsys.readouterr().out
    for section in ("Examples:", "Outputs:", "Side effects: none", "Exit codes:", "Related:"):
        assert section in text
    assert ".venv/bin/python -m lu_machinery.ci.delivery_baseline cohort.json" in text
    assert "docs/design/agent-friendly-delivery-baseline.md" in text




_DELIVERY_FIXTURES = {'not_delivered.json': '{\n  "claims": [],\n  "records": [\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "79fcf1fefa0488d58da89d17113fc8394d42889b",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": null,\n        "pr": null\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "79fcf1fefa0488d58da89d17113fc8394d42889b",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-unlinked"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": null,\n        "merged_at": null\n      },\n      "outcome": {\n        "issue": 105,\n        "pr": 205\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "c59a557ec71efd04b2efceff8e4c1700f1d00c7c",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-no-merge"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "3ad551a1686506b1ad719a53a336d017a8b88ad8",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 106,\n        "pr": 206\n      },\n      "review_of_record": null,\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-no-review"\n    },\n    {\n      "arm": "before",\n      "cleanup": null,\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "fe8a0c5241b089245650f0b2200a81b915566cb6",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 107,\n        "pr": 207\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "fe8a0c5241b089245650f0b2200a81b915566cb6",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-no-cleanup"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "a56e71690126e263f85746c719008e42788c829f",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 108,\n        "pr": 208\n      },\n      "review_of_record": {\n        "author_family": "openai",\n        "head_sha": "a56e71690126e263f85746c719008e42788c829f",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-self-review"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "7c95462a077516ab667ee2b6c2f48b871f3cf72d",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 109,\n        "pr": 209\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-stale-head"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "049a3421230f4ed7b38fd2153a862aa6b44ee299",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 110,\n        "pr": 210\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "049a3421230f4ed7b38fd2153a862aa6b44ee299",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "ci_test_repair",\n      "task_id": "fixture-outside-strata"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "exclusion": "dry_run",\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "90fc8ed8d5f805a369ad79a4202e1e6763d310ad",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 111,\n        "pr": 211\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "90fc8ed8d5f805a369ad79a4202e1e6763d310ad",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-excluded-dry-run"\n    }\n  ],\n  "schema_version": "delivery-baseline-input.v1"\n}\n', 'overlapping_spans.json': '{\n  "claims": [],\n  "records": [\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "ae7f76faa206fc03cb77cb54796349bc0ef41d05",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 103,\n        "pr": 203\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "ae7f76faa206fc03cb77cb54796349bc0ef41d05",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T11:10:00Z",\n          "end_field": "github.check_run.completedAt",\n          "kind": "check_wall",\n          "stage": "ci_landing",\n          "start": "2026-01-01T11:00:00Z",\n          "start_field": "github.check_run.startedAt"\n        },\n        {\n          "end": "2026-01-01T11:15:00Z",\n          "end_field": "github.check_run.completedAt",\n          "kind": "check_wall",\n          "stage": "ci_landing",\n          "start": "2026-01-01T11:05:00Z",\n          "start_field": "github.check_run.startedAt"\n        },\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-overlap-1"\n    }\n  ],\n  "schema_version": "delivery-baseline-input.v1"\n}\n', 'unknown_span.json': '{\n  "claims": [],\n  "records": [\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "2d52e1b0cab97dd497d5eb2f6dbdc86bcc4ea359",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 102,\n        "pr": 202\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "2d52e1b0cab97dd497d5eb2f6dbdc86bcc4ea359",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": null,\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        },\n        {\n          "end": "2026-01-01T11:30:00Z",\n          "end_field": "review_task.finished_at",\n          "kind": "worker_wall",\n          "stage": "verification_review",\n          "start": "2026-01-01T11:00:00Z",\n          "start_field": "file.mtime"\n        },\n        {\n          "end": "2026-01-01T09:01:00Z",\n          "end_field": "canary.checked_at",\n          "kind": "elapsed",\n          "stage": "onboarding",\n          "start": "2026-01-01T09:00:00Z",\n          "start_field": "session.capsule.generated_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-missing-1"\n    }\n  ],\n  "schema_version": "delivery-baseline-input.v1"\n}\n', 'stratum_floor.json': '{\n  "claims": [\n    {\n      "metric": "authoring.worker_wall",\n      "stratum": "routine_product_change"\n    },\n    {\n      "metric": "authoring.worker_wall",\n      "stratum": "architecture_change"\n    },\n    {\n      "metric": "authoring.worker_wall",\n      "stratum": "launcher_repair"\n    },\n    {\n      "metric": "ci_landing.check_wall",\n      "stratum": "launcher_repair"\n    },\n    {\n      "metric": "authoring.worker_wall",\n      "stratum": "pooled"\n    }\n  ],\n  "records": [\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "20b722634a85ecfa856951bb29b1a20f2f0b8475",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 121,\n        "pr": 221\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "20b722634a85ecfa856951bb29b1a20f2f0b8475",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-floor-before-routine_product_change-0"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "b0a98189fb4e4bbfaea50f4a5adfe4e02e68d191",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 122,\n        "pr": 222\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "b0a98189fb4e4bbfaea50f4a5adfe4e02e68d191",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:31:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-floor-before-routine_product_change-1"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "67efa8c1b52de2e8a01eeed7508198cacb9d4be4",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 123,\n        "pr": 223\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "67efa8c1b52de2e8a01eeed7508198cacb9d4be4",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:32:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-floor-before-routine_product_change-2"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "a8a2e4738d58664089b9055e479b5b2f1a48044b",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 124,\n        "pr": 224\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "a8a2e4738d58664089b9055e479b5b2f1a48044b",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:33:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-floor-before-routine_product_change-3"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "241d92640a5c6be2626e6a1919053f94451835c1",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 125,\n        "pr": 225\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "241d92640a5c6be2626e6a1919053f94451835c1",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:34:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-floor-before-routine_product_change-4"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "e2ec6090273decb145650410cae353dfcb1ea72f",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 126,\n        "pr": 226\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "e2ec6090273decb145650410cae353dfcb1ea72f",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "architecture_change",\n      "task_id": "fixture-floor-before-architecture_change-0"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "7c97bbdbcc8c80d9478b42b0eb2306258ae1478e",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 127,\n        "pr": 227\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "7c97bbdbcc8c80d9478b42b0eb2306258ae1478e",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:30:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-before-launcher_repair-0"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "ce415d5016b64e0afb37811bc66311fffff37e09",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 128,\n        "pr": 228\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "ce415d5016b64e0afb37811bc66311fffff37e09",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:31:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-before-launcher_repair-1"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "ee1fa2536958e7482661136bd9029a9f52dbf287",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 129,\n        "pr": 229\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "ee1fa2536958e7482661136bd9029a9f52dbf287",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:32:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-before-launcher_repair-2"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "1db4aec2238a3601c9ccdd51c8654550488eea7e",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 130,\n        "pr": 230\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "1db4aec2238a3601c9ccdd51c8654550488eea7e",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:33:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-before-launcher_repair-3"\n    },\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "2f0c49e874cda14916f3935d9ee9996149378f42",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 131,\n        "pr": 231\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "2f0c49e874cda14916f3935d9ee9996149378f42",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:34:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-before-launcher_repair-4"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "48833093f3446570c2be06e2de59b6d47a8cd8e5",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 132,\n        "pr": 232\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "48833093f3446570c2be06e2de59b6d47a8cd8e5",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:20:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-floor-after-routine_product_change-0"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "8f65ffecfef4690ce1fd6844cb61273a4c397f4c",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 133,\n        "pr": 233\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "8f65ffecfef4690ce1fd6844cb61273a4c397f4c",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:20:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "architecture_change",\n      "task_id": "fixture-floor-after-architecture_change-0"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "7e4d12be94736f1bc2c4563c9adac9090f4aacad",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 134,\n        "pr": 234\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "7e4d12be94736f1bc2c4563c9adac9090f4aacad",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:21:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "architecture_change",\n      "task_id": "fixture-floor-after-architecture_change-1"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "e460453df365a778100765cd978e2af8df36ccc0",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 135,\n        "pr": 235\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "e460453df365a778100765cd978e2af8df36ccc0",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:22:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "architecture_change",\n      "task_id": "fixture-floor-after-architecture_change-2"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "68d02d7aae3723d3195e21417678d7aae464b0b2",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 136,\n        "pr": 236\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "68d02d7aae3723d3195e21417678d7aae464b0b2",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:23:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "architecture_change",\n      "task_id": "fixture-floor-after-architecture_change-3"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "7d97284cc41761b68519c23ab37af9370b7625a0",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 137,\n        "pr": 237\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "7d97284cc41761b68519c23ab37af9370b7625a0",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:24:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "architecture_change",\n      "task_id": "fixture-floor-after-architecture_change-4"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "691861ca7cf9587fd73030fc08b7e3ec021d8fca",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 138,\n        "pr": 238\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "691861ca7cf9587fd73030fc08b7e3ec021d8fca",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:20:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-after-launcher_repair-0"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "7a54b8b8c42e43aeb41547b26186cd44dd43320d",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 139,\n        "pr": 239\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "7a54b8b8c42e43aeb41547b26186cd44dd43320d",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:21:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-after-launcher_repair-1"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "b00777be31393caa0a053ceb3e7b1380abc2d99a",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 140,\n        "pr": 240\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "b00777be31393caa0a053ceb3e7b1380abc2d99a",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:22:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-after-launcher_repair-2"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "c6e1da49afe710a6295f141349ca1bcd8730bf3d",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 141,\n        "pr": 241\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "c6e1da49afe710a6295f141349ca1bcd8730bf3d",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:23:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-after-launcher_repair-3"\n    },\n    {\n      "arm": "after",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "e799daf5eff4196b7bb2636f9c7a575a2bbe1ffd",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 142,\n        "pr": 242\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "e799daf5eff4196b7bb2636f9c7a575a2bbe1ffd",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:24:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        }\n      ],\n      "stratum": "launcher_repair",\n      "task_id": "fixture-floor-after-launcher_repair-4"\n    }\n  ],\n  "schema_version": "delivery-baseline-input.v1"\n}\n', 'canonical_linked.json': '{\n  "claims": [],\n  "records": [\n    {\n      "arm": "before",\n      "cleanup": {\n        "observed_at": "2026-01-02T13:00:00Z",\n        "state": "CLEANED_UP"\n      },\n      "matched_setup": {\n        "required_check": "CI Gate",\n        "review_profile": "code"\n      },\n      "merge": {\n        "head_sha": "5341a204ff752d90057e8280cf1619974298452f",\n        "merged_at": "2026-01-02T12:00:00Z"\n      },\n      "outcome": {\n        "issue": 101,\n        "pr": 201\n      },\n      "review_of_record": {\n        "author_family": "anthropic",\n        "head_sha": "5341a204ff752d90057e8280cf1619974298452f",\n        "reviewer_family": "openai",\n        "verdict": "APPROVE"\n      },\n      "spans": [\n        {\n          "end": "2026-01-01T10:00:00Z",\n          "end_field": "task.started_at",\n          "kind": "elapsed",\n          "stage": "intake",\n          "start": "2026-01-01T09:00:00Z",\n          "start_field": "github.issue.createdAt"\n        },\n        {\n          "end": "2026-01-01T10:45:00Z",\n          "end_field": "task.finished_at",\n          "kind": "worker_wall",\n          "stage": "authoring",\n          "start": "2026-01-01T10:00:00Z",\n          "start_field": "task.started_at"\n        },\n        {\n          "end": "2026-01-01T11:06:00Z",\n          "end_field": "github.check_run.completedAt",\n          "kind": "check_wall",\n          "stage": "ci_landing",\n          "start": "2026-01-01T11:00:00Z",\n          "start_field": "github.check_run.startedAt"\n        },\n        {\n          "end": "2026-01-01T11:00:00Z",\n          "end_field": "github.actions_job.started_at",\n          "kind": "queue_delay",\n          "stage": "ci_landing",\n          "start": "2026-01-01T10:59:00Z",\n          "start_field": "github.actions_job.created_at"\n        },\n        {\n          "end": "2026-01-02T12:00:00Z",\n          "end_field": "github.pr.mergedAt",\n          "kind": "elapsed",\n          "stage": "ci_landing",\n          "start": "2026-01-01T10:50:00Z",\n          "start_field": "github.pr.createdAt"\n        }\n      ],\n      "stratum": "routine_product_change",\n      "task_id": "fixture-canonical-1"\n    }\n  ],\n  "schema_version": "delivery-baseline-input.v1"\n}\n'}


@pytest.fixture
def delivery_fixture_paths(tmp_path, monkeypatch):
    directory = tmp_path / "delivery-baseline-fixtures"
    directory.mkdir()
    for name, text in _DELIVERY_FIXTURES.items():
        (directory / name).write_text(text, encoding="utf-8")
    monkeypatch.setitem(globals(), "FIXTURES", directory)

pytestmark = [pytest.mark.usefixtures('github_command_boundary')]
