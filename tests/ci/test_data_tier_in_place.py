"""The nightly data tier reads the host's own data in place, read-only, and the tested checkout's selection."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.ci import data_tier
from lu_machinery.common import scratch
from tests.helpers.product_tree import configured_roots as configured_roots


def test_selection_and_known_failures_come_from_the_tested_checkout_without_fallback(tmp_path):
    """Defect class: the runner tests a packaged, stale selection instead of the checkout's, or falls back silently."""
    with pytest.raises(data_tier.DataTierError, match=r"tested checkout has no scripts/ci/data_tier_selection\.json"):
        data_tier.load_selection(tmp_path)
    with pytest.raises(
        data_tier.DataTierError, match=r"tested checkout has no scripts/ci/data_tier_known_failures\.json"
    ):
        data_tier.load_known_failures(tmp_path)

    ci = tmp_path / "scripts" / "ci"
    ci.mkdir(parents=True)
    nodeid = "tests/test_example.py::test_reads_store"
    (ci / "data_tier_selection.json").write_text(
        json.dumps({"schema": 1, "class_c_merge_group": 1, "nodeids": [nodeid], "files": ["tests/test_example.py"]}),
        encoding="utf-8",
    )
    (ci / "data_tier_known_failures.json").write_text(json.dumps({"known_failures": {nodeid: 7}}), encoding="utf-8")
    example = tmp_path / "tests" / "test_example.py"
    example.parent.mkdir()
    example.write_text("def test_reads_store():\n    return None\n", encoding="utf-8")

    selection, digest = data_tier.load_selection(tmp_path)
    assert selection["nodeids"] == [nodeid]
    assert data_tier.load_selection(tmp_path, digest)[0] == selection
    assert data_tier.load_known_failures(tmp_path) == {nodeid: 7}
    with pytest.raises(data_tier.DataTierError, match="changed after the runner validated it"):
        data_tier.load_selection(tmp_path, "0" * 64)
    (ci / "data_tier_selection.json").write_text(json.dumps({"schema": 2}), encoding="utf-8")
    with pytest.raises(data_tier.DataTierError, match="schema-1 field"):
        data_tier.load_selection(tmp_path)


def _write_selection(checkout: Path, payload: dict, source: str) -> None:
    ci = checkout / "scripts" / "ci"
    ci.mkdir(parents=True, exist_ok=True)
    (ci / "data_tier_selection.json").write_text(json.dumps(payload), encoding="utf-8")
    example = checkout / "tests" / "test_example.py"
    example.parent.mkdir(parents=True, exist_ok=True)
    example.write_text(source, encoding="utf-8")


def test_selection_audit_reads_definitions_from_the_product_tree(tmp_path: Path) -> None:
    """Defect class: the audit trusts a stored class_c count, so a removed definition or a stale count drifts silently."""
    nodeid = "tests/test_example.py::test_reads_store"
    source = "def test_reads_store():\n    return None\n"
    stale = {"schema": 1, "class_c_merge_group": 127, "nodeids": [nodeid], "files": ["tests/test_example.py"]}
    _write_selection(tmp_path, stale, source)
    selection, _digest = data_tier.load_selection(tmp_path)
    assert selection["nodeids"] == [nodeid]
    assert selection["class_c_merge_group"] == 127

    _write_selection(
        tmp_path,
        {**stale, "nodeids": [nodeid, nodeid]},
        source,
    )
    with pytest.raises(data_tier.DataTierError, match="count or uniqueness differs"):
        data_tier.load_selection(tmp_path)

    _write_selection(tmp_path, stale, "def test_other():\n    return None\n")
    with pytest.raises(data_tier.DataTierError, match="absent from the product tree"):
        data_tier.load_selection(tmp_path)


def test_selection_audit_rejects_a_link_outside_the_checkout(tmp_path: Path) -> None:
    """Defect class: a tests/ path that resolves through a file or directory link outside the checkout."""
    outside = tmp_path / "outside.py"
    outside.write_text("def test_reads_store():\n    return None\n", encoding="utf-8")
    checkout = tmp_path / "product"
    nodeid = "tests/test_example.py::test_reads_store"
    payload = {"schema": 1, "class_c_merge_group": 1, "nodeids": [nodeid], "files": ["tests/test_example.py"]}
    ci = checkout / "scripts" / "ci"
    ci.mkdir(parents=True)
    (ci / "data_tier_selection.json").write_text(json.dumps(payload), encoding="utf-8")
    linked = checkout / "tests" / "test_example.py"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    with pytest.raises(data_tier.DataTierError, match="resolves outside the tested checkout"):
        data_tier.load_selection(checkout)

    linked.unlink()
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "test_example.py").write_text("def test_reads_store():\n    return None\n", encoding="utf-8")
    nested = checkout / "tests" / "linked"
    nested.symlink_to(outside_dir, target_is_directory=True)
    nested_id = "tests/linked/test_example.py::test_reads_store"
    (ci / "data_tier_selection.json").write_text(
        json.dumps({**payload, "nodeids": [nested_id], "files": ["tests/linked/test_example.py"]}),
        encoding="utf-8",
    )
    with pytest.raises(data_tier.DataTierError, match="resolves outside the tested checkout"):
        data_tier.load_selection(checkout)


WRITER = """
import json, os, sqlite3, sys
repo, store, exposed, existing, output, atlas, host, git, scratch, *read_only = sys.argv[1:]
sys.path.insert(0, repo)
from lu_machinery.ci import data_tier

results = {}
attempts = (
    ("store", store, "x"),
    ("exposed", exposed, "x"),
    ("existing", existing, "a"),
    ("host", host, "x"),
    ("git", git, "a"),
    ("output", output, "x"),
    ("scratch", scratch, "x"),
)
for name, path, mode in attempts:
    try:
        with open(path, mode) as stream:
            stream.write("written")
        results[name] = "written"
    except OSError as error:
        results[name] = error.strerror
results["read"] = open(existing).read()
data_tier.require_read_only(read_only)
results["hidden_roots"] = {root: os.listdir(root) for root in ("/run", "/tmp")}
connection = sqlite3.connect(f"file:{atlas}?mode=ro", uri=True)
results["atlas_rows"] = connection.execute("select count(*) from lemma").fetchone()[0]
try:
    connection.execute("insert into lemma values ('written')")
    results["atlas_write"] = "written"
except sqlite3.OperationalError as error:
    results["atlas_write"] = str(error)
print(json.dumps(results))
"""


