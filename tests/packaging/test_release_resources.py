"""Release resources must work without a machinery checkout or PYTHONPATH."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

from lu_machinery.common.repo_root import machinery_path
from tests.helpers.wheel import release_wheel

REPO = Path(__file__).resolve().parents[2]
SHARED = "agents_extensions/shared/"


def _run(command, *, cwd, env=None):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """Install the head-built wheel into an isolated consumer venv."""
    scratch = tmp_path_factory.mktemp("release-resources")
    wheel = release_wheel(REPO, scratch)
    consumer = scratch / "consumer"
    venv.EnvBuilder(with_pip=True).create(consumer)
    executable = consumer / "bin" / "python"
    installed = _run(
        [
            str(executable),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-compile",
            "--no-deps",
            str(wheel),
        ],
        cwd=scratch,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    # The deployer uses its declared runtime YAML dependency to lint product skills.
    yaml_installed = _run(
        [str(executable), "-I", "-m", "pip", "install", "--disable-pip-version-check",
         "--only-binary=:all:", "PyYAML==6.0.3"], cwd=scratch,
    )
    assert yaml_installed.returncode == 0, yaml_installed.stdout + yaml_installed.stderr
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LU_", "LEARN_UKRAINIAN_"))}
    env["PYTHONPATH"] = ""
    return scratch, wheel, executable, env


def test_wheel_omission_or_product_leakage(release):
    """Every declared release resource ships with its bytes; nothing undeclared ships."""
    _, wheel, _, _ = release
    tracked = set(subprocess.check_output(["git", "ls-files", SHARED], cwd=REPO, text=True).splitlines())
    # The release inventory is the declaration of what this release owns and deploys.
    declared = {SHARED + name for name in json.loads(machinery_path("agent_deploy/release.v1.json").read_text())["files"]}
    assert tracked == declared
    with zipfile.ZipFile(wheel) as archive:
        shipped = {name for name in archive.namelist() if name.startswith(SHARED)}
        assert shipped == declared
        assert not any(name.endswith("/__init__.py") for name in shipped)
        for name in declared:
            assert archive.read(name) == (REPO / name).read_bytes(), name


def test_checkout_dependent_resource_resolution(release):
    """An isolated installed consumer gets source-identical core, task and product bytes."""
    scratch, _, executable, env = release
    product = scratch / "product"
    product.mkdir()
    addendum = product / SHARED / "rules/core-curriculum.md"
    addendum.parent.mkdir(parents=True)
    addendum.write_text("# Selected product curriculum\n", encoding="utf-8")
    script = """
import hashlib, json, os, sys
from lu_machinery.lib import rules_core as r
scopes = ("core", "task:repo-change", "task:review", "task:driver")
result = {}
for scope in scopes:
    sources = r.scope_sources(scope)
    result[scope] = {"sha256": hashlib.sha256(r.assemble(sources).encode()).hexdigest(),
                     "origins": [str(r.source_path(s).resolve()) for s in sources]}
result["content"] = r.core_text("content", root=__import__("pathlib").Path(sys.argv[1]))
result["shared_without_product"] = str(r.source_path("agents_extensions/shared").resolve())
os.environ["LU_PRODUCT_ROOT"] = sys.argv[1]
result["shared_with_product"] = str(r.source_path("agents_extensions/shared").resolve())
result["core_with_product"] = r.core_text()
result["shell"] = str(__import__("pathlib").Path(r.__file__).with_suffix(".sh"))
# The shell no-print guard runs from the release even though this product has no hooks copy.
from lu_machinery.agent_runtime import secret_print_hook as guard
result["guard"] = str(guard.guard_path().resolve())
result["guard_decisions"] = [guard.run_guard(command)[0] for command in ("printf OK", "env")]
os.environ.pop("LU_PRODUCT_ROOT")
try:
    r.core_text("content")
except r.RulesCoreMissing:
    result["no_product"] = "refused"
else:
    result["no_product"] = "accepted"
