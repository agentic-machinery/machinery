"""Strict merge-check transport regressions for #9358; no network calls."""

import json
import subprocess
from pathlib import Path

import pytest

from lu_machinery.opsec.prepublish import PublishBlocked
from lu_machinery.publish.merge_guard import ensure_merge_ready


def merge_with_checks(stdout, returncode=0):
    def runner(args, **kwargs):
        if args[1] == "api":
            checks = json.loads(stdout)
            if "/check-runs?" in args[4]:
                body = {"total_count": len(checks), "check_runs": [
                    {"name": row["name"], "head_sha": "a" * 40,
                     "status": "in_progress" if row["bucket"] == "pending" else "completed",
                     "conclusion": "" if row["bucket"] == "pending" else
                         "failure" if row["bucket"] == "fail" else "success",
                     "app": {"id": 15368, "slug": "github-actions"}, "check_suite": {"id": i},
                     "details_url": f"https://github.com/unit/public/actions/runs/{i}/job/{i}"}
                    for i, row in enumerate(checks, 1)
                ]}
            else:
                i = int(args[4].rsplit("/", 1)[1])
                body = {"id": i, "head_sha": "a" * 40, "check_suite_id": i,
                        "workflow_id": i, "name": checks[i - 1].get("workflow")}
            return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
        if args[2] == "view":
            meta = {"number": 1, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}
            if "headRefOid,statusCheckRollup" in args:
                checks = json.loads(stdout)
                meta["statusCheckRollup"] = [
                    {"__typename": "CheckRun", "name": row["name"],
                     "detailsUrl": f"https://github.com/unit/public/actions/runs/{i}/job/{i}",
                     "status": "IN_PROGRESS" if row["bucket"] == "pending" else "COMPLETED",
                     "conclusion": "" if row["bucket"] == "pending" else
                         "FAILURE" if row["bucket"] == "fail" else "SUCCESS"}
                    for i, row in enumerate(checks, 1)
                ]
            return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")
        assert args[2] == "checks"
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    return ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})


@pytest.mark.parametrize("stdout", ["", " \n\t", "not JSON", "[", "null", "{}"])
def test_unestablished_check_state_is_refused(stdout):
    with pytest.raises(PublishBlocked, match="cannot establish check state"):
        merge_with_checks(stdout)


_NO_CHECKS = "OPSEC: merge refused: NO_CHECKS: the head has no blocking check."


@pytest.mark.parametrize("returncode,stdout,rollup", [
    (0, "[]", None),
    # gh's real answer for a head with no checks: exit 1, no output ("no checks reported").
    (1, "", []),
])
def test_head_without_a_blocking_check_is_refused(returncode, stdout, rollup):
    """Defect class: a head whose CI never ran (zero checks) passed as all-green."""
    def runner(args, **kwargs):
        if args[2] == "checks":
            return subprocess.CompletedProcess(args, returncode, stdout, "no checks reported" if returncode else "")
        meta = {"number": 1, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}
        if "headRefOid,statusCheckRollup" in args:
            assert rollup is not None, "an empty complete check list needs no rollup read"
            meta = {"headRefOid": "a" * 40, "statusCheckRollup": rollup}
        return subprocess.CompletedProcess(args, 0, json.dumps(meta), "")

    with pytest.raises(PublishBlocked) as refused:
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})
    assert str(refused.value) == _NO_CHECKS


@pytest.mark.parametrize("returncode", [2, 3])
def test_valid_json_with_transport_failure_is_refused(returncode):
    with pytest.raises(PublishBlocked, match="cannot establish check state"):
        merge_with_checks("[]", returncode)


@pytest.mark.parametrize("returncode,bucket", [(1, "fail"), (8, "pending")])
def test_known_nonzero_check_statuses_still_block(returncode, bucket):
    with pytest.raises(PublishBlocked):
        merge_with_checks(json.dumps([{"name": "CI Gate", "bucket": bucket}]), returncode)


