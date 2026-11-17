"""Consumer stand-ins for the clean-machine drill. Run by the release venv's interpreter with -I.

Each probe exercises one consumer surface of the installed lu-machinery release against the mock
world that drill.py builds, checks a known answer, prints one JSON line and exits 0 (pass) or 1.
The release is reached only through ``import lu_machinery`` or ``python -m lu_machinery...`` from
this interpreter, exactly as a consumer reaches it.

Usage: python -I probes.py <probe> [args]
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

PY = sys.executable
PROBES: dict[str, Callable[[list[str]], tuple[bool, dict]]] = {}


def probe(name: str) -> Callable:
    def register(function: Callable) -> Callable:
        PROBES[name] = function
        return function

    return register


def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: int = 120,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit (argv list, no shell)
    return subprocess.run(
        argv,
        check=False,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin,
    )


def module(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return run([PY, "-I", "-m", *args], **kwargs)


def product() -> str:
    return os.environ["LU_PRODUCT_ROOT"]


def scratch() -> Path:
    return Path(os.environ["TMPDIR"]).parent


def resource(relative: str) -> str:
    # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2 (the kit requires Python 3.12+)
    from importlib.resources import files

    return str(files("lu_machinery").joinpath(relative))


def stub_calls(tool: str | None = None) -> list[dict]:
    path = Path(os.environ["DRILL_STUB_STATE"]) / "calls.jsonl"
    if not path.exists():
        return []
    calls = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [call for call in calls if tool is None or call["tool"] == tool]


def clone_product(name: str) -> Path:
    """A private clone of the synthetic product for probes that commit or write tracked files."""
    target = scratch() / name
    result = run(["git", "clone", "-q", product(), str(target)])
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return target


# ---------------------------------------------------------------------------------------------


@probe("fleet-roundtrip")
def fleet_roundtrip(_: list[str]) -> tuple[bool, dict]:
    """Publish to the cto channel as another seat, then read the delivery's metadata back."""
    marker = f"drill marker {uuid.uuid4()}"
    published = module(
        "lu_machinery.fleet_comms",
        "channel",
        "publish",
        "cto",
        marker,
        "--sender",
        "claude-infra",
        "--idempotency-key",
        f"drill-{uuid.uuid4()}",
        cwd=product(),
    )
    if published.returncode != 0:
        return False, {
            "step": "publish",
            "rc": published.returncode,
            "err": published.stderr[-500:] + published.stdout[-500:],
        }
    message = json.loads(published.stdout)
    ids = message.get("delivery_ids") or []
    if (
        message.get("content_sha256") != hashlib.sha256(marker.encode()).hexdigest()
        or not ids
    ):
        return False, {"step": "publish", "message": message}
    status = module(
        "lu_machinery.fleet_comms", "deliveries", "status", ids[0], cwd=product()
    )
    ok = (
        status.returncode == 0
        and ids[0] in status.stdout
        and "claude-cto" in status.stdout
    )
    return ok, {
        "delivery": ids[0],
        "status_rc": status.returncode,
        "status": status.stdout[-400:],
        "err": status.stderr[-300:],
    }


@probe("fleet-authority")
def fleet_authority(_: list[str]) -> tuple[bool, dict]:
    """Open the authority service on the mock plane the way the keeper and inbox wrappers do."""
    from lu_machinery.fleet_comms import authority

    root = Path(product()) / "batch_state" / "fleet-comms" / "v1"
    service = authority.open_authority_service(root)
    names = [
        name
        for name in ("claude-cto", "claude-cto-1a2b3c4d", "codex")
        if authority.is_session_cto_recipient(name)
    ]
    close = getattr(service, "close", None)
    if callable(close):
        close()
    return names == ["claude-cto-1a2b3c4d"], {
        "service": type(service).__name__,
        "cto_recipients": names,
    }


@probe("backup-alert")
def backup_alert(_: list[str]) -> tuple[bool, dict]:
    """The failure alert posts one change-only report to cto, then stays quiet on a repeat."""
    state = scratch() / "alerts"
    unit = "learn-ukrainian-backup.service"
    first = module(
        "lu_machinery.orchestration.backup_failure_alert",
        "--state-dir",
        str(state),
        "failed",
        unit,
    )
    second = module(
        "lu_machinery.orchestration.backup_failure_alert",
        "--state-dir",
        str(state),
        "failed",
        unit,
    )
    recovered = module(
        "lu_machinery.orchestration.backup_failure_alert",
        "--state-dir",
        str(state),
        "recovered",
        unit,
    )
    ok = (
        first.returncode == 0
        and second.returncode == 0
        and "not re-posting" in (second.stdout + second.stderr)
        and recovered.returncode == 0
    )
    return ok, {
        "first": (first.stdout + first.stderr)[-300:],
        "second": (second.stdout + second.stderr)[-300:],
        "recovered": (recovered.stdout + recovered.stderr)[-300:],
    }


