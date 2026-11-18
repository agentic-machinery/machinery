from __future__ import annotations

from pathlib import Path
import pytest
from lu_machinery.ci import slot_inventory
import copy
import hashlib
import json
import subprocess
import sys
from lu_machinery.ci import components as c
from tests.helpers.product_tree import configured_roots as configured_roots

def _write_workflow(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def test_inventory_expands_static_matrix_and_ignores_non_pr_workflows(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "pr.yml",
        """on: pull_request
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu, macos]
        python: ['3.11', '3.12']
        exclude:
          - os: macos
            python: '3.11'
        include:
          - os: windows
            python: '3.12'
    runs-on: ${{ matrix.os }}
""",
    )
    _write_workflow(
        tmp_path,
        "push.yml",
        """on: push
jobs:
  ignored:
    runs-on: ubuntu-latest
""",
    )

    report = slot_inventory.inventory_report(tmp_path)

    assert report["total"] == 4
    assert report["workflows"] == [{"workflow": str(tmp_path / "pr.yml"), "jobs": {"test": 4}, "total": 4}]


def test_check_fails_when_total_exceeds_ceiling(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(slot_inventory, "CI_SLOT_CEILING", 31)
    _write_workflow(
        tmp_path,
        "ci.yml",
        """on: pull_request
jobs:
  pytest:
    strategy:
      matrix:
        shard: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    runs-on: ubuntu-latest
  baseline:
    strategy:
      matrix:
        lane: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    runs-on: ubuntu-latest
""",
    )

    result = slot_inventory.main(["--workflow-dir", str(tmp_path), "--check", "--json"])

    assert result == 1
    assert '"pass": false' in capsys.readouterr().out


def test_runtime_matrix_fails_closed(tmp_path: Path) -> None:
    """A matrix computed at run time cannot be counted, so the inventory refuses it."""
    _write_workflow(
        tmp_path,
        "ci.yml",
        """on: pull_request
jobs:
  pytest:
    strategy:
      matrix:
        shard: ${{ fromJSON(needs.plan.outputs.shards) }}
    runs-on: ubuntu-latest
""",
    )

    with pytest.raises(ValueError, match="static YAML list"):
        slot_inventory.inventory_report(tmp_path)


@pytest.fixture
def manifest():
    return c.load_manifest()


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=30)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    for name, text in {
        "scripts/config.py": "CONFIG = 1\n",
        "scripts/ci/components.py": "# runner\n",
        "curriculum/hidden.yaml": "version: 1\n",
        "tests/test_fixture.py": "def test_fixture():\n    assert True\n",
        "scripts/ci/frontend_change_denominator.json": json.dumps({"version": "1", "paths": ["site/"]}),
        "requirements-lock.txt": "# lock\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(tmp_path, "add", ".")
    return tmp_path


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_exact_overrides_precede_longest_prefix_and_shared_fallback(manifest):
    assert c.assign_path("scripts/practice/euphony_stem_engine.py", manifest) == (["shared-core"], "exact")
    assert c.assign_path("scripts/practice/new.py", manifest) == (["practice-frontend"], "prefix")
    assert c.assign_path("tests/test_unclassified.py", manifest) == (["shared-core"], "prefix")
    assert c.assign_path("new-root/file.txt", manifest) == (list(c.NODE_IDS), "unmapped")
    assert c.assign_path("../outside", manifest) == (list(c.NODE_IDS), "unmapped")


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_ambiguous_and_dynamic_unknowns_select_all(manifest):
    manifest["path_prefixes"].append({"path": "scripts/practice/", "components": ["atlas-data"]})
    assert c.assign_path("scripts/practice/new.py", manifest)[1] == "ambiguous"
    assert set(c.affected(["scripts/practice/new.py"], manifest)["components"]) == set(c.NODE_IDS)
    manifest["unresolved_edges"] = [{"path": "scripts/atlas/loader.py"}]
    assert c.assign_path("scripts/atlas/loader.py", manifest)[1] == "dynamic-unresolved"
    manifest["edges"][0]["resolved"] = False
    assert c.affected(["scripts/projects/open_model_data/view.py"], manifest)["fallback_reasons"] == ["dynamic-unresolved"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_affected_reaches_cycle_consumers_shared_and_empty(manifest):
    selected = c.affected(["curriculum/a.yaml"], manifest)["components"]
    assert {"curriculum-generation", "curriculum-display", "atlas-data", "atlas-frontend", "practice-frontend", "open-model-data"} <= set(selected)
    assert set(c.affected(["scripts/config.py"], manifest)["components"]) == set(c.NODE_IDS)
    assert c.affected([], manifest)["components"] == []
    result = c.affected(["scripts/projects/open_model_data/view.py"], manifest)
    assert set(result["components"]) == set(c.NODE_IDS)
    assert result["unresolved_edges"] > 0


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_inventory_includes_index_only_sparse_and_odd_paths(repo, manifest):
    hidden = repo / "curriculum/hidden.yaml"
    hidden.unlink()
    odd = repo / 'site/слово "name".ts'
    odd.parent.mkdir()
    odd.write_text("// fixture\n")
    git(repo, "add", str(odd.relative_to(repo)))
    assert "curriculum/hidden.yaml" in c.tracked_paths(repo)
    assert str(odd.relative_to(repo)) in c.tracked_paths(repo)
    report = c.inventory(manifest, repo)
    assert report["unassigned"] == report["unmapped_test_files"] == report["selector_parity_gaps"] == 0
    assert report["tracked_paths"] == 7
    rogue = repo / "new-root/file.py"
    rogue.parent.mkdir()
    rogue.write_text("# new\n")
    git(repo, "add", str(rogue.relative_to(repo)))
    assert c.inventory(manifest, repo)["unassigned"] == 1


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_parity_detects_missing_frontend_consumer_edge(repo, manifest):
    manifest["exact_paths"]["site/new.ts"] = ["open-model-data"]
    assert c.parity_gaps(["site/new.ts"], manifest, repo) == ["site/new.ts"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("mutation", ["schema", "node", "owner", "test-id", "prefix", "argv", "edge"])
def test_manifest_validation_rejects_bad_contract(tmp_path, manifest, mutation):
    if mutation == "schema":
        manifest["schema_version"] = 2
    elif mutation == "node":
        manifest["components"].pop("harness")
    elif mutation == "owner":
        manifest["exact_paths"]["a"] = ["unknown"]
    elif mutation == "test-id":
        manifest["components"]["harness"]["test_files"] = ["tests/test_a.py::test_one"]
    elif mutation == "prefix":
        manifest["path_prefixes"].append({"path": "scripts", "components": ["harness"]})
    elif mutation == "argv":
        manifest["components"]["harness"]["verify"][0]["argv"] = []
    else:
        manifest["edges"][0]["consumer"] = "unknown"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        c.load_manifest(path)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_test_prefixes_expand_index_and_sparse_census_is_complete(repo, manifest):
    manifest["shared_integration_tests"] = []
    manifest["components"]["harness"]["test_files"] = []
    manifest["components"]["harness"]["test_prefixes"] = ["tests/"]
    assert c.test_files("harness", manifest, repo) == ["tests/test_fixture.py"]
    (repo / "tests/test_fixture.py").unlink()
    assert c.test_files("harness", manifest, repo) == ["tests/test_fixture.py"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_ast_closure_relative_bare_literal_loads_and_nonliteral():
    sources = {
        "scripts/lexicon/source.py": b"X = 1\n",
        "scripts/lexicon/relative.py": b"from . import source\n",
        "scripts/build/consumer.py": b"from lexicon import relative\n",
        "scripts/audit/loader.py": b'''from pathlib import Path
import importlib.util as util
ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    return util.spec_from_file_location(name, path)
load('source', ROOT / 'scripts' / 'lexicon' / 'source.py')
''',
        "tests/test_use.py": b"import scripts.build.consumer\n",
        "tests/test_dynamic.py": b"from importlib import import_module as load\nload(target)\n",
    }
    graph = c.scan_imports(sources)
    assert ("scripts/lexicon/relative.py", "scripts/lexicon/source.py") in graph["file_edges"]
    assert ("scripts/build/consumer.py", "scripts/lexicon/relative.py") in graph["file_edges"]
    assert ("scripts/audit/loader.py", "scripts/lexicon/source.py") in graph["file_edges"]
    assert any(e["path"] == "tests/test_dynamic.py" for e in graph["unresolved_edges"])


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_indexed_imports_survive_sparse_checkout_and_invalidate_on_edits(repo, manifest):
    path = repo / "scripts/config.py"
    path.write_text("from scripts.ci import components\n")
    git(repo, "add", "scripts/config.py")
    git(repo, "update-index", "--skip-worktree", "scripts/config.py")
    path.unlink()
    assert ("scripts/config.py", "scripts/ci/components.py") in c.import_graph(manifest, repo)["file_edges"]
    path.write_text("import importlib\nimportlib.import_module(target)\n")
    git(repo, "update-index", "--no-skip-worktree", "scripts/config.py")
    assert any(e["path"] == "scripts/config.py" for e in c.import_graph(manifest, repo)["unresolved_edges"])


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_import_graph_reuse_tracks_manifest_content(repo, manifest, monkeypatch):
    (repo / "scripts/config.py").write_text("from scripts.ci import components\n")
    manifest["exact_paths"]["scripts/config.py"] = ["atlas-data"]
    manifest["exact_paths"]["scripts/ci/components.py"] = ["harness"]
    manifest["edges"] = [{"id": "required", "kind": "import", "producer": "harness",
                          "consumer": "atlas-data", "resolved": True}]
    scans = []
    scan = c.cached_import_scan

    def counted_scan(*args):
        scans.append(args)
        return scan(*args)

    monkeypatch.setattr(c, "cached_import_scan", counted_scan)
    first = c.import_graph(manifest, repo)
    assert first["node_edges"] == [("harness", "atlas-data")]
    assert first["missing_mandatory_edges"] == []
    # Equivalent content reuses the lifting, but callers cannot corrupt it.
    first["node_edges"].clear()
    assert c.import_graph(copy.deepcopy(manifest), repo)["node_edges"] == [("harness", "atlas-data")]
    assert len(scans) == 1

    # Mutate the same dictionary after its first query: an identity-keyed
    # cache would retain the old consumer and hide the missing required edge.
    manifest["exact_paths"]["scripts/config.py"][:] = ["practice-frontend"]
    changed = c.import_graph(manifest, repo)
    assert changed["node_edges"] == [("harness", "practice-frontend")]
    assert changed["missing_mandatory_edges"] == ["required"]
    assert len(scans) == 2

    manifest["edges"][0]["consumer"] = "practice-frontend"
    assert c.import_graph(manifest, repo)["missing_mandatory_edges"] == []
    assert len(scans) == 3


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_complete_node_test_set_ownership_importers_and_integration(repo, manifest):
    manifest["shared_integration_tests"] = ["tests/test_fixture.py"]
    manifest["components"]["atlas-data"]["test_files"] = []
    for path, source in {
        "scripts/lexicon/source.py": "VALUE = 1\n",
        "scripts/build/consumer.py": "from scripts.lexicon.source import VALUE\n",
        "tests/test_source.py": "from scripts.build.consumer import VALUE\n",
        "tests/test_other.py": "VALUE = 2\n",
        "tests/test_mapped.py": "VALUE = 3\n",
    }.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    manifest["exact_paths"]["tests/test_mapped.py"] = ["atlas-data"]
    git(repo, "add", ".")
    assert c.test_files("atlas-data", manifest, repo) == [
        "tests/test_fixture.py", "tests/test_mapped.py", "tests/test_source.py"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_unresolved_and_missing_mandatory_edges_force_all(manifest):
    graph = {"node_edges": [], "file_edges": [], "unresolved_edges": [], "missing_mandatory_edges": ["lost"]}
    assert set(c.affected(["scripts/lexicon/source.py"], manifest, graph)["components"]) == set(c.NODE_IDS)
    graph["missing_mandatory_edges"] = []
    graph["unresolved_edges"] = [{"path": "scripts/loader.py"}]
    assert c.affected(["site/new.ts"], manifest, graph)["fallback_reasons"] == ["dynamic-unresolved"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_vitest_files_follow_ownership_and_existing_scripts(repo, manifest):
    path = repo / "site/tests/unit/shared.test.ts"
    path.parent.mkdir(parents=True)
    path.write_text("// test\n")
    git(repo, "add", ".")
    for node in manifest["selector_contracts"]["frontend_components"]:
        files = c.vitest_files(node, manifest, repo)
        assert files == ["site/tests/unit/shared.test.ts"]
        commands = c.node_test_commands(node, manifest, ["tests/test_fixture.py"], files)
        assert commands[1]["argv"] == ["npm", "run", "test:unit", "--", "tests/unit/shared.test.ts"]
    built = manifest["vitest"]["built_output_files"]
    assert c.node_test_commands("atlas-frontend", manifest, [], built)[1]["argv"][:3] == ["npm", "run", "test:built-output"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_literal_targets_and_parse_errors_are_conservative():
    assert c.literal_target(c.ast.parse("Path(__file__).parent / 'x.py'", mode="eval").body, {}, "scripts/load.py") == c.PurePosixPath("scripts/x.py")
    graph = c.scan_imports({"scripts/broken.py": b"def invalid(\n"})
    assert graph["unresolved_edges"][0]["reason"] == "parse-error"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_vitest_collection_uses_installed_cli_and_keeps_failures(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '[{"file":"shared.test.ts","name":"renders"}]')
    monkeypatch.setattr(c.subprocess, "run", run)
    rows, code = c.collect_vitest(["site/tests/unit/shared.test.ts"])
    assert code == 0 and rows[0]["name"] == "renders"
    assert calls[0][0][-1] == "tests/unit/shared.test.ts"
    assert calls[0][1]["env"]["npm_config_offline"] == "true"
    assert c.collect_vitest([]) == ([], 0)
    monkeypatch.setattr(c.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 2, ""))
    assert c.collect_vitest(["site/tests/unit/shared.test.ts"])[1] == 2


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_collect_ids_at_check_time_and_collection_failure(repo, monkeypatch):
    monkeypatch.setattr(c, "project_interpreter", lambda root: Path(sys.executable))
    (repo / "pytest.ini").write_text("[pytest]\naddopts = -v\n")
    path = repo / "tests/test_fixture.py"
    path.write_text("import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_fixture(n):\n    assert n > 0\n")
    ids, code = c.collect_tests(["tests/test_fixture.py"], [], repo)
    assert code == 0
    assert ids == ["tests/test_fixture.py::test_fixture[1]", "tests/test_fixture.py::test_fixture[2]"]
    path.write_text("raise RuntimeError('broken collection')\n")
    assert c.collect_tests(["tests/test_fixture.py"], [], repo)[1] != 0


def input_wrapper(repo, manifest, component="atlas-data"):
    member = repo / "input.json"
    member.write_text('{"schema_version": 1}\n')
    return {
        "schema": "component-inputs.v1", "component": component,
        "identities": c.current_identities(component, manifest, repo),
        "families": [{"name": name, "schema": "existing-family-schema", "version": 1,
                      "members": [{"name": name, "path": "input.json", "sha256": hashlib.sha256(member.read_bytes()).hexdigest()}]}
                     for name in manifest["components"][component]["input_families"]],
    }


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_input_verification_and_source_lock_drift(repo, manifest):
    wrapper = input_wrapper(repo, manifest)
    path = repo / "inputs.json"
    path.write_text(json.dumps(wrapper))
    members = c.verify_inputs(path, "atlas-data", manifest, repo)
    assert members["lexicon-manifest"] == repo / "input.json"
    (repo / "requirements-lock.txt").write_text("# changed lock\n")
    with pytest.raises(ValueError, match="stale"):
        c.verify_inputs(path, "atlas-data", manifest, repo)

    uv_lock = repo / "uv.lock"
    uv_lock.write_text("version = 1\n")
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    path.write_text(json.dumps(input_wrapper(repo, manifest)))
    uv_lock.write_text("version = 1\nrevision = 2\n")
    with pytest.raises(ValueError, match="stale"):
        c.verify_inputs(path, "atlas-data", manifest, repo)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("mutation", ["schema", "component", "missing-family", "duplicate", "corrupt", "missing", "traversal", "symlink", "version"])
def test_input_refusals_do_not_write_outputs(repo, manifest, mutation):
    wrapper = input_wrapper(repo, manifest)
    if mutation in {"schema", "component"}:
        wrapper[mutation] = "wrong"
    elif mutation == "missing-family":
        wrapper["families"].pop()
    elif mutation == "duplicate":
        wrapper["families"].append(copy.deepcopy(wrapper["families"][0]))
    elif mutation == "corrupt":
        (repo / "input.json").write_text("corrupt\n")
    elif mutation == "missing":
        (repo / "input.json").unlink()
    elif mutation == "traversal":
        wrapper["families"][0]["members"][0]["path"] = "../outside"
    elif mutation == "symlink":
        (repo / "input.json").unlink()
        (repo / "input.json").symlink_to(repo.parent)
    else:
        wrapper["families"][0]["version"] = 0
    path = repo / "inputs.json"
    path.write_text(json.dumps(wrapper))
    with pytest.raises(ValueError):
        c.verify_inputs(path, "atlas-data", manifest, repo)
    assert not (repo / "output").exists()


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_expand_commands_fixed_argv_and_missing_inputs(repo, monkeypatch):
    monkeypatch.setattr(c, "project_interpreter", lambda root: Path(sys.executable))
    command = {"argv": ["{python}", "producer.py", "{member:source}", "{output_dir}/artifact.json"]}
    args = c.expand_command(command, repo, repo / "out", {"source": repo / "source $(literal).json"})
    assert args == [sys.executable, "producer.py", str(repo / "source $(literal).json"), str(repo / "out/artifact.json")]
    with pytest.raises(ValueError, match="missing named"):
        c.expand_command(command, repo, repo / "out", {})
    with pytest.raises(ValueError, match="output-dir"):
        c.expand_command(command, repo, None, {"source": repo / "source.json"})
    with pytest.raises(ValueError, match="placeholder"):
        c.expand_command({"argv": ["{unknown}"]}, repo, None, {})


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_run_commands_real_pass_failure_and_skips(repo, monkeypatch):
    monkeypatch.setattr(c, "project_interpreter", lambda root: Path(sys.executable))
    command = {"argv": ["{python}", "-m", "pytest", "-q", "tests/test_fixture.py"], "cwd": ".", "scope": "code-contract"}
    reports, code = c.run_commands([command], repo, None, {})
    assert code == 0 and reports[0]["result"] == "pass"
    (repo / "tests/test_fixture.py").write_text("import pytest\ndef test_fixture():\n    pytest.skip('artifact absent')\n")
    reports, code = c.run_commands([command], repo, None, {})
    assert code == 3 and reports[0]["result"] == "artifact-dependent" and reports[0]["skipped"] == 1
    (repo / "tests/test_fixture.py").write_text("def test_fixture():\n    assert False\n")
    reports, code = c.run_commands([command], repo, None, {})
    assert code == 1 and reports[0]["result"] == "fail"
    reports, code = c.run_commands([command, command], repo, None, {}, keep_going=True)
    assert code == 1 and len(reports) == 2


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_inventory_collection_and_identities_and_nul_paths(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(c, "current_identities", lambda *a: {"source": "structural-fixture"})
    monkeypatch.setattr(c, "collect_tests", lambda *a: (["tests/test_fixture.py::test_one"], 0))
    assert c.main(["inventory", "--component", "atlas-data", "--collect", "--identities"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["identities"]["source"] == "structural-fixture"
    assert report["test_ids"] == ["tests/test_fixture.py::test_one"]
    paths = tmp_path / "changed.z"
    paths.write_bytes(b"scripts/config.py\0new-root/weird\nname.ts\0")
    assert c.main(["affected", "--paths-file", str(paths)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed_paths"] == 2 and set(report["components"]) == set(c.NODE_IDS)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_cli_inventory_affected_and_error_are_structural(monkeypatch, capsys, manifest):
    assert c.main(["list"]) == 0
    assert set(json.loads(capsys.readouterr().out)["components"]) == set(c.NODE_IDS)
    assert c.main(["affected", "never-classified/new.txt"]) == 0
    assert set(json.loads(capsys.readouterr().out)["components"]) == set(c.NODE_IDS)
    monkeypatch.setattr(c, "inventory", lambda m: {"unassigned": 1, "ambiguous": 0, "dynamic_unresolved": 0,
                                                  "unmapped_test_files": 0, "selector_parity_gaps": 0})
    assert c.main(["inventory", "--check"]) == 1
    capsys.readouterr()
    assert c.main(["inventory", "--collect"]) == 2
    assert "invalid_or_unavailable" in capsys.readouterr().out
    monkeypatch.setattr(c, "collect_tests", lambda *a: ([], 2))
    assert c.main(["test", "--component", "atlas-data"]) == 2
    assert json.loads(capsys.readouterr().out)["fallback_reason"] == "collection-error"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_cli_help_and_required_build_arguments(capsys):
    for operation in ([], ["build"], ["test"], ["verify"], ["junit"]):
        with pytest.raises(SystemExit) as exit_info:
            c.parser().parse_args([*operation, "--help"])
        assert exit_info.value.code == 0
        help_text = capsys.readouterr().out
        assert all(label in help_text for label in ["Outputs:", "Exit codes:", "Related:", "Examples:"])
    with pytest.raises(SystemExit) as exit_info:
        c.parser().parse_args(["build", "--component", "atlas-data"])
    assert exit_info.value.code == 2


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_imported_parent_initializers_relative_missing_and_keyword_loads():
    sources = {
        "scripts/__init__.py": b"",
        "scripts/lexicon/__init__.py": b"from . import source\n",
        "scripts/lexicon/source.py": b"X = 1\n",
        "scripts/lexicon/consumer.py": b"from .missing import X\nfrom lexicon.absent import Y\n",
        "tests/test_use.py": b"import scripts.lexicon.source\n",
        "tests/test_load.py": b"from importlib.util import spec_from_file_location as load\nload(name='x', location='scripts/lexicon/source.py')\n",
    }
    graph = c.scan_imports(sources)
    assert ("scripts/lexicon/__init__.py", "scripts/lexicon/source.py") in graph["file_edges"]
    assert ("tests/test_use.py", "scripts/lexicon/__init__.py") in graph["file_edges"]
    assert ("tests/test_use.py", "scripts/__init__.py") in graph["file_edges"]
    assert ("tests/test_load.py", "scripts/lexicon/source.py") in graph["file_edges"]
    assert graph["unresolved_edges"] == [
        {"path": "scripts/lexicon/consumer.py", "line": line, "reason": "missing-local-import"}
        for line in (1, 2)
    ]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_configured_entry_point_calls_are_module_loads():
    """Plain, aliased and qualified helper calls load by name; a non-literal name stays unresolved."""
    sources = {
        "scripts/__init__.py": b"",
        "lu_machinery/ops/product_entry.py": (
            b"import importlib\n\ndef load_configured_module(name):\n    return importlib.import_module(name)\n"
        ),
        "scripts/review/hooks.py": b"X = 1\n",
        "scripts/plain.py": b"from lu_machinery.ops.product_entry import load_configured_module\nload_configured_module(name)\n",
        "scripts/aliased.py": (
            b"from lu_machinery.ops.product_entry import load_configured_module as load\n"
            b"load(first)\nload(name=second)\n"
        ),
        "scripts/qualified.py": b"import scripts.ops.product_entry as entry\nentry.load_configured_module('scripts.review.hooks')\n",
        "scripts/module_alias.py": b"from scripts.ops import product_entry\nproduct_entry.load_configured_module(name='scripts.review.hooks')\n",
        "scripts/other.py": b"from importlib import import_module as load\nload(name)\n",
    }
    graph = c.scan_imports(sources)
    assert ("scripts/qualified.py", "scripts/review/hooks.py") in graph["file_edges"]
    assert ("scripts/module_alias.py", "scripts/review/hooks.py") in graph["file_edges"]
    unresolved = {(edge["path"], edge["line"]) for edge in graph["unresolved_edges"]
                  if edge["reason"] == "nonliteral-or-missing-load"}
    configured = {(edge["path"], edge["line"]) for edge in graph["configured_loads"]}
    assert configured == {("scripts/plain.py", 2), ("scripts/aliased.py", 2), ("scripts/aliased.py", 3)}
    # The implementation and an unrelated import_module alias stay ordinary non-literal loads.
    assert unresolved == configured | {("lu_machinery/ops/product_entry.py", 4), ("scripts/other.py", 2)}
    assert c.CONFIGURED_LOADER_PATH in sources


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("loader", ["from runpy import run_path as load", "from importlib.util import spec_from_file_location as load"])
def test_configured_alias_never_reinterprets_another_loader(loader):
    """A file-wide helper alias that shares a name with another loader keeps that loader's reading."""
    call = "load('scripts/prod/runner.py')" if "run_path" in loader else "load('runner', 'scripts/prod/runner.py')"
    sources = {
        "scripts/__init__.py": b"",
        "scripts/prod/__init__.py": b"",
        "scripts/prod/runner.py": b"R = 1\n",
        "scripts/prod/widget.py": b"W = 1\n",
        "scripts/mixed.py": (
            f"def run():\n    {loader}\n    {call}\n\n"
            "def hooks():\n    from lu_machinery.ops.product_entry import load_configured_module as load\n"
            "    load('scripts.prod.widget')\n"
        ).encode(),
        "scripts/renamed.py": (
            b"from lu_machinery.ops.product_entry import load_configured_module as run_module\n"
            b"run_module(name='scripts.prod.widget')\n"
        ),
    }
    graph = c.scan_imports(sources)
    assert ("scripts/mixed.py", "scripts/prod/runner.py") in graph["file_edges"]
    # Read as the other loader, the helper's literal is unprovable, so it stays an unresolved load.
    assert {"path": "scripts/mixed.py", "line": 7, "reason": "nonliteral-or-missing-load"} in graph["unresolved_edges"]
    assert ("scripts/renamed.py", "scripts/prod/widget.py") in graph["file_edges"]
    assert not [edge for edge in graph["unresolved_edges"]
                if edge["path"] == "scripts/renamed.py" and edge["reason"] == "nonliteral-or-missing-load"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_skips_never_hide_a_failed_command(repo, monkeypatch):
    monkeypatch.setattr(c, "project_interpreter", lambda root: Path(sys.executable))
    (repo / "tests/test_fixture.py").write_text(
        "import pytest\ndef test_skip():\n    pytest.skip('artifact absent')\ndef test_fail():\n    assert False\n"
    )
    command = {"argv": ["{python}", "-m", "pytest", "-q", "tests/test_fixture.py"], "cwd": ".", "scope": "code-contract"}
    reports, code = c.run_commands([command], repo, None, {})
    assert code == 1 and reports[0]["skipped"] == 1 and reports[0]["result"] == "fail"


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_unknown_loads_and_their_importers_are_in_every_node_set(repo, manifest):
    manifest["shared_integration_tests"] = []
    manifest["components"]["atlas-data"]["test_files"] = []
    for path, text in {
        "scripts/build/unknown_loader.py": "import importlib\nimportlib.import_module(target)\n",
        "tests/test_indirect_load.py": "from scripts.build import unknown_loader\n",
        "tests/test_direct_load.py": "import importlib\nimportlib.import_module(target)\n",
        "tests/test_unrelated.py": "X = 1\n",
    }.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(repo, "add", ".")
    assert c.test_files("atlas-data", manifest, repo) == ["tests/test_direct_load.py", "tests/test_indirect_load.py"]
    report = c.inventory(manifest, repo)
    assert report["dynamic_unresolved"] == report["unresolved_import_edges"] == 2


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize('gap', ['unassigned', 'ambiguous', 'unmapped_test_files', 'selector_parity_gaps', 'missing_mandatory_edges'])
def test_inventory_check_allows_unknown_imports_but_rejects_contract_gaps(monkeypatch, capsys, gap):
    report = dict.fromkeys(('unassigned', 'ambiguous', 'unmapped_test_files', 'selector_parity_gaps'), 0)
    report.update(dynamic_unresolved=239, import_graph={'missing_mandatory_edges': []})
    monkeypatch.setattr(c, 'inventory', lambda m: report)
    assert c.main(['inventory', '--check']) == 0
    assert json.loads(capsys.readouterr().out)['dynamic_unresolved'] == 239
    if gap == 'missing_mandatory_edges':
        report['import_graph'][gap] = ['required-edge']
    else:
        report[gap] = 1
    assert c.main(['inventory', '--check']) == 1


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_journal_counts_phases_and_ignores_worker_duplicates(tmp_path, monkeypatch):
    from types import SimpleNamespace
    path = tmp_path / 'results.jsonl'
    monkeypatch.setenv('LU_COMPONENT_TEST_JOURNAL', str(path))
    monkeypatch.delenv('PYTEST_XDIST_WORKER', raising=False)
    c.pytest_runtest_logreport(SimpleNamespace(nodeid='pass', when='setup', outcome='passed'))
    c.pytest_runtest_logreport(SimpleNamespace(nodeid='pass', when='call', outcome='passed'))
    c.pytest_runtest_logreport(SimpleNamespace(nodeid='error', when='call', outcome='passed'))
    c.pytest_runtest_logreport(SimpleNamespace(nodeid='error', when='teardown', outcome='failed'))
    c.pytest_runtest_logreport(SimpleNamespace(nodeid='skip', when='setup', outcome='skipped'))
    monkeypatch.setenv('PYTEST_XDIST_WORKER', 'gw0')
    c.pytest_runtest_logreport(SimpleNamespace(nodeid='ignored', when='call', outcome='passed'))
    assert c.partial_test_results(path) == {'passed': 1, 'failed': 1, 'skipped': 1, 'failing_test_ids': ['error']}
    assert c.partial_test_results(tmp_path / 'missing')['passed'] == 0
    path.write_text(json.dumps({'id': 'unicode\u2028inside', 'when': 'call', 'outcome': 'failed'}, ensure_ascii=False) + '\n')
    assert c.partial_test_results(path)['failing_test_ids'] == ['unicode\u2028inside']


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_junit_coverage_fresh_sets_failures_skips_absence_and_duplicate_precedence(repo, manifest, monkeypatch):
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-m', 'fixture\n\nX-Agent: codex/impl-9884-d')
    path = repo / 'full.xml'
    path.write_text('''<testsuites><testsuite>
      <testcase classname="tests.test_fixture" name="test_ok[dot.value]"/>
      <testcase classname="tests.test_fixture.OtherClass" name="test_fail"><failure/></testcase>
      <testcase classname="tests.test_fixture" name="test_skip"><skipped/></testcase>
      </testsuite></testsuites>''')
    duplicate = repo / 'duplicate.xml'
    duplicate.write_text('<testsuite><testcase classname="tests.test_fixture.OtherClass" name="test_fail"/></testsuite>')
    ids = ['tests/test_fixture.py::test_ok[dot.value]', 'tests/test_fixture.py::OtherClass::test_fail',
           'tests/test_fixture.py::test_skip', 'tests/test_fixture.py::test_absent', 'tests/test_other.py::test_other']
    monkeypatch.setattr(c, 'test_files', lambda *a: ['tests/test_fixture.py'])
    calls = []
    def collect(files, args, root):
        calls.append((files, args))
        return ids, 0
    monkeypatch.setattr(c, 'collect_tests', collect)
    report, code = c.junit_coverage([path, duplicate], manifest, ['atlas-data', 'harness'], repo)
    assert len(calls) == 1
    for row in report['nodes'].values():
        assert {key: row[key] for key in ('collected', 'passed', 'failed', 'skipped', 'absent')} == {
            'collected': 4, 'passed': 1, 'failed': 1, 'skipped': 1, 'absent': 1}
        assert row['failing_test_ids'] == [ids[1]] and row['absent_test_ids'] == [ids[3]]
    assert code == 1
    assert report['junit_inputs'][0]['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(c, 'collect_tests', lambda *a: ([ids[0]], 0))
    assert c.junit_coverage([path], manifest, ['harness'], repo)[1] == 0
    monkeypatch.setattr(c, 'collect_tests', lambda *a: ([], 2))
    report, code = c.junit_coverage([path], manifest, ['harness'], repo)
    assert code == 2 and report['fallback_reason'] == 'collection-error'
    with pytest.raises(ValueError):
        c.junit_coverage([], manifest, ['harness'], repo)
    path.write_text('<testsuite><testcase name="tests.test_fixture"><skipped/></testcase></testsuite>')
    monkeypatch.setattr(c, 'collect_tests', lambda *a: ([ids[0]], 0))
    report, code = c.junit_coverage([path], manifest, ['harness'], repo)
    assert code == 1
    assert report['nodes']['harness']['module_results'] == {'tests/test_fixture.py': 'skipped'}
    assert report['nodes']['harness']['absent'] == 1
    path.write_text('<testsuite/>')
    with pytest.raises(ValueError):
        c.junit_coverage([path], manifest, ['harness'], repo)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("source,target", [
    ('from pathlib import Path\nPath("schemas/input.json").read_text()', "schemas/input.json"),
    ('open("schemas/input.json")', "schemas/input.json"),
    ('import io\nio.open("schemas/input.json")', "schemas/input.json"),
    ('import builtins\nbuiltins.open(file="schemas/input.json")', "schemas/input.json"),
    ('import subprocess, sys\nsubprocess.run([sys.executable, "scripts/config.py"])', "scripts/config.py"),
    ('from subprocess import run as go\ngo(["python3", "-m", "scripts.config"])', "scripts/config.py"),
])
def test_runtime_literal_file_and_subprocess_edges(source, target):
    graph = c.scan_imports({"tests/test_reader.py": source.encode(), "scripts/config.py": b"VALUE = 1"},
                           {"tests/test_reader.py", "scripts/config.py", "schemas/input.json"})
    assert ("tests/test_reader.py", target) in graph["file_edges"]
    assert not graph["unresolved_edges"]


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("source,reason", [
    ('open(variable)', "unresolved-file-read"),
    ('Path(variable).read_bytes()', "unresolved-file-read"),
    ('from pathlib import Path\nPath("missing.json").open()', "unresolved-file-read"),
    ('import sys as system\nsystem.path.insert(0, "scripts")', "sys-path"),
    ('import sys\nsys.path = ["scripts"]', "sys-path"),
    ('from sys import path as paths\npaths[:] = ["scripts"]', "sys-path"),
    ('import subprocess\nsubprocess.run(command)', "unresolved-subprocess"),
    ('import subprocess\nsubprocess.run(["python3", "scripts/config.py", argument])', "unresolved-subprocess"),
    ('import subprocess\nsubprocess.run(["python3", "scripts/config.py"], shell=True)', "unresolved-subprocess"),
    ('import os\nos.system("echo hello")', "unresolved-subprocess"),
])
def test_unresolved_runtime_edges_force_full_selection(manifest, source, reason):
    graph = c.scan_imports({"scripts/projects/open_model_data/reader.py": source.encode()})
    assert any(edge["reason"] == reason for edge in graph["unresolved_edges"])
    graph |= {"node_edges": [], "missing_mandatory_edges": []}
    assert set(c.affected(["scripts/projects/open_model_data/reader.py"], manifest, graph)["components"]) == set(c.NODE_IDS)


@pytest.mark.usefixtures("component_fixture_paths")
@pytest.mark.usefixtures('configured_roots')
def test_literal_file_read_importer_in_resolved_test_set(repo, manifest):
    schema = repo / "site/schemas/input.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}")
    reader = repo / "tests/test_schema_reader.py"
    reader.write_text('from pathlib import Path\nPath("site/schemas/input.json").read_text()\n')
    git(repo, "add", ".")
    assert "tests/test_schema_reader.py" in c.test_files("atlas-frontend", manifest, repo)




_COMPONENT_TOOL_PATHS = ['ci/components.json', 'ci/component_shadow.py', 'ci/components.py', 'ci/junit_results.py', 'ci/dependency_change_scope.py', 'ci/dependency_change_denominator.json', 'ci/pytest_report.py', 'ci/frontend_change_scope.py', 'ci/frontend_change_denominator.json', 'common/repo_root.py', 'common/jsonl.py', 'storage/test_baseline.py', 'ci/deploy_paths.py']


_COMPONENT_FIXTURE_BYTES = {'scripts/ci/components.json': b'{\n  "schema_version": 1,\n  "plan": "plans/component-separation-9721.md",\n  "plan_version": 2,\n  "baseline": "dff595c12a714713f7fb8f9f18ff5245283adf7e",\n  "description": "Index-backed path ownership and complete node test sets, including computed Python importers and shared integration tests. Computed unresolved edges force all-node selection and retain their test importers in every node set; no CI selection or independence certification.",\n  "components": {\n    "open-model-data": {\n      "lane": "open-model-data",\n      "test_files": [\n        "tests/test_open_model_view_exporter.py",\n        "tests/test_open_model_ready_view_production.py",\n        "tests/test_model_ready_receipt_audit.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [\n        "-m",\n        "not needs_artifact"\n      ],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_open_model_view_exporter.py",\n            "tests/test_open_model_ready_view_production.py",\n            "tests/test_model_ready_receipt_audit.py",\n            "-m",\n            "not needs_artifact"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.projects.open_model_data.model_view_exporter",\n            "continued-pretraining",\n            "--source-records",\n            "{member:source-records}",\n            "--payloads",\n            "{member:payloads}",\n            "--origin",\n            "human_authored",\n            "--representation-view",\n            "faithful_literary",\n            "--output",\n            "{output_dir}/view.jsonl",\n            "--receipt-output",\n            "{output_dir}/view-receipt.json"\n          ],\n          "cwd": ".",\n          "scope": "model-view"\n        }\n      ],\n      "input_families": [\n        "source-records",\n        "payloads",\n        "capability-policy"\n      ],\n      "output_families": [\n        "model-view",\n        "model-view-receipt"\n      ]\n    },\n    "atlas-data": {\n      "lane": "atlas",\n      "test_files": [\n        "tests/test_atlas_db.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_atlas_db.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.atlas.atlas_db",\n            "--manifest",\n            "{member:lexicon-manifest}",\n            "--db",\n            "{output_dir}/atlas.db",\n            "--curated-aliases",\n            "{member:curated-aliases}"\n          ],\n          "cwd": ".",\n          "scope": "atlas-db"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.audit.generate_search_index",\n            "--db",\n            "{output_dir}/atlas.db",\n            "--out",\n            "{output_dir}/search.json",\n            "--aliases-out",\n            "{output_dir}/aliases.json",\n            "--search-shards-out",\n            "{output_dir}/search-shards.json",\n            "--search-shard-dir",\n            "{output_dir}/search",\n            "--browse-meta-out",\n            "{output_dir}/browse-meta.json",\n            "--browse-flagged-out",\n            "{output_dir}/browse-flagged.json",\n            "--browse-dir",\n            "{output_dir}/browse"\n          ],\n          "cwd": ".",\n          "scope": "atlas-search"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.audit.generate_daily_pool",\n            "--db",\n            "{output_dir}/atlas.db",\n            "--out",\n            "{output_dir}/daily.json"\n          ],\n          "cwd": ".",\n          "scope": "atlas-daily"\n        }\n      ],\n      "input_families": [\n        "lexicon-manifest",\n        "curated-aliases",\n        "curriculum-vocabulary"\n      ],\n      "output_families": [\n        "atlas-db",\n        "atlas-search",\n        "atlas-daily",\n        "open-dataset"\n      ],\n      "preparation_commands": [\n        [\n          "npm",\n          "--prefix",\n          "site",\n          "run",\n          "hydrate:manifest"\n        ]\n      ]\n    },\n    "atlas-frontend": {\n      "lane": "atlas",\n      "test_files": [\n        "tests/test_frontend_change_scope.py",\n        "tests/test_deploy_pages_workflow.py",\n        "tests/test_auto_deploy_component_parity.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_frontend_change_scope.py",\n            "tests/test_deploy_pages_workflow.py",\n            "tests/test_auto_deploy_component_parity.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "npm",\n            "run",\n            "build:shell",\n            "--",\n            "--outDir",\n            "{output_dir}"\n          ],\n          "cwd": "site",\n          "scope": "shared-astro"\n        }\n      ],\n      "input_families": [\n        "frontend-runtime"\n      ],\n      "output_families": [\n        "shared-astro"\n      ],\n      "build_scope": "shared-astro"\n    },\n    "practice-frontend": {\n      "lane": "atlas",\n      "test_files": [\n        "tests/test_practice_deck_io.py",\n        "tests/test_makefile_practice_admit.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_practice_deck_io.py",\n            "tests/test_makefile_practice_admit.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "npm",\n            "run",\n            "build:shell",\n            "--",\n            "--outDir",\n            "{output_dir}"\n          ],\n          "cwd": "site",\n          "scope": "shared-astro"\n        }\n      ],\n      "input_families": [\n        "frontend-runtime",\n        "published-practice"\n      ],\n      "output_families": [\n        "shared-astro"\n      ],\n      "preparation_commands": [\n        [\n          "npm",\n          "--prefix",\n          "site",\n          "run",\n          "hydrate:practice"\n        ],\n        [\n          "npm",\n          "--prefix",\n          "site",\n          "run",\n          "hydrate:teacher"\n        ]\n      ],\n      "build_scope": "shared-astro"\n    },\n    "curriculum-generation": {\n      "lane": "curriculum",\n      "test_files": [\n        "tests/build/test_build_landing_pages.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/build/test_build_landing_pages.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.generate_curriculum_stats"\n          ],\n          "cwd": ".",\n          "scope": "canonical-worktree-output"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.build.build_arc_landing",\n            "a1",\n            "--write"\n          ],\n          "cwd": ".",\n          "scope": "canonical-worktree-output"\n        },\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "scripts.build.build_landing_pages"\n          ],\n          "cwd": ".",\n          "scope": "canonical-worktree-output"\n        }\n      ],\n      "input_families": [\n        "curriculum-sources"\n      ],\n      "output_families": [\n        "arc-json",\n        "lesson-mdx",\n        "landing-mdx",\n        "curriculum-stats"\n      ],\n      "preparation_commands": []\n    },\n    "curriculum-display": {\n      "lane": "curriculum",\n      "test_files": [\n        "tests/test_frontend_change_scope.py",\n        "tests/test_auto_deploy_component_parity.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/test_frontend_change_scope.py",\n            "tests/test_auto_deploy_component_parity.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [\n        {\n          "argv": [\n            "npm",\n            "run",\n            "build:shell",\n            "--",\n            "--outDir",\n            "{output_dir}"\n          ],\n          "cwd": "site",\n          "scope": "shared-astro"\n        }\n      ],\n      "input_families": [\n        "frontend-runtime",\n        "curriculum-display"\n      ],\n      "output_families": [\n        "shared-astro"\n      ],\n      "build_scope": "shared-astro"\n    },\n    "harness": {\n      "lane": "infra",\n      "test_files": [\n        "tests/ci/test_components.py",\n        "tests/ci/test_delivery_baseline.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/ci/test_components.py",\n            "tests/ci/test_delivery_baseline.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [],\n      "input_families": [],\n      "output_families": []\n    },\n    "shared-core": {\n      "lane": "infra",\n      "test_files": [\n        "tests/ci/test_components.py",\n        "tests/test_frontend_change_scope.py",\n        "tests/test_auto_deploy_component_parity.py"\n      ],\n      "test_prefixes": [],\n      "test_args": [],\n      "verify": [\n        {\n          "argv": [\n            "{python}",\n            "-m",\n            "pytest",\n            "-q",\n            "tests/ci/test_components.py",\n            "tests/test_frontend_change_scope.py",\n            "tests/test_auto_deploy_component_parity.py"\n          ],\n          "cwd": ".",\n          "scope": "code-contract"\n        }\n      ],\n      "build": [],\n      "input_families": [],\n      "output_families": []\n    }\n  },\n  "exact_paths": {\n    ".gitattributes": [\n      "shared-core"\n    ],\n    ".gitignore": [\n      "shared-core"\n    ],\n    ".gitleaks.toml": [\n      "shared-core"\n    ],\n    ".markdownlint.json": [\n      "shared-core"\n    ],\n    ".mcp.json": [\n      "shared-core"\n    ],\n    ".nvmrc": [\n      "shared-core"\n    ],\n    ".pre-commit-config.yaml": [\n      "shared-core"\n    ],\n    ".prettierrc": [\n      "shared-core"\n    ],\n    ".python-version": [\n      "shared-core"\n    ],\n    ".trufflehogignore": [\n      "shared-core"\n    ],\n    ".yamllint": [\n      "shared-core"\n    ],\n    "AGENTS.md": [\n      "shared-core"\n    ],\n    "CLAUDE.md": [\n      "shared-core"\n    ],\n    "CODE_OF_CONDUCT.md": [\n      "shared-core"\n    ],\n    "CONTRIBUTING.md": [\n      "shared-core"\n    ],\n    "GEMINI.md": [\n      "shared-core"\n    ],\n    "LICENSE": [\n      "shared-core"\n    ],\n    "LICENSE-CONTENT.md": [\n      "shared-core"\n    ],\n    "Makefile": [\n      "shared-core"\n    ],\n    "README.md": [\n      "shared-core"\n    ],\n    "RESIDUAL.md": [\n      "shared-core"\n    ],\n    "SECURITY.md": [\n      "shared-core"\n    ],\n    "UI-REVIEW-FINDINGS.md": [\n      "shared-core"\n    ],\n    "eslint.config.mjs": [\n      "shared-core"\n    ],\n    "package-lock.json": [\n      "shared-core"\n    ],\n    "package.json": [\n      "shared-core"\n    ],\n    "pyproject.toml": [\n      "shared-core"\n    ],\n    "pyrightconfig.json": [\n      "shared-core"\n    ],\n    "requirements-dev.txt": [\n      "shared-core"\n    ],\n    "requirements-lock.txt": [\n      "shared-core"\n    ],\n    "requirements.txt": [\n      "shared-core"\n    ],\n    "run-dispatcher.sh": [\n      "harness"\n    ],\n    "scripts/__init__.py": [\n      "shared-core"\n    ],\n    "scripts/audit/daily_cefr.py": [\n      "atlas-data"\n    ],\n    "scripts/audit/generate_daily_pool.py": [\n      "atlas-data"\n    ],\n    "scripts/audit/generate_search_index.py": [\n      "atlas-data"\n    ],\n    "scripts/audit/lexeme_filter.py": [\n      "atlas-data"\n    ],\n    "scripts/build/generate_lesson_schema.py": [\n      "shared-core"\n    ],\n    "scripts/ci/components.json": [\n      "shared-core"\n    ],\n    "scripts/ci/components.py": [\n      "shared-core"\n    ],\n    "scripts/config.py": [\n      "shared-core"\n    ],\n    "scripts/generate_curriculum_stats.py": [\n      "curriculum-generation"\n    ],\n    "scripts/practice/euphony_stem_engine.py": [\n      "shared-core"\n    ],\n    "services.sh": [\n      "harness"\n    ],\n    "site/scripts/generate-teacher-lesson-keys.ts": [\n      "practice-frontend"\n    ],\n    "site/scripts/generate-zno-deck-meta.ts": [\n      "practice-frontend"\n    ],\n    "site/scripts/hydrate-practice-deck.mjs": [\n      "practice-frontend"\n    ],\n    "site/scripts/hydrate-teacher-deck.mjs": [\n      "practice-frontend"\n    ],\n    "site/src/components/ZnoPractice.tsx": [\n      "practice-frontend"\n    ],\n    "site/src/content.config.ts": [\n      "curriculum-display"\n    ],\n    "site/src/data/lexicon-manifest.pointer.json": [\n      "atlas-data"\n    ],\n    "site/src/data/lexicon-practice-deck.pointer.json": [\n      "practice-frontend"\n    ],\n    "site/src/lib/lexicon/custom-decks.ts": [\n      "practice-frontend"\n    ],\n    "site/src/pages/[...slug].astro": [\n      "curriculum-display"\n    ],\n    "start-claude-driver.sh": [\n      "harness"\n    ],\n    "start-claude.sh": [\n      "harness"\n    ],\n    "start-codex-driver.sh": [\n      "harness"\n    ],\n    "start-codex.sh": [\n      "harness"\n    ],\n    "start-gemini-driver.sh": [\n      "harness"\n    ],\n    "start-gemini.sh": [\n      "harness"\n    ],\n    "tests/build/test_build_landing_pages.py": [\n      "curriculum-generation"\n    ],\n    "tests/ci/test_components.py": [\n      "shared-core"\n    ],\n    "tests/ci/test_delivery_baseline.py": [\n      "harness"\n    ],\n    "tests/conftest.py": [\n      "shared-core"\n    ],\n    "tests/test_atlas_db.py": [\n      "atlas-data"\n    ],\n    "tests/test_auto_deploy_component_parity.py": [\n      "atlas-frontend",\n      "curriculum-display",\n      "shared-core"\n    ],\n    "tests/test_deploy_pages_workflow.py": [\n      "atlas-frontend"\n    ],\n    "tests/test_frontend_change_scope.py": [\n      "atlas-frontend",\n      "curriculum-display",\n      "shared-core"\n    ],\n    "tests/test_makefile_practice_admit.py": [\n      "practice-frontend"\n    ],\n    "tests/test_model_ready_receipt_audit.py": [\n      "open-model-data"\n    ],\n    "tests/test_open_model_ready_view_production.py": [\n      "open-model-data"\n    ],\n    "tests/test_open_model_view_exporter.py": [\n      "open-model-data"\n    ],\n    "tests/test_practice_deck_io.py": [\n      "practice-frontend"\n    ],\n    "tsconfig.json": [\n      "shared-core"\n    ],\n    "zizmor.yml": [\n      "shared-core"\n    ]\n  },\n  "path_prefixes": [\n    {\n      "path": ".agent/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".cursor/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".gemini/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".githooks/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": ".github/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": ".mcp/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "agents_extensions/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "archive/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "assets/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "audit/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "batch_state/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "benchmarks/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "bin/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "curriculum/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "dashboards/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "data/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "data/lexicon-dataset/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "data/lexicon/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "data/projects/open_model_data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "docs/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "docs/projects/open-model-data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "eval/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "migrations/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "openwiki/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "orchestration/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "packages/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "packages/activity-kit/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend",\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "packaging/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "plans/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "pocketbase/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "registry/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "registry/lexicon/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "registry/practice/",\n      "components": [\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "registry/projects/open_model_data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "review/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "schemas/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "scripts/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "scripts/agent_runtime/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/api/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/atlas/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/build/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "scripts/curriculum/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "scripts/etymology/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/fleet/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/fleet_comms/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/generate_mdx/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "scripts/lexicon/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/open_dataset/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "scripts/orchestration/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/practice/",\n      "components": [\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "scripts/practice_deck/",\n      "components": [\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "scripts/project_api/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "scripts/projects/open_model_data/",\n      "components": [\n        "open-model-data"\n      ]\n    },\n    {\n      "path": "scripts/review/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "site/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend",\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "site/src/content/",\n      "components": [\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "site/src/lib/lexicon/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "site/src/pages/l2-uk-en/",\n      "components": [\n        "curriculum-display"\n      ]\n    },\n    {\n      "path": "site/src/pages/lexicon/",\n      "components": [\n        "atlas-frontend",\n        "practice-frontend"\n      ]\n    },\n    {\n      "path": "status/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "tests/projects/open_model_data/",\n      "components": [\n        "open-model-data",\n        "shared-core"\n      ]\n    },\n    {\n      "path": "tests/",\n      "components": [\n        "shared-core"\n      ]\n    },\n    {\n      "path": "tests/agent_runtime/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "tests/api/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "tests/build/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "tests/ci/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "tests/curriculum/",\n      "components": [\n        "curriculum-generation"\n      ]\n    },\n    {\n      "path": "tests/fixtures/atlas/",\n      "components": [\n        "atlas-data"\n      ]\n    },\n    {\n      "path": "tests/orchestration/",\n      "components": [\n        "harness"\n      ]\n    },\n    {\n      "path": "wiki/",\n      "components": [\n        "shared-core"\n      ]\n    }\n  ],\n  "edges": [\n    {\n      "id": "curriculum-vocabulary",\n      "producer": "curriculum-generation",\n      "consumer": "atlas-data",\n      "kind": "artifact",\n      "anchors": [\n        "Makefile:62",\n        "scripts/lexicon/build_data_manifest.py:234"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "atlas-links-curriculum-generation",\n      "producer": "atlas-data",\n      "consumer": "curriculum-generation",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/generate_mdx/atlas_links.py:75",\n        "site/src/pages/[...slug].astro:14"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "atlas-links-curriculum-display",\n      "producer": "atlas-data",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/generate_mdx/atlas_links.py:75",\n        "site/src/pages/[...slug].astro:14"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "curriculum-evidence-import",\n      "producer": "curriculum-generation",\n      "consumer": "atlas-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/atlas/word_card_foundation.py:23"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "lexicon-heritage-import",\n      "producer": "atlas-data",\n      "consumer": "curriculum-generation",\n      "kind": "import",\n      "anchors": [\n        "scripts/curriculum/evidence/sources.py:32",\n        "scripts/build/linear_pipeline.py:11761"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-euphony-curriculum-generation",\n      "producer": "shared-core",\n      "consumer": "curriculum-generation",\n      "kind": "import",\n      "anchors": [\n        "scripts/curriculum/validate/mechanical.py:44",\n        "scripts/practice/euphony_stem_engine.py"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-euphony-practice-frontend",\n      "producer": "shared-core",\n      "consumer": "practice-frontend",\n      "kind": "import",\n      "anchors": [\n        "scripts/curriculum/validate/mechanical.py:44",\n        "scripts/practice/euphony_stem_engine.py"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-model-import-atlas-data",\n      "producer": "atlas-data",\n      "consumer": "open-model-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/projects/open_model_data/language_contact_detector.py:39"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-model-import-shared-core",\n      "producer": "shared-core",\n      "consumer": "open-model-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/projects/open_model_data/build_decolonization_cases.py:52"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-model-import-harness",\n      "producer": "harness",\n      "consumer": "open-model-data",\n      "kind": "import",\n      "anchors": [\n        "scripts/projects/open_model_data/phase3_cycle007_evidence_compiler.py:44"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "open-dataset-publication",\n      "producer": "atlas-data",\n      "consumer": "atlas-data",\n      "kind": "artifact",\n      "anchors": [\n        "Makefile:65",\n        "Makefile:85",\n        "scripts/lexicon/export_open_dataset.py:184"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "dynamic-atlas-links",\n      "producer": "atlas-data",\n      "consumer": "curriculum-generation",\n      "kind": "dynamic",\n      "anchors": [\n        "scripts/audit/generate_search_index.py:88",\n        "scripts/audit/generate_search_index.py:96",\n        "scripts/audit/generate_search_index.py:102",\n        "scripts/generate_mdx/atlas_links.py:75"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-open-model-data",\n      "producer": "shared-core",\n      "consumer": "open-model-data",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-atlas-data",\n      "producer": "shared-core",\n      "consumer": "atlas-data",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-atlas-frontend",\n      "producer": "shared-core",\n      "consumer": "atlas-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-practice-frontend",\n      "producer": "shared-core",\n      "consumer": "practice-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-curriculum-generation",\n      "producer": "shared-core",\n      "consumer": "curriculum-generation",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-curriculum-display",\n      "producer": "shared-core",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "shared-storage-harness",\n      "producer": "shared-core",\n      "consumer": "harness",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/storage/artifacts.py:251",\n        "scripts/storage/paths.py:105"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-atlas-data-atlas-frontend",\n      "producer": "atlas-data",\n      "consumer": "atlas-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-practice-frontend-atlas-frontend",\n      "producer": "practice-frontend",\n      "consumer": "atlas-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-atlas-data-practice-frontend",\n      "producer": "atlas-data",\n      "consumer": "practice-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-practice-frontend-practice-frontend",\n      "producer": "practice-frontend",\n      "consumer": "practice-frontend",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-atlas-data-curriculum-display",\n      "producer": "atlas-data",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "runtime-practice-frontend-curriculum-display",\n      "producer": "practice-frontend",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "site/package.json:11",\n        "site/scripts/verify-consumed-artifacts.mjs:12",\n        "site/scripts/verify-consumed-artifacts.mjs:80"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "curriculum-display",\n      "producer": "curriculum-generation",\n      "consumer": "curriculum-display",\n      "kind": "artifact",\n      "anchors": [\n        "site/src/pages/[...slug].astro:13",\n        "site/src/content.config.ts:12"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "frontend-props-atlas-frontend",\n      "producer": "atlas-frontend",\n      "consumer": "curriculum-generation",\n      "kind": "schema",\n      "anchors": [\n        "scripts/build/generate_lesson_schema.py:19",\n        "scripts/ci/checks.sh:69"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "frontend-props-practice-frontend",\n      "producer": "practice-frontend",\n      "consumer": "curriculum-generation",\n      "kind": "schema",\n      "anchors": [\n        "scripts/build/generate_lesson_schema.py:19",\n        "scripts/ci/checks.sh:69"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "frontend-props-curriculum-display",\n      "producer": "curriculum-display",\n      "consumer": "curriculum-generation",\n      "kind": "schema",\n      "anchors": [\n        "scripts/build/generate_lesson_schema.py:19",\n        "scripts/ci/checks.sh:69"\n      ],\n      "resolved": true\n    },\n    {\n      "id": "production-receipt-audit",\n      "producer": "open-model-data",\n      "consumer": "open-model-data",\n      "kind": "artifact",\n      "anchors": [\n        "scripts/projects/open_model_data/audit_model_ready_receipts.py:64",\n        "scripts/projects/open_model_data/audit_model_ready_receipts.py:132"\n      ],\n      "resolved": true\n    }\n  ],\n  "residual_commands": [\n    {\n      "id": "legacy-atlas-make",\n      "component": "atlas-data",\n      "status": "expected-fail",\n      "owner_slice": 3,\n      "argv": [\n        "make",\n        "atlas"\n      ],\n      "reason": "Legacy recipe invokes export_open_dataset without --write; DB-backed interface bypasses it."\n    },\n    {\n      "id": "non-arc-landing",\n      "component": "curriculum-generation",\n      "status": "expected-fail",\n      "owner_slice": 4,\n      "argv": [\n        "{python}",\n        "-m",\n        "scripts.build.build_landing_pages",\n        "--check"\n      ],\n      "reason": "Owner must regenerate stale non-arc landing output before freshness certification."\n    },\n    {\n      "id": "open-model-data-tier",\n      "component": "open-model-data",\n      "status": "artifact-dependent",\n      "owner_slice": 5,\n      "argv": [\n        "{python}",\n        "-m",\n        "pytest",\n        "tests/test_model_ready_receipt_audit.py",\n        "-m",\n        "needs_artifact"\n      ],\n      "reason": "Existing data-tier executes marked artifact tests; absence is never passing evidence."\n    },\n    {\n      "id": "complete-runtime-atlas-frontend",\n      "component": "atlas-frontend",\n      "status": "artifact-dependent",\n      "owner_slice": 3,\n      "argv": [\n        "node",\n        "site/scripts/verify-consumed-artifacts.mjs"\n      ],\n      "reason": "Prepared runtime members required; complete readiness remains slice 3, then learner certification slice 8."\n    },\n    {\n      "id": "complete-runtime-practice-frontend",\n      "component": "practice-frontend",\n      "status": "artifact-dependent",\n      "owner_slice": 3,\n      "argv": [\n        "node",\n        "site/scripts/verify-consumed-artifacts.mjs"\n      ],\n      "reason": "Prepared runtime members required; complete readiness remains slice 3, then learner certification slice 8."\n    },\n    {\n      "id": "complete-runtime-curriculum-display",\n      "component": "curriculum-display",\n      "status": "artifact-dependent",\n      "owner_slice": 3,\n      "argv": [\n        "node",\n        "site/scripts/verify-consumed-artifacts.mjs"\n      ],\n      "reason": "Prepared runtime members required; complete readiness remains slice 3, then learner certification slice 8."\n    },\n    {\n      "id": "open-model-prepared-build-inputs",\n      "component": "open-model-data",\n      "status": "artifact-dependent",\n      "owner_slice": 5,\n      "argv": [\n        "{python}",\n        "-m",\n        "scripts.projects.open_model_data.model_view_exporter",\n        "continued-pretraining",\n        "--source-records",\n        "{member:source-records}",\n        "--payloads",\n        "{member:payloads}",\n        "--origin",\n        "human_authored",\n        "--representation-view",\n        "faithful_literary",\n        "--output",\n        "{output_dir}/view.jsonl",\n        "--receipt-output",\n        "{output_dir}/view-receipt.json"\n      ],\n      "reason": "The production build requires admitted human-authored source records and payloads. Structural test fixtures are rejected; they do not certify a production view."\n    }\n  ],\n  "selector_contracts": {\n    "frontend_denominator": "scripts/ci/frontend_change_denominator.json",\n    "pages_eligibility": "scripts/deploy/auto_deploy_eligibility.py",\n    "frontend_components": [\n      "atlas-frontend",\n      "practice-frontend",\n      "curriculum-display"\n    ]\n  },\n  "input_wrapper_contract": {\n    "schema": "component-inputs.v1",\n    "component": "one of the eight node IDs",\n    "identities": "Exact inventory --component ID --identities identities object",\n    "families": "Every required input family, with name, existing schema identifier, version and members",\n    "member": "Each member has unique logical name, repository-relative path and sha256; existing schemas are not replaced",\n    "verification_scope": "Digest and freshness wrapper. Product semantic readiness remains owned by slices 3-5 and held-out certification by slice 8."\n  },\n  "shared_integration_tests": [\n    "tests/ci/test_components.py",\n    "tests/test_frontend_change_scope.py",\n    "tests/test_auto_deploy_component_parity.py"\n  ],\n  "vitest": {\n    "assignment": "Configured Vitest file directories intersected with exact/longest-prefix ownership; broad site/ ownership assigns shared files to all three frontend nodes.",\n    "built_output_files": [\n      "site/tests/unit/build-renders.test.ts",\n      "site/tests/unit/etymology-handler-built-output.test.ts"\n    ]\n  }\n}\n', 'scripts/curriculum/deploy_paths.v1.json': b'{\n  "schema": "learn-ukrainian.deploy-paths",\n  "schema_version": 1,\n  "content_path_prefixes": [\n    "curriculum/",\n    "data/",\n    "registry/",\n    "site/src/content/",\n    "site/src/data/",\n    "site/src/lib/lexicon/curated-heteronyms.ts"\n  ],\n  "site_code_path_exceptions": [\n    "site/src/data/lexicon-practice-deck.pointer.json",\n    "site/src/data/lexicon-manifest.pointer.json"\n  ],\n  "site_code_path_prefixes": [\n    "site/",\n    "packages/activity-kit/"\n  ]\n}\n'}


@pytest.fixture
def component_fixture_paths(configured_roots, monkeypatch):
    root = configured_roots.primary
    import shutil
    import subprocess
    shutil.copytree(configured_roots.product, root, dirs_exist_ok=True)
    monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
    package = Path(c.__file__).resolve().parents[1]
    for relative in _COMPONENT_TOOL_PATHS:
        for prefix in ("scripts", "lu_machinery"):
            target = root / prefix / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((package / relative).read_bytes())
    for name, value in _COMPONENT_FIXTURE_BYTES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "component inputs"], check=True, capture_output=True, timeout=30)
    monkeypatch.setattr(c, "ROOT", root, raising=False)