def test_store_and_exposed_data_writes_are_denied_without_copies(tmp_path):
    """Defect class: tests write host stores or data, directly, through a host service or via state the host reads later."""
    primary, checkout, outputs = tmp_path / "primary", tmp_path / "checkout", tmp_path / "outputs"
    (primary / "data" / "ua-gec").mkdir(parents=True)
    (primary / "data" / "vesum.db").write_text("store", encoding="utf-8")
    (primary / "data" / "ua-gec" / "corpus.txt").write_text("host data", encoding="utf-8")
    (checkout / "data").mkdir(parents=True)
    (checkout / "data" / "tracked.txt").write_text("tracked", encoding="utf-8")
    (checkout / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    outputs.mkdir()
    # A live WAL Atlas whose writer holds it open, so its sidecars exist beside the host file.
    writer = sqlite3.connect(primary / "data" / "atlas.db")
    writer.execute("pragma journal_mode=wal")
    writer.execute("create table lemma (word text)")
    writer.execute("insert into lemma values ('слово')")
    writer.commit()

    mounts = data_tier.exposed_data(primary, checkout)
    assert mounts == [(primary / "data" / "ua-gec", checkout / "data" / "ua-gec")]
    data_tier.link_atlas(primary, checkout)
    assert (checkout / "data" / "atlas.db").resolve() == (primary / "data" / "atlas.db").resolve()
    with pytest.raises(data_tier.DataTierError, match="writable inside the test namespace"):
        data_tier.require_read_only([str(outputs)])

    read_only = [primary / "data", checkout / ".git"]
    argv = data_tier.read_only_namespace(read_only, mounts, [checkout, outputs])
    try:
        result = subprocess.run(
            [
                *argv,
                sys.executable,
                "-I",
                "-c",
                WRITER,
                str(Path(data_tier.__file__).parents[2]),
                str(primary / "data" / "new.db"),
                str(checkout / "data" / "ua-gec" / "new.txt"),
                str(checkout / "data" / "ua-gec" / "corpus.txt"),
                str(outputs / "junit.xml"),
                str(checkout / "data" / "atlas.db"),
                str(tmp_path / "host.txt"),
                str(checkout / ".git"),
                str(checkout / "scratch.txt"),
                "/",
                *map(str, read_only),
                str(checkout / "data" / "ua-gec"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        writer.close()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "store": "Read-only file system",
        "exposed": "Read-only file system",
        "existing": "Read-only file system",
        "host": "Read-only file system",
        "git": "Read-only file system",
        "output": "written",
        "scratch": "written",
        "read": "host data",
        "hidden_roots": {"/run": [], "/tmp": []},
        "atlas_rows": 1,
        "atlas_write": "attempt to write a readonly database",
    }
    assert (primary / "data" / "ua-gec" / "corpus.txt").read_text(encoding="utf-8") == "host data"
    assert not (primary / "data" / "new.db").exists()
    assert not (tmp_path / "host.txt").exists()
    assert (checkout / ".git").read_text(encoding="utf-8") == "gitdir: elsewhere\n"
    # Only an empty mount point and the Atlas link are left in the checkout: nothing was copied.
    assert list((checkout / "data" / "ua-gec").iterdir()) == []
    assert sorted(path.name for path in (checkout / "data").iterdir()) == ["atlas.db", "tracked.txt", "ua-gec"]
    assert (outputs / "junit.xml").read_text(encoding="utf-8") == "written"


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_junit_summary_and_known_citation_issue(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite><testcase classname="tests.test_citation_resolution_invariant" '
        'name="test_published_citations_resolve_invariant[wiki/grammar/b2/academic-writing.md]"><failure message="drift"/></testcase>'
        f'<testcase classname="tests.test_vesum" name="test_db"><skipped message="missing {Path.home()}/db"/></testcase>'
        '<testcase classname="tests.test_vesum" name="test_ok"/></testsuite>',
        encoding="utf-8",
    )
    raw_junit = junit.read_bytes()
    junit.unlink()
    data_tier.sanitize_junit(raw_junit, junit)
    summary = data_tier.junit_summary(junit)
    assert (summary["ran"], summary["passed"], summary["failed"], summary["skipped"]) == (3, 1, 1, 1)
    assert summary["skip_reasons"] == {"missing <host-path>": 1}
    assert (
        data_tier.known_issue(
            summary["failing_tests"][0],
            {
                "tests/test_citation_resolution_invariant.py::test_published_citations_resolve_invariant[wiki/grammar/b2/academic-writing.md]": 8403,
            },
        )
        == 8403
    )


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_known_issue_maps_whole_test_function_and_exact_ids() -> None:
    whole = "tests/test_citation_resolution_invariant.py::test_published_citations_resolve_invariant"
    exact = "tests/test_x.py::test_y[case-a]"
    baseline = {whole: 8403, exact: 11, "tests/test_x.py::test_y": 12}
    assert data_tier.known_issue(f"{whole}[wiki/grammar/b2/academic-writing.md]", baseline) == 8403
    assert data_tier.known_issue(f"{whole}[wiki/grammar/b1/aspect.md]", baseline) == 8403
    assert data_tier.known_issue(whole, baseline) == 8403
    assert data_tier.known_issue(exact, baseline) == 11
    assert data_tier.known_issue("tests/test_x.py::test_y[case-b]", baseline) == 12
    assert data_tier.known_issue(f"{whole}_other[wiki/a.md]", baseline) is None
    assert data_tier.known_issue("tests/test_x.py::test_z[case-a]", baseline) is None
    assert data_tier.known_issue("tests/test_citation_resolution_invariant.py::test_other", baseline) is None


@pytest.fixture()
def _no_live_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_roots) -> None:
    """The data-tier memory checks never read this host's real lu.slice (#9975)."""
    monkeypatch.setenv("LU_SLICE_CGROUP", str(tmp_path / "absent-pool" / "lu.slice"))
    monkeypatch.setattr(data_tier, "state_root", lambda: configured_roots.primary)


def _fake_slice(directory: Path, *, current: int, file_cache: int, high: str = "max") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "memory.current").write_text(f"{current}\n", encoding="ascii")
    (directory / "memory.high").write_text(f"{high}\n", encoding="ascii")
    (directory / "memory.max").write_text("max\n", encoding="ascii")
    (directory / "memory.stat").write_text(f"active_file 0\ninactive_file {file_cache}\n", encoding="ascii")
    return directory


def _slice_show(monkeypatch: pytest.MonkeyPatch, *, current: int, maximum: int, control_group: str = "") -> None:
    monkeypatch.setattr(data_tier, "available_memory", lambda: 10 * 1024**3)
    monkeypatch.setattr(
        data_tier,
        "command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                f"LoadState=loaded\nActiveState=active\nMemoryCurrent={current}\nMemoryMax={maximum}\n"
                f"ControlGroup={control_group}\n"
            )
        ),
    )


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_dispatch_slice_headroom_excludes_file_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gib = 1024**3
    monkeypatch.setattr(data_tier.pool_headroom, "CGROUP_ROOT", tmp_path / "cgroup")
    _fake_slice(tmp_path / "cgroup" / "lu.slice" / "lu-dispatch.slice", current=14 * gib, file_cache=12 * gib)
    _slice_show(monkeypatch, current=14 * gib, maximum=17 * gib, control_group="/lu.slice/lu-dispatch.slice")

    data_tier.require_memory()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_dispatch_slice_without_memory_stat_uses_raw_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gib = 1024**3
    monkeypatch.setattr(data_tier.pool_headroom, "CGROUP_ROOT", tmp_path / "cgroup")
    _slice_show(monkeypatch, current=14 * gib, maximum=17 * gib, control_group="/lu.slice/lu-dispatch.slice")

    with pytest.raises(data_tier.DataTierError, match="less than 4 GiB headroom"):
        data_tier.require_memory()
    assert "using raw MemoryCurrent" in capsys.readouterr().err


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_full_shared_pool_is_a_stop_condition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gib = 1024**3
    pool = _fake_slice(tmp_path / "lu.slice", current=28 * gib, file_cache=gib, high=str(28 * gib))
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))
    _slice_show(monkeypatch, current=gib, maximum=17 * gib)

    with pytest.raises(data_tier.DataTierError, match=r"lu\.slice non-cache use 27\.0 GiB plus a 4 GiB"):
        data_tier.require_memory()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_shared_pool_with_cache_only_pressure_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gib = 1024**3
    pool = _fake_slice(tmp_path / "lu.slice", current=28 * gib, file_cache=14 * gib, high=str(28 * gib))
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))
    _slice_show(monkeypatch, current=gib, maximum=17 * gib)

    data_tier.require_memory()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_missing_pool_cgroup_skips_and_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _slice_show(monkeypatch, current=1024, maximum=20 * 1024**3)

    data_tier.require_memory()

    err = capsys.readouterr().err
    assert "lu.slice pool check skipped" in err
    assert "/" not in err.split("skipped", 1)[1]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_memory_floor_is_a_stop_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_tier, "available_memory", lambda: 6 * 1024**3 - 1)
    with pytest.raises(data_tier.DataTierError, match="below the 6 GiB floor"):
        data_tier.require_memory()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_selection_filters_exact_ids_before_xdist(tmp_path: Path) -> None:
    ordinary = "tests/test_sample.py::test_data"
    private = "tests/test_sample.py::test_bulk[private fixture]"
    citation = "tests/test_citation_resolution_invariant.py::test_one[wiki/a1/example.md]"
    selection = {
        "nodeids": [ordinary, data_tier._key(private), "tests/test_citation_resolution_invariant.py"],
        "bulk_nodeids": [data_tier._key(private)],
    }

    class Item:
        def __init__(self, nodeid: str, live: bool = False) -> None:
            self.nodeid = nodeid
            self.live = live

        def get_closest_marker(self, marker: str) -> object | None:
            return object() if self.live and marker == "live_network" else None

    deselected = []
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items)))
    plugin = data_tier.SelectionPlugin(selection, None, "mount absent", tmp_path / "collected.json")
    items = [Item(ordinary), Item(private), Item(citation), Item("tests/test_sample.py::test_live", live=True)]
    plugin.pytest_collection_modifyitems(items, config)

    assert plugin.selected_nodeids == [ordinary, citation]
    assert plugin.bulk_skipped == [private]
    assert [item.nodeid for item in items] == [ordinary, citation]
    assert len(deselected) == 2
    assert json.loads((tmp_path / "collected.json").read_text()) == [ordinary, citation]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.usefixtures("validated_child_namespace")