@probe("scheduled-backup")
def scheduled_backup(args: list[str]) -> tuple[bool, dict]:
    """The scheduled backup runner, launched from the release's resource path, records its receipt."""
    mode = args[:1]
    receipt = scratch() / "last-run.json"
    result = run(
        ["bash", resource("orchestration/run_scheduled_backup.sh"), *mode],
        env={"LU_BACKUP_LAST_RUN": str(receipt)},
    )
    expected = "retention --execute" if mode == ["retention"] else "backup --execute"
    detail = {"rc": result.returncode, "out": (result.stdout + result.stderr)[-500:]}
    if expected not in result.stdout:
        return False, detail
    if not mode:
        data = json.loads(receipt.read_text()) if receipt.exists() else {}
        detail["receipt_exit_status"] = data.get("exit_status")
        return result.returncode == 0 and data.get("exit_status") == 0, detail
    return result.returncode == 0, detail


@probe("agent-deploy")
def agent_deploy(args: list[str]) -> tuple[bool, dict]:
    """Deploy release agent resources into a consumer, verify with --check, optionally run a hook."""
    # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2 (the kit requires Python 3.12+)
    from importlib.resources import files

    root = clone_product("deploy-consumer")
    inventory = json.loads(
        files("lu_machinery.agent_deploy").joinpath("release.v1.json").read_bytes()
    )
    settings: dict = {}
    for requirement in (
        inventory.get("required_hooks", {})
        .get("claude", {})
        .get(".claude/settings.json", [])
    ):
        *parents, event = requirement["path"]
        node = settings
        for name in parents:
            node = node.setdefault(name, {})
        node.setdefault(event, []).append(
            {**requirement["group"], "hooks": requirement["hooks"]}
        )
    (root / "agents_extensions" / "claude").mkdir(parents=True, exist_ok=True)
    (root / "agents_extensions" / "claude" / "settings.json").write_text(
        json.dumps(settings, indent=1)
    )
    descriptor = {
        "providers": ["claude"],
        "preserve": [],
        "sources": {"claude": {"path": "agents_extensions/claude", "required": True}},
    }
    (root / "agents_extensions" / "deployment.v1.json").write_text(
        json.dumps(descriptor)
    )
    with (root / ".gitignore").open("a") as handle:
        handle.write("/.claude/\n/.agents/\n/.deploy-state/\n/CLAUDE.md\n/AGENTS.md\n")
    run(["git", "add", "-A"], cwd=str(root))
    run(
        ["git", "commit", "-q", "-m", "drill: consumer deployment descriptor"],
        cwd=str(root),
    )
    applied = module(
        "lu_machinery.agent_deploy",
        "--project-root",
        str(root),
        env={"LU_PRODUCT_ROOT": str(root)},
    )
    checked = module(
        "lu_machinery.agent_deploy",
        "--project-root",
        str(root),
        "--check",
        env={"LU_PRODUCT_ROOT": str(root)},
    )
    detail = {
        "apply_rc": applied.returncode,
        "check_rc": checked.returncode,
        "apply": (applied.stdout + applied.stderr)[-400:],
        "check": (checked.stdout + checked.stderr)[-400:],
    }
    ok = applied.returncode == 0 and checked.returncode == 0
    if ok and args[:1] == ["session"]:
        # Harness-session stand-in: run each deployed SessionStart hook as the harness would.
        deployed = json.loads((root / ".claude" / "settings.json").read_text())
        hooks = [
            hook["command"]
            for group in deployed.get("hooks", {}).get("SessionStart", [])
            for hook in group.get("hooks", [])
        ]
        results = []
        for command in hooks:
            hooked = run(
                ["bash", "-c", command],
                cwd=str(root),
                stdin=json.dumps(
                    {
                        "session_id": "drill",
                        "cwd": str(root),
                        "hook_event_name": "SessionStart",
                        "source": "startup",
                    }
                ),
                env={"CLAUDE_PROJECT_DIR": str(root), "LU_PRODUCT_ROOT": str(root)},
                timeout=60,
            )
            results.append({"command": command[-120:], "rc": hooked.returncode})
        detail["session_hooks"] = results
        ok = bool(hooks) and all(item["rc"] == 0 for item in results)
    return ok, detail


@probe("project-state-reporter")
def project_state_reporter(_: list[str]) -> tuple[bool, dict]:
    """The reporter wrapper runs from the release and posts its state to the Monitor stand-in."""
    result = run(
        [
            "bash",
            "--noprofile",
            "--norc",
            resource("orchestration/run_project_state_reporter.sh"),
        ],
        timeout=150,
    )
    ok = (
        result.returncode == 0
        and '"received":true' in result.stdout.replace(" ", "")
        and "drill_standin" in result.stdout
    )
    return ok, {
        "rc": result.returncode,
        "out": result.stdout[-400:],
        "err": result.stderr[-400:],
    }


