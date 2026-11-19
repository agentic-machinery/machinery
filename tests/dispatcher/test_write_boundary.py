"""Read-only isolation against real bubblewrap and a disposable primary with one linked worktree."""

import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lu_machinery.dispatcher import write_boundary
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.opsec.test_git_push_scan import push_sandbox  # noqa: F401

_ALL_DATA_ROOTS = write_boundary._DATA_ROOTS
# One of GitHub's published address ranges; the launcher reads the real ones on the host.
_GITHUB_RANGE = "140.82.112.0/20"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def primary(tmp_path, monkeypatch):
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    (repo / "a.txt").write_text("primary\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    worktree = repo / ".worktrees" / "task"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "task")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(tmp_path / "scratch"))
    # GitHub's published address ranges, which the launcher reads with the host login in production.
    monkeypatch.setattr(write_boundary, "github_networks", lambda: (ipaddress.ip_network(_GITHUB_RANGE),))
    # The test's own scripts and fixtures live below its temporary root, which may be a data root the view
    # empties (/var/tmp): keep that one root visible, every other data root stays emptied.
    monkeypatch.setattr(write_boundary, "_DATA_ROOTS",
                        tuple(root for root in write_boundary._DATA_ROOTS if not tmp_path.resolve().is_relative_to(root)))
    return repo, worktree, tmp_path / "receipt.json"


def _run(primary, script: str, *extra: str) -> tuple[int, dict]:
    _, worktree, receipt = primary
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), *extra, "--", "/bin/sh", "-c", script,
    ])
    return code, json.loads(receipt.read_text())


def _digest(*paths: Path) -> dict:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}


@pytest.mark.parametrize("failure", ["setup", "canary", "interpreter", "key-helper", "profile-store", "plane"])
def test_setup_or_canary_failure_refuses_and_never_starts_the_provider(primary, monkeypatch, failure, tmp_path):
    """Defect class: a boundary that cannot start, or whose canary finds a writable primary, still launches the
    provider (an unsandboxed fallback), so the run proceeds without containment; or it starts with the Sources
    interpreter below a writable private root, whose parent a worker can move aside to plant its own (PR 528 F2);
    or with a Claude key helper whose input it cannot name, so the helper reads an emptied path, or with a
    Claude profile store the CLI would prefer to the selected login (PR 528 F3)."""
    repo, worktree, _ = primary
    extra = []
    if failure == "setup":
        monkeypatch.setattr(write_boundary, "unavailable", lambda: "bubblewrap (bwrap) is not installed")
    elif failure == "interpreter":
        monkeypatch.setattr(write_boundary, "_interpreter_root", lambda: Path("/tmp/tools/python"))
    elif failure == "key-helper":
        login = tmp_path / "home" / ".claude-api"
        login.mkdir(parents=True)
        (login / "settings.json").write_text(json.dumps({"apiKeyHelper": "sh -c 'cat \"$KEY_FILE\"'"}))
        extra = ["--claude-login", str(login)]
    elif failure == "plane":
        monkeypatch.setenv("FLEET_COMMS_ROOT", "relative/plane")
    elif failure == "profile-store":
        login = tmp_path / "home" / ".claude-b"
        login.mkdir(parents=True)
        (tmp_path / "home" / ".config" / "anthropic").mkdir(parents=True)
        (tmp_path / "home" / ".config" / "anthropic" / "active_config").write_text("other")
        extra = ["--claude-login", str(login)]
    else:
        planned = write_boundary._Boundary._probes

        def leaky(self, *args):
            # The canary reports a writable primary file: the boundary does not hold.
            return [*planned(self, *args), {"op": "write_open", "path": str(repo / "a.txt"), "expect_any": ["OK"]}]

        monkeypatch.setattr(write_boundary._Boundary, "_probes", leaky)
    marker = worktree / "provider-started"
    code, receipt = _run(primary, f"touch {marker}", *extra)
    assert code == write_boundary.REFUSED
    assert receipt["status"] == "refused"
    assert {"canary": "canary failed", "setup": "not installed", "interpreter": "writable private root",
            "key-helper": "key helper", "profile-store": "profile store",
            "plane": "FLEET_COMMS_ROOT is relative"}[failure] in receipt["reason"]
    assert not marker.exists()
    assert not write_boundary.scratch("wb-test").exists()


@pytest.mark.parametrize("disk", ["writable-file", "read-only-files", "read-only-files-and-root"])
def test_canary_probes_the_bind_with_a_writable_tracked_file_not_a_read_only_first_one(primary, disk):
    """Defect class: the canary probes the first tracked file even when it is read-only on disk, so the kernel's
    permission check (EACCES) answers before the read-only bind (EROFS) and every reviewer is refused; or it
    refuses every run once the primary write guard has made each tracked file read-only, its intended steady
    state, although the absent-path probes in the owner-writable repository root still prove the bind; or, with
    neither a tracked file nor the root writable, the canary passes or runs without having tested the bind."""
    repo, worktree, _ = primary
    first = repo / ".first.txt"  # git ls-files lists it before a.txt
    first.write_text("read-only on disk\n")
    _git(repo, "add", first.name)
    _git(repo, "commit", "-qm", "read-only first file")
    first.chmod(0o444)
    if disk != "writable-file":
        (repo / "a.txt").chmod(0o444)
    marker = repo.parent / "provider-started"
    root_mode = repo.stat().st_mode
    if disk == "read-only-files-and-root":
        repo.chmod(root_mode & ~0o222)
    try:
        code, receipt = _run(primary, f"echo started > {marker}", "--output", str(marker))
    finally:
        repo.chmod(root_mode)
    if disk == "read-only-files-and-root":
        assert code == write_boundary.REFUSED and receipt["status"] == "refused"
        assert "no tracked regular file and no repository root directory the host user may write" in receipt["reason"]
        assert not marker.exists()
        return
    assert code == 0 and receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    tracked = [probe for probe in receipt["canary"]["probes"] if probe["op"] in ("write_open", "chmod")
               and Path(probe["path"]).parent == repo]
    assert {(probe["op"], probe["path"], probe["outcome"]) for probe in tracked} == (
        {("write_open", str(repo / "a.txt"), "EROFS"), ("chmod", str(repo / "a.txt"), "EROFS")}
        if disk == "writable-file" else set())
    absent = {(probe["op"], probe["outcome"]) for probe in receipt["canary"]["probes"]
              if Path(probe["path"]).parent == repo and Path(probe["path"]).name.startswith(".write-boundary-canary-")}
    assert absent == {(op, "EROFS") for op in ("create", "mkdir", "rename", "unlink")}
    assert marker.exists()


def test_protected_primary_git_state_cannot_be_changed(primary):
    """Defect class: a reviewer changes the primary's HEAD, index, configuration, hooks or checked-out branch,
    directly or through Git porcelain, so later host-side Git commands in the primary run its code or state."""
    repo, worktree, _ = primary
    git_dir = repo / ".git"
    protected = [git_dir / "HEAD", git_dir / "index", git_dir / "config", git_dir / "refs/heads/main",
                 git_dir / "hooks/post-checkout"]
    before = _digest(*protected)
    script = (
        f"git -C {repo} config core.hooksPath /tmp/evil; git -C {repo} checkout -q -b evil;"
        f"git -C {repo} commit -q --allow-empty -m evil; echo 'ref: refs/heads/evil' > {git_dir}/HEAD;"
        f": > {git_dir}/index; printf '#!/bin/sh\\n' > {git_dir}/hooks/post-checkout;"
        f"git -C {repo} update-ref refs/heads/main HEAD; echo 0000 > {git_dir}/refs/heads/main;"
        f"echo 'gitdir: {git_dir}' > {worktree}/.git"
    )
    _, receipt = _run(primary, script)  # the provider's own exit status is not the subject; the host state is
    assert receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    assert _digest(*protected) == before
    assert _git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    hooks_path = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], capture_output=True)
    assert hooks_path.returncode == 1
    assert "evil" not in _git(repo, "branch", "--list")
    assert (worktree / ".git").read_text().startswith(f"gitdir: {git_dir / 'worktrees'}")


