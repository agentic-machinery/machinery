"""Keep slow-nightly and security dispatch exact and reject unregistered inputs."""

import json
import subprocess
from pathlib import Path

import pytest

from lu_machinery.common import github_client
from lu_machinery.opsec import gh_snapshot
from lu_machinery.opsec import prepublish as gate
from lu_machinery.publish import github as pub
from tests.common.github_cache_isolation import isolate_github_client_cache
from tests.helpers.product_tree import configured_roots  # noqa: F401
from tests.opsec_fixtures import CATALOG, synthetic_rules


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    yield from isolate_github_client_cache(tmp_path, monkeypatch)


@pytest.fixture
def dispatch_calls(tmp_path, synthetic_opsec, monkeypatch):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    calls = []

    def sender(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return calls, sender


@pytest.mark.parametrize("workflow", ["pytest-slow-nightly.yml", "security.yml"])
def test_missing_registration_or_wrong_dispatch_request(dispatch_calls, workflow):
    """An omitted workflow or a rewritten destination/ref breaks canonical dispatch."""
    calls, sender = dispatch_calls
    assert pub.main([
        "workflow-run", "--repo", "unit/public",
        "--workflow", workflow, "--ref", "reviewed/workflow",
    ], runner=sender) == 0
    assert calls == [
        [
            "gh", "workflow", "run", workflow,
            "--repo", "unit/public", "--ref=reviewed/workflow",
        ],
    ]


def test_repo_fork_scans_source_and_sends_only_the_authenticated_user_request(
    monkeypatch, synthetic_opsec, tmp_path, capsys,
):
    """A raw fork or a blocked selector must never reach GitHub."""
    (synthetic_opsec / "rules.json").write_text(json.dumps(synthetic_rules(pattern="blocked-repo")))
    monkeypatch.setenv("GH_HOST", "github.com")
    monkeypatch.setenv("GH_REPO", "other/inherited")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh-config"))
    calls = []

    def send(client, method, endpoint, headers, body, timeout):
        calls.append((client.host, method, endpoint, json.loads(body)))
        return github_client.Response(202, {}, b'{"html_url":"https://github.com/fleet/source"}')

    monkeypatch.setattr(github_client.GitHubClient, "_send", send)
    with pytest.raises(gate.PublishBlocked, match="raw write"):
        gh_snapshot.admit(["repo", "fork", "unit/source"], cwd=tmp_path, environment={})
    assert pub.main(["repo-fork", "--repo", "unit/blocked-repo"]) == 2
    assert "field=repo" in capsys.readouterr().err
    assert calls == []
    assert pub.main(["repo-fork", "--repo", "Unit/Source"]) == 0
    assert calls == [("github.com", "POST", "repos/unit/source/forks", {"default_branch_only": True})]
    assert capsys.readouterr().out == "https://github.com/fleet/source\n"


@pytest.mark.parametrize("workflow", ["pytest-slow-nightly.yaml", "security.yaml"])
def test_unknown_workflow_cannot_reach_dispatch(dispatch_calls, capsys, workflow):
    """A near-match filename must not bypass the exact workflow allowlist."""
    calls, sender = dispatch_calls
    with pytest.raises(SystemExit) as error:
        pub.main([
            "workflow-run", "--repo", "unit/public",
            "--workflow", workflow, "--ref", "main",
        ], runner=sender)
    assert error.value.code == 2
    assert capsys.readouterr().err == "OPSEC: invalid publisher arguments; use --help.\n"
    assert calls == []


@pytest.mark.parametrize("workflow", ["pytest-slow-nightly.yml", "security.yml"])
def test_unexpected_inputs_and_fields_cannot_reach_dispatch(dispatch_calls, capsys, workflow):
    """No-input workflow registration must not admit extra inputs or raw fields."""
    calls, sender = dispatch_calls
    assert pub.main([
        "workflow-run", "--repo", "unit/public",
        "--workflow", workflow, "--ref", "main",
        "--inputs", '{"force": true}',
    ], runner=sender) == 2
    assert capsys.readouterr().err == "OPSEC: invalid publisher field inputs.\n"
    with pytest.raises(gate.PublishBlocked, match=r"^OPSEC: unknown publisher field\.$"):
        pub.publish(
            "workflow-run", repo="unit/public", workflow=workflow,
            ref="main", raw_field="force=true", runner=sender,
        )
    assert calls == []


@pytest.mark.parametrize("workflow", ["pytest-slow-nightly.yml", "security.yml"])
def test_invalid_ref_cannot_reach_dispatch(dispatch_calls, capsys, workflow):
    """A ref containing a newline must fail existing ref validation before transport."""
    calls, sender = dispatch_calls
    assert pub.main([
        "workflow-run", "--repo", "unit/public",
        "--workflow", workflow, "--ref", "main\nother",
    ], runner=sender) == 2
    assert capsys.readouterr().err == "OPSEC: invalid publisher field ref.\n"
    assert calls == []


pytestmark = pytest.mark.usefixtures("configured_roots")


def test_ruleset_merge_queue_changes_only_the_named_parameter(dispatch_calls, capsys):
    """A ruleset write that edits another field, or reports success when the read-back differs, would ship.

    The defect is a merge-queue edit that also changes enforcement, another rule, an unnamed
    parameter, or a ruleset field the verb does not know how to preserve. Ported from product #12993.
    """
    publisher = pub
    assert [name for name in publisher.SCHEMAS if "ruleset" in name] == ["ruleset-merge-queue"]
    parameters = {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 2,
        "max_entries_to_merge": 4,
        "merge_method": "SQUASH",
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 0,
    }
    document = {
        "id": 20977023,
        "name": "main-merge-queue",
        "target": "branch",
        "source_type": "Repository",
        "source": "unit/public",
        "enforcement": "active",
        "node_id": "NODE",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "_links": {"self": {"href": "https://api.github.com/repos/unit/public/rulesets/20977023"}},
        "current_user_can_bypass": "never",
        "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
        "rules": [{"type": "deletion"}, {"type": "merge_queue", "parameters": parameters}],
    }
    endpoint = "repos/unit/public/rulesets/20977023"

    def serve(document, *, apply):
        calls, sent = [], {}
        state = json.loads(json.dumps(document))

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            assert args[args.index("--method") + 2] == endpoint
            calls.append(method)
            if method == "GET":
                return subprocess.CompletedProcess(args, 0, json.dumps(state), "")
            if method == "PUT":
                sent["body"] = json.loads(Path(args[args.index("--input") + 1]).read_text())
                if apply:
                    state.update(sent["body"])
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(method)

        return runner, calls, sent

    runner, calls, sent = serve(document, apply=True)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 0
    report = json.loads(capsys.readouterr().out)
    expected = {**parameters, "min_entries_to_merge_wait_minutes": 10}
    body = sent["body"]
    assert calls == ["GET", "PUT", "GET"]
    assert set(body) == {"name", "target", "enforcement", "bypass_actors", "conditions", "rules"}
    assert body["enforcement"] == "active" and body["conditions"] == document["conditions"]
    assert body["rules"][0] == {"type": "deletion"}
    assert body["rules"][1]["parameters"] == expected
    assert report == {"merge_queue": expected, "repo": "unit/public", "ruleset": 20977023}

    def refused(args, **kwargs):
        raise AssertionError("transport")

    with pytest.raises(publisher.gate.PublishBlocked, match="unknown publisher field"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=10, enforcement="disabled", runner=refused,
        )
    with pytest.raises(publisher.gate.PublishBlocked, match="invalid publisher field min_entries_to_merge_wait_minutes"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=361, runner=refused,
        )
    with pytest.raises(publisher.gate.PublishBlocked, match="explicit merge queue fields"):
        publisher.publish("ruleset-merge-queue", repo="unit/public", ruleset=20977023, runner=refused)
    with pytest.raises(SystemExit) as exc:
        publisher.main(
            ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
             "--min-entries-to-merge-wait-minutes", "10", "--enforcement", "disabled"],
            runner=refused,
        )
    assert exc.value.code == 2

    def before_put(document):
        calls = []

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            calls.append(method)
            if method == "PUT":
                raise AssertionError("put")
            return subprocess.CompletedProcess(args, 0, json.dumps(document), "")

        return runner, calls

    preserved = json.loads(json.dumps(document))
    preserved["future_field"] = True
    runner, calls = before_put(preserved)
    with pytest.raises(publisher.gate.PublishBlocked, match="does not preserve"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=10, runner=runner,
        )
    assert calls == ["GET"]

    missing = json.loads(json.dumps(document))
    del missing["rules"][1]["parameters"]["max_entries_to_build"]
    runner, calls = before_put(missing)
    with pytest.raises(publisher.gate.PublishBlocked, match="not on this ruleset"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            max_entries_to_build=2, runner=runner,
        )
    assert calls == ["GET"]

    duplicate = json.loads(json.dumps(document))
    duplicate["rules"].append({"type": "merge_queue", "parameters": dict(parameters)})
    runner, calls = before_put(duplicate)
    with pytest.raises(publisher.gate.PublishBlocked, match="no single merge_queue rule"):
        publisher.publish(
            "ruleset-merge-queue", repo="unit/public", ruleset=20977023,
            min_entries_to_merge_wait_minutes=10, runner=runner,
        )
    assert calls == ["GET"]

    runner, calls, sent = serve(document, apply=False)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 3
    assert calls == ["GET", "PUT", "GET"] and "Do not retry" in capsys.readouterr().err
    assert sent["body"]["rules"][1]["parameters"]["min_entries_to_merge_wait_minutes"] == 10

    def readback(second_get):
        calls = []

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            calls.append(method)
            if method == "GET" and calls.count("GET") == 1:
                return subprocess.CompletedProcess(args, 0, json.dumps(document), "")
            if method == "PUT":
                return subprocess.CompletedProcess(args, 0, "", "")
            if method == "GET":
                return second_get(args)
            raise AssertionError(method)

        return runner, calls

    def rate_limited(args):
        return subprocess.CompletedProcess(
            args, 75, json.dumps({"error": "github_rate_limited", "reset_at": 1}), "",
        )

    runner, calls = readback(rate_limited)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 3
    assert calls == ["GET", "PUT", "GET"] and "Do not retry" in capsys.readouterr().err

    def broken(args):
        raise RuntimeError("read-back transport")

    runner, calls = readback(broken)
    assert publisher.main(
        ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
         "--min-entries-to-merge-wait-minutes", "10"],
        runner=runner,
    ) == 3
    assert calls == ["GET", "PUT", "GET"] and "Do not retry" in capsys.readouterr().err

    def put_outcome(outcome):
        calls = []

        def runner(args, **kwargs):
            method = args[args.index("--method") + 1]
            calls.append(method)
            if method == "GET":
                return subprocess.CompletedProcess(args, 0, json.dumps(document), "")
            return outcome(args)

        return runner, calls

    def timed_out(args):
        raise subprocess.TimeoutExpired(args, 30)

    def lost(args):
        return subprocess.CompletedProcess(args, 1, "", "connection reset by peer")

    def rejected(args):
        # The shape the client's run() returns for a GitHub 4xx answer (_result_process).
        done = subprocess.CompletedProcess(args, 1, json.dumps({"error": "github_http_error", "status": 422}), "")
        done.github_result = github_client.Result(status=422, error="github_http_error")
        return done

    # A PUT whose answer is lost may already have landed: unverified, never a refusal or a retry.
    for outcome, code, text in ((timed_out, 3, "Do not retry"), (lost, 3, "Do not retry"),
                                (rejected, 1, "refused by GitHub")):
        runner, calls = put_outcome(outcome)
        assert publisher.main(
            ["ruleset-merge-queue", "--repo", "unit/public", "--ruleset", "20977023",
             "--min-entries-to-merge-wait-minutes", "10"],
            runner=runner,
        ) == code, outcome.__name__
        assert calls == ["GET", "PUT"] and text in capsys.readouterr().err, outcome.__name__

    with pytest.raises(SystemExit) as exc:
        publisher.main(["ruleset-merge-queue", "--help"])
    assert exc.value.code == 0
    assert "A rate limit on that read-back is exit 3, not 75" in capsys.readouterr().out