def test_empty_json_check_list_with_exit_code_1_is_refused():
    with pytest.raises(PublishBlocked, match="cannot establish check state"):
        merge_with_checks("[]", returncode=1)


@pytest.mark.parametrize("identity", [
    {"workflow": "CI"}, {}, {"workflow": None}, {"workflow": ""}, {"workflow": "Nightly"}, {"workflowName": "CI"},
])
def test_failing_continue_on_error_job_blocks_merge(monkeypatch, tmp_path, identity):
    """Defect class: ci.yml's continue-on-error flag exempts a failing job's check from the merge guard (C-6339)."""
    from lu_machinery.ci import advisory_checks

    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes((Path(__file__).resolve().parents[2] / "tests/fixtures/ci_advisory_workflow.yml").read_bytes())
    monkeypatch.setattr(advisory_checks, "product_root", lambda *_args, **_kwargs: tmp_path)
    checks = [
        {"name": "CI Gate", "bucket": "pass"},
        {"name": "Component shadow (advisory)", "bucket": "fail", **identity},
    ]
    with pytest.raises(PublishBlocked, match=r"merge refused: FAILING checks\."):
        merge_with_checks(json.dumps(checks), returncode=1)


# --- Head-bound check judgment ------------------------------------------------------------

HEAD = "a" * 40
VENDOR_APP = {"id": 987654, "slug": "example-vendor-app"}
VENDOR_NAME = "Vendor check"
ROWS = [{"name": "CI Gate", "bucket": "pass", "workflow": "CI"}, {"name": VENDOR_NAME, "bucket": "pending"}]


BOUND_ROLLUP = [
    {"__typename": "CheckRun", "name": "CI Gate", "status": "COMPLETED", "conclusion": "SUCCESS", "workflowName": "CI"},
    {"__typename": "CheckRun", "name": VENDOR_NAME, "status": "IN_PROGRESS", "conclusion": "", "workflowName": ""},
]
# A repeated name without workflow identity: judging it needs the exact-head REST check runs.
DUPLICATE_DETAILS = "https://github.com/unit/public/actions/runs/12/job/22"
DUPLICATE_ROLLUP = [{"__typename": "CheckRun", "name": "Body guard", "status": "COMPLETED", "conclusion": "SUCCESS",
                     "detailsUrl": DUPLICATE_DETAILS}] * 2
CHECK_RUNS = f"repos/unit/public/commits/{HEAD}/check-runs?filter=all&per_page=100"


