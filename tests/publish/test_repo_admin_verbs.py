"""repo-create and repo-transfer (GH-ADMIN-VERBS-1124): a fake gh runner and test-owned registry rows; no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.common import github_client
from lu_machinery.opsec import prepublish as gate
from lu_machinery.orchestration import fleet_repos
from lu_machinery.publish import github as pub

OLD, NEW = "fixture-old", "fixture-new"
REPO_ID = 111
SOURCE, DESTINATION = f"{OLD}/infra", f"{NEW}/infra-next"
CATALOG = {
    "public": {"github": f"{OLD}/public", "default": True, "local_name": "public", "role": "public-monorepo"},
    "infra-private": {
        "github": SOURCE, "alternate_github": DESTINATION, "repository_id": REPO_ID, "visibility": "private",
        "local_name": "infra", "role": "private-infra",
    },
    "single": {"github": f"{OLD}/single", "local_name": "single", "role": "private-labs"},
    "public-move": {
        "github": f"{OLD}/pub", "alternate_github": f"{NEW}/pub", "repository_id": 222, "visibility": "public",
        "local_name": "pub", "role": "public-labs",
    },
    # A registered name that a fixture's <name>-moved destination would take.
    "guard": {"github": f"{OLD}/held-moved", "local_name": "held-moved", "role": "private-labs"},
}
WORKER_MARKERS = ("LEARN_UKRAINIAN_DISPATCH_TASK_ID", "LU_WRITE_BOUNDARY_BROKER")
# The real state-directory and account-home resolvers, before the autouse fixture replaces them.
REAL_STATE_DIRECTORY, REAL_ACCOUNT_HOME = pub._repo_admin_directory, pub._account_home
PRINCIPAL = "octo-admin"


def repository(repository_id, full_name, *, private=True, archived=False, admin=True):
    return {
        "id": repository_id, "full_name": full_name, "private": private,
        "visibility": "private" if private else "public", "archived": archived, "permissions": {"admin": admin},
    }


def answer(args, document, code=0):
    return subprocess.CompletedProcess(args, code, json.dumps(document), "")


def not_found(args):
    return subprocess.CompletedProcess(args, 1, json.dumps({"message": "Not Found", "status": "404"}), "gh: Not Found (HTTP 404)")


def server_error(args):
    return subprocess.CompletedProcess(args, 1, "", "gh: Server Error (HTTP 502)")


class FakeGitHub:
    """Repositories, memberships and the authenticated login; records every call and every POST."""

    def __init__(self, *, login=PRINCIPAL, after_login=None):
        self.repos = {SOURCE: repository(REPO_ID, SOURCE)}
        self.memberships = {OLD: {"state": "active", "role": "admin"}, NEW: {"state": "active", "role": "admin"}}
        self.login, self.after_login = login, after_login
        self.calls, self.posts, self.overrides = [], [], {}
        self.next_id = 900

    def find(self, slug):
        return next((row for row in self.repos.values() if row["full_name"].casefold() == slug.casefold()), None)

    def __call__(self, args, **kwargs):
        assert args[:3] == ["gh", "api", "--method"], args
        assert not set(pub.REPO_ADMIN_CREDENTIAL_OVERRIDES) & set(kwargs["env"])
        self.calls.append(args)
        method, endpoint = args[3], args[4]
        override = self.overrides.get((method, endpoint, bool(self.posts)))
        if method == "POST":
            payload = json.loads(Path(args[args.index("--input") + 1]).read_text())
            self.posts.append((endpoint, payload))
        if override is not None:
            if isinstance(override, BaseException):
                raise override
            return override(args)
        if method == "POST":
            return self.post(args, endpoint, payload)
        if endpoint == "user":
            login = self.after_login if self.posts and self.after_login else self.login
            return answer(args, {"login": login, "id": 7})
        if endpoint.startswith("user/memberships/orgs/"):
            organization = endpoint.rsplit("/", 1)[1]
            membership = self.memberships.get(organization)
            return not_found(args) if membership is None else answer(
                args, {**membership, "organization": {"login": organization}},
            )
        found = self.find(endpoint.removeprefix("repos/"))
        return not_found(args) if found is None else answer(args, found)

    def post(self, args, endpoint, payload):
        if endpoint.startswith("orgs/"):
            self.next_id += 1
            created = repository(self.next_id, f"{endpoint.split('/')[1]}/{payload['name']}")
            self.repos[created["full_name"]] = created
            return answer(args, created)
        moved = self.repos.pop(endpoint.removeprefix("repos/").removesuffix("/transfer"))
        moved["full_name"] = f"{payload['new_owner']}/{payload['new_name']}"
        self.repos[moved["full_name"]] = moved
        return answer(args, moved, 0)


@pytest.fixture(autouse=True)
def selector(tmp_path, monkeypatch):
    """Test-owned registry, state, scratch and product directories; the selector names the old slug."""
    monkeypatch.setattr(gate, "catalog", lambda: CATALOG)
    monkeypatch.setattr(pub, "_transfer_sleep", lambda _delay: None)
    for name in (*WORKER_MARKERS, *pub.REPO_ADMIN_CREDENTIAL_OVERRIDES, "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "product").mkdir()
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(tmp_path / "product"))
    monkeypatch.setattr(pub, "_repo_admin_directory", lambda: str(state_dir(tmp_path)))
    # A test-owned account home, which HOME names (a HOME other than the account's is refused).
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(pub, "_account_home", lambda: str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    chosen = {"slug": SOURCE, "error": None}

    def active(identifier, *, repos, env=None, **_):
        if chosen["error"] is not None:
            raise chosen["error"]
        if chosen["slug"] is None:
            raise fleet_repos.FleetRepoError("fleet repository selector required")
        return fleet_repos.ActiveFleetRepo(repo=repos[identifier], active_github=chosen["slug"])

    monkeypatch.setattr(fleet_repos, "resolve_active_fleet_repo", active)
    return chosen


def state_dir(tmp_path):
    return tmp_path / "state" / "learn-ukrainian" / pub.REPO_ADMIN_DIRECTORY


def journal(tmp_path):
    return state_dir(tmp_path) / f"repo-transfer-{REPO_ID}.pending"


def run(verb, gh, **fields):
    result = pub.publish(verb, runner=gh, capture_output=True, text=True, principal=PRINCIPAL, **fields)
    return result, json.loads(result.stdout) if result.stdout else None


def create_fixture(gh, name="rehearsal-1"):
    result, receipt = run("repo-create", gh, repo="infra-private", name=name)
    assert result.returncode == 0, result
    return receipt, next(state_dir_of(receipt).glob(f"repo-create-{name}-*.json"))


def state_dir_of(_receipt):
    return Path(pub._repo_admin_directory())


CALLER = [
    ("HOME", None),
    # Only this process's HOME differs: the environment given to publish() carries the account's home.
    ("process HOME", None),
    *[(marker, "task-1") for marker in WORKER_MARKERS],
    *[(name, "/caller/value") for name in pub.REPO_ADMIN_CREDENTIAL_OVERRIDES],
    ("GH_HOST", "ghe.example.com"),
]


@pytest.mark.parametrize("verb,fields", [("repo-create", {"name": "rehearsal-1"}), ("repo-transfer", {})])
@pytest.mark.parametrize("name,value", CALLER)
def test_caller_authority_and_credential_overrides_refused_before_any_read(monkeypatch, verb, fields, name, value):
    """Defect class: a caller-controlled identity or credential (worker marker, token, gh config, host) reaching GitHub.

    An alternate spelling of the account's HOME is refused for the gh transport and for this process.
    """
    gh = FakeGitHub()
    if name == "process HOME":
        monkeypatch.setenv("HOME", f"{pub._account_home()}/.")
        with pytest.raises(gate.PublishBlocked, match="_REFUSED: HOME must be"):
            pub.publish(
                verb, runner=gh, capture_output=True, text=True, principal=PRINCIPAL, repo="infra-private",
                env={**os.environ, "HOME": pub._account_home()}, **fields,
            )
        assert gh.calls == []
        return
    monkeypatch.setenv(name, f"{pub._account_home()}/." if name == "HOME" else value)
    with pytest.raises(gate.PublishBlocked, match="_REFUSED: "):
        run(verb, gh, repo="infra-private", **fields)
    assert gh.calls == []


def test_principal_mismatch_refused_before_mutation_and_unsettled_after(tmp_path):
    """Defect class: a mutation under an unapproved principal, or a credential swapped while it ran."""
    gh = FakeGitHub(login="someone-else")
    with pytest.raises(gate.PublishBlocked, match="not the named principal"):
        run("repo-transfer", gh, repo="infra-private")
    assert gh.posts == []
    gh = FakeGitHub(after_login="someone-else")
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and len(gh.posts) == 1
    assert receipt["outcome"] == "unverified" and "someone-else" in receipt["detail"]
    assert journal(tmp_path).exists()


def _taken(slug):
    def change(gh):
        gh.repos[slug] = repository(5, slug)
    return change


@pytest.mark.parametrize("name,change,reason", [
    ("infra", None, "registered repository"),
    ("infra-next", None, "registered repository"),
    ("held", None, "registered repository"),
    ("rehearsal-1", _taken(f"{OLD}/rehearsal-1"), "is taken"),
    ("rehearsal-1", _taken(f"{NEW}/rehearsal-1-moved"), "is taken"),
    ("rehearsal-1", lambda gh: gh.memberships[OLD].update(role="member"), "not an active admin"),
    ("rehearsal-1", lambda gh: gh.memberships[OLD].update(state="pending"), "not an active admin"),
    ("rehearsal-1", lambda gh: gh.overrides.update({("GET", f"repos/{OLD}/rehearsal-1", False): server_error}),
     "unreadable"),
])
def test_repo_create_preflight_refusals(name, change, reason):
    """Defect class: a fixture colliding with a real repository or destination, or created without rights."""
    gh = FakeGitHub()
    if change is not None:
        change(gh)
    with pytest.raises(gate.PublishBlocked, match=reason):
        run("repo-create", gh, repo="infra-private", name=name)
    assert gh.posts == []


def test_repo_create_sends_once_and_receipt_binds_id(tmp_path, monkeypatch):
    """Defect class: a duplicate or missing creation, or a receipt that does not bind the created id."""
    gh = FakeGitHub()
    result, receipt = run("repo-create", gh, repo="infra-private", name="rehearsal-1", dry_run=True)
    assert result.returncode == 0 and receipt["outcome"] == "dry-run" and gh.posts == []
    assert receipt["preflight"]["no_visible_collisions"] == [f"{OLD}/rehearsal-1", f"{NEW}/rehearsal-1-moved"]
    result, receipt = run("repo-create", gh, repo="infra-private", name="rehearsal-1")
    assert result.returncode == 0 and receipt["outcome"] == "created"
    assert gh.posts == [(f"orgs/{OLD}/repos", {"name": "rehearsal-1", "private": True, "auto_init": True})]
    assert receipt["repository"] == {"slug": f"{OLD}/rehearsal-1", "id": 901, "private": True, "visibility": "private"}
    assert receipt["rehearsal_destination"] == f"{NEW}/rehearsal-1-moved"
    assert receipt["principal"]["before"] == receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}
    files = sorted(state_dir(tmp_path).glob("repo-create-rehearsal-1-*.json"))
    assert len(files) == 2 and all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert json.loads(files[-1].read_text()) == receipt
    # repo-create keeps bounded polling until the expected private readback arrives (AC-4 unchanged).
    sleeps = []
    monkeypatch.setattr(pub, "_transfer_sleep", sleeps.append)
    delayed = FakeGitHub()
    create_polls = []

    def public_then_private(args):
        create_polls.append(args)
        return answer(args, repository(901, f"{OLD}/rehearsal-2", private=len(create_polls) > 1))

    delayed.overrides[("GET", f"repos/{OLD}/rehearsal-2", True)] = public_then_private
    result, receipt = run("repo-create", delayed, repo="infra-private", name="rehearsal-2")
    assert result.returncode == 0 and receipt["outcome"] == "created"
    assert len(delayed.posts) == 1 and len(create_polls) == 2 and sleeps == list(pub.TRANSFER_VERIFY_DELAYS[:2])
    assert receipt["principal"]["before"] == receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}


@pytest.mark.parametrize("outcome", [
    server_error,
    lambda args: subprocess.CompletedProcess(args, 0, "not json", ""),
    subprocess.TimeoutExpired(["gh"], 60),
    "readback",
])
def test_repo_create_unknown_send_is_unsettled(outcome):
    """Defect class: a retry or a success claim after a creation whose result is unknown."""
    gh = FakeGitHub()
    if outcome == "readback":
        gh.overrides[("GET", f"repos/{OLD}/rehearsal-1", True)] = not_found
    else:
        gh.overrides[("POST", f"orgs/{OLD}/repos", False)] = outcome
    result, receipt = run("repo-create", gh, repo="infra-private", name="rehearsal-1")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and len(gh.posts) == 1
    assert receipt["outcome"] in {"ambiguous", "unverified"} and receipt["mutations_sent"] == 1


IN_PROGRESS = "Failed to transfer repository. A previous repository operation is still in progress."


def client_answer(status, body, error="github_http_error"):
    """A POST answer in the shared client's own typed shape (its real producer), with a status and a body."""
    def respond(args):
        return github_client._result_process(
            args, github_client.Result(body, status=status, error=error), {}, {"capture_output": True, "text": True},
        )
    return respond