@probe("quota-history")
def quota_history(_: list[str]) -> tuple[bool, dict]:
    """One collect against the Monitor stand-in appends one known row per slot."""
    history = scratch() / "quota" / "history.jsonl"
    history.parent.mkdir(mode=0o700)
    result = module(
        "lu_machinery.fleet.quota_history",
        "collect",
        "--file",
        str(history),
        "--timeout",
        "5",
    )
    rows = (
        [json.loads(line) for line in history.read_text().splitlines()]
        if history.exists()
        else []
    )
    unavailable = [row for row in rows if row.get("reason") == "source_unavailable"]
    return result.returncode == 0 and rows and not unavailable, {
        "rc": result.returncode,
        "rows": len(rows),
        "unavailable": len(unavailable),
        "err": result.stderr[-300:],
    }


@probe("prepublish-fail-closed")
def prepublish_fail_closed(_: list[str]) -> tuple[bool, dict]:
    """Without the private OPSEC matcher every public write is refused; nothing reaches gh."""
    from lu_machinery.opsec import prepublish

    try:
        prepublish.check_texts("drill/public-product", ["synthetic drill text"])
    except prepublish.PublishBlocked as exc:
        return True, {"refused": str(exc)[:200]}
    return False, {"refused": None}


@probe("publish-fail-closed")
def publish_fail_closed(_: list[str]) -> tuple[bool, dict]:
    """The publish CLI refuses a public issue comment without the private matcher; gh is never called."""
    body = scratch() / "body.txt"
    body.write_text("synthetic drill comment\n")
    result = module(
        "lu_machinery.publish",
        "issue-comment",
        "--repo",
        "drill/public-product",
        "--number",
        "1",
        "--body-file",
        str(body),
    )
    writes = [call for call in stub_calls("gh") if "comment" in call["argv"]]
    ok = (
        result.returncode != 0
        and "OPSEC" in (result.stdout + result.stderr)
        and not writes
    )
    return ok, {
        "rc": result.returncode,
        "out": (result.stdout + result.stderr)[-300:],
        "gh_writes": len(writes),
    }


@probe("resolver")
def resolver(_: list[str]) -> tuple[bool, dict]:
    """An Anthropic author resolves to a reviewer outside its family."""
    state = scratch() / "review-state.json"
    result = module(
        "lu_machinery.review.closeout_cli",
        "--state-file",
        str(state),
        "resolve-reviewer",
        "--author-model",
        "claude-opus-5-5",
        "--risk",
        "medium",
        "--review-profile",
        "code",
        "--owned-path",
        "drill/example.py",
        cwd=product(),
    )
    try:
        selected = json.loads(result.stdout).get("selected") or {}
    except ValueError:
        selected = {}
    family = selected.get("family")
    return result.returncode == 0 and family not in (None, "anthropic"), {
        "rc": result.returncode,
        "family": family,
        "model": selected.get("concrete_model"),
        "out": result.stdout[-600:],
        "err": result.stderr[-300:],
    }


@probe("scorecard-imports")
def scorecard_imports(_: list[str]) -> tuple[bool, dict]:
    """The scorecard's imports answer: fleet repositories load and the slot status reports its schema."""
    import lu_machinery.dispatcher  # noqa: F401
    from lu_machinery.common import test_slots  # noqa: F401
    from lu_machinery.orchestration import fleet_repos

    repos = fleet_repos.load_fleet_repos()
    status = module("lu_machinery.common.test_slots", "status", "--json")
    try:
        schema = json.loads(status.stdout).get("schema")
    except ValueError:
        schema = None
    ok = (
        bool(repos)
        and fleet_repos.default_repo_key(repos) in repos
        and schema == "test-slots.status.v1"
    )
    return ok, {
        "repos": len(repos),
        "default": fleet_repos.default_repo_key(repos),
        "slot_schema": schema,
    }


@probe("admission")
def admission(args: list[str]) -> tuple[bool, dict]:
    """Admission thresholds load and the host probes answer (as the reboot gate reads them)."""
    from lu_machinery.common import task_store_paths  # noqa: F401
    from lu_machinery.orchestration import dispatch_admission

    thresholds = dispatch_admission.load_thresholds({})
    disk = dispatch_admission.probe_disk()
    policy = dispatch_admission.load_product_policy()
    ok = disk.free_bytes > 0 and bool(policy.required_review_receipts(["anthropic"]))
    return ok, {
        "thresholds": type(thresholds).__name__,
        "disk_free_gb": round(disk.free_bytes / 2**30, 1),
        "receipts_for_anthropic": sorted(
            policy.required_review_receipts(["anthropic"])
        ),
    }