_BROKER_CLIENT = """
import json, os, socket, sys

def send(verb, payload, request_id):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(os.environ["LU_WRITE_BOUNDARY_BROKER"])
        connection.sendall(json.dumps({"verb": verb, "request_id": request_id, "payload": payload}).encode())
        connection.shutdown(socket.SHUT_WR)
        return json.loads(b"".join(iter(lambda: connection.recv(65536), b"")))["returncode"]

good = {"name": "pull", "number": 7}
print(json.dumps({
    "other_repo": send("github-read", {**good, "repo": "other/repo"}, "1" * 32),
    "bad_sha": send("github-read", {"name": "checks", "sha": "bad"}, "2" * 32),
    "extra_field": send("github-read", {**good, "endpoint": "other"}, "3" * 32),
    "unknown_verb": send("run", {"argv": ["/bin/sh"]}, "4" * 32),
    "good": send("github-read", good, "5" * 32),
    "replay": send("github-read", good, "5" * 32),
}))
"""


def test_broker_refuses_forged_or_replayed_requests(primary, monkeypatch, tmp_path):
    """Defect class: the read broker accepts a forged repository, malformed request or replayed request."""
    _, worktree, receipt = primary
    monkeypatch.setattr(write_boundary._Broker, "_repository_slug", lambda self: "owner/repo")
    host_steps = []
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None, cwd=None: host_steps.append(
        (module, argv)) or {"returncode": 0, "stdout": "", "stderr": ""})
    client = tmp_path / "client.py"
    client.write_text(_BROKER_CLIENT)
    report = tmp_path / "broker-results.json"
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--output", str(report), "--",
        "/bin/sh", "-c", f"{sys.executable} -I {client} > {report}",
    ])
    assert code == 0, json.loads(receipt.read_text())
    results = json.loads(report.read_text())
    assert results == {"other_repo": 2, "bad_sha": 2, "extra_field": 2, "unknown_verb": 2, "good": 0, "replay": 2}
    assert host_steps == [("publish.github", ["read", "pull", "--repo", "owner/repo", "--number", "7"])]


# -- publication through the broker (decision PUBLISH-CRED-1113, #434) ------------------------------------------------

_SENTINEL = "ghp_PROBE_SENTINEL_0123456789abcdefABCDEF01"
# The last name is not GitHub's but resolves to one of GitHub's published addresses (a DNS alias).
_GITHUB_HOSTS = ("github.com", "api.github.com", "uploads.github.com", "ssh.github.com", "codeload.github.com",
                 "ghcr.io", "alias.example.org")

_CREDENTIAL_PROBE = """
import json, os, socket, subprocess, sys
sentinel, paths, report = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
cells = {}

def holds(path):
    try:
        if os.path.isdir(path):
            return any(holds(os.path.join(path, name)) for name in os.listdir(path))
        with open(path, "rb") as handle:
            return sentinel.encode() in handle.read()
    except OSError:
        return False

cells["environment"] = not any(sentinel in value for value in os.environ.values()) and not any(
    name.startswith(("GH_", "GITHUB_")) for name in os.environ)
for name, path in paths.items():
    cells["file:" + name] = not holds(path)
fds = []
for fd in os.listdir("/proc/self/fd"):
    try:
        fds.append(holds(os.readlink(f"/proc/self/fd/{fd}")) or holds(f"/proc/self/fd/{fd}"))
    except OSError:
        pass
cells["inherited_descriptors"] = not any(fds)
proxy = os.environ.get("HTTPS_PROXY", "")
host, _, port = proxy.removeprefix("http://").rstrip("/").partition(":")
for name in json.loads(sys.argv[4]):
    with socket.create_connection((host, int(port)), timeout=10) as connection:
        connection.sendall(f"CONNECT {name}:443 HTTP/1.1\\r\\nHost: {name}:443\\r\\n\\r\\n".encode())
        cells["connect:" + name] = connection.recv(64).startswith(b"HTTP/1.1 403")
push = subprocess.run(["git", "-C", os.getcwd(), "push", "https://github.com/owner/repo.git", "HEAD:refs/heads/probe"],
                      capture_output=True, timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
cells["git_push"] = push.returncode != 0
for name, url in (("rest", "https://api.github.com/repos/owner/repo/issues"), ("graphql", "https://api.github.com/graphql")):
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=20)
        cells[name] = False
    except Exception:
        cells[name] = True
open(report, "w").write(json.dumps(cells))
"""