ADMIN_POSTS = {
    "repo-create": (["--name", "rehearsal-1"], f"orgs/{OLD}/repos", "repo-create-rehearsal-1-*.json"),
    "repo-transfer": ([], f"repos/{SOURCE}/transfer", f"repo-transfer-{REPO_ID}-*.json"),
}


@pytest.mark.parametrize("verb", sorted(ADMIN_POSTS))
@pytest.mark.parametrize("answer_,refused", [
    (client_answer(422, {"message": IN_PROGRESS, "documentation_url": "https://docs.github.com/rest"}), True),
    (client_answer(403, {"message": "Must have admin rights to Repository."}), True),
    (client_answer(502, {"message": "Server Error"}), False),
    (subprocess.TimeoutExpired(["gh"], 60), False),
    (client_answer(422, b"not json"), False),
    (client_answer(422, {"message": ""}), False),
    (client_answer(413, {"message": "github_response_too_large"}), False),
    (client_answer(429, None, error="github_rate_limited"), False),
    (lambda args: subprocess.CompletedProcess(args, 1, json.dumps({"message": IN_PROGRESS}), "HTTP 422"), False),
])
def test_github_refusal_of_the_post_is_a_refusal_not_unknown(tmp_path, capsys, verb, answer_, refused):
    """Defect class: GitHub's definite refusal of the one POST reported as an unknown result, or a lost answer as a refusal.

    Only the client's typed client-error answer with a message is a refusal: exit 2, its status and message in
    the receipt (on stdout and in its file) and on stderr, mutations_sent 0 and, for a transfer, the journal
    removed after the GET /user
    re-read. A 5xx, a timeout, a rate limit, the client's own 413, an answer without a readable message or an
    untyped failure stays unknown: exit 3, and a transfer keeps its journal.
    """
    extra, endpoint, pattern = ADMIN_POSTS[verb]
    argv = [verb, "--repo", "infra-private", "--principal", PRINCIPAL, *extra]
    gh = FakeGitHub()
    # The same answer to every POST of this test (the fake keys overrides on whether a POST was already sent).
    gh.overrides[("POST", endpoint, False)] = gh.overrides[("POST", endpoint, True)] = answer_
    code = pub.main(argv, runner=gh)
    out, err = capsys.readouterr()
    receipt = json.loads(sorted(state_dir(tmp_path).glob(pattern))[-1].read_text())
    assert len(gh.posts) == 1 and json.loads(out) == receipt
    if not refused:
        assert code == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "ambiguous"
        assert receipt["mutations_sent"] == 1 and "github_refusal" not in receipt
        assert journal(tmp_path).exists() is (verb == "repo-transfer")
        return
    document = answer_(["gh"]).github_result
    expected = {"status": document.status, "message": document.value["message"]}
    assert code == 2 and receipt["outcome"] == "refused" and receipt["mutations_sent"] == 0
    assert receipt["github_refusal"] == expected
    assert f"HTTP {expected['status']} {json.dumps(expected['message'])}" in receipt["detail"]
    assert f"HTTP {expected['status']} {json.dumps(expected['message'])}" in err and "_REFUSED: " in err
    assert "UNSETTLED" not in err
    if verb == "repo-transfer":
        # The journal goes only after a GET /user that follows the POST.
        assert gh.calls[-1] == ["gh", "api", "--method", "GET", "user"] and not journal(tmp_path).exists()
    # A receipt file that cannot be written still leaves the whole receipt on stdout.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pub, "_write_transfer_receipt", lambda sink, receipt: False)
        assert pub.main(argv, runner=gh) == 2 and len(gh.posts) == 2
        out, err = capsys.readouterr()
        assert json.loads(out)["github_refusal"] == expected and "RECEIPT_UNWRITTEN" in err
    if verb == "repo-transfer":
        # Nothing blocks a new attempt.
        assert not journal(tmp_path).exists()
        gh.overrides.clear()
        assert pub.main(argv, runner=gh) == 0 and len(gh.posts) == 3
        assert json.loads(capsys.readouterr().out)["outcome"] == "transferred"