def test_child_passes_only_selected_ids_to_two_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = "tests/test_sample.py::test_data"
    ignored = "tests/test_sample.py::test_unrelated"
    monkeypatch.setattr(
        data_tier,
        "load_selection",
        lambda *_args, **_kwargs: ({'nodeids': [selected], 'files': ['tests/test_sample.py'], 'bulk_nodeids': []}, "0" * 64),
    )

    class Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

        def get_closest_marker(self, _marker: str) -> None:
            return None

    def collect(_argv: list[str], *, plugins: list[data_tier.SelectionPlugin]) -> int:
        items = [Item(selected), Item(ignored)]
        config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: None))
        plugins[0].pytest_collection_modifyitems(items, config)
        return 0

    calls = []

    def execute(argv: list[str], *, timeout: int) -> int:
        calls.append(argv)
        assert timeout == 21600
        junit.write_text('<testsuite><testcase classname="tests.test_sample" name="test_data"/></testsuite>')
        return 0

    monkeypatch.setattr(pytest, "main", collect)
    monkeypatch.setattr(data_tier, "run_process_group", execute)
    junit = tmp_path / "junit.xml"
    args = SimpleNamespace(read_only=[], selection_sha256="0" * 64, junit=str(junit), collected=str(tmp_path / "collected.json"), only=None, bulk_reason=None)
    assert data_tier.pytest_child(args) == 0
    assert len(calls) == 1
    assert calls[0][-1] == selected
    assert ignored not in calls[0]
    assert calls[0][calls[0].index("-n") + 1] == "2"
    assert "--require-data" in calls[0]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.usefixtures("validated_child_namespace")