@pytest.mark.parametrize("mode,route", [("read-only", route) for route in ("claude", "codex", "cursor", "opencode")])
def test_no_github_write_authority_is_reachable_inside_any_route(primary, monkeypatch, tmp_path, route, mode):
    """Defect class: a reviewer recovers the host's GitHub login inside the boundary (from the environment, gh's
    configuration or an alternate one, Git credential files, netrc, SSH keys or an inherited descriptor) or reaches
    GitHub directly (CLI, REST, GraphQL or Git, by GitHub's names or another name resolving to its addresses), so it
    writes to GitHub without the broker's ownership and gates."""
    _, worktree, receipt = primary
    home = Path(write_boundary.os.environ["HOME"])
    stores = {"gh": home / ".config/gh/hosts.yml", "alternate_gh": tmp_path / "alt-gh/hosts.yml",
              "xdg_gh": tmp_path / "xdg/gh/hosts.yml", "git_credentials": home / ".git-credentials",
              "xdg_git_credentials": tmp_path / "xdg/git/credentials", "netrc": home / ".netrc",
              "ssh_key": home / ".ssh/id_ed25519"}
    for path in stores.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"oauth_token: {_SENTINEL}\n")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "alt-gh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, _SENTINEL)
    from lu_machinery.agent_runtime.attempt_network import AttemptEgress

    # Every name here resolves into GitHub's published ranges (the ``primary`` fixture supplies them).
    async def resolve(self, host):
        return ["140.82.112.3"]

    monkeypatch.setattr(AttemptEgress, "_resolve", resolve)
    credential = {"claude": home / ".claude/.credentials.json", "codex": home / ".codex/auth.json",
                  "agy": home / ".agy/token", "opencode": home / ".local/share/opencode/auth.json",
                  "cursor": home / ".config/cursor/auth.json"}.get(route)
    extra = []
    if credential is not None:  # provider refreshes persist for both authors and reviewers
        credential.parent.mkdir(parents=True, exist_ok=True)
        credential.write_text("{}")
        extra = ["--credential", str(credential)]
    # Codex runs on a task-local CODEX_HOME: the Sources server and the selected login, nothing of the real home.
    home_check, refreshed = "", str(credential)
    if route == "codex":
        (home / ".codex/config.toml").write_text('[mcp_servers.foreign]\nurl = "http://127.0.0.1:8766/mcp"\n')
        extra += ["--codex-auth", str(credential)]
        # Beside TMPDIR, never in it: Codex creates no helper PATH aliases under its temporary directory.
        home_check = (' && case "$CODEX_HOME/" in "$TMPDIR"/*) false;; esac'
                      f' && test "$(readlink "$CODEX_HOME/auth.json")" = {credential}'
                      ' && test "$(ls -A "$CODEX_HOME")" = "$(printf \'auth.json\\nconfig.toml\')"'
                      ' && grep -qFx "mcp_optional_startup_grace_ms = 0" "$CODEX_HOME/config.toml"'
                      ' && ! grep -q foreign "$CODEX_HOME/config.toml"'
                      ' && ! (echo "[mcp_servers.planted]" >> "$CODEX_HOME/config.toml") 2>/dev/null'
                      ' && grep -qF \'"/run/lu-write-boundary/sources-stdio.py", "/run/lu-write-boundary/sources.sock"]\''
                      ' "$CODEX_HOME/config.toml"')
        refreshed = '"$CODEX_HOME/auth.json"'  # a refresh through the link lands in the selected login
    # An AGY, Cursor or OpenCode write worker, and a read-only Cursor run, reads one task-local MCP file naming only
    # the Sources relay, never the host's own (planted here with a foreign server), and cannot rewrite it.
    sources_mcp = {"agy": "$HOME/.gemini/config/mcp_config.json", "cursor": "$HOME/.cursor/mcp.json",
                   "opencode": '"$OPENCODE_CONFIG"'}.get(route) if mode == "workspace-write" or route == "cursor" else None
    if sources_mcp is not None:
        for planted in (".gemini/config/mcp_config.json", ".cursor/mcp.json", ".config/opencode/opencode.json"):
            (home / planted).parent.mkdir(parents=True, exist_ok=True)
            (home / planted).write_text('{"mcpServers": {"foreign": {"url": "http://127.0.0.1:8766/mcp"}}}\n')
        extra += ["--sources-mcp", route]
        home_check = ((' && test "$OPENCODE_CONFIG" = /run/lu-write-boundary/sources-mcp.json' if route == "opencode"
                       else ' && test -z "$OPENCODE_CONFIG"')
                      + f" && grep -qF '\"/run/lu-write-boundary/sources.sock\"' {sources_mcp}"
                      + f" && ! grep -q foreign {sources_mcp} && ! (echo planted >> {sources_mcp}) 2>/dev/null")
        if mode == "read-only":  # with its permission profile, which Cursor rewrites through a file beside it
            profile = "$HOME/.cursor/cli-config.json"
            home_check += (f" && grep -qF 'Shell(git:push*)' {profile}"
                           f" && cp {profile} {profile}.1.tmp && mv {profile}.1.tmp {profile}")
    probe, report = tmp_path / "probe.py", tmp_path / "cells.json"
    probe.write_text(_CREDENTIAL_PROBE)
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--mode", mode, "--output", str(report), *extra, "--", "/bin/sh", "-c",
        f"python3 -I {probe} {_SENTINEL} '{json.dumps({k: str(v) for k, v in stores.items()})}' {report} "
        f"'{json.dumps(_GITHUB_HOSTS)}'"
        # The Sources proxy every provider names sits at its fixed path inside (sources_command), and its
        # directory cannot be renamed away and replaced by one the worker controls.
        " && test -r /run/lu-write-boundary/sources-stdio.py && ! mv /run/lu-write-boundary /run/moved 2>/dev/null"
        " && test -r /run/lu-write-boundary/sources-stdio.py"
        + home_check + (f" && echo refreshed > {refreshed}" if credential else ""),
    ])
    result = json.loads(receipt.read_text())
    assert code == 0 and result["status"] == "ran", result
    if route == "codex":  # the canary itself proved the trusted Sources definition read-only before Codex started
        assert [probe["outcome"] for probe in result["canary"]["probes"] if probe["op"] == "write_open"
                and probe["path"].endswith("/codex-home/config.toml")] == ["EROFS"], result["canary"]
    cells = json.loads(report.read_text())
    denominator = 1 + len(stores) + 1 + len(_GITHUB_HOSTS) + 3
    assert len(cells) == denominator and all(cells.values()), cells
    # Zero bytes reached any GitHub host: each CONNECT was refused before a connection existed.
    github = [row for row in result["egress"] if row["destination"].split(":")[0] in _GITHUB_HOSTS]
    assert len(github) == len(_GITHUB_HOSTS) and all(row.get("refused") and row["bytes"] == 0 for row in github)
    assert all(path.read_text() == f"oauth_token: {_SENTINEL}\n" for path in stores.values())
    if credential is not None:
        assert credential.read_text() == "refreshed\n"
    if route == "codex":
        assert result["codex_home"] == {"config_sha256": hashlib.sha256(write_boundary.codex_home_config().encode())
                                        .hexdigest(), "auth": "linked", "auth_writable": True}
    if sources_mcp is not None:
        config = write_boundary.sources_mcp_config(route)
        assert result["sources_mcp"] == {"provider": route, "config_sha256": hashlib.sha256(config.encode()).hexdigest()}
        assert [probe["outcome"] for probe in result["canary"]["probes"] if probe["op"] == "write_open"
                and probe["path"].endswith(("/mcp_config.json", "/.cursor/mcp.json", "/sources-mcp.json"))] == ["EROFS"]
        # Exactly one server, the Sources relay, in the shape the provider reads.
        servers = json.loads(config)["mcp" if route == "opencode" else "mcpServers"]
        assert len(servers) == 1, servers
        (server,) = servers.values()
        command = write_boundary.sources_command()
        assert ([*server["command"]] if route == "opencode" else [server["command"], *server["args"]]) == command
        assert ("cursor_permissions" in result) == (mode == "read-only")
    else:
        assert "sources_mcp" not in result


class _Launch:
    """The launch facts the broker reads: the task, its branch and author model, the primary and its scratch."""

    def __init__(self, repo: Path, scratch: Path, tip: str):
        self.args = type("Args", (), {"task_id": "wb-test", "author_model": "claude-opus-5-5",
                                     "mode": "read-only"})()
        self.branch, self.repository, self.common, self.base, self.tip = "refs/heads/task", repo, repo / ".git", scratch, tip
        self.target = self.branch  # the publication branch: a continuation's PR branch, else the dispatched one

    def current_tip(self) -> str:
        return self.tip


def test_read_only_broker_refuses_publication_and_preserves_checkout(primary, monkeypatch, tmp_path):
    """Defect class: a reviewer publishes through the host broker or writes reviewed files/Git metadata."""
    repo, worktree, receipt = primary
    report = tmp_path / "review-results.json"
    tip = _git(worktree, "rev-parse", "HEAD")
    before = _digest(worktree / "a.txt", repo / ".git/config", repo / ".git/refs/heads/task")
    steps = []
    monkeypatch.setattr(write_boundary._Broker, "_repository_slug", lambda self: "owner/repo")
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None, cwd=None: steps.append(
        (module, argv)) or {"returncode": 0, "stdout": "read-ok", "stderr": ""})
    probe = tmp_path / "review-probe.py"
    probe.write_text(_BROKER_CLIENT[:_BROKER_CLIENT.index("good =")] + '''
verbs = sys.argv[2].split(",")
results = {verb: send(verb, {}, f"{index:032x}") for index, verb in enumerate(verbs)}
results["github-read"] = send("github-read", {"name": "pull", "number": 7}, "f" * 32)
for name, path in (("worktree", "a.txt"), ("git", sys.argv[3])):
    try:
        with open(path, "a") as stream:
            stream.write("changed")
        results[name] = False
    except OSError:
        results[name] = True
open(sys.argv[1], "w").write(json.dumps(results))
''')
    verbs = ["review-dispatch", "review-wait", "budget-check", "record-verdict", "fleet-publish", "fleet-deliveries",
             "closeout-request", "push", "fetch-base", "pr-create", "pr-edit", "pr-comment", "pr-merge", "pr-ownership"]
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--mode", "read-only", "--output", str(report), "--", sys.executable, "-I",
        str(probe), str(report), ",".join(verbs), str(repo / ".git/config"),
    ])
    evidence = json.loads(receipt.read_text())
    assert code == 0 and evidence["canary"]["passed"], evidence
    assert json.loads(report.read_text()) == {**dict.fromkeys(verbs, 2), "github-read": 0, "worktree": True, "git": True}
    assert len(steps) == 1 and steps[0][0] == "publish.github"
    assert _digest(worktree / "a.txt", repo / ".git/config", repo / ".git/refs/heads/task") == before
    assert evidence["import"] == {"status": "unchanged", "before": tip, "after": tip}
    task_file = receipt.with_suffix(".task.json")
    write_boundary.receipt_file(task_file).write_text(receipt.read_text())
    assert write_boundary.verify(task_file, "wb-test", "read-only")[1] is None
    assert write_boundary.verify(task_file, "wb-test", "workspace-write")[1] == (
        "write_boundary_unproven: the receipt names another mode")