def test_github_refusal_keeps_journal_when_principal_changed(tmp_path):
    """Defect class: a journal removed on a refusal without the principal re-read after the last GitHub read."""
    gh = FakeGitHub(after_login="someone-else")
    gh.overrides[("POST", f"repos/{SOURCE}/transfer", False)] = client_answer(422, {"message": IN_PROGRESS})
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
    assert receipt["mutations_sent"] == 0 and receipt["github_refusal"] == {"status": 422, "message": IN_PROGRESS}
    assert "the refused POST transferred nothing" in receipt["detail"] and "someone-else" in receipt["detail"]
    assert len(gh.posts) == 1 and journal(tmp_path).exists()


def test_real_move_derives_endpoints_from_selector_row(selector):
    """Defect class: transfer endpoints taken from caller input, from a single-slug/unregistered row, or in reverse."""
    gh = FakeGitHub()
    result, receipt = run("repo-transfer", gh, repo="infra-private", dry_run=True)
    assert result.returncode == 0 and gh.posts == []
    assert (receipt["mode"], receipt["source"], receipt["destination"], receipt["repository_id"], receipt["visibility"]) == (
        "move", SOURCE, DESTINATION, REPO_ID, "private",
    )
    for key, slug, reason in [
        ("infra-private", DESTINATION, "already names"),
        ("infra-private", None, "selector cannot bind"),
        ("single", SOURCE, "no alternate_github"),
        ("public", f"{OLD}/public", "no alternate_github"),
        ("nope", SOURCE, "not a repository key"),
    ]:
        selector["slug"] = slug
        gh = FakeGitHub()
        with pytest.raises(gate.PublishBlocked, match=reason):
            run("repo-transfer", gh, repo=key)
        assert gh.posts == []
    # repo-create stays private-only: it still refuses public dual-slug rows.
    with pytest.raises(gate.PublishBlocked, match="not a private repository"):
        run("repo-create", FakeGitHub(), repo="public-move", name="rehearsal-1")


def test_public_row_transfer_pins_id_and_preserves_visibility(tmp_path, selector, monkeypatch):
    """Defect class: public dual-slug row refused, or transferred without id pinning, unchanged visibility, and readback."""
    pub_source, pub_dest, pub_id = f"{OLD}/pub", f"{NEW}/pub", 222
    pub_journal = state_dir(tmp_path) / f"repo-transfer-{pub_id}.pending"
    selector["slug"] = pub_source

    def public_gh():
        gh = FakeGitHub()
        gh.repos = {pub_source: repository(pub_id, pub_source, private=False)}
        return gh

    # Dry run passes with pinned id and visibility="public", sending no POST and leaving no journal.
    gh = public_gh()
    result, receipt = run("repo-transfer", gh, repo="public-move", dry_run=True)
    assert result.returncode == 0 and receipt["outcome"] == "dry-run"
    assert (receipt["mode"], receipt["source"], receipt["destination"], receipt["repository_id"], receipt["visibility"]) == (
        "move", pub_source, pub_dest, pub_id, "public",
    )
    assert gh.posts == [] and not pub_journal.exists()

    # Rehearsal --create-receipt remains private-only and refuses public rows before opening any receipt.
    with pytest.raises(gate.PublishBlocked, match="requires a private registry row"):
        run("repo-transfer", public_gh(), repo="public-move", create_receipt=str(tmp_path / "missing.json"))

    # Preflight refusals on a public row: wrong id, flipped visibility, destination collision, archived, non-admin.
    for mutate, reason in (
        (lambda g: g.repos[pub_source].update(id=999), "not the pinned"),
        (lambda g: g.repos[pub_source].update(private=True, visibility="private"), "is not public"),
        (lambda g: g.repos[pub_source].update(private=False, visibility="private"), "is not public"),
        (lambda g: g.repos.update({pub_dest: repository(5, pub_dest, private=False)}), "holds repository 5"),
        (lambda g: g.repos[pub_source].update(archived=True), "is archived"),
        (lambda g: g.repos[pub_source].update(permissions={"admin": False}), "not an admin"),
    ):
        gh = public_gh()
        mutate(gh)
        with pytest.raises(gate.PublishBlocked, match=reason):
            run("repo-transfer", gh, repo="public-move")
        assert gh.posts == [] and not pub_journal.exists()

    # Async 202 acceptance (minimal-repository omitting visibility, private=False) settles once destination appears public.
    sleeps = []
    monkeypatch.setattr(pub, "_transfer_sleep", sleeps.append)
    gh = public_gh()
    gh.overrides[("POST", f"repos/{pub_source}/transfer", False)] = lambda args: answer(
        args, {"id": pub_id, "full_name": pub_source, "private": False},
    )
    polls = []

    def appear_public_on_second(args):
        polls.append(args)
        if len(polls) >= 2:
            gh.repos.pop(pub_source, None)
            moved = repository(pub_id, pub_dest, private=False)
            gh.repos[pub_dest] = moved
            return answer(args, moved)
        return not_found(args)

    gh.overrides[("GET", f"repos/{pub_dest}", True)] = appear_public_on_second
    result, receipt = run("repo-transfer", gh, repo="public-move")
    assert result.returncode == 0 and receipt["outcome"] == "transferred" and receipt["visibility"] == "public"
    assert gh.posts == [(f"repos/{pub_source}/transfer", {"new_owner": NEW, "new_name": "pub"})]
    assert len(polls) == 2 and sleeps == list(pub.TRANSFER_VERIFY_DELAYS[:2])
    assert gh.calls[-1] == ["gh", "api", "--method", "GET", "user"]
    assert receipt["principal"]["before"] == receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}
    assert gh.find(pub_dest)["id"] == pub_id and gh.find(pub_dest)["visibility"] == "public"
    assert not pub_journal.exists()

    # Rerun reconciles as already-transferred with visibility="public" and zero new POSTs.
    del gh.overrides[("GET", f"repos/{pub_dest}", True)]
    result, receipt = run("repo-transfer", gh, repo="public-move")
    assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
    assert "public" in receipt["detail"] and len(gh.posts) == 1

    # Visibility flip in POST 202 answer (private=True or visibility="private"/None) exits 3 unverified and keeps journal.
    for bad_ack in (
        {"id": pub_id, "full_name": pub_source, "private": True},
        {"id": pub_id, "full_name": pub_source, "private": False, "visibility": "private"},
        {"id": pub_id, "full_name": pub_source, "private": False, "visibility": None},
    ):
        gh = public_gh()
        gh.overrides[("POST", f"repos/{pub_source}/transfer", False)] = lambda args, doc=bad_ack: answer(args, doc)
        result, receipt = run("repo-transfer", gh, repo="public-move")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
        assert "pinned public repository" in receipt["detail"] and pub_journal.exists()
        pub_journal.unlink()

    # Transient visibility mismatch on poll 1 followed by a matching public response on poll 2 must stop
    # immediately on poll 1, exit 3 unverified, and keep the journal (never overwrite evidence with poll 2).
    for first_doc in (
        repository(pub_id, pub_dest, private=True),
        {**repository(pub_id, pub_dest, private=False), "visibility": "private"},
        {"id": pub_id, "full_name": pub_dest, "private": True, "visibility": None, "archived": False},
    ):
        sleeps = []
        monkeypatch.setattr(pub, "_transfer_sleep", sleeps.append)
        gh = public_gh()
        mismatch_polls = []

        def mismatch_then_match(args, bad=first_doc, calls=mismatch_polls):
            calls.append(args)
            if len(calls) == 1:
                return answer(args, bad)
            return answer(args, repository(pub_id, pub_dest, private=False))

        gh.overrides[("GET", f"repos/{pub_dest}", True)] = mismatch_then_match
        result, receipt = run("repo-transfer", gh, repo="public-move")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
        assert "unexpected visibility" in receipt["detail"]
        assert len(gh.posts) == 1 and len(mismatch_polls) == 1 and sleeps == list(pub.TRANSFER_VERIFY_DELAYS[:1])
        assert pub_journal.exists()
        pub_journal.unlink()

    # Visibility flipped to private at destination readback exits 3 unverified, keeps journal, and stays unreconciled
    # until destination is observed public with the pinned id.
    gh = public_gh()
    gh.overrides[("GET", f"repos/{pub_dest}", True)] = lambda args: answer(
        args, repository(pub_id, pub_dest, private=True),
    )
    result, receipt = run("repo-transfer", gh, repo="public-move")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
    assert len(gh.posts) == 1 and pub_journal.exists()
    original_journal = pub_journal.read_text()
    gh.overrides.clear()
    gh.repos = {pub_dest: repository(pub_id, pub_dest, private=True)}
    result, receipt = run("repo-transfer", gh, repo="public-move")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
    assert "does not show it public" in receipt["detail"] and len(gh.posts) == 1 and pub_journal.exists()
    # The destination and catalog can agree with each other while disagreeing with the pending transfer.
    public_row = CATALOG["public-move"]
    with monkeypatch.context() as catalog_change:
        catalog_change.setitem(CATALOG, "public-move", {**public_row, "visibility": "private", "role": "private-labs"})
        for dry_run in (False, True):
            result, receipt = run("repo-transfer", gh, repo="public-move", dry_run=dry_run)
            assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
            assert "binds public visibility" in receipt["detail"]
            assert len(gh.posts) == 1 and pub_journal.read_text() == original_journal
    assert json.loads(original_journal)["visibility"] == "public"
    gh.repos = {pub_dest: repository(pub_id, pub_dest, private=False)}
    result, receipt = run("repo-transfer", gh, repo="public-move")
    assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
    assert len(gh.posts) == 1 and not pub_journal.exists()