def test_collection_time_data_skip_is_in_junit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = "tests/test_citation_resolution_invariant.py"
    monkeypatch.setattr(data_tier, "load_selection", lambda *_args, **_kwargs: ({'nodeids': [module], 'files': [module]}, "0" * 64))

    def collect(_argv: list[str], *, plugins: list[data_tier.SelectionPlugin]) -> int:
        report = SimpleNamespace(failed=False, skipped=True, nodeid=module, longrepr=(module, 1, "sources.db absent"))
        plugins[0].pytest_collectreport(report)
        return 5

    monkeypatch.setattr(pytest, "main", collect)
    junit = tmp_path / "junit.xml"
    args = SimpleNamespace(read_only=[], selection_sha256="0" * 64, junit=str(junit), collected=str(tmp_path / "collected.json"), only=None, bulk_reason=None)
    assert data_tier.pytest_child(args) == 0
    assert data_tier.junit_summary(junit)["skip_reasons"] == {"sources.db absent": 1}


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_failure_reporting_uses_one_fake_gh_issue_and_clean_comment_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect classes: gh is run from a non-checkout without a repository; a non-GitHub origin matches a github.com suffix."""
    checkout = tmp_path / "product"
    checkout.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=checkout, check=True, timeout=30)
    subprocess.run(
        [*git, "remote", "add", "origin", "https://github.com/example-org/example-product.git"],
        cwd=checkout,
        check=True,
        timeout=30,
    )
    monkeypatch.chdir(empty)
    monkeypatch.setattr(data_tier.github_client, "run", subprocess.run)
    executable = tmp_path / "gh"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"issue": None, "comments": [], "calls": []}), encoding="utf-8")
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"path = pathlib.Path({str(state)!r})\n"
        "state = json.loads(path.read_text())\n"
        "args = sys.argv[1:]\n"
        "if '--repo' not in args or args[args.index('--repo') + 1] != 'example-org/example-product':\n"
        "    sys.exit(4)\n"
        "state['calls'].append(args[:2])\n"
        "if args[:2] == ['issue', 'list']:\n"
        "    print(json.dumps([state['issue']] if state['issue'] else []))\n"
        "elif args[:2] == ['issue', 'create']:\n"
        "    state['issue'] = {'number': 77, 'title': '[infra][tests] Nightly data-tier failures', 'state': 'OPEN'}\n"
        "    state['body'] = sys.stdin.read()\n"
        "    print('issue 77')\n"
        "elif args[:2] == ['issue', 'edit']:\n"
        "    state['body'] = sys.stdin.read()\n"
        "elif args[:2] == ['issue', 'view']:\n"
        "    print(json.dumps({'comments': [{'body': body} for body in state['comments']]}))\n"
        "elif args[:2] == ['issue', 'comment']:\n"
        "    state['comments'].append(args[-1])\n"
        "elif args[:2] == ['issue', 'close']:\n"
        "    state['issue']['state'] = 'CLOSED'\n"
        "elif args[:2] == ['issue', 'reopen']:\n"
        "    state['issue']['state'] = 'OPEN'\n"
        "else:\n"
        "    sys.exit(4)\n"
        "path.write_text(json.dumps(state))\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    failed = {
        "run_key": "run-1",
        "main_sha": "a" * 40,
        "ran": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "failing_tests": [
            "tests/test_citation_resolution_invariant.py::test_published_citations_resolve_invariant[wiki/grammar/b2/academic-writing.md]"
        ],
        "skip_reasons": {},
    }
    baseline = {
        "tests/test_citation_resolution_invariant.py::test_published_citations_resolve_invariant[wiki/grammar/b2/academic-writing.md]": 8403
    }
    assert data_tier.report(failed, baseline, checkout).startswith("created")
    assert data_tier.report(failed, baseline, checkout) == "updated issue #77"
    clean = {**failed, "failed": 0, "passed": 2, "failing_tests": []}
    for errors in (
        {"runner_errors": ["memory guard stopped the run"]},
        {"missing_databases": ["sources.db"]},
    ):
        assert data_tier.report({**clean, **errors}, baseline, checkout) == "updated issue #77"
        assert json.loads(state.read_text())["comments"] == []
    assert data_tier.report(failed, baseline, checkout) == "updated issue #77"
    assert data_tier.report(clean, baseline, checkout) == "closed issue #77 after clean run"
    assert data_tier.report({**clean, "run_key": "run-2"}, baseline, checkout) == "no issue change"
    result = json.loads(state.read_text(encoding="utf-8"))
    assert "known issue #8403" in result["body"]
    assert len(result["comments"]) == 1
    assert result["calls"].count(["issue", "create"]) == 1
    assert result["calls"].count(["issue", "edit"]) == 4
    assert result["issue"]["state"] == "CLOSED"
    assert result["calls"].count(["issue", "close"]) == 1
    assert data_tier.report({**failed, "run_key": "run-3"}, baseline, checkout) == "updated issue #77"
    assert data_tier.report({**clean, "run_key": "run-4"}, baseline, checkout) == "closed issue #77 after clean run"
    result = json.loads(state.read_text())
    assert len(result["comments"]) == 2
    assert result["calls"].count(["issue", "create"]) == 1
    calls_before = len(result["calls"])
    for origin in (
        "https://notgithub.com/example-org/example-product.git",
        "https://evil.example/github.com/example-org/example-product.git",
        "https://evil.example?@github.com/example-org/example-product.git",
        "https://evil.example#@github.com/example-org/example-product.git",
        "https://g\u0131thub.com/example-org/example-product.git",
        "git@g\u0131thub.com:example-org/example-product.git",
    ):
        subprocess.run([*git, "remote", "set-url", "origin", origin], cwd=checkout, check=True, timeout=30)
        with pytest.raises(data_tier.DataTierError, match="not a GitHub owner/name"):
            data_tier.report(failed, baseline, checkout)
    subprocess.run(
        [*git, "remote", "set-url", "origin", "git@github.com:example-org/example-product.git"],
        cwd=checkout,
        check=True,
        timeout=30,
    )
    assert data_tier.github_repository(checkout) == "example-org/example-product"
    assert len(json.loads(state.read_text())["calls"]) == calls_before


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.parametrize(
    ("active", "current", "maximum", "error"),
    [
        ("inactive", "[not set]", "infinity", None),
        ("active", "1024", "infinity", None),
        ("active", "1024", str(8 * 1024**3), None),
        ("active", "[not set]", "infinity", "unknown"),
        ("active", "1024", "invalid", "unknown"),
        ("active", str(8 * 1024**3), str(10 * 1024**3), "headroom"),
    ],
)
def test_slice_memory_states(
    monkeypatch: pytest.MonkeyPatch, active: str, current: str, maximum: str, error: str | None
) -> None:
    monkeypatch.setattr(data_tier, "available_memory", lambda: 10 * 1024**3)
    monkeypatch.setattr(
        data_tier,
        "command",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=f"LoadState=loaded\nActiveState={active}\nMemoryCurrent={current}\nMemoryMax={maximum}\n"
        ),
    )
    if error:
        with pytest.raises(data_tier.DataTierError, match=error):
            data_tier.require_memory()
    else:
        data_tier.require_memory()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.usefixtures("validated_child_namespace")
def test_collection_error_keeps_healthy_selected_test_and_junit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test_good.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "test_bad.py").write_text("raise ImportError('broken import')\n")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setattr(
        data_tier,
        "load_selection",
        lambda *_args, **_kwargs: ({'nodeids': ['test_good.py::test_ok'], 'files': ['test_good.py', 'test_bad.py']}, "0" * 64),
    )
    junit = tmp_path / "junit.xml"
    calls = []

    def execute(argv: list[str], *, timeout: int) -> int:
        calls.append(argv)
        junit.write_text('<testsuite tests="1"><testcase classname="test_good" name="test_ok"/></testsuite>')
        return 0

    monkeypatch.setattr(data_tier, "run_process_group", execute)
    args = SimpleNamespace(read_only=[], selection_sha256="0" * 64, junit=str(junit), collected=str(tmp_path / "collected.json"), only=None, bulk_reason=None)
    assert data_tier.pytest_child(args) == 1
    assert calls[0][-1] == "test_good.py::test_ok"
    summary = data_tier.junit_summary(junit)
    assert (summary["passed"], summary["failed"]) == (1, 1)
    assert "test_bad.py" in summary["failing_tests"]


def _plant_selection_tree(checkout: Path) -> None:
    """Write the definitions the fixture selection names, so the audit reads this tree."""
    selection = json.loads((checkout / "scripts" / "ci" / "data_tier_selection.json").read_text(encoding="utf-8"))
    grouped: dict[str, set[str]] = {}
    for nodeid in selection["nodeids"]:
        relative, _separator, qualname = nodeid.split("[", 1)[0].partition("::")
        grouped.setdefault(relative, set()).add(qualname)
    for relative in selection["files"]:
        grouped.setdefault(relative, set())
    for relative, quals in grouped.items():
        classes: dict[str, list[str]] = {}
        functions: list[str] = []
        for qualname in sorted(quals):
            if "::" in qualname:
                class_name, method = qualname.split("::", 1)
                classes.setdefault(class_name, []).append(method)
            else:
                functions.append(qualname)
        lines = ['"""Selection stub."""', ""]
        for name in functions:
            lines.extend([f"def {name}():", "    return None", ""])
        for class_name, methods in classes.items():
            lines.append(f"class {class_name}:")
            for method in methods:
                lines.extend([f"    def {method}(self):", "        return None"])
            lines.append("")
        if len(lines) == 2:
            lines.append("VALUE = None")
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def nightly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    primary = tmp_path / "primary"
    project_python = primary / ".venv" / "bin" / "python"
    project_python.parent.mkdir(parents=True)
    project_python.touch()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    scratch_root = tmp_path / "fleet-scratch"
    monkeypatch.setenv("LU_SCRATCH_ROOT", str(scratch_root))
    monkeypatch.setattr(scratch, "DEFAULT_SCRATCH_ROOT", scratch_root)
    for name, value in _DATA_TIER_INPUTS.items():
        path = checkout / "scripts/ci" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    _plant_selection_tree(checkout)
    for argv in (["git", "init", "-q", str(checkout)], ["git", "-C", str(checkout), "add", "."],
                 ["git", "-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "selection"]):
        subprocess.run(argv, check=True, capture_output=True, timeout=30)
    monkeypatch.setattr(data_tier, "bind_stores", lambda _primary: ({}, [], {}, []))
    monkeypatch.setattr(data_tier, "exposed_data", lambda *_args: [])
    monkeypatch.setattr(data_tier, "link_atlas", lambda *_args: None)
    monkeypatch.setattr(data_tier, "read_only_namespace", lambda *_args: [])
    monkeypatch.setattr(data_tier.test_slots, "enabled", lambda: False)
    events = []
    reports = []
    snapshots = []
    remove_snapshot = data_tier.shutil.rmtree

    monkeypatch.setattr(data_tier, "state_root", lambda: primary)
    monkeypatch.setattr(data_tier, "primary_checkout", lambda: primary)
    monkeypatch.setattr(data_tier, "project_interpreter", lambda root: project_python if root == primary else None)
    monkeypatch.setattr(data_tier, "require_memory", lambda: None)
    monkeypatch.setattr(data_tier, "prune_stale_worktrees", lambda _primary: None)
    monkeypatch.setattr(data_tier, "make_test_worktree", lambda _primary: checkout)
    monkeypatch.setattr(data_tier, "command", lambda *args, **kwargs: SimpleNamespace(stdout="a" * 40))
    monkeypatch.setattr(data_tier, "bulk_status", lambda *args: (None, "unavailable"))
    monkeypatch.setattr(data_tier, "stop_scope", lambda unit: events.append("stop"))
    monkeypatch.setattr(data_tier, "remove_test_worktree", lambda *args: events.append("remove"))

    def execute(argv: list[str], **kwargs: object) -> int:
        assert kwargs["cwd"] == checkout
        assert "lu_machinery.ci.data_tier" in argv
        assert "-m" in argv
        assert 0 < kwargs["timeout"] <= data_tier.RUN_BUDGET_SECONDS
        Path(argv[argv.index("--junit") + 1]).write_text('<testsuite><testcase name="test_ok"/></testsuite>')
        return 0

    monkeypatch.setattr(data_tier, "run_process_group", execute)

    def report(summary: dict, baseline: dict, checkout: Path) -> str:
        reports.append(json.loads(json.dumps(summary)))
        return "reported"

    monkeypatch.setattr(data_tier, "report", report)
    try:
        yield SimpleNamespace(
            primary=primary,
            checkout=checkout,
            scratch_root=scratch_root,
            snapshots=snapshots,
            events=events,
            reports=reports,
            execute=execute,
        )
    finally:
        # Production may retain snapshots until stop is proven; tests own them
        # after their assertions, even when runner cleanup is monkeypatched.
        for snapshot in snapshots:
            if snapshot.exists():
                remove_snapshot(snapshot)
            assert not snapshot.exists()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.parametrize("action", ["removed", "skipped", "error"])
def test_checkout_cleanup_obeys_guard_outcome(action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / ".worktrees" / "data-tier" / ("run-" + "a" * 32)
    calls = []

    def remove(path: Path, **kwargs: object) -> SimpleNamespace:
        calls.append((path, kwargs))
        return SimpleNamespace(
            action=action, reason="guard outcome", error="guard failure" if action == "error" else None
        )

    monkeypatch.setattr(data_tier.worktree_claims, "remove_unclaimed_worktree", remove)
    if action == "removed":
        data_tier.remove_test_worktree(tmp_path, checkout)
    else:
        with pytest.raises(data_tier.DataTierError, match=f"checkout cleanup {action}: guard outcome"):
            data_tier.remove_test_worktree(tmp_path, checkout)
    assert calls == [
        (
            checkout,
            {
                "repo_root": tmp_path,
                "reason": "data-tier checkout cleanup",
                "owner_task_id": None,
                "force": True,
            },
        )
    ]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_checkout_creation_failure_uses_guarded_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created = []
    removed = []

    def command(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if "add" in argv:
            assert "--git-dir" not in argv
            assert kwargs.get("cwd") == tmp_path
            created.append(Path(argv[-2]))
            return SimpleNamespace(stdout="")
        raise data_tier.DataTierError("fetch failed")

    monkeypatch.setattr(data_tier, "command", command)
    monkeypatch.setattr(
        data_tier, "remove_test_worktree", lambda primary, checkout: removed.append((primary, checkout))
    )
    with pytest.raises(data_tier.DataTierError, match="fetch failed"):
        data_tier.make_test_worktree(tmp_path)
    assert len(created) == 1
    assert removed == [(tmp_path, created[0])]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.parametrize(
    "failure",
    [
        "selection",
        "memory",
        "interpreter",
        "worktree",
        "missing_database",
        "bulk",
        "second_memory",
        "collection",
        "runner_exit",
        "timeout",
        "malformed_junit",
        "cleanup",
        "scope_cleanup",
        "signal",
        "budget",
    ],
)
def test_every_runner_failure_reaches_report(
    nightly: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise data_tier.DataTierError("runner failed at /private/example.db")

    if failure in {"selection", "memory", "worktree", "bulk", "cleanup", "scope_cleanup"}:
        helper = {
            "selection": "load_selection",
            "memory": "require_memory",
            "worktree": "make_test_worktree",
            "bulk": "bulk_status",
            "cleanup": "remove_test_worktree",
            "scope_cleanup": "stop_scope",
        }[failure]
        monkeypatch.setattr(data_tier, helper, fail)
    elif failure == "interpreter":
        (nightly.primary / ".venv" / "bin" / "python").unlink()
    elif failure == "missing_database":
        monkeypatch.setattr(data_tier, "bind_stores", lambda *_args: ({}, [], {}, ["sources.db"]))
    elif failure == "second_memory":
        memory_calls = iter([None, "fail"])

        def memory() -> None:
            if next(memory_calls):
                fail()

        monkeypatch.setattr(data_tier, "require_memory", memory)
    elif failure == "budget":
        clock = iter([0, data_tier.RUN_BUDGET_SECONDS + 1])
        monkeypatch.setattr(data_tier.time, "monotonic", lambda: next(clock))
    else:

        def execute(argv: list[str], **kwargs: object) -> int:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("pytest", 1)
            if failure == "signal":
                os.kill(os.getpid(), data_tier.signal.SIGTERM)
            if failure == "collection":
                return 2
            nightly.execute(argv, **kwargs)
            if failure == "malformed_junit":
                Path(argv[argv.index("--junit") + 1]).write_text("invalid xml")
            return 3 if failure == "runner_exit" else 0

        monkeypatch.setattr(data_tier, "run_process_group", execute)
    assert data_tier.main(["run"]) == 1
    assert len(nightly.reports) == 1
    summary = nightly.reports[0]
    assert data_tier.run_failed(summary)
    body = data_tier.issue_body(summary, {})
    assert "/private/" not in body
    assert summary["run_key"] in body
    assert summary["runner_errors"] or summary["missing_databases"]
    receipts = list((nightly.primary / "batch_state" / "data-tier").glob("*.summary.json"))
    if failure != "selection":
        assert json.loads(receipts[0].read_text()) == summary
    if "stop" in nightly.events and failure != "cleanup":
        assert nightly.events == ["stop", "remove"]
    if failure == "scope_cleanup":
        assert nightly.events == []


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_github_failure_is_single_attempt_and_persisted(
    nightly: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def report(summary: dict, baseline: dict, checkout: Path) -> str:
        calls.append(json.loads(json.dumps(summary)))
        raise data_tier.DataTierError("gh unavailable at /private/gh")

    monkeypatch.setattr(data_tier, "report", report)
    assert data_tier.run(SimpleNamespace(only=None, no_report=False)) == 1
    assert len(calls) == 1
    summary = json.loads(next((nightly.primary / "batch_state" / "data-tier").glob("*.summary.json")).read_text())
    assert len(summary["runner_errors"]) == 1
    assert "/private/" not in json.dumps(summary)


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_clean_run_and_no_report(nightly: SimpleNamespace) -> None:
    assert data_tier.run(SimpleNamespace(only=None, no_report=True)) == 0
    assert nightly.reports == []
    assert nightly.events == ["stop", "remove"]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_issue_body_is_bounded_with_total_and_runner_errors() -> None:
    summary = {
        "run_key": "run-1",
        "main_sha": "a" * 40,
        "ran": 700,
        "passed": 0,
        "failed": 700,
        "skipped": 0,
        "failing_tests": [f"test_{i}" + "x" * 1000 for i in range(700)],
        "skip_reasons": {},
        "runner_errors": ["runner failed at /private/log"],
        "hydration_errors": {"group": "hydrate failed"},
        "missing_databases": ["sources.db"],
    }
    body = data_tier.issue_body(summary, {})
    assert len(body) <= data_tier.ISSUE_BODY_LIMIT
    assert "700 total" in body
    assert "runner failed at <host-path>" in body
    assert "sources.db" in body
    assert "Truncated" in body and "local run JUnit" in body


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_stale_worktrees_pruned_only_without_active_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / ".worktrees" / "data-tier"
    old = parent / ("run-" + "a" * 32)
    recent = parent / ("run-" + "b" * 32)
    unrelated = parent / "run-unrelated"
    for path in (old, recent, unrelated):
        path.mkdir(parents=True)
    os.utime(old, (0, 0))
    removed = []
    monkeypatch.setattr(data_tier, "remove_test_worktree", lambda primary, checkout: removed.append(checkout))
    monkeypatch.setattr(data_tier, "command", lambda *args, **kwargs: SimpleNamespace(stdout="active.scope"))
    with pytest.raises(data_tier.DataTierError, match="still active"):
        data_tier.prune_stale_worktrees(tmp_path)
    assert removed == []
    monkeypatch.setattr(data_tier, "command", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    data_tier.prune_stale_worktrees(tmp_path)
    assert removed == [old]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_scope_stop_handles_collected_and_loaded_units(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    state = "not-found"

    def command(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(stdout=f"LoadState={state}\n", returncode=1 if state == "not-found" else 0, stderr="")

    monkeypatch.setattr(data_tier, "command", command)
    monkeypatch.setattr(data_tier.subprocess, "run", command)
    data_tier.stop_scope("lu-data-tier-test.scope")
    assert len(calls) == 1
    state = "loaded"
    data_tier.stop_scope("lu-data-tier-test.scope")
    assert calls[-1] == ["systemctl", "--user", "stop", "lu-data-tier-test.scope"]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
@pytest.mark.parametrize("interrupted", [False, True])
def test_process_group_stops_descendants_on_timeout_or_interruption(
    monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    waits = []
    signals = []

    class Process:
        pid = 123

        def wait(self, *, timeout: int) -> int:
            waits.append(timeout)
            if len(waits) == 1 and interrupted:
                raise data_tier.DataTierError("run interrupted")
            if len(waits) < 3:
                raise subprocess.TimeoutExpired("child", timeout)
            return 0

    monkeypatch.setattr(data_tier.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(data_tier.os, "killpg", lambda pid, sig: signals.append(sig))
    with pytest.raises(data_tier.DataTierError, match="interrupted" if interrupted else "timed out"):
        data_tier.run_process_group([sys.executable, "-c", "pass"], timeout=2)
    assert signals == [data_tier.signal.SIGTERM, data_tier.signal.SIGKILL]
    assert waits == [2, 30, 10]


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_finished_data_tier_run_leaves_no_preserved_copy(tmp_path):
    """#12528: the run discards its own ignored output, so the guarded remover has nothing to copy."""
    primary = tmp_path / "primary"
    primary.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=primary, check=True, timeout=30)
    (primary / ".gitignore").write_text("data/\nsite/public/\n")
    (primary / "README.md").write_text("tracked\n")
    subprocess.run([*git, "add", "."], cwd=primary, check=True, timeout=30)
    subprocess.run([*git, "commit", "-q", "-m", "base"], cwd=primary, check=True, timeout=30)
    parent = primary / ".worktrees" / "data-tier"
    parent.mkdir(parents=True)
    checkout = parent / f"run-{'0123456789abcdef' * 2}"
    subprocess.run([*git, "worktree", "add", "-q", "--detach", str(checkout)], cwd=primary, check=True, timeout=30)
    (checkout / "data").mkdir()
    (checkout / "data" / "atlas.db").write_bytes(b"x" * 4096)
    (checkout / "data" / "ua-gec").mkdir()
    (checkout / "data" / "ua-gec" / "part.txt").write_text("copy")
    (checkout / "site" / "public").mkdir(parents=True)
    (checkout / "site" / "public" / "lexicon.json").write_text("{}")

    discarded = data_tier.discard_run_output(checkout)
    assert discarded["files"] == 3
    assert discarded["bytes"] >= 4096
    assert (checkout / "README.md").is_file()

    removal = data_tier.remove_test_worktree(primary, checkout)
    assert removal.action == "removed"
    assert not removal.preserved_artifacts
    assert not checkout.exists()
    assert not (primary / "batch_state" / "preserved").exists()