@pytest.mark.parametrize("login_root", ["home", "emptied-root"])
def test_declared_resources_in_masked_roots_stay_read_only_without_host_socket_access(primary, monkeypatch, tmp_path,
                                                                                    login_root):
    """Defect class: private tmpfs hides declared resources, or restoring them exposes host sockets/writes; or the
    view restores more than decision A4 allows, so a worker reads another provider's login, the operator's private
    state, another task's records or a checkout's secrets, and sends them out over its public egress; or it hides
    what the worker needs (the selected login, the toolchain, its own staged inputs); or a product commit's
    pre-commit hooks find no installed environment and try to fetch one from GitHub, which egress refuses."""
    import socket

    from lu_machinery.dispatcher import runtime_context

    repo, worktree, receipt = primary
    home = Path(os.environ["HOME"])
    # A short sibling of tmp_path: the AF_UNIX socket below it must fit in 108 bytes, so this
    # cannot live under the numbered test directory. The case name is part of the hash because
    # retention deletes a passed case's directory and the next case reuses that path.
    masked = tmp_path.parent / (
        "ro-" + hashlib.sha256(f"{login_root}\0{tmp_path}".encode()).hexdigest()[:8]
    )
    # The selected login below HOME, or below an emptied root outside it, with its key helper's file beside it.
    login = home / ".claude" if login_root == "home" else masked / "logins" / "b"
    # The key file inside the login directory itself, or below an emptied root of its own.
    keys = login / "keys" if login_root == "home" else masked / "keys"
    # pre-commit's store in HOME's .cache, or behind a .cache link (review F1 of PR 538).
    cache = home / ".cache" if login_root == "home" else home / "cache-volume"
    if login_root != "home":
        cache.mkdir()
        (home / ".cache").symlink_to("cache-volume")
    for path, text in ((home / ".codex/auth.json", "other login"), (home / "ops-state/notes", "private"),
                       (login / "projects/other.jsonl", "transcript"), (login / ".credentials.json", "selected"),
                       (home / ".local/bin/tool", "toolchain"), (keys / "selected", "selected key"),
                       (cache / "pre-commit/repo/hook", "hook env"), (cache / "other/data", "other"),
                       (keys / "other", "other key"),
                       (login / "settings.json", json.dumps({"env": {}, "apiKeyHelper": f"cat {keys}/selected"})),
                       (login / ".claude.json", json.dumps({"oauthAccount": {
                           "accountUuid": "u", "organizationUuid": "o", "emailAddress": "probe@example.invalid"},
                           "projects": {"/elsewhere": {}}}))):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    tasks = repo / "batch_state" / "tasks"
    (tasks / "wb-test.inputs").mkdir(parents=True)
    (tasks / "wb-test.inputs" / "brief.md").write_text("own input")
    (tasks / "other-task.result").write_text("another task")
    (repo / ".secrets").mkdir()
    (repo / ".secrets" / "key").write_text("secret")
    # A Fleet plane named with ~, a parent step and a link inside the restored primary is emptied where Fleet
    # resolves it (PR 528 F1).
    (repo / "dedicated-plane").mkdir()
    (repo / "dedicated-plane" / "private-record").write_text("private")
    (repo / "plane-link").symlink_to("dedicated-plane")
    assert repo.parent == home.parent
    monkeypatch.setenv("FLEET_COMMS_ROOT", f"~/../{repo.name}/plane-link")
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.setattr(write_boundary, "_HOME_TOOLCHAIN", (".local/bin",))
    monkeypatch.delenv("PRE_COMMIT_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    shared = masked / "shared"
    shared.mkdir(parents=True)
    (shared / "resource").write_text("declared")
    (masked / "hidden").write_text("sibling")
    monkeypatch.setattr(write_boundary, "_MASKED_ROOTS", (*write_boundary._MASKED_ROOTS, masked))
    # HOME named through a link inside the checkout's emptied state, its login named through that link too
    # (PR 528 F3).
    (repo / "batch_state" / "home-link").symlink_to(home)
    monkeypatch.setenv("HOME", str(repo / "batch_state" / "home-link"))
    named_login = repo / "batch_state" / "home-link" / login.relative_to(home) if login_root == "home" else login
    monkeypatch.setattr(runtime_context, "shared_root", lambda: shared)
    client = tmp_path / "resource-probe.py"
    client.write_text("""
import json, os, socket, sys
from pathlib import Path
shared, masked, report = map(Path, sys.argv[1:4])
assert (shared / "resource").read_text() == "declared"
assert not (masked / "hidden").exists()
try:
    (shared / "resource").open("w")
except OSError as exc:
    assert exc.errno == 30
else:
    raise AssertionError("declared resource writable")
with socket.socket(socket.AF_UNIX) as connection:
    try:
        connection.connect(str(shared / "host.sock"))
    except OSError:
        pass
    else:
        raise AssertionError("host socket reachable")
store = Path(os.environ["PRE_COMMIT_HOME"])
assert (store / "repo/hook").read_text() == "hook env"
try:
    (store / "repo/hook").open("w")
except OSError as exc:
    assert exc.errno == 30
else:
    raise AssertionError("pre-commit store writable")
assert not (store.parent / "other").exists()
home, repo, login, keys = Path(os.environ["HOME"]), Path(sys.argv[4]), Path(sys.argv[5]), Path(sys.argv[6])
view = {relative: (home / relative).exists() for relative in (".codex/auth.json", "ops-state", ".local/bin/tool",
                                                            ".cache/other")}
view.update({"login/" + relative: (login / relative).exists() for relative in (
    "projects", ".credentials.json", "settings.json")})
view.update({"keys/" + relative: (keys / relative).exists() for relative in ("selected", "other")})
# The selected login's global config holds only what its launch guard binds.
view["global_config"] = json.loads((login / ".claude.json").read_text())
view.update({relative: (repo / relative).exists() for relative in (
    "batch_state/tasks/other-task.result", ".secrets/key", "batch_state/tasks/wb-test.inputs/brief.md", "a.txt",
    "plane-link/private-record", "dedicated-plane/private-record")})

assert view == {".codex/auth.json": False, "ops-state": False, "login/projects": False,
                "login/.credentials.json": True, ".local/bin/tool": True, "login/settings.json": True,
                ".cache/other": False,
                "keys/selected": True, "keys/other": False,
                "global_config": {"oauthAccount": {"accountUuid": "u", "organizationUuid": "o"}},
                "batch_state/tasks/other-task.result": False, "plane-link/private-record": False,
                "dedicated-plane/private-record": False,
                ".secrets/key": False, "batch_state/tasks/wb-test.inputs/brief.md": True, "a.txt": True}, view
report.write_text("resources-readable-writes-and-sockets-denied")
""")
    report = tmp_path / "resource-result"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(shared / "host.sock"))
        listener.listen()
        code = write_boundary.main([
            "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
            "--receipt", str(receipt), "--mode", "read-only", "--output", str(report),
            "--credential", str(named_login / ".credentials.json"), "--claude-login", str(named_login),
            "--", sys.executable, "-I",
            str(client), str(shared), str(masked), str(report), str(repo), str(login), str(keys),
        ])
    evidence = json.loads(receipt.read_text())
    assert code == 0, evidence
    assert report.read_text() == "resources-readable-writes-and-sockets-denied"
    assert (shared / "resource").read_text() == "declared"
    # The canary itself checked the hidden paths for absence before the provider started.
    absent = {probe["path"] for probe in evidence["canary"]["probes"] if probe["expect_any"] == ["ENOENT"]}
    assert {str(home / ".codex/auth.json"), str(home / "ops-state"), str(tasks / "other-task.result"),
            str(repo / ".secrets/key")} <= absent, sorted(absent)