@pytest.fixture
def dismissal_transport(monkeypatch, synthetic_opsec):
    """A merged triage plus independent current and baseline SARIF, without an alert mutation."""
    import copy

    for key in ("LEARN_UKRAINIAN_DISPATCH_TASK_ID", pub.BROKER_ENV, "USER", "LOGNAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    before, current = "a" * 40, "b" * 40
    comment = "Alert #1 stores public metadata. https://github.com/unit/public/pull/2"
    instance = {
        "state": "open", "ref": "refs/heads/main", "commit_sha": current,
        "analysis_key": "codeql", "environment": "{}", "category": "python",
        "location": {"path": "sample.py", "start_line": 7}, "message": {"text": "Public metadata"},
    }
    alert = {"number": 1, "state": "open", "rule": {"id": "py/fixture"}, "most_recent_instance": instance}
    result = {
        "ruleId": "py/fixture", "rule": {"id": "py/fixture", "index": 0, "toolComponent": {"index": 0}},
        "message": {"text": "Public metadata"}, "properties": {"github/alertNumber": 1},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "sample.py", "index": 0}, "region": {"startLine": 7},
        }}],
        "codeFlows": [{"threadFlows": [{"locations": [{"location": {"message": {"text": "public source"}}}]}]}],
    }
    sarif = {"runs": [{"results": [result], "tool": {
        "driver": {"name": "CodeQL", "semanticVersion": "1.0.0"},
        "extensions": [{"name": "queries", "rules": [{"id": "py/fixture"}]}],
    }}]}
    root = "repos/unit/public/"
    documents = {
        "git/ref/heads/main": {"ref": "refs/heads/main", "object": {"type": "commit", "sha": current}},
        "pulls/2": {"merged": True, "merge_commit_sha": before, "html_url": "https://github.com/unit/public/pull/2", "base": {"ref": "main"}},
        "pulls/2/files?per_page=100": [[{"filename": pub._DISMISS_TRIAGE_PATH, "status": "modified", "patch": "+| 1 | `py/fixture` | `sample.py:7` | False positive. Public metadata only. |"}]],
        f"contents/{pub._DISMISS_TRIAGE_PATH}?ref={before}": "| 1 | `py/fixture` | `sample.py:7` | False positive. Public metadata only. |\n",
        "code-scanning/alerts/1": alert,
        "code-scanning/alerts/1/instances?per_page=100": [[instance]],
        "code-scanning/analyses?ref=refs%2Fheads%2Fmain&per_page=100&page=1": [
            {"id": aid, "commit_sha": sha, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": ""}
            for aid, sha in ((3, current), (2, before))
        ],
        "code-scanning/analyses/2": copy.deepcopy(sarif),
        "code-scanning/analyses/3": copy.deepcopy(sarif),
    }
    # Stale merge refs may retain older locations after their PRs stop being active.
    for number, state, merged in ((3, "MERGED", True), (4, "CLOSED", False)):
        documents[f"pulls/{number}"] = {
            "number": number, "state": state, "merged": merged,
            "url": f"https://github.com/unit/public/pull/{number}",
        }
        stale = copy.deepcopy(instance)
        stale.update(ref=f"refs/pull/{number}/merge", commit_sha="c" * 40)
        stale["location"]["start_line"] = 8
        documents["code-scanning/alerts/1/instances?per_page=100"][0].append(stale)
    writes, calls = [], []
    options = {"reason": "false positive", "readback": {}, "write_error": None, "read_error": None}

    def sender(argv, **kwargs):
        import re

        calls.append(list(argv))
        method = argv[argv.index("--method") + 1]
        endpoint = argv[argv.index("--method") + 2]
        if method == "POST" and endpoint == "graphql":
            body = json.loads(Path(argv[argv.index("--input") + 1]).read_text(encoding="utf-8"))
            assert body["variables"] == {"owner": "unit", "name": "public"}
            aliases = re.findall(r"(p\d+):\s*pullRequest\(number:(\d+)\)", body["query"])
            assert 1 <= len(aliases) <= pub._DISMISS_PR_BATCH_SIZE
            repo_rows = {alias: documents.get(f"pulls/{int(num)}") for alias, num in aliases}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"data": {"repository": repo_rows}}), "")
        assert endpoint.startswith(root)
        if method == "PATCH":
            assert endpoint == root + "code-scanning/alerts/1"
            writes.append(Path(argv[argv.index("--input") + 1]).read_bytes())
            if options["write_error"]:
                raise options["write_error"]
            if options.get("write_status"):
                reply = subprocess.CompletedProcess(argv, 1, "", "")
                reply.github_result = github_client.Result(status=options["write_status"])
                return reply
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        assert method == "GET"
        suffix = endpoint.removeprefix(root)
        if suffix == "code-scanning/alerts/1" and writes:
            if options["read_error"]:
                raise options["read_error"]
            document = {**alert, "state": "dismissed", "dismissed_reason": options["reason"], "dismissed_comment": comment, **options["readback"]}
        elif suffix == f"contents/{pub._DISMISS_TRIAGE_PATH}?ref={current}":
            document = options.get("current_triage", documents[f"contents/{pub._DISMISS_TRIAGE_PATH}?ref={before}"])
        else:
            document = documents[suffix]
        return subprocess.CompletedProcess(argv, 0, document if isinstance(document, str) else json.dumps(document), "")

    return documents, writes, calls, options, sender, comment


@pytest.mark.parametrize("reason", ["false positive", "won't fix", "used in tests"])
def test_dismissal_fixes_endpoint_state_and_replays_scanned_comment(dismissal_transport, monkeypatch, reason):
    documents, writes, _, options, sender, comment = dismissal_transport
    options["reason"] = reason
    if reason == "won't fix":
        key = next(key for key in documents if key.startswith("contents/"))
        documents[key] = documents[key].replace("False positive.", "Won't fix.")
    elif reason == "used in tests":
        key = next(key for key in documents if key.startswith("contents/"))
        documents[key] = documents[key].replace("sample.py", "tests/sample.py")
        documents["code-scanning/alerts/1/instances?per_page=100"][0][0]["location"]["path"] = "tests/sample.py"
        for aid in (2, 3):
            documents[f"code-scanning/analyses/{aid}"]["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = "tests/sample.py"
    triage_key = next(key for key in documents if key.startswith("contents/"))
    documents["pulls/2/files?per_page=100"][0][0]["patch"] = "+" + documents[triage_key].strip()
    scanned = []
    original = gate.check_texts

    def scan(destination, texts, **kwargs):
        scanned.extend(zip(kwargs["field_names"], texts))
        return original(destination, texts, **kwargs)

    monkeypatch.setattr(gate, "check_texts", scan)
    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason=reason, comment=comment, runner=sender, capture_output=True, text=True)
    assert answer.returncode == 0
    assert len(writes) == 1
    assert json.loads(writes[0]) == {"state": "dismissed", "dismissed_reason": reason, "dismissed_comment": comment}
    assert ("comment", comment) in scanned
    assert json.loads(answer.stdout)["dismissed_comment"] == comment
    assert pub.ENUMS["reason"] == {"completed", "not planned"}


