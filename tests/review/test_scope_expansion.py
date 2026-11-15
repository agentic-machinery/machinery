"""The expansion breaker counts author changes across base-branch merges."""

from __future__ import annotations
from lu_machinery.common.repo_root import machinery_path

import json
import subprocess
from dataclasses import asdict, replace
import pytest
from lu_machinery.review import closeout_cli
from lu_machinery.review.target_resolution import resolve_branch_target
from lu_machinery.review.product_hooks import HOOKS_REL
from lu_machinery.review.product_hooks import SCHEMA
from lu_machinery.review.product_hooks import ProductHooksError
from lu_machinery.review.product_hooks import _module_name
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.agent_runtime.product_roots import agent_runtime_roots as agent_runtime_roots

@pytest.mark.parametrize("mode", ["branch", "pr"])
@pytest.mark.parametrize("growth", ["files", "loc"])
def test_main_merge_excludes_unrelated_growth_but_author_growth_trips(
    tmp_path, capsys, mode, growth
):
    """A main merge must not inflate either files or LOC beyond the frozen scope."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit_file(name, content):
        (repo / name).write_text(content)
        git("add", name)
        git("commit", "-m", name)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    commit_file("seed.txt", "seed\n")
    git("checkout", "-b", "feature")
    commit_file("author.txt", "author\n")
    target = replace(
        resolve_branch_target(repo, "feature", "main"),
        mode=mode,
        base_ref_name="main" if mode == "pr" else None,
    )
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "target": asdict(target),
                "target_args": {"repo_root": str(repo), "mode": mode, "base": "main"},
            }
        )
    )
    prefix = ["--state-file", str(state_file)]
    assert (
        closeout_cli.main(
            prefix
            + [
                "freeze",
                "--issue",
                "Refs #100",
                "--intended-behavior",
                "author change",
                "--non-goals",
                "unrelated main changes",
                "--owner-boundary",
                "author files",
            ]
        )
        == 0
    )
    frozen_receipt = state_file.read_bytes()
    capsys.readouterr()

    git("checkout", "main")
    for index in range(3):
        commit_file(f"unrelated{index}.txt", "main\n" * 10)
    # PR checks use the remote-tracking base, even if local main is stale.
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "feature")
    git("merge", "--no-ff", "main", "-m", "merge main")
    if mode == "pr":
        git("branch", "-f", "main", target.base_sha)
    assert (
        closeout_cli.main(prefix + ["check-expansion", "--current-head", "HEAD"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == {"triggered": False, "reason": None}
    assert state_file.read_bytes() == frozen_receipt

    if growth == "files":
        commit_file("growth1.txt", "growth\n")
        commit_file("growth2.txt", "growth\n")
        reason = "review-triggered files (3) exceed 2x frozen baseline (1)"
    else:
        commit_file("author.txt", "author\n" * 3)
        reason = "review-triggered non-test LOC (3) exceeds 2x frozen baseline (1)"
    assert (
        closeout_cli.main(prefix + ["check-expansion", "--current-head", "HEAD"]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "triggered": True,
        "reason": reason,
    }
    assert state_file.read_bytes() == frozen_receipt


@pytest.mark.usefixtures('configured_roots')
def test_schema_version_rejects_boolean_and_float_lookalikes(tmp_path):
    """Equality with 1 must not admit JSON booleans or floating-point versions."""
    path = tmp_path / "review_hooks.v1.json"
    document = json.dumps(
        {
            "schema": SCHEMA,
            "schema_version": 1,
            "origin": HOOKS_REL,
            "attempt_hooks": "scripts.curriculum.review_attempt_hooks",
        }
    )
    path.write_text(document)
    assert _module_name(path) == "scripts.curriculum.review_attempt_hooks"
    for version in ("true", "1.0", "1e0"):
        path.write_text(document.replace('"schema_version": 1', f'"schema_version": {version}'))
        with pytest.raises(ProductHooksError) as caught:
            _module_name(path)
        assert caught.value.code == "hooks_schema_mismatch"


@pytest.fixture
def product_checkout_root(tmp_path, monkeypatch):
    """``tmp_path`` as a self-contained product checkout whose ``scripts`` package is the cached one.

    It has the package shapes the cases name (``scripts.audit`` a package, ``scripts.ops.product_entry``
    a module), and the machinery's cached entries of those names are hidden for the test, as once the
    machinery is extracted (C-4764): the loader refuses a cached package from another checkout.
    """
    import importlib.util
    import sys

    from lu_machinery.common import repo_root
    importlib.import_module("lu_machinery.review.product_hooks")

    for relative in ("__init__.py", "audit/__init__.py", "ops/__init__.py", "ops/product_entry.py"):
        (tmp_path / "scripts" / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "scripts" / relative).write_text("")
    marker = tmp_path / repo_root.PRODUCT_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("levels: {}\n")
    spec = importlib.util.spec_from_file_location(
        "scripts", tmp_path / "scripts" / "__init__.py", submodule_search_locations=[str(tmp_path / "scripts")]
    )
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    for name in ("scripts.audit", "scripts.ops", "scripts.ops.product_entry"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "scripts", package)
    before = set(sys.modules)
    yield tmp_path
    for name in set(sys.modules) - before:
        if name.startswith("scripts."):
            sys.modules.pop(name, None)


def _hooks_document(**changes):
    return json.dumps({
        "schema": SCHEMA,
        "schema_version": 1,
        "origin": HOOKS_REL,
        "attempt_hooks": "scripts.curriculum.review_attempt_hooks",
        **changes,
    })


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("{", "hooks_not_json"),
        ('{"schema": 1, "schema": 2}', "hooks_not_json"),
        ("[]", "hooks_fields"),
        ("{}", "hooks_fields"),
        (_hooks_document(extra=1), "hooks_fields"),
        (_hooks_document(schema="wrong"), "hooks_schema_mismatch"),
        (_hooks_document(schema_version=2), "hooks_schema_mismatch"),
        (_hooks_document(schema_version="1"), "hooks_schema_mismatch"),
        (_hooks_document(origin="wrong"), "hooks_origin_mismatch"),
    ],
)
def test_hooks_json_refusals_keep_error_codes(tmp_path, raw, code):
    path = tmp_path / "hooks.json"
    path.write_text(raw)
    with pytest.raises(ProductHooksError) as caught:
        _module_name(path)
    assert caught.value.code == code


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "oversized", "invalid_utf8", "unreadable"])
def test_hooks_file_refusals_keep_error_codes(tmp_path, monkeypatch, kind):
    from pathlib import Path

    from lu_machinery.review import product_hooks as hooks

    path = tmp_path / "hooks.json"
    expected = "hooks_not_json"
    if kind == "missing":
        expected = "hooks_missing"
    elif kind == "directory":
        path.mkdir()
        expected = "hooks_not_regular_file"
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(_hooks_document())
        path.symlink_to(target)
        expected = "hooks_not_regular_file"
    elif kind == "oversized":
        path.write_bytes(b" " * (hooks.MAX_HOOKS_BYTES + 1))
        expected = "hooks_too_large"
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    else:
        path.write_text(_hooks_document())

        def unreadable(_path):
            raise OSError("read refused")

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(ProductHooksError) as caught:
        _module_name(path)
    assert caught.value.code == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "name", [None, 1, [], "", "scripts", "audit.check", ".scripts.audit", "scripts..audit", "scripts.bad-name", "scripts.Audit", "scripts.audit\n", "scripts.українська"]
)
def test_hooks_refuses_malformed_names(tmp_path, name):
    path = tmp_path / "hooks.json"
    path.write_text(_hooks_document(attempt_hooks=name))
    with pytest.raises(ProductHooksError) as caught:
        _module_name(path)
    assert caught.value.code == "hooks_module_invalid"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("agent_runtime_roots")
def test_hooks_refuses_foreign_cached_module_without_repinning(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    from lu_machinery.common.repo_root import product_root
    from lu_machinery.ops.product_entry import ConfiguredModuleError
    from lu_machinery.review import product_hooks as hooks

    pinned = hooks.attempt_hooks()
    name = _module_name(product_root() / hooks.HOOKS_REL)
    foreign = ModuleType(name)
    foreign.__file__ = str(tmp_path / "foreign.py")
    monkeypatch.setitem(sys.modules, name, foreign)
    with pytest.raises(ConfiguredModuleError, match="not a file in this checkout"):
        hooks._attempt_hooks_from.__wrapped__(product_root())
    assert hooks.attempt_hooks() is pinned


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("name", "expected", "missing"),
    [
        ("scripts.audit.loader_slice_missing", "hooks_module_missing", None),
        ("scripts.loader_slice_missing.child", None, "scripts.loader_slice_missing"),
        ("scripts.ops.product_entry.child", None, "scripts.ops.product_entry.child"),
    ],
)
def test_hooks_missing_target_keeps_leaf_error_and_parent_error(
    product_checkout_root, tmp_path, monkeypatch, name, expected, missing
):
    from lu_machinery.review import product_hooks as hooks

    path = tmp_path / HOOKS_REL
    path.parent.mkdir(parents=True)
    path.write_text(_hooks_document(attempt_hooks=name))
    if expected:
        with pytest.raises(ProductHooksError) as caught:
            hooks.attempt_hooks(product_root=tmp_path)
        assert caught.value.code == expected
    else:
        with pytest.raises(ModuleNotFoundError) as caught:
            hooks.attempt_hooks(product_root=tmp_path)
        assert caught.value.name == missing


@pytest.mark.usefixtures('configured_roots')
def test_hooks_existing_module_dependency_error_is_not_translated(product_checkout_root, tmp_path, monkeypatch):
    import sys

    from lu_machinery.review import product_hooks as hooks

    name = "scripts.audit.loader_slice_dependency"
    (tmp_path / "scripts" / "audit" / "loader_slice_dependency.py").write_text("import loader_slice_missing_dependency\n")
    path = tmp_path / HOOKS_REL
    path.parent.mkdir(parents=True)
    path.write_text(_hooks_document(attempt_hooks=name))
    with pytest.raises(ModuleNotFoundError) as caught:
        hooks.attempt_hooks(product_root=tmp_path)
    assert caught.value.name == "loader_slice_missing_dependency"
    assert name not in sys.modules


@pytest.mark.usefixtures('configured_roots')
def test_configured_callers_are_counted_once_and_implementation_separately(tmp_path):
    """The dependency scan must expose both callers without double counting R's old wrapper."""
    from pathlib import Path

    from lu_machinery.ci.components import scan_imports
    from lu_machinery.lint.boundary_ratchet import CONFIG_PATH
    from lu_machinery.lint.boundary_ratchet import _configured_line
    from lu_machinery.lint.boundary_ratchet import _dynamic_count
    from lu_machinery.lint.boundary_ratchet import load_config

    paths = ["lu_machinery/research/registry.py", "lu_machinery/review/product_hooks.py", "lu_machinery/ops/product_entry.py"]
    graph = scan_imports({path: machinery_path(path.removeprefix("lu_machinery/")).read_bytes() for path in paths})
    assert [row["path"] for row in graph["configured_loads"]] == paths[:2]
    # This repository lint policy is intentionally not wheel package data.
    policy = tmp_path / CONFIG_PATH.name
    policy.write_bytes(subprocess.check_output(
        ["git", "show", "HEAD:lu_machinery/" + CONFIG_PATH.as_posix()],
        cwd=Path(__file__).resolve().parents[2],
    ))
    config = load_config(policy)
    assert _dynamic_count(graph, config, "machinery") == 2
    assert _configured_line(graph) == "configured-entry-point implementation=1 callers=2"
