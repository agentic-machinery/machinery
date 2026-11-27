"""Live canary evidence a change to the review output contract needs before it merges (#12399).

On 2026-11-01 PR #12372 changed the code-review output contract, passed review and CI,
and then no Codex, Claude or Grok review could settle (#12380, #12394, #12390). Its
tests used fixtures and its own review ran on the old dispatcher. A change to the paths
below therefore enters the merge queue only when, on its exact head, one live read-only
review per required reviewer route has settled ``done`` with a canonical receipt while
the candidate's own dispatcher supervised it.

The evidence is canary evidence, never a review of record. A canary task is pinned to a
head with no review branch and no PR, so ``record_cf_verdict`` refuses it ("review was
not branch-pinned"), and its receipt lives here, outside the PR comments the verdict
lookup reads. Runbook: ``docs/runbooks/write-worker-boundary.md#review-contract-canary``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from lu_machinery import state_paths
from lu_machinery.common import github_client, json_depth
from lu_machinery.common.repo_root import MACHINERY_ENV_VARS, STATE_ROOT_ENV

# The machinery package's directory name, which a change lists its files under, from this module's
# unresolved location: a symlinked package keeps its checkout name, and a short import spelling
# (the package directory on ``sys.path``) cannot shorten it.
_MACHINERY = Path(os.path.abspath(__file__)).parents[1].name

# The one declaration of the review output contract: bound schemas, the receipt gate, the
# verdict parser, the recorder, the dispatcher's settlement, and each reviewer provider's schema
# or reply handling. Every one is a machinery file, named wherever the machinery package is and
# whatever it is called; the schema is packaged.
CONTRACT_PATHS: tuple[str, ...] = (
    "uv.lock",  # the dependencies the canary installs before running any route
    f"{_MACHINERY}/review/contract_canary.py",  # this gate: a change to it is proven on live routes too
    f"{_MACHINERY}/schemas/code-review-findings.v1.schema.json",
    f"{_MACHINERY}/review/review_contract.py",
    f"{_MACHINERY}/review/render_contract.py",
    f"{_MACHINERY}/review/verdict_parser.py",
    f"{_MACHINERY}/review/record_cf_verdict.py",
    f"{_MACHINERY}/dispatcher/worker.py",
    f"{_MACHINERY}/dispatcher/output_schema.py",
    f"{_MACHINERY}/dispatcher/providers/agy.py",
    f"{_MACHINERY}/dispatcher/providers/claude.py",
    f"{_MACHINERY}/dispatcher/providers/codex.py",
    f"{_MACHINERY}/dispatcher/providers/cursor.py",
    f"{_MACHINERY}/dispatcher/providers/muse.py",
    f"{_MACHINERY}/dispatcher/providers/stream_json.py",
)

REASON = "needs-contract-canary"
SCHEMA = "review-contract-canary.v1"
RECEIPT_DIR = "review-contract-canary"
WHEELHOUSE_ENV = "LU_MACHINERY_WHEELHOUSE"  # the name product CI and the dependency audit use
SHA = re.compile(r"[0-9a-f]{40}\Z")
ROUTE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


class CanaryError(ValueError):
    """A canary task or receipt that cannot be canary evidence."""


def touches_contract(paths: Iterable[str]) -> bool:
    """Whether a change to ``paths`` changes the review output contract."""
    return any(path in CONTRACT_PATHS for path in paths)


def _formal_candidates() -> dict[str, list[Any]]:
    """Formal-review candidates by route, from the authoritative catalog."""
    from lu_machinery.review.model_catalog import load_model_catalog
    from lu_machinery.review.reviewer_resolver import REVIEW_CANDIDATES

    models = load_model_catalog()["models"]
    routes: dict[str, list[Any]] = {}
    for candidate in REVIEW_CANDIDATES.values():
        if candidate.formal_review_eligible and models[candidate.concrete_model].get("lifecycle") == "active":
            routes.setdefault(candidate.route, []).append(candidate)
    return routes


def required_routes() -> tuple[str, ...]:
    """Every active formal reviewer route in the catalog. An empty or unreadable catalog raises."""
    try:
        routes = tuple(sorted(_formal_candidates()))
    except Exception as exc:
        raise CanaryError("reviewer routes unavailable") from exc
    if not routes or not all(ROUTE.fullmatch(route) for route in routes):
        raise CanaryError("reviewer routes unavailable")
    return routes


def _route_models(candidates: Sequence[Any]) -> set[str]:
    """The ``--model`` spellings a dispatch to these candidates records."""
    from lu_machinery.dispatcher.providers.agy import transport_model
    from lu_machinery.review.reviewer_resolver import candidate_dispatch_model

    names: set[str] = set()
    for candidate in candidates:
        names.update({candidate.concrete_model, candidate_dispatch_model(candidate)})
        if candidate.route == "agy":
            names.add(transport_model(candidate.concrete_model))
    return names


def _receipt_gated(record: Mapping[str, Any]) -> bool:
    """Whether the dispatcher's canonical receipt gate decided this task's settlement."""
    from lu_machinery.review.review_contract import schema_path

    return (
        record.get("require_review_verdict") is True
        and record.get("schema_version") == 2
        and record.get("review_profile") in {"code", "infra"}
        and not record.get("review_attempt")
        and schema_path(profile=record.get("review_profile"), task=record) is not None
    )


def _attested_model(record: Mapping[str, Any]) -> object:
    """The model that ran, by the verdict recorder's attestation rules where they apply.

    A provider that reports its model from its own stream must have named a permitted one;
    Cursor's display names map to catalog ids as the recorder maps them.
    """
    from lu_machinery.review.record_cf_verdict import reviewer_identity

    if record.get("agent") in {"cursor", "grok", "grok-build"}:
        try:
            return reviewer_identity(record)[0]
        except Exception:  # unattested, unknown or unreadable: no executor
            return None
    if "resolved_model_known" in record:
        return record.get("resolved_model") if record["resolved_model_known"] is True else None
    return record.get("model")


def _root() -> Path:
    from lu_machinery.common.repo_root import state_root

    return state_root()


def receipt_root(root: Path | None = None) -> Path:
    return state_paths.contract_canary_dir(RECEIPT_DIR, root=root or _root())


def _receipt_path(head: str, route: str, root: Path | None = None) -> Path:
    if not SHA.fullmatch(head) or not ROUTE.fullmatch(route):
        raise CanaryError("receipt identity invalid")
    return receipt_root(root) / head / f"{route}.json"


def _settled_receipt(record: Mapping[str, Any], result: str, *, head: str) -> dict:
    """The receipt a settled task supports, from its settlement facts and the gate's catalog.

    The receipt is bound to the exact head commit, which fixes the contract assets and the
    whole dispatcher tree, to the clean dispatcher checkout at that head that settled the
    task, read-only from start to settlement so that no process could write code into it,
    and to the route and model. A verdict-bound task settles ``done`` only after that
    candidate dispatcher's own receipt gate accepted its reply, so rebuilding this receipt
    needs the settlement facts rather than another parse of the reply.
    """
    if not SHA.fullmatch(head):
        raise CanaryError("PR head is not a full commit SHA")
    checks = (
        (record.get("status") == "done", "task did not settle done"),
        (record.get("mode") == "read-only", "task is not read-only"),
        # Only a verdict-bound code or infra task outside a bound review attempt, and not classified
        # Ukrainian, settles through the dispatcher's canonical receipt gate (worker._receipt_failure).
        (_receipt_gated(record), "task did not settle through the receipt gate"),
        # The worktree started at the PR head and was still there when the task settled.
        (record.get("worktree_start_sha") == head and record.get("head_sha") == head,
         "task did not review the PR head"),
        # A canary is never a review of record: a branch- or PR-pinned task could be recorded as one.
        (not record.get("review_branch") and not record.get("pr"), "task is branch- or PR-pinned"),
    )
    for ok, why in checks:
        if not ok:
            raise CanaryError(why)
    code = record.get("dispatcher_code")
    if (
        not isinstance(code, Mapping)
        or code.get("head") != head
        or code.get("clean") is not True
        or record.get("dispatcher_code_start") != code
    ):
        raise CanaryError("task was not supervised by the candidate's clean dispatcher")
    if code.get("read_only") is not True:  # equal start and settlement records: sealed at both
        raise CanaryError("task's dispatcher checkout was not a read-only mount")
    route = record.get("agent")
    candidates = _formal_candidates()
    if route not in candidates:
        raise CanaryError(f"route {route!r} is not a required reviewer route")
    permitted = _route_models(candidates[route])
    if record.get("model") not in permitted:
        raise CanaryError(f"model {record.get('model')!r} is not a formal reviewer on route {route}")
    if _attested_model(record) not in permitted:
        raise CanaryError(f"route {route} did not attest a formal reviewer as the model that ran")
    if record.get("result_sha256") != hashlib.sha256(result.encode()).hexdigest():
        raise CanaryError("result does not match the task record")
    return {
        "schema": SCHEMA,
        "eligible_as_review": False,
        "head": head,
        "route": route,
        "model": record["model"],
        "task_id": record.get("task_id"),
        "result_sha256": record["result_sha256"],
        "dispatcher_head": code["head"],
    }


def build_receipt(record: Mapping[str, Any], result: str, *, head: str) -> dict:
    """The canary receipt for one settled task, or ``CanaryError`` naming why it is not one.

    Recording also checks the reply is a canonical receipt with the recorder's copy of the
    contract, which is the candidate's when ``record`` runs from the candidate worktree.
    """
    from lu_machinery.review.review_contract import (
        ContractError,
        canonical_receipt_payload,
    )

    receipt = _settled_receipt(record, result, head=head)
    try:
        canonical_receipt_payload(result)
    except ContractError as exc:
        raise CanaryError("result is not a canonical receipt") from exc
    return receipt


def refusal(paths: Iterable[str], head: str, root: Path | None = None) -> str | None:
    """None when the change may queue; otherwise the typed detail of ``REASON``.

    A change outside ``CONTRACT_PATHS`` needs nothing. A contract change needs a valid
    receipt for every required route at ``head``, and each receipt must still be what its
    settled task record and reply build. An unknown route list, an unreadable receipt or
    task, and a receipt its task does not rebuild all refuse.
    """
    if not touches_contract(paths):
        return None
    try:
        routes = required_routes()
    except Exception as exc:  # an unreadable catalog must refuse, never crash the caller's run
        return f"unverifiable: {exc}" if isinstance(exc, CanaryError) else f"unverifiable: {type(exc).__name__}"
    missing: list[str] = []
    for route in routes:
        try:
            receipt = json_depth.loads(_receipt_path(head, route, root).read_text(encoding="utf-8"), max_depth=512)
        except FileNotFoundError:
            missing.append(route)
            continue
        except Exception:  # unreadable, malformed or too deeply nested: refuse, never crash
            return f"unverifiable: receipt for {route}"
        if not isinstance(receipt, dict) or (
            receipt.get("schema"), receipt.get("eligible_as_review"), receipt.get("head"), receipt.get("route")
        ) != (SCHEMA, False, head, route):
            return f"unverifiable: receipt for {route}"
        try:
            # The file is a claim; the settled task it names is the evidence.
            task, result = _load_task(str(receipt.get("task_id")))
            rebuilt = _settled_receipt(task, result, head=head)
        except Exception:
            return f"unverifiable: task behind the receipt for {route}"
        if rebuilt != receipt:
            return f"unverifiable: receipt for {route} does not match its task"
    return f"missing: {','.join(missing)}" if missing else None


def _load_task(task_id: str) -> tuple[dict, str]:
    """A dispatcher task record and its reply, decoded without newline translation."""
    from lu_machinery.dispatcher import store

    task = store.read(task_id)
    result_file = task.get("result_file")
    if not isinstance(result_file, str):
        raise CanaryError("task has no result")
    return task, Path(result_file).read_bytes().decode("utf-8")


def record(task_id: str, root: Path | None = None) -> Path:
    """Write the receipt for one settled canary task at the head it reviewed.

    The release cut checks receipts only at the candidate's exact head, so a receipt
    for any other head never admits a release.
    """
    task, result = _load_task(task_id)
    receipt = build_receipt(task, result, head=str(task.get("head_sha")))
    path = _receipt_path(receipt["head"], receipt["route"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


_RUN_MODELS = {
    "codex": ("gpt-6.2-sol", "--codex-account", "sub"),
    "claude": ("claude-sonnet-5-5", "--claude-account", "b"),
    "cursor": ("grok-4.7-high",),
    "agy": ("gemini-4-neon-high",),
}
_CODEX_UNIT_UNSET = ("CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")

# Run inside the candidate interpreter, with the existing dispatcher and recorder.
# Keep dispatch and wait in one user unit; receipts are never reviews of record.
_RUN_ROUTE = """\
import contextlib, io, signal, sys
from lu_machinery.dispatcher.cli import main
from lu_machinery.review.contract_canary import record
def interrupted(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, interrupted)
identity = None
settled = False
try:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(sys.argv[1:])
    if code:
        # Dispatch may have started a supervisor before reporting an error.
        # Without a task identity and terminal wait, settlement is unknown.
        print(output.getvalue())
        raise SystemExit(code)
    task_id, nonce = output.getvalue().strip().splitlines()[-2:]
    identity = [task_id, '--run-nonce', nonce]
    code = main(['wait', *identity])
    settled = code in (0, 1)
    if code:
        raise SystemExit(code)
    print(record(task_id))