def bound_runner(calls, *, head=HEAD, error=None, bound=None, documents=None):
    def runner(args, **kwargs):
        calls.append(args)
        if args[2:5] == ["view", "1", "--json"] and args[5] == "headRefOid,statusCheckRollup":
            reply = {"headRefOid": head, "statusCheckRollup": BOUND_ROLLUP} if bound is None else bound
            return subprocess.CompletedProcess(args, 0, json.dumps(reply), "")
        if args[2] == "view":
            return subprocess.CompletedProcess(args, 0, json.dumps({"number": 1, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": head}), "")
        if args[2] == "checks":
            return subprocess.CompletedProcess(args, 8, json.dumps(ROWS), "")
        if error is not None:
            raise error
        if args[:4] == ["gh", "api", "--method", "GET"] and args[4] in (documents or {}):
            return subprocess.CompletedProcess(args, 0, json.dumps(documents[args[4]]), "")
        return subprocess.CompletedProcess(args, 1, "", "HTTP 404")

    return runner


def test_publisher_binds_canonical_check_urls_distinct_from_vendor_details():
    """A rollup detailsUrl that is the run's canonical html_url binds it, not only its vendor details_url."""
    calls = []
    old = {"name": "Body guard", "head_sha": HEAD, "status": "completed", "conclusion": "cancelled",
           "started_at": "2026-10-23T20:00:00Z", "check_suite": {"id": 1},
           "app": {"id": 15368, "slug": "github-actions"},
           "details_url": "https://github.com/unit/public/actions/runs/11/job/21"}
    current = {**old, "conclusion": "success", "started_at": "2026-10-23T21:00:00Z",
               "check_suite": {"id": 2}, "details_url": "https://github.com/unit/public/actions/runs/12/job/22"}
    rest = [current, old,
            {"name": "CI Gate", "head_sha": HEAD, "status": "completed", "conclusion": "success",
             "details_url": "https://github.com/unit/public/runs/23"},
            {"name": VENDOR_NAME, "head_sha": HEAD, "status": "completed", "conclusion": "success", "app": VENDOR_APP,
             "id": 24, "html_url": "https://github.com/unit/public/runs/24",
             "details_url": "https://example.invalid/gh/unit/public/trails/7"}]
    rollup = [{"__typename": "CheckRun", "name": row["name"], "status": row["status"].upper(),
               "conclusion": str(row.get("conclusion") or "").upper(),
               "detailsUrl": row.get("html_url", row["details_url"])}
              for row in rest]
    rollup.append({"__typename": "StatusContext", "context": "external-status", "state": "SUCCESS"})
    documents = {CHECK_RUNS: {"total_count": len(rest), "check_runs": rest},
                 "repos/unit/public/actions/runs/11": {"id": 11, "head_sha": HEAD, "check_suite_id": 1,
                                                       "workflow_id": 7, "name": "Body guard"},
                 "repos/unit/public/actions/runs/12": {"id": 12, "head_sha": HEAD, "check_suite_id": 2,
                                                       "workflow_id": 7, "name": "Body guard"}}
    runner = bound_runner(calls, bound={"headRefOid": HEAD, "statusCheckRollup": rollup}, documents=documents)
    assert ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={}, match_head=HEAD) == HEAD
    assert [call[1:6] for call in calls] == [
        ["pr", "view", "1", "--json", "number,isDraft,headRefOid,mergeStateStatus"],
        ["pr", "checks", "1", "--json", "name,bucket,state,workflow"],
        ["pr", "view", "1", "--json", "headRefOid,statusCheckRollup"],
        ["api", "--method", "GET", CHECK_RUNS],
        ["api", "--method", "GET", "repos/unit/public/actions/runs/12"],
        ["api", "--method", "GET", "repos/unit/public/actions/runs/11"]]


@pytest.mark.parametrize("bound", [
    {"headRefOid": "b" * 40, "statusCheckRollup": BOUND_ROLLUP},
    {"statusCheckRollup": BOUND_ROLLUP},
    {"headRefOid": HEAD, "statusCheckRollup": None},
    {"headRefOid": HEAD},
    [],
])
def test_publisher_refuses_rows_not_bound_to_the_metadata_head(bound):
    calls = []
    runner = bound_runner(calls, bound=bound)
    with pytest.raises(PublishBlocked, match="cannot establish check state"):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={}, match_head=HEAD)
    assert all(call[1] != "api" for call in calls)


def test_publisher_judges_the_bound_rollup_not_the_headless_rows():
    red = [{**BOUND_ROLLUP[0], "conclusion": "FAILURE"}, BOUND_ROLLUP[1]]
    runner = bound_runner([], bound={"headRefOid": HEAD, "statusCheckRollup": red})
    with pytest.raises(PublishBlocked, match=r"merge refused: FAILING checks\."):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})


OLD_CLIENT = (
    "unknown flag: --json\n\nUsage:  gh pr checks [<number> | <url> | <branch>] [flags]\n\n"
    "Flags:\n      --fail-fast         Exit watch mode on first check failure\n"
)


@pytest.mark.parametrize("rollup,refusal", [
    (None, "cannot establish check state"), ({}, "cannot establish check state"),
    ("", "cannot establish check state"), (False, "cannot establish check state"),
    (0, "cannot establish check state"), ([], "NO_CHECKS"),
])
def test_publisher_old_client_rollup_must_be_a_list(rollup, refusal):
    def runner(args, **kwargs):
        if args[2] == "checks":
            return subprocess.CompletedProcess(args, 1, "", OLD_CLIENT)
        if "headRefOid,statusCheckRollup" in args:
            return subprocess.CompletedProcess(args, 0, json.dumps({"headRefOid": HEAD, "statusCheckRollup": rollup}), "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": 1, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": HEAD}), "")

    with pytest.raises(PublishBlocked, match=refusal):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})


