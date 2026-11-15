"""Ledger counts queue runs and acts only at the D2 escalation boundary."""



from datetime import date, timedelta
from lu_machinery.ci import flake_ledger
from lu_machinery.ci.flake_ledger import make_ledger, update_issues
import copy
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from lu_machinery.common import flake_quarantine
from lu_machinery.common.flake_quarantine import load_registry
from lu_machinery.common.flake_quarantine import rerun_node_ids
from tests import conftest as root_conftest
from tests.helpers.product_tree import configured_roots as configured_roots

def test_ledger_counts_distinct_queue_runs_and_expiry(tmp_path, monkeypatch):
    today = date(2026, 9, 28)
    node = "tests/test_sample.py::test_flaky"
    xml = tmp_path / "rerun.xml"
    xml.write_text(
        '<testsuite><testcase classname="tests.test_sample" name="test_flaky" file="tests/test_sample.py">'
        '<properties><property name="flake.reruns" value="1"/></properties></testcase></testsuite>',
        encoding="utf-8",
    )
    nightly = tmp_path / "nightly.xml"
    nightly.write_text(
        '<testsuite><testcase classname="tests.test_sample" name="test_flaky" file="tests/test_sample.py">'
        '<failure message="still flaky"/></testcase></testsuite>', encoding="utf-8",
    )
    entries = [{"node_id": node, "fix_issue": 123, "owner": "infra", "expires_on": today}]
    ledger = make_ledger(entries, {1: [xml], 2: [xml]}, [nightly], today=today, missing_runs=[3])
    row = ledger["entries"][0]
    assert ledger["queue_run_count"] == 3
    assert row["recovered_queue_run_ids"] == [1, 2]
    assert row["queue_rerun_percent"] == 66.67
    assert row["nightly_failures"] == ["still flaky"]
    monkeypatch.setattr(flake_ledger, "_gh", lambda *args: '{"state":"OPEN","labels":[],"comments":[]}')
    assert update_issues(ledger, dry_run=True) == ["comment #123: expiry", "escalate #123: 66.67%"]
    entries[0]["expires_on"] = today + timedelta(days=1)
    assert not make_ledger(entries, {}, [nightly], today=today)["entries"][0]["expired"]


def test_queue_runs_that_reused_a_green_run_are_not_missing_evidence(tmp_path, monkeypatch):
    """ci.yml's merge-queue reuse skips every shard: no JUnit, and none expected."""
    import json
    import subprocess

    jobs = {
        101: [{"name": "Reuse check", "conclusion": "success"}, {"name": "pytest (1)", "conclusion": "skipped"}],
        102: [{"name": "Reuse check", "conclusion": "success"}, {"name": "pytest (1)", "conclusion": "failure"}],
        103: [{"name": "Reuse check", "conclusion": "failure"}, {"name": "pytest (1)", "conclusion": "skipped"}],
    }

    def fake_gh(*args):
        if args[:2] == ("run", "list"):
            return json.dumps([{"databaseId": run, "createdAt": "2026-09-29T00:00:00Z"} for run in jobs])
        if args[:2] == ("run", "download"):
            raise subprocess.CalledProcessError(1, ["gh", *args])
        if args[:2] == ("run", "view"):
            return json.dumps({"jobs": jobs[int(args[2])]})
        raise AssertionError(args)

    monkeypatch.setattr(flake_ledger, "_gh", fake_gh)
    artifacts, missing = flake_ledger.fetch_queue_junit(tmp_path, since=date(2026, 9, 28))
    assert artifacts == {}
    assert missing == [102, 103]


def _entry(today: date) -> dict:
    return {
        "node_id": "tests/test_sample.py::test_flaky",
        "fix_issue": 123,
        "owner": "infra",
        "admitted_on": today,
        "expires_on": today + timedelta(days=20),
        "observed_run_ids": [101, 102],
        "renewals": 0,
        "equivalent_coverage": {"tests/test_sample.py::test_stable": "Checks the same result without the timing dependency."},
        "admission_approval": 999,
    }


