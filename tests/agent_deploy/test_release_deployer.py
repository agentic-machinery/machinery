"""C5 defect classes, with literal independent output expectations for a tiny release.

The fixture supplies input resources only. Expected output paths and contents below
are literals, never constructed by the deployer's inventory or renderer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery.agent_deploy import plan
from lu_machinery.agent_deploy.__main__ import main

CORE = b"# Fleet core\n"
SKILL = b"---\nname: fleet\ndescription: Fleet example\n---\nRun the fleet example.\n"
PRODUCT = "---\nname: product\ndescription: Product example\n---\nRun the product example.\n"
HOOK = b"#!/bin/sh\nprintf 'fleet hook works\\n'\n"
# Literal evaluator assertions, independent of the release input mappings.
EXPECTED = {
    ".claude/hooks/fleet.sh", ".claude/rules/fleet.md", ".claude/skills/fleet/SKILL.md",
    ".claude/skills/product/SKILL.md", ".claude/settings.json",
    ".codex/hooks/fleet.sh", ".codex/rules/core.md", ".codex/rules/fleet.md",
    ".agents/skills/fleet/SKILL.md", ".agents/skills/product/SKILL.md",
    ".agent/hooks/fleet.sh", ".agent/rules/core.md", ".agent/rules/fleet.md",
    ".agent/skills/fleet/SKILL.md", ".agent/skills/product/SKILL.md", ".agent/settings.json",
    ".agent/rules.json", ".agent/rules/core-part-01.md",
    ".codex/settings.json", ".codex/hooks.json",
    ".claude/memory/MEMORY.md", ".codex/memory/MEMORY.md", ".agent/memory/MEMORY.md",
}


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode & 0o777, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


def write(root, relative, content, mode=0o644):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path


def descriptor(root, **changes):
    data = {"providers": ["claude", "codex", "agy"], "sources": {
        "shared": {"path": "agents_extensions/shared", "required": True},
        "codex": {"path": "agents_extensions/codex", "required": True},
    }, "preserve": [".agent/tmp", ".claude/settings.local.json"]}
    data.update(changes)
    write(root, plan.DESCRIPTOR, json.dumps(data))
    return data


@pytest.fixture
def release(tmp_path, monkeypatch):
    install = tmp_path / "installed"
    fleet = install / "agents_extensions/shared"
    package = install / "lu_machinery"
    write(fleet, "rules/core.md", CORE)
    write(fleet, "rules/fleet.md", "# Fleet rule\n")
    write(fleet, "hooks/fleet.sh", HOOK, 0o755)
    write(fleet, "skills/fleet/SKILL.md", SKILL)
    # Input mappings are intentionally explicit, including omissions.
    inventory = {"version": 1, "provider_replacements": {"codex": ["config.toml", "hooks.json", "memory"]}, "claude_autoload_excludes": ["rules/core.md", "rules/non-negotiable-rules.md", "rules/core-curriculum.md"], "files": {
        "rules/core.md": {"mode": 420, "owner": "fleet", "targets": {
            "codex": [".codex/rules/core.md"], "agy": [".agent/rules/core.md"]}},
        "rules/fleet.md": {"mode": 420, "owner": "fleet", "targets": {
            "claude": [".claude/rules/fleet.md"], "codex": [".codex/rules/fleet.md"], "agy": [".agent/rules/fleet.md"]}},
        "hooks/fleet.sh": {"mode": 493, "owner": "fleet", "targets": {
            "claude": [".claude/hooks/fleet.sh"], "codex": [".codex/hooks/fleet.sh"], "agy": [".agent/hooks/fleet.sh"]}},
        "skills/fleet/SKILL.md": {"mode": 420, "owner": "fleet", "targets": {
            "claude": [".claude/skills/fleet/SKILL.md"], "codex": [".agents/skills/fleet/SKILL.md"],
            "agy": [".agent/skills/fleet/SKILL.md", ".agents/skills/fleet/SKILL.md"]}},
    }, "required_hooks": {"claude": {".claude/settings.json": [
        {"path": ["hooks", "SessionStart"], "group": {"matcher": "startup"}, "hooks": [
            {"type": "command", "command": ".claude/hooks/fleet.sh", "timeout": 10}]}]},
        "codex": {".codex/hooks.json": [
            {"path": ["hooks", "SessionStart"], "group": {}, "hooks": [
                {"type": "command", "command": ".codex/hooks/fleet.sh", "timeout": 10, "statusMessage": "Checking learn-ukrainian session state"}]}]}}}
    write(package, "agent_deploy/release.v1.json", json.dumps(inventory))
    locations = {"lu_machinery": package, "lu_machinery.agent_deploy": package / "agent_deploy", "agents_extensions.shared": fleet}
    monkeypatch.setattr(plan.resources, "files", lambda name: locations[name])
    return fleet


@pytest.fixture
def consumer(tmp_path, release):
    root = tmp_path / "consumer"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    descriptor(root)
    write(root, "agents_extensions/shared/skills/product/SKILL.md", PRODUCT)
    write(root, "agents_extensions/shared/memory/MEMORY.md", "shared memory\n")
    write(root, "agents_extensions/codex/memory/MEMORY.md", "codex memory\n")
    write(root, "agents_extensions/shared/settings.json", json.dumps({"hooks": {"SessionStart": [
        {"matcher": "startup", "hooks": [{"type": "command", "command": ".claude/hooks/fleet.sh", "timeout": 10}]}]}}))
    write(root, "agents_extensions/codex/hooks.json", json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": ".codex/hooks/fleet.sh", "timeout": 10, "statusMessage": "Checking consumer session state"}]}]}}))
    return root


def invoke(monkeypatch, root=None, check=False):
    args = ["agent_deploy"]
    if root is not None:
        args += ["--project-root", str(root)]
    if check:
        args += ["--check"]
    monkeypatch.setattr(sys, "argv", args)
    return main()


def test_wrong_project_selection_never_falls_back(consumer, tmp_path, monkeypatch):
    """An invalid explicit target must never deploy to LU_PRODUCT_ROOT or cwd."""
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    before = snapshot(consumer)
    monkeypatch.chdir(unrelated)
    assert invoke(monkeypatch, tmp_path / "absent") == 2
    assert snapshot(consumer) == before
    assert list(unrelated.iterdir()) == []
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, check=True) == 0
    monkeypatch.delenv("LU_PRODUCT_ROOT")
    assert invoke(monkeypatch) == 2


def test_workspace_target_deploys_outside_every_checkout_and_only_there(consumer, tmp_path, monkeypatch, capsys):
    """Defect class: the deployer writing provider trees into a directory nested in a checkout (whose
    ancestor project instructions Claude Code would load), or accepting a non-Git target without the
    explicit workspace mode. A workspace renders its own sources, agents included, and checks clean."""
    workspace = tmp_path / "cto-workspace"
    shutil.copytree(consumer / "agents_extensions", workspace / "agents_extensions")
    write(workspace, "agents_extensions/shared/agents/cto.md", "---\nname: cto\ndescription: CTO\n---\nDrive.\n")

    def run(*options):
        monkeypatch.setattr(sys, "argv", ["agent_deploy", *options])
        return main()

    assert run("--project-root", str(workspace)) == 2
    assert "Git checkout root" in capsys.readouterr().err
    assert not (workspace / ".claude").exists()
    # The session that would run there owns these files: its own policy inputs are refused.
    assert run("--workspace", "--project-root", str(workspace)) == 2
    assert "workspace input is writable by this session" in capsys.readouterr().err
    assert not (workspace / ".claude").exists()
    # The same tree as the operator's: the caller is another user from here on.
    real_uid = os.getuid()
    monkeypatch.setattr(plan.os, "getuid", lambda: real_uid + 1)
    for path in (workspace, *workspace.rglob("*")):  # the operator's modes, whatever this umask is
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o100 else 0o644)
    source = workspace / "agents_extensions/shared/skills/product/SKILL.md"
    for unsafe, mode in ((workspace / "agents_extensions/shared/skills", 0o775), (source, 0o646)):
        original = unsafe.stat().st_mode & 0o777
        unsafe.chmod(mode)
        assert run("--workspace", "--project-root", str(workspace)) == 2
        assert "writable by this session" in capsys.readouterr().err
        unsafe.chmod(original)
    assert run("--workspace", "--project-root", str(workspace)) == 0
    assert run("--workspace", "--project-root", str(workspace), "--check") == 0
    assert (workspace / ".claude/agents/cto.md").read_text() == "---\nname: cto\ndescription: CTO\n---\nDrive.\n"
    assert (workspace / ".claude/skills/fleet/SKILL.md").read_bytes() == SKILL
    assert not (workspace / ".git").exists()
    write(workspace, ".claude/settings.local.json", "{}")
    assert run("--workspace", "--project-root", str(workspace), "--check") == 2
    assert "settings.local.json" in capsys.readouterr().err
    (workspace / ".claude/settings.local.json").unlink()
    # A local settings layer planned from the workspace's own sources is refused before any write.
    planned = write(workspace, "agents_extensions/shared/settings.local.json", "{}")
    descriptor(workspace, preserve=[".agent/tmp"])
    before = snapshot(workspace)
    assert run("--workspace", "--project-root", str(workspace)) == 2
    assert "settings.local.json" in capsys.readouterr().err
    assert snapshot(workspace) == before and not (workspace / ".claude/settings.local.json").exists()
    planned.unlink()
    descriptor(workspace)
    capsys.readouterr()

    nested = consumer / "tools" / "cto-workspace"
    shutil.copytree(consumer / "agents_extensions", nested / "agents_extensions")
    for target in (consumer, nested):
        before = snapshot(consumer)
        assert run("--workspace", "--project-root", str(target)) == 2
        assert "outside every Git checkout" in capsys.readouterr().err
        assert snapshot(consumer) == before
    guardless = tmp_path / "guardless-workspace"
    shutil.copytree(consumer / "agents_extensions", guardless / "agents_extensions")
    descriptor(guardless, providers=["codex"], sources={"codex": {"path": "agents_extensions/codex", "required": True}},
               preserve=[])
    assert run("--workspace", "--project-root", str(guardless)) == 2
    assert "must enable the claude provider" in capsys.readouterr().err
    assert not (guardless / ".codex").exists()
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(workspace))
    with pytest.raises(SystemExit) as refused:
        run("--workspace")
    assert refused.value.code == 2
    with pytest.raises(SystemExit) as refused:
        run("--workspace", "--project-root", str(workspace), "--inputs-commit", "0" * 40)
    assert refused.value.code == 2


def test_non_curriculum_consumer_lints_its_own_sources(consumer, monkeypatch):
    """A consumer with no curriculum marker must lint and deploy its own skills."""
    path = consumer / "agents_extensions/shared/skills/product/SKILL.md"
    path.write_text("---\nname: wrong\ndescription: Example\n---\n")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before
    path.write_text(PRODUCT)
    assert invoke(monkeypatch, consumer) == 0
    from lu_machinery.lint.lint_agent_skills import lint_skills
    assert lint_skills(consumer) == []
    path.write_text("broken")
    assert lint_skills(consumer) != []


@pytest.mark.parametrize("defect", ["missing", "namespace"])
def test_missing_release_resource_cannot_fall_back(consumer, release, monkeypatch, defect):
    """Missing release resources or an alternate namespace fail before mutation."""
    if defect == "missing":
        (release / "rules/core.md").unlink()
    else:
        (release / "rules/core.md").unlink()
        write(consumer, "rules/core.md", CORE)
        original = plan.resources.files
        monkeypatch.setattr(plan.resources, "files", lambda name: consumer if name == "agents_extensions.shared" else original(name))
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("defect", ["logical", "skill", "delegator", "disabled", "preserve-policy", "prefix", "skill-name"])
def test_silent_policy_override_refused_before_writes(consumer, monkeypatch, capsys, defect):
    """Duplicate names/paths or absent/disabled mandatory hooks cannot override fleet policy."""
    if defect == "logical":
        write(consumer, "agents_extensions/shared/rules/core.md", CORE)
    elif defect == "skill-name":
        write(consumer, "agents_extensions/shared/skills/different/SKILL.md", SKILL)
    elif defect == "prefix":
        write(consumer, "agents_extensions/shared/rules", "file instead of directory")
    elif defect == "skill":
        write(consumer, "agents_extensions/shared/skills/fleet/SKILL.md", SKILL)
    elif defect in {"delegator", "disabled"}:
        config = consumer / "agents_extensions/shared/settings.json"
        data = json.loads(config.read_text())
        if defect == "delegator":
            data["hooks"]["SessionStart"][0]["hooks"] = []
        else:
            data["hooks"]["SessionStart"][0]["disabled"] = True
        config.write_text(json.dumps(data))
    else:
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        data["preserve"].append(".claude/hooks")
        descriptor(consumer, **data)
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert snapshot(consumer) == before
    if defect == "skill-name":
        assert "duplicate skill name: fleet" in capsys.readouterr().err


def test_unsafe_retirement_preserves_modified_unknown_and_runtime(consumer, monkeypatch):
    """Only recorded, unchanged outputs may retire; unknown files and scratch survive."""
    write(consumer, ".agent/tmp/scratch", "keep")
    assert invoke(monkeypatch, consumer) == 0
    source = consumer / "agents_extensions/shared/skills/product/SKILL.md"
    source.unlink()
    source.parent.rmdir()
    victim = consumer / ".agent/skills/product/SKILL.md"
    victim.write_text("local change")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 1
    assert snapshot(consumer) == before
    victim.write_text(PRODUCT)
    unknown = write(consumer, ".claude/skills/unknown", "keep")
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 1
    assert snapshot(consumer) == before
    unknown.unlink()
    assert invoke(monkeypatch, consumer) == 0
    assert not victim.exists()
    assert not victim.parent.exists()
    assert (consumer / ".agent/tmp/scratch").read_text() == "keep"


@pytest.mark.parametrize("root", [".claude", ".agent", ".agents", ".codex", ".cursor"])
@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling", "fifo"])
@pytest.mark.parametrize("name", ["sessions", "cache\\key", "session\u200bid"])
def test_runtime_siblings_do_not_block_check_or_apply(consumer, tmp_path, monkeypatch, root, kind, name):
    """Runtime state outside owned top-levels must never block deployment."""
    if root == ".cursor":
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        descriptor(consumer, providers=data["providers"] + ["cursor"])
    outside = write(tmp_path, "outside-runtime", "keep outside\n")
    runtime = consumer / root / name
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        write(consumer, root + "/" + name + "/active", "keep runtime\n")
    elif kind == "file":
        runtime.write_text("keep runtime\n")
    elif kind == "symlink":
        runtime.symlink_to(outside)
    elif kind == "dangling":
        runtime.symlink_to(tmp_path / "absent")
    else:
        os.mkfifo(runtime)
    before = runtime.lstat()
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert invoke(monkeypatch, consumer) == 0
    assert runtime.lstat() == before
    assert outside.read_text() == "keep outside\n"


@pytest.mark.parametrize("path,providers", [(path, None) for path in [
    ".claude/commands/planted.md", ".claude/agents/planted.md",
    ".claude/output-styles/planted.md", ".claude/plugins/planted.md",
    ".claude/skills/old/SKILL.md", ".codex/agents/planted.toml",
    ".codex/config.toml", ".agents/plugins/planted/plugin.json",
    ".agents/rules/planted.md", ".agent/workflows/planted.md",
    ".claude/workflows/planted.js",
    ".claude/agent-memory/reviewer/MEMORY.md", ".claude/agent-memory-local/reviewer/MEMORY.md",
    ".claude/AGENTS.md",
    ".cursor/cli.json", ".cursor/environment.json", ".cursor/worktrees.json",
    ".claude/Skills/old/SKILL.md", ".cursor/BUGBOT.md",
    ".cursor/sandbox.json", ".cursor/permissions.json", ".cursor/config/bugbot.yaml",
]] + [
    # Every provider that discovers the shared .agents tree inventories it on its own,
    # from a fresh apply too (no earlier deployment state names .agents).
    # Claude alone never discovers it: the negative control.
    (path, providers) for providers in (["codex"], ["agy"], ["cursor"], ["claude"])
    for path in (".agents/skills/planted/SKILL.md", ".agents/rules/planted.md")
])
@pytest.mark.parametrize("check", [False, True], ids=["apply", "check"])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_provider_discovery_content_cannot_hide_as_runtime(consumer, tmp_path, monkeypatch, capsys, path, providers, check, kind):
    """Never-planned discovery content and stale skills must still report drift."""
    if providers is not None:
        descriptor(consumer, providers=providers, sources={
            "shared": {"path": "agents_extensions/shared", "required": True},
            **({"codex": {"path": "agents_extensions/codex", "required": True}} if "codex" in providers else {}),
        }, preserve=[])
    elif path.startswith(".cursor/"):
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        descriptor(consumer, providers=data["providers"] + ["cursor"])
    if providers is None:
        assert invoke(monkeypatch, consumer) == 0
    if kind == "file":
        write(consumer, path, 'model = "planted"\n' if path.endswith(".toml") else "planted\n")
    else:
        outside = write(tmp_path, "outside-discovery", "untouched\n")
        planted = consumer / path
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.symlink_to(outside)
    before = snapshot(consumer)
    if providers == ["claude"]:
        # Content Claude does not discover is not its deployment: no drift, left untouched.
        assert invoke(monkeypatch, consumer) == 0
        assert invoke(monkeypatch, consumer, check=check) == 0
        assert (consumer / path).is_symlink() == (kind == "symlink")
        assert {name: before[name] for name in before if name.startswith(".agents/")} == {
            name: value for name, value in snapshot(consumer).items() if name.startswith(".agents/")
        }
        return
    assert invoke(monkeypatch, consumer, check=check) == (1 if kind == "file" else 2)
    captured = capsys.readouterr()
    if kind == "file":
        assert "unknown unowned output: " + path in captured.out + captured.err
    else:
        if path == ".codex/config.toml":
            assert "non-regular deployment output or parent: " + path in captured.err
        else:
            assert "symlink or special file" in captured.err
        assert planted.is_symlink()
        assert outside.read_text() == "untouched\n"
    assert snapshot(consumer) == before


@pytest.mark.parametrize("root,names", [
    (".claude", "agents agent-memory agent-memory-local commands output-styles plugins skills rules hooks workflows settings.json settings.local.json CLAUDE.md AGENTS.md"),
    (".codex", "agents prompts plugins skills rules hooks config.toml hooks.json"),
    (".agent", "agents plugins skills rules workflows hooks hooks.json mcp_config.json settings.json rules.json skills.json agents.json plugins.json AGENTS.md GEMINI.md"),
    (".agents", "agents plugins skills rules workflows hooks hooks.json mcp_config.json rules.json skills.json agents.json plugins.json AGENTS.md GEMINI.md"),
    (".cursor", "agents commands skills rules hooks hooks.json mcp.json cli.json environment.json worktrees.json BUGBOT.md sandbox.json permissions.json config"),
])
@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_every_discovery_name_refuses_unowned_content(tmp_path, root, names, kind):
    """An omitted discovery entry must fail even without planned or recorded siblings."""
    assert plan.DISCOVERY[root] == set(names.split())
    outside = write(tmp_path, "outside-discovery", "untouched\n")
    empty_plan = plan.Plan(files={}, preserve=[], roots={root}, release="release", configuration="config")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        tree = plan.Tree(fd)
        for name in names.split():
            path = tmp_path / root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == "file":
                path.write_text("planted\n")
                if (root, name) == (".codex", "skills"):
                    with pytest.raises(plan.Invalid, match="non-directory deployment path: .codex/skills"):
                        plan.preflight(tree, empty_plan, {"files": {}}, False)
                else:
                    assert plan.preflight(tree, empty_plan, {"files": {}}, False) == [
                        "unknown unowned output: " + root + "/" + name]
            else:
                path.symlink_to(outside)
                with pytest.raises(plan.Invalid, match="symlink or special file"):
                    plan.preflight(tree, empty_plan, {"files": {}}, False)
            path.unlink()
        assert outside.read_text() == "untouched\n"
    finally:
        os.close(fd)


@pytest.mark.parametrize("name", ["cache\\key", "session\u200bid"])
def test_unsafe_names_inside_owned_discovery_still_fail_closed(tmp_path, name):
    """Ignoring runtime siblings must not bypass validation inside discovery trees."""
    write(tmp_path, ".claude/rules/" + name, "planted\n")
    empty_plan = plan.Plan(files={}, preserve=[], roots={".claude"}, release="release", configuration="config")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(plan.Invalid):
            plan.preflight(plan.Tree(fd), empty_plan, {"files": {}}, False)
    finally:
        os.close(fd)


@pytest.mark.parametrize("check", [False, True], ids=["apply", "check"])
@pytest.mark.parametrize("kind", ["nested-file", "parent-file", "leaf-directory", "symlink", "dangling", "fifo"])
def test_future_planned_output_refuses_runtime_collision(consumer, tmp_path, monkeypatch, capsys, check, kind):
    """New ownership refuses differing bytes and cross-type collisions without crashing."""
    assert invoke(monkeypatch, consumer) == 0
    target = consumer / ".claude/sessions/active"
    if kind == "parent-file":
        write(consumer, ".claude/sessions", "runtime bytes\n")
    else:
        target.parent.mkdir(parents=True)
        if kind == "nested-file":
            target.write_text("runtime bytes\n")
        elif kind == "leaf-directory":
            target.mkdir()
        elif kind in {"symlink", "dangling"}:
            outside = tmp_path / "outside-collision"
            if kind == "symlink":
                outside.write_text("untouched\n")
            target.symlink_to(outside)
        else:
            os.mkfifo(target)
    write(consumer, "agents_extensions/shared/sessions/active", "planned bytes\n")
    before = snapshot(consumer)
    with plan.selected_tree(str(consumer), check=check) as tree:
        with pytest.raises(plan.Invalid):
            plan.deploy(tree, check=check)
    assert invoke(monkeypatch, consumer, check=check) == 2
    error = capsys.readouterr().err
    if kind == "nested-file":
        assert "differing unowned output: .claude/sessions/active" in error
    elif kind == "parent-file":
        assert "non-directory deployment path: .claude/sessions" in error
    elif kind == "leaf-directory":
        assert "non-regular deployment output or parent: .claude/sessions/active" in error
    else:
        assert "symlink or special file" in error
    assert snapshot(consumer) == before


@pytest.mark.parametrize("path", [".agent", ".deploy-state", "agents_extensions/shared", ".claude/hooks"])
def test_symlink_escape_never_mutates_external_target(consumer, tmp_path, monkeypatch, path):
    """Source, output and state symlinks cannot redirect either apply or check."""
    import shutil
    external = tmp_path / "outside"
    external.mkdir()
    write(external, "sentinel", "untouched")
    target = consumer / path
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external, target_is_directory=True)
    before = snapshot(external)
    assert invoke(monkeypatch, consumer) == 2
    assert invoke(monkeypatch, consumer, check=True) == 2
    assert snapshot(external) == before


@pytest.mark.parametrize("defect", ["bytes", "mode", "hook", "agy", "codex"])
def test_false_green_check_recomposes_without_mutation(consumer, monkeypatch, defect):
    """Bytes, modes, hook references, generated parts and discovery paths all fail check."""
    assert invoke(monkeypatch, consumer) == 0
    if defect == "bytes":
        (consumer / ".claude/rules/fleet.md").write_text("wrong bytes")
    elif defect == "mode":
        (consumer / ".claude/hooks/fleet.sh").chmod(0o644)
    elif defect == "hook":
        (consumer / ".codex/hooks.json").write_text('{"hooks":{}}')
    elif defect == "agy":
        (consumer / ".agent/rules/core-part-01.md").write_text("wrong part")
    else:
        (consumer / ".codex/skills").mkdir()
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer, check=True) == 1
    assert snapshot(consumer) == before


def test_non_idempotency_literal_inventory_and_modes(consumer, monkeypatch):
    """Fresh apply and repeat apply must yield literal paths/bytes/modes and no rewrites."""
    assert invoke(monkeypatch, consumer) == 0
    paths = {p for p in snapshot(consumer) if p.startswith((".claude/", ".codex/", ".agents/", ".agent/", ".gemini/"))}
    assert paths == EXPECTED
    assert (consumer / ".codex/memory/MEMORY.md").read_text() == "codex memory\n"
    assert (consumer / ".claude/memory/MEMORY.md").read_text() == "shared memory\n"
    assert (consumer / ".agent/rules/core-part-01.md").read_bytes() == b"---\ntrigger: always_on\ndescription: Binding rules core, part 1 of 1.\n---\n# Fleet core\n"
    assert (consumer / ".agent/rules.json").read_text() == '{\n  "exclude": [\n    "core.md",\n    "model-assignment.md",\n    "workflow.md"\n  ]\n}\n'
    assert (consumer / ".claude/hooks/fleet.sh").stat().st_mode & 0o777 == 0o755
    assert (consumer / ".claude/rules/fleet.md").stat().st_mode & 0o777 == 0o644
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert snapshot(consumer) == before
    assert subprocess.check_output([str(consumer / ".claude/hooks/fleet.sh")], text=True) == "fleet hook works\n"
    assert (consumer / ".agents/skills/product/SKILL.md").read_text() == PRODUCT


@pytest.mark.parametrize("defect", ["interruption", "late-discovery", "partial_crash_then_source_change"])
def test_interrupted_apply_cannot_publish_false_success_and_recovers(consumer, monkeypatch, capsys, defect):
    """Interrupted outputs recover after source edits using the documented input restoration."""
    original = plan.Tree.replace
    calls = []
    def interrupted(self, path, blob, mode):
        calls.append(path)
        if defect == "partial_crash_then_source_change" and path == ".agent/skills/product/SKILL.md":
            original(self, path, blob, mode)
            raise OSError("injected interruption after product output")
        if len(calls) == 3 and defect != "partial_crash_then_source_change":
            if defect == "interruption":
                raise OSError("injected interruption")
            (consumer / ".codex/skills").mkdir(parents=True)
        return original(self, path, blob, mode)
    monkeypatch.setattr(plan.Tree, "replace", interrupted)
    assert invoke(monkeypatch, consumer) != 0
    assert not (consumer / plan.STATE).exists()
    assert invoke(monkeypatch, consumer, check=True) != 0
    source = consumer / "agents_extensions/shared/skills/product/SKILL.md"
    desired = PRODUCT.replace("Run the product example.", "Run the updated product example.")
    if defect == "partial_crash_then_source_change":
        source.write_text(desired)
        before = snapshot(consumer)
        assert invoke(monkeypatch, consumer) == 2
        assert "see --help section: Recovery after an interrupted fresh apply" in capsys.readouterr().err
        assert snapshot(consumer) == before
        monkeypatch.setattr(sys, "argv", ["agent_deploy", "--help"])
        with pytest.raises(SystemExit) as exited:
            main()
        assert exited.value.code == 0
        help_text = capsys.readouterr().out
        assert "Restore the product sources and installed" in help_text
        assert "apply again to establish ownership" in help_text
        assert "Do not delete provider directories or the ownership manifest" in help_text
        # Follow the documented recovery, without deleting any output or state.
        source.write_text(PRODUCT)
    monkeypatch.setattr(plan.Tree, "replace", original)
    if defect == "late-discovery":
        (consumer / ".codex/skills").rmdir()
    assert invoke(monkeypatch, consumer) == 0
    if defect == "partial_crash_then_source_change":
        source.write_text(desired)
        assert invoke(monkeypatch, consumer) == 0
        assert (consumer / ".agents/skills/product/SKILL.md").read_text() == desired
    assert invoke(monkeypatch, consumer, check=True) == 0


def test_concurrent_deployer_cannot_interleave(consumer, monkeypatch):
    """A second process holding the target inode lock must block deployment without writes."""
    with plan.selected_tree(str(consumer), False):
        before = snapshot(consumer)
        assert invoke(monkeypatch, consumer) == 1
        assert snapshot(consumer) == before


@pytest.mark.parametrize("selected", ["relative", "../escape", "bad\npath"])
def test_wrong_project_selection_invalid_input_has_no_side_effects(consumer, monkeypatch, selected):
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(consumer))
    before = snapshot(consumer)
    assert invoke(monkeypatch, selected) == 2
    assert snapshot(consumer) == before


def test_sigkill_after_first_write_recovers_without_deleting_unknown_files(consumer, release, monkeypatch):
    """SIGKILL bypasses finally cleanup; the next apply must recover its temporary file."""
    script = """