@pytest.mark.parametrize("repo_key,slug,repo_id,expected_visibility,changed_doc", [
    ("public-move", f"{OLD}/pub", 222, "public", repository(222, f"{OLD}/pub", private=True)),
    (
        "public-move", f"{OLD}/pub", 222, "public",
        {**repository(222, f"{OLD}/pub", private=False), "visibility": "private"},
    ),
    (
        "public-move", f"{OLD}/pub", 222, "public",
        {**repository(222, f"{OLD}/pub", private=True), "visibility": "public"},
    ),
    ("infra-private", SOURCE, REPO_ID, "private", repository(REPO_ID, SOURCE, private=False)),
    (
        "infra-private", SOURCE, REPO_ID, "private",
        {**repository(REPO_ID, SOURCE, private=True), "visibility": "public"},
    ),
])
def test_transfer_refuses_visibility_change_between_check_and_post(
    tmp_path, selector, repo_key, slug, repo_id, expected_visibility, changed_doc,
):
    """Defect class: source visibility flipping between preflight check and pre-POST re-read reaching POST."""
    selector["slug"] = slug
    expected_private = expected_visibility == "private"
    gh = FakeGitHub()
    gh.repos = {slug: repository(repo_id, slug, private=expected_private)}
    reads = []

    def respond(args):
        reads.append(args)
        if len(reads) == 1:
            return answer(args, repository(repo_id, slug, private=expected_private))
        return answer(args, changed_doc)

    gh.overrides[("GET", f"repos/{slug}", False)] = respond
    pending_journal = state_dir(tmp_path) / f"repo-transfer-{repo_id}.pending"
    with pytest.raises(gate.PublishBlocked, match=f"no longer holds the pinned {expected_visibility} repository"):
        run("repo-transfer", gh, repo=repo_key)
    assert len(reads) == 2 and gh.posts == [] and not pending_journal.exists()
    assert gh.calls[-1] == ["gh", "api", "--method", "GET", "user"]


def test_unregistered_and_single_slug_public_repositories_refused(monkeypatch, selector):
    """Defect class: an unregistered public repository, single-slug public row, or visibility/role mismatch admitted."""
    unregistered_slug = f"{OLD}/unregistered-public"
    gh = FakeGitHub()
    gh.repos = {
        unregistered_slug: repository(777, unregistered_slug, private=False),
        f"{OLD}/public": repository(778, f"{OLD}/public", private=False),
    }

    # Unregistered alias and raw owner/name slug are refused before any GitHub call.
    for key, reason in (
        ("unregistered-public", "not a repository key"),
        (unregistered_slug, "invalid publisher field repo"),
        ("public", "no alternate_github"),
    ):
        selector["slug"] = unregistered_slug
        with pytest.raises(gate.PublishBlocked, match=reason):
            run("repo-transfer", gh, repo=key)
    assert gh.calls == [] and gh.posts == []

    # Dual-slug rows whose declared visibility disagrees with their role prefix fail closed.
    bad_catalog = {
        **CATALOG,
        "pub-bad-role": {
            "github": f"{OLD}/pub-bad", "alternate_github": f"{NEW}/pub-bad",
            "repository_id": 333, "visibility": "public", "local_name": "pub-bad", "role": "private-labs",
        },
        "priv-bad-role": {
            "github": f"{OLD}/priv-bad", "alternate_github": f"{NEW}/priv-bad",
            "repository_id": 334, "visibility": "private", "local_name": "priv-bad", "role": "public-labs",
        },
        "pub-no-prefix": {
            "github": f"{OLD}/pub-raw", "alternate_github": f"{NEW}/pub-raw",
            "repository_id": 335, "visibility": "public", "local_name": "pub-raw", "role": "machinery",
        },
    }
    monkeypatch.setattr(gate, "catalog", lambda: bad_catalog)
    for key in ("pub-bad-role", "priv-bad-role", "pub-no-prefix"):
        with pytest.raises(gate.PublishBlocked, match="visibility matching its role"):
            run("repo-transfer", gh, repo=key)
    assert gh.calls == [] and gh.posts == []


def _second_source_read(gh, second):
    """The source slug holds the pinned id at preflight; the pre-POST re-read answers *second*."""
    reads = []

    def respond(args):
        reads.append(args)
        if len(reads) == 1:
            return answer(args, repository(REPO_ID, SOURCE))
        if isinstance(second, BaseException):
            raise second
        return answer(args, second)
    gh.overrides[("GET", f"repos/{SOURCE}", False)] = respond


def _journal_unremovable(monkeypatch):
    """os.unlink refuses the transfer journal, as on a directory made read-only mid-run."""
    unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if str(path).endswith(".pending"):
            raise PermissionError("read-only directory")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", refuse)


def _swapped_before_post(gh, _monkeypatch):
    _second_source_read(gh, repository(4242, SOURCE))


def _rate_limited_before_post(gh, _monkeypatch):
    _second_source_read(gh, github_client.GitHubRateLimited(None, "pool_exhausted"))


def _swapped_and_journal_unremovable(gh, monkeypatch):
    _second_source_read(gh, repository(4242, SOURCE))
    _journal_unremovable(monkeypatch)


@pytest.mark.parametrize("change,reason", [
    (lambda gh, _: gh.repos[SOURCE].update(id=999), "not the pinned"),
    (lambda gh, _: gh.repos[SOURCE].update(private=False, visibility="public"), "is not private"),
    (lambda gh, _: gh.repos[SOURCE].update(archived=True), "is archived"),
    (lambda gh, _: gh.repos[SOURCE].update(permissions={"admin": False}), "not an admin"),
    (lambda gh, _: gh.repos[SOURCE].update(full_name=f"{OLD}/elsewhere"), "neither at"),
    (_swapped_before_post, "no longer holds the pinned"),
    (_rate_limited_before_post, 75),
    (_swapped_and_journal_unremovable, "journal-retained"),
])
def test_transfer_source_identity_refusals(tmp_path, monkeypatch, change, reason):
    """Defect class: a stale or substituted source identity passing the fresh match, before or at the POST.

    A refusal before the POST leaves no journal and keeps its typed exit (2, or 75 for a rate limit);
    a journal it cannot remove is reported and exits 3.
    """
    gh = FakeGitHub()
    change(gh, monkeypatch)
    if reason == 75:
        result, _ = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == 75
    elif reason == "journal-retained":
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "journal-retained"
        assert "no POST was sent" in receipt["detail"] and gh.posts == [] and journal(tmp_path).exists()
        return
    else:
        with pytest.raises(gate.PublishBlocked, match=reason):
            run("repo-transfer", gh, repo="infra-private")
    assert gh.posts == [] and not journal(tmp_path).exists()


@pytest.mark.parametrize("change,reason", [
    (_taken(DESTINATION), "holds repository 5"),
    (lambda gh: gh.overrides.update({("GET", f"repos/{DESTINATION}", False): server_error}), "unreadable"),
    (lambda gh: gh.memberships[NEW].update(role="member"), "not an active admin"),
    (lambda gh: gh.memberships.pop(NEW), "not an active admin"),
])
def test_transfer_destination_preflight_refusals(change, reason):
    """Defect class: a transfer onto an occupied name or into an organization without creation rights."""
    gh = FakeGitHub()
    change(gh)
    with pytest.raises(gate.PublishBlocked, match=reason):
        run("repo-transfer", gh, repo="infra-private")
    assert gh.posts == []