finally:
    if identity and not settled:
        main(['cancel', *identity])
        settled = main(['wait', *identity]) in (0, 1)
    if settled:
        print('CANARY-ROUTE-SETTLED')
"""


def _invoke(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Use the shared GitHub client seam, including its non-GitHub argv transport."""
    result = cast(
        subprocess.CompletedProcess[str],
        github_client.run(list(argv), cwd=cwd, env=dict(env), text=True, capture_output=True, check=False),
    )
    if check and result.returncode:
        raise CanaryError(f"command failed ({result.returncode}): {shlex.join(argv)}\n{result.stderr.strip()}")
    return result


def _run_steps(
    work: Path, source: Path, repo_key: str, repo: str, head: str, interpreter: Path, routes: Sequence[str],
    hard_timeout: int, env: Mapping[str, str], wheelhouse: str,
) -> tuple[list[list[str]], list[tuple[str, list[str]]], list[str]]:
    """Build the exact argv plan without creating files, units, mounts or tasks.

    The checkout holds only the candidate commit (a depth-1 fetch, never the repository's
    history), and ``run`` removes it once the image is built. With a wheelhouse, the consumer
    venv also finds wheels there; the lock's hashes still decide which bytes install.
    """
    from lu_machinery.common.git_credentials import SELECTOR_ENV

    clone, mount = work / "source", work / "mount"
    python = str(work / "consumer/bin/python")
    url = f"https://github.com/{repo}.git"
    setup = [
        ["git", "init", "--quiet", str(clone)],
        ["git", "-C", str(clone), "fetch", "--quiet", "--no-tags", "--depth", "1", url, head],
        ["git", "-C", str(clone), "-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", head],
        # Dispatch creates its review worktrees in the registered primary; give it this exact object too.
        # Not from the shallow checkout: Git rejects a shallow root with only a warning and exit 0.
        ["git", "-C", str(source), "fetch", "--quiet", "--no-tags", url, head],
        ["mksquashfs", str(clone), str(work / "candidate.squashfs"), "-noappend", "-processors", "1"],
        ["snapfuse", str(work / "candidate.squashfs"), str(mount), "-o", "ro"],
        # The candidate's runtime closure from its own uv.lock, as release_cut exports it.
        ["uv", "export", "--locked", "--quiet", "--format", "requirements.txt", "--no-emit-project",
         "--no-default-groups", "--all-extras", "--no-extra", "test",
         "--project", str(mount), "--output-file", str(work / "runtime-requirements.txt")],
        [str(interpreter), "-I", "-m", "venv", str(work / "consumer")],
        [python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
         "--require-hashes", "--only-binary=:all:", *(["--find-links", wheelhouse] if wheelhouse else []),
         "-r", str(work / "runtime-requirements.txt")],
        [python, "-I", "-c", "import pathlib, sys, sysconfig; pathlib.Path(sysconfig.get_path('purelib'), 'contract-canary.pth').write_text(sys.argv[1] + '\\n')",
         str(mount)],
        [python, "-I", "-B", "-c", "from lu_machinery.dispatcher.worker import _dispatcher_code; import sys; c = _dispatcher_code(); assert c['head'] == sys.argv[1] and c['clean'] and c['read_only'], c", head],
    ]
    prompt = (
        f"Read-only review-contract canary at {head}. Inspect lu_machinery/review/contract_canary.py "
        "and report any concrete correctness defect you can verify. Do not change files or dispatch tasks. "
        "Return exactly the canonical code-review-findings.v1 JSON object required by your output contract: "
        "schema_version, overall (correctness, explanation, confidence), findings. "
        "An empty findings list is permitted if no defect is verified. This is canary evidence, "
        "never a review of record."
    )
    commands = []
    # Passing named context explicitly is necessary: the user manager does not inherit this shell's env.
    context = ("PATH", "LU_PRODUCT_ROOT", "LU_TASKS_DIR", SELECTOR_ENV,
               "FLEET_COMMS_ROOT", "LU_GITHUB_CACHE_DIR", *MACHINERY_ENV_VARS)
    unset_context = [key for key in context if not env.get(key)]
    for route in routes:
        model, *account = _RUN_MODELS[route]
        unit = f"{work.name.lstrip('.')}-{route}"
        argv = ["systemd-run", "--user", "--wait", "--pipe", "--collect", "--service-type=exec",
                f"--unit={unit}", f"--working-directory={work}"]
        # Account a is the login under HOME; an inherited Codex home or key would replace it.
        unset = [*unset_context, *_CODEX_UNIT_UNSET] if route == "codex" else unset_context
        if unset:
            argv.append(f"--property=UnsetEnvironment={' '.join(unset)}")
        argv += [f"--setenv={key}={env[key]}" for key in context if env.get(key)]
        argv += [python, "-I", "-B", "-c", _RUN_ROUTE, "dispatch", "--repo", repo_key,
                 "--agent", route, "--model", model, *account, "--effort", "high", "--mode", "read-only",
                 "--worktree", "--task-id", f"{unit}", "--pinned-head", head, "--base", head,
                 "--review-profile", "code", "--require-review-verdict", "--hard-timeout", str(hard_timeout),
                 "--prompt", prompt]
        commands.append((route, argv))
    unmount = [shutil.which("fusermount3") or "fusermount", "-u", str(mount)]
    return setup, commands, unmount


