"""The release cut's pure decisions, notes and X-Agent trailer; its umask for created paths and its alert's plane root."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery import release_cut
from lu_machinery.agent_runtime.adapters.acpx import _check_node_artifact
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

    monkeypatch.setattr(release_cut, "_run", run)
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


def test_created_commit_message_ends_with_the_x_agent_trailer(tmp_path, monkeypatch):
    """Defect classes: missing commit attribution and a version bump with a stale lock."""
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
        '[ "$*" = "lock --offline --no-python-downloads" ]\n'
        "grep -Fqx 'version = \"0.1.23\"' pyproject.toml\n"
        "sed 's/^version = \"0.1.22\"$/version = \"0.1.23\"/' uv.lock > next.lock\n"
        "mv next.lock uv.lock\n",
        encoding="utf-8",
    )
    uv.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    cut = Cut(
        work=work, dry_run=False, machinery_remote=str(tmp_path / "machinery.git"),
        product_remote=str(tmp_path / "product.git"), machinery_repo="learn-ukrainian/machinery",
        product_repo="learn-ukrainian/learn-ukrainian", host_python=tmp_path / "python", primary=tmp_path,
        alert=False, x_agent="claude/claude-cto", version="0.1.22", previous="0.1.21", baseline="0.1.21",
        git_identity=("-c", "user.name=Release Cut", "-c", "user.email=release-cut@example.invalid"),
    )
    # The host pre-push scan refuses a fixture that is not the public repository. The commit is what this test reads.
    original_run = cut.run

    def run(argv, **kwargs):
        if "push" in [str(part) for part in argv]:
            return subprocess.CompletedProcess([], 0, "", "")
        return original_run(argv, **kwargs)

    cut.run = run
    cut.publisher = lambda *_args: "https://github.com/learn-ukrainian/machinery/pull/633\n"
    assert cut.post_release("### Changed\n\n- Note\n") == 633
    post = subprocess.run(
        ["git", "-C", str(machinery), "log", "-1", "--format=%B"], check=True, capture_output=True, text=True,
    ).stdout
    assert post.rstrip("\n").endswith("X-Agent: claude/claude-cto")
    assert cut.git(machinery, "show", "HEAD:uv.lock") == uv_lock.replace("0.1.22", "0.1.23").strip()

    lock = tmp_path / "consumer.lock"
    lock.write_text("lu-machinery==0.1.22\n", encoding="utf-8")
    cut.publisher = lambda *_args: "https://github.com/learn-ukrainian/learn-ukrainian/pull/1\n"
    cut.open_adopt_pr(lock, product)
    adopt = subprocess.run(
        ["git", "-C", str(product), "log", "-1", "--format=%B"], check=True, capture_output=True, text=True,
    ).stdout
    assert adopt.rstrip("\n").endswith("X-Agent: claude/claude-cto")
    assert cut.git(product, "show", "HEAD:requirements-machinery.lock") == "lu-machinery==0.1.22"

    cut.version = "0.1.23"
    base_url = "https://github.com/learn-ukrainian/machinery/releases/download"
    old_url = f"{base_url}/v0.1.22/lu_machinery-0.1.22-py3-none-any.whl"
    new_url = f"{base_url}/v0.1.23/lu_machinery-0.1.23-py3-none-any.whl"
    manifest = product / "pyproject.toml"
    old_manifest = ('[project]\nname = "product"\nversion = "1.0.0"\n'
                    'dependencies = ["lu-machinery[api]==0.1.22"]\n'
                    '[dependency-groups]\nmachinery = ["lu-machinery"]\n'
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
            assert argv[1:] == ["lock", "--upgrade-package", "lu-machinery", "--no-python-downloads"]
            assert kwargs["cwd"] == product
            assert manifest.read_text(encoding="utf-8") == old_manifest.replace("0.1.22", "0.1.23")
            (product / "uv.lock").write_text(
                'version = 1\n[[package]]\nname = "lu-machinery"\nversion = "0.1.23"\n'
                f'source = {{ url = "{new_url}" }}\nwheels = [{{ url = "{new_url}", hash = "sha256:{digest}" }}]\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run(argv, **kwargs)

    cut.run = run_uv_adoption
    cut.open_adopt_pr(lock, product)
    assert cut.git(product, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == [
        "pyproject.toml", "uv.lock",
    ]
    assert f"sha256:{digest}" in cut.git(product, "show", "HEAD:uv.lock")
    assert cut.git(product, "log", "-1", "--format=%B").endswith("X-Agent: claude/claude-cto")
    (product / "uv.lock").write_text('package = "invalid"\n', encoding="utf-8")
    with pytest.raises(CutError, match="uv.lock package must be an array of tables"):
        release_cut.wheel_only_requirements("", product / "uv.lock")
    manifest.write_text("tool = []\n", encoding="utf-8")
    with pytest.raises(CutError, match="pyproject.toml tool must be a table"):
        cut.update_adopt_inputs(lock, product)


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


def _released_history(tmp_path, commits, releases):
    """A machinery remote whose first-parent history is ``commits`` ((title, version, tag)), and a dry-run Cut on it.

    GitHub is faked: ``releases`` maps a tag to its release's isPrerelease, and HEAD's CI Gate push run succeeded.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    for title, version, tag in commits:
        (seed / "pyproject.toml").write_text(f'[project]\nname = "lu-machinery"\nversion = "{version}"\n', encoding="utf-8")
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
    head = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    work = tmp_path / "work"
    work.mkdir()
    cut = Cut(work=work, dry_run=True, machinery_remote=str(remote), product_remote=str(tmp_path / "product.git"),
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


def test_a_withdrawn_last_tag_cuts_everything_merged_since_the_last_full_release(tmp_path):
    """Defect class: after a withdrawn pre-release the skip rule and notes start at its tag, so changes never ship."""
    cut = _released_history(tmp_path, [
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


def test_only_a_bump_since_a_full_release_still_skips(tmp_path):
    """Defect class: the withdrawn-release walk turns an unchanged window after a full release into an empty cut."""
    cut = _released_history(tmp_path, [
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
    monkeypatch.setattr(repo_root, "primary_root", lambda: primary)
    monkeypatch.setattr(repo_root, "project_interpreter", lambda: interpreter)
    return primary


# prepare_node_dependencies, in the accept interpreter: parents, the 0755 prefix, then the contract copies.
_PREPARE_PREFIX = (
    "import pathlib, shutil, sys; work = pathlib.Path(sys.argv[1]); contract = work / 'contract'; contract.mkdir(); "
    "(contract / '.nvmrc').write_text('22\\n'); prefix = work / 'accept/share/lu-machinery/node/0.1.23-lock'; "
    "prefix.parent.mkdir(parents=True); prefix.mkdir(mode=0o755); shutil.copyfile(contract / '.nvmrc', prefix / '.nvmrc')"
)


def test_paths_the_cut_creates_are_admitted_under_a_group_writable_umask(monkeypatch, tmp_path, group_writable_umask):
    """Defect class: under a 0002 umask the cut's Node prefix is group-writable and Node admission refuses it."""
    interpreter = tmp_path / "bin/python"
    interpreter.parent.mkdir()
    # `-I -m venv <path>`: venv copies activate scripts with their source mode, which the umask does not mask.
    interpreter.write_text('#!/bin/sh\nmkdir -p "$4/bin" && : > "$4/bin/activate" && chmod 664 "$4/bin/activate"\n',
                           encoding="utf-8")
    interpreter.chmod(0o755)
    _host(monkeypatch, tmp_path, interpreter)
    work = tmp_path / "work"

    def execute(cut):
        cut.venv(cut.work / "accept")
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

    def execute(cut):
        cut.step = "Node provisioning"
        raise CutError("Unsafe Node dependency artifact")

    monkeypatch.setattr(release_cut, "execute", execute)
    assert main(["cut", "--dry-run", "--work", str(tmp_path / "work"), "--x-agent", "claude/claude-opus-5-5"]) == 1
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
