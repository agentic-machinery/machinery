"""The release cut's pure decisions, notes and X-Agent trailer; its umask for created paths and its alert's plane root."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery import release_cut
from lu_machinery.agent_runtime.adapters.acpx import _check_node_artifact
from lu_machinery.ci import check_agent_attribution as attribution
from lu_machinery.common import repo_root
from lu_machinery.dispatcher import git as git_module
from lu_machinery.release_cut import Cut, CutError, main, preflight, record_changelog, release_notes

HEAD = "a" * 40
TAG_COMMIT = "b" * 40
NEXT = {"pyproject.toml": "0.1.22", "package.json": "0.1.22", "package-lock.json": "0.1.22",
        'package-lock.json packages[""]': "0.1.22"}
GREEN = [{"headSha": HEAD, "event": "push", "status": "completed", "conclusion": "success", "databaseId": 7}]
TITLES = ["fix(review): count a shared branch's review rounds per pull request (#627)"]
REPO = "learn-ukrainian/machinery"
CHANGELOG = (
    "# Changelog\n\n## [Unreleased]\n\n"
    '<a id="v0.1.21"></a>\n## [0.1.21] - 2026-11-15\n\n### Fixed\n\n- Earlier fix\n\n'
    f"[Unreleased]: https://github.com/{REPO}/compare/v0.1.21...HEAD\n"
    f"[0.1.21]: https://github.com/{REPO}/compare/v0.1.20...v0.1.21\n"
)
HAND_WRITTEN = "### Fixed\n\n- Hand-written line\n\n"


def decide(**changes):
    inputs = {"head": HEAD, "last_tag": "v0.1.21", "last_tag_commit": TAG_COMMIT, "baseline": "v0.1.21", "titles": TITLES,
              "versions": NEXT, "tags": ["v0.1.20", "v0.1.21"], "runs": GREEN, "changelog": CHANGELOG}
    return preflight(**{**inputs, **changes})


@pytest.mark.parametrize("titles", [
    [],
    ["chore(release): record 0.1.21 and start 0.1.22 (#630)"],
    ["chore(release): record 0.1.21 and start 0.1.22 (#630)", "chore(release): record 0.1.21 and start 0.1.22"],
])
def test_unchanged_main_skips_instead_of_cutting_an_empty_release(titles):
    """Defect class: a window with only the version bump merged publishes a release with no change."""
    decision = decide(titles=titles, versions=NEXT, runs=[])
    assert (decision.action, decision.version) == ("skip", "0.1.22")
    assert decision.reason.startswith("unchanged")


def test_head_equal_to_the_last_tag_skips():
    """Defect class: rerunning a window after a release tags the same commit again."""
    assert decide(head=TAG_COMMIT, last_tag_commit=TAG_COMMIT).action == "skip"


def test_a_real_change_beside_the_bump_is_cut():
    """Defect class: the bump PR's exemption hides a real change that merged in the same window."""
    titles = ["chore(release): record 0.1.21 and start 0.1.22 (#630)", *TITLES]
    decision = decide(titles=titles)
    assert (decision.action, decision.version) == ("cut", "0.1.22")


@pytest.mark.parametrize("name", sorted(NEXT))
def test_any_version_file_not_at_the_next_version_refuses(name):
    """Defect class: a tag whose wheel, Node contract or lock claims a different version than the tag."""
    decision = decide(versions={**NEXT, name: "0.1.21"})
    assert decision.action == "refuse"
    assert f"{name} 0.1.21" in decision.reason and "0.1.22" in decision.reason


def test_an_existing_tag_refuses_instead_of_moving_it():
    """Defect class: a published version is rebuilt or its tag moved."""
    decision = decide(tags=["v0.1.21", "v0.1.22"])
    assert decision.action == "refuse"
    assert "v0.1.22 already exists" in decision.reason


@pytest.mark.parametrize(("runs", "expected"), [
    ([], "no CI Gate push run"),
    ([{**GREEN[0], "conclusion": "failure"}], "concluded failure"),
    ([{**GREEN[0], "status": "in_progress", "conclusion": ""}], "is in_progress"),
    ([{**GREEN[0], "headSha": "c" * 40}], "no CI Gate push run"),
    ([{**GREEN[0], "event": "merge_group"}], "no CI Gate push run"),
    ([GREEN[0], {**GREEN[0], "databaseId": 8, "conclusion": "cancelled"}], "concluded cancelled"),
])
def test_ci_gate_not_green_on_the_exact_commit_refuses(runs, expected):
    """Defect class: tagging a commit whose own CI Gate push run did not succeed, or another commit's run."""
    decision = decide(runs=runs)
    assert decision.action == "refuse"
    assert expected in decision.reason


@pytest.mark.parametrize(("mode", "field", "value"), [
    ("full", "", None),
    ("reuse", "", None),
    ("reuse", "receipt", None),
    ("reuse", "receipt.schema", "unknown"),
    ("reuse", "receipt.repository", "other/repo"),
    ("reuse", "receipt.workflow", ".github/workflows/other.yml"),
    ("reuse", "receipt.mode", "skip"),
    ("full", "receipt.source", {}),
    ("reuse", "receipt.target.run_id", 6),
    ("reuse", "receipt.target.run_attempt", 2),
    ("reuse", "receipt.target.commit", "d" * 40),
    ("reuse", "receipt.target.tree", "d" * 40),
    ("reuse", "receipt.source", None),
    ("reuse", "receipt.source.run_id", True),
    ("reuse", "receipt.source.run_attempt", 2),
    ("reuse", "receipt.source.commit", "d" * 40),
    ("reuse", "receipt.source.tree", "d" * 40),
    ("reuse", "source", None),
    ("reuse", "source.conclusion", "failure"),
    ("reuse", "source.status", "in_progress"),
    ("reuse", "source.event", "push"),
    ("reuse", "source.head_branch", "gh-readonly-queue/other/pr-1"),
    ("reuse", "source.path", ".github/workflows/other.yml"),
    ("reuse", "source.head_sha", "d" * 40),
    ("reuse", "source.head_commit.tree_id", "d" * 40),
    ("reuse", "source.repository.full_name", "other/repo"),
    ("reuse", "source.head_repository.full_name", "other/repo"),
    ("reuse", "source_jobs", []),
    ("reuse", "source_jobs.0.conclusion", "failure"),
    ("reuse", "source_jobs.0.status", "queued"),
    ("reuse", "target.conclusion", "failure"),
    ("reuse", "target.run_attempt", 2),
    ("reuse", "target.head_branch", "other"),
    ("reuse", "tree", "d" * 40),
    ("reuse", "commit", "d" * 40),
])
def test_ci_reuse_receipt_rejects_mismatched_evidence(mode, field, value, tmp_path):
    """Defect class: a green push releases an untested tree using missing, stale or foreign CI evidence."""
    tree = "c" * 40
    target = {"id": 7, "run_attempt": 1, "head_sha": HEAD, "head_branch": "main", "event": "push",
              "status": "completed", "conclusion": "success", "path": ".github/workflows/ci-gate.yml",
              "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
              "head_commit": {"tree_id": tree}}
    source = {**target, "id": 5, "event": "merge_group", "head_branch": "gh-readonly-queue/main/pr-1"}
    receipt = {"schema": "ci-gate-evidence.v1", "repository": REPO, "workflow": ".github/workflows/ci-gate.yml",
               "mode": mode, "target": {"run_id": 7, "run_attempt": 1, "commit": HEAD, "tree": tree},
               "source": {"run_id": 5, "run_attempt": 1, "commit": HEAD, "tree": tree} if mode == "reuse" else None}
    inputs = {"receipt": receipt, "repository": REPO, "target": target, "commit": HEAD, "tree": tree, "source": source,
              "source_jobs": [{"name": "CI Gate", "status": "completed", "conclusion": "success"}]}
    if not field:
        assert release_cut.validate_ci_receipt(**inputs) is None
        cut = Cut(work=tmp_path, dry_run=True, machinery_remote="unused", product_remote="unused",
                  machinery_repo=REPO, product_repo="unused", host_python=Path(sys.executable),
                  primary=tmp_path, alert=False, x_agent="codex/test", head=HEAD)
        cut.publisher = lambda *_args: json.dumps(target if _args[-1] == "7" else source)
        cut.git = lambda *_args: tree

        def run(argv, **_kwargs):
            if argv[:3] == ["gh", "run", "download"]:
                assert argv[argv.index("--name") + 1] == "ci-gate-evidence-1"
                destination = Path(argv[argv.index("--dir") + 1])
                destination.mkdir()
                (destination / "ci-gate-evidence.json").write_text(json.dumps(receipt), encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")
            assert argv[:3] == ["gh", "run", "view"] and argv[argv.index("--attempt") + 1] == "1"
            return subprocess.CompletedProcess(argv, 0, json.dumps({"jobs": inputs["source_jobs"]}), "")

        cut.run = run
        cut.verify_ci_receipt(tmp_path, GREEN)
        assert cut.ci == f"; {mode} receipt verified on tree {tree}"
        return
    path = field.split(".")
    parent = inputs
    for key in path[:-1]:
        parent = parent[int(key)] if isinstance(parent, list) else parent[key]
    parent[path[-1]] = value
    with pytest.raises(CutError, match="CI evidence"):
        release_cut.validate_ci_receipt(**inputs)


@pytest.mark.parametrize("opener", [
    "",
    "## [Pending fixes]\n\n",
    '<a id="validation"></a>\n',
    "[ticket]: https://example.invalid/42\n\n",
    '<a id="v0.1.22"></a>\n## [0.1.21] - 2026-11-16\n\n',
])
def test_hand_written_unreleased_lines_refuse_instead_of_joining_the_release(opener):
    """Defect class: lines a worker PR wrote under Unreleased are filed under the next release, as on #655,
    including lines that open with a heading, anchor or link definition that is not a release's start."""
    changelog = CHANGELOG.replace("## [Unreleased]\n\n", "## [Unreleased]\n\n" + opener + HAND_WRITTEN)
    first = (opener or HAND_WRITTEN).splitlines()[0]
    decision = decide(changelog=changelog)
    assert decision.action == "refuse"
    assert f"Unreleased is not empty (starts {first!r})" in decision.reason
    with pytest.raises(CutError, match="Unreleased is not empty"):
        record_changelog(changelog, version="0.1.22", previous="0.1.21", date="2026-11-16",
                         notes="### Fixed\n\n- Generated\n", repo=REPO)


def test_an_empty_unreleased_section_records_the_release_below_it():
    """Defect class: the empty-Unreleased guard changes the generated section, anchor or compare links."""
    assert decide().action == "cut"
    assert record_changelog(CHANGELOG, version="0.1.22", previous="0.1.21", date="2026-11-16",
                            notes="### Fixed\n\n- Generated\n", repo=REPO) == (
        "# Changelog\n\n## [Unreleased]\n\n"
        '<a id="v0.1.22"></a>\n## [0.1.22] - 2026-11-16\n\n### Fixed\n\n- Generated\n\n'
        '<a id="v0.1.21"></a>\n## [0.1.21] - 2026-11-15\n\n### Fixed\n\n- Earlier fix\n\n'
        f"[Unreleased]: https://github.com/{REPO}/compare/v0.1.22...HEAD\n"
        f"[0.1.22]: https://github.com/{REPO}/compare/v0.1.21...v0.1.22\n"
        f"[0.1.21]: https://github.com/{REPO}/compare/v0.1.20...v0.1.21\n"
    )


def test_notes_are_generated_from_every_merged_title_except_the_bump():
    """Defect class: a merged PR is missing from the release notes, as #605 and #572 were."""
    titles = [  # git log order: newest first
        "fix(review): count a shared branch's review rounds per pull request (#627)",
        "chore(release): record 0.1.21 and start 0.1.22 (#626)",
        "feat(api): model value statistics route and page, landing by trailer line scan (#581) (#625)",
        "chore: remove three unused dispatcher, fleet and bridge helpers (#508) (#607)",
        "docs(release): tag the release PR's merge commit and collect consumer tests first (#613)",
        "Plain title without a type",
    ]
    url = "https://github.com/learn-ukrainian/machinery/pull/"
    assert release_notes(titles, "learn-ukrainian/machinery") == (
        "### Added\n\n"
        f"- Model value statistics route and page, landing by trailer line scan (#581) ([#625]({url}625))\n\n"
        "### Changed\n\n"
        "- Plain title without a type\n"
        f"- Tag the release PR's merge commit and collect consumer tests first ([#613]({url}613))\n\n"
        "### Fixed\n\n"
        f"- Count a shared branch's review rounds per pull request ([#627]({url}627))\n\n"
        "### Removed\n\n"
        f"- Remove three unused dispatcher, fleet and bridge helpers (#508) ([#607]({url}607))\n"
    )


def test_missing_x_agent_is_refused(capsys):
    """Defect class: a release command with no X-Agent value writes a commit review cannot attribute."""
    for command in ("cut", "post-release"):
        with pytest.raises(SystemExit) as caught:
            main([command])
        assert caught.value.code == 2
        assert "--x-agent" in capsys.readouterr().err
        for value in ("", "claude", "claude/", "/claude-cto", "Claude/claude-cto"):
            with pytest.raises(SystemExit) as malformed:
                main([command, "--x-agent", value])
            assert malformed.value.code == 2
            assert "x-agent" in capsys.readouterr().err.lower()


@pytest.mark.usefixtures("unconfigured_primary")
def test_unresolvable_x_agent_is_refused_at_parse_time(monkeypatch, capsys):
    """Defect class: syntactically valid release trailers leave the PR author unresolved."""
    monkeypatch.delenv("LU_TASKS_DIR", raising=False)
    monkeypatch.delenv("LU_PRODUCT_ROOT", raising=False)
    monkeypatch.setattr(release_cut, "_run", lambda args: 0)
    for command in ("cut", "post-release"):
        with pytest.raises(SystemExit) as caught:
            main([command, "--x-agent", "claude/claude-cto"])
        assert caught.value.code == 2
        error = capsys.readouterr().err
        assert "claude/claude-cto" in error
        assert "claude/claude-opus-5-5" in error
        assert "author family" in error


@pytest.mark.usefixtures("unconfigured_primary")
def test_resolvable_x_agent_reaches_release_runner(monkeypatch):
    """Defect class: parse-time attribution rejects an explicit model without a checkout."""
    monkeypatch.delenv("LU_TASKS_DIR", raising=False)
    monkeypatch.delenv("LU_PRODUCT_ROOT", raising=False)
    parsed = []

    def run(args):
        parsed.append((args.command, args.x_agent))
        return 0

    monkeypatch.setattr(release_cut, "_locked_run", run)
    for command in ("cut", "post-release"):
        assert main([command, "--x-agent", "claude/claude-opus-5-5"]) == 0
    assert parsed == [("cut", "claude/claude-opus-5-5"), ("post-release", "claude/claude-opus-5-5")]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(path)], check=True, capture_output=True, text=True)
    _git(path, "config", "user.name", "Release Cut")
    _git(path, "config", "user.email", "release-cut@example.invalid")
    _git(path, "add", ".")
    _git(path, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "seed")