@probe("dispatch-dry-run")
def dispatch_dry_run(_: list[str]) -> tuple[bool, dict]:
    """A read-only dispatch dry run admits and prints its plan; no worker starts."""
    task = f"drill-dry-{uuid.uuid4().hex[:8]}"
    result = module(
        "lu_machinery.dispatch",
        "dispatch",
        "--agent",
        "codex",
        "--model",
        "gpt-6.1-sol",
        "--task-id",
        task,
        "--prompt",
        "drill: dry run only",
        "--mode",
        "read-only",
        "--dry-run",
        cwd=product(),
        timeout=150,
    )
    provider_runs = [
        call for call in stub_calls("codex") if "--version" not in call["argv"]
    ]
    ok = result.returncode == 0 and not provider_runs
    return ok, {
        "rc": result.returncode,
        "out": result.stdout[-500:],
        "err": result.stderr[-500:],
        "provider_runs": len(provider_runs),
    }


@probe("linked-worktree")
def linked_worktree(_: list[str]) -> tuple[bool, dict]:
    """From a linked worktree the release's dispatcher reads the primary's task records."""
    worktree = os.environ["DRILL_WORKTREE"]
    result = module("lu_machinery.dispatch", "list", cwd=worktree)
    try:
        ids = sorted(item.get("task_id") for item in json.loads(result.stdout))
    except (ValueError, AttributeError):
        ids = []
    ok = result.returncode == 0 and "drill-old-1" in ids
    return ok, {"rc": result.returncode, "tasks": ids[:10], "err": result.stderr[-300:]}


@probe("merge-closeout")
def merge_closeout(_: list[str]) -> tuple[bool, dict]:
    """Closeout of an unknown PR reads GitHub through the stand-in and reports, changing nothing."""
    root = clone_product("closeout-consumer")
    bare = run(["git", "config", "remote.origin.url"], cwd=str(root)).stdout.strip()
    run(
        ["git", "remote", "set-url", "origin", "https://github.com/drill/product.git"],
        cwd=str(root),
    )
    run(
        [
            "git",
            "config",
            f"url.{bare}.insteadOf",
            "https://github.com/drill/product.git",
        ],
        cwd=str(root),
    )
    result = module(
        "lu_machinery.orchestration.merge_closeout",
        "1",
        "--repo",
        "drill/product",
        "--repo-root",
        str(root),
        "--json",
        timeout=150,
    )
    api = stub_calls("gh-api")
    try:
        report = json.loads(result.stdout)
    except ValueError:
        report = {}
    ok = (
        result.returncode in (0, 1)
        and bool(api)
        and "PR repository does not match" not in result.stdout
        and bool(report)
    )
    return ok, {
        "rc": result.returncode,
        "out": result.stdout[-500:],
        "err": result.stderr[-300:],
        "gh_api_calls": len(api),
    }


@probe("claims-lock")
def claims_lock(_: list[str]) -> tuple[bool, dict]:
    """A worktree lock has one identity whatever spelling reaches it, and a second holder is refused."""
    from lu_machinery.orchestration import worktree_claims

    lock_dir = scratch() / "locks"
    lock_dir.mkdir()
    worktree = Path(os.environ["DRILL_WORKTREE"])
    alias = scratch() / "alias"
    alias.symlink_to(worktree)
    first = worktree_claims.lock_path(worktree, lock_dir=lock_dir)
    second = worktree_claims.lock_path(alias, lock_dir=lock_dir)
    refused = False
    with worktree_claims.worktree_lock(worktree, lock_dir=lock_dir, timeout_s=1):
        try:
            with worktree_claims.worktree_lock(alias, lock_dir=lock_dir, timeout_s=0.2):
                pass
        except worktree_claims.WorktreeLockError:
            refused = True
    ok = first == second and refused
    return ok, {"same_identity": first == second, "second_holder_refused": refused}


@probe("launcher-dry-run")
def launcher_dry_run(args: list[str]) -> tuple[bool, dict]:
    """The product start script sources the release launcher core; the dry run prints the exec line."""
    mode = args[0] if args else "interactive"
    extra = (
        ["--epic", "epics"] if mode == "driver" else ["--model", "claude-sonnet-5-5"]
    )
    result = run(
        ["./start-claude.sh", *extra],
        cwd=product(),
        env={"LAUNCHER_DRY_RUN": "1", "DRILL_LAUNCH_MODE": mode},
        timeout=150,
    )
    ok = result.returncode == 0 and "would exec claude" in result.stdout
    launched = [
        call for call in stub_calls("claude") if "--version" not in call["argv"]
    ]
    return ok and not launched, {
        "rc": result.returncode,
        "out": result.stdout[-300:],
        "err": result.stderr[-300:],
        "provider_runs": len(launched),
    }