@pytest.mark.parametrize(("payload_slug", "accepted"), [("unit/public", True), ("neworg/public", True), ("evil/public", False)])
def test_dismissal_binds_either_baked_slug_of_the_repository(dismissal_transport, monkeypatch, payload_slug, accepted):
    """Defect class: a payload naming the other baked slug of the same repository is refused, or a slug
    with the same repository name under another owner is accepted (dismissed through unit/public)."""
    from tests.test_fleet_repos import transferred_catalog

    transferred_catalog(monkeypatch, github="unit/public", alternate="neworg/public")
    documents, writes, _, options, sender, comment = dismissal_transport
    comment = comment.replace("/unit/public/", f"/{payload_slug}/")
    options["readback"] = {"dismissed_comment": comment}
    documents["pulls/2"]["html_url"] = f"https://github.com/{payload_slug}/pull/2"
    for number in (3, 4):
        documents[f"pulls/{number}"]["url"] = f"https://github.com/{payload_slug}/pull/{number}"

    if not accepted:
        with pytest.raises(gate.PublishBlocked):
            pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
                        comment=comment, runner=sender, capture_output=True, text=True)
        assert writes == []
        return
    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
                         comment=comment, runner=sender, capture_output=True, text=True)
    assert answer.returncode == 0
    assert len(writes) == 1


@pytest.mark.parametrize("change", [
    {"number": 0}, {"number": True}, {"number": "1"}, {"reason": "completed"}, {"reason": "mitigated"},
    {"comment": "Alert #1 - https://github.com/unit/public/pull/2"},
    {"comment": "Alert #1 _ 123 https://github.com/unit/public/pull/2"},
    {"endpoint": "user"}, {"method": "POST"}, {"state": "open"}, {"json": {}}, {"query": "mutation {}"},
    {"comment": ""}, {"comment": "   "}, {"comment": "Alert #1 public data."},
    {"comment": "Alert #1 public data. https://github.com/other/repo/pull/2"},
    {"comment": "Alert #10 public data. https://github.com/unit/public/pull/2"},
    {"comment": "Alert #1 https://github.com/unit/public/pull/2"},
    {"comment": "Alert #1 public data. https://github.com/unit/public/pull/2/other"},
])
def test_dismissal_rejects_open_payload_and_unbound_comments(dismissal_transport, change):
    _, writes, _, _, sender, comment = dismissal_transport
    fields = {"repo": "unit/public", "number": 1, "reason": "false positive", "comment": comment, **change}
    with pytest.raises(gate.PublishBlocked):
        pub.publish("code-scanning-dismiss", runner=sender, **fields)
    assert writes == []


@pytest.mark.parametrize("fault", ["unmerged", "unrelated", "missing-row", "rule", "location", "flow", "analysis", "tool", "branch", "unknown-flow", "inherited-row"])
def test_dismissal_refuses_missing_or_changed_reviewed_evidence(dismissal_transport, fault):
    documents, writes, _, _, sender, comment = dismissal_transport
    if fault == "unmerged":
        documents["pulls/2"]["merged"] = False
    elif fault == "unrelated":
        documents["pulls/2/files?per_page=100"][0][0]["filename"] = "other.md"
    elif fault == "missing-row":
        key = next(key for key in documents if key.startswith("contents/"))
        documents[key] = documents[key].replace("| 1 |", "| 2 |")
    elif fault == "inherited-row":
        documents["pulls/2/files?per_page=100"][0][0]["patch"] = "+Unrelated documentation edit."
    elif fault == "rule":
        documents["code-scanning/alerts/1"]["rule"]["id"] = "py/other"
    elif fault == "location":
        documents["code-scanning/alerts/1/instances?per_page=100"][0][0]["location"]["start_line"] = 8
    elif fault == "flow":
        documents["code-scanning/analyses/3"]["runs"][0]["results"][0]["codeFlows"] = []
    elif fault == "analysis":
        documents["code-scanning/analyses?ref=refs%2Fheads%2Fmain&per_page=100&page=1"] = []
    elif fault == "tool":
        documents["code-scanning/analyses/3"]["runs"][0]["tool"]["driver"]["semanticVersion"] = "2.0.0"
    elif fault == "branch":
        documents["pulls/5"] = {
            "number": 5, "state": "OPEN", "merged": False,
            "url": "https://github.com/unit/public/pull/5",
        }
        instance = dict(documents["code-scanning/alerts/1/instances?per_page=100"][0][0],
                        ref="refs/pull/5/merge", location={"path": "sample.py", "start_line": 8})
        documents["code-scanning/alerts/1/instances?per_page=100"][0].append(instance)
    else:
        documents["code-scanning/analyses/3"]["runs"][0]["results"] = []
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []


@pytest.mark.parametrize("fault", ["state", "reason", "comment", "read", "rate-limit", "write"])
def test_dismissal_ambiguous_transport_or_pending_request_is_unknown_without_retry(dismissal_transport, fault):
    _, writes, _, options, sender, comment = dismissal_transport
    if fault in {"state", "reason", "comment"}:
        key, value = {"state": ("state", "open"), "reason": ("dismissed_reason", "won't fix"), "comment": ("dismissed_comment", "other")}[fault]
        options["readback"][key] = value
    elif fault == "rate-limit":
        options["read_error"] = github_client.GitHubRateLimited(None, "secondary_hold")
    elif fault == "write":
        options["write_error"] = TimeoutError("private transport detail")
    else:
        options["read_error"] = OSError("private transport detail")
    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender, capture_output=True, text=True)
    assert answer.returncode == 3
    assert "UNKNOWN" in answer.stderr and "Do not retry" in answer.stderr
    assert "private transport detail" not in answer.stderr
    assert len(writes) == 1


def test_dismissal_scanner_refusal_precedes_all_transport(dismissal_transport):
    from tests.opsec_fixtures import TOKEN

    _, writes, calls, _, sender, comment = dismissal_transport
    with pytest.raises(gate.PublishBlocked):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment + TOKEN, runner=sender)
    assert writes == [] and calls == []


@pytest.mark.parametrize("key,value", [
    ("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "worker-one"), ("LU_WRITE_BOUNDARY_BROKER", "/fixture/socket"),
    ("USER", "review"), ("LOGNAME", "review"),
])
@pytest.mark.parametrize("source", ["process", "api-env"])
def test_dismissal_worker_and_reviewer_cli_and_direct_api_refuse_before_transport(dismissal_transport, monkeypatch, key, value, source):
    _, writes, calls, _, sender, comment = dismissal_transport
    environment = {}
    if source == "process":
        monkeypatch.setenv(key, value)
    else:
        environment[key] = value
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_BOUNDARY_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, env=environment, runner=sender)
    if source == "process":
        assert pub.main(["code-scanning-dismiss", "--repo", "unit/public", "--number", "1", "--reason", "false positive", "--comment", comment], runner=sender) == 2
    assert calls == [] and writes == []


def test_dismissal_cli_verifies_the_final_state(dismissal_transport, capsys):
    _, writes, _, _, sender, comment = dismissal_transport
    assert pub.main(["code-scanning-dismiss", "--repo", "unit/public", "--number", "1", "--reason", "false positive", "--comment", comment], runner=sender) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"number": 1, "state": "dismissed", "dismissed_reason": "false positive", "dismissed_comment": comment}
    assert len(writes) == 1


def test_dismissal_refuses_evidence_that_changes_during_preflight(dismissal_transport):
    _, writes, _, _, sender, comment = dismissal_transport
    reads = 0

    def changing(argv, **kwargs):
        nonlocal reads
        result = sender(argv, **kwargs)
        if argv[:5] == ["gh", "api", "--method", "GET", "repos/unit/public/code-scanning/alerts/1"]:
            reads += 1
            if reads == 2:
                result.stdout = json.dumps({**json.loads(result.stdout), "state": "fixed"})
        return result

    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=changing)
    assert reads == 2 and writes == []


@pytest.mark.parametrize("reason,disposition", [("false positive", "Fixed."), ("won't fix", "False positive."), ("used in tests", "False positive.")])
def test_dismissal_reason_must_match_the_reviewed_disposition(dismissal_transport, reason, disposition):
    documents, writes, _, _, sender, comment = dismissal_transport
    key = next(key for key in documents if key.startswith("contents/"))
    documents[key] = documents[key].replace("False positive.", disposition)
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason=reason, comment=comment, runner=sender)
    assert writes == []


