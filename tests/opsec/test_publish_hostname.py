"""Resolve hosts once for built-in, REST and GraphQL publishing transports."""



import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from lu_machinery.opsec import prepublish as gate
from lu_machinery.opsec.gh_snapshot import admit, repository
from lu_machinery.publish import github as pub
from lu_machinery.review.record_cf_verdict import build_comment as _recovery_build_comment
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.opsec_fixtures import CATALOG as CATALOG
from tests.opsec_fixtures import ROOT as ROOT
from tests.opsec_fixtures import recorded_approval as recorded_approval

VALID_DESTINATIONS = [
    ("github.com/unit/public", []),
    ("GitHub.com/unit/public", []),
    ("GitHub.COM/unit/public", []),
    ("git.enterprise-example.test/unit/public", ["--hostname", "git.enterprise-example.test"]),
    ("GIT.Enterprise-Example.test/unit/public", ["--hostname", "git.enterprise-example.test"]),
    ("github.com.evil.example/unit/public", ["--hostname", "github.com.evil.example"]),
    ("evilgithub.com/unit/public", ["--hostname", "evilgithub.com"]),
    ("1-git.test/unit/public", ["--hostname", "1-git.test"]),
    ("g/unit/public", ["--hostname", "g"]),
    ("7/unit/public", ["--hostname", "7"]),
    ("unknown", []),
]
INVALID_DESTINATIONS = [
    "github.com@evil/unit/public",
    "github.com:443/unit/public",
    "",
    "/unit/public",
    "github.com",
    "github.com/",
    "github.com /unit/public",
    "git hub.com/unit/public",
    "github.com\t/unit/public",
    "github.com\n/unit/public",
    "github.com\r/unit/public",
    "github.com\0/unit/public",
    "github.com\x1f/unit/public",
    "github.com\x7f/unit/public",
    "gіthub.com/unit/public",  # Cyrillic i
    "github．com/unit/public",  # Full-width dot
    "Kithub.com/unit/public",  # Lowercasing would produce ASCII k
    "github.com\u202e/unit/public",  # Bidi control
    "git_hub.com/unit/public",
    ".github.com/unit/public",
    "github..com/unit/public",
    "github.com./unit/public",
    "-github.com/unit/public",
    "github-.com/unit/public",
]
INVALID_HOSTS = [
    dest.removesuffix("/unit/public") for dest in INVALID_DESTINATIONS if dest.endswith("/unit/public")
] + [
    "",
    "bad_host",
    "-x.example",
    "a..b",
    "user@github.com",
]


SOURCES = ["explicit", "explicit-host", "environment", "environment-host", "origin-https", "origin-http", "origin-ssh"]
PATHS = ["publish-builtin", "publish-rest", "publish-graphql", "publish-unlink", "read-graphql", "read-rest"]
UNBOUND_OPERATIONS = ["identity", "budget", "merge-facts", "gist-create"]


@pytest.mark.parametrize("dest,expected", VALID_DESTINATIONS)
def test_hostname_flag_uses_normalized_destinations(dest, expected):
    assert pub._hostname_flag(gate.normalize_repository(dest)) == expected


@pytest.mark.parametrize("dest", INVALID_DESTINATIONS)
def test_resolver_rejects_malformed_hosts(dest):
    assert gate.normalize_repository(dest) == "unknown"


@pytest.mark.parametrize("host", INVALID_HOSTS)
def test_hostname_normalizer_rejects_malformed_hosts(host):
    assert gate.normalize_hostname(host) is None


@pytest.mark.parametrize("dest,expected", VALID_DESTINATIONS[:-1])
def test_hostname_normalizer_preserves_valid_hosts(dest, expected):
    host = dest.split("/", 1)[0]
    assert gate.normalize_hostname(host) == host.lower()


@pytest.fixture
def destination_transport(monkeypatch, synthetic_opsec):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    calls = []

    def send(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def invoke(path, dest, source):
        host, _, short = dest.partition("/")
        environment, fields = {}, {}
        origin = "not-a-repository"
        if source == "explicit":
            fields["repo"] = dest
        elif source == "explicit-host":
            fields["repo"] = short
            environment["GH_HOST"] = host
        elif source == "environment":
            environment["GH_REPO"] = dest
        elif source == "environment-host":
            environment.update(GH_REPO=short, GH_HOST=host)
        elif source == "origin-ssh":
            origin = f"git@{host}:{short}.git"
        else:
            origin = f"{source.removeprefix('origin-')}://{dest}.git"

        def git_read(argv, **kwargs):
            assert argv == ["git", "remote", "get-url", "origin"]
            return subprocess.CompletedProcess(argv, 0, origin, "")

        # Keep real resolution/normalization; replace only the local Git probe.
        monkeypatch.setattr(pub, "repository", lambda *args: repository(*args, reader=git_read))
        if path == "publish-builtin":
            return pub.publish("issue-reopen", number=1, env=environment, runner=send, **fields)
        if path == "publish-rest":
            return pub.publish("issue-comment-json", number=1, body="clean", env=environment, runner=send, **fields)
        if path in {"publish-graphql", "publish-unlink"}:
            return pub.publish(
                "issue-unlink" if path == "publish-unlink" else "issue-link",
                parent_id="parent",
                child_id="child",
                env=environment,
                runner=send,
                **fields,
            )
        if path == "read-graphql":
            return pub.read("membership", number=1, env=environment, runner=send, **fields)
        return pub.read("issue", number=1, env=environment, runner=send, **fields)

    return invoke, calls


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("dest,expected", VALID_DESTINATIONS[:-1])
def test_transports_select_exact_hostname(source, path, dest, expected, destination_transport):
    invoke, calls = destination_transport
    assert invoke(path, dest, source).returncode == 0
    assert len(calls) == 1
    argv, environment = calls[0]
    host = dest.split("/", 1)[0].lower()
    assert environment["GH_HOST"] == host
    if path == "publish-builtin":
        assert argv == ["gh", "issue", "reopen", "1", "--repo", "unit/public"]
        return
    assert argv[:4] == ["gh", "api", "--method", "GET" if path == "read-rest" else "POST"]
    if path == "publish-rest":
        assert "--hostname" not in argv
        return  # REST publishing selects the validated host through GH_HOST.
    if expected:
        index = argv.index("--hostname")
        assert argv[index : index + 2] == expected
        assert argv.count("--hostname") == 1
    else:
        assert "--hostname" not in argv


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("dest", [dest for dest in INVALID_DESTINATIONS if dest.endswith("/unit/public")])
def test_transports_reject_malformed_hostname_before_send(source, path, dest, destination_transport):
    invoke, calls = destination_transport
    with pytest.raises(gate.PublishBlocked):
        invoke(path, dest, source)
    assert calls == []


@pytest.mark.parametrize("path", PATHS)
def test_repository_bound_transports_refuse_unknown(path, destination_transport):
    invoke, calls = destination_transport
    with pytest.raises(gate.PublishBlocked, match="repository unresolved"):
        invoke(path, "unknown", "environment")
    assert calls == []


@pytest.fixture
def inherited_host_transport(monkeypatch, synthetic_opsec, tmp_path):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    calls = []
    asset = tmp_path / "unit.txt"
    asset.write_text("clean")

    def git_read(argv, **kwargs):
        assert argv == ["git", "remote", "get-url", "origin"]
        return subprocess.CompletedProcess(argv, 0, "not-a-repository", "")

    monkeypatch.setattr(pub, "repository", lambda *args: repository(*args, reader=git_read))

    def send(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        payload = {"resources": {"graphql": {"limit": 5000, "remaining": 4900, "used": 100, "reset": 2000}}} if "rate_limit" in argv else {"merged_at": None}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    def invoke(operation, env):
        options = {"env": env, "runner": send}
        if operation == "gist-create":
            return pub.publish(operation, files=[pub.Asset(asset)], **options)
        if operation == "merge-facts":
            return pub.read(operation, batch=[("unit/public", 1)], **options)
        if operation in {"identity", "budget"}:
            return pub.read(operation, **options)
        # Explicit validated destinations must not hide hostile inherited hosts.
        options["repo"] = "github.com/unit/public"
        if operation == "publish-builtin":
            return pub.publish("issue-reopen", number=1, **options)
        if operation == "publish-rest":
            return pub.publish("issue-comment-json", number=1, body="clean", **options)
        if operation in {"publish-graphql", "publish-unlink"}:
            return pub.publish(
                "issue-unlink" if operation == "publish-unlink" else "issue-link",
                parent_id="parent",
                child_id="child",
                **options,
            )
        if operation == "read-graphql":
            return pub.read("membership", number=1, **options)
        return pub.read("issue", number=1, **options)

    return invoke, calls


@pytest.mark.parametrize("operation", UNBOUND_OPERATIONS + PATHS)
@pytest.mark.parametrize("host", INVALID_HOSTS)
@pytest.mark.parametrize("inherited", [False, True], ids=["explicit-env", "process-env"])
def test_inherited_malformed_hosts_never_reach_transport(
    operation, host, inherited, monkeypatch, inherited_host_transport
):
    invoke, calls = inherited_host_transport
    environment = {"GH_HOST": host}
    if inherited:
        monkeypatch.setattr(pub.os, "environ", environment)
    with pytest.raises(gate.PublishBlocked, match="invalid publisher hostname") as error:
        invoke(operation, None if inherited else environment)
    assert str(error.value) == "OPSEC: invalid publisher hostname."
    assert calls == []
    assert environment == {"GH_HOST": host}


@pytest.mark.parametrize("operation", UNBOUND_OPERATIONS)
@pytest.mark.parametrize("dest,expected", VALID_DESTINATIONS[:-1])
def test_unbound_transports_normalize_inherited_hosts(operation, dest, expected, inherited_host_transport):
    invoke, calls = inherited_host_transport
    host = dest.split("/", 1)[0]
    environment = {"GH_HOST": host}
    assert invoke(operation, environment).returncode == 0
    assert len(calls) == 1
    argv, child_environment = calls[0]
    assert child_environment["GH_HOST"] == host.lower()
    assert "--hostname" not in argv
    assert environment == {"GH_HOST": host}


@pytest.mark.parametrize("operation", UNBOUND_OPERATIONS)
def test_unbound_transports_preserve_absent_host(operation, inherited_host_transport):
    invoke, calls = inherited_host_transport
    assert invoke(operation, {}).returncode == 0
    assert len(calls) == 1
    argv, environment = calls[0]
    assert "GH_HOST" not in environment
    assert "--hostname" not in argv




@pytest.fixture
def dependabot_checkout(tmp_path, monkeypatch):
    """A real catalog checkout independent of the installed package directory."""
    checkout = tmp_path / CATALOG["public"]["local_name"]
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "add", "origin", "https://github.com/unit/public.git"],
        check=True, capture_output=True,
    )
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    package = tmp_path / "site-packages"
    package.mkdir()
    monkeypatch.setattr(gate, "ROOT", package)
    return checkout