@probe("git-hooks")
def git_hooks(_: list[str]) -> tuple[bool, dict]:
    """One machinery pre-commit hook and one product hook run on a consumer commit; a bad file is refused."""
    root = clone_product("hooks-consumer")
    config = (
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: machinery-no-bare-python\n"
        "        name: machinery check_no_bare_python (release)\n"
        f"        entry: {json.dumps(shlex.quote(PY) + ' -I -m lu_machinery.pre_commit.check_no_bare_python --files')}\n"
        "        language: system\n"
        "        files: \\.(sh|md)$\n"
        "      - id: product-marker\n"
        "        name: product hook (stand-in)\n"
        "        entry: bash -c 'test -f curriculum/l2-uk-en/curriculum.yaml'\n"
        "        language: system\n"
        "        pass_filenames: false\n"
    )
    (root / ".pre-commit-config.yaml").write_text(config)
    run(["git", "add", ".pre-commit-config.yaml"], cwd=str(root))
    env = {"PRE_COMMIT_HOME": str(scratch() / "pre-commit-home")}
    installed = module("pre_commit", "install", cwd=str(root), env=env)
    good = root / "scripts" / "good.sh"
    good.write_text("#!/usr/bin/env bash\necho fine\n")
    run(["git", "add", "-A"], cwd=str(root))
    clean = run(
        ["git", "commit", "-q", "-m", "drill: clean commit"], cwd=str(root), env=env
    )
    bad = root / "scripts" / "bad.sh"
    bad.write_text("#!/usr/bin/env bash\npython3 -c 'print(1)'\n")
    run(["git", "add", "-A"], cwd=str(root))
    dirty = run(
        ["git", "commit", "-q", "-m", "drill: bare python"], cwd=str(root), env=env
    )
    ok = installed.returncode == 0 and clean.returncode == 0 and dirty.returncode != 0
    return ok, {
        "install_rc": installed.returncode,
        "clean_rc": clean.returncode,
        "bad_rc": dirty.returncode,
        "clean": (clean.stdout + clean.stderr)[-300:],
        "bad": (dirty.stdout + dirty.stderr)[-400:],
    }


@probe("shipped-units")
def shipped_units(_: list[str]) -> tuple[bool, dict]:
    """Every unit the release ships names a module or resource the release itself provides."""
    import importlib.util
    import re

    # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2 (the kit requires Python 3.12+)
    from importlib.resources import files

    units = sorted(
        p
        for p in files("lu_machinery").joinpath("packaging/systemd").iterdir()
        if p.name.endswith(".service")
    )
    missing = []
    for unit in units:
        text = unit.read_text()
        for name in re.findall(r"-m (lu_machinery[\w.]*)", text):
            if importlib.util.find_spec(name) is None:
                missing.append(f"{unit.name}: {name}")
        for rel in re.findall(r'joinpath\("([^"]+)"\)', text):
            if not files("lu_machinery").joinpath(rel).is_file():
                missing.append(f"{unit.name}: {rel}")
    return bool(units) and not missing, {"units": len(units), "missing": missing}


@probe("cli-help")
def cli_help(_: list[str]) -> tuple[bool, dict]:
    """The command-line entry points host wrappers call answer --help from the release."""
    commands = [
        "lu_machinery.fleet_comms",
        "lu_machinery.dispatch",
        "lu_machinery.publish",
        "lu_machinery.ai_agent_bridge",
        "lu_machinery.orchestration.merge_queue_keeper",
        "lu_machinery.orchestration.reconcile_sweep",
        "lu_machinery.orchestration.stale_task_records",
        "lu_machinery.hygiene.tmp_sweep",
        "lu_machinery.agent_deploy",
        "lu_machinery.review.closeout_cli",
        "lu_machinery.fleet.quota_history",
        "lu_machinery.ops.lane_relaunch",
    ]
    failed = []
    for name in commands:
        result = module(name, "--help")
        if result.returncode != 0 or "usage" not in result.stdout.lower():
            failed.append(name)
    return not failed, {"checked": len(commands), "failed": failed}


PRODUCT_IMPORT_SCRIPT = (
    "import sys, json, drill_product_pkg; print(json.dumps({'root': str(drill_product_pkg.ROOT), 'machinery': "
    + "sorted({m.__file__ for n, m in sys.modules.items() if n.startswith('lu_machinery') and getattr(m, '__file__', None)})}))"
)