@pytest.mark.parametrize("current_triage", ["", "| 1 | `py/fixture` | `sample.py:7` | Real. Sensitive data. |\n", "| 1 | `py/fixture` | `sample.py:7` | Won't fix. Accepted risk. |\n"])
def test_dismissal_refuses_retracted_or_reclassified_current_triage(dismissal_transport, current_triage):
    _, writes, _, options, sender, comment = dismissal_transport
    options["current_triage"] = current_triage
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []


def test_dismissal_rejects_a_moved_unchanged_triage_row(dismissal_transport):
    documents, writes, _, _, sender, comment = dismissal_transport
    file = documents["pulls/2/files?per_page=100"][0][0]
    file["patch"] += "\n-" + file["patch"][1:]
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []


@pytest.mark.parametrize("text", [False, True])
@pytest.mark.parametrize("capture", [False, True])
def test_dismissal_definite_refusal_reports_fixed_message_and_preserves_output_type(dismissal_transport, capsys, text, capture):
    _, writes, _, options, sender, comment = dismissal_transport
    options["write_status"] = 422
    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender, text=text, capture_output=capture)
    assert answer.returncode == 1 and len(writes) == 1
    assert answer.stderr == ("GitHub refused the dismissal." if text else b"GitHub refused the dismissal.")
    assert ("GitHub refused" in capsys.readouterr().err) == (not capture)


def test_dismissal_refuses_a_current_triage_head_that_moves_during_validation(dismissal_transport):
    _, writes, _, _, sender, comment = dismissal_transport
    reads = 0

    def moving(argv, **kwargs):
        nonlocal reads
        answer = sender(argv, **kwargs)
        if argv[:5] == ["gh", "api", "--method", "GET", "repos/unit/public/git/ref/heads/main"]:
            reads += 1
            if reads == 2:
                answer.stdout = json.dumps({"ref": "refs/heads/main", "object": {"type": "commit", "sha": "c" * 40}})
        return answer

    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=moving)
    assert reads == 2 and writes == []


@pytest.mark.parametrize("row", [
    " | 1 | `py/fixture` | `sample.py:7` | Real. |",
    "| 1 | `py/fixture` | `sample.py:7` | Real. | ",
    "\t| 1 | `py/fixture` | `sample.py:7` | Real. |\t",
    "1 | `py/fixture` | `sample.py:7` | Real.",
    "| 1 | `py/fixture` | `sample.py:7` | Real.",
    "1 | `py/fixture` | `sample.py:7` | Real. |",
])
def test_dismissal_refuses_padded_or_outer_pipe_optional_contradicting_rows(dismissal_transport, row):
    documents, writes, _, options, sender, comment = dismissal_transport
    original = documents[next(key for key in documents if key.startswith("contents/"))]
    options["current_triage"] = original + row + "\n"
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []


@pytest.mark.parametrize("change", [
    {"ref": "refs/tags/main", "object": {"type": "commit", "sha": "b" * 40}},
    {"ref": "refs/heads/main", "object": {"type": "tag", "sha": "b" * 40}},
    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "b" * 39}},
])
def test_dismissal_current_triage_requires_the_exact_branch_commit_reference(dismissal_transport, change):
    documents, writes, calls, _, sender, comment = dismissal_transport
    documents["git/ref/heads/main"] = change
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []
    assert any("repos/unit/public/git/ref/heads/main" in argv for argv in calls)
    assert not any(any("commits/main" in item for item in argv) for argv in calls)