print(json.dumps(result))
"""
    ran = _run([str(executable), "-I", "-c", script, str(product)], cwd=scratch, env=env)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    data = json.loads(ran.stdout)
    source_sets = {
        "core": ["rules/core.md"],
        "task:repo-change": [
            "rules/critical-rules.md",
            "rules/dispatch-must-use-worktree.md",
            "rules/workflow.md",
            "rules/design-charter.md",
        ],
        "task:review": ["skills/local-code-review/SKILL.md"],
        "task:driver": [
            "rules/fleet-comms-coordination.md",
            "rules/fleet-driver-routing.md",
            "rules/fleet-shared-doctrine.md",
            "rules/fleet-role-scorecard.md",
            "skills/drive-epic/SKILL.md",
        ],
    }
    for scope, names in source_sets.items():
        bodies = [(REPO / SHARED / name).read_text().rstrip() + "\n" for name in names]
        expected = "\n\n---\n\n".join(bodies).rstrip() + "\n"
        assert data[scope]["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
        assert all(Path(origin).is_relative_to(executable.parent.parent) for origin in data[scope]["origins"])
    core = (REPO / SHARED / "rules/core.md").read_text().rstrip() + "\n"
    assert data["content"] == core + "\n\n---\n\n# Selected product curriculum\n"
    assert data["no_product"] == "refused"
    assert data["shared_with_product"] == data["shared_without_product"]
    assert Path(data["shared_without_product"]).is_relative_to(executable.parent.parent)
    assert data["core_with_product"] == core
    assert not (product / SHARED / "hooks").exists()
    assert Path(data["guard"]).is_relative_to(executable.parent.parent)
    assert Path(data["guard"]).read_bytes() == (REPO / SHARED / "hooks/guard-secret-print.py").read_bytes()
    assert data["guard_decisions"] == [True, False]
    expected_block = (
        f'<rules-core seat="core" sha256="{hashlib.sha256(core.encode()).hexdigest()}">\n{core}</rules-core>'
    )
    shell_env = {**env, "TMPDIR": str(scratch / "shell-tmp")}
    Path(shell_env["TMPDIR"]).mkdir()
    helper = scratch / "launcher-helper"
    helper.mkdir()
    (helper / ".venv").symlink_to(executable.parent.parent, target_is_directory=True)
    shell_script = r"""if [ "$3" = launcher ]; then
  source "${1%rules_core.sh}launcher_core.sh"
  LC_DURABLE_HELPER_ROOT="$4"
  LC_MODE=worker LC_PROVIDER=claude LC_DRY_RUN=0
  launcher_load_rules_core
else
  source "$1"
  rules_core_load "$2" "" claude || exit 1
fi
case "$3" in
  block|launcher) printf '%s' "$LC_RULES_CORE" ;;
  toml) rules_core_toml "$2" ;;