@probe("product-import")
def product_import(_: list[str]) -> tuple[bool, dict]:
    """A product package imports the release in-process; every machinery module comes from the venv."""
    import sysconfig

    site = Path(sysconfig.get_paths()["purelib"]).resolve()
    package = scratch() / "drill_product_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from lu_machinery.common.repo_root import product_root\n"
        "from lu_machinery.orchestration import fleet_repos\n"
        "from lu_machinery import agent_identity, contracts, model_families\n"
        "ROOT = product_root()\n"
    )
    result = run(
        [
            PY,
            "-c",
            PRODUCT_IMPORT_SCRIPT,
        ],
        cwd=str(scratch()),
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return False, {"rc": result.returncode, "err": result.stderr[-400:]}
    outside = [
        path for path in data["machinery"] if site not in Path(path).resolve().parents
    ]
    ok = (
        result.returncode == 0
        and data["root"] == product()
        and data["machinery"]
        and not outside
    )
    return ok, {
        "modules": len(data["machinery"]),
        "outside_release": outside[:5],
        "root_ok": data["root"] == product(),
    }


@probe("skill-cli")
def skill_cli(_: list[str]) -> tuple[bool, dict]:
    """One fleet CLI (plane status) and one shipped skill file, both from the release."""
    # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2 (the kit requires Python 3.12+)
    from importlib.resources import files

    status = module("lu_machinery.fleet_comms", "plane-status", cwd=product())
    try:
        mode = json.loads(status.stdout).get("mode")
    except ValueError:
        mode = None
    skills = files("agents_extensions.shared").joinpath("skills")
    names = (
        sorted(p.name for p in skills.iterdir() if p.joinpath("SKILL.md").is_file())
        if skills.is_dir()
        else []
    )
    ok = status.returncode == 0 and mode == "authority" and "full-send" in names
    return ok, {"plane_mode": mode, "skills": len(names)}


@probe("sources-probe")
def sources_probe(_: list[str]) -> tuple[bool, dict]:
    """The release's Sources watchdog probes the stand-in's /health on the Sources port."""
    healthy = module(
        "lu_machinery.orchestration.sources_watchdog",
        "--dump-dir",
        str(scratch() / "dumps"),
        "--health-url",
        os.environ["DRILL_SOURCES_URL"] + "/health",
        "run-once",
    )
    sick = module(
        "lu_machinery.orchestration.sources_watchdog",
        "--dump-dir",
        str(scratch() / "dumps"),
        "--health-url",
        os.environ["DRILL_SOURCES_URL"] + "/missing",
        "run-once",
    )
    ok = (
        healthy.returncode == 0
        and "healthy" in (healthy.stdout + healthy.stderr)
        and "unhealthy" in (sick.stdout + sick.stderr)
    )
    return ok, {
        "healthy": (healthy.stdout + healthy.stderr)[-300:],
        "sick": (sick.stdout + sick.stderr)[-300:],
    }


@probe("release-integrity")
def release_integrity(_: list[str]) -> tuple[bool, dict]:
    """The installed release is exactly the expected version's RECORD, with no left-over files."""
    import base64
    import sysconfig
    from importlib import metadata

    want = os.environ["DRILL_VERSION"]
    dist = metadata.distribution("lu-machinery")
    site = Path(sysconfig.get_paths()["purelib"]).resolve()
    listed, bad = set(), []
    for item in dist.files or []:
        path = Path(item.locate()).resolve()
        listed.add(path)
        if item.hash and path.is_file():
            digest = (
                base64.urlsafe_b64encode(
                    hashlib.new(item.hash.mode, path.read_bytes()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if digest != item.hash.value:
                bad.append(str(item))
    extra = [
        str(p.relative_to(site))
        for top in ("lu_machinery", "agents_extensions")
        for p in (site / top).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.resolve() not in listed
    ]
    ok = dist.version == want and not bad and not extra
    return ok, {
        "version": dist.version,
        "want": want,
        "hash_mismatch": bad[:5],
        "unlisted": extra[:5],
    }


@probe("stand-in-no-release")
def stand_in_no_release(_: list[str]) -> tuple[bool, dict]:
    """A consumer with no machinery dependency: its interpreter cannot import the release at all."""
    import importlib.util

    spec = importlib.util.find_spec("lu_machinery")
    return spec is None, {
        "interpreter": sys.executable,
        "lu_machinery_importable": spec is not None,
    }


@probe("no-machinery-checkout")
def no_machinery_checkout(_: list[str]) -> tuple[bool, dict]:
    """No Git checkout of machinery is reachable from the drill's world or home."""
    found = []
    for base in (Path(os.environ["HOME"]).parent, Path(os.environ["HOME"])):
        for marker in base.rglob("lu_machinery/__init__.py"):
            if "site-packages" not in marker.parts:
                found.append(str(marker))
    return not found, {"checkouts": found[:5]}


@probe("data-tier")
def data_tier(_: list[str]) -> tuple[bool, dict]:
    """The data-tier runner's own selection, baseline and JUnit logic over a real pytest run.

    The full runner needs a production host (a 6 GiB memory floor, the dispatch slice, real data
    stores mounted read-only, systemd scopes); this probe drives the same functions the runner
    uses on the synthetic product's audited selection: one passing test and one known failure.
    """
    from lu_machinery.ci import data_tier as runner

    checkout = Path(product())
    selection, _digest = runner.load_selection(checkout)
    baseline = runner.load_known_failures(checkout)
    junit = scratch() / "junit.xml"
    result = run(
        [
            PY,
            "-I",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *selection["files"],
        ],
        cwd=product(),
    )
    summary = runner.junit_summary(junit) if junit.exists() else {}
    summary["pytest_exit"] = result.returncode
    failing = summary.get("failing_tests", [])
    issues = [runner.known_issue(nodeid, baseline) for nodeid in failing]
    ok = (
        (summary.get("ran"), summary.get("passed"), summary.get("failed")) == (2, 1, 1)
        and failing == [selection["nodeids"][1]]
        and issues == [1]
        and runner.run_failed(summary)
    )
    return ok, {
        "summary": {
            k: summary.get(k) for k in ("ran", "passed", "failed", "pytest_exit")
        },
        "failing": failing,
        "known_issues": issues,
    }


@probe("tmp-sweep")
def tmp_sweep(_: list[str]) -> tuple[bool, dict]:
    """Temp sweep (dry run) and batch_state retention report over the synthetic state root."""
    sweep = module(
        "lu_machinery.hygiene.tmp_sweep",
        "--temp-root",
        str(scratch() / "tmp"),
        "--task-root",
        str(Path(product()) / "batch_state" / "tasks"),
        "--state-dir",
        str(scratch() / "sweep"),
        "--json",
    )
    retention = module(
        "lu_machinery.hygiene.batch_state_retention",
        "--batch-root",
        str(Path(product()) / "batch_state"),
        "--json",
    )
    try:
        mode = json.loads(sweep.stdout).get("mode")
        schema = json.loads(retention.stdout).get("schema")
    except ValueError:
        mode = schema = None
    ok = (
        sweep.returncode == 0
        and retention.returncode == 0
        and mode == "dry-run"
        and schema == "batch-state-staleness.v1"
    )
    return ok, {
        "sweep_rc": sweep.returncode,
        "mode": mode,
        "retention_rc": retention.returncode,
        "schema": schema,
    }


@probe("wake-watch")
def wake_watch(args: list[str]) -> tuple[bool, dict]:
    """Wake watchers post through Fleet Comms and gate public text through prepublish."""
    fleet_ok, fleet = fleet_roundtrip(args)
    gate_ok, gate = prepublish_fail_closed(args)
    return fleet_ok and gate_ok, {"fleet": fleet, "prepublish": gate}


@probe("mq-guard")
def mq_guard(_: list[str]) -> tuple[bool, dict]:
    """The queue guard's imports load, and the integration sweep reports through the GitHub stand-in."""
    from lu_machinery.opsec import prepublish  # noqa: F401
    from lu_machinery.orchestration import integration_sweep, merge_queue_keeper  # noqa: F401
    from lu_machinery.publish import github  # noqa: F401

    result = module(
        "lu_machinery.orchestration.integration_sweep",
        "--report",
        "--json",
        "--repo",
        "drill/product",
        "--repo-root",
        product(),
    )
    api = stub_calls("gh-api")
    ok = (
        result.returncode == 0
        and result.stdout.strip().startswith(("[", "{"))
        and bool(api)
    )
    return ok, {
        "rc": result.returncode,
        "out": result.stdout[-300:],
        "err": result.stderr[-300:],
        "gh_api_calls": len(api),
    }


@probe("stale-records")
def stale_records(_: list[str]) -> tuple[bool, dict]:
    """Archive moves exactly the two terminal records older than three days, on a private copy."""
    import shutil

    tasks = scratch() / "tasks"
    shutil.copytree(Path(product()) / "batch_state" / "tasks", tasks)
    result = module(
        "lu_machinery.orchestration.stale_task_records",
        "archive",
        "--tasks-dir",
        str(tasks),
        "--min-age-days",
        "3",
        "--apply",
        "--json",
    )
    archived = (
        sorted(path.name for path in (tasks / "archive").glob("*.json"))
        if (tasks / "archive").is_dir()
        else []
    )
    ok = result.returncode == 0 and archived == [
        "drill-old-1.json",
        "drill-old-2.json",
    ]
    return ok, {
        "rc": result.returncode,
        "archived": archived,
        "err": result.stderr[-300:],
    }


@probe("api")
def api(_: list[str]) -> tuple[bool, dict]:
    """Serve the release's Monitor API like its unit does, then GET health, rules and every plain GET route."""
    import socket
    import time
    import urllib.error
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    launcher = (
        "import sys, scripts, uvicorn; from lu_machinery.common.repo_root import machinery_path; "
        "sys.argv[1:1] = ['--log-config', str(machinery_path('api/logging.json'))]; uvicorn.main()"
    )
    log = (scratch() / "api.log").open("w")
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit (fixed argv, no shell)
    server = subprocess.Popen(
        [
            PY,
            "-c",
            launcher,
            "lu_machinery.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=product(),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"

    def get(path: str, timeout: float = 20.0) -> tuple[int, bytes]:
        try:
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected (loopback URL on the port this probe bound)
            with urllib.request.urlopen(base + path, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except OSError:
            return 0, b""

    try:
        deadline = time.monotonic() + 60
        status = 0
        while time.monotonic() < deadline and server.poll() is None:
            status, _ = get("/api/health", timeout=2)
            if status == 200:
                break
            # nosemgrep: python.lang.best-practice.arbitrary-sleep (bounded readiness poll of the server it started)
            time.sleep(0.5)
        if status != 200:
            return False, {
                "step": "start",
                "status": status,
                "log": (scratch() / "api.log").read_text()[-800:],
            }
        rules_status, rules = get("/api/rules?scope=core&format=markdown")
        openapi_status, openapi = get("/openapi.json")
        paths = (
            sorted(
                path
                for path, ops in json.loads(openapi or b"{}").get("paths", {}).items()
                if "get" in ops and "{" not in path
            )
            if openapi_status == 200
            else []
        )
        errors = {}
        for path in paths:
            code, _ = get(path)
            if code == 0 or code >= 500:
                errors[path] = code
        ok = (
            rules_status == 200
            and b"Rules core" in rules
            and bool(paths)
            and not errors
        )
        return ok, {
            "rules_status": rules_status,
            "get_routes": len(paths),
            "server_errors": dict(list(errors.items())[:10]),
        }
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


@probe("review-path")
def review_path(_: list[str]) -> tuple[bool, dict]:
    """The review verifier pins the exact head and refuses a held-out malformed verdict."""
    head = run(["git", "rev-parse", "HEAD"], cwd=product()).stdout.strip()
    manifest = module(
        "lu_machinery.verify_review",
        "--emit-target-manifest",
        "--mode",
        "commit",
        "--commit",
        "HEAD",
        cwd=product(),
    )
    reply = scratch() / "reply.txt"
    reply.write_text("VERDICT: APPROVE\n")
    verdict = module(
        "lu_machinery.verify_review",
        "--review-file",
        str(reply),
        "--mode",
        "commit",
        "--commit",
        "HEAD",
        cwd=product(),
    )
    try:
        target = json.loads(manifest.stdout)
        receipt = json.loads(verdict.stdout)
    except ValueError:
        return False, {
            "manifest": manifest.stdout[-300:],
            "verdict": verdict.stdout[-300:],
            "err": verdict.stderr[-300:],
        }
    ok = (
        manifest.returncode == 0
        and target.get("head_sha") == head
        and receipt.get("final_disposition") == "invalid"
    )
    return ok, {
        "head_pinned": target.get("head_sha") == head,
        "held_out_disposition": receipt.get("final_disposition"),
        "error": receipt.get("error"),
    }


@probe("stand-in-venv")
def stand_in_venv(_: list[str]) -> tuple[bool, dict]:
    """A consumer on the release venv that never imports machinery loads none of it at start-up."""
    loaded = sorted(
        name
        for name in sys.modules
        if name.split(".")[0] in ("lu_machinery", "agents_extensions")
    )
    return not loaded, {
        "interpreter": sys.executable,
        "machinery_modules_at_startup": loaded[:10],
    }


@probe("no-launcher-for-copy")
def no_launcher_for_copy(_: list[str]) -> tuple[bool, dict]:
    """Backup copies stay unlaunched: nothing the release ships or deploys names a .bak file."""
    import re

    # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2 (the kit requires Python 3.12+)
    from importlib.resources import files

    hits = []
    for top in ("packaging", "launchers", "lib"):
        root = files("lu_machinery").joinpath(top)
        for path in Path(str(root)).rglob("*"):
            if path.is_file() and re.search(
                r"\.bak\b", path.read_text(errors="replace")
            ):
                hits.append(str(path.relative_to(Path(str(root)).parent)))
    return not hits, {"references": hits[:5]}


@probe("retired-unit")
def retired_unit(_: list[str]) -> tuple[bool, dict]:
    """A retired unit stays retired: the release ships no unit file for the worktree reaper."""
    # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2 (the kit requires Python 3.12+)
    from importlib.resources import files

    units = [
        p.name for p in files("lu_machinery").joinpath("packaging/systemd").iterdir()
    ]
    reaper = [name for name in units if "reaper" in name]
    return not reaper, {"shipped_units": len(units), "reaper_units": reaper}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in PROBES:
        print(
            f"usage: probes.py {{{','.join(sorted(PROBES))}}} [args]", file=sys.stderr
        )
        return 2
    try:
        ok, detail = PROBES[argv[0]](argv[1:])
    except Exception as exc:  # noqa: BLE001 - a probe that crashes is a failed row, with the reason
        ok, detail = False, {"exception": f"{type(exc).__name__}: {exc}"[:800]}
    print(
        json.dumps(
            {"probe": argv[0], "ok": bool(ok), "detail": detail},
            ensure_ascii=False,
            default=str,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