# The work directory peaks while the image is built: the depth-1 checkout (its tree plus a
# compressed pack) and the image beside it, at most twice the tree's blob bytes. The checkout
# is then removed; the consumer venv (about 110 MiB) and file-system slack fit the allowance.
# Measured 2026-11-16 on a 747 MB product tree: 962 MiB checkout, 331 MiB image, 108 MiB venv.
_TREE_PEAK_FACTOR = 2
_VENV_ALLOWANCE = 512 * 2**20


def _space_refusal(home: Path, source: Path, env: Mapping[str, str]) -> str | None:
    """Why the work directory's file system cannot take this run, or None.

    A run that would leave the file system at or above the dispatch admission's refuse level
    (``df`` percent used) would stop every dispatch on the host, so it never starts. The need
    is estimated from the registered checkout's current tree; an unreadable disk refuses.
    """
    from lu_machinery.orchestration.dispatch_admission import load_thresholds, read_disk

    limit = load_thresholds().disk_refuse_used_percent
    listing = _invoke(["git", "-C", str(source), "ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"],
                      cwd=home, env=env).stdout
    tree = 0
    for entry in filter(None, listing.split("\0")):
        _mode, kind, _oid, size = entry.partition("\t")[0].split()  # a malformed line raises: refuse
        if kind == "blob":
            tree += int(size)
    need = _TREE_PEAK_FACTOR * tree + _VENV_ALLOWANCE
    disk = read_disk(home, shutil.disk_usage)
    if disk.used_bytes is None or disk.free_bytes is None:
        return f"cannot read free space under {home}: {disk.text()}"
    after = 100.0 * (disk.used_bytes + need) / (disk.used_bytes + disk.free_bytes)
    if after >= limit:
        return (f"refusing to start: the canary needs about {need / 2**30:.1f} GiB under {home}, which would "
                f"take its file system from {disk.text()} to {after:.1f}% used; dispatch admission refuses "
                f"at {limit:g}%. Free space first.")
    return None