@pytest.fixture
def broker(primary, tmp_path, monkeypatch):
    """A broker on the disposable primary, without a socket; GitHub reads and host steps are recorded fakes."""
    repo, worktree, _ = primary
    scratch = tmp_path / "broker-scratch"
    scratch.mkdir()
    instance = write_boundary._Broker.__new__(write_boundary._Broker)
    instance.boundary = _Launch(repo, scratch, _git(worktree, "rev-parse", "HEAD"))
    instance.steps = []
    instance.bound_pr = None
    instance.serial, instance.state, instance.children, instance.closed = threading.Lock(), threading.Lock(), set(), False
    instance.pulls = {7: {"state": "OPEN", "headRefName": "task", "headRefOid": instance.boundary.tip,
                          "isCrossRepository": False, "baseRefName": "main"},
                      9: {"state": "OPEN", "headRefName": "task", "headRefOid": instance.boundary.tip,
                          "isCrossRepository": False, "baseRefName": "release"},
                      # Historical PRs of a reused branch name: the same head branch, an older head, the default base.
                      10: {"state": "CLOSED", "headRefName": "task", "headRefOid": "4" * 40,
                           "isCrossRepository": False, "baseRefName": "main"},
                      11: {"state": "MERGED", "headRefName": "task", "headRefOid": "5" * 40,
                           "isCrossRepository": False, "baseRefName": "main"},
                      8: {"state": "OPEN", "headRefName": "other", "headRefOid": "1" * 40, "isCrossRepository": False}}
    monkeypatch.setattr(write_boundary._Broker, "_repository_slug", lambda self: "owner/repo")

    def gh_json(self, argv):
        if argv[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "main"}}
        return self.pulls.get(int(argv[2]))

    monkeypatch.setattr(write_boundary._Broker, "_gh_json", gh_json)
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None, cwd=None: self.steps.append(
        (module, argv)) or {"returncode": 0, "stdout": "", "stderr": ""})
    return instance


def _refused(call) -> str:
    with pytest.raises(write_boundary._BrokerRefusal) as refusal:
        call()
    return str(refusal.value)


_PRIVATE_URL = "https://github.com/learn-ukrainian/learn-ukrainian-infra-private.git"


@pytest.mark.parametrize(("url", "host", "scanned"), [
    (_PRIVATE_URL, None, False),
    ("https://github.com/example-owner/unregistered.git", None, True),
    (_PRIVATE_URL, "tls-off-masked", True),
    (_PRIVATE_URL, "rewritten", True),
], ids=["private-exempt", "public-scanned", "private-tls-off-masked-by-host-config-scanned",
        "private-rewritten-destination-scanned"])
def test_host_push_scan_exempts_only_a_registered_private_destination(primary, monkeypatch, url, host, scanned):
    """Defect classes: the broker's host push scan treats a registered private destination as public (it cannot
    prove the transport's route and refuses every private push), or exempts a public destination from the scan,
    or proves the route with configuration the transport does not use: host global or command-scope settings that
    mask the repository's http.sslVerify=false, or a URL that a rewrite sends to another destination (#565)."""
    _, worktree, _ = primary
    tip = _git(worktree, "rev-parse", "HEAD")
    monkeypatch.delenv("LU_OPSEC_REAL_GIT", raising=False)  # the broker's host environment carries no pin
    if host == "tls-off-masked":  # only the repository's own configuration reaches the transport
        _git(worktree, "config", "http.sslVerify", "false")
        (Path(os.environ["HOME"]) / "global.gitconfig").write_text(f'[http "{url}"]\n\tsslVerify = true\n')
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(Path(os.environ["HOME"]) / "global.gitconfig"))
        for name, value in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.sslVerify",
                            "GIT_CONFIG_VALUE_0": "true"}.items():
            monkeypatch.setenv(name, value)
    elif host == "rewritten":
        _git(worktree, "config", "url.https://example.invalid/public.git.insteadOf", url)
    real_run, seen = subprocess.run, {}

    def run(argv, **kwargs):
        if "-I" in argv:  # the real host push scan, in its own interpreter
            seen["scan"] = real_run(argv, **kwargs)
            return seen["scan"]
        if "push" not in argv:
            return real_run(argv, **kwargs)
        seen["push"] = argv  # the transport is not run: nothing leaves the test
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    try:
        write_boundary._git(worktree, "push", "--quiet", "--no-verify", "--porcelain",
                            "--force-with-lease=refs/heads/task:", url, f"{tip}:refs/heads/task")
    except write_boundary.Refused:
        assert scanned  # a public scan may refuse (synthetic history or no matcher); a private push never is
    assert (b"commit(s) scanned" in seen["scan"].stderr) is scanned, seen["scan"].stderr
    if not scanned:
        assert seen["scan"].returncode == 0 and os.path.isabs(seen["push"][0])


def test_github_read_runs_only_named_reads_of_the_own_repository(broker):
    """Defect class: the read verb forwards a request-chosen endpoint, method or repository, so the host login
    performs a write or reads another repository's security state for the worker."""
    assert "name must be one of" in _refused(lambda: broker._github_read({"name": "dependabot-alerts"}))
    assert "github-read takes" in _refused(lambda: broker._github_read({"name": "pull", "endpoint": "a/b"}))
    assert "this task's repository" in _refused(lambda: broker._github_read({"name": "pull", "number": 7, "repo": "a/b"}))
    assert "paginate" in _refused(lambda: broker._github_read({"name": "comments", "number": 7, "paginate": "yes"}))
    assert "positive id" in _refused(lambda: broker._github_read({"name": "pull", "number": "7; rm"}))
    assert broker.steps == []
    assert broker._github_read({"name": "checks", "sha": broker.boundary.tip})["returncode"] == 0
    assert broker.steps[-1] == ("publish.github", ["read", "checks", "--repo", "owner/repo", "--sha", broker.boundary.tip])
    # A public repository keeps its unnarrowed reads (#565 changes nothing for it): any run, issue or PR id.
    for request in ({"name": "run", "number": 5}, {"name": "issue", "number": 99}, {"name": "pull", "number": 8},
                    {"name": "checks", "sha": "2" * 40}):
        assert broker._github_read(request)["returncode"] == 0
    # The publisher's own read, forwarded from inside, names the launch's repository and may paginate (a long thread).
    assert broker._github_read({"name": "comments", "number": 7, "repo": "Owner/Repo", "paginate": True})["returncode"] == 0
    assert broker.steps[-1] == ("publish.github", ["read", "comments", "--repo", "owner/repo", "--number", "7",
                                                   "--paginate"])
    assert len(broker.steps) == 6


def test_private_repository_refuses_cross_repository_publication(broker, primary, monkeypatch):
    """Defect class: a named read reaches another repository or an unregistered origin."""
    from lu_machinery.dispatcher import agy_sandbox

    broker.launch_repository = ("owner/repo", True)
    assert "repository" in _refused(lambda: broker._github_read({"name": "pull", "number": 7, "repo": "owner/other"}))
    assert broker.steps == []
    # The launch freezes an origin outside the registry as a refusal, never as a repository to act on.
    repo, _, _ = primary
    _git(repo, "remote", "add", "origin", "https://github.com/owner/unregistered.git")
    monkeypatch.setattr(agy_sandbox, "public_repository", lambda name: False)
    monkeypatch.setattr(agy_sandbox, "private_repository", lambda name: False)
    frozen = write_boundary._Broker._resolve_repository(broker)
    assert isinstance(frozen, write_boundary._BrokerRefusal) and "not a registered repository" in str(frozen)
    monkeypatch.setattr(agy_sandbox, "private_repository", lambda name: name == "owner/unregistered")
    assert write_boundary._Broker._resolve_repository(broker) == ("owner/unregistered", True)