def test_transfer_sends_once_and_verifies_exact_readback(tmp_path, monkeypatch):
    """Defect class: a duplicate transfer, or success reported without the exact destination readback.

    A receipt file that cannot be written does not turn a no-mutation success (dry run, already
    transferred) into a refusal.
    """
    gh = FakeGitHub()
    result, receipt = run("repo-transfer", gh, repo="infra-private", dry_run=True)
    assert result.returncode == 0 and gh.posts == [] and not journal(tmp_path).exists()
    assert receipt["preflight"]["no_visible_collisions"] == [DESTINATION]
    with monkeypatch.context() as patch:
        patch.setattr(pub, "_write_transfer_receipt", lambda sink, receipt: False)
        result, receipt = run("repo-transfer", gh, repo="infra-private", dry_run=True)
        assert result.returncode == 0 and receipt["outcome"] == "dry-run"
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == 0 and receipt["outcome"] == "transferred"
    assert gh.posts == [(f"repos/{SOURCE}/transfer", {"new_owner": NEW, "new_name": "infra-next"})]
    assert gh.find(DESTINATION)["id"] == REPO_ID
    assert receipt["principal"]["before"] == receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}
    assert not journal(tmp_path).exists()
    with monkeypatch.context() as patch:
        patch.setattr(pub, "_write_transfer_receipt", lambda sink, receipt: False)
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == 0 and receipt["outcome"] == "already-transferred" and len(gh.posts) == 1


def test_transfer_settles_after_async_accept(tmp_path, monkeypatch):
    """Defect class: async 202 transfer acceptance rejected or settled without bounded destination and principal verification.

    GitHub's POST /repos/{source}/transfer returns 202 Accepted with minimal-repository (where visibility
    may be omitted and full_name may still name the source or an intermediate owner/name pair) while the
    transfer runs asynchronously. repo-transfer must poll GET /repos/{destination} within TRANSFER_VERIFY_DELAYS
    (8 reads over 180 s), re-read GET /user after the last GitHub read, and settle exit 0 with one POST once
    the pinned private repository appears; it must fail closed (exit 3, journal kept) on bound expiry, a
    switched final principal, or a non-private / unrelated POST answer.
    """
    source_owner, source_name = SOURCE.split("/", 1)
    dest_owner, dest_name = DESTINATION.split("/", 1)

    def _run_async(ack_doc, *, appear_on_poll=3, after_login=None):
        sleeps = []
        monkeypatch.setattr(pub, "_transfer_sleep", sleeps.append)
        gh = FakeGitHub(after_login=after_login)
        gh.overrides[("POST", f"repos/{SOURCE}/transfer", False)] = lambda args: answer(args, ack_doc)
        polls = []

        def destination_poll(args):
            polls.append(args)
            if appear_on_poll is not None and len(polls) >= appear_on_poll:
                gh.repos.pop(SOURCE, None)
                moved = repository(REPO_ID, DESTINATION)
                gh.repos[DESTINATION] = moved
                return answer(args, moved)
            return not_found(args)

        gh.overrides[("GET", f"repos/{DESTINATION}", True)] = destination_poll
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        return result, receipt, gh, polls, sleeps

    # Async 202 acceptance shapes: source without optional visibility, source with visibility,
    # and both intermediate owner/name pairs during an async transfer + rename.
    for ack_doc, appear_on in (
        ({"id": REPO_ID, "full_name": SOURCE, "private": True}, 3),
        (repository(REPO_ID, SOURCE), 3),
        ({"id": REPO_ID, "full_name": f"{dest_owner}/{source_name}", "private": True}, 2),
        ({"id": REPO_ID, "full_name": f"{source_owner}/{dest_name}", "private": True}, 2),
    ):
        result, receipt, gh, polls, sleeps = _run_async(ack_doc, appear_on_poll=appear_on)
        assert result.returncode == 0, receipt
        assert receipt["outcome"] == "transferred" and receipt["mutations_sent"] == 1
        assert len(gh.posts) == 1 and len(polls) == appear_on
        assert sleeps == list(pub.TRANSFER_VERIFY_DELAYS[:appear_on])
        assert gh.calls[-1] == ["gh", "api", "--method", "GET", "user"]
        assert receipt["principal"]["before"] == receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}
        assert not journal(tmp_path).exists()

    # Bound exhausted: destination never appears across all 8 polls (180 s) -> exit 3, journal kept, one POST.
    result, receipt, gh, polls, sleeps = _run_async(
        {"id": REPO_ID, "full_name": SOURCE, "private": True}, appear_on_poll=None,
    )
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
    assert len(gh.posts) == 1 and len(polls) == len(pub.TRANSFER_VERIFY_DELAYS)
    assert sleeps == list(pub.TRANSFER_VERIFY_DELAYS)
    assert "8 reads over 180 s" in receipt["detail"]
    assert journal(tmp_path).exists()
    journal(tmp_path).unlink()

    # F14 re-read-last: destination appears on poll 3, then principal switches (login or account id) before final
    # GET /user -> exit 3, journal kept.
    result, receipt, gh, polls, sleeps = _run_async(
        {"id": REPO_ID, "full_name": SOURCE, "private": True}, appear_on_poll=3, after_login="someone-else",
    )
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
    assert len(gh.posts) == 1 and len(polls) == 3 and "someone-else" in receipt["detail"]
    assert journal(tmp_path).exists()
    journal(tmp_path).unlink()

    sleeps = []
    monkeypatch.setattr(pub, "_transfer_sleep", sleeps.append)
    gh = FakeGitHub()
    gh.overrides[("POST", f"repos/{SOURCE}/transfer", False)] = lambda args: answer(
        args, {"id": REPO_ID, "full_name": SOURCE, "private": True},
    )
    polls = []

    def appear_on_third(args):
        polls.append(args)
        if len(polls) >= 3:
            gh.repos.pop(SOURCE, None)
            moved = repository(REPO_ID, DESTINATION)
            gh.repos[DESTINATION] = moved
            return answer(args, moved)
        return not_found(args)

    gh.overrides[("GET", f"repos/{DESTINATION}", True)] = appear_on_third
    gh.overrides[("GET", "user", True)] = lambda args: answer(args, {"login": PRINCIPAL, "id": 22})
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
    assert len(gh.posts) == 1 and len(polls) == 3 and "principal id changed" in receipt["detail"]
    assert journal(tmp_path).exists()
    journal(tmp_path).unlink()

    # Fail-closed POST answers: explicit non-private visibility, null visibility, private=False, or unrelated slug.
    for bad_ack in (
        {"id": REPO_ID, "full_name": SOURCE, "private": True, "visibility": "public"},
        {"id": REPO_ID, "full_name": SOURCE, "private": True, "visibility": None},
        {"id": REPO_ID, "full_name": SOURCE, "private": False},
        {"id": REPO_ID, "full_name": "other-org/other-repo", "private": True},
    ):
        result, receipt, gh, polls, sleeps = _run_async(bad_ack, appear_on_poll=1)
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
        assert len(gh.posts) == 1 and polls == [] and sleeps == [] and journal(tmp_path).exists()
        journal(tmp_path).unlink()

    # Transient public visibility on destination poll 1 followed by private on poll 2 stops on poll 1 and keeps journal.
    sleeps = []
    monkeypatch.setattr(pub, "_transfer_sleep", sleeps.append)
    gh = FakeGitHub()
    mismatch_polls = []

    def public_then_private(args):
        mismatch_polls.append(args)
        return answer(args, repository(REPO_ID, DESTINATION, private=len(mismatch_polls) > 1))

    gh.overrides[("GET", f"repos/{DESTINATION}", True)] = public_then_private
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
    assert "unexpected visibility" in receipt["detail"]
    assert len(gh.posts) == 1 and len(mismatch_polls) == 1 and sleeps == list(pub.TRANSFER_VERIFY_DELAYS[:1])
    assert journal(tmp_path).exists()
    journal(tmp_path).unlink()


def _applied_then(gh, reply):
    """A POST answer that applies the transfer and then answers *reply* (a document or an exception)."""
    def respond(args):
        gh.post(args, f"repos/{SOURCE}/transfer", {"new_owner": NEW, "new_name": "infra-next"})
        if isinstance(reply, BaseException):
            raise reply
        return answer(args, reply)
    return respond