@pytest.mark.usefixtures('configured_roots')
def test_registry_lint_rejects_missing_bad_expiry_second_renewal_and_duplicate_runs(tmp_path):
    today = date(2026, 9, 28)
    entry = _entry(today)
    path = tmp_path / "registry.yaml"
    path.write_text("entries:\n  - node_id: tests/test_sample.py::test_flaky\n", encoding="utf-8")
    with pytest.raises(ValueError, match="required fields"):
        load_registry(path)
    for change, message in (
        ({"expires_on": today + timedelta(days=31)}, "30 days"),
        ({"renewals": 2}, "one renewal"),
        ({"observed_run_ids": [101, 101]}, "distinct"),
        ({"observed_run_ids": [101]}, "at least two"),
    ):
        bad = entry | change
        path.write_text(yaml.safe_dump({"entries": [bad]}), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_registry(path)
    path.write_text(yaml.safe_dump({"entries": [entry]}), encoding="utf-8")
    assert load_registry(path) == [entry]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("change,message", [
    ({"node_id": "tests/test_sample.py::test_flaky*"}, "invalid pytest node_id"),
    ({"node_id": "tests/../test_sample.py::test_flaky"}, "invalid pytest node_id"),
    ({"node_id": "tests/test_sample.py::test_flaky\n"}, "invalid pytest node_id"),
    ({"equivalent_coverage": {}}, "equivalent_coverage"),
    ({"equivalent_coverage": {"tests/test_sample.py::test_flaky": "same test"}}, "equivalent_coverage"),
    ({"equivalent_coverage": {"tests/test_sample.py::test_stable": " "}}, "equivalent_coverage"),
    ({"equivalent_coverage": {"tests/test_sample.py": "module"}}, "equivalent_coverage"),
    ({"admission_approval": True}, "admission_approval"),
    ({"owner": " "}, "owner"),
    ({"fix_issue": True}, "positive issue"),
    ({"observed_run_ids": [101, False]}, "positive observed run"),
    ({"renewals": 1, "renewed_on": date(2026, 10, 29)}, "first 30 days"),
    ({"renewals": 1, "renewed_on": date(2026, 9, 27)}, "first 30 days"),
    ({"expires_on": date(2026, 9, 27)}, "30 days"),
])
def test_registry_rejects_invalid_admission_fields(tmp_path, change, message):
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"entries": [_entry(date(2026, 9, 28)) | change]}))
    with pytest.raises(ValueError, match=message):
        load_registry(path)


@pytest.mark.usefixtures('configured_roots')
def test_single_renewal_and_exact_parameterized_class_node(tmp_path):
    entry = _entry(date(2026, 9, 28)) | {
        "node_id": "tests/test_sample.py::TestSample::test_flaky[a*b]",
        "renewals": 1,
        "renewed_on": date(2026, 10, 28),
        "expires_on": date(2026, 11, 27),
    }
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"entries": [entry]}))
    assert load_registry(path) == [entry]
    assert rerun_node_ids([entry], today=date(2026, 12, 4), event_name="pull_request") == {entry["node_id"]}
    assert rerun_node_ids([entry], today=date(2026, 12, 5), event_name="pull_request") == set()
    entry["expires_on"] += timedelta(days=1)
    path.write_text(yaml.safe_dump({"entries": [entry]}))
    with pytest.raises(ValueError, match="30 days"):
        load_registry(path)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("renewals", [0, 1])
def test_reruns_start_on_admission_or_renewal_date(tmp_path, renewals):
    start = date(2027, 8, 1)
    entry = _entry(start)
    if renewals:
        entry.update(admitted_on=start - timedelta(days=30), renewals=1, renewed_on=start)
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump({"entries": [entry]}))
    entries = load_registry(path)
    assert rerun_node_ids(entries, today=date(2026, 10, 20), event_name="pull_request") == set()
    assert rerun_node_ids(entries, today=start - timedelta(days=1), event_name="pull_request") == set()
    assert rerun_node_ids(entries, today=start, event_name="pull_request") == {entry["node_id"]}
    expiry = entry["expires_on"]
    assert rerun_node_ids(entries, today=expiry, event_name="pull_request") == {entry["node_id"]}
    assert rerun_node_ids(entries, today=expiry + timedelta(days=7), event_name="pull_request") == {entry["node_id"]}
    assert rerun_node_ids(entries, today=expiry + timedelta(days=8), event_name="pull_request") == set()