def test_dismissal_paginates_analyses_until_match_on_page_n(dismissal_transport):
    """Defect class: unbounded history fetch or missed match when the commit analysis is on page N."""
    documents, writes, _, _, sender, comment = dismissal_transport
    documents["code-scanning/alerts/1"]["tool"] = {"name": "CodeQL"}
    documents["pulls/2"]["merged_at"] = "2026-11-21T01:00:00Z"
    before, current = "a" * 40, "b" * 40
    pages = {
        1: [
            {"id": 9, "commit_sha": "f" * 40, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": "", "created_at": "2026-11-21T01:40:00Z"},
            {"id": 3, "commit_sha": current, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": "", "created_at": "2026-11-21T01:30:00Z"},
        ],
        2: [
            {"id": 8, "commit_sha": "e" * 40, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": "", "created_at": "2026-11-21T01:20:00Z"},
            {"id": 7, "commit_sha": before, "analysis_key": "codeql", "environment": "{}", "category": "other", "error": "", "created_at": "2026-11-21T01:10:00Z"},
        ],
        3: [
            {"id": 2, "commit_sha": before, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": "", "created_at": "2026-11-21T01:02:00Z"},
        ],
    }
    prefix = "repos/unit/public/code-scanning/analyses?ref=refs%2Fheads%2Fmain&tool_name=CodeQL&per_page=100&page="
    analysis_calls = []

    def paginated(argv, **kwargs):
        endpoint = argv[argv.index("--method") + 2]
        if endpoint.startswith("repos/unit/public/code-scanning/analyses?"):
            assert "--paginate" not in argv and "--slurp" not in argv
            assert endpoint.startswith(prefix), endpoint
            page_num = int(endpoint.removeprefix(prefix))
            analysis_calls.append(page_num)
            assert page_num <= 3, f"fetched past matching page: {page_num}"
            return subprocess.CompletedProcess(argv, 0, json.dumps(pages[page_num]), "")
        return sender(argv, **kwargs)

    answer = pub.publish(
        "code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
        comment=comment, runner=paginated, capture_output=True, text=True,
    )
    assert answer.returncode == 0
    assert analysis_calls == [1, 2, 3]
    assert len(writes) == 1


@pytest.mark.parametrize("mode", [
    "bound", "older-than-commit", "timeout",
    "missing-commit-sha", "invalid-commit-sha", "missing-analysis-key", "invalid-environment", "invalid-category",
])
def test_dismissal_refuses_when_paginated_analyses_never_match(dismissal_transport, mode):
    """Defect class: unbounded pagination or fail-open when analyses never match, pass the commit time, time out, or carry malformed identity fields."""
    documents, writes, _, _, sender, comment = dismissal_transport
    if mode == "older-than-commit":
        documents["pulls/2"]["merged_at"] = "2026-11-21T01:00:00Z"
    prefix = "repos/unit/public/code-scanning/analyses?ref=refs%2Fheads%2Fmain&per_page=100&page="
    valid_rows = documents["code-scanning/analyses?ref=refs%2Fheads%2Fmain&per_page=100&page=1"]
    analysis_calls = []

    def never_matches(argv, **kwargs):
        endpoint = argv[argv.index("--method") + 2]
        if endpoint.startswith("repos/unit/public/code-scanning/analyses?"):
            assert "--paginate" not in argv and "--slurp" not in argv
            assert endpoint.startswith(prefix), endpoint
            page_num = int(endpoint.removeprefix(prefix))
            analysis_calls.append(page_num)
            if mode == "timeout" and page_num == 2:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 30))
            if mode in {"missing-commit-sha", "invalid-commit-sha", "missing-analysis-key", "invalid-environment", "invalid-category"}:
                bad = {"id": 99, "commit_sha": "f" * 40, "analysis_key": "codeql", "environment": "{}", "category": "python", "error": ""}
                if mode == "missing-commit-sha":
                    bad.pop("commit_sha")
                elif mode == "invalid-commit-sha":
                    bad["commit_sha"] = "not-a-sha"
                elif mode == "missing-analysis-key":
                    bad.pop("analysis_key")
                elif mode == "invalid-environment":
                    bad["environment"] = None
                else:
                    bad["category"] = 123
                return subprocess.CompletedProcess(argv, 0, json.dumps([bad, *valid_rows]), "")
            row = {
                "id": 100 + page_num, "commit_sha": "f" * 40,
                "analysis_key": "codeql", "environment": "{}", "category": "python", "error": "",
            }
            if mode == "older-than-commit":
                row["created_at"] = "2026-11-20T00:00:00Z"
            return subprocess.CompletedProcess(argv, 0, json.dumps([row]), "")
        return sender(argv, **kwargs)

    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=never_matches)
    assert writes == []
    expected_pages = {
        "bound": list(range(1, pub._DISMISS_ANALYSIS_MAX_PAGES + 1)),
        "older-than-commit": [1],
        "timeout": [1, 2],
        "missing-commit-sha": [1],
        "invalid-commit-sha": [1],
        "missing-analysis-key": [1],
        "invalid-environment": [1],
        "invalid-category": [1],
    }[mode]
    assert analysis_calls == expected_pages


def test_dismissal_batches_pr_states_in_bounded_requests_and_fails_closed_on_reopen_or_bad_graphql(dismissal_transport, tmp_path, monkeypatch):
    """Defect class: per-PR GET amplification on closed/merged PRs, duplicate same-commit triage fetch, or fail-open on reopened, missing/404, partial, or malformed batched PR state responses."""
    import copy

    documents, writes, calls, _, sender, comment = dismissal_transport
    before = "a" * 40
    main_instance = copy.deepcopy(documents["code-scanning/alerts/1/instances?per_page=100"][0][0])
    main_instance["commit_sha"] = before
    documents["git/ref/heads/main"]["object"]["sha"] = before

    def set_pr_instances(count):
        instances = [copy.deepcopy(main_instance)]
        for number in range(100, 100 + count):
            state, merged = ("MERGED", True) if number % 2 == 0 else ("CLOSED", False)
            documents[f"pulls/{number}"] = {
                "number": number, "state": state, "merged": merged,
                "url": f"https://github.com/unit/public/pull/{number}",
            }
            stale = copy.deepcopy(main_instance)
            stale.update(ref=f"refs/pull/{number}/merge", commit_sha="c" * 40)
            stale["location"]["start_line"] = 8
            instances.append(stale)
        documents["code-scanning/alerts/1/instances?per_page=100"] = [instances]

    # 97 closed/merged PRs: 11 preflight REST GETs (+1 post-PATCH readback GET) + 2 GraphQL POSTs = 13 preflight requests.
    set_pr_instances(97)
    answer = pub.publish(
        "code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
        comment=comment, runner=sender, capture_output=True, text=True,
    )
    assert answer.returncode == 0
    assert len(writes) == 1
    get_endpoints = [argv[argv.index("--method") + 2] for argv in calls if argv[argv.index("--method") + 1] == "GET"]
    graphql_calls = [argv for argv in calls if argv[argv.index("--method") + 1] == "POST" and argv[argv.index("--method") + 2] == "graphql"]
    assert len(get_endpoints) == 12  # 11 preflight GETs + 1 post-PATCH readback GET
    assert len(graphql_calls) == 2   # 1 loop batch + 1 race re-read batch
    assert sum(ep.startswith(f"repos/unit/public/contents/{pub._DISMISS_TRIAGE_PATH}?") for ep in get_endpoints) == 1
    assert not any(ep.startswith("repos/unit/public/pulls/") and ep.split("/")[-1].isdigit() and ep != "repos/unit/public/pulls/2" for ep in get_endpoints)

    # >100 PRs chunk cleanly at _DISMISS_PR_BATCH_SIZE (105 PRs -> 2 chunks x 2 phases = 4 GraphQL POSTs).
    writes.clear()
    calls.clear()
    set_pr_instances(105)
    answer = pub.publish(
        "code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
        comment=comment, runner=sender, capture_output=True, text=True,
    )
    assert answer.returncode == 0 and len(writes) == 1
    assert sum(argv[argv.index("--method") + 1] == "POST" and argv[argv.index("--method") + 2] == "graphql" for argv in calls) == 4

    # A skipped PR reopening during preflight must fail closed before PATCH.
    writes.clear()
    set_pr_instances(2)
    gql_reads = 0

    def reopened_during_preflight(argv, **kwargs):
        nonlocal gql_reads
        if argv[argv.index("--method") + 1] == "POST" and argv[argv.index("--method") + 2] == "graphql":
            gql_reads += 1
            if gql_reads == 2:
                documents["pulls/101"]["state"] = "OPEN"
        return sender(argv, **kwargs)

    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=reopened_during_preflight)
    assert gql_reads == 2 and writes == []

    # Missing/404 PR (null), GraphQL errors (including falsey/empty error envelopes with otherwise valid rows),
    # missing/non-object data, non-object extensions, unexpected keys, partial/extra alias keys, malformed rows,
    # and non-zero GraphQL exit must fail closed on both initial read and pre-PATCH recheck.
    set_pr_instances(2)
    valid_repo = {"p100": documents["pulls/100"], "p101": documents["pulls/101"]}
    bad_gql_responses = [
        (1, "{}"),
        (0, json.dumps([])),
        (0, json.dumps({"extensions": {}})),
        (0, json.dumps({"data": None})),
        (0, json.dumps({"data": []})),
        (0, json.dumps({"data": {"repository": valid_repo, "extra": 1}})),
        (0, json.dumps({"errors": [{"type": "NOT_FOUND"}], "data": {"repository": valid_repo}})),
        (0, json.dumps({"errors": [], "data": {"repository": valid_repo}})),
        (0, json.dumps({"errors": None, "data": {"repository": valid_repo}})),
        (0, json.dumps({"errors": False, "data": {"repository": valid_repo}})),
        (0, json.dumps({"data": {"repository": valid_repo}, "unexpected": True})),
        (0, json.dumps({"data": {"repository": valid_repo}, "extensions": []})),
        (0, json.dumps({"data": {"repository": valid_repo}, "extensions": None})),
        (0, json.dumps({"data": {"repository": valid_repo}, "extensions": False})),
        (0, json.dumps({"data": {"repository": valid_repo}, "extensions": "invalid"})),
        (0, json.dumps({"data": {"repository": valid_repo}, "extensions": 1})),
        (0, json.dumps({"data": {"repository": {"p100": documents["pulls/100"], "p101": None}}})),
        (0, json.dumps({"data": {"repository": {"p100": documents["pulls/100"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": documents["pulls/100"], "p101": documents["pulls/101"], "p102": documents["pulls/100"]}}})),
        (0, json.dumps({"data": {"repository": None}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "extra": 1}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "state": "OPEN", "merged": True}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "state": "CLOSED", "merged": True}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "state": "MERGED", "merged": False}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "state": "closed"}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "state": []}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "url": "https://github.com/other/public/pull/100"}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "number": 999}, "p101": documents["pulls/101"]}}})),
        (0, json.dumps({"data": {"repository": {"p100": {**documents["pulls/100"], "number": True}, "p101": documents["pulls/101"]}}})),
    ]
    for code, payload in bad_gql_responses:
        def bad_graphql(argv, code=code, payload=payload, **kwargs):
            if argv[argv.index("--method") + 1] == "POST" and argv[argv.index("--method") + 2] == "graphql":
                return subprocess.CompletedProcess(argv, code, payload, "")
            return sender(argv, **kwargs)

        with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
            pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=bad_graphql)
        assert writes == []

    # Wrong-typed extensions on the pre-PATCH recheck also fails closed with zero writes;
    # a spec-compliant object-valued extensions envelope is accepted.
    recheck_calls = 0

    def bad_extensions_on_recheck(argv, **kwargs):
        nonlocal recheck_calls
        if argv[argv.index("--method") + 1] == "POST" and argv[argv.index("--method") + 2] == "graphql":
            recheck_calls += 1
            if recheck_calls == 2:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"data": {"repository": valid_repo}, "extensions": []}), "",
                )
        return sender(argv, **kwargs)

    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=bad_extensions_on_recheck)
    assert recheck_calls == 2 and writes == []

    def valid_extensions_object(argv, **kwargs):
        if argv[argv.index("--method") + 1] == "POST" and argv[argv.index("--method") + 2] == "graphql":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"data": {"repository": valid_repo}, "extensions": {"warnings": []}}), "",
            )
        return sender(argv, **kwargs)

    answer = pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=valid_extensions_object, capture_output=True, text=True)
    assert answer.returncode == 0 and len(writes) == 1
    writes.clear()

    # Exercise the real GitHubClient raw-response HTTP seam (runner=None) on both initial read and pre-PATCH recheck:
    # top-level JSON strings, duplicate object members, and non-JSON constants must not be normalized away,
    # while valid responses succeed and GraphQL rate-limit answers preserve GitHubRateLimited.
    import re

    real_client_cls = github_client.GitHubClient
    http_writes = []
    http_state = {"gql_calls": 0, "phase": None, "override": None}

    def http_transport(method, endpoint, headers, body, timeout):
        if method == "POST" and endpoint == "graphql":
            http_state["gql_calls"] += 1
            if http_state["override"] is not None and http_state["gql_calls"] == http_state["phase"]:
                return http_state["override"]
            parsed = json.loads(body.decode("utf-8"))
            aliases = re.findall(r"(p\d+):\s*pullRequest\(number:(\d+)\)", parsed["query"])
            repo_rows = {alias: documents.get(f"pulls/{int(num)}") for alias, num in aliases}
            return github_client.Response(200, {}, json.dumps({"data": {"repository": repo_rows}}).encode("utf-8"))
        suffix = endpoint.removeprefix("repos/unit/public/")
        if method == "PATCH":
            assert suffix == "code-scanning/alerts/1"
            http_writes.append(body)
            return github_client.Response(200, {}, b"{}")
        assert method == "GET"
        if suffix == "code-scanning/alerts/1" and http_writes:
            doc = {**documents["code-scanning/alerts/1"], "state": "dismissed", "dismissed_reason": "false positive", "dismissed_comment": comment}
        elif suffix in {"pulls/2/files?per_page=100", "code-scanning/alerts/1/instances?per_page=100"}:
            doc = documents[suffix][0]
        else:
            doc = documents[suffix]
        raw_bytes = doc.encode("utf-8") if isinstance(doc, str) else json.dumps(doc).encode("utf-8")
        return github_client.Response(200, {}, raw_bytes)

    class RawHttpClient(real_client_cls):
        def __init__(self, **kwargs):
            kwargs["cache_dir"] = tmp_path / "gh-cache"
            kwargs["transport"] = http_transport
            super().__init__(**kwargs)

    monkeypatch.setattr(github_client, "GitHubClient", RawHttpClient)
    answer = pub.publish(
        "code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
        comment=comment, runner=None, capture_output=True, text=True,
    )
    assert answer.returncode == 0 and len(http_writes) == 1 and http_state["gql_calls"] == 2
    http_writes.clear()

    valid_repo_json = json.dumps(valid_repo)
    p100_json = json.dumps(documents["pulls/100"])
    raw_http_bad_bodies = [
        json.dumps(json.dumps({"data": {"repository": valid_repo}})).encode("utf-8"),
        ('{"data":{"repository":' + valid_repo_json + '},"extensions":[],"extensions":{}}').encode("utf-8"),
        ('{"data":{"repository":{"p100":' + p100_json + ',"p101":{"number":101,"state":"OPEN","state":"CLOSED","merged":false,"url":"https://github.com/unit/public/pull/101"}}}}').encode("utf-8"),
        ('{"data":{"repository":' + valid_repo_json + '},"extensions":{"cost":NaN}}').encode("utf-8"),
        ('{"data":{"repository":' + valid_repo_json + '},"extensions":{"cost":Infinity}}').encode("utf-8"),
        b'\xef\xbb\xbf{"data":{"repository":' + valid_repo_json.encode("utf-8") + b"}}",
        b"\xff",
    ]
    for phase in (1, 2):
        for raw_body in raw_http_bad_bodies:
            http_state.update(gql_calls=0, phase=phase, override=github_client.Response(200, {}, raw_body))
            with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
                pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=None)
            assert http_state["gql_calls"] == phase and http_writes == []

    # Typed rate-limit handling stays intact across the raw-response GraphQL seam (exit 75, zero writes).
    http_state.update(
        gql_calls=0,
        phase=1,
        override=github_client.Response(200, {}, json.dumps({"errors": [{"type": "RATE_LIMIT", "message": "API rate limit exceeded"}]}).encode("utf-8")),
    )
    answer = pub.publish(
        "code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
        comment=comment, runner=None, capture_output=True, text=True,
    )
    assert answer.returncode == 75
    assert json.loads(answer.stdout)["error"] == "github_rate_limited"
    assert http_state["gql_calls"] == 1 and http_writes == []

    # AC-02: _dismiss_get decodes REST JSON strictly on both real GitHubClient (runner=None) and injected runner:
    # duplicate keys, NaN/Infinity, UTF-8 BOM, invalid UTF-8, top-level JSON strings, and bad paginated pages refuse with zero writes.
    http_state.update(gql_calls=0, phase=None, override=None)
    valid_alert_json = json.dumps(documents["code-scanning/alerts/1"])
    bad_rest_bodies = [
        b'{"number":1,"state":"closed","state":"open","rule":{"id":"py/test"},"tool":{"name":"CodeQL"}}',
        b'{"number":1,"state":"open","rule":{"id":"py/test"},"extra":NaN}',
        b'{"number":1,"state":"open","rule":{"id":"py/test"},"extra":Infinity}',
        b'{"number":1,"state":"open","rule":{"id":"py/test"},"extra":1e999}',
        b'{"number":1,"state":"open","rule":{"id":"py/test"},"extra":-1e999}',
        b"\xef\xbb\xbf" + valid_alert_json.encode("utf-8"),
        b"\xff",
        json.dumps(valid_alert_json).encode("utf-8"),
        b"not json",
    ]
    for bad_rest in bad_rest_bodies:
        def rest_override_transport(method, endpoint, headers, body, timeout, bad_rest=bad_rest):
            if method == "GET" and endpoint == "repos/unit/public/code-scanning/alerts/1":
                return github_client.Response(200, {}, bad_rest)
            return http_transport(method, endpoint, headers, body, timeout)

        class BadRestHttpClient(real_client_cls):
            def __init__(self, **kwargs):
                kwargs["cache_dir"] = tmp_path / "gh-cache-rest"
                kwargs["transport"] = rest_override_transport
                super().__init__(**kwargs)

        monkeypatch.setattr(github_client, "GitHubClient", BadRestHttpClient)
        with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
            pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=None)
        assert http_writes == []

    # Post-PATCH readback via _dismiss_get also decodes strictly (duplicate key -> exit 3 UNKNOWN, never exit 0).
    def bad_readback_transport(method, endpoint, headers, body, timeout):
        if method == "GET" and endpoint == "repos/unit/public/code-scanning/alerts/1" and http_writes:
            return github_client.Response(
                200,
                {},
                b'{"number":1,"state":"open","state":"dismissed","dismissed_reason":"false positive","dismissed_comment":"' + comment.encode("utf-8") + b'"}',
            )
        return http_transport(method, endpoint, headers, body, timeout)

    class BadReadbackHttpClient(real_client_cls):
        def __init__(self, **kwargs):
            kwargs["cache_dir"] = tmp_path / "gh-cache-readback"
            kwargs["transport"] = bad_readback_transport
            super().__init__(**kwargs)

    monkeypatch.setattr(github_client, "GitHubClient", BadReadbackHttpClient)
    readback_answer = pub.publish(
        "code-scanning-dismiss", repo="unit/public", number=1, reason="false positive",
        comment=comment, runner=None, capture_output=True, text=True,
    )
    assert readback_answer.returncode == 3 and len(http_writes) == 1
    http_writes.clear()
    monkeypatch.setattr(github_client, "GitHubClient", real_client_cls)

    for bad_rest_text in (
        '{"number":1,"state":"closed","state":"open","rule":{"id":"py/test"},"tool":{"name":"CodeQL"}}',
        '{"number":1,"state":"open","rule":{"id":"py/test"},"extra":NaN}',
        '{"number":1,"state":"open","rule":{"id":"py/test"},"extra":-Infinity}',
        '{"number":1,"state":"open","rule":{"id":"py/test"},"extra":1e999}',
        '{"number":1,"state":"open","rule":{"id":"py/test"},"extra":-1e999}',
        "\ufeff" + valid_alert_json,
        json.dumps(valid_alert_json),
    ):
        def bad_rest_runner(argv, bad_rest_text=bad_rest_text, **kwargs):
            if argv[argv.index("--method") + 1] == "GET" and argv[argv.index("--method") + 2] == "repos/unit/public/code-scanning/alerts/1":
                return subprocess.CompletedProcess(argv, 0, bad_rest_text, "")
            return sender(argv, **kwargs)

        with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
            pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=bad_rest_runner)
        assert writes == []

    # Zero-page paginated REST response ([]) must also fail closed.
    documents["pulls/2/files?per_page=100"] = []
    with pytest.raises(gate.PublishBlocked, match="DISMISSAL_TRIAGE_REFUSED"):
        pub.publish("code-scanning-dismiss", repo="unit/public", number=1, reason="false positive", comment=comment, runner=sender)
    assert writes == []