class _Unwritable:
    """A stdout whose every write fails, like a closed pipe."""

    def write(self, _text):
        raise OSError("closed")

    def flush(self):
        raise OSError("closed")

    def fileno(self):
        raise OSError("closed")


@pytest.mark.parametrize("outcome", [
    server_error,
    subprocess.TimeoutExpired(["gh"], 60),
    "public-readback",
    "rate-limited-readback",
    "empty-acknowledgement",
    "other-id-acknowledgement",
    "old-slug-reused",
    "stdout-unwritable",
    "pending-destination-server-error",
    "pending-destination-malformed",
    "pending-destination-rate-limited",
    "reconciled-journal-unremovable",
    "pending-selector-flipped",
    "pending-selector-unbound",
    "pending-selector-crashed",
    "pending-directory-unusable",
    "pending-state-home-changed",
])
def test_unsettled_transfer_blocks_retry_until_reconciled(tmp_path, monkeypatch, selector, outcome):
    """Defect class: a second send after an unknown outcome, or a post-send failure reported as nothing landed.

    While a journal is pending, only a verified destination match settles it (exit 0); every other outcome,
    including an unreadable destination, exits 3 and sends nothing.
    """
    gh = FakeGitHub()
    transfer = ("POST", f"repos/{SOURCE}/transfer", False)
    if outcome == "pending-state-home-changed":
        # The real resolvers: the account's password-database home, whatever XDG_STATE_HOME says
        # (a HOME other than that home is refused before any read, T1).
        import pwd
        from types import SimpleNamespace

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path / "home")))
        monkeypatch.setattr(pub, "_repo_admin_directory", REAL_STATE_DIRECTORY)
        monkeypatch.setattr(pub, "_account_home", REAL_ACCOUNT_HOME)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-a"))
        gh.overrides[transfer] = subprocess.TimeoutExpired(["gh"], 60)
        result, _ = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED
        gh.overrides.clear()
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-b"))
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
        assert len(gh.posts) == 1
        assert (tmp_path / "home/.local/state/learn-ukrainian/repo-admin" / f"repo-transfer-{REPO_ID}.pending").exists()
        return
    if isinstance(outcome, str) and outcome.startswith(("pending-", "reconciled-")):
        gh.overrides[transfer] = subprocess.TimeoutExpired(["gh"], 60)
        result, _ = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and journal(tmp_path).exists()
        gh.overrides.clear()
        if outcome == "pending-selector-unbound":
            selector["slug"] = None
        if outcome == "pending-selector-crashed":
            # A selector failure outside FleetRepoError (the parser's RecursionError) is judged after the journal.
            selector["error"] = RecursionError("maximum recursion depth exceeded")
        if outcome == "pending-directory-unusable":
            state_dir(tmp_path).chmod(0o750)
        if outcome in {"pending-selector-unbound", "pending-selector-crashed", "pending-directory-unusable"}:
            try:
                result, receipt = run("repo-transfer", gh, repo="infra-private")
            finally:
                state_dir(tmp_path).chmod(0o700)
            assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
            assert len(gh.posts) == 1 and journal(tmp_path).exists()
            return
        if outcome == "pending-selector-flipped":
            # A premature selector flip does not hide the pending transfer; the destination still settles it.
            selector["slug"] = DESTINATION
            result, receipt = run("repo-transfer", gh, repo="infra-private")
            assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
            gh.repos = {DESTINATION: repository(REPO_ID, DESTINATION)}
            result, receipt = run("repo-transfer", gh, repo="infra-private")
            assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
            assert len(gh.posts) == 1 and not journal(tmp_path).exists()
            return
        if outcome == "reconciled-journal-unremovable":
            gh.repos = {DESTINATION: repository(REPO_ID, DESTINATION)}
            _journal_unremovable(monkeypatch)
            result, receipt = run("repo-transfer", gh, repo="infra-private")
            assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
            assert receipt["journal_retained"] is True and journal(tmp_path).exists() and len(gh.posts) == 1
            return
        gh.overrides[("GET", f"repos/{DESTINATION}", True)] = {
            "pending-destination-server-error": server_error,
            "pending-destination-malformed": lambda args: answer(args, {"id": "not-a-number"}),
            "pending-destination-rate-limited": github_client.GitHubRateLimited(None, "pool_exhausted"),
        }[outcome]
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
        assert len(gh.posts) == 1 and journal(tmp_path).exists()
        return
    if outcome == "old-slug-reused":
        # The response is lost, then a new repository takes the old name: the exact destination still settles it.
        gh.overrides[transfer] = _applied_then(gh, subprocess.TimeoutExpired(["gh"], 60))
        result, _ = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and journal(tmp_path).exists()
        gh.overrides.clear()
        gh.repos[SOURCE] = repository(4242, SOURCE)
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
        assert len(gh.posts) == 1 and not journal(tmp_path).exists()
        return
    if outcome == "stdout-unwritable":
        monkeypatch.setattr("sys.stdout", _Unwritable())
        assert pub.main(["repo-transfer", "--repo", "infra-private", "--principal", PRINCIPAL], runner=gh) == 3
        receipts = sorted(state_dir(tmp_path).glob(f"repo-transfer-{REPO_ID}-*.json"))
        assert len(gh.posts) == 1 and json.loads(receipts[-1].read_text())["outcome"] == "transferred"
        return
    if outcome == "empty-acknowledgement":
        gh.overrides[transfer] = _applied_then(gh, {})
    elif outcome == "other-id-acknowledgement":
        gh.overrides[transfer] = _applied_then(gh, repository(4242, SOURCE))
    elif outcome == "public-readback":
        gh.overrides[("GET", f"repos/{DESTINATION}", True)] = lambda args: answer(
            args, repository(REPO_ID, DESTINATION, private=False),
        )
    elif outcome == "rate-limited-readback":
        gh.overrides[("GET", f"repos/{DESTINATION}", True)] = github_client.GitHubRateLimited(None, "pool_exhausted")
    else:
        gh.overrides[transfer] = outcome
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and len(gh.posts) == 1 and journal(tmp_path).exists()
    gh.overrides.clear()
    gh.repos = {SOURCE: repository(REPO_ID, SOURCE)}
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
    assert len(gh.posts) == 1 and journal(tmp_path).exists()
    gh.repos = {DESTINATION: repository(REPO_ID, DESTINATION)}
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
    assert len(gh.posts) == 1 and not journal(tmp_path).exists()


