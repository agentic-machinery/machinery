"""The release cut's pure decisions: skip, refuse or cut, the generated release notes, and the X-Agent trailer."""

from __future__ import annotations

import json
import subprocess

import pytest

from lu_machinery.release_cut import Cut, main, preflight, release_notes, x_agent_value

HEAD = "a" * 40
TAG_COMMIT = "b" * 40
NEXT = {"pyproject.toml": "0.1.22", "package.json": "0.1.22", "package-lock.json": "0.1.22",
        'package-lock.json packages[""]': "0.1.22"}
GREEN = [{"headSha": HEAD, "event": "push", "status": "completed", "conclusion": "success", "databaseId": 7}]
TITLES = ["fix(review): count a shared branch's review rounds per pull request (#627)"]


def decide(**changes):
    inputs = {"head": HEAD, "last_tag": "v0.1.21", "last_tag_commit": TAG_COMMIT, "titles": TITLES,
              "versions": NEXT, "tags": ["v0.1.20", "v0.1.21"], "runs": GREEN}
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
    assert x_agent_value("claude/claude-cto") == "claude/claude-cto"
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


def test_created_commit_message_ends_with_the_x_agent_trailer(tmp_path):
    """Defect class: the adopt and post-release commits omit an X-Agent trailer, so review cannot resolve an author family."""
    machinery_seed = tmp_path / "machinery-seed"
    _init_repo(machinery_seed)
    (machinery_seed / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "[Unreleased]: https://github.com/learn-ukrainian/machinery/compare/v0.1.21...HEAD\n",
        encoding="utf-8",
    )
    (machinery_seed / "pyproject.toml").write_text('[project]\nname = "lu-machinery"\nversion = "0.1.22"\n', encoding="utf-8")
    (machinery_seed / "package.json").write_text(
        json.dumps({"name": "lu-machinery-acp", "version": "0.1.22"}, indent=2) + "\n", encoding="utf-8",
    )
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
        product_remote=str(tmp_path / "product.git"), machinery_repo="learn-ukrainian/machinery",
        product_repo="learn-ukrainian/learn-ukrainian", host_python=tmp_path / "python", primary=tmp_path,
        alert=False, x_agent="claude/claude-cto", version="0.1.22", previous="0.1.21",
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

    lock = tmp_path / "consumer.lock"
    lock.write_text("lu-machinery==0.1.22\n", encoding="utf-8")
    cut.publisher = lambda *_args: "https://github.com/learn-ukrainian/learn-ukrainian/pull/1\n"
    cut.open_adopt_pr(lock, product)
    adopt = subprocess.run(
        ["git", "-C", str(product), "log", "-1", "--format=%B"], check=True, capture_output=True, text=True,
    ).stdout
    assert adopt.rstrip("\n").endswith("X-Agent: claude/claude-cto")