QUEUE_REF = "refs/heads/gh-readonly-queue/main/pr-7-" + "d" * 40
LAST_OF_SET = "Analysis is last of its type and deletion may cause alert deletion. To delete this analysis, set the confirm_delete parameter to true."


@pytest.fixture
def delete_transport(monkeypatch, synthetic_opsec):
    """A fake GitHub answering the delete verbs' reads; every DELETE is logged before it is answered."""
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    root = "repos/unit/public/"
    documents = {
        "code-scanning/analyses/41": {
            "id": 41, "ref": QUEUE_REF, "deletable": True, "category": "/language:python", "tool": {"name": "CodeQL"},
        },
        "": {"full_name": "unit/public", "default_branch": "main"},
        "branches/salvage/old-attempt": {"name": "salvage/old-attempt", "protected": False, "commit": {"sha": "e" * 40}},
        "branches/main": {"name": "main", "protected": True, "commit": {"sha": "f" * 40}},
        "rules/branches/salvage/old-attempt?per_page=100": [[]],
        "pulls?state=open&per_page=100": [[
            {"number": 3, "head": {"ref": "claude/other", "repo": {"full_name": "unit/public"}}, "base": {"ref": "main"}},
            {"number": 4, "head": {"ref": "salvage/old-attempt", "repo": {"full_name": "fork/public"}}, "base": {"ref": "main"}},
        ]],
    }
    deletes = []
    options = {"delete_answers": [], "deleted": set()}

    def reply(argv, code, document=None, status=200):
        answer = subprocess.CompletedProcess(argv, code, "" if document is None else json.dumps(document), "")
        answer.github_result = github_client.Result(value=document, status=status)
        return answer

    def sender(argv, **kwargs):
        method = argv[argv.index("--method") + 1]
        endpoint = argv[argv.index("--method") + 2]
        assert endpoint.startswith(root.rstrip("/"))
        suffix = endpoint.removeprefix(root.rstrip("/")).removeprefix("/")
        if method == "DELETE":
            deletes.append(suffix)
            answer = options["delete_answers"].pop(0) if options["delete_answers"] else (200, {})
            if isinstance(answer, Exception):
                raise answer
            code_status, document = answer
            if code_status >= 400:
                return reply(argv, 1, document, code_status)
            options["deleted"].add(suffix.split("?")[0].replace("git/refs/", "git/ref/"))
            return reply(argv, 0, document, code_status)
        assert method == "GET"
        if suffix in options["deleted"] or suffix.startswith("git/ref/") or suffix not in documents:
            if suffix in options.get("present", ()):
                return reply(argv, 0, {"ref": "refs/heads/x"})
            return reply(argv, 1, {"message": "Not Found"}, 404)
        return reply(argv, 0, documents[suffix])

    return documents, deletes, options, sender