def test_pending_transfer_bound_to_original_principal(tmp_path):
    """Defect class: an exit 0 or a journal removal resting on a principal read older than the last GitHub read.

    The account id changes after the POST, so the first run is unsettled; while GitHub still reports the
    other id, or the journal's binding is missing, unreadable or names another login, a rerun exits 3 and
    keeps the journal. Only the original login and id settle it. Every exit 0 without a POST (pending
    settlement, already transferred with no journal, either verb's dry run) and every pre-POST journal
    removal re-reads GET /user after its last GitHub read: a switched login or id, or a failed re-read,
    exits 3 and keeps the journal.
    """
    gh = FakeGitHub()
    other_account = lambda args: answer(args, {"login": PRINCIPAL, "id": 22})  # noqa: E731
    gh.overrides[("GET", "user", True)] = other_account
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unverified"
    assert gh.find(DESTINATION)["id"] == REPO_ID and journal(tmp_path).exists()
    for dry_run in (False, True):
        result, receipt = run("repo-transfer", gh, repo="infra-private", dry_run=dry_run)
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled"
        assert "(id 7)" in receipt["detail"] and "(id 22)" in receipt["detail"] and journal(tmp_path).exists()
    bound = journal(tmp_path).read_text()
    bound_principal = {"login": PRINCIPAL, "id": 7}
    assert json.loads(bound)["principal"] == bound_principal
    del gh.overrides[("GET", "user", True)]
    for content, mode in [
        ("{}", 0o600),
        ("not json", 0o600),
        (json.dumps({"principal": {"login": "someone-else", "id": 7}, "visibility": "private"}), 0o600),
        (json.dumps({"principal": {"login": PRINCIPAL, "id": True}, "visibility": "private"}), 0o600),
        (json.dumps({"principal": bound_principal}), 0o600),
        (json.dumps({"principal": bound_principal, "visibility": ["private"]}), 0o600),
        (json.dumps({"principal": bound_principal, "visibility": "internal"}), 0o600),
        (bound, 0o644),
    ]:
        journal(tmp_path).write_text(content)
        journal(tmp_path).chmod(mode)
        result, receipt = run("repo-transfer", gh, repo="infra-private")
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "unreconciled", content
        assert journal(tmp_path).exists()
    journal(tmp_path).write_text(bound)
    journal(tmp_path).chmod(0o600)
    other_login = lambda args: answer(args, {"login": "someone-else", "id": 7})  # noqa: E731

    def switched(fake, later, dry_run, *, verb="repo-transfer", **fields):
        """One run whose preflight GET /user is the principal and whose next GET /user answers *later*."""
        calls, posts = [], len(fake.posts)
        fake.overrides[("GET", "user", bool(posts))] = lambda args: (
            calls.append(args) or (answer(args, {"login": PRINCIPAL, "id": 7}) if len(calls) == 1 else later(args))
        )
        result, receipt = run(verb, fake, repo="infra-private", dry_run=dry_run, **fields)
        del fake.overrides[("GET", "user", bool(posts))]
        assert result.returncode == pub.REPO_ADMIN_UNSETTLED and len(calls) == 2 and len(fake.posts) == posts
        assert receipt["principal"]["after"] == (None if later is server_error else json.loads(later([]).stdout))
        return receipt

    # The account switches after the preflight read: the re-read at settlement must still be the starter.
    for later in (other_account, other_login, server_error):
        for dry_run in (False, True):
            assert switched(gh, later, dry_run)["outcome"] == "unreconciled" and journal(tmp_path).exists()
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == 0 and receipt["outcome"] == "already-transferred"
    assert receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}
    assert receipt["journal_principal"] == {"login": PRINCIPAL, "id": 7} and not journal(tmp_path).exists()
    assert len(gh.posts) == 1
    # Already transferred with no journal, and both verbs' dry runs: the same final re-read, nothing created.
    fresh = FakeGitHub()
    for later in (other_account, other_login, server_error):
        for dry_run in (False, True):
            assert switched(gh, later, dry_run)["outcome"] == "unverified" and not journal(tmp_path).exists()
        assert switched(fresh, later, True)["outcome"] == "unverified" and not journal(tmp_path).exists()
        receipt = switched(fresh, later, True, verb="repo-create", name="rehearsal-1")
        assert receipt["outcome"] == "unverified" and fresh.find(f"{OLD}/rehearsal-1") is None
    for fake, verb, fields, outcome in (
        (gh, "repo-transfer", {}, "already-transferred"),
        (fresh, "repo-transfer", {}, "dry-run"),
        (fresh, "repo-create", {"name": "rehearsal-1"}, "dry-run"),
    ):
        result, receipt = run(verb, fake, repo="infra-private", dry_run=True, **fields)
        assert result.returncode == 0 and receipt["outcome"] == outcome
        assert receipt["principal"]["before"] == receipt["principal"]["after"] == {"login": PRINCIPAL, "id": 7}
    assert len(gh.posts) == 1 and fresh.posts == []
    # A refusal before the POST (would be 2, or 75) removes the journal only under the same re-read principal.
    for second in (repository(4242, SOURCE), github_client.GitHubRateLimited(None, "pool_exhausted")):
        for later in (other_account, other_login, server_error):
            refused = FakeGitHub()
            _second_source_read(refused, second)
            receipt = switched(refused, later, False)
            assert receipt["outcome"] == "unreconciled" and "no POST was sent" in receipt["detail"]
            assert journal(tmp_path).exists() and refused.posts == []
            journal(tmp_path).unlink()


def _edit(**changes):
    def apply(path, tmp_path):
        document = json.loads(path.read_text())
        for key, value in changes.items():
            if key == "id":
                document["repository"]["id"] = value
            elif key == "slug":
                document["repository"]["slug"] = value
            else:
                document[key] = value
        path.write_text(json.dumps(document))
        return path
    return apply