def test_concurrent_release_cuts_cannot_overlap(tmp_path):
    """Defect class: concurrent release cuts overlap across worktrees."""
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    _init_repo(primary)
    _git(primary, "worktree", "add", "-b", "worker", str(linked))
    script = """
import sys
from pathlib import Path
from lu_machinery import release_cut
from lu_machinery.dispatcher import git
git.resolve_repo = lambda name=None: (Path(sys.argv[1]), {})
release_cut.x_agent_value = lambda value, **kwargs: value
def run(args):
    print('entered', flush=True)
    sys.stdin.readline()
    return 0
release_cut._run = run
raise SystemExit(release_cut.main(['cut', '--x-agent', 'fixture/worker']))
"""
    first = subprocess.Popen([sys.executable, "-c", script, str(primary)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "entered"
        second = subprocess.run([sys.executable, "-c", script, str(linked)], input="\n", text=True,
                                capture_output=True, timeout=10)
        assert second.returncode != 0
        assert "release-cut.lock" in second.stderr
        first.kill()
        assert first.wait(timeout=10) != 0
        third = subprocess.run([sys.executable, "-c", script, str(linked)], input="\n", text=True,
                               capture_output=True, timeout=10)
        assert third.returncode == 0
        assert third.stdout.strip() == "entered"
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()


def _bare_checkout(seed, bare, checkout):
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "--quiet", str(bare), str(checkout)], check=True, capture_output=True, text=True)


def test_release_shallow_clone_cannot_truncate_callers_linked_worktree_history(tmp_path, monkeypatch):
    """Defect class: inherited GIT_COMMON_DIR sends a disposable clone's shallow writes to the caller."""
    primary, remote, linked, work = (tmp_path / name for name in ("primary", "remote.git", "linked", "cut"))
    _init_repo(primary)
    _git(primary, "commit", "--allow-empty", "-m", "base")
    _git(primary, "worktree", "add", "-b", "worker", str(linked))
    _git(primary, "clone", "--bare", str(primary), str(remote))
    work.mkdir()
    cut = Cut(work=work, dry_run=False, machinery_remote=str(remote), product_remote=remote.as_uri(),
              machinery_repo="fixture/machinery", product_repo="fixture/product", host_python=tmp_path / "python",
              primary=primary, alert=False, x_agent="codex/fixture")
    common = primary / ".git"
    config = (common / "config").read_bytes()
    refs = git_module._verification_git(primary, "show-ref")
    head = git_module._verification_git(primary, "rev-parse", "HEAD")
    assert git_module._verification_git(primary, "rev-parse", "--is-shallow-repository") == "false"
    with monkeypatch.context() as caller:
        caller.setenv("GIT_COMMON_DIR", str(common))
        caller.chdir(linked)
        cut.run([git_module.GIT_BINARY, "clone", "--quiet", "--depth", "1", "--branch", "main",
                 remote.as_uri(), work / "product"])
    assert git_module._verification_git(primary, "rev-parse", "--is-shallow-repository") == "false"
    assert git_module._verification_git(linked, "rev-parse", "--is-shallow-repository") == "false"
    assert not (common / "shallow").exists()
    assert (common / "config").read_bytes() == config
    assert git_module._verification_git(primary, "show-ref") == refs
    assert git_module._verification_git(primary, "rev-parse", "HEAD") == head
    clone = work / "product"
    assert git_module._verification_git(clone, "rev-parse", "--is-shallow-repository") == "true"
    assert git_module._verification_git(clone, "rev-parse", "--git-common-dir") == ".git"
    assert (clone / ".git/shallow").read_text().strip() == head


def _assert_declared(message: str, agent: str) -> None:
    """The message declares exactly this X-Agent and one valid X-Verified line, as CI reads it."""
    assert attribution.extract_attribution(message) == attribution.Attribution((agent,), 0)
    assert attribution.classify_verification(message) == "present"
    assert attribution.verification_findings(message, "0" * 40, None) == []