def test_queue_ref_analysis_is_deleted_newest_first_without_following_next_urls(delete_transport):
    """Defect class: a queue-ref analysis that is not last in its set is sent with confirm_delete, or the next URL is followed."""
    _, deletes, options, sender = delete_transport
    options["delete_answers"] = [(200, {
        "next_analysis_url": "https://api.github.com/repos/unit/public/code-scanning/analyses/40",
        "confirm_delete_url": "https://api.github.com/repos/unit/public/code-scanning/analyses/40?confirm_delete",
    })]
    answer = pub.publish("code-scanning-analysis-delete", repo="unit/public", analysis_id=41, runner=sender, capture_output=True, text=True)
    assert answer.returncode == 0, answer.stderr
    assert deletes == ["code-scanning/analyses/41"]
    assert json.loads(answer.stdout) == {
        "repo": "unit/public", "analysis_id": 41, "ref": QUEUE_REF, "category": "/language:python",
        "tool": "CodeQL", "confirm_delete": False, "deleted": True,
    }


def test_last_queue_ref_analysis_of_its_set_is_deleted_through_the_confirm_step(delete_transport, capsys):
    """Defect class: GitHub's last-of-set 400 ends the cleanup, or confirms a different analysis."""
    _, deletes, options, sender = delete_transport
    options["delete_answers"] = [(400, {"message": LAST_OF_SET}), (200, {"next_analysis_url": None, "confirm_delete_url": None})]
    assert pub.main(["code-scanning-analysis-delete", "--repo", "unit/public", "--analysis-id", "41"], runner=sender) == 0
    assert deletes == ["code-scanning/analyses/41", "code-scanning/analyses/41?confirm_delete"]
    assert json.loads(capsys.readouterr().out)["confirm_delete"] is True


@pytest.mark.parametrize("ref", [
    "refs/heads/main", "refs/pull/7/merge", "refs/pull/7/head", "refs/heads/gh-readonly-queue/main/pr-7",
    "refs/heads/gh-readonly-queue/release/pr-7-" + "d" * 40, QUEUE_REF + "x", "refs/heads/feature",
])
def test_analysis_on_main_pull_or_other_ref_is_refused_before_any_delete(delete_transport, ref):
    """Defect class: an analysis of main or a pull request ref reaches DELETE."""
    documents, deletes, _, sender = delete_transport
    documents["code-scanning/analyses/41"]["ref"] = ref
    with pytest.raises(gate.PublishBlocked, match="ANALYSIS_DELETE_REFUSED"):
        pub.publish("code-scanning-analysis-delete", repo="unit/public", analysis_id=41, runner=sender)
    assert deletes == []


@pytest.mark.parametrize("fault", ["older", "live-ref", "identity", "missing"])
def test_older_live_or_unreadable_queue_analysis_is_refused_before_any_delete(delete_transport, fault):
    """Defect class: an older analysis of its set, a live queue ref's analysis or unreadable evidence reaches DELETE."""
    documents, deletes, options, sender = delete_transport
    if fault == "older":
        documents["code-scanning/analyses/41"]["deletable"] = False
    elif fault == "live-ref":
        options["present"] = {"git/ref/heads/" + QUEUE_REF.removeprefix("refs/heads/")}
    elif fault == "identity":
        documents["code-scanning/analyses/41"]["id"] = 40
    else:
        del documents["code-scanning/analyses/41"]
    with pytest.raises(gate.PublishBlocked, match="ANALYSIS_DELETE_REFUSED"):
        pub.publish("code-scanning-analysis-delete", repo="unit/public", analysis_id=41, runner=sender)
    assert deletes == []


@pytest.mark.parametrize("fault,code", [("not-deletable", 1), ("transport", 3), ("server", 3), ("readback", 3)])
def test_analysis_delete_refusal_or_ambiguous_answer_is_never_retried(delete_transport, fault, code):
    """Defect class: a non-confirm 400 is retried with confirm_delete, or an ambiguous delete reports success."""
    _, deletes, options, sender = delete_transport
    options["delete_answers"] = [{
        "not-deletable": (400, {"message": "Analysis specified is not deletable."}),
        "transport": TimeoutError("private transport detail"),
        "server": (502, {"message": "Bad Gateway"}),
        "readback": (200, {}),
    }[fault]]
    if fault == "readback":
        options["present"] = {"code-scanning/analyses/41"}
    answer = pub.publish("code-scanning-analysis-delete", repo="unit/public", analysis_id=41, runner=sender, capture_output=True, text=True)
    assert answer.returncode == code
    assert deletes == ["code-scanning/analyses/41"]
    assert "private transport detail" not in answer.stderr


def test_exact_unused_branch_is_deleted(delete_transport):
    """Defect class: the delete targets another ref than the exact name, or a fork PR head blocks a local branch."""
    _, deletes, _, sender = delete_transport
    answer = pub.publish("branch-delete", repo="unit/public", branch="salvage/old-attempt", runner=sender, capture_output=True, text=True)
    assert answer.returncode == 0, answer.stderr
    assert deletes == ["git/refs/heads/salvage/old-attempt"]
    assert json.loads(answer.stdout) == {"repo": "unit/public", "branch": "salvage/old-attempt", "sha": "e" * 40, "deleted": True}


@pytest.mark.parametrize("fault", ["default", "protected", "ruleset", "open-head", "open-base", "headless-fork", "absent", "renamed"])
def test_default_protected_or_used_branch_is_refused_before_any_delete(delete_transport, fault):
    """Defect class: a default, protected, ruleset-covered or open-PR branch reaches DELETE."""
    documents, deletes, _, sender = delete_transport
    branch = "salvage/old-attempt"
    pulls = documents["pulls?state=open&per_page=100"][0]
    if fault == "default":
        branch = "main"
    elif fault == "protected":
        documents["branches/salvage/old-attempt"]["protected"] = True
    elif fault == "ruleset":
        documents["rules/branches/salvage/old-attempt?per_page=100"] = [[{"type": "deletion"}]]
    elif fault == "open-head":
        pulls.append({"number": 5, "head": {"ref": branch, "repo": {"full_name": "Unit/Public"}}, "base": {"ref": "main"}})
    elif fault == "open-base":
        pulls.append({"number": 5, "head": {"ref": "claude/x", "repo": {"full_name": "unit/public"}}, "base": {"ref": branch}})
    elif fault == "headless-fork":
        pulls.append({"number": 5, "head": {"ref": branch, "repo": None}, "base": {"ref": "main"}})
    elif fault == "absent":
        del documents["branches/salvage/old-attempt"]
    else:
        documents["branches/salvage/old-attempt"]["name"] = "Salvage/old-attempt"
    with pytest.raises(gate.PublishBlocked, match="BRANCH_DELETE_REFUSED"):
        pub.publish("branch-delete", repo="unit/public", branch=branch, runner=sender)
    assert deletes == []


def test_delete_verbs_require_an_explicit_repository_and_appear_in_help(delete_transport, capsys):
    """Defect class: a delete verb falls back to GH_REPO or origin, or is missing from the publisher help."""
    _, deletes, _, sender = delete_transport
    assert pub.main(["branch-delete", "--branch", "salvage/old-attempt"], runner=sender) == 2
    assert pub.main(["code-scanning-analysis-delete", "--analysis-id", "41"], runner=sender) == 2
    assert deletes == []
    with pytest.raises(SystemExit):
        pub.main(["--help"])
    listing = capsys.readouterr().out
    assert "code-scanning-analysis-delete" in listing and "branch-delete" in listing


@pytest.mark.parametrize("verb,fields,before", [
    ("code-scanning-analysis-delete", {"analysis_id": 41}, []),
    ("code-scanning-analysis-delete", {"analysis_id": 41}, [(400, {"message": LAST_OF_SET})]),
    ("branch-delete", {"branch": "salvage/old-attempt"}, []),
])
def test_rate_limited_delete_exits_75_without_another_delete(delete_transport, verb, fields, before):
    """Defect class: a DELETE GitHub rejects for its rate limit is retried, or reported as deleted or UNKNOWN."""
    _, deletes, options, sender = delete_transport
    options["delete_answers"] = [*before, github_client.GitHubRateLimited(None, "secondary_hold")]
    answer = pub.publish(verb, repo="unit/public", runner=sender, capture_output=True, text=True, **fields)
    assert answer.returncode == 75
    assert json.loads(answer.stdout)["error"] == "github_rate_limited"
    assert len(deletes) == len(before) + 1