def test_private_repository_reads_only_the_authorized_pr_and_its_checks(broker):
    """Defect class: private read scope leak. In a private repository github-read returns an unrelated PR, issue,
    workflow run or check run (or checks of a commit that is not the task's), so a worker that reaches the public
    internet reads private content beyond its own PR; or the authorized PR and its checks are refused (C-5541)."""
    broker.launch_repository = ("owner/repo", True)
    tip = broker.boundary.tip
    # Without --pr, only the open PR of the review branch is authorized, checked live.
    for request in ({"name": "pull", "number": 7}, {"name": "comments", "number": 7}, {"name": "checks", "sha": tip},
                    {"name": "commit", "sha": tip}):
        assert broker._github_read(request)["returncode"] == 0, request
    for request in ({"name": "pull", "number": 8}, {"name": "issue", "number": 99}, {"name": "run", "number": 5},
                    {"name": "jobs", "number": 5}, {"name": "check-annotations", "number": 5},
                    {"name": "checks", "sha": "2" * 40}, {"name": "pull"}, {"name": "pull", "number": 7, "sha": tip}):
        _refused(lambda request=request: broker._github_read(request))
    assert [argv[1] for _module, argv in broker.steps] == ["pull", "comments", "checks", "commit"]
    # A reviewer: the launch's --pr is the authorized PR; its live head's checks are readable too.
    broker.steps.clear()
    broker.boundary.args.mode, broker.boundary.args.pr = "read-only", 8
    broker.boundary.tip = "3" * 40
    assert broker._github_read({"name": "reviews", "number": 8})["returncode"] == 0
    assert broker._github_read({"name": "checks", "sha": "1" * 40})["returncode"] == 0
    for request in ({"name": "reviews", "number": 7}, {"name": "timeline", "number": 9},
                    {"name": "checks", "sha": "2" * 40}, {"name": "run", "number": 8}):
        assert "private repository" in _refused(lambda request=request: broker._github_read(request))
    assert [argv[1] for _module, argv in broker.steps] == ["reviews", "checks"]


@pytest.mark.parametrize("foreign", [9, 10, 11], ids=["different-base", "closed-same-branch", "merged-same-branch"])
def test_private_repository_acts_only_on_its_one_bound_pr(broker, foreign):
    """Defect class: a private read binds an unrelated PR merely because its head branch has the same name."""
    broker.launch_repository = ("owner/repo", True)
    def call(pr):
        return broker._github_read({"name": "pull", "number": pr})
    # No launch PR: the same-named branch's other PR is never bound as the task's own.
    _refused(lambda: call(foreign))
    assert broker.bound_pr is None and broker.steps == []
    # The open own PR against the default base binds; the binding then never moves to another PR.
    assert call(7)["returncode"] == 0 and broker.bound_pr == 7
    assert "own PR (7)" in _refused(lambda: call(foreign))
    assert broker.bound_pr == 7 and len(broker.steps) == 1
    # A launch --pr: every operation names that PR, whatever the named PR's branch.
    broker.bound_pr, broker.boundary.args.pr = None, 7
    broker.steps.clear()
    _refused(lambda: call(foreign))
    assert call(7)["returncode"] == 0 and len(broker.steps) == 1


# -- the skills' own publish commands forward to the broker inside the boundary (#443) --------------------------------

_FORWARDED = {
    "github-read": ("{py} -m lu_machinery.publish read pull --number 7",
                    "{py} -m lu_machinery.publish read pull --number 7 --repo owner/other"),
    "github-read-env": ("GH_REPO={slug} {py} -m lu_machinery.publish read pull --number 7",
                        "GH_REPO=owner/other {py} -m lu_machinery.publish read pull --number 7"),
}


@pytest.mark.parametrize("target", ["own", "foreign", "private", "private-foreign"])
@pytest.mark.parametrize("operation", sorted(_FORWARDED))
def test_skill_publish_commands_reach_the_broker_for_the_own_target_only(primary, tmp_path, monkeypatch, operation,
                                                                         target, push_sandbox):  # noqa: F811
    """Defect class: named publisher reads bypass the launch's repository scope or fail to reach the relay."""
    import sys

    from lu_machinery.dispatcher import agy_sandbox
    from lu_machinery.orchestration.fleet_repos import load_fleet_repos

    repo, worktree, receipt = primary
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(push_sandbox.root))
    monkeypatch.setattr(write_boundary, "machinery_path", lambda relative: push_sandbox.root / "lu_machinery" / relative)
    slug = "owner/repo"
    if target.startswith("private"):  # the registry's own private entry, checked by the real eligibility rules
        slug = next(item.github for item in load_fleet_repos().values() if item.role.startswith("private-"))
        assert not agy_sandbox.public_repository(slug) and agy_sandbox.private_repository(slug)
    else:
        monkeypatch.setattr(agy_sandbox, "public_repository", lambda name: name == "owner/repo")
    _git(repo, "remote", "add", "origin", f"https://github.com/{slug}.git")
    tip = _git(worktree, "rev-parse", "HEAD")
    pulls = {7: {"state": "OPEN", "headRefName": "task", "headRefOid": tip, "isCrossRepository": False,
                 "baseRefName": "main"},
             8: {"state": "OPEN", "headRefName": "other", "headRefOid": tip, "isCrossRepository": False,
                 "baseRefName": "main"}}

    def gh_json(self, argv):
        return {"defaultBranchRef": {"name": "main"}} if argv[:2] == ["repo", "view"] else pulls.get(int(argv[2]))

    steps = []
    monkeypatch.setattr(write_boundary._Broker, "_gh_json", gh_json)
    monkeypatch.setattr(write_boundary._Broker, "_module", lambda self, module, argv, key, stdin=None, cwd=None: steps.append(
        argv) or {"returncode": 0, "stdout": "published\n", "stderr": ""})
    command = _FORWARDED[operation][target.endswith("foreign")].format(py=sys.executable, slug=slug)
    output, status = tmp_path / "out.txt", tmp_path / "code.txt"
    code = write_boundary.main([
        "--task-id", "wb-test", "--worktree", str(worktree), "--scratch", str(write_boundary.scratch("wb-test")),
        "--receipt", str(receipt), "--output", str(output), "--output", str(status), "--",
        "/bin/sh", "-c", f"{command} > {output} 2>&1; echo $? > {status}",
    ])
    result = json.loads(receipt.read_text())
    assert code == 0, result
    exit_code, text = int(status.read_text()), output.read_text()
    requests = [(row["verb"], row["returncode"]) for row in result["broker"]]
    if not target.endswith("foreign"):
        assert exit_code == 0 and requests == [("github-read", 0)], text
        assert steps and steps[-1][0] == "read"
        assert steps[-1][steps[-1].index("--repo") + 1] == slug
    else:
        assert exit_code != 0 and requests == [("github-read", 2)] and "refused" in text, text
        assert steps == []