@pytest.fixture
def admission(monkeypatch):
    entry = _entry(date(2026, 9, 28))
    prefix = "repos/example/project"
    issue_url = f"https://api.github.com/{prefix}/issues/123"
    sha = "a" * 40
    responses = {
        f"{prefix}/issues/123": {"state": "open", "url": issue_url},
        f"{prefix}/issues/comments/999": {
            "issue_url": issue_url, "author_association": "OWNER",
            "body": f"<!-- flake-quarantine-admission:{flake_quarantine.admission_digest(entry)}:APPROVE -->",
        },
        f"{prefix}/compare/{sha}...main": {"merge_base_commit": {"sha": sha}},
    }
    for run_id in (101, 102):
        responses[f"{prefix}/actions/runs/{run_id}"] = {
            "head_branch": "main", "head_sha": sha, "head_repository": {"full_name": "example/project"},
            "event": "schedule", "path": ".github/workflows/ci.yml", "status": "completed",
        }
    xml = (
        '<testsuite><testcase classname="tests.test_sample" name="test_flaky">'
        '<failure message="assert 0"/></testcase>'
        '<testcase classname="tests.test_sample" name="test_stable"/></testsuite>'
    )
    artifacts = {101: xml, 102: xml}
    duplicates = {}
    calls = []

    def gh(*args):
        calls.append(args)
        if args[0] == "api":
            return json.dumps(responses[args[1]])
        if args[:2] == ("repo", "view"):
            return '{"nameWithOwner":"example/project"}'
        assert args[:2] == ("run", "download")
        assert args[3:7] == ("--repo", "example/project", "--pattern", "pytest-junit-shard-*")
        Path(args[-1], "junit.xml").write_text(artifacts[int(args[2])])
        if int(args[2]) in duplicates:
            Path(args[-1], "junit-duplicate.xml").write_text(duplicates[int(args[2])])
        return ""

    monkeypatch.setattr(flake_quarantine, "_gh", gh)
    return SimpleNamespace(entry=entry, responses=responses, artifacts=artifacts, duplicates=duplicates,
                           calls=calls, prefix=prefix)


@pytest.mark.usefixtures('configured_roots')
def test_admission_verifies_two_main_failures_coverage_issue_and_bound_approval(admission):
    assert flake_quarantine.check_admissions([admission.entry], [], repository="example/project") == 1
    assert [call[2] for call in admission.calls if call[:2] == ("run", "download")] == ["101", "102"]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("renewals", [0, 1])