def _malformed_envelopes_for(valid_data: dict) -> list[tuple[str, str]]:
    """Every wrong-typed, falsey-error, unknown-member, and non-strict JSON envelope class paired with valid data."""
    valid_json = json.dumps(valid_data)
    return [
        ("errors-empty-list", json.dumps({"data": valid_data, "errors": []})),
        ("errors-null", json.dumps({"data": valid_data, "errors": None})),
        ("errors-false", json.dumps({"data": valid_data, "errors": False})),
        ("errors-empty-str", json.dumps({"data": valid_data, "errors": ""})),
        ("errors-zero", json.dumps({"data": valid_data, "errors": 0})),
        ("errors-dict", json.dumps({"data": valid_data, "errors": {}})),
        ("errors-str", json.dumps({"data": valid_data, "errors": "err"})),
        ("errors-non-dict-entry", json.dumps({"data": valid_data, "errors": [1]})),
        ("errors-populated", json.dumps({"data": valid_data, "errors": [{"message": "boom"}]})),
        ("data-missing", json.dumps({})),
        ("data-missing-with-extensions", json.dumps({"extensions": {}})),
        ("data-null", json.dumps({"data": None})),
        ("data-list", json.dumps({"data": []})),
        ("data-false", json.dumps({"data": False})),
        ("data-str", json.dumps({"data": "ok"})),
        ("extensions-null", json.dumps({"data": valid_data, "extensions": None})),
        ("extensions-list", json.dumps({"data": valid_data, "extensions": []})),
        ("extensions-false", json.dumps({"data": valid_data, "extensions": False})),
        ("extensions-true", json.dumps({"data": valid_data, "extensions": True})),
        ("extensions-str", json.dumps({"data": valid_data, "extensions": "bad"})),
        ("extensions-int", json.dumps({"data": valid_data, "extensions": 1})),
        ("unknown-top-level-key", json.dumps({"data": valid_data, "unexpected": True})),
        ("top-level-list", json.dumps([])),
        ("top-level-json-string", json.dumps(json.dumps({"data": valid_data}))),
        ("duplicate-key", '{"data":' + valid_json + ',"extensions":[],"extensions":{}}'),
        ("nan-constant", '{"data":' + valid_json + ',"extensions":{"cost":NaN}}'),
        ("infinity-constant", '{"data":' + valid_json + ',"extensions":{"cost":Infinity}}'),
        ("float-overflow-pos", '{"data":' + valid_json + ',"extensions":{"cost":1e999}}'),
        ("float-overflow-neg", '{"data":' + valid_json + ',"extensions":{"cost":-1e999}}'),
        ("utf8-bom", '\ufeff{"data":' + valid_json + "}"),
    ]


@pytest.mark.parametrize(
    "case_id",
    [name for name, _ in _malformed_envelopes_for({})],
)
def test_publish_graphql_callers_refuse_malformed_envelopes_with_zero_writes(monkeypatch, tmp_path, synthetic_opsec, case_id):
    """Defect class: _confirm_enqueued, _transfer_label_connection, and read('issue-count') accept falsey/wrong-typed errors, non-object data/extensions, unknown keys, or non-strict JSON."""
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    head = "a" * 40

    # 1. _confirm_enqueued refuses with ENQUEUE_UNVERIFIED and sends zero writes (no --disable-auto).
    confirm_valid_data = {"repository": {"pullRequest": {"headRefOid": head, "isInMergeQueue": True, "autoMergeRequest": {"enabledAt": "2026-11-21T00:00:00Z"}}}}
    confirm_payload = dict(_malformed_envelopes_for(confirm_valid_data))[case_id]
    writes = []

    def confirm_read(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, confirm_payload, "")

    def confirm_write(argv, **kwargs):
        writes.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(gate.PublishBlocked, match="ENQUEUE_UNVERIFIED"):
        pub._confirm_enqueued("unit/public", 1, head, "github.com/unit/public", tmp_path, (confirm_read, confirm_write), tmp_path, {})
    assert writes == []

    # 2. _transfer_label_connection refuses with ValueError (and issue-transfer refuses with zero mutations).
    transfer_valid_data = {
        "repository": {
            "id": "R_1",
            "nameWithOwner": "unit/public",
            "visibility": "PUBLIC",
            "viewerPermission": "ADMIN",
            "isArchived": False,
            "labels": {"totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
        }
    }
    transfer_payload = dict(_malformed_envelopes_for(transfer_valid_data))[case_id]

    def transfer_read(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, transfer_payload, "")

    with pytest.raises(ValueError):
        pub._transfer_label_connection(
            pub.TRANSFER_REPOSITORY_QUERY,
            {"owner": "unit", "name": "public"},
            lambda repo: ({"id": repo["id"]}, repo["labels"]),
            transfer_read,
            tmp_path,
            {},
            tmp_path,
        )

    # 3. read("issue-count") refuses with OPSEC: malformed issue count response.
    count_valid_data = {"repository": {"issues": {"totalCount": 7}}}
    count_payload = dict(_malformed_envelopes_for(count_valid_data))[case_id]
    with pytest.raises(gate.PublishBlocked, match="malformed issue count response"):
        pub.read(
            "issue-count",
            repo="unit/public",
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, count_payload, ""),
            cwd=tmp_path,
            env={},
            capture_output=True,
            text=True,
        )


def test_publish_graphql_callers_accept_valid_extensions_object(monkeypatch, tmp_path, synthetic_opsec):
    """Defect class: strict GraphQL envelope check rejects spec-compliant object-valued extensions."""
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    head = "a" * 40
    ext = {"cost": 1}

    confirm_ok = json.dumps({
        "data": {"repository": {"pullRequest": {"headRefOid": head, "isInMergeQueue": True, "autoMergeRequest": None}}},
        "extensions": ext,
    })
    pub._confirm_enqueued(
        "unit/public", 1, head, "github.com/unit/public", tmp_path,
        (lambda argv, **kw: subprocess.CompletedProcess(argv, 0, confirm_ok, ""), lambda *a, **kw: pytest.fail("no write")),
        tmp_path, {},
    )

    transfer_ok = json.dumps({
        "data": {
            "repository": {
                "id": "R_1",
                "labels": {"totalCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"name": "bug"}]},
            }
        },
        "extensions": ext,
    })
    identity, labels = pub._transfer_label_connection(
        pub.TRANSFER_REPOSITORY_QUERY,
        {"owner": "unit", "name": "public"},
        lambda repo: ({"id": repo["id"]}, repo["labels"]),
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, transfer_ok, ""),
        tmp_path, {}, tmp_path,
    )
    assert identity == {"id": "R_1"} and labels == ["bug"]

    count_ok = json.dumps({"data": {"repository": {"issues": {"totalCount": 42}}}, "extensions": ext})
    result = pub.read(
        "issue-count",
        repo="unit/public",
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, count_ok, ""),
        cwd=tmp_path, env={}, capture_output=True, text=True,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["data"]["repository"]["issues"]["totalCount"] == 42

    # F3: production read("issue-count") (runner=None) raises PublishBlocked for malformed GraphQL envelopes
    # regardless of check=False/True, while preserving exit 75 rate limits and HTTP 500 errors.
    real_client_cls = github_client.GitHubClient
    http_reply = {"response": github_client.Response(200, {}, count_ok.encode("utf-8"))}

    class IssueCountHttpClient(real_client_cls):
        def __init__(self, **kwargs):
            kwargs["cache_dir"] = tmp_path / "issue-count-cache"
            kwargs["transport"] = lambda *a: http_reply["response"]
            super().__init__(**kwargs)

    monkeypatch.setattr(github_client, "GitHubClient", IssueCountHttpClient)
    over_depth_err = b'{"errors":' + b"[" * 513 + b"]" * 513 + b"}"
    gql_ct = {"Content-Type": "application/graphql-response+json; charset=utf-8"}
    for status_code, resp_headers, bad_body in (
        (200, {}, b'{"data":{"repository":{"issues":{"totalCount":7}}},"errors":[]}'),
        (200, {}, b'{"data":{"repository":{"issues":{"totalCount":7}}},"extensions":[]}'),
        (200, {}, b'{"data":{"repository":{"issues":{"totalCount":7}}},"extensions":{"cost":1e999}}'),
        (400, {}, b'{"errors":[{"message":"boom"}],"extensions":{"cost":1e999}}'),
        (500, {}, b'{"errors":[{"message":"boom"}],"errors":[]}'),
        (400, {}, over_depth_err),
        (400, gql_ct, over_depth_err),
        (500, gql_ct, b'{"errors":[{"message":"boom \xff"}]}'),
        (400, {}, b"\xff"),
        (400, {}, b"[]"),
        (400, {}, b'{"data":[]}'),
        (502, gql_ct, b"<html>502 Bad Gateway</html>"),
    ):
        http_reply["response"] = github_client.Response(status_code, resp_headers, bad_body)
        for check_flag in (False, True):
            with pytest.raises(gate.PublishBlocked, match="malformed issue count response"):
                pub.read("issue-count", repo="unit/public", runner=None, cwd=tmp_path, env={}, capture_output=True, text=True, check=check_flag)
    for http_status, http_body in (
        (500, b'{"message":"Server Error"}'),
        (502, b"<html>502 Bad Gateway</html>"),
    ):
        http_reply["response"] = github_client.Response(http_status, {}, http_body)
        err_http = pub.read("issue-count", repo="unit/public", runner=None, cwd=tmp_path, env={}, capture_output=True, text=True, check=False)
        assert err_http.returncode == 1 and err_http.github_result.error == "github_http_error"
        with pytest.raises(subprocess.CalledProcessError) as exc_http:
            pub.read("issue-count", repo="unit/public", runner=None, cwd=tmp_path, env={}, capture_output=True, text=True, check=True)
        assert exc_http.value.github_result.error == "github_http_error"
    http_reply["response"] = github_client.Response(200, {}, b'{"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}')
    rl = pub.read("issue-count", repo="unit/public", runner=None, cwd=tmp_path, env={}, capture_output=True, text=True, check=False)
    assert rl.returncode == 75 and rl.github_result.error == "github_rate_limited"
    monkeypatch.setattr(github_client, "GitHubClient", real_client_cls)