def test_write_dispatch_launches_the_provider_directly_with_its_own_login(sandbox, monkeypatch, tmp_path):
    """Defect class: a workspace-write dispatch is wrapped in the write boundary, so the worker runs with a copied
    or bound login and no GitHub (operator order NO-WORKER-SANDBOX-1115). The supervisor must start the provider
    itself, with the dispatching user's HOME and the selected login directory, and wait on no boundary receipt."""
    from types import SimpleNamespace

    from lu_machinery.dispatcher import store, worker
    from lu_machinery.dispatcher.types import Invocation, ParsedResult, TaskSpec
    from tests.dispatcher.conftest import run_git

    home = tmp_path / "user-home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv(write_boundary.BROKER_ENV, raising=False)
    monkeypatch.setenv("LU_TASKS_DIR", str(sandbox.tasks))
    capture = tmp_path / "provider-env.json"
    script = "import json, os, sys; open(sys.argv[1], 'w').write(json.dumps(dict(os.environ))); print('Finished.')"
    provider = SimpleNamespace(
        build=lambda spec, env: Invocation([sys.executable, "-c", script, str(capture)], b"", env, None),
        parse=lambda stdout, _stderr, _code, _final: ParsedResult(stdout.strip() or None, None, None),
    )
    wrapped = []
    monkeypatch.setattr(worker.providers, "load", lambda _agent: provider)
    monkeypatch.setattr(worker.write_boundary, "launcher", lambda *args: wrapped.append("launcher") or [])
    monkeypatch.setattr(worker.write_boundary, "verify", lambda *args, **kwargs: wrapped.append("verify") or ({}, None))
    monkeypatch.setattr(worker.headroom, "check_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.headroom, "check_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_reap_settled", lambda *_args, **_kwargs: None)
    spec = TaskSpec("direct-write", "claude", "claude-opus-5-5", None, "workspace-write", sandbox.repo,
                    tmp_path / "direct-write.prompt", sandbox.tasks / "direct-write.result", "a")
    spec.prompt_file.write_bytes(b"write")
    record = store.new_record(spec, {"key": "public", "github": str(sandbox.remote)}, "main",
                              run_git(sandbox.repo, "rev-parse", "HEAD"), {"research_owned_path": []})
    record["prompt_file"] = str(sandbox.tasks / "direct-write.prompt")
    path = store.create(record, b"write")
    (sandbox.tasks / "logs").mkdir()
    worker.supervise(path, {}, 30)
    settled = json.loads(path.read_text())
    # The provider ran and committed nothing, so settlement reached the delivery check (a boundary refusal fails).
    assert (settled["status"], settled.get("error")) == ("no_deliverable", "no_commits_or_unknown")
    assert wrapped == [] and "write_boundary" not in settled
    seen = json.loads(capture.read_text())
    assert seen["HOME"] == str(home)
    assert seen["CLAUDE_CONFIG_DIR"] == str(home / ".claude")
    assert write_boundary.BROKER_ENV not in seen


def _savant_write(primary, monkeypatch, tmp_path, script: str, *owned: str,
                  read_path: str | None = None) -> tuple[int, dict, Path]:
    """Run ``script`` as a Savant write run's provider over the view of the primary's published commit."""
    repo = primary[0]
    tip = _git(repo, "rev-parse", "HEAD")
    # A registered public origin whose anonymous listing serves the start commit (C2), as on GitHub.
    monkeypatch.setattr(write_boundary, "_public_origin", lambda _repository: "example/public")
    monkeypatch.setattr(write_boundary, "_remote_tips", lambda slug: {"refs/heads/main": tip})
    needles = tmp_path / "needles.json"
    needles.write_text('{"schema_version": 1, "run_users": [], "home_dirs": []}', encoding="utf-8")
    monkeypatch.setenv("LU_OPSEC_NEEDLES_FILE", str(needles))
    # Production checkouts sit below an emptied data root; so does this one: the run sees its view only.
    monkeypatch.setattr(write_boundary, "_DATA_ROOTS", _ALL_DATA_ROOTS)
    patch = tmp_path / "wb-test.patch"
    code, data = _run(primary, script, "--mode", "workspace-write", "--snapshot-bytes", "65536", "--no-github-relays",
                      "--no-sources-relay", "--upload-ceiling", "65536", "--draft-patch", str(patch),
                      *(arg for path in owned for arg in ("--owned-path", path)),
                      *(("--read-path", read_path) if read_path else ()))
    return code, data, patch


def _verify(tmp_path, mode: str) -> str | None:
    """The settlement's reading of the launcher's receipt, as the worker reads it beside the task record."""
    task = tmp_path / "wb-test.json"
    write_boundary.receipt_file(task).write_bytes((tmp_path / "receipt.json").read_bytes())
    return write_boundary.verify(task, "wb-test", mode)[1]


def test_savant_write_run_edits_only_its_view_and_leaves_a_patch(primary, monkeypatch, tmp_path):
    """Defect class (#898): a Savant write run reaches the worktree, Git metadata, the primary, the host's files,
    another repository or GitHub, or its edits leave the boundary other than as an owned-path patch of the
    published commit."""
    repo, worktree, _ = primary
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("old\n")
    (repo / "gone.txt").write_text("remove me\n")
    # A tracked name the screen refuses never enters the view, so it cannot refuse an unrelated owned edit.
    (repo / "docs" / ("ghp_" + "A" * 36 + ".md")).write_text("public\n")
    # So does a tracked file nested deeper than the draft's bound (review round 3).
    deep = repo.joinpath("deep", *["d"] * write_boundary.MAX_VIEW_DEPTH)
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("deep\n")
    _git(repo, "add", "docs", "gone.txt", "deep")
    _git(repo, "commit", "-qm", "fixture")
    _git(worktree, "merge", "-q", "--ff-only", "main")
    tip = _git(repo, "rev-parse", "HEAD")
    objects, status = _git(repo, "count-objects", "-v"), _git(repo, "status", "--porcelain")
    # Every primary object file's mtime: Git freshens an existing object it is asked to write (review of #913).
    stamps = {path: path.stat().st_mtime_ns for path in (repo / ".git" / "objects").rglob("*") if path.is_file()}
    host_tmp = tmp_path / "outside-written"
    script = (
        # Owned edits of its own view: change, add and delete.
        "printf 'new text\\n' > docs/guide.md; printf 'primary\\n' > docs/new.md; rm gone.txt;"
        # Every other write path fails or lands in the private, discarded HOME and TMPDIR.
        f"echo x > {worktree}/a.txt; echo x > {repo}/a.txt; echo x > {repo}/.git/HEAD; echo x > {host_tmp};"
        f"git -C {repo} commit -q --allow-empty -m evil; git -C {worktree} status;"
        "echo x > $HOME/home-file; echo x > $TMPDIR/tmp-file; true"
    )
    code, receipt, patch = _savant_write(primary, monkeypatch, tmp_path, script, "docs", "gone.txt")
    assert code == 0 and receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    probes = {(probe["op"], probe["path"]): probe["outcome"] for probe in receipt["canary"]["probes"]}
    assert probes[("stat", str(worktree))] == "ENOENT" and probes[("stat", str(repo / ".git" / "HEAD"))] == "ENOENT"
    # No host service: neither Sources relay nor GitHub relay exists inside.
    assert probes[("stat", write_boundary._SOURCES_INSIDE)] == "ENOENT" and receipt["sources"] == {"status": "disabled"}
    imported = receipt["import"]
    assert imported["status"] == "drafted" and imported["paths"] == ["docs/guide.md", "docs/new.md", "gone.txt"]
    assert imported["patch_sha256"] == hashlib.sha256(patch.read_bytes()).hexdigest()
    assert _verify(tmp_path, "workspace-write") is None
    assert _verify(tmp_path, "read-only") == "write_boundary_unproven: the receipt names another mode"
    # Nothing outside the patch changed: the worktree, the primary's files, refs and objects, the host.
    assert _git(worktree, "status", "--porcelain") == "" and _git(repo, "status", "--porcelain") == status
    assert _git(repo, "rev-parse", "HEAD") == tip and _git(repo, "count-objects", "-v") == objects
    # docs/new.md's bytes equal a.txt's blob, which the primary already holds: its file was not touched either.
    assert {path: path.stat().st_mtime_ns for path in (repo / ".git" / "objects").rglob("*") if path.is_file()} == stamps
    assert not host_tmp.exists() and not (tmp_path / "home" / "home-file").exists()
    assert not write_boundary.scratch("wb-test").exists()
    # A normal worker applies the patch to the start commit and gets exactly the run's edits.
    _git(worktree, "apply", "--index", str(patch))
    assert (worktree / "docs" / "guide.md").read_text() == "new text\n"
    assert (worktree / "docs" / "new.md").read_text() == "primary\n" and not (worktree / "gone.txt").exists()


def test_savant_write_run_drafts_nothing_after_a_failed_provider_or_from_an_oversized_view(primary, monkeypatch,
                                                                                          tmp_path):
    """Defect class (#898, review of #913): a failed provider's partial edits become a deliverable patch, or a
    view already past the draft's entry budget starts a run that can never deliver."""
    repo, worktree, _ = primary
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("old\n")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-qm", "fixture")
    _git(worktree, "merge", "-q", "--ff-only", "main")
    code, receipt, patch = _savant_write(primary, monkeypatch, tmp_path, "printf 'x\\n' > docs/guide.md; exit 3",
                                         "docs")
    assert code == 3 and receipt["status"] == "ran" and receipt["import"]["status"] == "unchanged"
    assert not patch.exists()
    # a.txt, docs/ and docs/guide.md are three entries; a budget of two refuses before the provider starts.
    monkeypatch.setattr(write_boundary, "MAX_VIEW_ENTRIES", 2)
    marker = tmp_path / "started"
    code, receipt, patch = _savant_write(primary, monkeypatch, tmp_path, f"touch {marker}", "docs")
    assert code == write_boundary.REFUSED and "would hold more than 2 files and directories" in receipt["reason"]
    assert not marker.exists() and not patch.exists()


@pytest.mark.parametrize(("case", "script", "reason"), [
    ("outside-owned", "printf 'x\\n' > a.txt", "1 edited path(s) lie outside the owned paths"),
    ("link", "ln -s ../a.txt docs/link", "is not a directory or a regular file"),
    ("hard-link", "ln docs/guide.md docs/second", "is not a directory or a single-link regular file"),
    ("fifo", "mkfifo docs/pipe", "is not a directory or a single-link regular file"),
    ("excluded-name", "printf 'x\\n' > docs/.env", "excluded, unprintable or screened name"),
    ("screened-name", "printf 'x\\n' > docs/ghp_" + "A" * 36, "excluded, unprintable or screened name"),
    ("filtered-path", "printf 'x\\n' > docs/AGENTS.txt", "a path of the start commit that the view left out"),
    # A directory where the start commit holds a file the view left out: Git would delete that file.
    ("file-parent", "mkdir docs/AGENTS.txt && printf 'x\\n' > docs/AGENTS.txt/child",
     "a path of the start commit that the view left out"),
    ("secret", "printf 'k = ghp_%s\\n' AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA > docs/guide.md", "a private key, a token"),
    ("oversize", "head -c 70000 /dev/zero > docs/big.bin", "more than 65536 bytes"),
    ("too-deep", "mkdir -p docs/" + "d/" * 64 + " && printf 'x\\n' > docs/" + "d/" * 64 + "f", "nests deeper than 64"),
])
def test_savant_write_run_refuses_any_other_edit(primary, monkeypatch, tmp_path, case, script, reason):
    """Defect class (#898): an edit outside the owned paths, a link, an excluded or filtered path, a secret or an
    oversized view still yields a deliverable patch."""
    repo, worktree, _ = primary
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("old\n")
    # Left out of the view by its content: a new file there would replace bytes the run never read.
    (repo / "docs" / "AGENTS.txt").write_text("k = ghp_" + "A" * 36 + "\n")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-qm", "fixture")
    _git(worktree, "merge", "-q", "--ff-only", "main")
    _code, receipt, patch = _savant_write(primary, monkeypatch, tmp_path, script, "docs")
    assert receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    assert receipt["import"]["status"] == "refused" and reason in receipt["import"]["reason"], receipt["import"]
    assert not patch.exists() and _git(worktree, "status", "--porcelain") == ""
    refusal = _verify(tmp_path, "workspace-write")
    assert refusal.startswith("write_boundary_import_failed: refused: savant_draft_refused")


def test_savant_write_run_needs_its_bounds(primary, tmp_path):
    """Defect class (#898): a write run starts without the view, the patch target, its owned paths or with a
    GitHub or Sources relay or Sources MCP."""
    full = ["--mode", "workspace-write", "--snapshot-bytes", "1024", "--no-github-relays", "--no-sources-relay",
            "--draft-patch", str(tmp_path / "p.patch"), "--owned-path", "docs"]
    for drop in ("--snapshot-bytes", "--no-github-relays", "--no-sources-relay", "--draft-patch", "--owned-path"):
        index = full.index(drop)
        argv = full[:index] + full[index + (1 if drop.startswith("--no-") else 2):]
        with pytest.raises(SystemExit):
            _run(primary, "true", *argv)
    for extra in (["--sources-mcp", "opencode"], ["--mode", "read-only"]):
        with pytest.raises(SystemExit):
            _run(primary, "true", *full, *extra)


def test_savant_write_draft_never_fetches_into_a_partial_clone(primary, monkeypatch, tmp_path):
    """Defect class (#898, review of #913): the draft's checks of a refused edit download a blob the view left out
    into a partial-clone primary (host network and a primary write after the run)."""
    source = tmp_path / "source"
    (source / "docs").mkdir(parents=True)
    _git(source, "init", "-q", "-b", "main")
    for key, value in (("user.email", "probe@example.invalid"), ("user.name", "Probe"),
                       ("uploadpack.allowFilter", "true"), ("uploadpack.allowAnySHA1InWant", "true")):
        _git(source, "config", key, value)
    (source / "docs" / "guide.md").write_text("old\n")
    (source / "docs" / "omitted.md").write_text("never read by the run\n")
    _git(source, "add", "docs")
    _git(source, "commit", "-qm", "fixture")
    omitted = _git(source, "rev-parse", "HEAD:docs/omitted.md")
    repo = tmp_path / "partial"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", source.as_uri(), str(repo)],
                   check=True, capture_output=True)
    worktree = repo / ".worktrees" / "task"
    _git(repo, "worktree", "add", "-q", "--no-checkout", "-b", "task", str(worktree), "main")

    def local(oid: str) -> bool:
        return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", oid], capture_output=True,
                              env={**os.environ, "GIT_NO_LAZY_FETCH": "1"}).returncode == 0

    assert not local(omitted)
    partial = (repo, worktree, tmp_path / "receipt.json")
    code, receipt, patch = _savant_write(partial, monkeypatch, tmp_path, "printf 'x\\n' > docs/omitted.md",
                                         "docs", read_path="docs/guide.md")
    assert receipt["status"] == "ran" and receipt["canary"]["passed"], receipt
    assert receipt["import"]["status"] == "refused" and "left out" in receipt["import"]["reason"]
    assert not patch.exists() and not local(omitted)


def test_savant_write_draft_checks_only_the_new_paths_in_a_wide_directory(primary, monkeypatch, tmp_path):
    """Defect class (#898, review of #913 round 3): the draft's path check lists a wide directory's unrelated
    children and refuses a valid owned addition at the listing cap."""
    repo, worktree, _ = primary
    (repo / "docs").mkdir()
    for index in range(40):
        (repo / "docs" / f"sibling-{index:02d}.md").write_text("sibling\n")
    (repo / "docs" / "guide.md").write_text("old\n")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-qm", "fixture")
    _git(worktree, "merge", "-q", "--ff-only", "main")
    # The 40 siblings' entries (about 2.6 KB) would exceed this cap; one exact entry per path stays far below it.
    monkeypatch.setattr(write_boundary, "MAX_TREE_LISTING_BYTES", 1024)
    _code, receipt, patch = _savant_write(primary, monkeypatch, tmp_path, "printf 'new\\n' > docs/new.md", "docs",
                                          read_path="docs/guide.md")
    assert receipt["import"]["status"] == "drafted" and receipt["import"]["paths"] == ["docs/new.md"], receipt["import"]
    assert patch.exists()
