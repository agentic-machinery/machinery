"""repo-create and repo-transfer (GH-ADMIN-VERBS-1124): a fake gh runner and test-owned registry rows; no network."""

from __future__ import annotations

import json
import os
import subprocess
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
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    chosen = {"slug": SOURCE}

    def active(identifier, *, repos, env=None, **_):
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
    return Path(os.environ["XDG_STATE_HOME"]) / "learn-ukrainian" / pub.REPO_ADMIN_DIRECTORY


CALLER = [
    *[(marker, "task-1") for marker in WORKER_MARKERS],
    *[(name, "/caller/value") for name in pub.REPO_ADMIN_CREDENTIAL_OVERRIDES],
    ("GH_HOST", "ghe.example.com"),
]


@pytest.mark.parametrize("verb,fields", [("repo-create", {"name": "rehearsal-1"}), ("repo-transfer", {})])
@pytest.mark.parametrize("name,value", CALLER)
def test_caller_authority_and_credential_overrides_refused_before_any_read(monkeypatch, verb, fields, name, value):
    """Defect class: a caller-controlled identity or credential (worker marker, token, gh config, host) reaching GitHub."""
    monkeypatch.setenv(name, value)
    gh = FakeGitHub()
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


def test_repo_create_sends_once_and_receipt_binds_id(tmp_path):
    """Defect class: a duplicate or missing creation, or a receipt that does not bind the created id."""
    gh = FakeGitHub()
    result, receipt = run("repo-create", gh, repo="infra-private", name="rehearsal-1", dry_run=True)
    assert result.returncode == 0 and receipt["outcome"] == "dry-run" and gh.posts == []
    result, receipt = run("repo-create", gh, repo="infra-private", name="rehearsal-1")
    assert result.returncode == 0 and receipt["outcome"] == "created"
    assert gh.posts == [(f"orgs/{OLD}/repos", {"name": "rehearsal-1", "private": True, "auto_init": True})]
    assert receipt["repository"] == {"slug": f"{OLD}/rehearsal-1", "id": 901, "private": True, "visibility": "private"}
    assert receipt["rehearsal_destination"] == f"{NEW}/rehearsal-1-moved"
    assert receipt["principal"]["before"]["login"] == receipt["principal"]["after"]["login"] == PRINCIPAL
    files = sorted(state_dir(tmp_path).glob("repo-create-rehearsal-1-*.json"))
    assert len(files) == 2 and all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert json.loads(files[-1].read_text()) == receipt


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


def test_real_move_derives_endpoints_from_selector_row(selector):
    """Defect class: transfer endpoints taken from caller input, from a non-private row, or in reverse."""
    gh = FakeGitHub()
    result, receipt = run("repo-transfer", gh, repo="infra-private", dry_run=True)
    assert result.returncode == 0 and gh.posts == []
    assert (receipt["mode"], receipt["source"], receipt["destination"], receipt["repository_id"]) == (
        "move", SOURCE, DESTINATION, REPO_ID,
    )
    for key, slug, reason in [
        ("infra-private", DESTINATION, "already names"),
        ("infra-private", None, "selector cannot bind"),
        ("single", SOURCE, "no alternate_github"),
        ("public-move", SOURCE, "not a private repository"),
        ("nope", SOURCE, "not a repository key"),
    ]:
        selector["slug"] = slug
        gh = FakeGitHub()
        with pytest.raises(gate.PublishBlocked, match=reason):
            run("repo-transfer", gh, repo=key)
        assert gh.posts == []


@pytest.mark.parametrize("change,reason", [
    (lambda row: row.update(id=999), "not the pinned"),
    (lambda row: row.update(private=False, visibility="public"), "is not private"),
    (lambda row: row.update(archived=True), "is archived"),
    (lambda row: row.update(permissions={"admin": False}), "not an admin"),
    (lambda row: row.update(full_name=f"{OLD}/elsewhere"), "neither at"),
])
def test_transfer_source_identity_refusals(change, reason):
    """Defect class: a stale or substituted source identity passing the fresh match."""
    gh = FakeGitHub()
    change(gh.repos[SOURCE])
    with pytest.raises(gate.PublishBlocked, match=reason):
        run("repo-transfer", gh, repo="infra-private")
    assert gh.posts == []


@pytest.mark.parametrize("change,reason", [
    (_taken(DESTINATION), "is taken"),
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


def test_transfer_sends_once_and_verifies_exact_readback(tmp_path):
    """Defect class: a duplicate transfer, or success reported without the exact destination readback."""
    gh = FakeGitHub()
    result, receipt = run("repo-transfer", gh, repo="infra-private", dry_run=True)
    assert result.returncode == 0 and gh.posts == [] and not journal(tmp_path).exists()
    result, receipt = run("repo-transfer", gh, repo="infra-private")
    assert result.returncode == 0 and receipt["outcome"] == "transferred"
    assert gh.posts == [(f"repos/{SOURCE}/transfer", {"new_owner": NEW, "new_name": "infra-next"})]
    assert gh.find(DESTINATION)["id"] == REPO_ID and receipt["principal"]["after"]["login"] == PRINCIPAL
    assert not journal(tmp_path).exists()


@pytest.mark.parametrize("outcome", [
    server_error,
    subprocess.TimeoutExpired(["gh"], 60),
    "public-readback",
    "rate-limited-readback",
])
def test_unsettled_transfer_blocks_retry_until_reconciled(tmp_path, outcome):
    """Defect class: a second send after an unknown outcome, or a post-send rate limit reported as nothing landed."""
    gh = FakeGitHub()
    if outcome == "public-readback":
        gh.overrides[("GET", f"repos/{DESTINATION}", True)] = lambda args: answer(
            args, repository(REPO_ID, DESTINATION, private=False),
        )
    elif outcome == "rate-limited-readback":
        gh.overrides[("GET", f"repos/{DESTINATION}", True)] = github_client.GitHubRateLimited(None, "pool_exhausted")
    else:
        gh.overrides[("POST", f"repos/{SOURCE}/transfer", False)] = outcome
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
    """Defect class: --help drifting from the CLI help standard (no examples, outputs, exit codes or option help)."""
    with pytest.raises(SystemExit) as exit_:
        pub.main([verb, "--help"])
    out = capsys.readouterr().out
    assert exit_.value.code == 0
    for section in ("Examples:", "Outputs:", "Exit codes:", "Related:", "--principal", "--dry-run", "UNSETTLED"):
        assert section in out
    assert "omitted by default" not in out