def test_changed_reviewed_head_refuses_whatever_the_checks_say():
    runner = bound_runner([])
    with pytest.raises(PublishBlocked, match="reviewed head changed"):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={}, match_head="b" * 40)


def test_rate_limit_during_a_check_run_read_is_not_masked():
    from lu_machinery.common.github_client import GitHubRateLimited

    limited = GitHubRateLimited(None, "fixture")
    runner = bound_runner([], error=limited, bound={"headRefOid": HEAD, "statusCheckRollup": DUPLICATE_ROLLUP})
    with pytest.raises(GitHubRateLimited):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})


def test_garbage_check_run_reply_is_refused():
    base = bound_runner([], bound={"headRefOid": HEAD, "statusCheckRollup": DUPLICATE_ROLLUP})

    def runner(args, **kwargs):
        if args[1] == "api":
            return subprocess.CompletedProcess(args, 0, "not JSON", "")
        return base(args, **kwargs)

    with pytest.raises(PublishBlocked, match="cannot establish check state"):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})


# --- pr-merge's own read transport over a multi-page head ------------------------------------
# The publisher sends the guard's reads through merge_readiness_runner, where github_client
# translates `gh pr view --json statusCheckRollup` and `gh api` into REST pages. A head with
# more check runs than one page must reach the verdict a plain gh runner reaches.

API = "https://api.github.com/"
ACTIONS = {"id": 15368, "slug": "github-actions"}


def _check_run(number, name="", **fields):
    return {"id": number, "name": name or f"Job {number}", "head_sha": HEAD, "status": "completed",
            "conclusion": "success", "html_url": f"https://github.com/unit/public/runs/{number}",
            "details_url": f"https://example.invalid/{number}", "started_at": "2026-10-28T16:00:00Z", **fields}


def _multi_page_head(runs, *, second_total=None):
    """REST documents for a head whose check runs span two pages of 100."""
    pull = {"number": 1, "state": "open", "draft": False, "mergeable_state": "clean", "head": {"sha": HEAD, "ref": "fix", "repo": {"full_name": "unit/public"}},
            "base": {"ref": "main", "repo": {"full_name": "unit/public"}}}
    documents = {"repos/unit/public/pulls/1": (pull, None),
                 f"repos/unit/public/commits/{HEAD}/status?per_page=100": ({"total_count": 0, "statuses": []}, None)}
    for endpoint in (f"repos/unit/public/commits/{HEAD}/check-runs?per_page=100",
                     f"repos/unit/public/commits/{HEAD}/check-runs?filter=all&per_page=100"):
        documents[endpoint] = ({"total_count": len(runs), "check_runs": runs[:100]}, f"{API}{endpoint}&page=2")
        documents[endpoint + "&page=2"] = ({"total_count": second_total or len(runs), "check_runs": runs[100:]}, None)
    return documents


def _publisher_transport(tmp_path, monkeypatch, documents, checks):
    """merge_readiness_runner over a real GitHubClient whose HTTP and native gh are fixtures."""
    from lu_machinery.common import github_client as gh
    from lu_machinery.publish.github import merge_readiness_runner

    real_client, requested = gh.GitHubClient, []

    def transport(method, endpoint, headers, body, timeout):
        path = endpoint.removeprefix(API)
        requested.append(path)
        if path not in documents:
            return gh.Response(404, {"X-RateLimit-Remaining": "5000"}, b'{"message": "Not Found"}')
        value, link = documents[path]
        reply = {"ETag": f'"{len(requested)}"', "X-RateLimit-Remaining": "5000", "X-RateLimit-Reset": "2000"}
        if link:
            reply["Link"] = f'<{link}>; rel="next"'
        return gh.Response(200, reply, json.dumps(value).encode())

    def native(args, **kwargs):
        assert args[1:3] == ["pr", "checks"], args
        return subprocess.CompletedProcess(args, 0, json.dumps(checks).encode(), b"")

    monkeypatch.setenv("LU_GITHUB_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gh, "GitHubClient", lambda **kwargs: real_client(
        **{**kwargs, "cache_dir": tmp_path, "transport": transport, "runner": native}))
    return merge_readiness_runner(), requested