import os, signal, sys
from pathlib import Path
from lu_machinery.agent_deploy import plan
from lu_machinery.agent_deploy.__main__ import main
install = Path(sys.argv[1])
locations = {"lu_machinery": install / "lu_machinery",
             "lu_machinery.agent_deploy": install / "lu_machinery/agent_deploy",
             "agents_extensions.shared": install / "agents_extensions/shared"}
plan.resources.files = lambda name: locations[name]
def kill_after_write(fd, mode):
    os.kill(os.getpid(), signal.SIGKILL)
plan.os.fchmod = kill_after_write
sys.argv = ["agent_deploy", "--project-root", sys.argv[2]]
raise SystemExit(main())
"""
    killed = subprocess.run([sys.executable, "-c", script, str(release.parents[1]), str(consumer)],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert not (consumer / plan.STATE).exists()
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer, check=True) == 1
    assert snapshot(consumer) == before
    assert invoke(monkeypatch, consumer) == 0
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert not any(p.name.startswith(".agent-deploy-") for p in consumer.rglob("*"))
    assert not (consumer / ".deploy-state/agent-deploy.tmp").exists()


def test_unknown_staging_content_is_preserved_before_first_apply(consumer, monkeypatch):
    """A reserved staging filename never licenses deleting unrelated unowned bytes."""
    write(consumer, ".deploy-state/agent-deploy.tmp", "unrelated user content", 0o600)
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert invoke(monkeypatch, consumer, check=True) == 2
    assert snapshot(consumer) == before


@pytest.mark.parametrize("defect", ["global", "never_matcher", "local_source", "local_preserved", "codex_feature", "codex_alias"])
def test_mandatory_hooks_cannot_be_globally_disabled_or_made_unreachable(consumer, monkeypatch, defect):
    """F1: actual provider disable switches and nonmatching groups cannot neutralize fleet hooks."""
    if defect == "never_matcher":
        path = consumer / "agents_extensions/codex/hooks.json"
        data = json.loads(path.read_text())
        data["hooks"]["SessionStart"][0]["matcher"] = "no-such-source-ever"
        path.write_text(json.dumps(data))
    elif defect in {"codex_feature", "codex_alias"}:
        key = "hooks" if defect == "codex_feature" else "codex_hooks"
        write(consumer, "agents_extensions/codex/config.toml", f"[features]\n{key} = false\n")
    elif defect == "global":
        path = consumer / "agents_extensions/shared/settings.json"
        data = json.loads(path.read_text())
        data["disableAllHooks"] = True
        path.write_text(json.dumps(data))
    elif defect == "local_source":
        data = json.loads((consumer / plan.DESCRIPTOR).read_text())
        data["preserve"] = [".agent/tmp"]
        descriptor(consumer, **data)
        write(consumer, "agents_extensions/shared/settings.local.json", '{"disableAllHooks": true}')
    else:
        write(consumer, ".claude/settings.local.json", '{"disableAllHooks": true}')
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == 2
    assert invoke(monkeypatch, consumer, check=True) == 2
    assert snapshot(consumer) == before


def test_bytecode_caches_are_neither_drift_nor_deployed_but_a_loose_pyc_is(consumer, monkeypatch):
    """Defect class: importing a deployed script wrote ``__pycache__`` beside it, and the next
    ``--check`` and deploy refused it as unknown output, so the launcher refused to start."""
    assert invoke(monkeypatch, consumer) == 0
    write(consumer, ".claude/skills/product/__pycache__/helper.cpython-312.pyc", b"\x00bytecode")
    write(consumer, ".agents/skills/fleet/__pycache__/helper.cpython-312.pyc", b"\x00bytecode")
    write(consumer, "agents_extensions/shared/skills/product/__pycache__/source.cpython-312.pyc", b"\x00bytecode")
    assert invoke(monkeypatch, consumer, check=True) == 0
    assert invoke(monkeypatch, consumer) == 0
    assert not (consumer / ".claude/skills/product/__pycache__/source.cpython-312.pyc").exists()
    # Outside a cache directory a .pyc imports without its source, so it is still unknown output.
    write(consumer, ".claude/skills/product/helper.pyc", b"\x00bytecode")
    assert invoke(monkeypatch, consumer, check=True) == 1


@pytest.mark.parametrize("kind", ["skill", "file"])
def test_codex_provider_never_plans_retired_skill_discovery(consumer, monkeypatch, kind):
    """F2: provider skills use .agents; an invalid skills leaf refuses before any write."""
    body = "---\nname: codexonly\ndescription: Codex example\n---\nUse this example.\n"
    if kind == "skill":
        write(consumer, "agents_extensions/codex/skills/codexonly/SKILL.md", body)
        assert invoke(monkeypatch, consumer) == 0
        assert (consumer / ".agents/skills/codexonly/SKILL.md").read_text() == body
        assert not (consumer / ".codex/skills").exists()
        assert invoke(monkeypatch, consumer, check=True) == 0
    else:
        write(consumer, "agents_extensions/codex/skills", "not a skill directory")
        before = snapshot(consumer)
        assert invoke(monkeypatch, consumer) == 2
        assert snapshot(consumer) == before


@pytest.mark.parametrize("kind", ["symlink", "fifo", "unreadable"])
def test_preserved_runtime_content_is_not_opened_or_traversed(consumer, tmp_path, monkeypatch, kind):
    """F3: sockets, links and unreadable files in excluded runtime paths cannot block deploy."""
    runtime = consumer / ".agent/tmp"
    runtime.mkdir(parents=True)
    path = runtime / "active"
    outside = write(tmp_path, "outside-runtime", "external runtime\n")
    outside_before = (outside.read_bytes(), outside.stat().st_mtime_ns)
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text("private runtime\n")
        path.chmod(0)
    mode_before = path.lstat().st_mode
    try:
        assert invoke(monkeypatch, consumer) == 0
        assert invoke(monkeypatch, consumer, check=True) == 0
        assert path.lstat().st_mode == mode_before
        assert (outside.read_bytes(), outside.stat().st_mtime_ns) == outside_before
    finally:
        if kind == "unreadable":
            path.chmod(0o600)
    if kind == "unreadable":
        assert path.read_text() == "private runtime\n"


def test_packaged_resource_edits_load_without_stale_pins_and_change_identity(tmp_path, monkeypatch):
    """Every listed resource can change without stale pins or an unchanged release identity."""
    repo = Path(__file__).resolve().parents[2]
    installed = tmp_path / "installed"
    package = installed / "lu_machinery"
    package.mkdir(parents=True)
    shutil.copytree(Path(plan.__file__).resolve().parent, package / "agent_deploy")
    fleet = installed / "agents_extensions/shared"
    shutil.copytree(repo / "agents_extensions/shared", fleet)
    locations = {"lu_machinery": package, "lu_machinery.agent_deploy": package / "agent_deploy"}
    monkeypatch.setattr(plan.resources, "files", lambda name: locations[name])
    original, inventory, identity = plan.release_inputs()
    assert plan.release_inputs()[2] == identity
    for name in inventory["files"]:
        path = fleet / name
        before = path.read_bytes()
        edited = before + b"\n# scratch release edit\n"
        path.write_bytes(edited)
        try:
            outputs, _, changed = plan.release_inputs()
            assert outputs[name].blob == edited
            assert outputs[name].mode == original[name].mode
            assert changed != identity, name
            assert plan.release_inputs()[2] == changed
        finally:
            path.write_bytes(before)
    assert plan.release_inputs()[2] == identity
    assert all("sha256" not in record for record in inventory["files"].values())
    # Modes and routing metadata remain release identity inputs as well.
    inventory_path = package / "agent_deploy/release.v1.json"
    inventory["files"]["rules/core.md"]["mode"] = 493
    inventory_path.write_bytes(plan.encoded(inventory))
    assert plan.release_inputs()[2] != identity
    inventory["files"]["rules/core.md"]["mode"] = original["rules/core.md"].mode
    inventory_path.write_bytes(plan.encoded(inventory))
    assert plan.release_inputs()[2] == identity
    inventory["provider_replacements"]["codex"].append("scratch-metadata")
    inventory_path.write_bytes(plan.encoded(inventory))
    assert plan.release_inputs()[2] != identity


@pytest.mark.parametrize("memory_overlay", [False, True])
def test_declared_but_missing_overlay_does_not_drop_shared_memory(consumer, monkeypatch, memory_overlay):
    """A declared but absent provider overlay must not silently remove shared memory."""
    if not memory_overlay:
        (consumer / "agents_extensions/codex/memory/MEMORY.md").unlink()
    assert invoke(monkeypatch, consumer) == 0
    expected = "codex memory\n" if memory_overlay else "shared memory\n"
    assert (consumer / ".codex/memory/MEMORY.md").read_text() == expected
    assert invoke(monkeypatch, consumer, check=True) == 0


@pytest.mark.parametrize(("path", "text", "expected"), [
    ("phases/content.md", "persona\n", 0),
    ("phases/research.md", "persona\n", 2),
    ("phases/content.md", "Helpful Neighbor\n", 2),
])
def test_scoped_prompt_lint_does_not_reject_unrelated_content(consumer, monkeypatch, path, text, expected):
    """A research-only prompt rule must respect its file glob without disabling global rules."""
    write(consumer, "curriculum/l2-uk-en/curriculum.yaml", "tracks: {}\n")
    write(consumer, "agents_extensions/shared/" + path, text)
    before = snapshot(consumer)
    assert invoke(monkeypatch, consumer) == expected
    if expected:
        assert snapshot(consumer) == before
    else:
        assert (consumer / (".claude/" + path)).read_text() == text
        assert invoke(monkeypatch, consumer, check=True) == 0


# Defect class of every test below: installed-deploy output verification in reject_untracked_inputs
# (machinery#22). A consumer deploys the real packaged release through the deploy CLI, and the pre-push
# gate runs the deploy's check in this interpreter, as a consumer's pre-push hook runs it.
GATE_PRODUCT_HOOK = b"#!/usr/bin/env python3\nprint('product hook')\n"


def load_gate(monkeypatch):
    checkout = next(p for p in Path(__file__).resolve().parents if (p / ".githooks/pre_push_gate.py").is_file())
    spec = importlib.util.spec_from_file_location("deploy_output_pre_push_gate", checkout / ".githooks/pre_push_gate.py")
    gate = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, gate)
    spec.loader.exec_module(gate)
    return gate


def git_environment():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def commit_all(root, message):
    for command in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null",
                                    "commit", "-q", "-m", message]):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True, env=git_environment())


def deploy_cli(root, *options):
    result = subprocess.run([sys.executable, "-I", "-m", "lu_machinery.agent_deploy", "--project-root", str(root),
                             *options], capture_output=True, text=True, env=git_environment(), check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


def release_inventory():
    return plan.decode(plan.resources.files("lu_machinery.agent_deploy").joinpath("release.v1.json").read_bytes())


def test_release_requires_each_claude_python_guard_as_its_native_command():
    """Defect class: a mandatory Claude Python delegator run by the interpreter PATH selects,
    or release bytes drifting from the installed native hook command they must equal."""
    from lu_machinery.agent_runtime.agy_hook_config import CLAUDE_GUARD_MODES, native_hook_command

    commands = [hook["command"] for requirement in release_inventory()["required_hooks"]["claude"][".claude/settings.json"]
                for hook in requirement["hooks"]]
    assert [command for command in commands if command.endswith(".py")] == []
    natives = {native_hook_command(mode) for mode in (*CLAUDE_GUARD_MODES, "claude-audit-hook")}
    assert {command for command in commands if command.startswith("/bin/bash ")} == natives


def test_claude_hook_commands_run_from_a_project_path_with_a_space(tmp_path):
    """Defect class: a mandatory Claude hook command that leaves $CLAUDE_PROJECT_DIR unquoted is
    split at a space in the project path, so the shell runs a truncated path and exits 127 (#722)."""
    project = tmp_path / "project with space"
    hooks = project / ".claude/hooks"
    hooks.mkdir(parents=True)
    ran = tmp_path / "ran"
    prefix = "$CLAUDE_PROJECT_DIR/.claude/hooks/"
    commands = [hook["command"] for requirement in release_inventory()["required_hooks"]["claude"][".claude/settings.json"]
                for hook in requirement["hooks"] if hook["command"].replace('"', "").startswith(prefix)]
    assert commands
    expected = set()
    for command in commands:
        stub = hooks / command.replace('"', "").removeprefix(prefix)
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$RAN"\n')
        stub.chmod(0o755)
        expected.add(str(stub))
        result = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL,
                                env={"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(project), "RAN": str(ran)})
        assert result.returncode == 0, (command, result.returncode, result.stderr)
    assert set(ran.read_text().splitlines()) == expected


@pytest.fixture
def deployed(tmp_path):
    root = tmp_path / "gate-consumer"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=git_environment())
    settings: dict = {}
    for requirement in release_inventory()["required_hooks"]["claude"][".claude/settings.json"]:
        *parents, event = requirement["path"]
        node = settings
        for name in parents:
            node = node.setdefault(name, {})
        node.setdefault(event, []).append({**requirement["group"], "hooks": requirement["hooks"]})
    write(root, "agents_extensions/claude/settings.json", json.dumps(settings))
    write(root, "agents_extensions/claude/hooks/product.py", GATE_PRODUCT_HOOK, 0o755)
    write(root, plan.DESCRIPTOR, json.dumps({"providers": ["claude"], "preserve": [],
                                             "sources": {"claude": {"path": "agents_extensions/claude", "required": True}}}))
    write(root, ".gitignore", "/.claude/\n/.agents/\n/.deploy-state/\n")
    commit_all(root, "consumer")
    deploy_cli(root)
    return root


def gate_verdict(gate, root):
    try:
        return gate.reject_untracked_inputs(root), None
    except gate.GateOutcome as error:
        return None, error


def forge_manifest_listing(root, extra):
    """The checkout's manifest, rewritten to own ``extra`` as a product output with its current bytes."""
    manifest = json.loads((root / plan.STATE).read_bytes())
    manifest["files"][extra] = {"type": "file", "sha256": hashlib.sha256((root / extra).read_bytes()).hexdigest(),
                                "mode": 0o644, "owner": "product", "source": "agents_extensions/claude/extra.py"}
    return plan.encoded(manifest)


def test_installed_deploy_outputs_verified_by_check_are_not_untracked_inputs(deployed, monkeypatch):
    """The #22 refusal: product-owned hook copies the session runs from block every push.

    The release itself deploys no Python: its modules run from the installed release, never a copy
    importable from the consumer tree."""
    gate = load_gate(monkeypatch)
    listed = subprocess.run(["git", "-C", str(deployed), "ls-files", "--others", "--", "*.py"], capture_output=True,
                            text=True, check=True, env=git_environment()).stdout.split()
    assert set(listed) == {".claude/hooks/product.py"}
    assert [name for name, record in release_inventory()["files"].items()
            if name.endswith(".py") and record["targets"]] == []
    manifest, error = gate_verdict(gate, deployed)
    assert error is None
    assert manifest == hashlib.sha256((deployed / plan.STATE).read_bytes()).hexdigest()


@pytest.mark.parametrize("change", ["product-copy", "source-committed-after-deploy",
                                    "mode-changed-after-deploy", "source-edited-during-check"])
def test_deployed_output_edited_after_deploy_is_refused(deployed, monkeypatch, change):
    """A stale or edited copy, or one deployed from an uncommitted source, is not a verified output."""
    gate = load_gate(monkeypatch)
    assert gate_verdict(gate, deployed)[1] is None
    changed, cause = ".claude/hooks/product.py", "output drift: .claude/hooks/product.py"
    source = deployed / "agents_extensions/claude/hooks/product.py"
    if change == "source-committed-after-deploy":
        source.write_bytes(GATE_PRODUCT_HOOK + b"print('changed')\n")
        commit_all(deployed, "source change")
    elif change == "mode-changed-after-deploy":
        (deployed / changed).chmod(0o644)
    elif change == "source-edited-during-check":
        # The source holds other bytes while the check runs: the copy matches that edit, not the commit.
        source.write_bytes(GATE_PRODUCT_HOOK + b"print('uncommitted')\n")
        deploy_cli(deployed)
    else:
        with (deployed / changed).open("ab") as handle:
            handle.write(b"print('edited')\n")
    _, error = gate_verdict(gate, deployed)
    assert error is not None and error.reason == "untracked_inputs"
    assert cause in error.detail


@pytest.mark.parametrize("extra", [".claude/hooks/extra.py", ".claude/extra.py", ".agents/extra.py"])
def test_unplanned_file_in_provider_directory_is_refused(deployed, monkeypatch, extra):
    """A file the plan does not own is refused, whether or not the deploy's check inventories its name."""
    gate = load_gate(monkeypatch)
    assert gate_verdict(gate, deployed)[1] is None
    write(deployed, extra, "value = 1\n")
    _, error = gate_verdict(gate, deployed)
    assert error is not None and error.reason == "untracked_inputs"
    assert extra in error.detail


@pytest.mark.parametrize(("failure", "cause"), [
    ("no-manifest", "the deploy check exited 1"),
    ("check-cannot-run", "the deploy check exited 3"),
    ("forged-manifest-on-disk", "the deploy check exited 1"),
    ("manifest-swapped-back-during-check", ".claude/extra.py"),
    ("descriptor-redirected-during-check", ".claude/extra.py"),
    ("symlinked-copy", "the deploy check exited 2"),
])
def test_failed_deploy_check_keeps_provider_files_refused(deployed, monkeypatch, tmp_path, failure, cause):
    """Only the check's own recomposed manifest can exempt a file; the checkout's manifest never does."""
    gate = load_gate(monkeypatch)
    assert gate_verdict(gate, deployed)[1] is None
    state = deployed / plan.STATE
    if failure == "no-manifest":
        state.unlink()
    elif failure == "check-cannot-run":
        monkeypatch.setattr(gate.sys, "executable", str(write(tmp_path, "failing.sh", "#!/bin/sh\nexit 3\n", 0o755)))
    elif failure in ("forged-manifest-on-disk", "manifest-swapped-back-during-check"):
        write(deployed, ".claude/extra.py", "value = 1\n")
        forged = write(tmp_path, "forged.json", forge_manifest_listing(deployed, ".claude/extra.py"))
        if failure == "forged-manifest-on-disk":
            shutil.copyfile(forged, state)
        else:
            # The genuine manifest is in place only while the check runs; the forged one before and after.
            genuine = write(tmp_path, "genuine.json", state.read_bytes())
            shutil.copyfile(forged, state)
            swap = (f'#!/bin/sh\ncp "{genuine}" "{state}"\n"{sys.executable}" "$@"\nstatus=$?\n'
                    f'cp "{forged}" "{state}"\nexit $status\n')
            monkeypatch.setattr(gate.sys, "executable", str(write(tmp_path, "swapping.sh", swap, 0o755)))
    elif failure == "descriptor-redirected-during-check":
        # A committed directory the outgoing descriptor never deploys; the descriptor names it only while
        # the check runs, and the outputs and manifest are the ones that redirected descriptor plans.
        write(deployed, "tools/settings.json", (deployed / "agents_extensions/claude/settings.json").read_bytes())
        write(deployed, "tools/extra.py", "value = 1\n")
        commit_all(deployed, "an undeployed committed directory")
        descriptor_path = deployed / plan.DESCRIPTOR
        committed = descriptor_path.read_bytes()
        descriptor_path.write_bytes(committed.replace(b'"agents_extensions/claude"', b'"tools"'))
        deploy_cli(deployed)
        assert (deployed / ".claude/extra.py").is_file()
    else:
        copy = deployed / ".claude/hooks/product.py"
        target = write(tmp_path, "product-copy.py", copy.read_bytes(), 0o755)
        copy.unlink()
        copy.symlink_to(target)
    _, error = gate_verdict(gate, deployed)
    assert error is not None and error.reason == "untracked_inputs"
    assert cause in error.detail


def test_gate_provider_directories_match_deploy_targets(monkeypatch):
    """Layout drift: the gate must name every directory the deploy writes."""
    gate = load_gate(monkeypatch)
    assert sorted(gate.DEPLOY_ROOTS) == sorted({*plan.TARGETS.values(), ".agents"})


def test_print_manifest_is_the_recomposed_plan_not_the_checkout_copy(deployed):
    """The check's manifest output must come from its recomposed plan, never from the worker's file."""
    genuine = (deployed / plan.STATE).read_bytes()
    assert deploy_cli(deployed, "--check", "--print-manifest").encode() == genuine
    write(deployed, ".claude/extra.py", "value = 1\n")
    (deployed / plan.STATE).write_bytes(forge_manifest_listing(deployed, ".claude/extra.py"))
    result = subprocess.run([sys.executable, "-I", "-m", "lu_machinery.agent_deploy", "--project-root",
                             str(deployed), "--check", "--print-manifest"], capture_output=True, env=git_environment(),
                            check=False)
    assert result.returncode == 1 and result.stdout == b""
    (deployed / plan.STATE).write_bytes(genuine)
    printed = deploy_cli(deployed, "--check", "--print-manifest")
    assert ".claude/extra.py" not in json.loads(printed)["files"]


def test_inputs_commit_plans_from_the_commit_not_the_checkout(deployed):
    """The plan printed or applied with --inputs-commit must come from the commit, never from checkout inputs."""
    head = subprocess.run(["git", "-C", str(deployed), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True, env=git_environment()).stdout.strip()
    committed = deploy_cli(deployed, "--check", "--print-manifest", "--inputs-commit", head)
    assert committed.encode() == (deployed / plan.STATE).read_bytes()
    descriptor_path = deployed / plan.DESCRIPTOR
    descriptor_path.write_bytes(descriptor_path.read_bytes().replace(b'"required": true', b'"required": false'))
    (deployed / "agents_extensions/claude/hooks/product.py").write_bytes(GATE_PRODUCT_HOOK + b"print('edit')\n")
    write(deployed, "agents_extensions/claude/hooks/added.py", GATE_PRODUCT_HOOK, 0o755)
    assert deploy_cli(deployed, "--check", "--print-manifest", "--inputs-commit", head) == committed
    # An apply from the commit restores what an apply of the edited checkout wrote.
    deploy_cli(deployed)
    assert (deployed / plan.STATE).read_bytes() != committed.encode()
    deploy_cli(deployed, "--inputs-commit", head)
    assert (deployed / plan.STATE).read_bytes() == committed.encode()
    assert deploy_cli(deployed, "--check", "--print-manifest", "--inputs-commit", head) == committed
    tree = subprocess.run(["git", "-C", str(deployed), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True,
                          check=True, env=git_environment()).stdout.strip()
    for oid in (tree, head[:12]):
        refused = subprocess.run([sys.executable, "-I", "-m", "lu_machinery.agent_deploy", "--project-root",
                                  str(deployed), "--check", "--inputs-commit", oid], capture_output=True, text=True,
                                 env=git_environment(), check=False)
        assert refused.returncode == 2 and "invalid deployment" in refused.stderr, refused.stderr


@pytest.mark.parametrize("redirect", ["core-worktree", "replace-ref"])
def test_inputs_commit_reads_ignore_worker_git_settings(deployed, redirect):
    """Checkout Git settings changed during a check must not redirect what --inputs-commit reads."""
    committed = (deployed / plan.DESCRIPTOR).read_bytes()
    alternate = committed.replace(b'"required": true', b'"required": false')
    # A committed copy under the checkout's own name: a lookup relative to the checkout's parent finds it.
    write(deployed, f"{deployed.name}/{plan.DESCRIPTOR}", alternate)
    commit_all(deployed, "an alternate descriptor")

    def git(*arguments):
        return subprocess.run(["git", "-C", str(deployed), *arguments], capture_output=True, text=True, check=True,
                              env=git_environment()).stdout.strip()

    head = git("rev-parse", "HEAD")
    with plan.selected_tree(str(deployed), check=True) as tree:
        if redirect == "core-worktree":
            git("config", "core.worktree", str(deployed.parent))
        else:
            git("replace", git("rev-parse", f"HEAD:{plan.DESCRIPTOR}"), git("rev-parse", f"HEAD:{deployed.name}/{plan.DESCRIPTOR}"))
        inputs = plan.CommitInputs(tree, head)
        assert inputs.read(plan.DESCRIPTOR) == (committed, 0o644)
        assert set(inputs.inventory("agents_extensions/claude")) == {"settings.json", "hooks/product.py"}
        # A path below a non-directory entry is refused as the checkout refuses it, never read as absent.
        for reader in (inputs, tree):
            with pytest.raises(plan.Invalid, match="^non-directory deployment path: agents_extensions/claude/settings"):
                reader.read("agents_extensions/claude/settings.json/source")