def _stub_adopt_input_refresh(cut, monkeypatch):
    """Supply files to stage when the test exercises PR publication independently of uv."""

    def refresh(product):
        (product / "pyproject.toml").write_text('[project]\nname = "product"\nversion = "1.0.0"\n', encoding="utf-8")
        (product / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    monkeypatch.setattr(cut, "update_adopt_inputs", refresh)


def test_created_commit_message_declares_x_agent_and_x_verified(tmp_path, monkeypatch):
    """Defect classes: missing attribution or MISSING_VERIFICATION on release PRs, stale locks and credentials in uv subprocesses."""
    machinery_seed = tmp_path / "machinery-seed"
    _init_repo(machinery_seed)
    (machinery_seed / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "[Unreleased]: https://github.com/learn-ukrainian/machinery/compare/v0.1.21...HEAD\n",
        encoding="utf-8",
    )
    (machinery_seed / "pyproject.toml").write_text('[project]\nname = "lu-machinery"\nversion = "0.1.22"\n', encoding="utf-8")
    uv_lock = 'version = 1\n\n[[package]]\nname = "lu-machinery"\nversion = "0.1.22"\n'
    (machinery_seed / "uv.lock").write_text(uv_lock, encoding="utf-8")
    (machinery_seed / "package.json").write_text(
        json.dumps({"name": "lu-machinery-acp", "version": "0.1.22"}, indent=2) + "\n", encoding="utf-8",
    )
    (machinery_seed / "package-lock.json").write_text(
        json.dumps({"name": "lu-machinery-acp", "version": "0.1.22", "packages": {"": {"version": "0.1.22"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(machinery_seed, "add", "CHANGELOG.md", "pyproject.toml", "package.json", "package-lock.json", "uv.lock")
    _git(machinery_seed, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "version files")

    product_seed = tmp_path / "product-seed"
    _init_repo(product_seed)
    work = tmp_path / "work"
    work.mkdir()
    machinery = work / "machinery"
    product = work / "product"
    _bare_checkout(machinery_seed, tmp_path / "machinery.git", machinery)
    _bare_checkout(product_seed, tmp_path / "product.git", product)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        '[ "$HOME" = "${PWD%/*}/home" ]\n'
        '[ -z "${GH_TOKEN+x}${ANTHROPIC_API_KEY+x}${UV_EXCLUDE_NEWER+x}" ]\n'
        '[ "$#" -eq 4 ]\n'
        '[ "$1 $2 $3" = "lock --no-python-downloads --python" ]\n'
        f'[ "$4" = {shlex.quote(sys.executable)} ]\n'
        "grep -Fqx 'version = \"0.1.23\"' pyproject.toml\n"
        "sed 's/^version = \"0.1.22\"$/version = \"0.1.23\"/' uv.lock > next.lock\n"
        "mv next.lock uv.lock\n",
        encoding="utf-8",
    )
    uv.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", "fixture-gh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-provider")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "2000-01-01")
    cut = Cut(
        work=work, dry_run=False, machinery_remote=str(tmp_path / "machinery.git"),
        product_remote=str(tmp_path / "product.git"), machinery_repo="learn-ukrainian/machinery",
        product_repo="learn-ukrainian/learn-ukrainian", host_python=Path(sys.executable), primary=tmp_path,
        alert=False, x_agent="claude/claude-cto", version="0.1.22", previous="0.1.21", baseline="0.1.21",
        git_identity=("-c", "user.name=Release Cut", "-c", "user.email=release-cut@example.invalid"),
    )
    # The host pre-push scan refuses a fixture that is not the public repository. The commit is what this test reads.
    original_run = cut.run

    def run(argv, **kwargs):
        if "push" in [str(part) for part in argv]:
            assert kwargs.get("env") is None
            assert cut.env()["GH_TOKEN"] == "fixture-gh"
            return subprocess.CompletedProcess([], 0, "", "")
        return original_run(argv, **kwargs)

    cut.run = run
    cut.publisher = lambda *_args: "https://github.com/learn-ukrainian/machinery/pull/633\n"
    assert cut.post_release("### Changed\n\n- Note\n") == 633
    post = subprocess.run(
        ["git", "-C", str(machinery), "log", "-1", "--format=%B"], check=True, capture_output=True, text=True,
    ).stdout
    _assert_declared(post, "claude/claude-cto")
    assert cut.git(machinery, "show", "HEAD:uv.lock") == uv_lock.replace("0.1.22", "0.1.23").strip()

    cut.publisher = lambda *_args: "https://github.com/learn-ukrainian/learn-ukrainian/pull/1\n"
    cut.version = "0.1.23"
    base_url = "https://github.com/learn-ukrainian/machinery/releases/download"
    old_url = f"{base_url}/v0.1.22/lu_machinery-0.1.22-py3-none-any.whl"
    new_url = f"{base_url}/v0.1.23/lu_machinery-0.1.23-py3-none-any.whl"
    manifest = product / "pyproject.toml"
    old_manifest = ('[project]\nname = "product"\nversion = "1.0.0"\n'
                    'dependencies = ["lu-machinery[api]==0.1.22"]\n'
                    '[dependency-groups]\nmachinery = ["lu-machinery"]\n'
                    '[tool.uv]\nexclude-newer = "7 days"\n'
                    'exclude-newer-package = { another-package = "2026-11-20T12:00:00Z" }\n'
                    f'[tool.uv.sources]\nlu-machinery = {{ url = "{old_url}" }}\n')
    manifest.write_text(old_manifest, encoding="utf-8")
    (product / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    cut.git(product, "add", "pyproject.toml", "uv.lock")
    cut.git(product, *cut.committer(), "commit", "--quiet", "-m", "fixture: use uv")
    published = work / "published"
    published.mkdir()
    payload = b"published release wheel"
    (published / "lu_machinery-0.1.23-py3-none-any.whl").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    def run_uv_adoption(argv, **kwargs):
        if argv[0] == str(uv):
            assert argv[1:] == ["lock", "--upgrade-package", "lu-machinery", "--exclude-newer-package",
                                "lu-machinery=false", "--no-python-downloads", "--python", Path(sys.executable)]
            assert kwargs["cwd"] == product
            assert kwargs["env"]["HOME"] == str(work / "home")
            assert {"GH_TOKEN", "ANTHROPIC_API_KEY", "UV_EXCLUDE_NEWER"}.isdisjoint(kwargs["env"])
            expected = old_manifest.replace("0.1.22", "0.1.23").replace(
                "{ another-package", "{ lu-machinery = false, another-package")
            assert manifest.read_text(encoding="utf-8") == expected
            (product / "uv.lock").write_text(
                'version = 1\n[[package]]\nname = "lu-machinery"\nversion = "0.1.23"\n'
                f'source = {{ url = "{new_url}" }}\nwheels = [{{ url = "{new_url}", hash = "sha256:{digest}" }}]\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run(argv, **kwargs)

    cut.run = run_uv_adoption
    cut.open_adopt_pr(product)
    assert cut.git(product, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == [
        "pyproject.toml", "uv.lock",
    ]
    assert f"sha256:{digest}" in cut.git(product, "show", "HEAD:uv.lock")
    _assert_declared(cut.git(product, "log", "-1", "--format=%B"), "claude/claude-cto")
    (product / "uv.lock").write_text('package = "invalid"\n', encoding="utf-8")
    with pytest.raises(CutError, match="uv.lock package must be an array of tables"):
        release_cut.wheel_only_requirements("", product / "uv.lock")
    manifest.write_text("tool = []\n", encoding="utf-8")
    with pytest.raises(CutError, match="pyproject.toml tool must be a table"):
        cut.update_adopt_inputs(product)


def test_adopt_pr_commits_candidate_snapshot(tmp_path, monkeypatch):
    """Defect class: the adoption commit updates the wheel pin and leaves a stale generated snapshot."""
    product = tmp_path / "product"
    _init_repo(product)
    release_base = f"https://github.com/{REPO}/releases/download"
    old_url = f"{release_base}/v0.1.22/lu_machinery-0.1.22-py3-none-any.whl"
    wheel_name = "lu_machinery-0.1.23-py3-none-any.whl"
    new_url = f"{release_base}/v0.1.23/{wheel_name}"
    (product / "pyproject.toml").write_text(
        '[project]\nname = "example-product"\nversion = "0.1.0"\n'
        'dependencies = ["lu-machinery==0.1.22"]\n'
        f'[tool.uv.sources]\nlu-machinery = {{ url = "{old_url}" }}\n'
        '[tool.lu-machinery.adoption-snapshot]\nmodule = "route_snapshot"\noutput = "snapshot.json"\n',
        encoding="utf-8",
    )
    (product / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    old_snapshot = '{"routes": ["GET /before"]}\n'
    (product / "snapshot.json").write_text(old_snapshot, encoding="utf-8")
    (product / "route_snapshot.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "from lu_machinery import ROUTES\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "assert os.environ['LU_PRODUCT_ROOT'] == str(Path.cwd())\n"
        "print(json.dumps({'routes': ROUTES}))\n",
        encoding="utf-8",
    )
    _git(product, "add", ".")
    _git(product, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "product snapshot at old pin")

    published = tmp_path / "published"
    published.mkdir()
    (published / wheel_name).write_bytes(b"candidate release fixture")
    wheel_digest = hashlib.sha256(b"candidate release fixture").hexdigest()
    lock = tmp_path / "consumer.lock"
    lock.write_text("lu-machinery==0.1.23\n", encoding="utf-8")
    host_python = Path(sys.executable)
    cut = Cut(
        work=tmp_path, dry_run=False, machinery_remote="", product_remote="", machinery_repo=REPO,
        product_repo="example/product", host_python=host_python, primary=tmp_path,
        alert=False, x_agent="claude/claude-cto", version="0.1.23", built="0.1.23",
        git_identity=("-c", "user.name=Release Cut", "-c", "user.email=release-cut@example.invalid"),
        publisher_python=host_python,
    )
    cut.uv = tmp_path / "uv"
    monkeypatch.setenv("GH_TOKEN", "fixture-token")

    def install_candidate(venv, consumer_lock, version):
        assert (consumer_lock, version) == (lock, "0.1.23")
        cut.run([host_python, "-I", "-m", "venv", "--without-pip", venv])
        candidate = venv / "bin/python"
        site = Path(cut.run([candidate, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip())
        (site / "lu_machinery").mkdir()
        (site / "lu_machinery/__init__.py").write_text("ROUTES = ['GET /before', 'GET /after']\n", encoding="utf-8")
        metadata = site / "lu_machinery-0.1.23.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: lu-machinery\nVersion: 0.1.23\n", encoding="utf-8")
        return candidate

    run = cut.run
    pushed: list[str] = []

    def run_adoption(argv, **kwargs):
        if argv[0] == cut.uv:
            (product / "uv.lock").write_text(
                'version = 1\n[[package]]\nname = "lu-machinery"\nversion = "0.1.23"\n'
                f'source = {{ url = "{new_url}" }}\nwheels = [{{ url = "{new_url}", hash = "sha256:{wheel_digest}" }}]\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["git", "-C", product] and argv[3] == "push":
            pushed.append(cut.git(product, "rev-parse", "HEAD"))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run(argv, **kwargs)

    cut.run = run_adoption
    cut._install_venv = install_candidate
    cut.publisher = lambda *args: "https://github.com/example/product/pull/701\n"
    cut.open_adopt_pr(product)

    assert cut.adopt_pr == 701
    assert len(pushed) == 1
    assert cut.git(product, "show", f"{pushed[0]}^:snapshot.json") == old_snapshot.strip()
    assert json.loads(cut.git(product, "show", f"{pushed[0]}:snapshot.json")) == {"routes": ["GET /before", "GET /after"]}
    assert cut.git(product, "diff-tree", "--no-commit-id", "--name-only", "-r", pushed[0]).splitlines() == [
        "pyproject.toml", "snapshot.json", "uv.lock",
    ]


def test_annotated_tag_uses_the_checkout_identity_when_the_host_has_none(tmp_path, monkeypatch):
    """Defect class: the annotated tag ignores the checkout identity and fails after the wheel build."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE", "EMAIL"):
        monkeypatch.delenv(key, raising=False)
    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "machinery.git"
    clone = work / "machinery"
    _bare_checkout(checkout, bare, clone)
    cut = Cut(work=work, dry_run=False, machinery_remote=str(bare), product_remote=str(tmp_path / "product.git"),
              machinery_repo=REPO, product_repo="learn-ukrainian/learn-ukrainian", host_python=tmp_path / "python",
              primary=checkout, alert=False, x_agent="claude/claude-cto")
    name, email = (cut.git(checkout, "config", "--get", key, check=False) for key in ("user.name", "user.email"))
    assert (name, email) == ("Release Cut", "release-cut@example.invalid")
    assert cut.git(clone, "config", "--get", "user.name", check=False) == ""
    assert cut.git(clone, "config", "--get", "user.email", check=False) == ""
    cut.git_identity = ("-c", f"user.name={name}", "-c", f"user.email={email}")
    cut.head = cut.git(clone, "rev-parse", "HEAD")
    cut.version = "0.1.22"
    dist = work / "dist"
    dist.mkdir()
    wheel = dist / "lu_machinery-0.1.22-py3-none-any.whl"
    source = dist / "lu_machinery-0.1.22.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"sdist")
    sums = dist / "SHA256SUMS"
    sums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (wheel, source)),
                    encoding="utf-8")
    files = {"wheel": wheel, "sdist": source, "sums": sums}
    original_run = cut.run

    def run(argv, **kwargs):
        command = [str(part) for part in argv]
        # The host pre-push scan refuses a fixture that is not the public repository. The tag is what this test reads.
        if "push" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["gh", "release"]:
            published = Path(command[command.index("-D") + 1])
            published.mkdir()
            for path in files.values():
                (published / path.name).write_bytes(path.read_bytes())
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(argv, **kwargs)

    cut.run = run
    cut.publisher = lambda *_args: ""
    cut.publish(files, "notes\n")
    expected = f"{name}|<{email}>"
    assert cut.git(clone, "cat-file", "-t", "refs/tags/v0.1.22") == "tag"
    assert cut.git(clone, "rev-parse", "refs/tags/v0.1.22^{commit}") == cut.head
    assert cut.git(clone, "for-each-ref", "--format=%(taggername)|%(taggeremail)", "refs/tags/v0.1.22") == expected
    refused_work = tmp_path / "refuse"
    refused_work.mkdir()
    refused = Cut(work=refused_work, dry_run=True, machinery_remote=str(tmp_path / "missing.git"),
                  product_remote=str(tmp_path / "missing-product.git"), machinery_repo=REPO,
                  product_repo="learn-ukrainian/learn-ukrainian", host_python=tmp_path / "python",
                  primary=checkout, alert=False, x_agent="claude/claude-cto")
    with pytest.raises(CutError, match=r"^no git user\.name and user\.email in the registered machinery checkout$"):
        refused.preflight()
    assert refused.step == "preflight"
    assert list(refused_work.iterdir()) == []


def _released_history(tmp_path, monkeypatch, commits, releases, *, machinery_uv="==0.13.9", product_uv="==0.13.9"):
    """A machinery remote whose first-parent history is ``commits`` ((title, version, tag)), and a dry-run Cut on it.

    GitHub is faked: ``releases`` maps a tag to its release's isPrerelease, and HEAD's CI Gate push run succeeded.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    for title, version, tag in commits:
        (seed / "pyproject.toml").write_text(
            f'[project]\nname = "lu-machinery"\nversion = "{version}"\n\n[tool.uv]\nrequired-version = "{machinery_uv}"\n',
            encoding="utf-8",
        )
        package = {"name": "lu-machinery-acp", "version": version}
        (seed / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
        lock = {**package, "packages": {"": {"version": version}}}
        (seed / "package-lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", title)
        if tag:
            _git(seed, "tag", tag)
    remote = tmp_path / "machinery.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(remote)], check=True, capture_output=True)
    product = tmp_path / "product seed"
    _init_repo(product)
    (product / "pyproject.toml").write_text(f'[tool.uv]\nrequired-version = "{product_uv}"\n', encoding="utf-8")
    (product / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(product, "add", "pyproject.toml", "uv.lock")
    _git(product, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Set required uv version")
    bin_dir = tmp_path / "tool bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\n[ "$#" = 5 ] && [ "$1" = --offline ] && [ "$2" = --config-file ] '
        '&& [ "$4" = cache ] && [ "$5" = dir ] || exit 2\n'
        'case "$(cat "$3")" in\n'
        '  \'required-version = "==0.13.9"\'|\'required-version = ">=0.13.8,<0.14"\') exit 0;;\n'
        '  *) exit 2;;\n'
        'esac\n',
        encoding="utf-8",
    )
    uv.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
    head = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    work = tmp_path / "work"
    work.mkdir()
    cut = Cut(work=work, dry_run=True, machinery_remote=str(remote), product_remote=str(product),
              machinery_repo="learn-ukrainian/machinery", product_repo="learn-ukrainian/learn-ukrainian",
              host_python=tmp_path / "python", primary=tmp_path, alert=False, x_agent="claude/claude-cto",
              git_identity=("-c", "user.name=Release Cut", "-c", "user.email=release-cut@example.invalid"))
    original_run = cut.run
    tree = cut.git(seed, "rev-parse", "HEAD^{tree}")
    target = {"id": 7, "run_attempt": 1, "head_sha": head, "head_branch": "main", "event": "push",
              "status": "completed", "conclusion": "success", "path": ".github/workflows/ci-gate.yml",
              "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
              "head_commit": {"tree_id": tree}}
    cut.publisher = lambda *_args: json.dumps(target)


    def run(argv, **kwargs):
        command = [str(part) for part in argv]
        if command[:3] == ["gh", "run", "list"]:
            return subprocess.CompletedProcess(command, 0, json.dumps([{**GREEN[0], "headSha": head}]), "")
        if command[:3] == ["gh", "run", "download"]:
            destination = Path(command[command.index("--dir") + 1])
            destination.mkdir()
            (destination / "ci-gate-evidence.json").write_text(json.dumps({
                "schema": "ci-gate-evidence.v1", "repository": REPO, "workflow": ".github/workflows/ci-gate.yml",
                "mode": "full", "target": {"run_id": 7, "run_attempt": 1, "commit": head, "tree": tree},
                "source": None,
            }), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "release", "view"]:
            if command[3] not in releases:
                return subprocess.CompletedProcess(command, 1, "", "release not found\n")
            return subprocess.CompletedProcess(command, 0, json.dumps({"isPrerelease": releases[command[3]]}), "")
        return original_run(argv, **kwargs)

    cut.run = run
    return cut


@pytest.mark.parametrize(("machinery_uv", "product_uv", "mismatched_side"), [
    ("==0.13.9", ">=0.13.8,<0.14", None),
    ("==0.13.8", "==0.13.9", "machinery"),
    ("==0.13.9", "==0.13.8", "product"),
])
def test_cut_checks_both_uv_requirements_before_build(tmp_path, monkeypatch, machinery_uv, product_uv, mismatched_side):
    """Defect class: missing or incompatible uv inputs allow a cut to reach build or tagging."""
    cut = _released_history(tmp_path, monkeypatch, [
        ("seed versions", "0.1.21", "v0.1.21"),
        ("fix(release): check uv in preflight", "0.1.22", None),
    ], releases={"v0.1.21": False}, machinery_uv=machinery_uv, product_uv=product_uv)

    def unexpected_step(*_):
        pytest.fail("preflight allowed build or publication without the required uv inputs")

    monkeypatch.setattr(cut, "build", unexpected_step)
    monkeypatch.setattr(cut, "publish", unexpected_step)
    if mismatched_side is None:
        decision, _, _ = cut.preflight()
        assert decision.action == "cut"
        assert (cut.work / "product" / "pyproject.toml").is_file()

        product_seed = Path(cut.product_remote)
        _git(product_seed, "rm", "uv.lock")
        _git(product_seed, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Remove the lock")
        cut.work = tmp_path / "missing-lock-cut"
        cut.work.mkdir()
        with pytest.raises(release_cut.UvPreflightError, match=r"uv preflight \(product\): uv.lock is required"):
            release_cut.execute(cut)
        assert cut.step == "preflight"
    else:
        cut.dry_run = mismatched_side == "product"
        with pytest.raises(CutError, match=rf"uv preflight \({mismatched_side}\): uv on PATH could not satisfy") as exc:
            release_cut.execute(cut)
        assert isinstance(exc.value, release_cut.UvPreflightError)
        assert exc.value.side == mismatched_side
        assert cut.step == "preflight"


def test_a_withdrawn_last_tag_cuts_everything_merged_since_the_last_full_release(tmp_path, monkeypatch):
    """Defect class: after a withdrawn pre-release the skip rule and notes start at its tag, so changes never ship."""
    cut = _released_history(tmp_path, monkeypatch, [
        ("seed versions", "0.1.22", "v0.1.22"),
        ("chore(release): record 0.1.22 and start 0.1.23 (#630)", "0.1.23", None),
        ("fix(dispatcher): an unreadable stranger no longer voids the fallback settlement stop (#632)", "0.1.23", None),
        ("fix(api): name the model-stats store without a transcript filename (#641)", "0.1.23", "v0.1.23"),
        ("chore(release): record 0.1.23 and start 0.1.24 (#644)", "0.1.24", None),
    ], releases={"v0.1.22": False, "v0.1.23": True})
    decision, titles, built = cut.preflight()
    assert (decision.action, decision.version, built) == ("cut", "0.1.24", "0.1.24")
    assert decision.reason.endswith("4 commit(s) since v0.1.22")
    assert (cut.previous, cut.baseline) == ("0.1.23", "0.1.22")
    url = "https://github.com/learn-ukrainian/machinery/pull/"
    assert release_notes(titles, "learn-ukrainian/machinery") == (
        "### Fixed\n\n"
        f"- An unreadable stranger no longer voids the fallback settlement stop ([#632]({url}632))\n"
        f"- Name the model-stats store without a transcript filename ([#641]({url}641))\n"
    )


def test_only_a_bump_since_a_full_release_still_skips(tmp_path, monkeypatch):
    """Defect class: the withdrawn-release walk turns an unchanged window after a full release into an empty cut."""
    cut = _released_history(tmp_path, monkeypatch, [
        ("seed versions", "0.1.21", "v0.1.21"),
        ("fix(review): count a shared branch's review rounds per pull request (#627)", "0.1.22", "v0.1.22"),
        ("chore(release): record 0.1.22 and start 0.1.23 (#630)", "0.1.23", None),
    ], releases={"v0.1.21": False, "v0.1.22": False})
    decision, titles, _built = cut.preflight()
    assert (decision.action, decision.version) == ("skip", "0.1.23")
    assert decision.reason == "unchanged: nothing but the version bump merged since v0.1.22"
    assert titles == ["chore(release): record 0.1.22 and start 0.1.23 (#630)"]


def test_consumer_collection_names_the_candidate_interpreter(monkeypatch, tmp_path):
    """Defect class: collection inherits the caller's LEARN_UKRAINIAN_PYTHON; unset, 43 product modules fail to import."""
    monkeypatch.delenv("LEARN_UKRAINIAN_PYTHON", raising=False)
    site, product, candidate = tmp_path / "site", tmp_path / "product", tmp_path / "accept/bin/python"
    site.mkdir()
    cut = Cut(work=tmp_path, dry_run=True, machinery_remote="", product_remote="", machinery_repo="learn-ukrainian/machinery",
              product_repo="learn-ukrainian/learn-ukrainian", host_python=tmp_path / "python", primary=tmp_path,
              alert=False, x_agent="claude/claude-cto", built="0.1.24")
    collected: list[dict[str, str]] = []

    def run(argv, **kwargs):
        command = [str(part) for part in argv]
        stdout = {release_cut._SITE_PATHS: f"{tmp_path / 'host-site'}\n", release_cut._SHOW_LU: f"0.1.24\n{site}/lu_machinery/__init__.py\n"}
        if "pytest" in command:
            collected.append(kwargs["env"])
            return subprocess.CompletedProcess(command, 0, "44149 tests collected\n", "")
        if "purelib" in command[-1]:
            return subprocess.CompletedProcess(command, 0, f"{site}\n", "")
        return subprocess.CompletedProcess(command, 0, stdout.get(command[-1], "d" * 40 + "\n"), "")

    cut.run = run
    assert cut.consumer_collection(candidate, product) == "44149 tests collected"
    assert [(env["LEARN_UKRAINIAN_PYTHON"], env["LU_PRODUCT_ROOT"]) for env in collected] == [(str(candidate), str(product))]


@pytest.fixture
def group_writable_umask():
    """The host's umask: 0002, under which the first 0.1.23 dry run wrote a 664 .nvmrc."""
    previous = os.umask(0o002)
    yield
    os.umask(previous)


def _host(monkeypatch, tmp_path, interpreter):
    """Point main() at a fixture primary checkout and interpreter instead of the registered repositories."""
    primary = tmp_path / "primary"
    _init_repo(primary)
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(git_module, "resolve_repo", lambda name=None: (primary, {"github": f"learn-ukrainian/{name or 'learn-ukrainian'}"}))
    monkeypatch.setattr(repo_root, "primary_root", lambda *args, **kwargs: primary)
    monkeypatch.setattr(repo_root, "project_interpreter", lambda: interpreter)
    return primary


def test_successful_default_work_is_removed_and_log_archived(monkeypatch, tmp_path):
    """Defect class: successful default release work directories accumulate large artifacts."""
    _host(monkeypatch, tmp_path, sys.executable)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def execute(cut):
        cut.version = "1.2.3"
        with (cut.work / "cut.log").open("a") as log:
            log.write("fixture release complete\n")
        (cut.work / "large-artifact").write_text("fixture")
        sealed = cut.work / "accept" / "node"
        sealed.mkdir(parents=True)
        (sealed / "file").write_text("fixture")
        sealed.chmod(0o555)
        sealed.parent.chmod(0o555)
        return 0

    monkeypatch.setattr(release_cut, "execute", execute)
    assert main(["cut", "--dry-run", "--no-alert", "--x-agent", "claude/claude-opus-5-5"]) == 0
    assert not list(tmp_path.glob(".lu-release-cut-*"))
    logs = list((tmp_path / "state" / "lu-release-cut").glob("1.2.3-*.log"))
    assert len(logs) == 1
    assert "fixture release complete" in logs[0].read_text()


# prepare_node_dependencies, in the accept interpreter: parents, the 0755 prefix, then the contract copies.
_PREPARE_PREFIX = (
    "import pathlib, shutil, sys; work = pathlib.Path(sys.argv[1]); contract = work / 'contract'; contract.mkdir(); "
    "(contract / '.nvmrc').write_text('22\\n'); prefix = work / 'accept/share/lu-machinery/node/0.1.23-lock'; "
    "prefix.parent.mkdir(parents=True); prefix.mkdir(mode=0o755); shutil.copyfile(contract / '.nvmrc', prefix / '.nvmrc')"
)


def test_paths_the_cut_creates_are_admitted_under_a_group_writable_umask(monkeypatch, tmp_path, group_writable_umask):
    """Defect classes: writable Node prefixes and ambient credentials reaching the clean install."""
    interpreter = tmp_path / "bin/python"
    interpreter.parent.mkdir()
    # `-I -m venv <path>`: venv copies activate scripts with their source mode, which the umask does not mask.
    interpreter.write_text(
        '#!/bin/sh\nset -eu\n'
        '[ "$HOME" = "$PWD/home" ]\n'
        '[ -z "${GH_TOKEN+x}${ANTHROPIC_API_KEY+x}${XDG_CONFIG_HOME+x}" ]\n'
        'case "$2" in\n'
        '  -m) case "$3" in\n'
        '    venv) mkdir -p "$4/bin"; : > "$4/bin/activate"; chmod 664 "$4/bin/activate"; cp "$0" "$4/bin/python";;\n'
        '    pip) printf "%s\\n" "$4" >> install-commands;;\n'
        '    *) exit 1;;\n'
        '  esac;;\n'
        '  -c) printf "0.1.22 %s/accept/lu_machinery/__init__.py\\n" "$PWD";;\n'
        '  *) exit 1;;\n'
        'esac\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    _host(monkeypatch, tmp_path, interpreter)
    monkeypatch.setenv("GH_TOKEN", "fixture-gh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-provider")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ambient-config"))
    work = tmp_path / "work"

    def execute(cut):
        (work / "consumer.lock").write_text("lu-machinery==0.1.22\n")
        cut.install(work / "consumer.lock", "0.1.22")
        cut.run([sys.executable, "-I", "-c", _PREPARE_PREFIX, cut.work])
        return 0

    monkeypatch.setattr(release_cut, "execute", execute)
    assert main(["cut", "--dry-run", "--no-alert", "--work", str(work), "--x-agent", "claude/claude-opus-5-5"]) == 0
    prefix = work / "accept/share/lu-machinery/node/0.1.23-lock"
    _check_node_artifact(prefix / ".nvmrc")
    for directory in (prefix, prefix.parent):
        _check_node_artifact(directory, directory=True)
    writable = {str(path.relative_to(tmp_path)): oct(path.lstat().st_mode & 0o777)
                for path in (work, *work.rglob("*")) if path.lstat().st_mode & 0o022}
    assert (work / "accept/bin/activate").is_file()
    assert (work / "install-commands").read_text().splitlines() == ["install", "check"]
    assert writable == {}
    assert os.umask(0o002) == 0o002, "main() must restore the caller's umask"


def test_failure_alert_is_published_from_the_primary_checkout(monkeypatch, tmp_path, capsys):
    """Defect class: the failure alert runs in the non-Git work directory, so Fleet Comms refuses to anchor the plane."""
    record = tmp_path / "alert.txt"
    publisher = tmp_path / "bin/python"
    publisher.parent.mkdir()
    # Fleet Comms resolves the plane root from its cwd's checkout and exits 3 outside one.
    publisher.write_text(
        "#!/bin/sh\n"
        f"git rev-parse --show-toplevel > '{record}' || exit 3\n"
        f"printf '%s\\n' \"$*\" >> '{record}'\n"
        f"cat >> '{record}'\n",
        encoding="utf-8",
    )
    publisher.chmod(0o755)
    primary = _host(monkeypatch, tmp_path, publisher)
    monkeypatch.setenv("HOME", str(tmp_path))

    def execute(cut):
        cut.step = "Node provisioning"
        raise CutError("Unsafe Node dependency artifact")

    monkeypatch.setattr(release_cut, "execute", execute)
    assert main(["cut", "--dry-run", "--x-agent", "claude/claude-opus-5-5"]) == 1
    assert len(list(tmp_path.glob(".lu-release-cut-*/cut.log"))) == 1
    assert "alert to claude-cto failed" not in capsys.readouterr().err
    toplevel, argv, body = record.read_text(encoding="utf-8").split("\n", 2)
    assert Path(toplevel).resolve() == primary.resolve()
    assert "channel publish cto - --sender release-cut --source release --recipient claude-cto" in argv
    assert body.startswith("release-cut FAILED (dry run) at Node provisioning: Unsafe Node dependency artifact")


def test_success_reaps_only_rebuildable_scratch_and_failure_preserves_it(monkeypatch, tmp_path):
    """Defect class: finished cuts accumulate scratch, or failed cuts lose diagnostic files."""
    removed = {"accept", "product", "npm-cache", "build-venv", "agent-consumer", "src", "src.tar", "home", "tmp"}
    kept = {"cut.log", "dist", "wheelhouse", "notes-prerelease.md", "notes-release.md",
            "adopt-body.md", "post-release-body.md", "markers.log", "published"}
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("untouched")
    outside.chmod(0o555)
    for scenario in ("success", "acceptance failure", "post-release failure", "interrupted", "dry run"):
        work = tmp_path / scenario
        work.mkdir()
        for name in kept | removed:
            path = work / name
            if name in {"dist", "wheelhouse", "published"} or name in removed - {"src.tar"}:
                leaf = path / "nested"
                leaf.mkdir(parents=True)
                (leaf / "payload").write_text(name)
                (leaf / "outside-link").symlink_to(outside, target_is_directory=True)
                if name in removed:
                    leaf.chmod(0o555)
                    path.chmod(0o555)
            else:
                path.write_text(name)
        sums, lock = work / "dist/SHA256SUMS", work / "wheelhouse/consumer.lock"
        sums.write_text("checksums\n")
        lock.write_text("consumer lock\n")
        cut = Cut(work=work, dry_run=scenario == "dry run", machinery_remote="", product_remote="",
                  machinery_repo=REPO, product_repo="learn-ukrainian/learn-ukrainian", host_python=tmp_path / "python",
                  primary=tmp_path, alert=False, x_agent="codex/cto-45ff-cutclean-sol")
        monkeypatch.setattr(cut, "preflight", lambda: (decide(), TITLES, "0.1.22"))
        monkeypatch.setattr(cut, "build", lambda *_: {"sums": sums})
        monkeypatch.setattr(cut, "wheelhouse", lambda *_: lock)
        monkeypatch.setattr(cut, "install", lambda *_: work / "accept/bin/python")
        monkeypatch.setattr(cut, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""))
        for step in ("publish", "open_adopt_pr", "node_refusals", "node_isolation", "resources", "alert_cto", "promote"):
            monkeypatch.setattr(cut, step, lambda *_a: None)
        monkeypatch.setattr(cut, "node_provision", lambda *_: {
            "prefix": str(work / "accept/node"), "node_version": "22", "npm_version": "10"})
        monkeypatch.setattr(cut, "consumer_collection", lambda *_: "1 test collected")
        monkeypatch.setattr(cut, "contract_canary", lambda: subprocess.CompletedProcess([], 0, "code: PASS", ""))
        monkeypatch.setattr(cut, "post_release", lambda *_: 1)

        def fail(*_):
            raise CutError("fixture failure")

        def interrupt(*_):
            raise KeyboardInterrupt

        if scenario == "acceptance failure":
            monkeypatch.setattr(cut, "resources", fail)
        elif scenario == "post-release failure":
            monkeypatch.setattr(cut, "post_release", fail)
        elif scenario == "interrupted":
            monkeypatch.setattr(cut, "resources", interrupt)
        if "failure" in scenario:
            with pytest.raises(CutError, match="fixture failure"):
                release_cut.execute(cut)
        elif scenario == "interrupted":
            with pytest.raises(KeyboardInterrupt):
                release_cut.execute(cut)
        else:
            assert release_cut.execute(cut) == 0
        expected = kept if scenario == "success" else kept | removed
        if scenario == "dry run":
            expected |= {"notes-dry-run.md"}
        assert {path.name for path in work.iterdir()} == expected
        assert sentinel.read_text() == "untouched"
        assert outside.stat().st_mode & 0o777 == 0o555
        if scenario == "success":
            (work / "unlisted").write_text("retained")
            cut.remove_rebuildable_scratch()
            assert (work / "unlisted").read_text() == "retained"
        else:
            for name in removed - {"src.tar"}:
                assert (work / name / "nested/payload").read_text() == name

    boundary = tmp_path / "boundary"
    boundary.mkdir()
    (boundary / "accept").write_text("retain on refusal")
    (boundary / "product").symlink_to(outside, target_is_directory=True)
    cut.work = boundary
    with pytest.raises(CutError, match="outside the work directory"):
        cut.remove_rebuildable_scratch()
    assert (boundary / "accept").read_text() == "retain on refusal"
    assert sentinel.read_text() == "untouched"
    assert outside.stat().st_mode & 0o777 == 0o555


def test_check_changelog_cli_refuses_occupied_unreleased_and_accepts_empty(tmp_path, capsys):
    """Defect class: a PR that writes under CHANGELOG.md Unreleased passes CI and breaks release preflight, as on #1243."""
    clean = tmp_path / "clean.md"
    clean.write_text(CHANGELOG, encoding="utf-8")
    assert main(["check-changelog", "--path", str(clean)]) == 0
    assert "Unreleased section is empty" in capsys.readouterr().out

    post_release = tmp_path / "post-release.md"
    post_release.write_text(
        record_changelog(CHANGELOG, version="0.1.22", previous="0.1.21", date="2026-11-23",
                         notes="### Fixed\n\n- Generated\n", repo=REPO),
        encoding="utf-8",
    )
    assert main(["check-changelog", "--path", str(post_release)]) == 0
    assert "Unreleased section is empty" in capsys.readouterr().out

    occupied = tmp_path / "occupied.md"
    occupied.write_text(CHANGELOG.replace("## [Unreleased]\n\n", "## [Unreleased]\n\n" + HAND_WRITTEN), encoding="utf-8")
    assert main(["check-changelog", "--path", str(occupied)]) == 1
    assert "Unreleased is not empty" in capsys.readouterr().err

    symlink = tmp_path / "link.md"
    symlink.symlink_to(clean)
    assert main(["check-changelog", "--path", str(symlink)]) == 1
    assert "check-changelog failed" in capsys.readouterr().err

    assert main(["check-changelog", "--path", str(tmp_path / "missing.md")]) == 1
    assert "check-changelog failed" in capsys.readouterr().err

    assert main(["check-changelog", "--path", str(tmp_path)]) == 1
    assert "not a regular file" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [None, "", "   ", 13, True, ["v1"], "line1\nline2", "bad\x00ms"])
def test_release_milestone_setting_accepts_omitted_or_valid_and_refuses_invalid(bad):
    """Defect class: invalid release_milestone config silently falls back to no milestone instead of failing closed."""
    assert release_cut.configured_release_milestone({}, repo_label="machinery") is None
    assert (
        release_cut.configured_release_milestone(
            {"release_milestone": "v0.11.0 - CI and GitHub budget reliability"}, repo_label="machinery",
        )
        == "v0.11.0 - CI and GitHub budget reliability"
    )
    with pytest.raises(CutError, match="MILESTONE_REFUSED"):
        release_cut.configured_release_milestone({"release_milestone": bad}, repo_label="machinery")


def test_release_prs_pass_configured_milestones_and_dry_run_reports_them(tmp_path, monkeypatch, capsys):
    """Defect class: release_cut opens adopt/post-release PRs without the repo's configured release_milestone."""
    from lu_machinery.opsec.prepublish import PublishBlocked
    from lu_machinery.publish import github as publish_github

    machinery_seed = tmp_path / "machinery-seed"
    _init_repo(machinery_seed)
    (machinery_seed / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "[Unreleased]: https://github.com/learn-ukrainian/machinery/compare/v0.1.21...HEAD\n",
        encoding="utf-8",
    )
    (machinery_seed / "pyproject.toml").write_text('[project]\nname = "lu-machinery"\nversion = "0.1.22"\n', encoding="utf-8")
    (machinery_seed / "package.json").write_text(json.dumps({"name": "lu-machinery-acp", "version": "0.1.22"}, indent=2) + "\n", encoding="utf-8")
    (machinery_seed / "package-lock.json").write_text(
        json.dumps({"name": "lu-machinery-acp", "version": "0.1.22", "packages": {"": {"version": "0.1.22"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(machinery_seed, "add", "CHANGELOG.md", "pyproject.toml", "package.json", "package-lock.json")
    _git(machinery_seed, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "version files")

    product_seed = tmp_path / "product-seed"
    _init_repo(product_seed)
    work = tmp_path / "work"
    work.mkdir()
    machinery = work / "machinery"
    product = work / "product"
    _bare_checkout(machinery_seed, tmp_path / "machinery.git", machinery)
    _bare_checkout(product_seed, tmp_path / "product.git", product)

    cut = Cut(
        work=work, dry_run=False, machinery_remote=str(tmp_path / "machinery.git"),
        product_remote=str(tmp_path / "product.git"), machinery_repo=REPO,
        product_repo="learn-ukrainian/learn-ukrainian.github.io", host_python=Path(sys.executable), primary=tmp_path,
        alert=False, x_agent="claude/claude-opus-5-5", version="0.1.22", previous="0.1.21", baseline="0.1.21",
        git_identity=("-c", "user.name=Release Cut", "-c", "user.email=release-cut@example.invalid"),
        machinery_milestone="v0.11.0 - CI and GitHub budget reliability",
        product_milestone="Machinery simplification (product side)",
    )
    original_run = cut.run
    cut.run = lambda argv, **kwargs: (
        subprocess.CompletedProcess([], 0, "", "") if "push" in [str(part) for part in argv] else original_run(argv, **kwargs)
    )
    published_calls: list[tuple[str, ...]] = []

    def fake_publisher(*args: str) -> str:
        published_calls.append(args)
        repo = args[args.index("--repo") + 1]
        return f"https://github.com/{repo}/pull/701\n"

    cut.publisher = fake_publisher

    assert cut.post_release("### Changed\n\n- Note\n") == 701
    assert "--milestone" in published_calls[-1]
    assert published_calls[-1][published_calls[-1].index("--milestone") + 1] == "v0.11.0 - CI and GitHub budget reliability"

    _stub_adopt_input_refresh(cut, monkeypatch)
    cut.open_adopt_pr(product)
    assert "--milestone" in published_calls[-1]
    assert published_calls[-1][published_calls[-1].index("--milestone") + 1] == "Machinery simplification (product side)"

    # Dry-run milestone verification reports resolved milestone and fails closed on invalid milestone.
    def fake_resolve(repo: str, milestone: str, **_kwargs):
        if milestone == "bad-milestone":
            raise PublishBlocked("MILESTONE_REFUSED: milestone 'bad-milestone' does not exist")
        return (13, milestone) if repo == REPO else (9, milestone)

    monkeypatch.setattr(publish_github, "resolve_repo_milestone", fake_resolve)
    cut.product_milestone = None
    cut.report_release_milestones(include_adopt=True)
    out = capsys.readouterr().out
    assert "adopt (learn-ukrainian/learn-ukrainian.github.io)=none" in out
    assert "post-release (learn-ukrainian/machinery)=v0.11.0 - CI and GitHub budget reliability (#13)" in out

    cut.machinery_milestone = "bad-milestone"
    with pytest.raises(CutError, match="MILESTONE_REFUSED"):
        cut.report_release_milestones(include_adopt=True)


def test_run_refuses_invalid_configured_release_milestone(monkeypatch, tmp_path, capsys):
    """Defect class: a malformed release_milestone in fleet_repos.yaml reaches git clone instead of failing fast."""
    primary = tmp_path / "primary"
    _init_repo(primary)
    monkeypatch.setenv("LU_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setattr(
        git_module,
        "resolve_repo",
        lambda name=None: (primary, {"github": f"learn-ukrainian/{name or 'learn-ukrainian'}", "release_milestone": ""}),
    )
    monkeypatch.setattr(repo_root, "primary_root", lambda *args, **kwargs: primary)
    monkeypatch.setattr(repo_root, "project_interpreter", lambda: Path(sys.executable))
    assert main(["cut", "--dry-run", "--no-alert", "--work", str(tmp_path / "w"), "--x-agent", "claude/claude-opus-5-5"]) == 1
    assert "MILESTONE_REFUSED" in capsys.readouterr().err


def test_execute_skips_and_refuses_before_live_milestone_lookup(tmp_path, monkeypatch):
    """Defect class: live milestone lookup runs before preflight skip/refuse, breaking exit-0 skips or masking refusal reasons."""
    from lu_machinery.publish import github as publish_github

    called: list[tuple[str, str]] = []

    def fail_if_called(repo: str, milestone: str, **_kwargs):
        called.append((repo, milestone))
        raise AssertionError("resolve_repo_milestone must not run on skip or non-dry-run refuse")

    monkeypatch.setattr(publish_github, "resolve_repo_milestone", fail_if_called)

    work = tmp_path / "work"
    work.mkdir()
    cut = Cut(
        work=work, dry_run=False, machinery_remote="", product_remote="", machinery_repo=REPO,
        product_repo="learn-ukrainian/learn-ukrainian.github.io", host_python=Path(sys.executable), primary=tmp_path,
        alert=False, x_agent="claude/claude-opus-5-5", machinery_milestone="v0.11.0",
    )

    cut.preflight = lambda: (release_cut.Decision("skip", "no changes since v0.1.21", "0.1.22"), [], "0.1.22")
    assert release_cut.execute(cut) == 0
    assert called == []

    cut.preflight = lambda: (release_cut.Decision("refuse", "CI Gate has not passed", "0.1.22"), [], "0.1.22")
    with pytest.raises(CutError, match="CI Gate has not passed"):
        release_cut.execute(cut)
    assert called == []


def test_publisher_uses_release_interpreter_not_stale_host_python(tmp_path, monkeypatch, capsys):
    """Defect class: release_cut invokes the consumer's stale host_python publisher instead of the release's own
    publisher, failing with OPSEC: invalid publisher arguments when the release passes a new flag (0.1.62 skew)."""
    import lu_machinery

    machinery_seed = tmp_path / "machinery-seed"
    _init_repo(machinery_seed)
    (machinery_seed / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "[Unreleased]: https://github.com/learn-ukrainian/machinery/compare/v0.1.61...HEAD\n",
        encoding="utf-8",
    )
    (machinery_seed / "pyproject.toml").write_text('[project]\nname = "lu-machinery"\nversion = "0.1.62"\n', encoding="utf-8")
    (machinery_seed / "package.json").write_text(
        json.dumps({"name": "lu-machinery-acp", "version": "0.1.62"}, indent=2) + "\n", encoding="utf-8",
    )
    (machinery_seed / "package-lock.json").write_text(
        json.dumps({"name": "lu-machinery-acp", "version": "0.1.62", "packages": {"": {"version": "0.1.62"}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(machinery_seed, "add", "CHANGELOG.md", "pyproject.toml", "package.json", "package-lock.json")
    _git(machinery_seed, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "version files")

    product_seed = tmp_path / "product-seed"
    _init_repo(product_seed)
    work = tmp_path / "work"
    work.mkdir()
    (work / "home").mkdir()
    (work / "tmp").mkdir()
    machinery = work / "machinery"
    product = work / "product"
    _bare_checkout(machinery_seed, tmp_path / "machinery.git", machinery)
    _bare_checkout(product_seed, tmp_path / "product.git", product)

    fleet = tmp_path / "fleet"
    fleet.mkdir()
    primary = fleet / "learn-ukrainian"
    _init_repo(primary)
    sibling_machinery = fleet / "machinery"
    _init_repo(sibling_machinery)
    host_log = tmp_path / "host-publish.log"
    accept_log = tmp_path / "accept-publish.log"
    fail_pytest_marker = work / "fail-pytest"

    # Simulate the new release's verified clean-install interpreter: requires -I, primary env, and accepts --milestone.
    accept_script = (
        "#!/bin/sh\nset -eu\n"
        'venv_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"\n'
        'if [ "${1:-}" = "-I" ] && [ "${2:-}" = "-m" ] && [ "${3:-}" = "pip" ]; then exit 0; fi\n'
        'if [ "${1:-}" = "-I" ] && [ "${2:-}" = "-c" ]; then\n'
        '  case "${3:-}" in\n'
        '    *purelib*) printf "%s/lib/python3.14/site-packages\\n" "$venv_root";;\n'
        '    *) printf "0.1.62 %s/lib/python3.14/site-packages/lu_machinery/__init__.py\\n" "$venv_root";;\n'
        '  esac\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-c" ]; then\n'
        '  printf "0.1.62 %s/lib/python3.14/site-packages/lu_machinery/__init__.py\\n" "$venv_root"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pytest" ]; then\n'
        '  [ -f "$venv_root/lib/python3.14/site-packages/zz-release-cut-consumer.pth" ]\n'
        f'  if [ -e {shlex.quote(str(fail_pytest_marker))} ]; then\n'
        '    printf "1 error during collection\\n"\n'
        "    exit 1\n"
        "  fi\n"
        '  printf "10 tests collected\\n"\n'
        "  exit 0\n"
        "fi\n"
        '[ "${1:-}" = "-I" ] || { printf "missing -I: %s\\n" "$*" >&2; exit 11; }\n'
        '[ "${2:-}" = "-m" ] && [ "${3:-}" = "lu_machinery.publish" ] || { printf "bad module: %s\\n" "$*" >&2; exit 12; }\n'
        f'[ "${{LEARN_UKRAINIAN_PRIMARY_REPO_ROOT:-}}" = {shlex.quote(str(primary))} ] || {{ printf "bad primary=%s\\n" "${{LEARN_UKRAINIAN_PRIMARY_REPO_ROOT:-}}" >&2; exit 14; }}\n'
        f'[ "${{LU_PRODUCT_ROOT:-}}" = {shlex.quote(str(primary))} ] || {{ printf "bad product=%s\\n" "${{LU_PRODUCT_ROOT:-}}" >&2; exit 15; }}\n'
        'if [ -e "$venv_root/lib/python3.14/site-packages/zz-release-cut-consumer.pth" ]; then\n'
        '  printf "contaminated publisher venv: overlay present\\n" >&2; exit 16\n'
        "fi\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(accept_log))}\n'
        'case "${4:-}" in\n'
        '  pr-create) printf "https://github.com/learn-ukrainian/machinery/pull/1275\\n";;\n'
        '  *) ;;\n'
        'esac\n'
    )

    # Simulate the consumer's stale host_python (0.1.61): creates venvs, returns site paths, rejects --milestone.
    host_python = tmp_path / "host-venv/bin/python"
    host_python.parent.mkdir(parents=True)
    accept_template = tmp_path / "accept-python-template"
    accept_template.write_text(accept_script, encoding="utf-8")
    accept_template.chmod(0o755)
    host_python.write_text(
        "#!/bin/sh\nset -eu\n"
        'if [ "${1:-}" = "-I" ] && [ "${2:-}" = "-m" ] && [ "${3:-}" = "venv" ]; then\n'
        '  mkdir -p "$4/bin" "$4/lib/python3.14/site-packages"\n'
        f'  cp {shlex.quote(str(accept_template))} "$4/bin/python"\n'
        '  chmod 755 "$4/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${1:-}" = "-I" ] && [ "${2:-}" = "-c" ]; then\n'
        f'  printf "%s\\n" {shlex.quote(str(tmp_path / "host-site"))}\n'
        "  exit 0\n"
        "fi\n"
        'if [ "${2:-}" = "-m" ] && [ "${3:-}" = "lu_machinery.publish" ]; then\n'
        f'  printf "%s\\n" "$*" >> {shlex.quote(str(host_log))}\n'
        '  case " $* " in\n'
        '    *" --milestone "*) printf "OPSEC: invalid publisher arguments\\n" >&2; exit 2;;\n'
        '  esac\n'
        '  exit 0\n'
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    host_python.chmod(0o755)

    cut = Cut(
        work=work, dry_run=False, machinery_remote=str(tmp_path / "machinery.git"),
        product_remote=str(tmp_path / "product.git"), machinery_repo=REPO,
        product_repo="learn-ukrainian/learn-ukrainian.github.io", host_python=host_python, primary=primary,
        alert=False, x_agent="claude/claude-opus-5-5", version="0.1.62", previous="0.1.61", baseline="0.1.61",
        git_identity=("-c", "user.name=Release Cut", "-c", "user.email=release-cut@example.invalid"),
        machinery_milestone="v0.11.0 - CI and GitHub budget reliability",
        product_milestone="Machinery simplification (product side)",
        built="0.1.62",
    )
    cut.head = cut.git(machinery, "rev-parse", "HEAD")

    dist = work / "dist"
    dist.mkdir()
    wheel = dist / "lu_machinery-0.1.62-py3-none-any.whl"
    sdist = dist / "lu_machinery-0.1.62.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    sums = dist / "SHA256SUMS"
    sums.write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in (wheel, sdist)), encoding="utf-8")
    files = {"wheel": wheel, "sdist": sdist, "sums": sums}
    lock = work / "consumer.lock"
    lock.write_text("lu-machinery==0.1.62\n", encoding="utf-8")

    original_run = cut.run

    def run(argv, **kwargs):
        command = [str(part) for part in argv]
        if command[:2] == ["git", "-C"] and "push" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["gh", "release"]:
            published = Path(command[command.index("-D") + 1])
            published.mkdir()
            for path in files.values():
                (published / path.name).write_bytes(path.read_bytes())
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(argv, **kwargs)

    cut.run = run

    # 1. After install(), every publisher verb in cut runs accept/bin/python -I (never stale host_python).
    installed = cut.install(lock, "0.1.62")
    assert installed == work / "accept/bin/python"
    pub_argv, pub_env = cut.publisher_invocation("pr-create", "--milestone", "v0.11.0")
    assert pub_argv == [installed, "-I", "-m", "lu_machinery.publish", "pr-create", "--milestone", "v0.11.0"]
    assert "PYTHONPATH" not in pub_env
    cut.publish(files, "notes\n")
    _stub_adopt_input_refresh(cut, monkeypatch)
    cut.open_adopt_pr(product)
    cut.promote("final notes\n")
    assert cut.post_release("### Fixed\n\n- Fix\n") == 1275

    assert not host_log.exists(), f"stale host_python must never run lu_machinery.publish: {host_log.read_text()}"
    verbs = [line.split()[3] for line in accept_log.read_text(encoding="utf-8").splitlines()]
    assert verbs == ["release-create", "pr-create", "release-edit", "pr-ready", "pr-create"]
    lines = accept_log.read_text(encoding="utf-8").splitlines()
    assert "--milestone" in lines[1] and "--milestone" in lines[4]

    # Cleanup after full release emits a complete resume command with required roots, LU_PYTHON, --x-agent,
    # --no-alert when alert=False, and a non-leaking --machinery-remote placeholder when a custom remote was used.
    monkeypatch.setattr(cut, "remote_state", lambda: (True, "released", [1275]))
    custom_resume_msg = cut.clean_up("post-release PR")
    quoted_root = shlex.quote(str(primary.absolute()))
    quoted_host_py = shlex.quote(str(host_python.absolute()))
    assert "--no-alert --machinery-remote <original-machinery-remote>" in custom_resume_msg
    assert str(tmp_path / "machinery.git") not in custom_resume_msg
    # With the default GitHub machinery remote, the emitted resume command runs directly in a fresh shell and
    # preserves --no-alert (and omits it when alert=True).
    with monkeypatch.context() as default_remote_ctx:
        default_remote_ctx.setattr(cut, "machinery_remote", f"https://github.com/{REPO}.git")
        resume_msg = cut.clean_up("post-release PR")
        default_remote_ctx.setattr(cut, "alert", True)
        alert_on_msg = cut.clean_up("post-release PR")
    expected_resume = (
        f"LEARN_UKRAINIAN_PRIMARY_REPO_ROOT={quoted_root} LU_PRODUCT_ROOT={quoted_root} "
        f"LU_PYTHON={quoted_host_py} "
        f"{shlex.quote(str(installed))} -I -m lu_machinery.release_cut post-release "
        f"--x-agent claude/claude-opus-5-5 --no-alert"
    )
    assert expected_resume in resume_msg
    assert "--machinery-remote" not in resume_msg
    assert "--no-alert" not in alert_on_msg
    # Execute the emitted env-prefixed command in a fresh shell with no root/interpreter env vars inherited and
    # no <primary>/.venv/bin/python, exercising real main -> _locked_run -> _run (project_interpreter, resolve_repo,
    # milestone parsing, git identity, lock acquisition) and stubbing only the live open_post_release call.
    assert not (primary / ".venv/bin/python").exists()
    probe_home = tmp_path / "probe-home"
    probe_home.mkdir()
    probe_runner = tmp_path / "resume-probe-python"
    pkg_parent = Path(lu_machinery.__file__).resolve().parents[1]
    probe_runner.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(pkg_parent)!r})\n"
        "from pathlib import Path\n"
        "from lu_machinery import release_cut\n"
        "def stub_open_post_release(probe_cut):\n"
        f"    assert probe_cut.primary.resolve() == Path({str(primary)!r}).resolve()\n"
        f"    assert probe_cut.host_python == Path({str(host_python.absolute())!r})\n"
        "    assert probe_cut.x_agent == 'claude/claude-opus-5-5'\n"
        "    assert probe_cut.alert is False\n"
        "    print('RUN_OK', probe_cut.host_python, probe_cut.x_agent, probe_cut.alert)\n"
        "    return 0\n"
        "release_cut.open_post_release = stub_open_post_release\n"
        # The registry's machinery row is dual-slug: the host selector is the operator's, never the test's.
        "from lu_machinery.common import git_credentials\n"
        "git_credentials.load_selector = lambda **_k: git_credentials.SelectorState(\n"
        "    {'machinery': 'learn-ukrainian/machinery', 'infra-private': 'learn-ukrainian/learn-ukrainian-infra-private'},\n"
        "    'd' * 64)\n"
        "raise SystemExit(release_cut.main(sys.argv[4:]))\n",
        encoding="utf-8",
    )
    probe_runner.chmod(0o755)
    emitted_for_probe = expected_resume.replace(shlex.quote(str(installed)), shlex.quote(str(probe_runner)), 1)
    clean_shell_env = {
        key: value for key, value in os.environ.items()
        if key not in {
            "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", "LU_PRIMARY_REPO_ROOT", "LU_PRODUCT_ROOT",
            "LU_PYTHON", "LEARN_UKRAINIAN_PYTHON", "LU_TASKS_DIR",
            "LU_STATE_ROOT", "LEARN_UKRAINIAN_STATE_ROOT", "LEARN_UKRAINIAN_DISPATCH_TASK_ID",
        }
    }
    clean_shell_env["HOME"] = str(probe_home)
    probe_result = subprocess.run(
        ["/bin/sh", "-c", emitted_for_probe], cwd=tmp_path, env=clean_shell_env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert probe_result.returncode == 0, f"emitted resume command failed: {probe_result.stderr}"
    assert f"RUN_OK {host_python.absolute()} claude/claude-opus-5-5 False" in probe_result.stdout
    assert (sibling_machinery / ".git/release-cut.lock").is_file()
    # Negative control (PR1278-F2): omitting LU_PYTHON fails in real _run when primary has no default venv.
    without_lu_python = emitted_for_probe.replace(f"LU_PYTHON={quoted_host_py} ", "", 1)
    fail_probe = subprocess.run(
        ["/bin/sh", "-c", without_lu_python], cwd=tmp_path, env=clean_shell_env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert fail_probe.returncode != 0
    assert "project interpreter not found" in fail_probe.stderr

    # 1b. Failed post_release git push (rejected, TimeoutExpired, or OSError) prints and raises the exact manual
    # publisher pr-create command without reusing or force-updating remote branches; fresh-shell execution opens the PR.
    push_failure: Exception | None = CutError(
        "git -C machinery push exited 1: ! [rejected] HEAD -> release/start-0.1.63 (non-fast-forward)"
    )

    def run_with_push_failure(argv, **kwargs):
        command = [str(part) for part in argv]
        if push_failure is not None and command[:2] == ["git", "-C"] and "push" in command:
            raise push_failure
        return run(argv, **kwargs)

    for failure, expected_detail in (
        (
            CutError("git -C machinery push exited 1: ! [rejected] HEAD -> release/start-0.1.63 (non-fast-forward)"),
            "non-fast-forward",
        ),
        (
            subprocess.TimeoutExpired(["git", "-C", str(machinery), "push"], 300),
            "timed out",
        ),
        (
            FileNotFoundError(2, "No such file or directory", "git"),
            "No such file or directory",
        ),
    ):
        push_failure = failure
        cut.git(machinery, "switch", "--quiet", "main")
        cut.git(machinery, "branch", "-D", "release/start-0.1.63")
        accept_log.write_text("", encoding="utf-8")
        capsys.readouterr()
        with monkeypatch.context() as push_ctx:
            push_ctx.setattr(cut, "run", run_with_push_failure)
            with pytest.raises(CutError) as push_exc:
                cut.post_release("### Fixed\n\n- Fix\n")
        err_text = str(push_exc.value)
        out_text = capsys.readouterr().out
        assert expected_detail in err_text
        assert "Manual recovery:" in err_text
        assert "post-release push failed;" in out_text
        manual_line = err_text.splitlines()[-1].strip()
        assert manual_line in out_text
        assert f"LEARN_UKRAINIAN_PRIMARY_REPO_ROOT={quoted_root}" in manual_line
        assert f"LU_PRODUCT_ROOT={quoted_root}" in manual_line
        assert f"LU_PYTHON={quoted_host_py}" in manual_line
        assert f"--body-file {shlex.quote(str((work / 'post-release-body.md').absolute()))}" in manual_line
        assert (work / "post-release-body.md").is_file()
    manual_result = subprocess.run(
        ["/bin/sh", "-c", manual_line], cwd=tmp_path, env=clean_shell_env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert manual_result.returncode == 0, f"manual pr-create command failed: {manual_result.stderr}"
    assert "/pull/1275" in manual_result.stdout
    assert "--milestone" in accept_log.read_text(encoding="utf-8")
    assert not host_log.exists()
    push_failure = None

    # 2. Cut-time consumer_collection uses a disposable venv under work/tmp, keeping work/accept unmutated for
    # promotion and failure rollback even when overlay write raises ENOSPC or pytest exits nonzero.
    assert cut.consumer_collection(installed, product) == "10 tests collected"
    accept_overlay = work / "accept/lib/python3.14/site-packages/zz-release-cut-consumer.pth"
    assert not accept_overlay.exists()
    collection_dirs = sorted((work / "tmp").glob("collection-*"))
    assert len(collection_dirs) == 1
    assert (collection_dirs[0] / "lib/python3.14/site-packages/zz-release-cut-consumer.pth").is_file()

    # Nonzero pytest --collect-only exit raises CutError and leaves work/accept clean (PR1278-F6).
    fail_pytest_marker.write_text("1", encoding="utf-8")
    try:
        with pytest.raises(CutError, match=r"product test collection failed \(exit 1\): 1 error during collection"):
            cut.consumer_collection(installed, product)
    finally:
        fail_pytest_marker.unlink()
    assert not accept_overlay.exists()

    real_write_text = Path.write_text

    def failing_overlay_write(self_path, data, *args, **kwargs):
        if self_path.name == "zz-release-cut-consumer.pth":
            real_write_text(self_path, data, *args, **kwargs)
            raise OSError(28, "No space left on device")
        return real_write_text(self_path, data, *args, **kwargs)

    with monkeypatch.context() as write_ctx:
        write_ctx.setattr(Path, "write_text", failing_overlay_write)
        with pytest.raises(OSError, match="No space left on device"):
            cut.consumer_collection(installed, product)
    assert not accept_overlay.exists()

    # Rollback after a pre-release failure still runs release-edit, pr-close and yanked post-release on clean accept.
    accept_log.write_text("", encoding="utf-8")
    # Reset machinery branch state so post_release(yanked=True) can create release/start-0.1.63 cleanly.
    cut.git(machinery, "switch", "--quiet", "main")
    cut.git(machinery, "branch", "-D", "release/start-0.1.63")
    monkeypatch.setattr(cut, "remote_state", lambda: (True, "prerelease", [1275]))
    rollback_summary = cut.clean_up("consumer test collection")
    assert "FAILED" not in rollback_summary, rollback_summary
    rollback_verbs = [line.split()[3] for line in accept_log.read_text(encoding="utf-8").splitlines()]
    assert rollback_verbs == ["release-edit", "pr-close", "pr-create"]
    assert not host_log.exists()

    # Standalone consumer_collection (publisher_python=None, as in CI) writes and keeps the overlay in the given venv.
    standalone_site = tmp_path / "standalone-site"
    standalone_site.mkdir()
    standalone_cut = Cut(
        work=tmp_path, dry_run=True, machinery_remote="", product_remote="", machinery_repo=REPO,
        product_repo="learn-ukrainian/learn-ukrainian", host_python=host_python, primary=primary,
        alert=False, x_agent="claude/claude-opus-5-5", built="0.1.62",
    )

    def standalone_run(cmd, **_kwargs):
        parts = [str(part) for part in cmd]
        if "pytest" in parts:
            assert (standalone_site / "zz-release-cut-consumer.pth").is_file()
            return subprocess.CompletedProcess(parts, 0, "10 tests collected\n", "")
        if "purelib" in parts[-1]:
            return subprocess.CompletedProcess(parts, 0, f"{standalone_site}\n", "")
        if parts[-1] == release_cut._SITE_PATHS:
            return subprocess.CompletedProcess(parts, 0, f"{tmp_path / 'host-site'}\n", "")
        if parts[-1] == release_cut._SHOW_LU:
            return subprocess.CompletedProcess(parts, 0, f"0.1.62\n{standalone_site}/lu_machinery/__init__.py\n", "")
        return subprocess.CompletedProcess(parts, 0, "d" * 40 + "\n", "")

    standalone_cut.run = standalone_run
    assert standalone_cut.consumer_collection(installed, product) == "10 tests collected"
    assert (standalone_site / "zz-release-cut-consumer.pth").is_file()

    # 3. Before install() / standalone post-release (publisher_python=None): never falls back to stale host_python.
    cut.publisher_python = None
    monkeypatch.setenv("PYTHONWARNINGS", "error")
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "userbase"))
    # Explicit source-checkout fixture outside site-packages so behavior is identical in wheel-installed CI.
    src_root = tmp_path / "source-checkout"
    src_init = src_root / "lu_machinery/__init__.py"
    src_init.parent.mkdir(parents=True)
    src_init.write_text("", encoding="utf-8")
    with monkeypatch.context() as src_ctx:
        src_ctx.setattr(lu_machinery, "__file__", str(src_init))
        argv, env = cut.publisher_invocation("read", "run", "--repo", REPO, "--number", "7")
        manual_src = cut._manual_post_release_command(["pr-create", "--repo", REPO, "--head", "release/start-0.1.63"])
    assert argv == [Path(sys.executable), "-P", "-s", "-m", "lu_machinery.publish", "read", "run", "--repo", REPO, "--number", "7"]
    assert env["PYTHONPATH"] == str(src_root.resolve())
    assert "PYTHONWARNINGS" not in env and "PYTHONUSERBASE" not in env
    # Source-checkout mode omits a copy-paste shell command and instead prints preserved body path, branch, title,
    # and plain manual steps referencing docs/release.md on push rejection or timeout.
    assert manual_src is None
    for failure, expected_detail in (
        (
            CutError("git -C machinery push exited 1: ! [rejected] HEAD -> release/start-0.1.63 (non-fast-forward)"),
            "non-fast-forward",
        ),
        (
            subprocess.TimeoutExpired(["git", "-C", str(machinery), "push"], 300),
            "timed out",
        ),
    ):
        cut.git(machinery, "switch", "--quiet", "main")
        cut.git(machinery, "branch", "-D", "release/start-0.1.63")
        push_failure = failure
        capsys.readouterr()
        with monkeypatch.context() as src_push_ctx:
            src_push_ctx.setattr(lu_machinery, "__file__", str(src_init))
            src_push_ctx.setattr(cut, "run", run_with_push_failure)
            with pytest.raises(CutError) as src_push_exc:
                cut.post_release("### Fixed\n\n- Fix\n")
        push_failure = None
        src_err = str(src_push_exc.value)
        src_out = capsys.readouterr().out
        assert expected_detail in src_err
        assert "Manual recovery:" in src_err
        assert "post-release push failed;" in src_out
        for expected in (
            f"--repo {shlex.quote(REPO)}",
            "--title 'chore(release): record 0.1.62 and start 0.1.63'",
            f"--body-file {shlex.quote(str((work / 'post-release-body.md').absolute()))}",
            "--base main",
            "--head release/start-0.1.63",
            "docs/release.md",
            f"--milestone {shlex.quote(cut.machinery_milestone)}",
        ):
            assert expected in src_err
            assert expected in src_out
        assert "PYTHONPATH=" not in src_err and "-I -S" not in src_err and "lu_machinery.publish" not in src_err
        assert (work / "post-release-body.md").is_file()

    # Installed venv invocation (e.g. resuming post-release with accept/bin/python -I) uses -I with no PYTHONPATH
    # and still emits an exact copy-paste manual command on push failure.
    fake_site = work / "accept/lib/python3.14/site-packages"
    fake_init = fake_site / "lu_machinery/__init__.py"
    fake_init.parent.mkdir(parents=True, exist_ok=True)
    fake_init.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(installed))
    monkeypatch.setattr(lu_machinery, "__file__", str(fake_init))
    monkeypatch.setattr(release_cut.sysconfig, "get_path", lambda key: str(fake_site) if key in {"purelib", "platlib"} else None)
    argv_installed, env_installed = cut.publisher_invocation("pr-create", "--milestone", "v0.11.0")
    assert argv_installed == [installed, "-I", "-m", "lu_machinery.publish", "pr-create", "--milestone", "v0.11.0"]
    assert "PYTHONPATH" not in env_installed
    manual_installed = cut._manual_post_release_command(["pr-create", "--repo", REPO, "--head", "release/start-0.1.63"])
    assert manual_installed is not None
    assert f"{shlex.quote(str(installed))} -I -m lu_machinery.publish pr-create" in manual_installed
    assert "PYTHONPATH=" not in manual_installed
    # Actually invoke publisher through this post-release resume path while host_python stays stale.
    out = cut.publisher("pr-create", "--repo", REPO, "--milestone", "v0.11.0")
    assert "/pull/1275" in out
    assert not host_log.exists()

    # Fail-closed checks when running interpreter/package metadata is invalid, looping, malformed, or unsafe.
    loop_site = tmp_path / "loop-site"
    loop_site.symlink_to(loop_site)
    with monkeypatch.context() as loop_ctx:
        loop_ctx.setattr(lu_machinery, "__file__", str(src_init))
        loop_ctx.setattr(release_cut.sysconfig, "get_path", lambda key: str(loop_site) if key == "purelib" else None)
        with pytest.raises(CutError, match="cannot resolve running interpreter purelib path"):
            cut.publisher_invocation("pr-create")
    with monkeypatch.context() as loop_ctx:
        loop_ctx.setattr(lu_machinery, "__file__", str(src_init))
        loop_ctx.setattr(release_cut.sysconfig, "get_path", lambda key: str(loop_site / "site-packages") if key == "purelib" else None)
        with pytest.raises(CutError, match="cannot resolve running interpreter purelib path"):
            cut.publisher_invocation("pr-create")
    # Dangling symlink at ancestor (e.g. broken venv/lib -> missing) or leaf must refuse, not fall through.
    dangling_lib = tmp_path / "dangling-lib"
    dangling_lib.symlink_to(tmp_path / "missing-target")
    with monkeypatch.context() as malformed_ctx:
        malformed_ctx.setattr(lu_machinery, "__file__", str(src_init))
        malformed_ctx.setattr(
            release_cut.sysconfig, "get_path",
            lambda key: str(dangling_lib / "python3.14/site-packages") if key == "purelib" else None,
        )
        with pytest.raises(CutError, match="cannot resolve running interpreter purelib path"):
            cut.publisher_invocation("pr-create")
    with monkeypatch.context() as malformed_ctx:
        malformed_ctx.setattr(lu_machinery, "__file__", str(src_init))
        malformed_ctx.setattr(release_cut.sysconfig, "get_path", lambda key: str(dangling_lib) if key == "platlib" else None)
        with pytest.raises(CutError, match="cannot resolve running interpreter platlib path"):
            cut.publisher_invocation("pr-create")
    # Non-directory leaf or ancestor occupying purelib/platlib must refuse, while genuinely absent optional path passes.
    file_site = tmp_path / "file-site"
    file_site.write_text("not a dir\n", encoding="utf-8")
    with monkeypatch.context() as malformed_ctx:
        malformed_ctx.setattr(lu_machinery, "__file__", str(src_init))
        malformed_ctx.setattr(release_cut.sysconfig, "get_path", lambda key: str(file_site) if key == "purelib" else None)
        with pytest.raises(CutError, match=r"running interpreter purelib path .* is not a directory"):
            cut.publisher_invocation("pr-create")
    with monkeypatch.context() as malformed_ctx:
        malformed_ctx.setattr(lu_machinery, "__file__", str(src_init))
        malformed_ctx.setattr(
            release_cut.sysconfig, "get_path",
            lambda key: str(file_site / "site-packages") if key == "platlib" else None,
        )
        with pytest.raises(CutError, match=r"running interpreter platlib path .* has non-directory ancestor"):
            cut.publisher_invocation("pr-create")
    with monkeypatch.context() as absent_ctx:
        absent_ctx.setattr(lu_machinery, "__file__", str(src_init))
        absent_ctx.setattr(
            release_cut.sysconfig, "get_path",
            lambda key: str(tmp_path / "absent-lib64/python3.14/site-packages") if key == "platlib" else None,
        )
        argv_absent, env_absent = cut.publisher_invocation("pr-create")
        assert argv_absent[:4] == [installed, "-P", "-s", "-m"]
        assert env_absent["PYTHONPATH"] == str(src_root.resolve())
    with monkeypatch.context() as nul_ctx:
        nul_ctx.setattr(lu_machinery, "__file__", str(src_init))
        nul_ctx.setattr(
            release_cut.sysconfig, "get_path",
            lambda key: f"{tmp_path}/absent-parent/bad\x00site" if key == "platlib" else None,
        )
        with pytest.raises(CutError, match="contains NUL byte"):
            cut.publisher_invocation("pr-create")
    loop_pkg = tmp_path / "loop-pkg"
    loop_pkg.symlink_to(loop_pkg)
    with monkeypatch.context() as loop_ctx:
        loop_ctx.setattr(lu_machinery, "__file__", str(loop_pkg))
        with pytest.raises(CutError, match="cannot resolve running lu_machinery package"):
            cut.publisher_invocation("pr-create")
    unsafe_dir = tmp_path / f"bad{os.pathsep}root" / "lu_machinery"
    unsafe_dir.mkdir(parents=True)
    unsafe_init = unsafe_dir / "__init__.py"
    unsafe_init.write_text("", encoding="utf-8")
    monkeypatch.setattr(lu_machinery, "__file__", str(unsafe_init))
    with pytest.raises(CutError, match="unsafe lu_machinery package root"):
        cut.publisher_invocation("pr-create")
    monkeypatch.setattr(lu_machinery, "__file__", None)
    with pytest.raises(CutError, match="cannot locate the running lu_machinery package"):
        cut.publisher_invocation("pr-create")
    monkeypatch.setattr(sys, "executable", "")
    with pytest.raises(CutError, match="cannot locate the running Python interpreter"):
        cut.publisher_invocation("pr-create")
    # Manual command formatting fails soft (None) when publisher_invocation fails so original push errors are not masked.
    assert cut._manual_post_release_command(["pr-create"]) is None


def test_build_syncs_locked_build_group_from_uv_lock(tmp_path, monkeypatch):
    """Defect class: release build reads the workflow heredoc or installs the build group without --locked/--only-group/--require-hashes/--only-binary=:all:/--no-deps, leaks ambient env, or fails open on missing uv, lock mismatch, or missing wheel hash."""
    seed = tmp_path / "machinery-seed"
    _init_repo(seed)
    (seed / "pyproject.toml").write_text(
        '[project]\nname = "lu-machinery"\nversion = "0.1.68"\n\n[dependency-groups]\nbuild = ["build==1.6.1"]\n',
        encoding="utf-8",
    )
    (seed / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(seed, "add", "pyproject.toml", "uv.lock")
    _git(seed, "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "fixture build inputs")
    head = subprocess.run(
        ["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()

    bare = tmp_path / "machinery.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--bare", str(seed), str(bare)], check=True, capture_output=True, text=True, timeout=30,
    )

    build_log = tmp_path / "build-calls.log"
    fail_uv_marker = tmp_path / "fail-uv-export"
    fail_pip_marker = tmp_path / "fail-pip-hash"

    host_python = tmp_path / "host-python"
    host_python.write_text(
        "#!/bin/sh\nset -eu\n"
        '[ -z "${GH_TOKEN+x}${ANTHROPIC_API_KEY+x}${UV_INDEX_URL+x}${UV_EXCLUDE_NEWER+x}${UV_NO_SYNC+x}" ]\n'
        'if [ "$1 $2 $3" = "-I -m venv" ]; then\n'
        '  mkdir -p "$4/bin"\n'
        '  cp "$0" "$4/bin/python"\n'
        '  chmod 755 "$4/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2 $3 $4" = "-I -m pip install" ]; then\n'
        f'  if [ -e {shlex.quote(str(fail_pip_marker))} ]; then\n'
        '    printf "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES\\n" >&2\n'
        "    exit 1\n"
        "  fi\n"
        '  [ "$#" -eq 12 ]\n'
        '  [ "$5 $6 $7 $8 $9 ${10} ${11}" = "--no-cache-dir --disable-pip-version-check --no-input --require-hashes --only-binary=:all: --no-deps --requirement" ]\n'
        '  grep -Fq "build==1.6.1 --hash=sha256:" "${12}"\n'
        f'  printf "pip %s\\n" "${{12}}" >> {shlex.quote(str(build_log))}\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2 $3" = "-I -m build" ]; then\n'
        '  [ "$4 $5 $6 $7" = "--sdist --wheel --no-isolation --outdir" ]\n'
        f'  printf "build %s %s\\n" "$8" "$9" >> {shlex.quote(str(build_log))}\n'
        '  printf "wheel-bytes" > "$8/lu_machinery-0.1.68-py3-none-any.whl"\n'
        '  printf "sdist-bytes" > "$8/lu_machinery-0.1.68.tar.gz"\n'
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    host_python.chmod(0o755)

    def make_cut(name: str) -> Cut:
        work = tmp_path / name
        work.mkdir()
        (work / "home").mkdir()
        (work / "tmp").mkdir()
        subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", str(bare), str(work / "machinery")],
            check=True, capture_output=True, text=True, timeout=30,
        )
        return Cut(
            work=work, dry_run=True, machinery_remote=str(bare), product_remote="",
            machinery_repo=REPO, product_repo="learn-ukrainian/learn-ukrainian.github.io",
            host_python=host_python, primary=tmp_path, alert=False, x_agent="claude/claude-opus-5-5",
            head=head,
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        "#!/bin/sh\nset -eu\n"
        f'if [ -e {shlex.quote(str(fail_uv_marker))} ]; then\n'
        '  printf "error: lockfile mismatch or missing build group\\n" >&2\n'
        "  exit 2\n"
        "fi\n"
        '[ -f pyproject.toml ] && [ -f uv.lock ] && [ ! -e .github/workflows/ci-gate.yml ]\n'
        '[ -z "${GH_TOKEN+x}${ANTHROPIC_API_KEY+x}${UV_INDEX_URL+x}${UV_EXCLUDE_NEWER+x}${UV_NO_SYNC+x}" ]\n'
        '[ "$HOME" = "${PWD%/*}/home" ] && [ "$TMPDIR" = "${PWD%/*}/tmp" ]\n'
        '[ "$#" -eq 11 ]\n'
        '[ "$1 $2 $3 $4 $5 $6 $7 $8 $9 ${10}" = "export --locked --quiet --format requirements.txt --no-emit-project --only-group build --no-python-downloads --python" ]\n'
        f'[ "${{11}}" = {shlex.quote(str(host_python))} ]\n'
        'printf "build==1.6.1 --hash=sha256:%s\\n" "ecd351a4be9d35a9eaaba244a7687143c9c7d4aea6ac964e7e7ddab20cbcf4e7"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    monkeypatch.setenv("GH_TOKEN", "fixture-gh")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-key")
    monkeypatch.setenv("UV_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "2000-01-01")
    monkeypatch.setenv("UV_NO_SYNC", "1")

    # 1. Happy path: exports the locked build group from uv.lock (no ci-gate.yml), installs hash-checked wheels, builds dist.
    ok_cut = make_cut("work-ok")
    ok_cut.uv = fake_uv
    files = ok_cut.build("0.1.68")
    assert files["wheel"].read_bytes() == b"wheel-bytes"
    assert files["sdist"].read_bytes() == b"sdist-bytes"
    assert files["sums"].is_file()
    assert (ok_cut.work / "build-requirements.txt").is_file()
    assert build_log.read_text(encoding="utf-8").splitlines() == [
        f"pip {ok_cut.work / 'build-requirements.txt'}",
        f"build {ok_cut.work / 'dist'} {ok_cut.work / 'src'}",
    ]

    # 2. Fail closed when uv export rejects a missing/mismatched lock or build group: never invokes pip or build.
    build_log.unlink()
    fail_uv_marker.write_text("1", encoding="utf-8")
    bad_lock_cut = make_cut("work-bad-lock")
    bad_lock_cut.uv = fake_uv
    with pytest.raises(CutError, match=r"lockfile mismatch or missing build group"):
        bad_lock_cut.build("0.1.68")
    assert not build_log.exists()
    fail_uv_marker.unlink()

    # 3. Fail closed when pip --require-hashes rejects a missing or mismatched wheel hash: never invokes build.
    fail_pip_marker.write_text("1", encoding="utf-8")
    bad_hash_cut = make_cut("work-bad-hash")
    bad_hash_cut.uv = fake_uv
    with pytest.raises(CutError, match=r"DO NOT MATCH THE HASHES"):
        bad_hash_cut.build("0.1.68")
    assert not build_log.exists()
    fail_pip_marker.unlink()

    # 4. Fail closed when uv is unavailable on PATH.
    no_uv_cut = make_cut("work-no-uv")
    monkeypatch.setattr(release_cut.shutil, "which", lambda _name: None)
    with pytest.raises(CutError, match=r"uv is not on PATH; the release build tools are exported from uv\.lock"):
        no_uv_cut.build("0.1.68")


# Dual-slug transfer bridge (A01b): machinery baked as org/repos and neworg/repos, bound to repository_id 4242.
_BAKED = ("org/repos", "neworg/repos")


def _transferred(monkeypatch, active):
    """The fleet registry's machinery row transferred between the two baked slugs; the host selector picks *active*."""
    from lu_machinery.common import git_credentials
    from lu_machinery.orchestration import fleet_repos

    row = fleet_repos.FleetRepo(
        "machinery", "org/repos", "machinery", "public-machinery", default=True,
        repository_id=4242, visibility="public", alternate_github="neworg/repos",
    )
    monkeypatch.setattr(fleet_repos, "_repository_catalog", lambda _path: {"machinery": row})
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"machinery": active}, "d" * 64),
    )
    return {"key": "machinery", "github": "org/repos", "alternate_github": "neworg/repos",
            "repository_id": 4242, "visibility": "public", "local_name": "machinery"}


@pytest.mark.parametrize("remote", [None, "active", "case", "local", "file", "inactive", "elsewhere", "file-host"])
@pytest.mark.parametrize("active", [*_BAKED, "evil/repo"])
def test_release_cut_runs_at_the_active_slug_and_logs_the_selector(monkeypatch, tmp_path, capsys, active, remote):
    """Defect class: the release cut clones, tags and publishes at the registry's first slug after a transfer, and
    its log never names the selector; a selector choosing a slug the row does not bake must refuse the cut."""
    _host(monkeypatch, tmp_path, sys.executable)
    machinery = _transferred(monkeypatch, active)
    monkeypatch.setattr(git_module, "resolve_repo", lambda name=None: (
        tmp_path / "primary", machinery if name == "machinery" else {"github": "home/repo"}))
    seen = {}

    def execute(cut):
        seen.update(repo=cut.machinery_repo, remote=cut.machinery_remote, log=(cut.work / "cut.log").read_text())
        return 0

    monkeypatch.setattr(release_cut, "execute", execute)
    argv = ["cut", "--dry-run", "--no-alert", "--work", str(tmp_path / "w"), "--x-agent", "claude/claude-opus-5-5"]
    inactive = next((slug for slug in _BAKED if slug != active), _BAKED[0])
    override = {
        "active": f"https://github.com/{active}.git", "case": f"https://github.com/{active.upper()}.git",
        # Relative to the launch directory: Git must receive the checked absolute location, never this spelling.
        "local": "mirror.git", "file": (tmp_path / "mirror.git").as_uri(),
        "inactive": f"https://github.com/{inactive}.git", "elsewhere": f"https://example.invalid/{active}.git",
        "file-host": f"file://example.invalid{tmp_path / 'mirror.git'}",
    }.get(remote)
    (tmp_path / "mirror.git").mkdir()
    monkeypatch.chdir(tmp_path)
    if override is not None:
        argv += ["--machinery-remote", override]
    if active == "evil/repo":
        assert main(argv) == 1
        assert "release cut refused: fleet repository selector" in capsys.readouterr().err and not seen
        return
    if remote in {"inactive", "elsewhere", "file-host"}:
        assert main(argv) == 1
        assert "release cut refused: --machinery-remote is " in capsys.readouterr().err and not seen
        return
    assert main(argv) == 0
    local = (tmp_path / "mirror.git").resolve().as_uri()
    expected = local if remote in {"local", "file"} else f"https://github.com/{active}.git"
    assert (seen["repo"], seen["remote"]) == (active, expected)
    assert f'fleet_repo_selector: {{"selector_sha256": "{"d" * 64}", "selector_value": "{active}"}}' in seen["log"]


@pytest.mark.parametrize("active", _BAKED)
@pytest.mark.parametrize("pinned", [*_BAKED, "Org/Repos", "NewOrg/Repos", "evil/repo", "org/repo\u017f"])
def test_adoption_replaces_a_pin_under_either_baked_slug_with_the_active_slug(monkeypatch, tmp_path, active, pinned):
    """Defect class: after a transfer the product's wheel pin still names the old release slug, so the adoption
    refuses it as another repository's; a pin under a slug the row does not bake must still refuse."""
    _transferred(monkeypatch, active)
    product = tmp_path / "product"
    product.mkdir()
    old_url = f"https://github.com/{pinned}/releases/download/v0.1.22/lu_machinery-0.1.22-py3-none-any.whl"
    (product / "pyproject.toml").write_text(
        '[project]\nname = "example-product"\nversion = "0.1.0"\ndependencies = ["lu-machinery==0.1.22"]\n'
        f'[tool.uv.sources]\nlu-machinery = {{ url = "{old_url}" }}\n', encoding="utf-8")
    cut = Cut(work=tmp_path, dry_run=True, machinery_remote="unused", product_remote="unused", machinery_repo=active,
              product_repo="unused", host_python=Path(sys.executable), primary=tmp_path, alert=False,
              x_agent="codex/test", head=HEAD)
    cut.version, cut.uv = "0.1.23", "uv"

    class Locked(Exception):
        pass

    def run(*_args, **_kwargs):
        raise Locked

    cut.run = run
    if pinned == "evil/repo":
        with pytest.raises(CutError, match="from this release repository"):
            cut.update_adopt_inputs(product)
        return
    if not pinned.isascii():  # a non-ASCII look-alike of a baked slug is no wheel URL at all
        with pytest.raises(CutError, match="must declare one lu-machinery wheel URL$"):
            cut.update_adopt_inputs(product)
        return
    with pytest.raises(Locked):
        cut.update_adopt_inputs(product)
    text = (product / "pyproject.toml").read_text(encoding="utf-8")
    assert f"https://github.com/{active}/releases/download/v0.1.23/lu_machinery-0.1.23-py3-none-any.whl" in text
    assert "0.1.22" not in text


@pytest.mark.parametrize("active", _BAKED)
@pytest.mark.parametrize("recorded", [*_BAKED, "evil/repo"])
def test_ci_receipt_recorded_under_either_baked_slug_proves_the_active_repository(monkeypatch, active, recorded):
    """Defect class: a CI evidence receipt written before a transfer names the old slug and is refused as foreign
    once the cut runs at the active slug; a receipt of another repository must still refuse."""
    _transferred(monkeypatch, active)
    tree = "c" * 40
    target = {"id": 7, "run_attempt": 1, "head_sha": HEAD, "head_branch": "main", "event": "push",
              "status": "completed", "conclusion": "success", "path": ".github/workflows/ci-gate.yml",
              "repository": {"full_name": active}, "head_repository": {"full_name": active},
              "head_commit": {"tree_id": tree}}
    receipt = {"schema": "ci-gate-evidence.v1", "repository": recorded, "workflow": ".github/workflows/ci-gate.yml",
               "mode": "full", "target": {"run_id": 7, "run_attempt": 1, "commit": HEAD, "tree": tree}, "source": None}

    def validate():
        return release_cut.validate_ci_receipt(receipt, repository=active, target=target, commit=HEAD, tree=tree)

    if recorded == "evil/repo":
        with pytest.raises(CutError, match="repository or workflow mismatch"):
            validate()
    else:
        assert validate() is None