def test_publisher_transport_reads_every_check_run_page_of_a_multi_page_head(tmp_path, monkeypatch):
    """136 check runs, two of one name superseded across workflow runs: READY, as with plain gh."""
    runs = [_check_run(n) for n in range(1, 135)]
    runs += [_check_run(135, "Body guard", conclusion="cancelled", app=ACTIONS, check_suite={"id": 1},
                        started_at="2026-10-28T15:00:00Z",
                        details_url="https://github.com/unit/public/actions/runs/11/job/135"),
             _check_run(136, "Body guard", app=ACTIONS, check_suite={"id": 2},
                        details_url="https://github.com/unit/public/actions/runs/12/job/136")]
    documents = _multi_page_head(runs)
    for run_id, suite in ((11, 1), (12, 2)):
        documents[f"repos/unit/public/actions/runs/{run_id}"] = (
            {"id": run_id, "head_sha": HEAD, "check_suite_id": suite, "workflow_id": 7, "name": "Body guard"}, None)
    checks = [{"name": row["name"], "bucket": "pass", "state": "SUCCESS", "workflow": ""} for row in runs]
    runner, requested = _publisher_transport(tmp_path, monkeypatch, documents, checks)
    environment = {"GH_HOST": "github.com", "GH_TOKEN": "fixture-only"}
    assert ensure_merge_ready("unit/public", 1, runner=runner, cwd=tmp_path, environment=environment,
                              match_head=HEAD) == HEAD
    assert f"repos/unit/public/commits/{HEAD}/check-runs?per_page=100&page=2" in requested
    assert f"repos/unit/public/commits/{HEAD}/check-runs?filter=all&per_page=100&page=2" in requested
    assert {"repos/unit/public/actions/runs/11", "repos/unit/public/actions/runs/12"} <= set(requested)


def test_publisher_transport_refusal_names_the_read_that_failed(tmp_path, monkeypatch):
    """A rollup whose total shifts between pages stays unverifiable, and the refusal says which read."""
    runs = [_check_run(n) for n in range(1, 137)]
    documents = _multi_page_head(runs, second_total=137)
    checks = [{"name": row["name"], "bucket": "pass", "state": "SUCCESS", "workflow": ""} for row in runs]
    runner, _ = _publisher_transport(tmp_path, monkeypatch, documents, checks)
    environment = {"GH_HOST": "github.com", "GH_TOKEN": "fixture-only"}
    with pytest.raises(PublishBlocked) as refused:
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=tmp_path, environment=environment)
    assert str(refused.value) == (
        "OPSEC: merge refused: cannot establish check state (pr view rollup: github_checks_incomplete).")


@pytest.mark.parametrize("second", [
    {"total_count": 101, "check_runs": [_check_run(101), _check_run(102)]},
    {"total_count": True, "check_runs": [_check_run(101)]},
    {"total_count": 101.0, "check_runs": [_check_run(101)]},
    {"total_count": 101},
])
def test_publisher_transport_refuses_an_inexact_multi_page_walk(tmp_path, monkeypatch, second):
    """More rows than the total, a non-integer total or a page without its collection is incomplete."""
    runs = [_check_run(n) for n in range(1, 101)]
    documents = _multi_page_head([*runs, _check_run(101)])
    for endpoint in [path for path in documents if path.endswith("&page=2")]:
        documents[endpoint] = (second, None)
    checks = [{"name": row["name"], "bucket": "pass", "state": "SUCCESS", "workflow": ""} for row in runs]
    runner, _ = _publisher_transport(tmp_path, monkeypatch, documents, checks)
    environment = {"GH_HOST": "github.com", "GH_TOKEN": "fixture-only"}
    with pytest.raises(PublishBlocked, match=r"pr view rollup: github_checks_incomplete"):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=tmp_path, environment=environment)