esac
"""
    for rendering in ("block", "toml", "launcher"):
        shell = _run(
            ["bash", "-c", shell_script, "rules-shell", data["shell"], str(executable), rendering, str(helper)],
            cwd=scratch,
            env=shell_env,
        )
        assert shell.returncode == 0, shell.stdout + shell.stderr
        if rendering in {"block", "launcher"}:
            assert shell.stdout == expected_block
        elif rendering == "toml":
            assert tomllib.loads("value = " + shell.stdout)["value"] == expected_block


def test_missing_resource_admission_bypass(release):
    """A missing installed core refuses before a caller creates its side-effect marker."""
    scratch, _, executable, env = release
    locate = _run(
        [
            str(executable),
            "-I",
            "-c",
            "from lu_machinery.lib.rules_core import source_path, CORE_REL; print(source_path(CORE_REL))",
        ],
        cwd=scratch,
        env=env,
    )
    assert locate.returncode == 0, locate.stderr
    core = Path(locate.stdout.strip())
    saved = core.read_bytes()
    marker = scratch / "started"
    core.unlink()
    try:
        ran = _run(
            [
                str(executable),
                "-I",
                "-c",
                "from lu_machinery.lib.rules_core import require_core; require_core(); "
                "from pathlib import Path; Path('started').touch()",
            ],
            cwd=scratch,
            env=env,
        )
        assert ran.returncode != 0
        assert "RulesCoreMissing" in ran.stderr
        assert "core.md" in ran.stderr
        assert not marker.exists()
    finally:
        core.write_bytes(saved)


# Each harness's native skill discovery directories, fixed here so a manifest entry cannot redefine them.
NATIVE_SKILLS = {"claude": (".claude/skills",), "codex": (".agents/skills",),
                 "agy": (".agent/skills", ".agents/skills"), "cursor": (".cursor/skills",)}
HARNESSES = tuple(NATIVE_SKILLS)
CATALOG = """
import json, sys, types
from lu_machinery.dispatcher import runtime_context
raw = runtime_context.prompt(types.SimpleNamespace(), b"")
context = json.loads(raw.split(b"<dispatch-context>\\n", 1)[1].split(b"\\n</dispatch-context>", 1)[0])
print(json.dumps({"shared": str(runtime_context.shared_root()), "skills": context["skills"]}))
"""


def _catalog(release):
    """The skills the installed dispatcher advertises to every adapter, keyed by release path."""
    scratch, _, executable, env = release
    tasks = scratch / "tasks"
    tasks.mkdir(exist_ok=True)
    ran = _run([str(executable), "-I", "-c", CATALOG], cwd=scratch, env={**env, "LU_TASKS_DIR": str(tasks)})
    assert ran.returncode == 0, ran.stdout + ran.stderr
    data = json.loads(ran.stdout)
    shared = Path(data["shared"])
    assert shared.is_relative_to(executable.parent.parent)
    return {Path(skill["path"]).relative_to(shared).as_posix(): Path(skill["path"]) for skill in data["skills"]}


def _wheel_digests(wheel):
    """SHA-256 of each shipped file as the wheel's RECORD declares it."""
    with zipfile.ZipFile(wheel) as archive:
        record = next(name for name in archive.namelist() if name.endswith(".dist-info/RECORD"))
        rows = archive.read(record).decode().splitlines()
    digests = {}
    for row in rows:
        name, digest, _ = row.rsplit(",", 2)
        if digest:
            algorithm, value = digest.split("=", 1)
            assert algorithm == "sha256", row
            digests[name] = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).hex()
    return digests


def _native(path, harness):
    return [f"{directory}/{path.removeprefix('skills/')}" for directory in NATIVE_SKILLS[harness]]


def test_catalog_skill_missing_harness_destination(release):
    """Every skill the dispatcher advertises has a release destination in each harness's native skill directory."""
    _, wheel, _, _ = release
    advertised = _catalog(release)
    assert "skills/verify-ukrainian/SKILL.md" in advertised
    with zipfile.ZipFile(wheel) as archive:
        files = json.loads(archive.read("lu_machinery/agent_deploy/release.v1.json"))["files"]
    for path in advertised:
        assert path in files, path
        for harness in HARNESSES:
            for native in _native(path, harness):
                assert native in files[path]["targets"].get(harness, []), (path, harness, native)


def test_deployed_skill_stale_bytes(release):
    """Catalog bytes and every deployed copy of each advertised skill carry the wheel's recorded digest."""
    _, wheel, _, _ = release
    advertised = _catalog(release)
    consumer, _, deployed = _deploy_consumer(release, "parity-consumer", list(HARNESSES))
    assert deployed.returncode == 0, deployed.stdout + deployed.stderr
    digests = _wheel_digests(wheel)
    with zipfile.ZipFile(wheel) as archive:
        files = json.loads(archive.read("lu_machinery/agent_deploy/release.v1.json"))["files"]
    for path, opened in advertised.items():
        released = digests[SHARED + path]
        assert hashlib.sha256((REPO / SHARED / path).read_bytes()).hexdigest() == released, path
        assert hashlib.sha256(opened.read_bytes()).hexdigest() == released, path
        outputs = {native for harness in HARNESSES for native in _native(path, harness)}
        outputs.update(target for targets in files[path]["targets"].values() for target in targets)
        for target in sorted(outputs):
            assert hashlib.sha256((consumer / target).read_bytes()).hexdigest() == released, target