@pytest.mark.parametrize("declared", [False, True])
def test_dependabot_installed_package_reads_callers_physical_checkout(dependabot_checkout, declared):
    """A package outside Git must not prevent a proved checkout's inventory read."""
    checkout = dependabot_checkout
    calls = []
    nested = checkout / "nested"
    nested.mkdir()

    def sender(argv, **kwargs):
        calls.append(argv)
        assert kwargs["cwd"].resolve() == nested
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(argv, 0, '[]\n[]\n', "")

    result = pub.read(
        "dependabot-alerts", cwd=nested,
        env={"LU_PRODUCT_ROOT": str(checkout)} if declared else {},
        runner=sender, capture_output=True, text=True,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == []
    assert len(calls) == 1
    assert calls[0][:7] == [
        "gh", "api", "--method", "GET", "repos/unit/public/dependabot/alerts?state=open&per_page=100",
        "--paginate", "--jq",
    ]


@pytest.mark.parametrize("unproved", ["origin", "host", "explicit", "inherited", "git", "symlink", "product"])
def test_dependabot_installed_package_keeps_identity_refusals(dependabot_checkout, tmp_path, unproved):
    """Changing the anchor must preserve physical identity and destination refusals."""
    checkout = dependabot_checkout
    environment, fields = {}, {}
    if unproved == "origin":
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "set-url", "origin", "https://other.example/unit/public.git"],
            check=True, capture_output=True,
        )
    elif unproved == "host":
        environment["GH_HOST"] = "other.example"
    elif unproved == "explicit":
        fields["repo"] = "unit/private"
    elif unproved == "inherited":
        environment["GH_REPO"] = "unit/private"
    elif unproved == "git":
        (checkout / ".git" / "HEAD").write_text("invalid\n")
    elif unproved == "symlink":
        link = tmp_path / "linked"
        link.symlink_to(checkout, target_is_directory=True)
        checkout = link
    else:
        environment["LU_PRODUCT_ROOT"] = str(tmp_path / "missing")
    with pytest.raises(gate.PublishBlocked, match="DEPENDABOT_REPOSITORY_REFUSED"):
        pub.read(
            "dependabot-alerts", cwd=checkout, env=environment,
            runner=lambda *a, **k: pytest.fail("unproved checkout reached transport"), **fields,
        )


_recovery_HEAD = "a" * 40


_recovery_TESTED = "c" * 40


def _recovery_evidence(head=_recovery_HEAD):
    return {
        "pr": 42,
        "head": head,
        "run_id": 123,
        "job_ids": [456],
        "tested_sha": _recovery_TESTED,
        "queue_group_diff": {
            "base_sha": "d" * 40,
            "head_sha": _recovery_TESTED,
            "paths": ["lu_machinery/earlier-pr.py", "lu_machinery/this-pr.py"],
        },
        "failure_evidence": "Job log confirms runner checkout failure",
        "unrelated_to_diff": "Failure precedes execution of changed code",
    }


_recovery_NEW_HEAD = "b" * 40


class _recovery_Transport:
    def __init__(self):
        self.head = _recovery_HEAD
        self.proof = _recovery_evidence()
        self.approved = True
        self.attempt = 1
        self.status = "completed"
        self.conclusion = "failure"
        self.event = "merge_group"
        self.failed_ids = [456]
        self.removed = True
        self.future_commit = False
        self.removal_head = _recovery_HEAD
        self.removal_date = "2026-10-09T11:00:00Z"
        self.removal_failure = False
        self.removal_history = None
        self.removal_page_size = 100
        self.removal_reason = "removed by actor"
        self.timeline_count = None
        self.removal_reads = []
        self.pushes = []
        self.associations = None
        self.proof_login = "driver"
        self.proof_association = "MEMBER"
        self.failure = False
        self.moved = False
        self.pull_reads = 0
        self.writes = []
        self.queued = False
        self.files = [[{"filename": "lu_machinery/example.py"}]]
        self.compare_rows = {
            "base_commit": {"sha": "d" * 40},
            "merge_base_commit": {"sha": "d" * 40},
            "status": "ahead",
            "files": [{"filename": "lu_machinery/earlier-pr.py"}, {"filename": "lu_machinery/this-pr.py"}],
        }
        self.candidate_record = {
            "base_sha": "d" * 40,
            "head_sha": _recovery_TESTED,
            "head_ref": "refs/heads/gh-readonly-queue/main/pr-42-" + "e" * 40,
            "run_id": 123,
            "run_attempt": 1,
        }

    def __call__(self, argv, **kwargs):
        if argv[1:3] == ["pr", "view"]:
            assert "isInMergeQueue" not in argv[argv.index("--json") + 1].split(",")
            data = {
                "number": 42,
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "headRefOid": self.head,
                "headRefName": "codex/fixture",
                "state": "OPEN",
                "isCrossRepository": False,
                "baseRefName": "main",
            }
        elif argv[1:3] == ["pr", "checks"]:
            data = []
        elif argv[1:5] == ["api", "--method", "POST", "graphql"]:
            payload = json.loads(Path(argv[6]).read_text())
            query = payload["query"]
            if query.startswith("mutation"):
                assert "enqueuePullRequest" in query
                self.writes.append(argv)
                if self.failure:
                    raise subprocess.TimeoutExpired(argv, 60)
                self.queued = True
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}), ""
                )
            if "mergeQueueEntry" in query:
                data = {
                    "number": 42,
                    "state": "OPEN",
                    "headRefOid": self.head,
                    "headRefName": "codex/fixture",
                    "isInMergeQueue": self.queued,
                    "mergeQueueEntry": {"headCommit": {"oid": _recovery_TESTED}},
                }
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"data": {"repository": {"pullRequest": data}}}), ""
                )
            if "timelineItems" in query:
                self.removal_reads.append(payload)
                if self.removal_failure:
                    return subprocess.CompletedProcess(argv, 1, "", "lookup failed")
                events = (
                    self.removal_history
                    if self.removal_history is not None
                    else [
                        {
                            "beforeCommit": {
                                "oid": self.removal_head,
                                "committedDate": "2099-01-01T00:00:00Z"
                                if self.future_commit
                                else "2026-10-09T10:00:00Z",
                            }
                            if self.removal_head
                            else None,
                            "createdAt": self.removal_date,
                            "reason": self.removal_reason,
                        }
                    ]
                    if self.removed
                    else []
                )
                if "first:100" in query:
                    start = int(payload["variables"].get("cursor") or 0)
                    nodes = events[start : start + self.removal_page_size]
                    end = start + len(nodes)
                    removals = {
                        "nodes": nodes,
                        "totalCount": self.timeline_count if self.timeline_count is not None else len(events),
                        "pageInfo": {"hasNextPage": end < len(events), "endCursor": str(end) if nodes else None},
                    }
                else:
                    removals = {"nodes": events[-1:]}
                pull = {"headRefOid": self.head, "pushes": {"nodes": self.pushes}, "removals": removals}
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"data": {"repository": {"pullRequest": pull}}}), ""
                )
            data = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": self.head,
                            "isMergeQueueEnabled": True,
                            "id": "PR_fixture_42",
                            "viewerMergeHeadlineText": "change (#42)",
                            "viewerMergeBodyText": "change",
                        }
                    }
                }
            }
        elif argv[1] == "api":
            endpoint = next(arg for arg in argv if arg.startswith("repos/") or arg.split("?", 1)[0] == "user").split(
                "?", 1
            )[0]
            if endpoint.endswith("/comments"):
                approval = {
                    "body": _recovery_build_comment(
                        sha=self.head,
                        task_id="review",
                        started="2026-10-09T10:00:00.000001+00:00",
                        verdict="APPROVED" if self.approved else "CHANGES_REQUESTED",
                        model="claude-opus-5-5",
                        family="anthropic",
                        reply="VERDICT: APPROVE",
                    ),
                    "user": {"login": "driver"},
                    "author_association": "MEMBER",
                    "created_at": "2026-10-09T10:00:01Z",
                    "updated_at": "2026-10-09T10:00:01Z",
                }
                comments = [approval]
                if self.proof is not None:
                    comments.append(
                        {
                            "id": 789,
                            "body": "<!-- ci-recovery-evidence " + json.dumps(self.proof) + " -->",
                            "user": {"login": self.proof_login},
                            "author_association": self.proof_association,
                        }
                    )
                data = [comments]
            elif endpoint.endswith("/files"):
                data = self.files
            elif endpoint.endswith("/timeline"):
                data = (
                    [[{"id": 987, "event": "removed_from_merge_queue", "created_at": "2026-10-09T11:00:00Z"}]]
                    if self.removed
                    else [[]]
                )
                if self.future_commit:
                    data[0].append(
                        {"event": "committed", "sha": self.head, "committer": {"date": "2099-01-01T00:00:00Z"}}
                    )
            elif endpoint.endswith("/jobs"):
                data = [{"jobs": [{"id": job, "conclusion": "failure"} for job in self.failed_ids]}]
            elif endpoint.endswith("/runs/123"):
                data = {
                    "id": 123,
                    "status": self.status,
                    "conclusion": self.conclusion,
                    "head_sha": _recovery_TESTED,
                    "event": self.event,
                    "head_branch": "gh-readonly-queue/main/pr-42-" + "e" * 40,
                    "run_attempt": self.attempt,
                    "pull_requests": self.associations
                    if self.associations is not None
                    else []
                    if self.event == "merge_group"
                    else [{"number": 42, "head": {"sha": self.head}}],
                }
            elif endpoint.endswith("/actions/runs/123/artifacts"):
                data = {
                    "total_count": 1,
                    "artifacts": [{"name": "ci-queue-candidate", "id": 77, "expired": False, "size_in_bytes": 1024}],
                }
            elif endpoint.endswith("/actions/artifacts/77/zip"):
                with io.BytesIO() as buffer:
                    with zipfile.ZipFile(buffer, "w") as archive:
                        archive.writestr("queue-candidate.json", json.dumps(self.candidate_record))
                    kwargs["stdout"].write(buffer.getvalue())
                return subprocess.CompletedProcess(argv, 0, "", "")
            elif "/compare/" in endpoint:
                data = self.compare_rows
            elif endpoint.endswith("/pulls/42"):
                self.pull_reads += 1
                data = {
                    "state": "open",
                    "head": {"sha": _recovery_NEW_HEAD if self.moved and self.pull_reads > 1 else self.head},
                }
            elif endpoint == "user":
                data = {"login": "driver"}
            else:
                pytest.fail(f"unexpected read {endpoint}")
        else:
            self.writes.append(argv)
            if self.failure:
                raise subprocess.TimeoutExpired(argv, 60)
            if argv[1:3] == ["pr", "merge"]:
                self.queued = True
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")


@pytest.fixture()
def catalog(monkeypatch):
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.delenv("AGENT_NO_MERGE", raising=False)


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("args,read", [
    (["api", "repos/unit/public/issues/1", "-X", "POST"], False),
    (["api", "repos/unit/public/issues/1", "--method=GET", "-X=POST"], False),
    (["api", "repos/unit/public/issues/1", "-XGET"], True),
    (["api", "repos/unit/public/issues/1", "--field=x=y"], False),
    (["api", "repos/unit/public/issues/1", "--raw-field=x=y"], False),
    (["api", "repos/unit/public/issues/1", "--input=-"], False),
    (["api", "repos/unit/public/issues/1", "-f", "x=y"], False),
    (["api", "repos/unit/public/issues/1", "-F", "x=y"], False),
    (["api", "graphql", "-f", "query=query{x}"], True),
    (["api", "repos/unit/public/unknown/endpoint"], True),
    (["api", "https://other.invalid/repos/unit/public/issues/1"], False),
    (["release", "delete-asset", "v1", "fixture-asset"], False),
    (["release", "delete-asset", "v1", "fixture-asset", "-y"], False),
    (["repo", "set-default", "unit/public"], False),
    (["repo", "set-default", "--unset"], False),
    (["repo", "set-default", "--view", "--view=false", "unit/public"], False),
    (["repo", "set-default", "--view"], True),
    (["release", "verify-asset", "v1", "fixture-asset"], True),
    (["release", "view", "delete-asset"], True),
    (["repo", "view", "set-default"], True),
    (["repo", "read-file", "delete-asset"], True),
    (["repo", "read-dir", "set-default"], True),
    (["pr", "list", "--search", "delete-asset set-default update-branch"], True),
    (["pr", "list", "--limit", "invalid"], True),
    (["run", "watch", "1", "--compact"], True),
])
def test_native_reads_preserve_semantics_and_public_writes_remain_refused(args, read, tmp_path):
    """Retired read inventories must not refuse GET/query forms or admit public writes/uploads."""
    calls = []

    def transport(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 7, "native output", "native diagnostic")

    if read:
        result = gate.checked_run(["gh", *args], cwd=tmp_path, env={}, runner=transport)
        assert calls == [["gh", *args]]
        assert (result.returncode, result.stdout, result.stderr) == (7, "native output", "native diagnostic")
    else:
        with pytest.raises(gate.PublishBlocked):
            gate.checked_run(["gh", *args], cwd=tmp_path, env={}, runner=transport)
        assert calls == []


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("flag", ["-t", "-t=true", "-t=false", "-t=1", "-ttrue", "-t1", "-at", "--show-token", "--show-token=true", "--show-token=false", "--show-token=1"])
def test_auth_token_flag_spellings_refused(flag, tmp_path):
    for args in (["auth", "status", flag], ["auth", "status", flag, "--active"], ["-R", "unit/private", "auth", "status", flag]):
        with pytest.raises(gate.PublishBlocked):
            admit(args, cwd=tmp_path, environment={})