def test_admission_refuses_future_start_before_fetching_evidence(admission, monkeypatch, renewals):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 20)

    monkeypatch.setattr(flake_quarantine, "date", Today)
    start = date(2026, 10, 21)
    admission.entry.update(admitted_on=start, expires_on=start + timedelta(days=20))
    if renewals:
        admission.entry.update(admitted_on=start - timedelta(days=30), renewals=1, renewed_on=start)
    admission.responses[f"{admission.prefix}/issues/comments/999"]["body"] = (
        f"<!-- flake-quarantine-admission:{flake_quarantine.admission_digest(admission.entry)}:APPROVE -->"
    )
    with pytest.raises(ValueError, match=r"start date.*future"):
        flake_quarantine.check_admissions([admission.entry], [], repository="example/project")
    assert admission.calls == []
    monkeypatch.setattr(Today, "today", classmethod(lambda cls: cls(2026, 10, 21)))
    assert flake_quarantine.check_admissions([admission.entry], [], repository="example/project") == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("resource,change,message", [
    ("issues/123", {"state": "closed"}, "open repair issue"),
    ("issues/123", {"pull_request": {}}, "not a PR"),
    ("issues/comments/999", {"issue_url": "another issue"}, "designated admission approval"),
    ("issues/comments/999", {"author_association": "NONE"}, "designated admission approval"),
    ("issues/comments/999", {"body": "APPROVE without the digest"}, "designated admission approval"),
    ("actions/runs/102", {"head_branch": "feature"}, "unmodified main"),
    ("actions/runs/102", {"head_repository": {"full_name": "someone/fork"}}, "unmodified main"),
    ("actions/runs/102", {"event": "merge_group"}, "seven days"),
    ("actions/runs/102", {"event": "pull_request_target"}, "unmodified main"),
    ("actions/runs/102", {"path": ".github/workflows/other.yml"}, "unmodified main"),
    ("actions/runs/102", {"status": "in_progress"}, "unmodified main"),
    ("actions/runs/102", {"head_sha": "not-a-sha"}, "unmodified main"),
    (f"compare/{'a' * 40}...main", {"merge_base_commit": {"sha": "b" * 40}}, "ancestor of main"),
])
def test_admission_refuses_nonbaseline_or_unapproved_evidence(admission, resource, change, message):
    admission.responses[f"{admission.prefix}/{resource}"].update(change)
    with pytest.raises(ValueError, match=message):
        flake_quarantine.check_admissions([admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("replacement,message", [
    (("test_flaky", "test_flaky[other]"), "exact-node baseline failure"),
    (('<failure message="assert 0"/>', ""), "exact-node baseline failure"),
    (('<failure message="assert 0"/>', '<skipped message="disabled"/>'), "exact-node baseline failure"),
    (("test_stable", "test_different"), "equivalent coverage"),
    (('name="test_stable"/>', 'name="test_stable"><failure/></testcase>'), "equivalent coverage"),
    (('name="test_stable"/>', 'name="test_stable"><skipped/></testcase>'), "equivalent coverage"),
    (('name="test_stable"/>', 'name="test_stable"><properties><property name="flake.reruns" value="1"/>'
      '</properties></testcase>'), "equivalent coverage"),
])
def test_admission_requires_exact_failed_node_and_passing_equivalent_coverage(admission, replacement, message):
    admission.artifacts[102] = admission.artifacts[102].replace(*replacement)
    with pytest.raises(ValueError, match=message):
        flake_quarantine.check_admissions([admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
def test_first_failure_recovered_in_baseline_is_still_observed_evidence(admission):
    admission.artifacts[102] = admission.artifacts[102].replace(
        '<failure message="assert 0"/>',
        '<properties><property name="flake.reruns" value="1"/></properties>',
    )
    assert flake_quarantine.check_admissions([admission.entry], [], repository="example/project") == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("node,message", [
    ("test_flaky", "exact-node baseline failure"),
    ("test_stable", "equivalent coverage"),
])
def test_admission_refuses_duplicate_results_for_same_node(admission, node, message):
    duplicate = f'<testcase classname="tests.test_sample" name="{node}">'
    if node == "test_flaky":
        duplicate += '<failure message="assert 0"/>'
    duplicate += '</testcase>'
    admission.duplicates[102] = f"<testsuite>{duplicate}</testsuite>"
    with pytest.raises(ValueError, match=message):
        flake_quarantine.check_admissions([admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
def test_unchanged_entries_and_removals_need_no_expiring_artifacts(admission):
    assert flake_quarantine.check_admissions([admission.entry], [copy.deepcopy(admission.entry)],
                                           repository="example/project") == 0
    assert flake_quarantine.check_admissions([], [admission.entry], repository="example/project") == 0
    assert admission.calls == []


@pytest.mark.usefixtures('configured_roots')
def test_renewal_requires_new_bound_approval(admission):
    old = copy.deepcopy(admission.entry)
    admission.entry.update(renewals=1, renewed_on=date(2026, 10, 18), expires_on=date(2026, 11, 17))
    with pytest.raises(ValueError, match="bound to this entry"):
        flake_quarantine.check_admissions([admission.entry], [old], repository="example/project")
    admission.responses[f"{admission.prefix}/issues/comments/999"]["body"] = (
        f"<!-- flake-quarantine-admission:{flake_quarantine.admission_digest(admission.entry)}:APPROVE -->"
    )
    assert flake_quarantine.check_admissions([admission.entry], [old], repository="example/project") == 1


@pytest.mark.usefixtures('configured_roots')
def test_github_unavailable_never_admits(admission, monkeypatch):
    def unavailable(*args):
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="unavailable")

    monkeypatch.setattr(flake_quarantine, "_gh", unavailable)
    with pytest.raises(subprocess.CalledProcessError, match="non-zero exit status"):
        flake_quarantine.check_admissions([admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
def test_selection_listed_unlisted_schedule_and_expired(monkeypatch):
    today = date.today()
    entry = _entry(today)
    assert rerun_node_ids([entry], today=today, event_name="merge_group") == {entry["node_id"]}
    assert rerun_node_ids([entry], today=today, event_name="schedule") == set()
    entry["expires_on"] = today - timedelta(days=7)
    assert rerun_node_ids([entry], today=today, event_name="pull_request") == {entry["node_id"]}
    entry["expires_on"] = today - timedelta(days=8)
    assert rerun_node_ids([entry], today=today, event_name="pull_request") == set()

    class Item:
        def __init__(self, nodeid):
            self.nodeid = nodeid
            self.markers = []

        def add_marker(self, marker):
            self.markers.append(marker)

        def get_closest_marker(self, name):
            return None

    entry["expires_on"] = today
    listed = Item(entry["node_id"])
    unlisted = Item("tests/test_sample.py::test_other")
    monkeypatch.setattr(root_conftest, "load_registry", lambda _path=None: [entry])
    monkeypatch.setattr(root_conftest, "_sparse_missing_trees", lambda: set())
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    root_conftest.pytest_collection_modifyitems(None, [listed, unlisted])
    assert len(listed.markers) == 1 and listed.markers[0].kwargs["reruns"] == 1
    assert unlisted.markers == []
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    listed.markers.clear()
    root_conftest.pytest_collection_modifyitems(None, [listed])
    assert listed.markers == []
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("LU_FLAKE_DISABLE_RERUN", "1")
    root_conftest.pytest_collection_modifyitems(None, [listed])
    assert listed.markers == []


@pytest.fixture
def merge_admission(admission, tmp_path, monkeypatch):
    """Use real commit snapshots and the shared scanner, without network fetches."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    for path, source in {
        "tests/test_sample.py": "from scripts.unit import value\ndef test_flaky(): pass\ndef test_stable(): pass\n",
        "scripts/unit.py": "from scripts.dep import value\n",
        "scripts/dep.py": "value = 1\n",
        "unrelated.txt": "base\n",
    }.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", base)
    (repo / "unrelated.txt").write_text("head\n")
    git("add", ".")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    monkeypatch.setattr(flake_quarantine, "product_root", lambda: repo)
    original_run = subprocess.run

    def run(argv, **kwargs):
        if argv[:3] == ["git", "fetch", "--no-tags"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(flake_quarantine.subprocess, "run", run)
    for run_id, number in ((101, 41), (102, 42)):
        admission.responses[f"{admission.prefix}/actions/runs/{run_id}"].update(
            event="merge_group", head_sha=head,
            head_branch=f"gh-readonly-queue/main/pr-{number}-{base}",
            created_at=datetime.now(UTC).isoformat(),
        )
    return SimpleNamespace(admission=admission, repo=repo, base=base, git=git)


@pytest.mark.usefixtures('configured_roots')
def test_admission_accepts_merge_groups_for_two_unrelated_prs(merge_admission):
    a = merge_admission.admission
    assert flake_quarantine.check_admissions([a.entry], [], repository="example/project") == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("changed", ["tests/test_sample.py", "scripts/unit.py", "scripts/dep.py"])
def test_merge_group_refuses_changed_test_or_transitive_import(merge_admission, changed):
    m = merge_admission
    with (m.repo / changed).open("a") as source:
        source.write("\n# changed by this PR\n")
    m.git("add", ".")
    m.git("commit", "-qm", "touch dependency")
    m.admission.responses[f"{m.admission.prefix}/actions/runs/102"]["head_sha"] = m.git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="touches the test or an imported module"):
        flake_quarantine.check_admissions([m.admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
def test_merge_group_refuses_unresolved_test_imports(merge_admission):
    m = merge_admission
    # An unresolved loader could reach newly added Python sources.
    (m.repo / "scripts/unit.py").write_text("import scripts.missing\n")
    m.git("add", ".")
    m.git("commit", "-qm", "unresolved")
    base = m.git("rev-parse", "HEAD")
    m.git("update-ref", "refs/remotes/origin/main", base)
    (m.repo / "scripts/other.py").write_text("value = 1\n")
    m.git("add", ".")
    m.git("commit", "-qm", "unrelated")
    for run_id, number in ((101, 41), (102, 42)):
        m.admission.responses[f"{m.admission.prefix}/actions/runs/{run_id}"].update(
            head_sha=m.git("rev-parse", "HEAD"), head_branch=f"gh-readonly-queue/main/pr-{number}-{base}",
        )
    with pytest.raises(ValueError, match="import closure is unresolved"):
        flake_quarantine.check_admissions([m.admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("change,message", [
    ({"head_branch": "gh-readonly-queue/main/pr-41-BASE"}, "two distinct PRs"),
    ({"head_branch": "feature"}, "identifiable PR"),
    ({"created_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()}, "seven days"),
    ({"created_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}, "seven days"),
    ({"created_at": "2026-10-20"}, "seven days"),
])
def test_merge_group_requires_distinct_prs_and_recent_timestamp(merge_admission, change, message):
    m = merge_admission
    change = {key: value.replace("BASE", m.base) for key, value in change.items()}
    m.admission.responses[f"{m.admission.prefix}/actions/runs/102"].update(change)
    with pytest.raises(ValueError, match=message):
        flake_quarantine.check_admissions([m.admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
def test_one_main_and_one_merge_group_do_not_combine_into_admission(merge_admission):
    m = merge_admission
    m.admission.responses[f"{m.admission.prefix}/actions/runs/101"].update(event="schedule", head_branch="main")
    head = m.admission.responses[f"{m.admission.prefix}/actions/runs/101"]["head_sha"]
    m.admission.responses[f"{m.admission.prefix}/compare/{head}...main"] = {"merge_base_commit": {"sha": head}}
    with pytest.raises(ValueError, match="two main runs or merge-group failures"):
        flake_quarantine.check_admissions([m.admission.entry], [], repository="example/project")


@pytest.mark.usefixtures('configured_roots')
def test_merge_group_refuses_dependency_changed_by_queue_ahead_pr(merge_admission):
    m = merge_admission
    (m.repo / "scripts/dep.py").write_text("value = 2\n")
    m.git("add", ".")
    m.git("commit", "-qm", "ahead PR changes dependency")
    ahead = m.git("rev-parse", "HEAD")
    (m.repo / "unrelated.txt").write_text("behind PR\n")
    m.git("add", ".")
    m.git("commit", "-qm", "behind PR")
    run = m.admission.responses[f"{m.admission.prefix}/actions/runs/101"]
    run.update(head_sha=m.git("rev-parse", "HEAD"), head_branch=f"gh-readonly-queue/main/pr-41-{ahead}")
    with pytest.raises(ValueError, match="touches the test"):
        flake_quarantine._merge_group_evidence(run, m.admission.entry["node_id"])


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("changed", ["conftest.py", "tests/conftest.py", "scripts/fixture.py"])
def test_merge_group_includes_conftest_chain_and_imports(merge_admission, changed):
    m = merge_admission
    for path, source in {"conftest.py": "", "tests/conftest.py": "import scripts.fixture\n",
                         "scripts/fixture.py": "value = 1\n"}.items():
        (m.repo / path).write_text(source)
    m.git("add", ".")
    m.git("commit", "-qm", "fixtures")
    m.git("update-ref", "refs/remotes/origin/main", m.git("rev-parse", "HEAD"))
    (m.repo / changed).write_text("# fixture changed\n")
    m.git("add", ".")
    m.git("commit", "-qm", "fixture change")
    run = m.admission.responses[f"{m.admission.prefix}/actions/runs/101"]
    run["head_sha"] = m.git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="touches the test"):
        flake_quarantine._merge_group_evidence(run, m.admission.entry["node_id"])


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("operation", ["fetch", "merge-base"])
def test_merge_group_git_failure_is_an_admission_refusal(merge_admission, monkeypatch, operation):
    m = merge_admission
    original = subprocess.run
    def fail(argv, **kwargs):
        if argv[:2] == ["git", operation]:
            raise subprocess.CalledProcessError(128, argv, stderr="fatal: unavailable")
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match=f"admission refused: git {operation} failed"):
        flake_quarantine._merge_group_evidence(
            m.admission.responses[f"{m.admission.prefix}/actions/runs/101"], m.admission.entry["node_id"])


@pytest.mark.usefixtures('configured_roots')
def test_main_prints_admission_refusal(admission, monkeypatch, capsys):
    monkeypatch.setattr(flake_quarantine, "load_registry", lambda _path=None: [admission.entry])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="entries: []"))
    def refuse(*args, **kwargs):
        raise ValueError("merge-group admission refused: git fetch failed")
    monkeypatch.setattr(flake_quarantine, "check_admissions", refuse)
    assert flake_quarantine.main(["--base-ref", "origin/main"]) == 1
    assert "Quarantine admission refused: merge-group admission refused: git fetch failed" in capsys.readouterr().out


@pytest.mark.usefixtures('configured_roots')
def test_unresolved_loader_refuses_unrelated_python_changes(merge_admission):
    m = merge_admission
    (m.repo / "scripts/unit.py").write_text("import importlib\nimportlib.import_module(module_name)\n")
    m.git("add", ".")
    m.git("commit", "-qm", "dynamic loader")
    m.git("update-ref", "refs/remotes/origin/main", m.git("rev-parse", "HEAD"))
    (m.repo / "scripts/other.py").write_text("value = 1\n")
    m.git("add", ".")
    m.git("commit", "-qm", "possibly reachable module")
    run = m.admission.responses[f"{m.admission.prefix}/actions/runs/101"]
    run["head_sha"] = m.git("rev-parse", "HEAD")
    with pytest.raises(ValueError, match="unresolved for changed Python"):
        flake_quarantine._merge_group_evidence(run, m.admission.entry["node_id"])