def _deploy_consumer(release, name, providers):
    """Deploy the installed wheel into a fresh Git consumer that has one product skill."""
    scratch, wheel, executable, env = release
    consumer = scratch / name
    consumer.mkdir()
    subprocess.run(["git", "init", "-q", str(consumer)], check=True)
    source = consumer / "agents_extensions/shared"
    skill = source / "skills/consumer/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: consumer\ndescription: Consumer example\n---\nRead this example.\n")
    with zipfile.ZipFile(wheel) as archive:
        inventory = json.loads(archive.read("lu_machinery/agent_deploy/release.v1.json"))
    descriptor = {"providers": providers, "sources": {
        "shared": {"path": "agents_extensions/shared", "required": True},
        "codex": {"path": "agents_extensions/codex", "required": True}}, "preserve": []}
    (consumer / "agents_extensions/deployment.v1.json").write_text(json.dumps(descriptor))
    # Configurations are input fixtures. Independent assertions below never use
    # the inventory to derive expected output paths, bytes, modes, or counts.
    for provider, configs in inventory["required_hooks"].items():
        for _destination, requirements in configs.items():
            data = {}
            for requirement in requirements:
                node = data
                for key in requirement["path"][:-1]:
                    node = node.setdefault(key, {})
                node.setdefault(requirement["path"][-1], []).append(
                    {**requirement["group"], "hooks": [
                        {**hook, "statusMessage": "Checking consumer session state"}
                        for hook in requirement["hooks"]]})
            path = source / "settings.json" if provider == "claude" else consumer / "agents_extensions/codex/hooks.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
    command = [str(executable), "-I", "-m", "lu_machinery.agent_deploy", "--project-root", str(consumer)]
    return consumer, command, _run(command, cwd=scratch, env=env)


def test_wheel_only_deployer_isolation_idempotency_and_false_green(release):
    """A real installed wheel deploys a non-curriculum consumer and detects tampering."""
    scratch, _, executable, env = release
    consumer, command, first = _deploy_consumer(release, "agent-consumer", ["claude", "codex", "agy"])
    assert first.returncode == 0, first.stdout + first.stderr
    assert (consumer / ".agents/skills/consumer/SKILL.md").read_text() == "---\nname: consumer\ndescription: Consumer example\n---\nRead this example.\n"
    assert not (consumer / ".claude/rules/core.md").exists()
    assert not (consumer / ".codex/skills").exists()
    codex_hooks = json.loads((consumer / ".codex/hooks.json").read_text())["hooks"]
    assert all(hook["statusMessage"] == "Checking consumer session state"
               for groups in codex_hooks.values() for group in groups for hook in group["hooks"])
    hook = consumer / ".claude/hooks/enforce-venv.sh"
    assert hook.stat().st_mode & 0o777 == 0o755
    assert (consumer / ".agent/rules/core-part-01.md").read_text().startswith("---\ntrigger: always_on\n")
    def snapshot():
        return {str(p.relative_to(consumer)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
                for p in consumer.rglob("*") if p.is_file() and ".git" not in p.parts}
    before = snapshot()
    second = _run(command, cwd=scratch, env=env)
    assert second.returncode == 0 and "0 changes" in second.stdout, second.stdout + second.stderr
    checked = _run([*command, "--check"], cwd=scratch, env=env)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert snapshot() == before
    # Edit both the output AND the recorded manifest. Check must reconstruct
    # the expected bytes from the installed release instead of trusting state.
    hook.write_text("#!/bin/sh\nexit 0\n")
    state = consumer / ".deploy-state/agent-deploy.v1.json"
    data = json.loads(state.read_text())
    data["files"][".claude/hooks/enforce-venv.sh"]["sha256"] = hashlib.sha256(hook.read_bytes()).hexdigest()
    state.write_text(json.dumps(data))
    tampered = snapshot()
    drift = _run([*command, "--check"], cwd=scratch, env=env)
    assert drift.returncode == 1, drift.stdout + drift.stderr
    assert snapshot() == tampered
    # No namespace/product copy can supply a missing installed resource.
    locate = _run([str(executable), "-I", "-c",
        "from importlib.resources import files; print(files('lu_machinery').joinpath('..','agents_extensions','shared','rules','core.md'))"], cwd=scratch, env=env)
    assert locate.returncode == 0, locate.stderr
    core = Path(locate.stdout.strip())
    blob = core.read_bytes()
    core.unlink()
    try:
        missing = _run(command, cwd=scratch, env=env)
        assert missing.returncode == 2, missing.stdout + missing.stderr
        assert snapshot() == tampered
    finally:
        core.write_bytes(blob)