@pytest.mark.usefixtures('configured_roots', '_no_live_pool')
@pytest.mark.reads_content
def test_discarded_non_ascii_file_is_counted_with_its_real_byte_size(tmp_path):
    """#12537: a discarded file with a non-ASCII name is counted with its real byte size."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=tmp_path, check=True, timeout=30)
    (tmp_path / ".gitignore").write_text("data/\n")
    (tmp_path / "README.md").write_text("tracked\n")
    subprocess.run([*git, "add", "."], cwd=tmp_path, check=True, timeout=30)
    subprocess.run([*git, "commit", "-q", "-m", "base"], cwd=tmp_path, check=True, timeout=30)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "слово.db").write_bytes(b"x" * 4096)
    (tmp_path / "привіт.txt").write_bytes(b"y" * 100)
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "a\rb").write_bytes(b"z" * 7)
    (tmp_path / os.fsdecode(b"output/\xff.bin")).write_bytes(b"w" * 7)

    assert data_tier.discard_run_output(tmp_path) == {"files": 4, "bytes": 4210}
    assert not (tmp_path / "привіт.txt").exists()
    assert not (tmp_path / "data").exists()




@pytest.fixture
def validated_child_namespace(monkeypatch):
    monkeypatch.setattr(data_tier, "require_read_only", lambda _paths: None)


_DATA_TIER_INPUTS = {'data_tier_selection.json': '{\n  "schema": 1,\n  "source_run": 36611889906,\n  "source_sha": "410da34bb875aca2acfc960da4d8c9b87ae7867b",\n  "audit_issue": 9218,\n  "class_c_merge_group": 127,\n  "class_c_nightly": 0,\n  "real_store_audit": {\n    "issue": 10021,\n    "historical_h126": 124,\n    "current_factory_g177": 175,\n    "identified_real_store_cases": 181,\n    "new_selection_entries": 16,\n    "new_collection_files": 1\n  },\n  "artifact_groups": [],\n  "nodeids": [\n    "tests/agent_runtime/test_review_mcp.py::test_sources_server_stdio_integration",\n    "tests/audit/test_antonenko_prose_narrowing.py::test_fallback_activates_when_narrowed_query_finds_nothing",\n    "tests/audit/test_antonenko_prose_narrowing.py::test_hits_preserve_backward_compatible_fields",\n    "tests/audit/test_antonenko_prose_narrowing.py::test_narrowed_retrieval_fires_on_russianism_phrase",\n    "tests/audit/test_judge_eval_lib_h2.py::test_antonenko_fulltext_search_returns_page_and_snippet",\n    "tests/audit/test_judge_eval_lib_h2.py::test_build_judge_prompt_h2_preserves_canonical_greeting_protection",\n    "tests/audit/test_judge_eval_lib_h2.py::test_ua_gec_calque_search_hits_known_calque",\n    "tests/audit/test_judge_eval_lib_h2.py::test_ua_gec_index_loads_with_relevant_tags",\n    "tests/build/test_alphabet_modules.py::test_repair_chai_neighbors_are_vesum_words_and_mutants_are_not",\n    "tests/build/test_fresh_style_cards.py::test_card_exemplars_are_attested_in_the_corpus[a1]",\n    "tests/build/test_fresh_style_cards.py::test_card_exemplars_are_attested_in_the_corpus[a2]",\n    "tests/build/test_fresh_style_cards.py::test_card_exemplars_are_attested_in_the_corpus[b1plus]",\n    "tests/curriculum/evidence/test_base_request.py::test_a1_base_request_accepted_by_build_words",\n    "tests/curriculum/evidence/test_evidence_cli.py::test_cli_dry_run_committed_five_lemmas_request",\n    "tests/curriculum/evidence/test_tags.py::test_every_live_vesum_atom_is_declared",\n    "tests/curriculum/resolver/test_resolver_real_data.py::test_named_real_cases",\n    "tests/curriculum/resolver/test_resolver_real_data.py::test_ulp_lesson_10_replay",\n    "tests/curriculum/resolver/test_resolver_real_data.py::test_zaraz_checked_and_unchecked_contract[False]",\n    "tests/curriculum/resolver/test_resolver_real_data.py::test_zaraz_checked_and_unchecked_contract[True]",\n    "tests/mcp/test_sources_golden_set.py::test_sources_golden_set_matches_frozen_handlers",\n    "tests/mcp/test_ua_gec_search.py::test_search_ua_gec_errors_execution",\n    "tests/mcp/test_ua_gec_search.py::test_search_ua_gec_errors_tag_filter_does_not_raise",\n    "tests/test_adjective_mechanics_engine.py::test_vesum_verification",\n    "tests/test_admit_fmu_boosters.py::test_fmu_booster_all_single_words_vesum_attested",\n    "tests/test_adverb_mechanics_engine.py::test_vesum_rejects_empty_and_punctuation_answers",\n    "tests/test_adverb_mechanics_engine.py::test_vesum_sanitization_and_malformed_token_detection",\n    "tests/test_adverb_mechanics_engine.py::test_vesum_verification_clean",\n    "tests/test_atlas_conformance.py::test_heritage_lemma_lookup_attests_grinchenko_word_real_db",\n    "tests/test_check_text.py::test_acceptance_textbook_fixture_correctness_and_planted",\n    "tests/test_check_text.py::test_mcp_call_tool_dispatch",\n    "tests/test_check_text.py::test_synthetic_shadow_curated_and_suspicion_split",\n    "tests/test_content_lexicon_reconciler.py::test_real_vesum_db_smoke_recognizes_basic_inflected_form",\n    "tests/test_content_spelling_gate.py::test_real_vesum_db_recognizes_valid_form_and_flags_typo",\n    "tests/test_curated_seed_to_lexical_jsonl.py::test_sample_seed_round_trips_through_projection",\n    "tests/test_esum_search.py::test_search_esum_berkut_returns_turkic_origin",\n    "tests/test_export_runtime_shards.py::test_real_db_public_counts_and_alias_targets",\n    "tests/test_generate_practice_deck.py::test_live_heritage_normative_support_is_verbatim_and_names_every_frame",\n    "tests/test_generate_practice_deck.py::test_live_language_review_fixes_are_source_bound",\n    "tests/test_generate_practice_deck.py::test_live_paronym_gloss_sources_are_verbatim_and_two_sided_or_withheld",\n    "tests/test_interjection_mechanics_engine.py::test_is_valid_vesum_token_rejections_and_compounds",\n    "tests/test_interjection_mechanics_engine.py::test_vesum_verification_100_percent",\n    "tests/test_mcp_sources_identity_integration.py::test_real_endpoint_logs_hash_only_never_argument_values_or_response_text",\n    "tests/test_mcp_sources_identity_integration.py::test_real_transport_attests_endpoint_identity_against_local_files",\n    "tests/test_mcp_sources_identity_integration.py::test_real_transport_fails_closed_on_a_real_tool_error",\n    "tests/test_mcp_sources_identity_integration.py::test_real_transport_preflight_requires_every_frozen_tool",\n    "tests/test_mcp_sources_identity_integration.py::test_real_transport_verify_words_round_trip_is_harmless_and_public",\n    "tests/test_mcp_sources_server.py::TestIntegrationSmoke::test_smoke_check_modern_form_archaic_only",\n    "tests/test_mcp_sources_server.py::TestIntegrationSmoke::test_smoke_check_modern_form_mixed",\n    "tests/test_mcp_sources_server.py::TestIntegrationSmoke::test_smoke_check_modern_form_modern_only",\n    "tests/test_mcp_sources_server.py::TestIntegrationSmoke::test_smoke_verify_lemma_archaic",\n    "tests/test_mcp_sources_server.py::TestIntegrationSmoke::test_smoke_verify_word_archaic",\n    "tests/test_miyklas_relation_miner.py::test_cleaned_artifact_has_no_known_label_or_chopped_headwords",\n    "tests/test_morphological_validator.py::TestAccusativeConstraint::test_accusative_caught_nom_only",\n    "tests/test_morphological_validator.py::TestAccusativeHomonyms::test_verb_homonym_not_flagged_as_acc",\n    "tests/test_morphological_validator.py::TestAgreement::test_correct_agreement_ok",\n    "tests/test_morphological_validator.py::TestAgreement::test_mismatch_caught",\n    "tests/test_morphological_validator.py::TestAgreement::test_sentence_boundary_respected",\n    "tests/test_morphological_validator.py::TestAgreement::test_це_not_flagged",\n    "tests/test_morphological_validator.py::TestBracketStripping::test_phonetic_brackets_skipped",\n    "tests/test_morphological_validator.py::TestCaseDetection::test_adverb_not_flagged_as_case",\n    "tests/test_morphological_validator.py::TestCaseDetection::test_locative_caught",\n    "tests/test_morphological_validator.py::TestCaseDetection::test_nominative_ok",\n    "tests/test_morphological_validator.py::TestChunkExceptions::test_farewell_exempt",\n    "tests/test_morphological_validator.py::TestImperativeDetection::test_imperative_after_m47_ok",\n    "tests/test_morphological_validator.py::TestImperativeDetection::test_imperative_caught",\n    "tests/test_morphological_validator.py::TestPresentTenseOnly::test_noun_homonym_not_flagged_as_past",\n    "tests/test_morphological_validator.py::TestPresentTenseOnly::test_past_tense_caught",\n    "tests/test_morphological_validator.py::TestStressMarks::test_stressed_words_not_split",\n    "tests/test_morphological_validator.py::TestVerbDetection::test_standalone_verb_caught",\n    "tests/test_morphological_validator.py::TestVerbDetection::test_verb_after_m15_ok",\n    "tests/test_navsi200_asr_bakeoff.py::TestAsrBakeoffLedgerAndPrivacy::test_honest_accounting_in_ledger",\n    "tests/test_navsi200_asr_bakeoff.py::TestAsrBakeoffLedgerAndPrivacy::test_privacy_no_teacher_names_in_ledger",\n    "tests/test_navsi200_asr_bakeoff.py::TestAsrBakeoffLedgerAndPrivacy::test_privacy_no_verbatim_transcripts_in_ledger",\n    "tests/test_navsi200_captions.py::TestNavsi200CaptionsLedger::test_all_catalog_videos_accounted_for",\n    "tests/test_navsi200_captions.py::TestNavsi200CaptionsLedger::test_ledger_metadata_shape",\n    "tests/test_navsi200_captions.py::TestNavsi200CaptionsLedger::test_lessons_shape_and_types",\n    "tests/test_navsi200_captions.py::TestNavsi200CaptionsLedger::test_privacy_no_teacher_names_in_ledger",\n    "tests/test_navsi200_captions.py::TestNavsi200CaptionsLedger::test_summary_consistency",\n    "tests/test_numeral_mechanics_engine.py::test_vesum_verification_clean",\n    "tests/test_ohoiko_paired_headword_split.py::test_analyze_all_curated_leftovers_disposition",\n    "tests/test_ohoiko_paired_headword_split.py::test_classify_taught_candidate",\n    "tests/test_ohoiko_paired_headword_split.py::test_live_curated_unit_a_leftovers_census_invariants",\n    "tests/test_ohoiko_paired_headword_split.py::test_live_taught_residual_census_invariants",\n    "tests/test_ohoiko_paired_headword_split.py::test_resolve_leg_lemma_recovers_ocr_lookalikes",\n    "tests/test_ohoiko_paired_headword_split.py::test_space_collapse_marks_multi_component_tokenization_manual",\n    "tests/test_ohoiko_paired_headword_split.py::test_space_collapse_requires_collapsed_vesum_and_invalid_components",\n    "tests/test_ohoiko_paired_headword_split.py::test_taught_residual_census_artifact_file_integrity",\n    "tests/test_ohoiko_paired_headword_split.py::test_ulp_taught_leftovers_heritage_holds",\n    "tests/test_practice_quality_gate.py::test_ground_with_sources_direct",\n    "tests/test_practice_quality_gate.py::test_reviewer_probe_on_the_committed_deck_and_real_sources_db",\n    "tests/test_practice_quality_gate.py::test_reviewer_source_label_probe_on_committed_err_0005[True]",\n    "tests/test_pronoun_mechanics_engine.py::test_vesum_database_validation",\n    "tests/test_pronoun_mechanics_engine.py::test_vesum_distractor_validation",\n    "tests/test_reattribute_ukrlib.py::TestPostSearchQuality::test_post_author_chunks_in_sources_db[\\\\u041a\\\\u043e\\\\u0442\\\\u043b\\\\u044f\\\\u0440\\\\u0435\\\\u0432\\\\u0441\\\\u044c\\\\u043a\\\\u0438\\\\u0439 \\\\u0406.-\\\\u0415\\\\u043d\\\\u0435\\\\u0457\\\\u0434\\\\u0430]",\n    "tests/test_reattribute_ukrlib.py::TestPostSearchQuality::test_post_author_chunks_in_sources_db[\\\\u041a\\\\u043e\\\\u0446\\\\u044e\\\\u0431\\\\u0438\\\\u043d\\\\u0441\\\\u044c\\\\u043a\\\\u0438\\\\u0439 \\\\u041c.-Fata Morgana]",\n    "tests/test_reattribute_ukrlib.py::TestPostSearchQuality::test_post_author_chunks_in_sources_db[\\\\u041c\\\\u0438\\\\u0440\\\\u043d\\\\u0438\\\\u0439 \\\\u041f.-\\\\u0425\\\\u0456\\\\u0431\\\\u0430 \\\\u0440\\\\u0435\\\\u0432\\\\u0443\\\\u0442\\\\u044c \\\\u0432\\\\u043e\\\\u043b\\\\u0438]",\n    "tests/test_reattribute_ukrlib.py::TestPostSearchQuality::test_post_author_chunks_in_sources_db[\\\\u041d\\\\u0435\\\\u0447\\\\u0443\\\\u0439-\\\\u041b\\\\u0435\\\\u0432\\\\u0438\\\\u0446\\\\u044c\\\\u043a\\\\u0438\\\\u0439 \\\\u0406.-\\\\u041a\\\\u0430\\\\u0439\\\\u0434\\\\u0430\\\\u0448\\\\u0435\\\\u0432\\\\u0430 \\\\u0441\\\\u0456\\\\u043c\'\\\\u044f]",\n    "tests/test_reattribute_ukrlib.py::TestPostSearchQuality::test_post_author_chunks_in_sources_db[\\\\u0422\\\\u0438\\\\u0447\\\\u0438\\\\u043d\\\\u0430 \\\\u041f.-\\\\u0410\\\\u0440\\\\u0444\\\\u0430\\\\u043c\\\\u0438, \\\\u0430\\\\u0440\\\\u0444\\\\u0430\\\\u043c\\\\u0438]",\n    "tests/test_sources_db.py::TestHeadwordFirstRanking::test_search_style_guide_live_db_documented_examples",\n    "tests/test_verb_mechanics_engine.py::test_vesum_verification",\n    "tests/test_vesum_heritage_attestation.py::test_derivational_fallback_does_not_leak_adversarial_russianisms",\n    "tests/test_vesum_heritage_attestation.py::test_direct_standard_active_participle_calques_stay_existing_behavior",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_accepts_engine_authentic_not_in_allowlist",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_accepts_inflected_denominal_adjectives",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_accepts_negated_participles_of_standard_bases",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_accepts_oblique_inflections_of_dialect_words",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_accepts_productive_derivational_bases",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_accepts_productive_noun_diminutives",\n    "tests/test_vesum_heritage_attestation.py::test_folk_vesum_gate_still_rejects_teaching_prose_russianisms",\n    "tests/test_vesum_heritage_attestation.py::test_morphology_fallback_does_not_leak_russianism_via_verb_root",\n    "tests/test_vocab_gen.py::TestVesumEnrichment::test_noun_gets_pos_and_gender",\n    "tests/wiki/diagnostics/test_corpus_gaps.py::test_load_coverage_map_resolves_the_published_artifact",\n    "tests/wiki/test_grade_filter.py::test_search_textbooks_does_not_apply_hard_grade_filter",\n    "tests/wiki/test_t1_t2_pipeline.py::test_t1_t2_pipeline_surfaces_section_level_a1_evidence",\n    "tests/test_esum_search.py::test_search_esum_sibir_is_outside_volume_one_scope",\n    "tests/verification/test_antonenko_patterns.py::test_live_all_checks_clean_corpus",\n    "tests/verification/test_antonenko_patterns.py::test_live_recommendations_vesum",\n    "tests/verification/test_antonenko_patterns.py::test_live_evidence_chunks_contain_condemned_forms",\n    "tests/test_check_text.py::test_mcp_book_calque_acceptance",\n    "tests/test_check_text.py::test_mcp_concluding_is_reported_once",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u0412\\\\u0456\\\\u043d \\\\u043f\\\\u0440\\\\u0438\\\\u0439\\\\u043d\\\\u044f\\\\u0432 \\\\u043f\\\\u0440\\\\u043e\\\\u043f\\\\u043e\\\\u0437\\\\u0438\\\\u0446\\\\u0456\\\\u044e \\\\u0441\\\\u0442\\\\u0430\\\\u0442\\\\u0438 \\\\u0434\\\\u0438\\\\u0440\\\\u0435\\\\u043a\\\\u0442\\\\u043e\\\\u0440\\\\u043e\\\\u043c]",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u041a\\\\u043e\\\\u0440\\\\u043e\\\\u043b\\\\u044c \\\\u043f\\\\u0440\\\\u0438\\\\u0439\\\\u043d\\\\u044f\\\\u0432 \\\\u043f\\\\u0440\\\\u043e\\\\u043f\\\\u043e\\\\u0437\\\\u0438\\\\u0446\\\\u0456\\\\u044e \\\\u0441\\\\u0443\\\\u043b\\\\u0442\\\\u0430\\\\u043d\\\\u0430]",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u0412\\\\u0456\\\\u043d \\\\u0441\\\\u0442\\\\u043e\\\\u044f\\\\u0432 \\\\u043d\\\\u0430 \\\\u043f\\\\u0440\\\\u043e\\\\u0442\\\\u044f\\\\u0437\\\\u0456 \\\\u043a\\\\u0456\\\\u043b\\\\u044c\\\\u043a\\\\u0430 \\\\u0445\\\\u0432\\\\u0438\\\\u043b\\\\u0438\\\\u043d]",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u0412\\\\u0456\\\\u043d \\\\u0441\\\\u0438\\\\u0434\\\\u0456\\\\u0432 \\\\u043d\\\\u0430 \\\\u043f\\\\u0440\\\\u043e\\\\u0442\\\\u044f\\\\u0437\\\\u0456 \\\\u0433\\\\u043e\\\\u0434\\\\u0438\\\\u043d\\\\u0438 \\\\u0434\\\\u0432\\\\u0456 \\\\u0439 \\\\u0437\\\\u0430\\\\u0441\\\\u0442\\\\u0443\\\\u0434\\\\u0438\\\\u0432\\\\u0441\\\\u044f]",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u0412\\\\u043e\\\\u043d\\\\u0438 \\\\u0432\\\\u0442\\\\u0440\\\\u0430\\\\u0442\\\\u0438\\\\u043b\\\\u0438 \\\\u0441\\\\u0432\\\\u0456\\\\u0434\\\\u043e\\\\u043c\\\\u0456\\\\u0441\\\\u0442\\\\u044c \\\\u0441\\\\u0432\\\\u043e\\\\u0454\\\\u0457 \\\\u0432\\\\u0456\\\\u0434\\\\u043f\\\\u043e\\\\u0432\\\\u0456\\\\u0434\\\\u0430\\\\u043b\\\\u044c\\\\u043d\\\\u043e\\\\u0441\\\\u0442\\\\u0456]",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u042f\\\\u043a \\\\u043d\\\\u0435 \\\\u0434\\\\u0438\\\\u0432\\\\u043d\\\\u043e, \\\\u0432\\\\u0456\\\\u043d \\\\u043f\\\\u0440\\\\u0438\\\\u0439\\\\u0448\\\\u043e\\\\u0432]",\n    "tests/test_check_text.py::test_mcp_round2_no_firm_book_false_positives[\\\\u041f\\\\u043e \\\\u043c\\\\u043e\\\\u0457\\\\u0439 \\\\u0434\\\\u0443\\\\u043c\\\\u0446\\\\u0456 \\\\u0442\\\\u0430\\\\u043a \\\\u043d\\\\u0435 \\\\u043c\\\\u043e\\\\u0436\\\\u043d\\\\u0430 \\\\u0440\\\\u043e\\\\u0431\\\\u0438\\\\u0442\\\\u0438]"\n  ],\n  "files": [\n    "tests/agent_runtime/test_review_mcp.py",\n    "tests/audit/test_antonenko_prose_narrowing.py",\n    "tests/audit/test_judge_eval_lib_h2.py",\n    "tests/build/test_alphabet_modules.py",\n    "tests/build/test_fresh_style_cards.py",\n    "tests/curriculum/evidence/test_base_request.py",\n    "tests/curriculum/evidence/test_evidence_cli.py",\n    "tests/curriculum/evidence/test_tags.py",\n    "tests/curriculum/resolver/test_resolver_real_data.py",\n    "tests/mcp/test_sources_golden_set.py",\n    "tests/mcp/test_ua_gec_search.py",\n    "tests/test_adjective_mechanics_engine.py",\n    "tests/test_admit_fmu_boosters.py",\n    "tests/test_adverb_mechanics_engine.py",\n    "tests/test_atlas_conformance.py",\n    "tests/test_check_text.py",\n    "tests/test_content_lexicon_reconciler.py",\n    "tests/test_content_spelling_gate.py",\n    "tests/test_curated_seed_to_lexical_jsonl.py",\n    "tests/test_esum_search.py",\n    "tests/test_export_runtime_shards.py",\n    "tests/test_generate_practice_deck.py",\n    "tests/test_interjection_mechanics_engine.py",\n    "tests/test_mcp_sources_identity_integration.py",\n    "tests/test_mcp_sources_server.py",\n    "tests/test_miyklas_relation_miner.py",\n    "tests/test_morphological_validator.py",\n    "tests/test_navsi200_asr_bakeoff.py",\n    "tests/test_navsi200_captions.py",\n    "tests/test_numeral_mechanics_engine.py",\n    "tests/test_ohoiko_paired_headword_split.py",\n    "tests/test_practice_quality_gate.py",\n    "tests/test_pronoun_mechanics_engine.py",\n    "tests/test_reattribute_ukrlib.py",\n    "tests/test_sources_db.py",\n    "tests/test_verb_mechanics_engine.py",\n    "tests/test_vesum_heritage_attestation.py",\n    "tests/test_vocab_gen.py",\n    "tests/wiki/diagnostics/test_corpus_gaps.py",\n    "tests/wiki/test_grade_filter.py",\n    "tests/wiki/test_t1_t2_pipeline.py",\n    "tests/verification/test_antonenko_patterns.py"\n  ],\n  "bulk_nodeids": [],\n  "nightly_policy": {\n    "issue": 9256,\n    "documentation": "docs/ci/data-tier-selection.md",\n    "accepted_skip_reasons": [],\n    "removed_selection_entries": 429,\n    "original_selection_entries": 556,\n    "data_policy": "Existing repository files and local stores only; no hydration."\n  }\n}\n', 'data_tier_known_failures.json': '{\n  "schema": 1,\n  "known_failures": {}\n}\n'}