@pytest.mark.usefixtures("catalog", "configured_roots", "synthetic_opsec")
def test_issue_create_epic_admits_only_an_explicit_cto_root(monkeypatch, capsys):
    for name in ("GH_HOST", "GH_REPO", "SESSION_EPIC", "LEARN_UKRAINIAN_DISPATCH_TASK_ID", pub.BROKER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SESSION_SEAT", "cto")
    title = "[EPIC][new-stream] Coordination"
    body = "New stream root"
    sent = []

    def send(argv, **_kwargs):
        body_file = Path(argv[argv.index("--body-file") + 1])
        sent.append((argv[:5], body_file.read_text(), argv[5]))
        return subprocess.CompletedProcess(argv, 0, "https://github.com/unit/public/issues/23", "")

    for repo, issue_title, flags, diagnostic in (
        ("unit/public", title, [], "issue-create requires --parent"),
        ("unit/public", title, ["--no-epic"], "issue-create requires --parent"),
        ("unit/public", "Ordinary issue", ["--epic"], "EPIC_REFUSED"),
        ("unit/public", "[EPIC][x] \nTitle", ["--epic"], "EPIC_REFUSED"),
        ("unit/public", title, ["--epic", "--parent", "14"], "EPIC_REFUSED"),
        ("unit/private", title, ["--epic"], "EPIC_REFUSED"),
        ("other.example/unit/public", title, ["--epic"], "REPO_REFUSED"),
        ("other.example/unit/private", title, [], "REPO_REFUSED"),
        ("outside/unknown", title, ["--epic"], "REPO_REFUSED"),
    ):
        args = ["issue-create", "--repo", repo, "--title", issue_title, "--body", body, *flags]
        assert pub.main(args, runner=send) == 2
        assert diagnostic in capsys.readouterr().err

    args = ["issue-create", "--repo", "unit/public", "--title", title, "--body", body, "--epic"]
    with monkeypatch.context() as worker:
        worker.setenv("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "test-worker")
        assert pub.main(args, runner=send) == 2
        assert "requires the CTO seat" in capsys.readouterr().err

    assert sent == []
    assert pub.main(args, runner=send) == 0
    assert sent == [(["gh", "issue", "create", "--repo", "unit/public"], body, f"--title={title}")]


@pytest.mark.usefixtures("configured_roots", "synthetic_opsec")
def test_issue_create_links_sibling_to_default_registry_epic(tmp_path, monkeypatch, capsys):
    """Catch publisher rejection of valid parents when a fleet repository has no local stream roots."""
    from lu_machinery.orchestration import issue_stream_audit

    registry = tmp_path / "issue_streams.yaml"
    registry.write_text(
        "streams:\n  epics:\n    epics: [42, 43]\n    closed_epics: [43]\n"
        "repos:\n  bootstrap:\n    streams:\n      epics:\n        epics: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(issue_stream_audit, "REGISTRY_PATH", registry)
    monkeypatch.setattr(gate, "catalog", lambda: {
        **CATALOG,
        "sibling": {"github": "unit/sibling", "role": "public-content"},
        "bootstrap": {"github": "unit/bootstrap", "role": "public-content"},
        "other-owner": {"github": "other/sibling", "role": "public-content"},
    })
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv(pub.BROKER_ENV, raising=False)

    calls = []

    def send(argv, **_kwargs):
        if argv[1:4] == ["api", "--method", "GET"]:
            path = argv[4].split("?", 1)[0]
            assert path in {f"repos/unit/{name}/issues/42" for name in ("public", "sibling", "bootstrap")}
            issue_repo = path.removeprefix("repos/").removesuffix("/issues/42")
            calls.append(("read", issue_repo))
            body = {
                "number": 42,
                "state": "open",
                "node_id": f"NODE_{issue_repo.replace('/', '_')}",
                "html_url": f"https://github.com/{issue_repo}/issues/42",
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")
        if argv[1:3] == ["issue", "create"]:
            child_repo = argv[argv.index("--repo") + 1]
            calls.append(("create", child_repo))
            return subprocess.CompletedProcess(argv, 0, f"https://github.com/{child_repo}/issues/42\n", "")
        assert argv[1:5] == ["api", "--method", "POST", "graphql"]
        payload = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
        assert "addSubIssue" in payload["query"]
        calls.append(("link", payload["variables"]))
        return subprocess.CompletedProcess(argv, 0, '{"data":{"addSubIssue":{"issue":{"number":42}}}}', "")

    for repo in ("unit/sibling", "unit/bootstrap"):
        calls.clear()
        args = ["issue-create", "--repo", repo, "--title", "Task", "--body", "Body"]
        for refused in ("43", "99"):
            assert pub.main([*args, "--parent", refused], runner=send) == 2
            assert "PARENT_REFUSED" in (diagnostic := capsys.readouterr().err)
            assert "unit/public" in diagnostic
        assert calls == []

        assert pub.main([*args, "--parent", "42"], runner=send) == 0
        assert capsys.readouterr().out.strip() == f"https://github.com/{repo}/issues/42"
        assert calls == [
            ("read", "unit/public"),
            ("create", repo),
            ("read", repo),
            ("link", {"p": "NODE_unit_public", "c": f"NODE_{repo.replace('/', '_')}"}),
        ]

    for ineligible in ("other/sibling", "unit/private"):
        calls.clear()
        assert pub.main(
            ["issue-create", "--repo", ineligible, "--title", "Task", "--body", "Body", "--parent", "42"],
            runner=send,
        ) == 2
        assert "PARENT_REFUSED" in capsys.readouterr().err
        assert calls == []


@pytest.mark.usefixtures("configured_roots", "synthetic_opsec")
@pytest.mark.parametrize(
    ("active_slug", "inactive_slug"),
    [
        ("learn-ukrainian/machinery", "agentic-machinery/machinery"),
        ("agentic-machinery/machinery", "learn-ukrainian/machinery"),
    ],
    ids=["pre-flip", "post-flip"],
)
def test_a02_issue_create_policy_and_parent_both_selector_states_and_same_issue_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    active_slug: str,
    inactive_slug: str,
) -> None:
    """Defect class: an issue in a moved repo resolves to the other repo registry;
    the same number in two repos is treated as one issue."""
    import yaml

    from lu_machinery.common import git_credentials
    from lu_machinery.orchestration import fleet_repos, issue_stream_audit

    for name in ("GH_HOST", "GH_REPO", "SESSION_EPIC", "LEARN_UKRAINIAN_DISPATCH_TASK_ID", pub.BROKER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SESSION_SEAT", "cto")

    public_slug = "learn-ukrainian/learn-ukrainian.github.io"
    sel_dir = tmp_path / "selector"
    sel_dir.mkdir(mode=0o700)
    sel_file = sel_dir / "selector.yaml"
    sel_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "active_slugs": {
                    "machinery": active_slug,
                    "infra-private": "learn-ukrainian/learn-ukrainian-infra-private",
                },
            }
        ),
        encoding="utf-8",
    )
    sel_file.chmod(0o400)
    sel_dir.chmod(0o500)
    try:
        monkeypatch.setattr(git_credentials, "_is_readonly_mount", lambda _fd: True)
        monkeypatch.setenv(fleet_repos.SELECTOR_ENV, str(sel_file))

        # A declared, empty section permits CTO --epic and still requires --parent on ordinary issues.
        empty_streams = tmp_path / "issue_streams_empty.yaml"
        empty_streams.write_text(
            "schema_version: 1\n"
            "repos:\n"
            "  machinery:\n"
            "    streams:\n"
            "      epics:\n"
            "        title: Machinery\n"
            "        epics: []\n"
            "streams:\n"
            "  epics:\n"
            "    title: Public\n"
            "    epics: [10, 99]\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(issue_stream_audit, "REGISTRY_PATH", empty_streams)

        writes = []

        def send_bootstrap(argv, **_kwargs):
            writes.append(argv)
            return subprocess.CompletedProcess(argv, 0, f"https://github.com/{active_slug}/issues/1216\n", "")

        assert pub.main(
            ["issue-create", "--repo", active_slug, "--title", "Task", "--body", "Body"],
            runner=send_bootstrap,
        ) == 2
        diagnostic = capsys.readouterr().err
        assert "PARENT_" in diagnostic
        if active_slug.split("/", 1)[0] == public_slug.split("/", 1)[0]:
            assert "PARENT_REQUIRED" in diagnostic
            assert public_slug in diagnostic
        assert writes == []

        assert (
            pub.main(
                [
                    "issue-create",
                    "--repo",
                    active_slug,
                    "--title",
                    "[EPIC][epics] Machinery root",
                    "--body",
                    "Root",
                    "--epic",
                ],
                runner=send_bootstrap,
            )
            == 0
        )
        assert writes == [
            [
                "gh",
                "issue",
                "create",
                "--repo",
                active_slug,
                "--title=[EPIC][epics] Machinery root",
                "--body-file",
                writes[0][writes[0].index("--body-file") + 1],
            ]
        ]

        # Inactive slug and missing selector are refused before any write.
        writes.clear()
        assert (
            pub.main(
                [
                    "issue-create",
                    "--repo",
                    inactive_slug,
                    "--title",
                    "[EPIC][epics] Machinery root",
                    "--body",
                    "Root",
                    "--epic",
                ],
                runner=send_bootstrap,
            )
            == 2
        )
        assert "REPO_REFUSED" in capsys.readouterr().err
        assert writes == []

        # Post-PR-2: #10 exists in BOTH repos, #99 only in public, #20 only in machinery.
        populated_streams = tmp_path / "issue_streams_populated.yaml"
        populated_streams.write_text(
            "schema_version: 1\n"
            "repos:\n"
            "  machinery:\n"
            "    streams:\n"
            "      epics:\n"
            "        title: Machinery\n"
            "        epics: [10, 20]\n"
            "streams:\n"
            "  epics:\n"
            "    title: Public\n"
            "    epics: [10, 99]\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(issue_stream_audit, "REGISTRY_PATH", populated_streams)

        read_endpoints = []
        link_payloads = []
        created_repos = []

        def transport(argv, **_kwargs):
            if argv[1:4] == ["api", "--method", "GET"]:
                endpoint = argv[4].split("?", 1)[0]
                read_endpoints.append(endpoint)
                if endpoint == f"repos/{active_slug}/issues/10":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(
                            {
                                "number": 10,
                                "state": "open",
                                "node_id": "M_ROOT_10",
                                "repository_url": f"https://api.github.com/repos/{active_slug}",
                            }
                        ),
                        "",
                    )
                if endpoint == f"repos/{active_slug}/issues/500":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(
                            {
                                "number": 500,
                                "state": "open",
                                "node_id": "M_CHILD_500",
                                "repository_url": f"https://api.github.com/repos/{active_slug}",
                            }
                        ),
                        "",
                    )
                if endpoint == f"repos/{public_slug}/issues/10":
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        json.dumps(
                            {
                                "number": 10,
                                "state": "open",
                                "node_id": "P_ROOT_10",
                                "repository_url": f"https://api.github.com/repos/{public_slug}",
                            }
                        ),
                        "",
                    )
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            if argv[1:3] == ["issue", "create"]:
                created_repos.append(argv[argv.index("--repo") + 1])
                return subprocess.CompletedProcess(argv, 0, f"https://github.com/{active_slug}/issues/500\n", "")
            if argv[1:5] == ["api", "--method", "POST", "graphql"]:
                payload = json.loads(Path(argv[argv.index("--input") + 1]).read_text())
                link_payloads.append(payload)
                return subprocess.CompletedProcess(argv, 0, "{}", "")
            raise AssertionError(f"unexpected transport call: {argv}")

        # Public-only root #99 is refused in machinery; machinery-only root #20 is refused in public.
        assert (
            pub.main(
                ["issue-create", "--repo", active_slug, "--parent", "99", "--title", "Task", "--body", "Body"],
                runner=transport,
            )
            == 2
        )
        assert "PARENT_REFUSED" in capsys.readouterr().err

        # During a transfer window, a parent payload naming the row's other baked slug still binds,
        # while a payload naming a different fleet repository row is refused.
        def transfer_window_read(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "number": 10,
                        "state": "open",
                        "node_id": "M_ROOT_10_ALT",
                        "repository_url": f"https://api.github.com/repos/{inactive_slug}",
                        "html_url": f"https://github.com/{inactive_slug}/issues/10",
                    }
                ),
                "",
            )

        repos = pub._catalog_fleet_repos()
        active = pub._fleet_catalog_row(active_slug, repos=repos)
        assert pub._parent_epic_node(transfer_window_read, active, repos, 10, tmp_path, None) == (
            active_slug, "M_ROOT_10_ALT",
        )

        def foreign_repo_read(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "number": 10,
                        "state": "open",
                        "node_id": "P_ROOT_10",
                        "repository_url": f"https://api.github.com/repos/{public_slug}",
                        "html_url": f"https://github.com/{public_slug}/issues/10",
                    }
                ),
                "",
            )

        with pytest.raises(gate.PublishBlocked, match="PARENT_REFUSED"):
            pub._parent_epic_node(foreign_repo_read, active, repos, 10, tmp_path, None)
        assert (
            pub.main(
                ["issue-create", "--repo", public_slug, "--parent", "20", "--title", "Task", "--body", "Body"],
                runner=transport,
            )
            == 2
        )
        assert "PARENT_REFUSED" in capsys.readouterr().err
        assert created_repos == []

        # Same root number #10 in machinery resolves machinery's own node ID (M_ROOT_10), never public's.
        assert (
            pub.main(
                ["issue-create", "--repo", active_slug, "--parent", "10", "--title", "Task", "--body", "Body"],
                runner=transport,
            )
            == 0
        )
        assert created_repos == [active_slug]
        assert f"repos/{active_slug}/issues/10" in read_endpoints
        assert f"repos/{public_slug}/issues/10" not in read_endpoints
        assert link_payloads[-1]["variables"] == {"p": "M_ROOT_10", "c": "M_CHILD_500"}

        # Missing selector fails closed on issue-create with REPO_REFUSED.
        monkeypatch.delenv(fleet_repos.SELECTOR_ENV, raising=False)
        assert (
            pub.main(
                ["issue-create", "--repo", active_slug, "--parent", "10", "--title", "Task", "--body", "Body"],
                runner=transport,
            )
            == 2
        )
        assert "REPO_REFUSED" in capsys.readouterr().err

        # Malformed alternate_github (and non-github.com hosts) fail closed (F2 + CodeQL host partition).
        for bad_alt in (
            123,
            "agentic-machinery/machïnery",
            ["agentic-machinery/machinery"],
            "",
            "https://github.com.evil.com/agentic-machinery/machinery",
            "https://gitlab.com/agentic-machinery/machinery",
        ):
            bad_catalog = {
                "public": {"github": public_slug, "default": True, "local_name": "p", "role": "public-monorepo"},
                "machinery": {
                    "github": "learn-ukrainian/machinery",
                    "alternate_github": bad_alt,
                    "repository_id": 1445647449,
                    "visibility": "public",
                    "local_name": "m",
                    "role": "public-machinery",
                },
            }
            with monkeypatch.context() as ctx:
                ctx.setattr(gate, "catalog", lambda rows=bad_catalog: rows)
                with pytest.raises(gate.PublishBlocked, match="allowlist malformed"):
                    pub._fleet_catalog_row("learn-ukrainian/machinery", env={})

        # Foreign-host repository with matching slug is never treated as a registered GitHub fleet repo (F1).
        assert pub._fleet_catalog_row(f"https://gitlab.com/{active_slug}", env={}) is None
        assert pub._fleet_catalog_row(f"https://github.com.evil.com/{active_slug}", env={}) is None
        host_writes = []
        with pytest.raises(gate.PublishBlocked, match="REPO_REFUSED"):
            pub.publish(
                "issue-create",
                repo=f"other.example/{active_slug}",
                title="Task",
                body="Body",
                runner=lambda argv, **_kw: host_writes.append(argv),
            )
        assert host_writes == []

        # Missing, repos-only, empty, or streams:{} registry refuses default-repo issue-create (with or without --epic) (F2).
        missing_reg = tmp_path / "does_not_exist.yaml"
        repos_only_reg = tmp_path / "repos_only.yaml"
        repos_only_reg.write_text(
            "schema_version: 1\nrepos:\n  machinery:\n    streams:\n      epics:\n        epics: []\n",
            encoding="utf-8",
        )
        empty_reg = tmp_path / "empty_doc.yaml"
        empty_reg.write_text("schema_version: 1\n", encoding="utf-8")
        empty_streams_reg = tmp_path / "empty_streams_doc.yaml"
        empty_streams_reg.write_text("schema_version: 1\nstreams: {}\n", encoding="utf-8")
        for bad_reg_path in (missing_reg, repos_only_reg, empty_reg, empty_streams_reg):
            with monkeypatch.context() as ctx:
                ctx.setattr(issue_stream_audit, "REGISTRY_PATH", bad_reg_path)
                for cli_args in (
                    [
                        "issue-create",
                        "--repo",
                        public_slug,
                        "--title",
                        "[EPIC][epics] Default root",
                        "--body",
                        "Root",
                        "--epic",
                    ],
                    [
                        "issue-create",
                        "--repo",
                        public_slug,
                        "--title",
                        "Task without parent",
                        "--body",
                        "Body",
                    ],
                ):
                    assert (
                        pub.main(
                            cli_args,
                            runner=lambda argv, **_kw: host_writes.append(argv),
                        )
                        == 2
                    )
                    assert "REGISTRY_REFUSED" in capsys.readouterr().err
        assert host_writes == []

        # Malformed or mismatched parent issue lookup responses fail closed with PARENT_REFUSED (F4).
        for bad_issue_json in (
            json.dumps([]),
            json.dumps({"number": 10, "state": "open", "node_id": "   ", "repository_url": f"https://api.github.com/repos/{active_slug}"}),
            json.dumps({"number": 99, "state": "open", "node_id": "WRONG_NUM", "repository_url": f"https://api.github.com/repos/{active_slug}"}),
            json.dumps({"number": 10, "state": "open", "node_id": "WRONG_REPO", "repository_url": f"https://api.github.com/repos/{public_slug}"}),
        ):
            def _bad_parent_runner(argv, *, payload=bad_issue_json, **_kw):
                if argv[1:4] == ["api", "--method", "GET"]:
                    return subprocess.CompletedProcess(argv, 0, payload, "")
                raise AssertionError(f"unexpected write call: {argv}")

            with monkeypatch.context() as ctx:
                ctx.setenv(fleet_repos.SELECTOR_ENV, str(sel_file))
                assert (
                    pub.main(
                        ["issue-create", "--repo", active_slug, "--parent", "10", "--title", "Task", "--body", "Body"],
                        runner=_bad_parent_runner,
                    )
                    == 2
                )
                assert "PARENT_REFUSED" in capsys.readouterr().err
    finally:
        sel_dir.chmod(0o700)
        sel_file.chmod(0o600)


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("repo", ["unit/public", "unit/private"])
@pytest.mark.parametrize("state", ["draft", "red", "pending", "unknown", "ready"])
def test_merge_readiness_applies_to_public_and_private(synthetic_opsec, repo, state):
    writes = []
    reads = []
    enqueue_payloads = []
    queue_proofs = []
    def spy(args, **kwargs):
        if args[1:3] == ["pr", "view"] and "id,headRefOid,isCrossRepository,baseRefName,labels" in args:
            reads.append(args)
            body = {"id": "PR_fixture_1", "headRefOid": "a" * 40, "isCrossRepository": False, "baseRefName": "main", "labels": []}
        elif args[1:3] == ["pr", "view"]:
            reads.append(args)
            body = {"number": 1, "isDraft": state == "draft", "mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}
            if "headRefOid,statusCheckRollup" in args:
                body["statusCheckRollup"] = [{
                    "__typename": "CheckRun", "name": "CI Gate",
                    "status": "IN_PROGRESS" if state == "pending" else "COMPLETED",
                    "conclusion": {"red": "FAILURE", "pending": "", "unknown": "UNKNOWN"}.get(state, "SUCCESS"),
                }]
        elif args[1:3] == ["pr", "checks"]:
            reads.append(args)
            body = [{"name": "CI Gate", "bucket": {"red": "fail", "pending": "pending", "unknown": "unrecognised"}.get(state, "pass")}]
        elif args[1:5] == ["api", "--method", "POST", "graphql"]:
            document = json.loads(Path(args[args.index("--input") + 1]).read_text())
            if document["query"].startswith("mutation"):
                writes.append(args)
                enqueue_payloads.append(document)
                return subprocess.CompletedProcess(args, 0, "{}", "")
            if document["query"] == pub.QUEUE_ENTRY_QUERY:
                # pr-merge proves the reviewed head's queue entry after the enqueue (machinery #281).
                queue_proofs.append(args)
                body = {"data": {"repository": {"pullRequest": {
                    "headRefOid": "a" * 40, "isInMergeQueue": True,
                    "mergeQueueEntry": {"headCommit": {"oid": "a" * 40}}, "autoMergeRequest": None}}}}
                return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
            reads.append(args)
            if document["query"] == pub.GQL_READS["queue-status"]:
                body = {"data": {"repository": {"pullRequest": {"headRefOid": "a" * 40, "isInMergeQueue": False}}}}
                return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
            body = {"data": {"repository": {"pullRequest": {
                "id": "PR_fixture_1", "headRefOid": "a" * 40, "isMergeQueueEnabled": True,
                "viewerMergeHeadlineText": "clean (#1)", "viewerMergeBodyText": "clean"}}}}
        elif args[1:4] == ["api", "--method", "GET"]:
            reads.append(args)
            if "/pulls/1/files" in args[4]:
                body = [[{"filename": "lu_machinery/example.py"}]]
            else:
                body = [[recorded_approval("a" * 40)]] if "/issues/1/comments" in args[4] else {"login": "unit"}
        else:
            writes.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
    if state == "ready":
        assert pub.publish("pr-merge", repo=repo, number=1, runner=spy, env={}).returncode == 0
        assert len(writes) == 1
        assert enqueue_payloads[0]["variables"] == {"id": "PR_fixture_1", "head": "a" * 40}
        assert "enqueuePullRequest" in enqueue_payloads[0]["query"]
    else:
        with pytest.raises(gate.PublishBlocked):
            pub.publish("pr-merge", repo=repo, number=1, runner=spy, env={})
        assert writes == []
    # Read the head-bound checks; ready merges also read the default squash text
    # the recorded review seats (#12020), here an exact-head approval (#12031), and the PR's files (#12181);
    # on a queue-enabled base they also read the head's queue identity and membership (machinery #325).
    assert len(reads) == (9 if state == "ready" else 1 if state == "draft" else 3)
    assert len(queue_proofs) == (1 if state == "ready" else 0)


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_review_contract_change_without_canary_evidence_can_merge(synthetic_opsec):
    # Contract canaries run at release cut; an approved green contract PR may merge.
    writes = []
    def spy(args, **kwargs):
        if args[1:3] == ["pr", "view"]:
            body = {"number": 1, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": "a" * 40, "statusCheckRollup": [
                {"__typename": "CheckRun", "name": "CI Gate", "status": "COMPLETED", "conclusion": "SUCCESS"}]}
        elif args[1:3] == ["pr", "checks"]:
            body = [{"name": "CI Gate", "bucket": "pass"}]
        elif args[1:5] == ["api", "--method", "POST", "graphql"]:
            body = {"data": {"repository": {"pullRequest": {
                "id": "PR_fixture_1", "headRefOid": "a" * 40, "isMergeQueueEnabled": False,
                "viewerMergeHeadlineText": "clean (#1)", "viewerMergeBodyText": "clean"}}}}
        elif args[1:4] == ["api", "--method", "GET"]:
            if "/pulls/1/files" in args[4]:
                body = [[{"filename": "lu_machinery/dispatcher/providers/codex.py"}]]
            else:
                body = [[recorded_approval("a" * 40)]] if "/issues/1/comments" in args[4] else {"login": "unit"}
        else:
            writes.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")
    assert pub.publish("pr-merge", repo="unit/public", number=1, runner=spy, env={}).returncode == 0
    assert len(writes) == 1


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_worker_merge_guard_and_changed_head_refuse_before_write():
    with pytest.raises(gate.PublishBlocked, match="AGENT_NO_MERGE"):
        pub.publish("pr-merge", repo="unit/private", number=1, env={"AGENT_NO_MERGE": "1"}, runner=lambda *a, **k: pytest.fail("send"))


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("verb,fields,expected", [
    ("pr-update-branch", {"number": 1}, ["pr", "update-branch", "1"]),
    ("pr-ready", {"number": 1}, ["pr", "ready", "1"]),
    ("pr-close", {"number": 1}, ["pr", "close", "1"]),
    ("issue-reopen", {"number": 1}, ["issue", "reopen", "1"]),
    ("run-rerun", {"number": 123}, ["run", "rerun", "123"]),
    ("run-cancel", {"number": 123}, ["run", "cancel", "123"]),
    ("workflow-run", {"workflow": "ci.yml", "ref": "unit", "inputs": {}}, ["workflow", "run", "ci.yml"]),
])
def test_no_text_driver_operations(verb, fields, expected, monkeypatch, tmp_path, synthetic_opsec, github_transport):
    calls = []
    def spy(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    sender = spy
    expected_requests = {
        "pr-update-branch": [("PUT", "repos/unit/public/pulls/1/update-branch", {})],
        "pr-close": [("PATCH", "repos/unit/public/pulls/1", {"state": "closed"})],
        "issue-reopen": [("PATCH", "repos/unit/public/issues/1", {"state": "open"})],
    }
    if verb in expected_requests:
        from lu_machinery.common.github_client import Response

        def transport(method, endpoint, headers, body, timeout):
            return Response(200, {}, b"{}")

        requests = github_transport(transport)
        original_transport = pub._run_transport

        def observe(command, **kwargs):
            calls.append(command)
            return original_transport(command, **kwargs)

        monkeypatch.setattr(pub, "_run_transport", observe)
        sender = None
    if verb == "run-rerun":
        Transport = _recovery_Transport
        sender = Transport()
        calls = sender.writes
        monkeypatch.setattr(pub.recovery, "ledger_path", lambda cwd: tmp_path / "ci-recovery.sqlite3")
    pub.publish(verb, repo="unit/public", cwd=tmp_path, runner=sender, env={}, **fields)
    assert calls[0][1:4] == expected
    assert calls[0][calls[0].index("--repo") + 1] == "unit/public"
    if verb == "run-rerun":
        assert "--failed" in calls[0]
    with pytest.raises(gate.PublishBlocked):
        pub.publish(verb, repo="unit/public", body="arbitrary", runner=spy, **fields)
    assert len(calls) == 1
    if verb in expected_requests:
        assert [
            (method, endpoint, json.loads(body) if body is not None else None)
            for method, endpoint, _headers, body, _timeout in requests
        ] == expected_requests[verb]


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_run_force_cancel_allows_stuck_cancelled_run_and_posts_force_cancel(
    tmp_path, synthetic_opsec, github_transport
):
    """Defect class: a non-completed run with cancelled scan jobs and a queued always() job fails to POST force-cancel."""
    from lu_machinery.common.github_client import Response

    run_doc = {"id": 123, "status": "queued", "conclusion": None}
    jobs_page = {
        "total_count": 2,
        "jobs": [
            {"id": 1, "run_id": 123, "name": "Secret scan", "status": "completed", "conclusion": "cancelled"},
            {"id": 2, "run_id": 123, "name": "Fleet security gate", "status": "queued", "conclusion": None},
        ],
    }
    calls = []

    def spy(args, **kwargs):
        calls.append((args, kwargs["env"]))
        if args[1:4] == ["api", "--method", "GET"] and "/actions/runs/123/jobs" in args[4]:
            return subprocess.CompletedProcess(args, 0, json.dumps([jobs_page]), "")
        if args[1:4] == ["api", "--method", "GET"] and "/actions/runs/123" in args[4]:
            return subprocess.CompletedProcess(args, 0, json.dumps(run_doc), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    assert pub.publish("run-force-cancel", repo="unit/public", number=123, cwd=tmp_path, runner=spy, env={}).returncode == 0
    assert [argv for argv, _ in calls] == [
        ["gh", "api", "--method", "GET", "repos/unit/public/actions/runs/123?per_page=100"],
        ["gh", "api", "--method", "GET", "repos/unit/public/actions/runs/123/jobs?per_page=100", "--paginate", "--slurp"],
        ["gh", "api", "--method", "POST", "repos/unit/public/actions/runs/123/force-cancel"],
    ]
    assert all(env["GH_HOST"] == "github.com" for _, env in calls)
    assert pub.main(["run-force-cancel", "--repo", "unit/public", "--number", "123"], runner=spy) == 0

    def transport(method, endpoint, headers, body, timeout):
        if method == "GET" and endpoint.endswith("/actions/runs/123?per_page=100"):
            return Response(200, {}, json.dumps(run_doc).encode())
        if method == "GET" and endpoint.endswith("/actions/runs/123/jobs?per_page=100"):
            return Response(200, {}, json.dumps(jobs_page).encode())
        if method == "POST" and endpoint == "repos/unit/public/actions/runs/123/force-cancel":
            return Response(202, {}, b"")
        pytest.fail(f"unexpected request: {method} {endpoint}")

    requests = github_transport(transport)
    assert pub.publish("run-force-cancel", repo="unit/public", number=123, cwd=tmp_path, env={}).returncode == 0
    assert [(method, endpoint, body) for method, endpoint, _headers, body, _timeout in requests] == [
        ("GET", "repos/unit/public/actions/runs/123?per_page=100", None),
        ("GET", "repos/unit/public/actions/runs/123/jobs?per_page=100", None),
        ("POST", "repos/unit/public/actions/runs/123/force-cancel", None),
    ]


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize(
    "repo,host,run_doc,jobs_pages,expect_jobs_read",
    [
        ("unit/public", "github.com", {"id": 123, "status": "cancelling", "conclusion": None}, None, False),
        ("unit/public", "github.com", {"id": 123, "status": "in_progress", "conclusion": "cancelled"}, None, False),
        (
            "unit/public",
            "github.com",
            {"id": 123, "status": "queued", "conclusion": None},
            [
                {"total_count": 2, "jobs": [{"id": 1, "run_id": 123, "status": "queued", "conclusion": None}]},
                {"total_count": 2, "jobs": [{"id": 2, "run_id": 123, "status": "completed", "conclusion": "cancelled"}]},
            ],
            True,
        ),
        (
            "unit/public",
            "github.com",
            {"id": 123, "status": "waiting", "conclusion": None},
            [{"total_count": 1, "jobs": [{"id": 1, "run_id": 123, "status": "cancelling", "conclusion": None}]}],
            True,
        ),
        (
            "git.enterprise-example.test/unit/public",
            "git.enterprise-example.test",
            {"id": 123, "status": "cancelling", "conclusion": None},
            None,
            False,
        ),
    ],
)
def test_run_force_cancel_allows_cancelling_multipage_and_enterprise_host(
    repo, host, run_doc, jobs_pages, expect_jobs_read, tmp_path, synthetic_opsec
):
    """Defect class: cancelling/multipage/enterprise eligible runs are wrongly refused or lose --hostname."""
    calls = []

    def spy(args, **kwargs):
        calls.append((args, kwargs["env"]))
        if args[1:4] == ["api", "--method", "GET"] and "/actions/runs/123/jobs" in args[4]:
            assert jobs_pages is not None
            return subprocess.CompletedProcess(args, 0, json.dumps(jobs_pages), "")
        if args[1:4] == ["api", "--method", "GET"] and "/actions/runs/123" in args[4]:
            return subprocess.CompletedProcess(args, 0, json.dumps(run_doc), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    assert pub.publish("run-force-cancel", repo=repo, number=123, cwd=tmp_path, runner=spy, env={}).returncode == 0
    assert len(calls) == (3 if expect_jobs_read else 2)
    post_argv, post_env = calls[-1]
    expected_host_flag = [] if host == "github.com" else ["--hostname", host]
    assert post_argv == ["gh", "api", "--method", "POST", "repos/unit/public/actions/runs/123/force-cancel", *expected_host_flag]
    assert post_env["GH_HOST"] == host


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize(
    "run_doc,jobs_pages,match",
    [
        ({"id": 123, "status": "completed", "conclusion": "success"}, None, "already completed"),
        ({"id": 123, "status": "completed", "conclusion": "failure"}, None, "already completed"),
        ({"id": 123, "status": "completed", "conclusion": "cancelled"}, None, "already completed"),
        (
            {"id": 123, "status": "queued", "conclusion": None},
            [{"total_count": 2, "jobs": [
                {"id": 1, "run_id": 123, "status": "queued", "conclusion": None},
                {"id": 2, "run_id": 123, "status": "queued", "conclusion": None},
            ]}],
            "no prior cancellation evidence",
        ),
        (
            {"id": 123, "status": "in_progress", "conclusion": None},
            [{"total_count": 2, "jobs": [
                {"id": 1, "run_id": 123, "status": "completed", "conclusion": "success"},
                {"id": 2, "run_id": 123, "status": "in_progress", "conclusion": None},
            ]}],
            "no prior cancellation evidence",
        ),
        (
            {"id": 123, "status": "queued", "conclusion": None},
            [{"total_count": 2, "jobs": [
                {"id": 1, "run_id": 123, "status": "completed", "conclusion": "failure"},
                {"id": 2, "run_id": 123, "status": "queued", "conclusion": None},
            ]}],
            "no prior cancellation evidence",
        ),
        (
            {"id": 123, "status": "queued", "conclusion": None},
            [{"total_count": 0, "jobs": []}],
            "no prior cancellation evidence",
        ),
    ],
)
def test_run_force_cancel_refuses_completed_and_uncancelled_runs_before_post(
    run_doc, jobs_pages, match, tmp_path, synthetic_opsec
):
    """Defect class: a completed run or an active run with no prior cancel reaches force-cancel POST."""
    calls = []

    def spy(args, **kwargs):
        calls.append(args)
        if args[1:4] == ["api", "--method", "POST"]:
            pytest.fail("must not POST force-cancel")
        if "/actions/runs/123/jobs" in args[4]:
            assert jobs_pages is not None, "completed run must not read jobs"
            return subprocess.CompletedProcess(args, 0, json.dumps(jobs_pages), "")
        return subprocess.CompletedProcess(args, 0, json.dumps(run_doc), "")

    with pytest.raises(gate.PublishBlocked, match=f"FORCE_CANCEL_REFUSED:.*{match}"):
        pub.publish("run-force-cancel", repo="unit/public", number=123, cwd=tmp_path, runner=spy, env={})
    assert all(call[1:4] == ["api", "--method", "GET"] for call in calls)


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize(
    "run_reply,jobs_reply",
    [
        ((1, ""), None),
        ((0, "not-json"), None),
        ((0, json.dumps({"id": 999, "status": "cancelling", "conclusion": None})), None),
        ((0, json.dumps({"id": True, "status": "cancelling", "conclusion": None})), None),
        ((0, json.dumps({"id": 123, "status": "bogus", "conclusion": None})), None),
        ((0, json.dumps({"id": 123, "status": None, "conclusion": None})), None),
        ((0, json.dumps({"id": 123, "status": "queued", "conclusion": "success"})), None),
        ((0, json.dumps({"id": 123, "status": "queued", "conclusion": None})), (1, "")),
        ((0, json.dumps({"id": 123, "status": "queued", "conclusion": None})), (0, "not-json")),
        ((0, json.dumps({"id": 123, "status": "queued", "conclusion": None})), (0, json.dumps([]))),
        ((0, json.dumps({"id": 123, "status": "queued", "conclusion": None})), (0, json.dumps(["bad"]))),
        (
            (0, json.dumps({"id": 123, "status": "queued", "conclusion": None})),
            (0, json.dumps([{"total_count": 2, "jobs": [{"id": 1, "run_id": 123, "status": "completed", "conclusion": "cancelled"}]}])),
        ),
        (
            (0, json.dumps({"id": 123, "status": "queued", "conclusion": None})),
            (0, json.dumps([{"total_count": True, "jobs": [{"id": 1, "run_id": 123, "status": "completed", "conclusion": "cancelled"}]}])),
        ),
        (
            (0, json.dumps({"id": 123, "status": "queued", "conclusion": None})),
            (0, json.dumps([{"total_count": 1, "jobs": [{"id": 1, "run_id": 999, "status": "completed", "conclusion": "cancelled"}]}])),
        ),
        (
            (0, json.dumps({"id": 123, "status": "queued", "conclusion": None})),
            (0, json.dumps([{"total_count": 1, "jobs": [{"id": 1, "run_id": 123, "status": "bogus", "conclusion": "cancelled"}]}])),
        ),
        (
            (0, json.dumps({"id": 123, "status": "queued", "conclusion": None})),
            (0, json.dumps([{"total_count": 1, "jobs": [{"id": 1, "run_id": 123, "status": "completed", "conclusion": ""}]}])),
        ),
    ],
)
def test_run_force_cancel_fails_closed_on_unreadable_or_malformed_evidence(
    run_reply, jobs_reply, tmp_path, synthetic_opsec
):
    """Defect class: unreadable, mismatched, truncated or malformed run/jobs responses reach force-cancel POST."""
    def spy(args, **kwargs):
        if args[1:4] == ["api", "--method", "POST"]:
            pytest.fail("must not POST force-cancel")
        code, body = jobs_reply if "/actions/runs/123/jobs" in args[4] else run_reply
        return subprocess.CompletedProcess(args, code, body, "")

    with pytest.raises(gate.PublishBlocked, match="FORCE_CANCEL_REFUSED"):
        pub.publish("run-force-cancel", repo="unit/public", number=123, cwd=tmp_path, runner=spy, env={})


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_run_force_cancel_refuses_invalid_inputs_dot_repo_and_repeated_cli_flags(tmp_path, monkeypatch):
    """Defect class: invalid fields, path-traversal repo segments or repeated CLI flags reach transport."""
    def fail_send(*args, **kwargs):
        pytest.fail("must not reach transport")

    for bad_fields in (
        {"repo": "unit/public", "number": 123, "body": "arbitrary"},
        {"repo": "unit/public", "number": 0},
        {"repo": "unit/public", "number": -5},
        {"repo": "unit/public", "number": True},
        {"repo": "unit/..", "number": 123},
        {"repo": "./public", "number": 123},
    ):
        with pytest.raises(gate.PublishBlocked):
            pub.publish("run-force-cancel", cwd=tmp_path, runner=fail_send, env={}, **bad_fields)

    monkeypatch.setattr(pub, "repository", lambda *args: "unknown")
    with pytest.raises(gate.PublishBlocked, match="repository unresolved"):
        pub.publish("run-force-cancel", number=123, cwd=tmp_path, runner=fail_send, env={})

    for bad_cli in (
        ["run-force-cancel", "--repo", "unit/public", "--number", "1", "--number", "2"],
        ["run-force-cancel", "--repo", "unit/public", "--repo", "unit/private", "--number", "1"],
    ):
        with pytest.raises(SystemExit) as exc:
            pub.main(bad_cli, runner=fail_send)
        assert exc.value.code == 2


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("stage", ["run", "jobs"])
def test_run_force_cancel_propagates_rate_limit_without_posting(stage, tmp_path, synthetic_opsec):
    """Defect class: a rate-limited preflight lookup is masked as exit 2 or reaches force-cancel POST."""
    from lu_machinery.common import github_client

    def spy(args, **kwargs):
        if args[1:4] == ["api", "--method", "POST"]:
            pytest.fail("must not POST force-cancel")
        if "/actions/runs/123/jobs" in args[4]:
            result = subprocess.CompletedProcess(args, 75, json.dumps({"error": "github_rate_limited"}), "")
            result.github_result = github_client.Result(error="github_rate_limited", reason="secondary_hold")
            return result
        if stage == "run":
            result = subprocess.CompletedProcess(args, 75, json.dumps({"error": "github_rate_limited"}), "")
            result.github_result = github_client.Result(error="github_rate_limited", reason="secondary_hold")
            return result
        return subprocess.CompletedProcess(args, 0, json.dumps({"id": 123, "status": "queued", "conclusion": None}), "")

    answer = pub.publish("run-force-cancel", repo="unit/public", number=123, cwd=tmp_path, runner=spy, env={}, capture_output=True, text=True)
    assert answer.returncode == 75
    assert json.loads(answer.stdout)["error"] == "github_rate_limited"


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_run_force_cancel_and_run_cancel_help_document_contract(capsys):
    """Defect class: run-force-cancel is missing from --help or run-cancel still claims force cancellation is unavailable."""
    with pytest.raises(SystemExit) as exc:
        pub.main(["run-force-cancel", "--help"])
    assert exc.value.code == 0
    force_help = capsys.readouterr().out
    assert "force-cancel" in force_help and "run-cancel" in force_help and "75" in force_help

    with pytest.raises(SystemExit) as exc:
        pub.main(["run-cancel", "--help"])
    assert exc.value.code == 0
    cancel_help = capsys.readouterr().out
    assert "run-force-cancel" in cancel_help
    assert "Force cancellation is unavailable" not in cancel_help


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("fields", [
    {"workflow": "unknown.yml", "ref": "main"},
    {"workflow": "ci.yml", "ref": "main", "inputs": {"body": "anything"}},
    {"workflow": "ci.yml", "ref": "--flag"},
])
def test_workflow_closed_names_and_inputs(fields):
    with pytest.raises(gate.PublishBlocked):
        pub.publish("workflow-run", repo="unit/public", runner=lambda *a, **k: pytest.fail("send"), **fields)


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("workflow", ["ci.yml", "deploy-pages.yml", "stale-branch-cleanup.yml"])
@pytest.mark.parametrize("inputs", [None, {}])
@pytest.mark.parametrize("entrypoint", ["publisher", "cli"])
@pytest.mark.usefixtures("publisher_workflows")
def test_named_workflow_run_no_inputs(workflow, inputs, entrypoint, synthetic_opsec):
    calls = []

    def spy(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    if entrypoint == "cli":
        args = ["workflow-run", "--repo", "unit/public", "--workflow", workflow, "--ref", "main"]
        if inputs is not None:
            args += ["--inputs", json.dumps(inputs)]
        assert pub.main(args, runner=spy) == 0
    else:
        fields = {} if inputs is None else {"inputs": inputs}
        result = pub.publish(
            "workflow-run", repo="unit/public", workflow=workflow, ref="main", runner=spy, **fields
        )
        assert result.returncode == 0
    assert calls == [["gh", "workflow", "run", workflow, "--repo", "unit/public", "--ref=main"]]


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("workflow", ["ci.yml", "deploy-pages.yml", "stale-branch-cleanup.yml"])
@pytest.mark.parametrize("invalid", [
    {"inputs": {"force": "true"}},
    {"repo": "owner/repo?unexpected=true"},
    {"ref": "--flag"},
    {"ref": "main\nother"},
])
def test_workflow_invalid_fields_refuse_before_send(workflow, invalid):
    fields = {"repo": "unit/public", "workflow": workflow, "ref": "main", **invalid}

    def sender(*args, **kwargs):
        pytest.fail("send")

    with pytest.raises(gate.PublishBlocked):
        pub.publish("workflow-run", runner=sender, **fields)
    args = ["workflow-run"]
    for key, value in fields.items():
        args.append("--" + key + "=" + (json.dumps(value) if key == "inputs" else value))
    assert pub.main(args, runner=sender) == 2


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_unknown_workflow_cli_refuses_before_send():
    with pytest.raises(SystemExit) as error:
        pub.main(
            ["workflow-run", "--repo", "unit/public", "--workflow", "unknown.yml", "--ref", "main"],
            runner=lambda *a, **k: pytest.fail("send"),
        )
    assert error.value.code == 2


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_checkout_only_in_real_dispatch_directory(tmp_path):
    worktree = tmp_path / ".worktrees/dispatch/unit/task"
    worktree.mkdir(parents=True)
    def git_reader(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, str(worktree), "")
    assert not admit(["pr", "checkout", "1"], cwd=worktree, environment={}, reader=git_reader).write
    with pytest.raises(gate.PublishBlocked):
        admit(["pr", "checkout", "1"], cwd=worktree, environment={},
              reader=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, str(tmp_path), ""))
    with pytest.raises(gate.PublishBlocked):
        admit(["pr", "checkout", "1"], cwd=tmp_path, environment={})
    link = tmp_path / ".worktrees/dispatch/unit/link"
    link.symlink_to(worktree, target_is_directory=True)
    with pytest.raises(gate.PublishBlocked):
        admit(["pr", "checkout", "1"], cwd=link, environment={})


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_private_write_pins_repo_even_with_second_remote(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GH_"))}
    for args in (["init", "-q", str(tmp_path)], ["-C", str(tmp_path), "remote", "add", "origin", "https://github.com/unit/private.git"], ["-C", str(tmp_path), "remote", "add", "upstream", "https://github.com/unit/public.git"]):
        subprocess.run(["git", *args], env=env, check=True, timeout=10)
    frozen = admit(["issue", "comment", "1", "--body", "fixture"], cwd=tmp_path, environment=env)
    assert frozen.destination == "github.com/unit/private"
    assert frozen.argv[frozen.argv.index("--repo") + 1] == "github.com/unit/private"


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_invalid_asset_cli_name_is_masked_exit_two(capsys):
    with pytest.raises(SystemExit) as error:
        pub.main(["release-upload", "--repo", "unit/public", "--tag", "unit", "--assets", "invalid#name"])
    assert error.value.code == 2
    assert "invalid#name" not in capsys.readouterr().err


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_hook_recognises_typed_merge(monkeypatch):
    spec = importlib.util.spec_from_file_location("r5_guard", ROOT / "agents_extensions/shared/hooks/guard-pr-merge.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for prefix in (
        [sys.executable, "--check-hash-based-pycs", "always", "-Im", "lu_machinery.publish", "--"],
        ["/fixture/bin with space/python3.14", "-Wignore", "-X", "dev", "-mlu_machinery.publish.__main__"],
    ):
        merge = guard._merge_args([
            *prefix, "pr-merge",
            "--repo", "unit/public", "--number=2", "--repo", "unit/private", "--number", "1",
            "--match-head=" + "a" * 40, "--expect-queue=false",
        ])
        assert merge.refusal is None
        assert guard._pr_selector(merge.args) == "1" and guard._repo_option(merge.args) == "unit/private"


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("repo", ["unit/public", "unit/private"])
@pytest.mark.parametrize("state", ["draft", "red", "pending", "ready"])
def test_hook_judges_publisher_readiness(repo, state, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("r5_hook", ROOT / "agents_extensions/shared/hooks/guard-pr-merge.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    command = (
        f".venv/bin/python -I -m lu_machinery.publish.__main__ pr-merge --repo {repo} --number=2 --number 1 "
        f"--match-head {'a' * 40} --expect-queue true"
    )
    monkeypatch.setattr(guard, "_read_payload", lambda: {"tool_input": {"command": command}})
    reads = []
    def snapshot(pr, target, cwd=None):
        reads.append((pr, target))
        return {"isDraft": state == "draft"}, (["CI Gate"] if state == "red" else [], ["CI Gate"] if state == "pending" else [], 1)
    monkeypatch.setattr(guard, "_pr_snapshot", snapshot)
    assert guard.main() == (0 if state == "ready" else 2)
    assert reads == [("1", repo)]


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_review_publisher_default_uses_guarded_transport(synthetic_opsec, tmp_path, monkeypatch):
    from lu_machinery.fleet_comms import review_publisher
    calls = []
    executable = tmp_path / "real-gh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("AGENT_REAL_GH", str(executable))
    def send(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "https://example.invalid/comment/1", "")
    monkeypatch.setattr(pub, "_run_transport", send)
    assert review_publisher.post_pr_comment(repository="unit/public", pr_number=1, body="fixture") == "https://example.invalid/comment/1"
    assert len(calls) == 1 and calls[0][1:3] == ["pr", "comment"]
    assert "--body-file" in calls[0]


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_issue_count_fixed_read_positive_and_existing_reads_unchanged(tmp_path, monkeypatch):
    calls = []
    query_paths = []
    scratch = tmp_path / "managed-scratch"
    scratch.mkdir()
    monkeypatch.setattr(pub, "ensure_scratch_root", lambda: scratch)
    def transport(command, **kwargs):
        query_path = Path(command[command.index("--input") + 1])
        assert query_path.parent.parent == scratch
        query_paths.append(query_path)
        document = json.loads(query_path.read_text())
        calls.append(document)
        return subprocess.CompletedProcess(command, 0, '{"data":{"repository":{"issues":{"totalCount":612}}}}', '')
    result = pub.read("issue-count", repo="unit/public", runner=transport, cwd=tmp_path, env={}, capture_output=True, text=True)
    assert json.loads(result.stdout)["data"]["repository"]["issues"]["totalCount"] == 612
    assert calls == [{"query": "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){issues(states:OPEN){totalCount}}}",
                      "variables": {"owner": "unit", "name": "public"}}]
    assert "issue-count" not in pub.GQL_READS and "issue-count" not in pub.REST_READS
    assert all(not path.exists() for path in query_paths)
    assert list(scratch.iterdir()) == []


@pytest.mark.usefixtures('configured_roots', 'catalog')
@pytest.mark.parametrize("response", ['{}', '{"data":{"repository":null}}', '{"data":{"repository":{"issues":{"totalCount":true}}}}',
                                     '{"data":{"repository":{"issues":{"totalCount":-1}}}}',
                                     '{"data":{"repository":{"issues":{"totalCount":3}}},"errors":[{"message":"private"}]}'])
def test_issue_count_malformed_refused(response):
    with pytest.raises(gate.PublishBlocked, match="malformed issue count response"):
        pub.read("issue-count", repo="unit/private", runner=lambda command, **k: subprocess.CompletedProcess(command, 0, response, ''), env={})


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_issue_count_unknown_repository_or_fields_refused():
    for fields in [{"repo": "unit/unknown"}, {"repo": "unit/public", "number": 1}]:
        with pytest.raises(gate.PublishBlocked):
            pub.read("issue-count", **fields, runner=lambda *a, **k: pytest.fail("must not send"), env={})


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_issue_count_cli_prints_validated_json(capsys):
    response = '{"data":{"repository":{"issues":{"totalCount":42}}}}'
    assert pub.main(['read', 'issue-count', '--repo', 'unit/public'], runner=lambda command, **k: subprocess.CompletedProcess(command, 0, response, '')) == 0
    assert json.loads(capsys.readouterr().out)['data']['repository']['issues']['totalCount'] == 42


@pytest.mark.usefixtures('configured_roots', 'catalog')
def test_issue_count_uses_fixed_read_output_contract():
    import io
    response = '{"data":{"repository":{"issues":{"totalCount":42}}}}'
    def transport(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, response, '')
    result = pub.read('issue-count', repo='unit/public', runner=transport, env={}, capture_output=True)
    assert isinstance(result.stdout, bytes)
    assert json.loads(result.stdout)['data']['repository']['issues']['totalCount'] == 42
    output = io.StringIO()
    result = pub.read('issue-count', repo='unit/public', runner=transport, env={}, text=True, stdout=output)
    assert result.returncode == 0
    assert json.loads(output.getvalue())['data']['repository']['issues']['totalCount'] == 42


_WORKFLOW_INPUTS = {'deploy-pages.yml': '---\nname: Deploy to GitHub Pages\n\non:\n  # #5356: only deploy a pushed main revision after a fail-closed preflight\n  # proves that no curriculum or generated site content changed since the last\n  # successful Pages deployment.  Manual dispatch remains the certification\n  # route for learner-content changes.\n  push:\n    branches: [main]\n  workflow_dispatch:\n\n# Workflow-level: least privilege. pages/id-token write stay on the deploy job\n# only (zizmor excessive-permissions / code-scanning #358 #359).\npermissions:\n  actions: read\n  contents: read\n  deployments: read\n\nconcurrency:\n  group: "pages"\n  cancel-in-progress: false\n\njobs:\n  auto-deploy-eligibility:\n    name: Determine automatic deployment eligibility\n    if: github.event_name == \'push\'\n    runs-on: ubuntu-latest\n    timeout-minutes: 10\n    outputs:\n      deploy: ${{ steps.classify.outputs.deploy }}\n    steps:\n      - name: Checkout full main history\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          fetch-depth: 0\n          filter: blob:none\n          persist-credentials: false\n\n      - name: Setup Python\n        uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0\n        with:\n          python-version-file: \'.python-version\'\n\n      - name: Create Python venv\n        run: |\n          set -euo pipefail\n          python -m venv .venv\n          # The hash-pinned lu-machinery release first, as .github/actions/python-ci-env installs it\n          # (machinery docs/release.md, Consumers); the lock\'s hashes decide which bytes install.\n          version="$(sed -nE \'s/^lu-machinery==([0-9]+(\\.[0-9]+)*) \\\\$/\\1/p\' requirements-machinery.lock)"\n          wheelhouse="${RUNNER_TEMP}/lu-machinery-wheelhouse"\n          mkdir -p "$wheelhouse"\n          curl -fsSL --retry 10 --retry-all-errors --retry-delay 5 --retry-max-time 300 --connect-timeout 20 \\\n            -o "${wheelhouse}/lu_machinery-${version}-py3-none-any.whl" \\\n            "https://github.com/learn-ukrainian/machinery/releases/download/v${version}/lu_machinery-${version}-py3-none-any.whl"\n          .venv/bin/python -m pip install --require-hashes --no-deps --find-links "$wheelhouse" -r requirements-machinery.lock\n\n      - name: Resolve the last successful Pages deployment\n        id: last-deploy\n        env:\n          GH_TOKEN: ${{ github.token }}\n          REPOSITORY: ${{ github.repository }}\n        run: |\n          set -euo pipefail\n          deployments_json="$(\n            curl --fail --silent --show-error --retry 3 --connect-timeout 10 --max-time 30 \\\n              --header "Authorization: Bearer ${GH_TOKEN}" \\\n              --header \'X-GitHub-Api-Version: 2022-11-28\' \\\n              "https://api.github.com/repos/${REPOSITORY}/deployments?environment=github-pages&per_page=20"\n          )"\n\n          last_deploy_sha=""\n          while IFS=$\'\\t\' read -r dep_id dep_sha; do\n            [[ -z "$dep_id" || "$dep_id" == "null" ]] && continue\n\n            statuses_json="$(\n              curl --fail --silent --show-error --retry 3 --connect-timeout 10 --max-time 30 \\\n                --header "Authorization: Bearer ${GH_TOKEN}" \\\n                --header \'X-GitHub-Api-Version: 2022-11-28\' \\\n                "https://api.github.com/repos/${REPOSITORY}/deployments/${dep_id}/statuses"\n            )"\n\n            has_success="$(jq -r \'[.[] | select(.state == "success")] | length > 0\' <<< "$statuses_json")"\n            if [[ "$has_success" == "true" ]]; then\n              if git cat-file -e "${dep_sha}^{commit}" 2>/dev/null && \\\n                 git merge-base --is-ancestor "$dep_sha" "$GITHUB_SHA" 2>/dev/null; then\n                last_deploy_sha="$dep_sha"\n                break\n              fi\n            fi\n          done < <(jq -r \'.[] | "\\(.id)\\t\\(.sha)"\' <<< "$deployments_json")\n\n          if [[ -n "$last_deploy_sha" ]]; then\n            echo "Resolved last deployment anchor: $last_deploy_sha"\n            printf \'sha=%s\\n\' "$last_deploy_sha" >> "$GITHUB_OUTPUT"\n          else\n            echo "No prior successful deployment anchor resolvable; auto-deploy will fail closed."\n            printf \'sha=\\n\' >> "$GITHUB_OUTPUT"\n          fi\n\n      - name: Classify all changes since that deployment\n        id: classify\n        env:\n          LAST_DEPLOY_SHA: ${{ steps.last-deploy.outputs.sha }}\n        run: |\n          set -euo pipefail\n          if [[ -z "${LAST_DEPLOY_SHA:-}" ]]; then\n            echo "auto-deploy eligibility: no_prior_deployment (fail closed)"\n            echo "::warning title=Pages Auto-Deploy Skipped::Pages auto-deploy was skipped: no_prior_deployment. See job summary for details."\n            if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then\n              # shellcheck disable=SC2016\n              {\n                echo \'### ⚠️ Pages Auto-Deploy: Skipped\'\n                echo \'- **Decision**: `no_prior_deployment`\'\n                echo \'- **Detail**: No prior successful deployment anchor resolvable; fail closed.\'\n                echo \'\'\n              } >> "$GITHUB_STEP_SUMMARY"\n            fi\n            printf \'deploy=false\\n\' >> "$GITHUB_OUTPUT"\n            printf \'reason=no_prior_deployment\\n\' >> "$GITHUB_OUTPUT"\n            exit 0\n          fi\n          changed_paths="$RUNNER_TEMP/changed-paths.nul"\n          git diff --no-renames --name-only --diff-filter=ACDMRT -z \\\n            "$LAST_DEPLOY_SHA" "$GITHUB_SHA" > "$changed_paths"\n          .venv/bin/python scripts/deploy/auto_deploy_eligibility.py \\\n            --changed-paths "$changed_paths" \\\n            --github-output "$GITHUB_OUTPUT" \\\n            --github-step-summary "$GITHUB_STEP_SUMMARY"\n\n  deploy:\n    permissions:\n      contents: read\n      pages: write\n      id-token: write\n    needs: auto-deploy-eligibility\n    if: >-\n      always() && (github.event_name == \'workflow_dispatch\' ||\n      needs.auto-deploy-eligibility.outputs.deploy == \'true\')\n    environment:\n      name: github-pages\n      url: ${{ steps.deployment.outputs.page_url }}\n    runs-on: ubuntu-latest\n    timeout-minutes: 20\n    defaults:\n      run:\n        working-directory: ./site\n    steps:\n      - name: Checkout\n        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          # This job builds + deploys via actions/deploy-pages (OIDC) and never\n          # git-pushes, so the checkout token must not persist into .git/config\n          # where it could leak through the uploaded Pages artifact (zizmor\n          # artipacked / CodeQL #249).\n          persist-credentials: false\n\n      - name: Setup Node.js\n        uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0\n        with:\n          # Astro 7.4.2 requires Node >=22.23.4. The version is .nvmrc.\n          node-version-file: \'.nvmrc\'\n          cache: \'npm\'\n          cache-dependency-path: site/package-lock.json\n\n      - name: Setup Python\n        uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0\n        with:\n          python-version-file: \'.python-version\'\n\n      - name: Create Python venv\n        working-directory: .\n        run: |\n          python -m venv .venv\n          set -euo pipefail\n          .venv/bin/python -m pip install --upgrade pip\n          # The hash-pinned lu-machinery release first, as .github/actions/python-ci-env installs it\n          # (machinery docs/release.md, Consumers); the lock\'s hashes decide which bytes install.\n          version="$(sed -nE \'s/^lu-machinery==([0-9]+(\\.[0-9]+)*) \\\\$/\\1/p\' requirements-machinery.lock)"\n          wheelhouse="${RUNNER_TEMP}/lu-machinery-wheelhouse"\n          mkdir -p "$wheelhouse"\n          curl -fsSL --retry 10 --retry-all-errors --retry-delay 5 --retry-max-time 300 --connect-timeout 20 \\\n            -o "${wheelhouse}/lu_machinery-${version}-py3-none-any.whl" \\\n            "https://github.com/learn-ukrainian/machinery/releases/download/v${version}/lu_machinery-${version}-py3-none-any.whl"\n          .venv/bin/python -m pip install --require-hashes --no-deps --find-links "$wheelhouse" -r requirements-machinery.lock\n          .venv/bin/python -m pip install PyYAML==6.0.3 jsonschema==4.26.0 regex==2026.5.9\n\n      - name: Restore Atlas lexicon manifest cache\n        id: atlas-manifest-cache\n        uses: actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0\n        with:\n          path: site/src/data/lexicon-manifest.json\n          key: lexicon-manifest-${{ hashFiles(\'site/src/data/lexicon-manifest.pointer.json\') }}\n\n      - name: Verify Atlas manifest matches pointer (rehydrate if stale)\n        working-directory: .\n        run: |\n          .venv/bin/python - <<\'PY\'\n          import sys\n          sys.path.insert(0, \'scripts\')\n          from lexicon.manifest_io import load_manifest\n          load_manifest()\n          print(\'Atlas manifest verified against pointer\')\n          PY\n\n      - name: Save Atlas lexicon manifest cache\n        if: steps.atlas-manifest-cache.outputs.cache-hit != \'true\'\n        uses: actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0\n        with:\n          path: site/src/data/lexicon-manifest.json\n          key: lexicon-manifest-${{ hashFiles(\'site/src/data/lexicon-manifest.pointer.json\') }}\n\n      # #11194: the optional noindex /lexicon-v2/ candidate preview. Downloads only\n      # the public word-cards release assets named by the committed pin, without a\n      # token, and refuses any size/SHA-256 mismatch before the build sees them.\n      # The release tag only locates the assets; the pinned bytes decide.\n      - name: Prepare pinned word-card preview release\n        id: word-card-preview\n        working-directory: .\n        env:\n          RELEASE_DIR: ${{ runner.temp }}/word-card-preview-release\n        run: >-\n          .venv/bin/python scripts/deploy/prepare_word_card_preview.py prepare\n          --dest "$RELEASE_DIR" --github-output "$GITHUB_OUTPUT"\n\n      - name: Install dependencies\n        run: npm ci\n\n      - name: Prepare pinned Atlas search runtime\n        working-directory: .\n        env:\n          ATLAS_TREE_ASSET_ID: ${{ vars.ATLAS_TREE_ASSET_ID }}\n          ATLAS_TREE_SHA256: ${{ vars.ATLAS_TREE_SHA256 }}\n          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n        run: >-\n          .venv/bin/python site/scripts/prepare-atlas-search.py\n          --dest "$RUNNER_TEMP/atlas-search-runtime"\n\n      # #5274: normal build leaves BUILD_ETYMOLOGY_ROUTES off (~296 MB). build:full\n      # prerenders ~25k standalone etymology pages and blew the 1 GiB Pages budget.\n      # Pronunciation audio stays a local Makefile target (gitignored); Pages CI must not invoke TTS.\n      # #11194: PUBLIC_ATLAS_WORD_CARD_PREVIEW_CONTROLS is test-only and must be\n      # absent here (Vite inlines it); the build refuses to start if it is set.\n      - name: Build Site\n        env:\n          ATLAS_SEARCH_RUNTIME_TREE: ${{ runner.temp }}/atlas-search-runtime\n          ATLAS_WORD_CARD_PREVIEW: \'1\'\n          ATLAS_WORD_CARD_RELEASE_PATH: ${{ steps.word-card-preview.outputs.release-path }}\n          ATLAS_WORD_CARD_RELEASE_PIN: ${{ steps.word-card-preview.outputs.pin }}\n        run: |\n          if [[ -n "${PUBLIC_ATLAS_WORD_CARD_PREVIEW_CONTROLS+set}" ]]; then\n            echo "::error title=Word-card preview refused::PUBLIC_ATLAS_WORD_CARD_PREVIEW_CONTROLS must be absent from the public build"\n            exit 1\n          fi\n          npm run build\n\n      # Atlas owns the route set; its committed inventory is reconciled with the\n      # verified release, then the built preview must match it exactly.\n      - name: Verify word-card preview routes\n        working-directory: .\n        env:\n          RELEASE_DIR: ${{ steps.word-card-preview.outputs.release-path }}\n        run: >-\n          .venv/bin/python scripts/deploy/prepare_word_card_preview.py verify-dist site/dist\n          --release-dir "$RELEASE_DIR"\n          --inventory site/src/lib/lexicon/word-card-preview-route-inventory.json\n\n      - name: Emit immutable deployment marker\n        run: |\n          mkdir -p dist/.well-known\n          printf \'%s\\n\' "$GITHUB_SHA" > "dist/.well-known/learn-ukrainian-deployment-${GITHUB_SHA}.txt"\n\n      # PR3 D1/R1/R7: vendor pinned atlas runtime tree into dist/atlas/ (same-origin).\n      # Public Pages preparation above requires the immutable pin. The vendor\n      # library still permits unpinned callers outside this recipe to skip.\n      # Pin is asset id + sha256 digest — never a mutable release tag name.\n      # Runs AFTER build, BEFORE the size gate so the vendored tree counts.\n      - name: Vendor Atlas runtime tree\n        working-directory: .\n        env:\n          ATLAS_TREE_ASSET_ID: ${{ vars.ATLAS_TREE_ASSET_ID }}\n          ATLAS_TREE_SHA256: ${{ vars.ATLAS_TREE_SHA256 }}\n          ATLAS_TREE_METRICS_PATH: ${{ runner.temp }}/atlas-vendor-metrics.json\n          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n        run: .venv/bin/python scripts/deploy/vendor_atlas_tree.py site/dist\n\n      # #5274: fail-closed size gate (replaces advisory 70% warn-only). Runs after\n      # build (+ optional atlas vendoring), before upload-pages-artifact — unknown\n      # profile or ≥80% of 1 GiB fails.\n      - name: Check published-site size (fail-closed)\n        working-directory: .\n        env:\n          DEPLOY_PROFILE: github-pages\n        run: .venv/bin/python scripts/deploy/check_site_size.py site/dist\n\n      - name: Setup Pages\n        uses: actions/configure-pages@45bfe0192ca1faeb007ade9deae92b16b8254a0d # v6.0.0\n\n      # #11194: the pinned upload action drops every dot-named path unless\n      # include-hidden-files is true, which silently removed the .well-known\n      # marker. Turning it on would publish ANY hidden path, so this final audit\n      # (after marker, build and vendoring) allows only .well-known/ holding the\n      # single regular exact-SHA marker and fails the job before upload otherwise.\n      - name: Audit hidden Pages outputs (deployment marker only)\n        working-directory: .\n        run: .venv/bin/python scripts/deploy/verify_pages_deployment_marker.py site/dist --sha "$GITHUB_SHA"\n\n      - name: Upload artifact\n        uses: actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0\n        with:\n          path: \'./site/dist\'\n          # Explicit, never the action default: publishes the audited marker.\n          include-hidden-files: true\n\n      - name: Deploy to GitHub Pages\n        id: deployment\n        uses: actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346 # v5.0.1\n', 'stale-branch-cleanup.yml': 'name: Stale branch cleanup\n\non:\n  schedule:\n    - cron: \'43 4 * * *\'\n  workflow_dispatch:\n\npermissions: {}\n\nconcurrency:\n  group: stale-branch-cleanup\n  cancel-in-progress: false\n\njobs:\n  cleanup:\n    if: github.ref == format(\'refs/heads/{0}\', github.event.repository.default_branch)\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    permissions:\n      contents: write\n      pull-requests: read\n    steps:\n      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          ref: ${{ github.sha }}\n          persist-credentials: false\n          fetch-depth: 1\n          sparse-checkout: |\n            .github/branch-cleanup-keep.txt\n            requirements-machinery.lock\n            .python-version\n          sparse-checkout-cone-mode: false\n      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0\n        with:\n          python-version-file: \'.python-version\'\n      # The locked lu-machinery wheel alone (no dependencies); the lock\'s hash decides which bytes install.\n      - name: Install the locked lu-machinery release\n        run: |\n          set -euo pipefail\n          version="$(sed -nE \'s/^lu-machinery==([0-9]+(\\.[0-9]+)*) \\\\$/\\1/p\' requirements-machinery.lock)"\n          wheelhouse="${RUNNER_TEMP}/lu-machinery-wheelhouse"\n          mkdir -p "$wheelhouse"\n          curl -fsSL --retry 10 --retry-all-errors --retry-delay 5 --retry-max-time 300 --connect-timeout 20 \\\n            -o "${wheelhouse}/lu_machinery-${version}-py3-none-any.whl" \\\n            "https://github.com/learn-ukrainian/machinery/releases/download/v${version}/lu_machinery-${version}-py3-none-any.whl"\n          sed -n \'/^lu-machinery==/,/--hash=/p\' requirements-machinery.lock > "${RUNNER_TEMP}/lu-machinery-lock.txt"\n          python -m pip install --disable-pip-version-check --quiet --no-index --no-deps --require-hashes \\\n            --find-links "$wheelhouse" -r "${RUNNER_TEMP}/lu-machinery-lock.txt"\n      - name: Remove eligible remote branches\n        env:\n          GH_TOKEN: ${{ github.token }}\n          REPO: ${{ github.repository }}\n          # Git reads credentials through gh only for this step; nothing is persisted.\n          GIT_CONFIG_COUNT: \'1\'\n          GIT_CONFIG_KEY_0: credential.https://github.com.helper\n          GIT_CONFIG_VALUE_0: \'!gh auth git-credential\'\n          LU_PRODUCT_ROOT: ${{ github.workspace }}\n          LEARN_UKRAINIAN_PRIMARY_REPO_ROOT: ${{ github.workspace }}\n        run: |\n          git remote set-url origin "https://github.com/$REPO.git"\n          git remote set-url --push origin "https://github.com/$REPO.git"\n          python -m lu_machinery.hygiene.remote_branch_cleanup --repo "$REPO"\n'}


@pytest.fixture
def publisher_workflows(configured_roots, tmp_path, monkeypatch):
    import shutil
    product = tmp_path / "publisher-product"
    shutil.copytree(configured_roots.product, product)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(product))
    for name, text in _WORKFLOW_INPUTS.items():
        (product / ".github/workflows" / name).write_text(text)

pytestmark = [pytest.mark.usefixtures('configured_roots')]