def _moved(path, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    os.replace(path, target / path.name)
    return target / path.name


def _readable(path, tmp_path):
    path.chmod(0o644)
    return path


def _symlink(path, tmp_path):
    link = path.with_name("link.json")
    link.symlink_to(path)
    return link


def _relative(path, tmp_path):
    return Path(os.path.relpath(path))


def _replaced(path, tmp_path):
    return path


@pytest.mark.parametrize("tamper,reason", [
    (_moved, "must name a receipt in"),
    (_readable, "mode 0600"),
    (_symlink, "cannot be opened"),
    (_relative, "absolute path"),
    (_edit(outcome="dry-run"), "not a created fixture"),
    (_edit(id=REPO_ID), "names a registered repository"),
    (_edit(rehearsal_destination=DESTINATION), "not a created fixture"),
    (_edit(rehearsal_destination=f"{NEW}/rehearsal-1-moved-2"), "not a created fixture"),
    (_edit(slug="elsewhere/rehearsal-1"), "not a created fixture"),
    (_edit(mutations_sent=True), "not a created fixture"),
    (lambda path, tmp_path: path.parent / "..", "mode 0600 file"),
    (_replaced, "not the pinned"),
    (None, None),
])
def test_rehearsal_transfer_bound_to_create_receipt(tmp_path, tamper, reason):
    """Defect class: rehearsal authority taken from a forged, edited, linked or name-matched receipt.

    The untampered receipt transfers exactly its id to exactly its recorded destination.
    """
    gh = FakeGitHub()
    created, path = create_fixture(gh)
    if tamper is None:
        result, receipt = run("repo-transfer", gh, repo="infra-private", create_receipt=str(path))
        assert result.returncode == 0 and receipt["outcome"] == "transferred" and receipt["mode"] == "rehearsal"
        assert gh.posts[1] == (
            f"repos/{OLD}/rehearsal-1/transfer", {"new_owner": NEW, "new_name": "rehearsal-1-moved"},
        )
        assert receipt["repository_id"] == created["repository"]["id"] == gh.find(f"{NEW}/rehearsal-1-moved")["id"]
        assert gh.find(SOURCE)["id"] == REPO_ID
        return
    if tamper is _replaced:
        # The fixture name now holds another repository: the receipt's id, not the name, binds the transfer.
        gh.repos[f"{OLD}/rehearsal-1"]["id"] = 4242
    with pytest.raises(gate.PublishBlocked, match=reason):
        run("repo-transfer", gh, repo="infra-private", create_receipt=str(tamper(path, tmp_path)))
    assert len(gh.posts) == 1


@pytest.mark.parametrize("verb", ["repo-create", "repo-transfer"])
def test_help_documents_examples_outputs_and_exit_codes(capsys, verb):
    """Check the CLI help standard and that successful in-process calls write to the caller's streams."""
    with pytest.raises(SystemExit) as exit_:
        pub.main([verb, "--help"])
    out = capsys.readouterr().out
    assert exit_.value.code == 0
    for section in (
        "Examples:", "Outputs:", "Exit codes:", "Related:", "--principal", "--dry-run", "UNSETTLED", "LU_PRODUCT_ROOT",
    ):
        assert section in out
    assert "omitted by default" not in out
    assert "run as the destination organization owner" in out
    assert "every repository in that organization" in out
    assert "visible collisions only" in out
    with pytest.raises(SystemExit) as exit_:
        pub.main(["ruleset-merge-queue", "--invalid"])
    assert exit_.value.code == 2 and "invalid publisher arguments" in capsys.readouterr().err
    gh = FakeGitHub()
    argv = [verb, "--repo", "infra-private", "--principal", PRINCIPAL, *(["--name", "rehearsal-1"] if verb == "repo-create" else [])]
    for extra, outcome in ((["--dry-run"], "dry-run"), ([], "created" if verb == "repo-create" else "transferred")):
        assert pub.main([*argv, *extra], runner=gh) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["outcome"] == outcome and f"{verb}: receipt " in captured.err


@pytest.mark.parametrize("verb,fields,lock", [
    ("repo-transfer", {}, f"repo-transfer-{REPO_ID}.lock"),
    ("repo-create", {"name": "rehearsal-1"}, "repo-create-rehearsal-1.lock"),
])
def test_concurrent_run_lock_refuses_without_sending(tmp_path, verb, fields, lock):
    """Defect class: a second run acting on reads taken before the first run's mutation."""
    import fcntl

    directory = state_dir(tmp_path)
    directory.mkdir(mode=0o700, parents=True)
    with open(directory / lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        gh = FakeGitHub()
        result, receipt = run(verb, gh, repo="infra-private", **fields)
    assert result.returncode == pub.REPO_ADMIN_UNSETTLED and receipt["outcome"] == "in-progress"
    assert gh.calls == []


# A child interpreter whose stdout and stderr (or only one of them) are pipes with no reader, so every
# write fails; "descriptorless" replaces stdout with a failing wrapper that has no descriptor to redirect,
# "over-descriptor" with a failing wrapper over the valid descriptor 1, "fails-later" with a stream that
# fails on the final flush, and "bufferless" with a working stream that has no binary
# buffer; "closed" closes descriptor 1 before the CLI runs, and "closed-stdout-object" / "closed-stderr-object"
# close that stream object; "fileno-valueerror" gives stdout a fileno() that raises ValueError (argparse
# probes it while building the parser).
_BROKEN_STREAMS_CHILD = """
import importlib.util, io, os, runpy, sys
streams = os.environ["REPO_ADMIN_TEST_STREAMS"]
read_end, write_end = os.pipe()
os.close(read_end)
for descriptor, broken in ((1, "stdout"), (2, "stderr")):
    if streams in {"both", broken}:
        os.dup2(write_end, descriptor)


class Descriptorless(io.TextIOBase):
    def write(self, _text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


if streams == "descriptorless":
    sys.stdout = Descriptorless()
if streams == "bufferless":
    sys.stdout = io.StringIO()


class OverDescriptor(Descriptorless):
    def fileno(self):
        return 1


class FailsLater(io.TextIOBase):
    def __init__(self, inner):
        self.inner, self.failing = inner, False

    def write(self, text):
        return self.inner.write(text)

    def flush(self):
        if self.failing:
            raise BrokenPipeError(32, "Broken pipe")
        return self.inner.flush()

    def fileno(self):
        return self.inner.fileno()


if streams == "over-descriptor":
    sys.stdout = OverDescriptor()
if streams == "fails-later":
    sys.stdout = FailsLater(sys.stdout)
if streams == "closed":
    sys.stdout.flush()
    os.close(1)
if streams == "closed-stdout-object":
    sys.stdout.close()


class FilenoValueError(io.TextIOWrapper):
    def fileno(self):
        raise ValueError("no descriptor")


if streams == "fileno-valueerror":
    sys.stdout = FilenoValueError(io.BytesIO(), encoding="utf-8")
if streams == "closed-stderr-object":
    sys.stderr.close()
spec = importlib.util.spec_from_file_location("repo_admin_fixture", sys.argv[1])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from lu_machinery.opsec import prepublish as gate
from lu_machinery.orchestration import fleet_repos
from lu_machinery.publish import github as pub
gate.catalog = lambda: fixture.CATALOG
pub._transfer_sleep = lambda _delay: None
pub._repo_admin_directory = lambda: os.environ["REPO_ADMIN_TEST_DIRECTORY"]
pub._account_home = lambda: os.environ["HOME"]
fleet_repos.resolve_active_fleet_repo = lambda identifier, *, repos, env=None, **_: fleet_repos.ActiveFleetRepo(
    repo=repos[identifier], active_github=fixture.SOURCE,
)
if streams == "fails-later":
    settle = pub._repo_admin_result

    def settled_then_failing(*args, **kwargs):
        result = settle(*args, **kwargs)
        sys.stdout.failing = True
        return result

    pub._repo_admin_result = settled_then_failing
gh = fixture.FakeGitHub()
if os.environ.get("REPO_ADMIN_TEST_RATE_LIMIT"):
    from lu_machinery.common import github_client
    gh.overrides[("GET", "user", False)] = github_client.GitHubRateLimited(None, "pool_exhausted")
if sys.argv[2] == "ruleset-merge-queue":
    import json, subprocess
    guarded = pub._stream_write

    def recorded(name, text):
        with open(os.path.join(os.environ["REPO_ADMIN_TEST_MARKERS"], "guard-calls"), "a") as handle:
            handle.write(name + "\\n")
        return guarded(name, text)

    pub._stream_write = recorded
    gate.check_texts = lambda *_args, **_kwargs: None
    ruleset = {
        "id": 7, "name": "queue", "target": "branch", "enforcement": "active", "bypass_actors": [],
        "conditions": {"ref_name": {"exclude": [], "include": ["refs/heads/main"]}},
        "rules": [{"type": "merge_queue", "parameters": {"min_entries_to_merge_wait_minutes": 0}}],
    }

    def gh(args, **_kwargs):
        if args[args.index("--method") + 1] == "PUT":
            open(os.path.join(os.environ["REPO_ADMIN_TEST_MARKERS"], "ruleset-put"), "w").close()
            raise subprocess.TimeoutExpired(args, 30)
        return subprocess.CompletedProcess(args, 0, json.dumps(ruleset), "")
publish = pub.publish

def publish_with_fake_gh(verb, *, runner=None, **fields):
    return publish(verb, runner=gh, **fields)

pub.publish = publish_with_fake_gh
sys.argv[1:] = sys.argv[2:]
runpy.run_module("lu_machinery.publish", run_name="__main__", alter_sys=True)
"""


_RULESET = ["ruleset-merge-queue", "--repo", f"{OLD}/public", "--ruleset", "7", "--min-entries-to-merge-wait-minutes", "1"]


_CREATE = ["repo-create", "--repo", "infra-private", "--name", "rehearsal-1", "--principal", PRINCIPAL]
_TRANSFER = ["repo-transfer", "--repo", "infra-private", "--principal", PRINCIPAL]
_CREATED, _TRANSFERRED = "repo-create-rehearsal-1-*.json", f"repo-transfer-{REPO_ID}-*.json"


@pytest.mark.parametrize("argv,pattern,limited,streams,code", [
    (_CREATE, _CREATED, False, "both", 3),
    (_TRANSFER, _TRANSFERRED, False, "both", 3),
    (_TRANSFER, _TRANSFERRED, True, "both", 75),
    (_TRANSFER, _TRANSFERRED, False, "stderr", 3),
    (_CREATE, _CREATED, False, "descriptorless", 3),
    (_TRANSFER, _TRANSFERRED, True, "bufferless", 75),
    (["repo-transfer", "--help"], None, False, "stdout", 0),
    (_CREATE, _CREATED, False, "over-descriptor", 3),
    (_CREATE, _CREATED, False, "fails-later", 3),
    (["repo-create", "--help"], None, False, "closed", 0),
    (["repo-create", "--help"], None, False, "closed-stdout-object", 0),
    (["repo-transfer", "--invalid"], None, False, "closed-stderr-object", 2),
    (["--", "repo-create", "--help"], None, False, "stdout", 0),
    (["--", *_CREATE], _CREATED, False, "fails-later", 3),
    (["repo-create", "--help"], None, False, "fileno-valueerror", 0),
    (["repo-transfer", "--invalid"], None, False, "fileno-valueerror", 2),
    (_RULESET, None, False, "stderr", 120),
])
def test_broken_output_streams_keep_the_exit_code(tmp_path, argv, pattern, limited, streams, code):
    """Defect class: interpreter shutdown overriding the documented exit code after a failed stream write.

    After a POST it stays 3, whichever stream failed, whether or not it has a descriptor, and also when
    the failure first appears at the final flush (never 0 or 120); a rate limit before any POST
    stays 75, also on a stream without a binary buffer (never 2); --help stays 0 and invalid arguments
    stay 2, also with stdout's descriptor or a stream object closed, a fileno() that raises, or a leading
    "--" before the verb. The repo-admin output guard never reaches another verb: a ruleset-merge-queue
    PUT that timed out with a broken stderr keeps the code the shared main() gives it.
    """
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {*WORKER_MARKERS, *pub.REPO_ADMIN_CREDENTIAL_OVERRIDES, "GH_HOST", "PYTEST_CURRENT_TEST"}
    }
    # The child imports the same lu_machinery as this test.
    root = str(Path(pub.__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [root, environment.get("PYTHONPATH")]))
    environment["REPO_ADMIN_TEST_DIRECTORY"] = str(state_dir(tmp_path))
    environment["REPO_ADMIN_TEST_STREAMS"] = streams
    environment["REPO_ADMIN_TEST_MARKERS"] = str(tmp_path)
    if limited:
        environment["REPO_ADMIN_TEST_RATE_LIMIT"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", _BROKEN_STREAMS_CHILD, __file__, *argv],
        env=environment, cwd=tmp_path, capture_output=True, timeout=120, check=False,
    )
    assert completed.returncode == code, completed.stderr
    if argv is _RULESET:
        # Unchanged from main() before the repo-admin verbs: the shared path leaves the failed stderr
        # to interpreter shutdown (120); the repo-admin guard, had it run, would have made it 2.
        assert (tmp_path / "ruleset-put").exists() and not (tmp_path / "guard-calls").exists()
    if pattern is None:
        return
    receipts = sorted(state_dir(tmp_path).glob(pattern))
    assert receipts and json.loads(receipts[-1].read_text())["mutations_sent"] == (0 if limited else 1)
