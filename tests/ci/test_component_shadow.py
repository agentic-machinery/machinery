"""component_shadow scoring-instant handling (#100 R-9721)."""



import pytest
from lu_machinery.ci import component_shadow
import argparse
import copy
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from lu_machinery.ci import component_shadow as s
from lu_machinery.ci import components as c
from lu_machinery.ci import pytest_report
from tests.helpers.product_tree import configured_roots as configured_roots
from pathlib import Path

@pytest.mark.parametrize("bad", ["", "   ", "not-a-time"])
def test_check_refuses_an_explicit_empty_or_malformed_now(bad):
    """An explicit empty --now must be refused, not silently replaced by the current time."""
    with pytest.raises(ValueError):
        component_shadow.check({}, None, now=bad)


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    for name in s.TOOLS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((c.ROOT / name).read_bytes())
    for relative in ("scripts/ci/components.json", "scripts/ci/dependency_change_denominator.json", "scripts/ci/frontend_change_denominator.json"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((c.ROOT / relative).read_bytes())
    path = tmp_path / "tests/test_sample.py"
    path.parent.mkdir()
    path.write_text("def test_sample():\n    assert True\n")
    commit(tmp_path)
    return tmp_path


@pytest.fixture
def static_graph(monkeypatch):
    graph = {"file_edges": [], "node_edges": [], "unresolved_edges": [], "missing_mandatory_edges": []}
    monkeypatch.setattr(c, "import_graph", lambda *args: graph)
    return graph


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_empty_diff_and_missing_base_are_full(repo, static_graph):
    head = git(repo, "rev-parse", "HEAD")
    for base, reason in [(head, "empty-diff"), ("", "missing-base"), ("no-such-base", "diff-unavailable")]:
        result = s.select(base, head, "pull_request", repo)
        assert result["mode"] == "full"
        assert result["reason"] == reason
        assert result["selected_test_files"] == ["tests/test_sample.py"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_historical_subject_can_predate_observer_tools(repo, static_graph):
    for name in s.TOOLS:
        if name.startswith("lu_machinery/"):
            (repo / name).unlink()
    head = commit(repo)
    result = s.select(head, head, "pull_request", repo)
    assert result["mode"] == "full"
    assert set(result["identities"]["tool_hashes"]) == set(s.TOOLS)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("event", ["workflow_dispatch", "merge_group", "schedule"])
def test_non_pr_events_always_full(repo, static_graph, event):
    assert s.select("", "HEAD", event, repo)["reason"] == "non-pr-event"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_stacked_pr_diff_includes_parent_and_ignores_new_base_commits(repo, static_graph):
    base = git(repo, "rev-parse", "HEAD")
    parent = repo / "site/parent.ts"
    parent.parent.mkdir()
    parent.write_text("parent")
    commit(repo)
    (repo / "site/top.ts").write_text("top")
    head = commit(repo)
    # Construct the advanced base with plumbing, without branch switching.
    git(repo, "read-tree", base)
    (repo / "site/base-only.ts").write_text("base")
    git(repo, "add", "site/base-only.ts")
    tree = git(repo, "write-tree")
    advanced_base = git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit-tree", tree, "-p", base, "-m", "advanced base")
    git(repo, "read-tree", head)
    result = s.select(advanced_base, head, "pull_request", repo)
    assert result["changed_paths"] == ["site/parent.ts", "site/top.ts"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_renames_and_deletions_include_both_sides(repo, static_graph):
    path = repo / "site/old.ts"
    path.parent.mkdir()
    path.write_text("unchanged\n" * 20)
    deleted = repo / "site/deleted.ts"
    deleted.write_text("delete")
    base = commit(repo)
    path.rename(repo / "site/new.ts")
    deleted.unlink()
    head = commit(repo)
    result = s.select(base, head, "pull_request", repo)
    assert result["changed_paths"] == ["site/deleted.ts", "site/new.ts", "site/old.ts"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("path,reason", [
    (".github/test.txt", "workflow-tool-or-dependency"),
    ("site/pnpm-lock.yaml", "workflow-tool-or-dependency"),
    ("site/nested/package.json", "workflow-tool-or-dependency"),
    ("site/nested/requirements.in", "workflow-tool-or-dependency"),
    ("requirements-new.txt", "workflow-tool-or-dependency"),
    ("scripts/ci/new.py", "workflow-tool-or-dependency"),
    ("scripts/config.py", "shared-core"),
    ("unknown/new.txt", "unmapped"),
])
def test_full_triggers(repo, static_graph, path, reason):
    base = git(repo, "rev-parse", "HEAD")
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("changed")
    head = commit(repo)
    result = s.select(base, head, "pull_request", repo)
    assert result["mode"] == "full"
    assert result["reason"] == reason


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_selection_uses_resolved_importers_and_integration(repo, static_graph):
    source = "scripts/projects/open_model_data/new.py"
    test = repo / "tests/test_importer.py"
    test.write_text("import scripts.projects.open_model_data.new\n")
    base = commit(repo)
    path = repo / source
    path.parent.mkdir(parents=True)
    path.write_text("VALUE=1")
    head = commit(repo)
    static_graph["file_edges"] = [("tests/test_importer.py", source)]
    result = s.select(base, head, "pull_request", repo)
    assert result["mode"] == "selected"
    assert result["selected_nodes"] == ["open-model-data"]
    assert "tests/test_importer.py" in result["selected_test_files"]
    assert set(c.load_manifest()["shared_integration_tests"]) <= set(result["selected_test_files"])


IN_WINDOW = "2026-10-09T00:00:00Z"


@pytest.fixture
def registration():
    return {"schema": s.SCHEMA, "registered_at": "2026-10-07T00:00:00Z", "stop_at": "2026-11-06T00:00:00Z",
            "identities": {"tool_hashes": {"tool": "hash"}}, "historical_run_ids": ["999"],
            "minimum_narrowed_cases": 1,
            "live_window": {"completed_first_attempt_pr_runs": 150, "minimum_pytest_red_runs": 30},
            "flaky_classification_rule": s.FLAKE_RULE, "base_commit_rerun_rule": s.BASE_RULE,
            "injections_frozen": True, "injected_cases": [
                {"id": "injection-" + label, "coverage": [label], "registered_at": "2026-10-07T00:00:00Z",
                 "expected_failure_ids": ["tests/test_selected.py::test_fault"]}
                for label in sorted(s.INJECTION_COVERAGE)]}


def receipt(run_id, kind="historical", **overrides):
    return {"schema": s.SCHEMA, "kind": kind, "run_id": str(run_id), "run_attempt": 1,
            "head_sha": "head", "base_sha": "base", "observed_at": "2026-10-08T00:00:00Z",
            "mode": "selected", "would_skip_test_files": ["tests/test_other.py"],
            "selected_test_files": ["tests/test_selected.py"], "full_junit_failing_ids": [],
            "collection_errors": [], "artifact_problems": [], "junit_hashes": {"shard.xml": "hash"},
            "identities": {"tool_hashes": {"tool": "hash"}}, **overrides}


def save(root, value, name=None):
    if value.get("schema") == s.SCHEMA:
        suite = ET.Element("testsuite")
        failures = set(value["full_junit_failing_ids"]) | set(value["collection_errors"])
        for failure in sorted(failures or {"tests/test_selected.py::test_pass"}):
            file, *suffix = failure.split("::")
            if suffix:
                case = ET.SubElement(suite, "testcase", classname=file[:-3].replace("/", "."), name="::".join(suffix))
            else:
                case = ET.SubElement(suite, "testcase", name=file[:-3].replace("/", "."))
            if failure in failures:
                ET.SubElement(case, "error" if failure in value["collection_errors"] else "failure")
        artifact = root / str(value["run_id"]) / "junit" / "shard.xml"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(ET.tostring(suite))
        if value.get("junit_hashes"):
            value["junit_hashes"] = {artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()}
        value["full_junit_failing_ids"] = sorted(failures)
    path = root / (name or str(value["run_id"]) + ".json")
    path.write_text(json.dumps(value))


@pytest.fixture
def window(tmp_path):
    rows = [{"run_id": str(i), "event": "pull_request", "run_attempt": 1, "status": "completed",
             "created_at": "2026-10-07T01:00:00Z", "completed_at": "2026-10-08T00:00:00Z",
             "head_sha": "head", "pytest_red": i <= 30} for i in range(1, 151)]
    save(tmp_path, {"complete": True, "first_attempts": True, "runs": rows}, "runs.json")
    for row in rows:
        save(tmp_path, receipt(row["run_id"], "live"))
    save(tmp_path, receipt("999"))
    for label in s.INJECTION_COVERAGE:
        save(tmp_path, receipt("injection-" + label, "injected",
                               full_junit_failing_ids=["tests/test_selected.py::test_fault"]))
    return tmp_path


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_complete_window_zero_misses_is_the_only_success(registration, window):
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["status"] == "pass"
    assert result["registered_case_count"] == 163
    assert result["narrowed_red_case_count"] == 31
    assert result["narrowed_injected_case_count"] == 12
    assert result["narrowed_case_count"] == 43
    assert result["missed_failure_count"] == result["unresolved_count"] == 0


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("mode", ["full", "selected"])
def test_window_without_any_skipped_files_cannot_pass(registration, window, mode):
    for path in window.glob("*.json"):
        value = json.loads(path.read_text())
        if value.get("schema") == s.SCHEMA:
            value.update(mode=mode, would_skip_test_files=[])
            path.write_text(json.dumps(value))
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["status"] == "unresolved"
    assert result["narrowed_case_count"] == 0
    assert result["narrowed_red_case_count"] == result["narrowed_injected_case_count"] == 0
    assert "narrowed-cases:0/1" in result["unresolved"]
    assert s.main(["check", "--registration", str(_registration_file(window, registration)),
                   "--receipts", str(window), "--now", IN_WINDOW]) == 1


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_narrowing_below_registered_minimum_cannot_pass(registration, window):
    registration["minimum_narrowed_cases"] = 44
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["status"] == "unresolved"
    assert result["narrowed_case_count"] == 43
    assert result["minimum_narrowed_cases"] == 44
    assert "narrowed-cases:43/44" in result["unresolved"]
    assert s.main(["check", "--registration", str(_registration_file(window, registration)),
                   "--receipts", str(window), "--now", IN_WINDOW]) == 1
    registration["minimum_narrowed_cases"] = 43
    assert s.check(registration, window, now=IN_WINDOW)["status"] == "pass"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("minimum", [None, 0, -1, True, "1"])
def test_missing_or_invalid_registered_narrowing_minimum_is_unresolved(registration, window, minimum):
    registration["minimum_narrowed_cases"] = minimum
    assert "minimum-narrowed-cases-not-pre-registered" in s.check(registration, window, now=IN_WINDOW)["unresolved"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("fields", [
    {"mode": "full", "would_skip_test_files": ["tests/test_other.py"]},
    {"mode": "selected", "would_skip_test_files": ["tests/test_selected.py"]},
    {"mode": None}, {"would_skip_test_files": None},
])
def test_invalid_narrowing_evidence_is_not_counted(registration, window, fields):
    save(window, receipt("999", **fields))
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["narrowed_red_case_count"] == 30
    assert result["status"] == "unresolved"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_in_flight_run_completed_after_registration_enters_window(registration, window):
    census = json.loads((window / "runs.json").read_text())
    census["runs"][0]["created_at"] = "2026-10-06T23:59:00Z"
    save(window, census, "runs.json")
    assert s.check(registration, window, now=IN_WINDOW)["status"] == "pass"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_every_skipped_failure_is_counted(registration, window):
    save(window, receipt("999", full_junit_failing_ids=["tests/test_skip.py::test_a", "tests/test_skip.py::test_b"]))
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["missed_failure_count"] == 2
    assert result["status"] == "fail"
    assert s.main(["check", "--registration", str(_registration_file(window, registration)),
                   "--receipts", str(window), "--now", IN_WINDOW]) == 1


def _registration_file(root, registration):
    path = root / "registration.input"
    path.write_text(json.dumps(registration))
    return path


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_check_cli_now_controls_stop_boundary_without_real_clock(registration, window, monkeypatch, capsys):
    """Prevent real-clock dependence from changing unresolved CLI results at the stop boundary."""
    (window / "999.json").unlink()
    registration_path = _registration_file(window, registration)

    def unexpected_real_clock():
        raise AssertionError("check --now must not read the real clock")

    monkeypatch.setattr(s, "utc_now", unexpected_real_clock)
    for now, status, stopped in [
        ("2026-11-05T23:59:59.999999Z", "unresolved", False),
        (registration["stop_at"], "inconclusive", True),
    ]:
        assert s.main(["check", "--registration", str(registration_path),
                       "--receipts", str(window), "--now", now]) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == status
        assert result["day_30_stop"] is stopped


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_pre_registered_base_rerun_exempts_only_identical_id(registration, window):
    save(window, receipt("999", full_junit_failing_ids=["tests/test_skip.py::test_a", "tests/test_skip.py::test_b"]))
    save(window, receipt("base-99", "base", candidate_run_id="999", head_sha="base",
                         full_junit_failing_ids=["tests/test_skip.py::test_a"]), "base.json")
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["missed_failure_count"] == 1
    assert result["base_exemptions"] == [{"run_id": "999", "test_id": "tests/test_skip.py::test_a"}]
    wrong_rule = copy.deepcopy(registration)
    wrong_rule["base_commit_rerun_rule"] = "after the fact exemption"
    with pytest.raises(ValueError, match="required rules"):
        s.check(wrong_rule, window, now=IN_WINDOW)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_collection_error_outside_selection_counts_even_with_base_failure(registration, window):
    save(window, receipt("999", collection_errors=["tests/test_skip.py"]))
    save(window, receipt("base-99", "base", candidate_run_id="999", head_sha="base",
                         full_junit_failing_ids=["tests/test_skip.py"]), "base.json")
    assert s.check(registration, window, now=IN_WINDOW)["missed_failure_count"] == 1


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("problem", ["missing", "artifactless", "unreproducible"])
def test_unresolved_historical_cases_block_success(registration, window, problem):
    if problem == "missing":
        (window / "999.json").unlink()
    elif problem == "artifactless":
        save(window, receipt("999", junit_hashes={}))
    else:
        save(window, receipt("999", artifact_problems=["cannot-reproduce"]))
    result = s.check(registration, window, now="2026-11-07T00:00:00Z")
    assert result["status"] == "inconclusive"
    assert result["unresolved_count"] == 1
    assert s.main(["check", "--registration", str(_registration_file(window, registration)),
                   "--receipts", str(window), "--now", "2026-11-07T00:00:00Z"]) == 1


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_registration_after_live_results_is_refused_but_historical_is_allowed(registration, window):
    save(window, receipt("999", observed_at="2026-10-03T00:00:00Z"))
    assert not s.check(registration, window, now=IN_WINDOW)["unresolved_count"]
    save(window, receipt("1", "live", observed_at=registration["registered_at"]))
    with pytest.raises(ValueError, match="registration-after-results"):
        s.check(registration, window, now=IN_WINDOW)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_missing_live_census_or_red_denominator_cannot_pass(registration, window):
    (window / "runs.json").unlink()
    result = s.check(registration, window, now=IN_WINDOW)
    assert result["unresolved_count"] >= 3
    assert result["missed_failure_count"] == 0


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_oracle_rejects_altered_receipt_and_missing_artifacts(registration, window):
    value = receipt("999", full_junit_failing_ids=["tests/test_skip.py::test_failure"])
    save(window, value)
    value["full_junit_failing_ids"] = []
    (window / "999.json").write_text(json.dumps(value))
    result = s.check(registration, window, now=IN_WINDOW)
    assert "oracle-unavailable-or-mismatched:999" in result["unresolved"]
    (window / "1/junit/shard.xml").unlink()
    assert "oracle-unavailable-or-mismatched:1" in s.check(registration, window, now=IN_WINDOW)["unresolved"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_unobserved_injected_fault_is_unresolved(registration, window):
    save(window, receipt("injection-harness", "injected"))
    assert "injection-not-demonstrated:injection-harness" in s.check(registration, window, now=IN_WINDOW)["unresolved"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_day_30_does_not_accept_late_evidence(registration, window):
    save(window, receipt("1", "live", observed_at="2026-11-07T00:00:00Z"))
    result = s.check(registration, window, now="2026-11-07T00:00:00Z")
    assert result["status"] == "inconclusive"
    assert "receipt-after-day-30:1" in result["unresolved"]


def shard_artifacts(root, directory):
    directory.mkdir()
    (directory / "pytest-shard-1-files.txt").write_text("tests/test_sample.py\n")
    (directory / "pytest-shard-1-needs-artifact.txt").write_text("")
    (directory / "pytest-shard-1.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_sample" name="test_sample" time="2"/>'
        '</testsuite></testsuites>')
    expected = root / pytest_report.EXPECTED_ARTIFACT_SKIPS
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_text("")


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_full_report_identical_before_and_after_shadow(repo, static_graph, tmp_path, capsys):
    directory = tmp_path / "artifacts"
    shard_artifacts(repo, directory)
    output = tmp_path / "tested-tree.json"
    args = ["--root", str(repo), "--results", str(directory), "--record", str(output)]
    assert pytest_report.main(args) == 0
    original = output.read_bytes(), capsys.readouterr().out
    s.report(argparse.Namespace(root=repo, results=directory, output=tmp_path / "shadow.json", event="pull_request",
             base=git(repo, "rev-parse", "HEAD"), head="HEAD", shards=1, kind="live", run_id="123",
             attempt=1, candidate_run_id="", cost=None))
    assert pytest_report.main(args) == 0
    assert (output.read_bytes(), capsys.readouterr().out) == original
    assert json.loads(output.read_text())["tier"] == "full"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_junit_failures_collection_errors_and_missing_shards(repo, tmp_path):
    directory = tmp_path / "artifacts"
    shard_artifacts(repo, directory)
    (directory / "pytest-shard-1.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_sample" name="test_a"><failure/></testcase>'
        '<testcase classname="tests.test_sample" name="test_b"><failure/></testcase>'
        '<testcase name="tests.test_broken"><error message="collection failure"/></testcase>'
        '</testsuite></testsuites>')
    result = s.read_full_results(directory, repo, 1)
    assert result["artifact_problems"] == []
    assert result["full_junit_failing_ids"] == ["tests/test_broken.py", "tests/test_sample.py::test_a", "tests/test_sample.py::test_b"]
    assert result["collection_errors"] == ["tests/test_broken.py"]
    assert result["executed_test_ids"] == ["tests/test_sample.py::test_a", "tests/test_sample.py::test_b"]
    assert "missing-or-duplicate-junit-shards" in s.read_full_results(directory, repo, 2)["artifact_problems"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_junit_duplicate_failure_followed_by_pass_is_retained(repo, tmp_path):
    directory = tmp_path / "artifacts"
    shard_artifacts(repo, directory)
    (directory / "pytest-shard-1.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.test_sample" name="test_a"><failure/></testcase>'
        '<testcase classname="tests.test_sample" name="test_a"/></testsuite></testsuites>')
    result = s.read_full_results(directory, repo, 1)
    assert result["artifact_problems"] == []
    assert result["full_junit_failing_ids"] == ["tests/test_sample.py::test_a"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_report_unavailable_evidence_is_zero_exit_and_unresolved(repo, static_graph, tmp_path):
    output = tmp_path / "shadow.json"
    assert s.main(["report", "--root", str(repo), "--results", str(tmp_path / "missing"), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["artifact_problems"]
    census = result["review_unresolved_edge_census"]
    assert census["total"] == sum(census["by_kind"].values()) == 14692
    assert census["head_sha"] == "c3695226db3b18ec0e3ab42c9a7a3221e00df69c"
    assert result["projected_cost_after_lost_reuse"]["unknown_reason"] == "matched-pr-queue-cost-inputs-not-supplied"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_register_binds_historical_ids_hashes_rules_and_stop(repo, static_graph):
    baseline = {"complete": True, "created_window": s.BASELINE, "runs": [
        {"run_id": "42", "event": "pull_request", "pytest_red": True},
        {"run_id": "43", "event": "merge_group", "pytest_red": True},
        {"run_id": "44", "event": "pull_request", "pytest_red": False}]}
    result = s.register(baseline, repo)
    assert result["historical_run_ids"] == ["42", "43"]
    assert set(result["identities"]["tool_hashes"]) == set(s.TOOLS)
    assert s.instant(result["stop_at"]) - s.instant(result["registered_at"]) == s.timedelta(days=30)
    assert result["injections_frozen"] is False
    assert result["minimum_narrowed_cases"] == 1
    assert s.register(baseline, repo, minimum_narrowed_cases=44)["minimum_narrowed_cases"] == 44
    with pytest.raises(ValueError, match="positive integer"):
        s.register(baseline, repo, minimum_narrowed_cases=0)
    with pytest.raises(ValueError, match="complete baseline"):
        s.register({**baseline, "complete": False}, repo)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_registration_cli_refuses_overwrite(repo, static_graph, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"complete": True, "created_window": s.BASELINE, "runs": [
        {"run_id": "42", "event": "pull_request", "pytest_red": True}]}))
    output = tmp_path / "registration.json"
    args = ["register", "--root", str(repo), "--baseline", str(baseline), "--output", str(output),
            "--minimum-narrowed-cases", "44"]
    assert s.main(args) == 0
    assert json.loads(output.read_text())["minimum_narrowed_cases"] == 44
    before = output.read_bytes()
    assert s.main(args) == 2
    assert output.read_bytes() == before


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_gh_pages_refuses_incomplete_census(github_transport):
    from lu_machinery.common.github_client import Response

    github_transport(lambda *args: Response(200, {}, b'{"total_count":2,"jobs":[{"id":1}]}'))
    with pytest.raises(ValueError, match="incomplete GitHub census"):
        s.gh_pages("repos/owner/repo/actions/runs/42/jobs", "jobs")


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_live_inventory_uses_first_attempt_even_after_rerun(github_transport):
    from lu_machinery.common.github_client import Response

    run = {"id": 42, "event": "pull_request", "status": "completed", "conclusion": "success",
           "run_attempt": 2, "created_at": "2026-10-08T00:00:00Z", "updated_at": "2026-10-09T00:00:00Z", "head_sha": "head"}

    def transport(method, endpoint, headers, body, timeout):
        assert method == "GET" and body is None
        if "/workflows/" in endpoint:
            payload = {"total_count": 1, "workflow_runs": [run]}
        elif endpoint.endswith("/attempts/1"):
            payload = {**run, "run_attempt": 1, "conclusion": "failure"}
        else:
            assert endpoint.endswith("/attempts/1/jobs?per_page=100")
            payload = {"total_count": 1, "jobs": [{"name": "pytest (1)", "conclusion": "failure",
                                                     "completed_at": "2026-10-08T01:00:00Z"}]}
        return Response(200, {}, json.dumps(payload).encode())

    calls = github_transport(transport)
    result = s.inventory("owner/repo", "start..stop", first_attempts=True)
    assert result["first_attempts"] is True
    assert result["runs"][0]["run_attempt"] == 1
    assert result["runs"][0]["pytest_red"] is True
    assert [call[1] for call in calls] == [
        "repos/owner/repo/actions/workflows/ci.yml/runs?created=start..stop&per_page=100",
        "repos/owner/repo/actions/runs/42/attempts/1",
        "repos/owner/repo/actions/runs/42/attempts/1/jobs?per_page=100",
    ]






_COMPONENT_TOOL_PATHS = ['ci/components.json', 'ci/component_shadow.py', 'ci/components.py', 'ci/junit_results.py', 'ci/dependency_change_scope.py', 'ci/dependency_change_denominator.json', 'ci/pytest_report.py', 'ci/frontend_change_scope.py', 'ci/frontend_change_denominator.json', 'common/repo_root.py', 'common/jsonl.py', 'storage/test_baseline.py', 'ci/deploy_paths.py']


_COMPONENT_FIXTURE_BYTES = {'scripts/ci/components.json': b'{\n  "schema_version": 1,\n  "plan": "plans/component-separation-9721.md",\n  "plan_version": 2,\n  "baseline": "dff595c12a714713f7fb8f9f18ff5245283adf7e",\n  "description": "Index-backed path ownership and complete node test sets, including computed Python importers and shared integration tests. Computed unresolved edges force all-node selection and retain their test importers in every node set; no CI selection or independence certification.",\n  "components": {\n    "open-model-data": {\n      "lane": "open-model-data",\n      "test_files": [\n        "tests/test_open_model_view_exporter.py",\n        "tests/test_open_model_ready_view_production.py",\n        "tests/test_model_ready_receipt_audit.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [\n        "-m",\n        "not needs_artifact"\n      ],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_open_model_view_exporter.py",\n            "tests/test_open_model_ready_view_production.py",\n            "tests/test_model_ready_receipt_audit.py",\n            "-m",\n            "not needs_artifact"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.projects.open_model_data.model_view_exporter",\n            "continued-pretraining",\n            "--source-records",\n            "{member:source-records}",\n            "--payloads",\n            "{member:payloads}",\n            "--origin",\n            "human_authored",\n            "--representation-view",\n            "faithful_literary",\n            "--output",\n            "{output_dir}/view.jsonl",\n            "--receipt-output",\n            "{output_dir}/view-receipt.json"\n          ],\n          "cwd": ".",\n          "scope": "model-view"\n        }\n      ],\n      "input_families": [\n        "source-records",\n        "payloads",\n        "capability-policy"\n      ],\n      "output_families": [\n        "model-view",\n        "model-view-receipt"\n      ]\n    },\n    "atlas-data": {\n      "lane": "atlas",\n      "test_files": [\n        "tests/test_atlas_db.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_atlas_db.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.atlas.atlas_db",\n            "--manifest",\n            "{member:lexicon-manifest}",\n            "--db",\n            "{output_dir}/atlas.db",\n            "--curated-aliases",\n            "{member:curated-aliases}"\n          ],\n          "cwd": ".",\n          "scope": "atlas-db"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.audit.generate_search_index",\n            "--db",\n            "{output_dir}/atlas.db",\n            "--out",\n            "{output_dir}/search.json",\n            "--aliases-out",\n            "{output_dir}/aliases.json",\n            "--search-shards-out",\n            "{output_dir}/search-shards.json",\n            "--search-shard-dir",\n            "{output_dir}/search",\n            "--browse-meta-out",\n            "{output_dir}/browse-meta.json",\n            "--browse-flagged-out",\n            "{output_dir}/browse-flagged.json",\n            "--browse-dir",\n            "{output_dir}/browse"\n          ],\n          "cwd": ".",\n          "scope": "atlas-search"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.audit.generate_daily_pool",\n            "--db",\n            "{output_dir}/atlas.db",\n            "--out",\n            "{output_dir}/daily.json"\n          ],\n          "cwd": ".",\n          "scope": "atlas-daily"\n        }\n      ],\n      "input_families": [\n        "lexicon-manifest",\n        "curated-aliases",\n        "curriculum-vocabulary"\n      ],\n      "output_families": [\n        "atlas-db",\n        "atlas-search",\n        "atlas-daily",\n        "open-dataset"\n      ],\n      "preparation_commands": [\n        [\n          "npm",\n          "--prefix",\n          "site",\n          "run",\n          "hydrate:manifest"\n        ]\n      ]\n    },\n    "atlas-frontend": {\n      "lane": "atlas",\n      "test_files": [\n        "tests/test_frontend_change_scope.py",\n        "tests/test_deploy_pages_workflow.py",\n        "tests/test_auto_deploy_component_parity.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_frontend_change_scope.py",\n            "tests/test_deploy_pages_workflow.py",\n            "tests/test_auto_deploy_component_parity.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "npm",\n            "run",\n            "build:shell",\n            "--",\n            "--outDir",\n            "{output_dir}"\n          ],\n          "cwd": "site",\n          "scope": "shared-astro"\n        }\n      ],\n      "input_families": [\n        "frontend-runtime"\n      ],\n      "output_families": [\n        "shared-astro"\n      ],\n      "build_scope": "shared-astro"\n    },\n    "practice-frontend": {\n      "lane": "atlas",\n      "test_files": [\n        "tests/test_practice_deck_io.py",\n        "tests/test_makefile_practice_admit.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_practice_deck_io.py",\n            "tests/test_makefile_practice_admit.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "npm",\n            "run",\n            "build:shell",\n            "--",\n            "--outDir",\n            "{output_dir}"\n          ],\n          "cwd": "site",\n          "scope": "shared-astro"\n        }\n      ],\n      "input_families": [\n        "frontend-runtime",\n        "published-practice"\n      ],\n      "output_families": [\n        "shared-astro"\n      ],\n      "preparation_commands": [\n        [\n          "npm",\n          "--prefix",\n          "site",\n          "run",\n          "hydrate:practice"\n        ],\n        [\n          "npm",\n          "--prefix",\n          "site",\n          "run",\n          "hydrate:teacher"\n        ]\n      ],\n      "build_scope": "shared-astro"\n    },\n    "curriculum-generation": {\n      "lane": "curriculum",\n      "test_files": [\n        "tests/build/test_build_landing_pages.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/build/test_build_landing_pages.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.generate_curriculum_stats"\n          ],\n          "cwd": ".",\n          "scope": "canonical-worktree-output"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.build.build_arc_landing",\n            "a1",\n            "--write"\n          ],\n          "cwd": ".",\n          "scope": "canonical-worktree-output"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.build.build_landing_pages"\n          ],\n          "cwd": ".",\n          "scope": "canonical-worktree-output"\n        }\n      ],\n      "input_families": [\n        "curriculum-sources"\n      ],\n      "output_families": [\n        "arc-json",\n        "lesson-mdx",\n        "landing-mdx",\n        "curriculum-stats"\n      ],\n      "preparation_commands": []\n    },\n    "curriculum-display": {\n      "lane": "curriculum",\n      "test_files": [\n        "tests/test_frontend_change_scope.py",\n        "tests/test_auto_deploy_component_parity.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_frontend_change_scope.py",\n            "tests/test_auto_deploy_component_parity.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "npm",\n            "run",\n            "build:shell",\n            "--",\n            "--outDir",\n            "{output_dir}"\n          ],\n          "cwd": "site",\n          "scope": "shared-astro"\n        }\n      ],\n      "input_families": [\n        "frontend-runtime",\n        "curriculum-display"\n      ],\n      "output_families": [\n        "shared-astro"\n      ],\n      "build_scope": "shared-astro"\n    },\n    "harness": {\n      "lane": "infra",\n      "test_files": [\n        "tests/ci/test_components.py",\n        "tests/ci/test_delivery_baseline.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/ci/test_components.py",\n            "tests/ci/test_delivery_baseline.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [],\n      "input_families": [],\n      "output_families": []\n    },\n    "shared-core": {\n      "lane": "infra",\n      "test_files": [\n        "tests/ci/test_components.py",\n        "tests/test_frontend_change_scope.py",\n        "tests/test_auto_deploy_component_parity.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/ci/test_components.py",\n            "tests/test_frontend_change_scope.py",\n            "tests/test_auto_deploy_component_parity.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [],\n      "input_families": [],\n      "output_families": []\n    }\n  },\n  "exact_paths": {\n    ".gitattributes": [\n      "shared-core"\n    ],\n    ".gitignore": [\n      "shared-core"\n    ],\n    ".gitleaks.toml": [\n      "shared-core"\n    ],\n    ".markdownlint.json": [\n      "shared-core"\n    ],\n    ".mcp.json": [\n      "shared-core"\n    ],\n    ".nvmrc": [\n      "shared-core"\n    ],\n    ".pre-commit-config.yaml": [\n      "shared-core"\n    ],\n    ".prettierrc": [\n      "shared-core"\n    ],\n    ".python-version": [\n      "shared-core"\n    ],\n    ".trufflehogignore": [\n      "shared-core"\n    ],\n    ".yamllint": [\n      "shared-core"\n    ],\n    "AGENTS.md": [\n      "shared-core"\n    ],\n    "CLAUDE.md": [\n      "shared-core"\n    ],\n    "CODE_OF_CONDUCT.md": [\n      "shared-core"\n    ],\n    "CONTRIBUTING.md": [\n      "shared-core"\n    ],\n    "GEMINI.md": [\n      "shared-core"\n    ],\n    "LICENSE": [\n      "shared-core"\n    ],\n    "LICENSE-CONTENT.md": [\n      "shared-core"\n    ],\n    "Makefile": [\n      "shared-core"\n    ],\n    "README.md": [\n      "shared-core"\n    ],\n    "RESIDUAL.md": [\n      "shared-core"\n    ],\n    "SECURITY.md": [\n      "shared-core"\n    ],\n    "UI-REVIEW-FINDINGS.md": [\n      "shared-core"\n    ],\n    "eslint.config.mjs": [\n      "shared-core"\n    ],\n    "package-lock.json": [\n      "shared-core"\n    ],\n    "package.json": [\n      "shared-core"\n    ],\n    "pyproject.toml": [\n      "shared-core"\n    ],\n    "pyrightconfig.json": [\n      "shared-core"\n    ],\n    "requirements-dev.txt": [\n      "shared-core"\n    ],\n    "requirements-lock.txt": [\n      "shared-core"\n    ],\n    "requirements.txt": [\n      "shared-core"\n    ],\n    "run-dispatcher.sh": [\n      "harness"\n    ],\n    "scripts/__init__.py": [\n      "shared-core"\n    ],\n    "scripts/audit/daily_cefr.py": [\n      "atlas-data"\n    ],\n    "scripts/audit/generate_daily_pool.py": [\n      "atlas-data"\n    ],\n    "scripts/audit/generate_search_index.py": [\n      "atlas-data"\n    ],\n    "scripts/audit/lexeme_filter.py": [\n      "atlas-data"\n    ],\n    "scripts/build/generate_lesson_schema.py": [\n      "shared-core"\n    ],\n    "scripts/ci/components.json": [\n      "shared-core"\n    ],\n    "scripts/ci/components.py": [\n      "shared-core"\n    ],\n    "scripts/config.py": [\n      "shared-core"\n    ],\n    "scripts/generate_curriculum_stats.py": [\n      "curriculum-generation"\n    ],\n    "scripts/practice/euphony_stem_engine.py": [\n      "shared-core"\n    ],\n    "services.sh": [\n      "harness"\n    ],\n    "site/scripts/generate-teacher-lesson-keys.ts": [\n      "practice-frontend"\n    ],\n    "site/scripts/generate-zno-deck-meta.ts": [\n      "practice-frontend"\n    ],\n    "site/scripts/hydrate-practice-deck.mjs": [\n      "practice-frontend"\n    ],\n    "site/scripts/hydrate-teacher-deck.mjs": [\n      "practice-frontend"\n    ],\n    "site/src/components/ZnoPractice.tsx": [\n      "practice-frontend"\n    ],\n    "site/src/content.config.ts": [\n      "curriculum-display"\n    ],\n    "site/src/data/lexicon-manifest.pointer.json": [\n      "atlas-data"\n    ],\n    "site/src/data/lexicon-practice-deck.pointer.json": [\n      "practice-frontend"\n    ],\n    "site/src/lib/lexicon/custom-decks.ts": [\n      "practice-frontend"\n    ],\n    "site/src/pages/[...slug].astro": [\n      "curriculum-display"\n    ],\n    "start-claude-driver.sh": [\n      "harness"\n    ],\n    "start-claude.sh": [\n      "harness"\n    ],\n    "start-codex-driver.sh": [\n      "harness"\n    ],\n    "start-codex.sh": [\n      "harness"\n    ],\n    "start-gemini-driver.sh": [\n      "harness"\n    ],\n    "start-gemini.sh": [\n      "harness"\n    ],\n    "tests/build/test_build_landing_pages.py": [\n      "curriculum-generation"\n    ],\n    "tests/ci/test_components.py": [\n      "shared-core"\n    ],\n    "tests/ci/test_delivery_baseline.py": [\n      "harness"\n    ],\n    "tests/conftest.py": [\n      "shared-core"\n    ],\n    "tests/test_atlas_db.py": [\n      "atlas-data"\n    ],\n    "tests/test_auto_deploy_component_parity.py": [\n      "atlas-frontend",\n      "curriculum-display",\n      "shared-core"\n    ],\n    "tests/test_deploy_pages_workflow.py": [\n      "atlas-frontend"\n    ],\n    "tests/test_frontend_change_scope.py": [\n      "atlas-frontend",\n      "curriculum-display",\n      "shared-core"\n    ],\n    "tests/test_makefile_practice_admit.py": [\n      "practice-frontend"\n    ],\n    "tests/test_model_ready_receipt_audit.py": [\n      "open-model-data"\n    ],\n    "tests/test_open_model_ready_view_production.py": [\n      "open-model-data"\n    ],\n    "tests/test_open_model_view_exporter.py": [\n      "open-model-data"\n    ],\n    "tests/test_practice_deck_io.py": [\n      "practice-frontend"\n    ],\n    "tsconfig.json": [\n      "shared-core"\n    ],\n    "zizmor.yml": [\n      "shared-core"\n    ]\n  },\n  "path_prefixes": [\n    {\n      "path": ".agent/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".cursor/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".gemini/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".githooks/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".github/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": ".mcp/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "agents_extensions/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "archive/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "assets/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "audit/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "batch_state/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "benchmarks/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "bin/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "curriculum/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "dashboards/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "data/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "data/lexicon-dataset/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "data/lexicon/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "data/projects/open_model_data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "docs/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "docs/projects/open-model-data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "eval/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "migrations/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "openwiki/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "orchestration/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "packages/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "packages/activity-kit/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend",\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "packaging/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "plans/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "pocketbase/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "registry/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "registry/lexicon/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "registry/practice/",\n      "components": [\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "registry/projects/open_model_data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "review/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "schemas/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "scripts/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "scripts/agent_runtime/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/api/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/atlas/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/build/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "scripts/curriculum/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "scripts/etymology/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/fleet/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/fleet_comms/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/generate_mdx/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "scripts/lexicon/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/open_dataset/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/orchestration/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/practice/",\n      "components": [\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "scripts/practice_deck/",\n      "components": [\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "scripts/project_api/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/projects/open_model_data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "scripts/review/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "site/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend",\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "site/src/content/",\n      "components": [\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "site/src/lib/lexicon/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "site/src/pages/l2-uk-en/",\n      "components": [\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "site/src/pages/lexicon/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "status/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "tests/projects/open_model_data/",\n      "components": [\n        "open-model-data",\n        "shared-core"\n      ]\n    },\n    {\n      "path": "tests/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "tests/agent_runtime/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "tests/api/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "tests/build/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "tests/ci/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "tests/curriculum/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "tests/fixtures/atlas/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "tests/orchestration/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "wiki/",\n      "components": [\n        "shared-core"\n      ]\n    }\n  ],\n  "edges": [\n    {\n      "id": "curriculum-vocabulary",\n      "producer": "curriculum-generation",\n      "consumer": "atlas-data",\n      "kind": "artifact",\n      "anchors": [\n        "Makefile:62",\n        "scripts/lexicon/build_data_manifest.py:234"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "atlas-links-curriculum-generation",\n      "producer": "atlas-data",\n      "consumer": "curriculum-generation",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/generate_mdx/atlas_links.py:75",\n        "site/src/pages/[...slug].astro:14"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "atlas-links-curriculum-display",\n      "producer": "atlas-data",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/generate_mdx/atlas_links.py:75",\n        "site/src/pages/[...slug].astro:14"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "curriculum-evidence-import",\n      "producer": "curriculum-generation",\n      "consumer": "atlas-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/atlas/word_card_foundation.py:23"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "lexicon-heritage-import",\n      "producer": "atlas-data",\n      "consumer": "curriculum-generation",\n      "kind": "import",\n      "anchors": [\n        "scripts/curriculum/evidence/sources.py:32",\n        "scripts/build/linear_pipeline.py:11761"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-euphony-curriculum-generation",\n      "producer": "shared-core",\n      "consumer": "curriculum-generation",\n      "kind": "import",\n      "anchors": [\n        "scripts/curriculum/validate/mechanical.py:44",\n        "scripts/practice/euphony_stem_engine.py"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-euphony-practice-frontend",\n      "producer": "shared-core",\n      "consumer": "practice-frontend",\n      "kind": "import",\n      "anchors": [\n        "scripts/curriculum/validate/mechanical.py:44",\n        "scripts/practice/euphony_stem_engine.py"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-model-import-atlas-data",\n      "producer": "atlas-data",\n      "consumer": "open-model-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/projects/open_model_data/language_contact_detector.py:39"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-model-import-shared-core",\n      "producer": "shared-core",\n      "consumer": "open-model-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/projects/open_model_data/build_decolonization_cases.py:52"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-model-import-harness",\n      "producer": "harness",\n      "consumer": "open-model-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/projects/open_model_data/phase3_cycle007_evidence_compiler.py:44"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-dataset-publication",\n      "producer": "atlas-data",\n      "consumer": "atlas-data",\n      "kind": "artifact",\n      "anchors": [\n        "Makefile:65",\n        "Makefile:85",\n        "scripts/lexicon/export_open_dataset.py:184"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "dynamic-atlas-links",\n      "producer": "atlas-data",\n      "consumer": "curriculum-generation",\n      "kind": "dynamic",\n      "anchors": [\n        "scripts/audit/generate_search_index.py:88",\n        "scripts/audit/generate_search_index.py:96",\n        "scripts/audit/generate_search_index.py:102",\n        "scripts/generate_mdx/atlas_links.py:75"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-open-model-data",\n      "producer": "shared-core",\n      "consumer": "open-model-data",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-atlas-data",\n      "producer": "shared-core",\n      "consumer": "atlas-data",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-atlas-frontend",\n      "producer": "shared-core",\n      "consumer": "atlas-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-practice-frontend",\n      "producer": "shared-core",\n      "consumer": "practice-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-curriculum-generation",\n      "producer": "shared-core",\n      "consumer": "curriculum-generation",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-curriculum-display",\n      "producer": "shared-core",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-harness",\n      "producer": "shared-core",\n      "consumer": "harness",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-atlas-data-atlas-frontend",\n      "producer": "atlas-data",\n      "consumer": "atlas-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-practice-frontend-atlas-frontend",\n      "producer": "practice-frontend",\n      "consumer": "atlas-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-atlas-data-practice-frontend",\n      "producer": "atlas-data",\n      "consumer": "practice-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-practice-frontend-practice-frontend",\n      "producer": "practice-frontend",\n      "consumer": "practice-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-atlas-data-curriculum-display",\n      "producer": "atlas-data",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-practice-frontend-curriculum-display",\n      "producer": "practice-frontend",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "curriculum-display",\n      "producer": "curriculum-generation",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "site/src/pages/[...slug].astro:13",\n        "site/src/content.config.ts:12"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "frontend-props-atlas-frontend",\n      "producer": "atlas-frontend",\n      "consumer": "curriculum-generation",\n      "kind": "schema",\n      "anchors": [\n        "scripts/build/generate_lesson_schema.py:19",\n        "scripts/ci/checks.sh:69"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "frontend-props-practice-frontend",\n      "producer": "practice-frontend",\n      "consumer": "curriculum-generation",\n      "kind": "schema",\n      "anchors": [\n        "scripts/build/generate_lesson_schema.py:19",\n        "scripts/ci/checks.sh:69"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "frontend-props-curriculum-display",\n      "producer": "curriculum-display",\n      "consumer": "curriculum-generation",\n      "kind": "schema",\n      "anchors": [\n        "scripts/build/generate_lesson_schema.py:19",\n        "scripts/ci/checks.sh:69"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "production-receipt-audit",\n      "producer": "open-model-data",\n      "consumer": "open-model-data",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/projects/open_model_data/audit_model_ready_receipts.py:64",\n        "scripts/projects/open_model_data/audit_model_ready_receipts.py:132"\n      ],\n      "resolved": true\n    }\n  ],\n  "residual_commands": [\n    {\n      "id": "legacy-atlas-make",\n      "component": "atlas-data",\n      "status": "expected-fail",\n      "owner_slice": 3,\n      "argv": [\n        "make",\n        "atlas"\n      ],\n      "reason": "Legacy recipe invokes export_open_dataset without --write; DB-backed interface bypasses it."\n    },\n    {\n      "id": "non-arc-landing",\n      "component": "curriculum-generation",\n      "status": "expected-fail",\n      "owner_slice": 4,\n      "argv": [\n        "{python}",\n        "-m",\n        "scripts.build.build_landing_pages",\n        "--check"\n      ],\n      "reason": "Owner must regenerate stale non-arc landing output before freshness certification."\n    },\n    {\n      "id": "open-model-data-tier",\n      "component": "open-model-data",\n      "status": "artifact-dependent",\n      "owner_slice": 5,\n      "argv": [\n        "{python}",\n        "-m",\n        "pytest",\n        "tests/test_model_ready_receipt_audit.py",\n        "-m",\n        "needs_artifact"\n      ],\n      "reason": "Existing data-tier executes marked artifact tests; absence is never passing evidence."\n    },\n    {\n      "id": "complete-runtime-atlas-frontend",\n      "component": "atlas-frontend",\n      "status": "artifact-dependent",\n      "owner_slice": 3,\n      "argv": [\n        "node",\n        "site/scripts/verify-consumed-artifacts.mjs"\n      ],\n      "reason": "Prepared runtime members required; complete readiness remains slice 3, then learner certification slice 8."\n    },\n    {\n      "id": "complete-runtime-practice-frontend",\n      "component": "practice-frontend",\n      "status": "artifact-dependent",\n      "owner_slice": 3,\n      "argv": [\n        "node",\n        "site/scripts/verify-consumed-artifacts.mjs"\n      ],\n      "reason": "Prepared runtime members required; complete readiness remains slice 3, then learner certification slice 8."\n    },\n    {\n      "id": "complete-runtime-curriculum-display",\n      "component": "curriculum-display",\n      "status": "artifact-dependent",\n      "owner_slice": 3,\n      "argv": [\n        "node",\n        "site/scripts/verify-consumed-artifacts.mjs"\n      ],\n      "reason": "Prepared runtime members required; complete readiness remains slice 3, then learner certification slice 8."\n    },\n    {\n      "id": "open-model-prepared-build-inputs",\n      "component": "open-model-data",\n      "status": "artifact-dependent",\n      "owner_slice": 5,\n      "argv": [\n        "{python}",\n        "-m",\n        "scripts.projects.open_model_data.model_view_exporter",\n        "continued-pretraining",\n        "--source-records",\n        "{member:source-records}",\n        "--payloads",\n        "{member:payloads}",\n        "--origin",\n        "human_authored",\n        "--representation-view",\n        "faithful_literary",\n        "--output",\n        "{output_dir}/view.jsonl",\n        "--receipt-output",\n        "{output_dir}/view-receipt.json"\n      ],\n      "reason": "The production build requires admitted human-authored source records and payloads. Structural test fixtures are rejected; they do not certify a production view."\n    }\n  ],\n  "selector_contracts": {\n    "frontend_denominator": "scripts/ci/frontend_change_denominator.json",\n    "pages_eligibility": "scripts/deploy/auto_deploy_eligibility.py",\n    "frontend_components": [\n      "atlas-frontend",\n      "practice-frontend",\n      "curriculum-display"\n    ]\n  },\n  "input_wrapper_contract": {\n    "schema": "component-inputs.v1",\n    "component": "one of the eight node IDs",\n    "identities": "Exact inventory --component ID --identities identities object",\n    "families": "Every required input family, with name, existing schema identifier, version and members",\n    "member": "Each member has unique logical name, repository-relative path and sha256; existing schemas are not replaced",\n    "verification_scope": "Digest and freshness wrapper. Product semantic readiness remains owned by slices 3-5 and held-out certification by slice 8."\n  },\n  "shared_integration_tests": [\n    "tests/ci/test_components.py",\n    "tests/test_frontend_change_scope.py",\n    "tests/test_auto_deploy_component_parity.py"\n  ],\n  "vitest": {\n    "assignment": "Configured Vitest file directories intersected with exact/longest-prefix ownership; broad site/ ownership assigns shared files to all three frontend nodes.",\n    "built_output_files": [\n      "site/tests/unit/build-renders.test.ts",\n      "site/tests/unit/etymology-handler-built-output.test.ts"\n    ]\n  }\n}\n', 'scripts/curriculum/deploy_paths.v1.json': b'{\n  "schema": "learn-ukrainian.deploy-paths",\n  "schema_version": 1,\n  "content_path_prefixes": [\n    "curriculum/",\n    "data/",\n    "registry/",\n    "site/src/content/",\n    "site/src/data/",\n    "site/src/lib/lexicon/curated-heteronyms.ts"\n  ],\n  "site_code_path_exceptions": [\n    "site/src/data/lexicon-practice-deck.pointer.json",\n    "site/src/data/lexicon-manifest.pointer.json"\n  ],\n  "site_code_path_prefixes": [\n    "site/",\n    "packages/activity-kit/"\n  ]\n}\n'}


@pytest.fixture
def component_fixture_paths(configured_roots, monkeypatch):
    root = configured_roots.primary
    import shutil
    import subprocess
    shutil.copytree(configured_roots.product, root, dirs_exist_ok=True)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
    package = Path(c.__file__).resolve().parents[1]
    for relative in _COMPONENT_TOOL_PATHS:
        for prefix in ("scripts", "lu_machinery"):
            target = root / prefix / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((package / relative).read_bytes())
    for name, value in _COMPONENT_FIXTURE_BYTES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "component inputs"], check=True, capture_output=True, timeout=30)
    monkeypatch.setattr(c, "ROOT", root, raising=False)