def run(args: argparse.Namespace) -> int:
    """Host-only orchestration of the established sealed-checkout canary procedure."""
    from lu_machinery.common.git_context import sanitized_git_env
    from lu_machinery.common.repo_root import (
        primary_root,
        product_root,
        project_interpreter,
        state_root,
    )
    from lu_machinery.dispatcher.git import resolve_repo
    from lu_machinery.orchestration.fleet_repos import FleetRepoError, resolve_active_fleet_repo

    if not args.dry_run and os.environ.get("LEARN_UKRAINIAN_DISPATCH_TASK_ID"):
        raise CanaryError("run must execute on the host, outside a dispatched worker; use --dry-run here")
    source, repository = resolve_repo(args.repo)
    if repository["key"] != "machinery":
        # A consumer tree has no lu_machinery package: the installed wheel, not the candidate, would
        # supervise its routes, and the attestation step refuses that before any provider call.
        raise CanaryError(f"--repo {args.repo}: only machinery heads can be canaried; this runner cannot attest "
                          f"the dispatcher of a {repository['key']} head")
    try:
        # Read and clone at the row's active slug; a transferred row without a trusted selector fails closed.
        github = resolve_active_fleet_repo(repository["key"]).active_github
    except FleetRepoError as exc:
        raise CanaryError(f"--repo {args.repo}: {exc}") from None
    env = sanitized_git_env(strip_config_injection=True)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    primary = str(primary_root())
    state = state_root()
    # Native hooks reserve the compatibility state variable for the Git checkout.
    env.pop(STATE_ROOT_ENV, None)
    # Keep dispatch, wait, record and score on the same plane, even if the user
    # manager has old root settings or this caller has a task-scoped override.
    env.update(
        LU_PRIMARY_REPO_ROOT=primary,
        LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=primary,
        LU_PRODUCT_ROOT=str(product_root()),
        LU_STATE_ROOT=str(state),
        LU_TASKS_DIR=str(state_paths.tasks_dir(state)),
    )
    head = args.head
    if args.pr is not None:
        result = github_client.run(
            ["gh", "pr", "view", str(args.pr), "--repo", github, "--json", "headRefOid"],
            cwd=source, env=env, text=True, capture_output=True, check=True,
        )
        head = json_depth.loads(result.stdout, max_depth=512)["headRefOid"]
    if not isinstance(head, str) or not SHA.fullmatch(head):
        raise CanaryError("--head must be a full lowercase 40-character commit SHA")
    if args.hard_timeout <= 0:
        raise CanaryError("--hard-timeout must be positive")
    routes = required_routes()
    if set(routes) != set(_RUN_MODELS):
        raise CanaryError("required routes changed; update the supported canary model map before running")
    interpreter = project_interpreter()
    env.update(LU_PYTHON=str(interpreter), LEARN_UKRAINIAN_PYTHON=str(interpreter))
    wheelhouse = os.environ.get(WHEELHOUSE_ENV, "")
    if wheelhouse and not (os.path.isabs(wheelhouse) and os.path.isdir(wheelhouse)):
        raise CanaryError(f"{WHEELHOUSE_ENV} must be the absolute path of an existing wheelhouse directory")
    if args.dry_run:
        work = Path.home() / ".lu-contract-canary-DRY-RUN"
    elif refused := _space_refusal(Path.home(), source, env):
        raise CanaryError(refused)  # before anything is created
    else:
        work = Path(tempfile.mkdtemp(prefix=".lu-contract-canary-", dir=Path.home()))
    setup, route_commands, unmount = _run_steps(
        work, source, repository["key"], github, head, interpreter, routes, args.hard_timeout, env,
        wheelhouse,
    )
    # Force a full score even if this head's changed files do not touch the contract.
    # The sealed candidate supplies the gate and catalog that will be installed together.
    check = [str(work / "consumer/bin/python"), "-I", "-B", "-m", "lu_machinery.review.contract_canary",
             "check", "--head", head, *args.paths, f"{_MACHINERY}/review/contract_canary.py"]
    if args.dry_run:
        print("DRY RUN: no files, mount, user unit, tasks or receipts created; no live proof")
        print(f"refuse unless {Path.home()} has room for about twice the registered checkout's tree plus "
              f"{_VENV_ALLOWANCE // 2**20} MiB below the dispatch admission's disk refuse level")
        if not wheelhouse:
            print(f"{WHEELHOUSE_ENV} is unset: pip uses the package index only")
        print(f"mkdir {work / 'mount'} (temporary directory under home; fusermount refuses /var/tmp)")
        for command in setup:
            print(shlex.join(command))
            if command[0] == "mksquashfs":
                print(f"remove {work / 'source'} (the image holds the candidate)")
        for command in [*(argv for _, argv in route_commands), check, unmount]:
            print(shlex.join(command))
        print(f"remove temporary directory {work} after successful unmount")
        return 0
    mount_attempted = False
    pending: dict[str, Future[subprocess.CompletedProcess[str]]] = {}
    submitting_route: str | None = None
    try:
        (work / "mount").mkdir()
        for command in setup:
            if command[0] == "snapfuse":
                mount_attempted = True
            _invoke(command, cwd=work, env=env)
            if command[0] == "mksquashfs":
                # The image holds the candidate; keeping the checkout would only raise the peak.
                shutil.rmtree(work / "source")
        with (work / "candidate.squashfs").open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        print(f"head {head}; squashfs sha256:{digest}", flush=True)
        failures = 0
        # Each existing unit still dispatches, waits and records its route. Submit all
        # units before collecting results; the context joins every unit even on interrupt.
        with ThreadPoolExecutor(max_workers=len(route_commands)) as executor:
            for route, command in route_commands:
                submitting_route = route
                pending[route] = executor.submit(_invoke, command, cwd=work, env=env, check=False)
                submitting_route = None
            for route, future in pending.items():
                result = future.result()
                if "CANARY-ROUTE-SETTLED" not in result.stdout.splitlines():
                    print(result.stdout, end="")
                    print(result.stderr, end="", file=sys.stderr)
                    raise CanaryError(f"{route}: user unit returned without terminal-task confirmation")
                failures += bool(result.returncode)
                print(f"{route}: {'FAIL' if result.returncode else 'PASS'} (exit {result.returncode})", flush=True)
                if result.returncode:
                    print(result.stdout, end="")
                    print(result.stderr, end="", file=sys.stderr)
        result = _invoke(check, cwd=work, env=env, check=False)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return 1 if failures or result.returncode else 0
    finally:
        unknown = [
            route for route, future in pending.items()
            if not future.done() or future.exception() is not None
            or "CANARY-ROUTE-SETTLED" not in future.result().stdout.splitlines()
        ]
        # submit can queue work and then be interrupted before returning its future.
        if submitting_route is not None and submitting_route not in pending:
            unknown.append(submitting_route)
        if unknown:
            # No candidate code may disappear while any detached task might still use it.
            raise CanaryError(f"{','.join(unknown)}: route completion unknown; retained mount and environment at {work}; "
                              "settle its tasks before unmounting")
        # Never traverse/remove a still-mounted checkout when unmount fails.
        if mount_attempted and os.path.ismount(work / "mount"):
            try:
                _invoke(unmount, cwd=work, env=env)
                if os.path.ismount(work / "mount"):
                    raise CanaryError("unmount returned success but the candidate is still mounted")
            except BaseException:
                print(f"cleanup failed; retained {work}; unmount with {shlex.join(unmount)}", file=sys.stderr)
                raise
        shutil.rmtree(work)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] + "\nUse run on the host; canaries are not reviews of record.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  .venv/bin/python -m lu_machinery.review.contract_canary run --repo machinery --pr 585 --dry-run\n"
        "  .venv/bin/python -m lu_machinery.review.contract_canary check --head <40-hex-sha> lu_machinery/dispatcher/worker.py\n"
        "Outputs: run builds a sealed checkout, dispatches live tasks, records receipts and removes its mount.\n"
        "Exit codes: 0 pass (or dry-run only), 1 canary failure, 2 invalid input/setup/cleanup failure.\n"
        "Related: docs/runbooks/write-worker-boundary.md#review-contract-canary; machinery #585.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("routes", help="Print the required reviewer routes")
    rec = sub.add_parser("record", help="Record one settled canary task as canary evidence")
    rec.add_argument("--task-id", required=True, help="Settled task ID, e.g. contract-canary-codex")
    chk = sub.add_parser("check", help="Print the refusal for a PR head, if any")
    chk.add_argument("--head", required=True, help="Exact reviewed commit, 40 lowercase hex characters")
    chk.add_argument("paths", nargs="+", help="Changed repository-relative files, e.g. lu_machinery/dispatcher/worker.py")
    runner = sub.add_parser(
        "run", help="Build, run, record and score the sealed-checkout canary on the host",
        description="Orchestrate the four-route review-contract canary at an exact head.\n"
        "Run on the host with a working user bus and existing provider credentials; workers use --dry-run.\n"
        f"Only machinery heads are supported. {WHEELHOUSE_ENV}, when set, is an absolute wheelhouse\n"
        "path pip also searches. A run refuses before creating\n"
        "anything when its estimated disk need would reach the dispatch admission's disk refuse level.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        f"  {WHEELHOUSE_ENV}=<wheelhouse> .venv/bin/python -m lu_machinery.review.contract_canary run --repo machinery --pr 585\n"
        "  .venv/bin/python -m lu_machinery.review.contract_canary run --head <40-hex-sha> --dry-run\n"
        "Outputs: temporary SquashFS/venv under home (the depth-1 checkout is removed once the image is built);\n"
        "live read-only tasks and shared canary receipts.\n"
        "The mount and temporary directory are removed; failed unmount retains the directory for recovery.\n"
        "Exit codes: 0 all routes and check pass, or dry-run plan; 1 route/check failure;\n"
        "2 invalid input, too little disk, setup or cleanup failure.\n"
        "Related: docs/runbooks/write-worker-boundary.md#review-contract-canary.",
    )
    runner.add_argument("--repo", default="machinery",
                        help="Fleet registry key holding the candidate code; only machinery is supported (default: machinery)")
    target = runner.add_mutually_exclusive_group(required=True)
    target.add_argument("--head", help="Exact candidate commit, 40 lowercase hex characters (default: from --pr)")
    target.add_argument("--pr", type=int, help="PR whose head to resolve once, e.g. 585 (default: use --head)")
    runner.add_argument("--hard-timeout", type=int, default=1200, help="Positive per-route deadline in seconds (default: 1200)")
    runner.add_argument("--dry-run", action="store_true", help="Print argv plan without creating files or dispatching (default: off)")
    runner.add_argument("paths", nargs="*", help="Changed repository-relative files for check (default: the contract gate itself)")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run(args)
        if args.command == "routes":
            print("\n".join(required_routes()))
        elif args.command == "record":
            print(record(args.task_id))
        else:
            detail = refusal(args.paths, args.head)
            print(f"{REASON}: {detail}" if detail else "ok")
            return 1 if detail else 0
    except (CanaryError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"{REASON}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