def test_publisher_transport_refuses_a_repeated_check_run_across_pages(tmp_path, monkeypatch):
    runs = [_check_run(n) for n in range(1, 137)]
    runs[120] = runs[5]
    checks = [{"name": row["name"], "bucket": "pass", "state": "SUCCESS", "workflow": ""} for row in runs]
    runner, _ = _publisher_transport(tmp_path, monkeypatch, _multi_page_head(runs), checks)
    environment = {"GH_HOST": "github.com", "GH_TOKEN": "fixture-only"}
    with pytest.raises(PublishBlocked, match=r"pr view rollup: github_checks_incomplete"):
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=tmp_path, environment=environment)


def test_refusal_names_a_failed_actions_run_read_with_gh_http_status():
    rest = [{"name": "Body guard", "head_sha": HEAD, "status": "completed", "conclusion": "success",
             "check_suite": {"id": 2}, "app": ACTIONS, "details_url": DUPLICATE_DETAILS}]
    documents = {CHECK_RUNS: {"total_count": 1, "check_runs": rest}}
    base = bound_runner([], documents=documents, bound={"headRefOid": HEAD, "statusCheckRollup": DUPLICATE_ROLLUP})

    def runner(args, **kwargs):
        if args[1] == "api" and args[4] == "repos/unit/public/actions/runs/12":
            return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
        return base(args, **kwargs)

    with pytest.raises(PublishBlocked) as refused:
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})
    assert str(refused.value) == (
        "OPSEC: merge refused: cannot establish check state (actions run: exit 1 HTTP 404).")


def test_refusal_names_a_read_that_raised():
    base = bound_runner([])

    def runner(args, **kwargs):
        if "headRefOid,statusCheckRollup" in args:
            raise subprocess.TimeoutExpired(args, 15)
        return base(args, **kwargs)

    with pytest.raises(PublishBlocked) as refused:
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})
    assert str(refused.value) == "OPSEC: merge readiness unverifiable (pr view rollup: TimeoutExpired)."


@pytest.mark.parametrize("returncode,stdout,stderr,named", [
    (1, "", "gh: Not Found (HTTP 404)", "pr checks: exit 1 HTTP 404"),
    (0, "{}", "", "pr checks: invalid shape"),
    (0, "not JSON", "", "pr checks: malformed JSON"),
    (1, "[]", "", "pr checks: empty failing list"),
])
def test_refusal_names_how_the_checks_read_failed(returncode, stdout, stderr, named):
    def runner(args, **kwargs):
        if args[2] == "checks":
            return subprocess.CompletedProcess(args, returncode, stdout, stderr)
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": 1, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": HEAD}), "")

    with pytest.raises(PublishBlocked) as refused:
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})
    assert str(refused.value) == f"OPSEC: merge refused: cannot establish check state ({named})."


@pytest.mark.parametrize("bound,named", [
    ({"headRefOid": HEAD, "statusCheckRollup": None}, "pr view rollup: invalid shape"),
    (None, "no read failed; check rows unverifiable"),
])
def test_refusal_names_an_invalid_shape_or_says_no_read_failed(bound, named):
    rollup = {"headRefOid": "b" * 40, "statusCheckRollup": BOUND_ROLLUP} if bound is None else bound
    runner = bound_runner([], bound=rollup)
    with pytest.raises(PublishBlocked) as refused:
        ensure_merge_ready("unit/public", 1, runner=runner, cwd=".", environment={})
    assert str(refused.value) == f"OPSEC: merge refused: cannot establish check state ({named})."
