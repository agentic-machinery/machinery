"""Cut one lu-machinery release from main's HEAD, then publish it and open its adoption.

``docs/release.md`` ("Cut a release") is the procedure; this command is its only
implementation. One run tags main's HEAD (there is no release PR), builds once,
installs the wheelhouse into a clean environment, publishes a GitHub pre-release,
opens the product's draft adopt PR, runs acceptance, then promotes the release,
marks the adopt PR ready and opens the post-release PR that records the
changelog and moves main to the next version. Any failure stops the run and
alerts ``claude-cto`` through Fleet Comms. Published files are never replaced
and a tag is never moved: a failed pre-release is marked withdrawn and the next
version replaces it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import textwrap
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal

from lu_machinery.common import json_depth
from lu_machinery.common.git_context import sanitized_git_env
from lu_machinery.common.repo_root import ENV_ALIASES, MACHINERY_ENV_VARS, PRIMARY_ROOT_ENV, PRODUCT_ROOT_ENV

# The post-release PR's title. A range holding only these PRs is unchanged.
BUMP_TITLE = "chore(release): record {released} and start {next}"
_BUMP = re.compile(r"chore\(release\): record \d+\.\d+\.\d+ and start \d+\.\d+\.\d+( \(#\d+\))?")
_VERSION = re.compile(r"([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})")
# Same value grammar as lu_machinery.ci.check_agent_attribution (``X-Agent: <agent>/<task-id>``).
_X_AGENT = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*/[A-Za-z0-9._-]+")
_X_AGENT_EXAMPLE = "claude/claude-opus-5-5"
# ``X-Verified`` lines (docs/runbooks/agent-attribution.md): only checks the cut already ran when it writes the commit.
# The adopt PR opens before the contract canary runs, so its line must not name the canary.
_ADOPT_VERIFIED = "X-Verified: release_cut preflight, wheel build, clean install and published asset SHA-256 check"
_POST_RELEASE_VERIFIED = "X-Verified: release_cut changelog replay and version-file replay"
_TAG_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
_PR_SUFFIX = re.compile(r"^(?P<text>.*?) \(#(?P<pr>\d+)\)$")
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?: (?P<desc>.+)$")
# Unreleased is empty when the next non-blank text starts a release (its anchor, then its heading)
# or is the trailing block of link definitions; anything else is hand-written content.
_UNRELEASED = "## [Unreleased]\n\n"
_RELEASE_START = re.compile(r'<a id="v(\d+\.\d+\.\d+)"></a>\n## \[\1\] - ')
_LINK_DEFINITION = re.compile(r"\[[^\]\n]+\]: \S+")
_REMOVAL_WORDS = frozenset({"remove", "removes", "retire", "retires", "delete", "deletes", "drop", "drops"})
SECTIONS = ("Added", "Changed", "Fixed", "Removed")
VERSION_FILES = ("pyproject.toml", "package.json", "package-lock.json")
ALERT_RECIPIENT = "claude-cto"
_PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "no_proxy", "all_proxy"})
CI_WORKFLOW = "CI Gate"
# Node lazy require sites that docs/release.md names; each must resolve inside the prefix.
_LAZY_CJS = (
    "require('debug')('release-cut')('lazy colours');"
    "for (const name of ['@modelcontextprotocol/sdk/validation/cfworker', 'b4a', 'bare-events',"
    " 'bare-stream', 'express']) require(name);"
)
_LAZY_ESM = "await import('@modelcontextprotocol/sdk/validation/cfworker');"
# Python snippets run inside the candidate environments.
_SHOW_LU = ("import importlib.metadata as m, lu_machinery; "
            "print(m.version('lu-machinery')); print(lu_machinery.__file__)")
_ACPX = "from lu_machinery.agent_runtime.adapters.acpx import "
_NODE_INFO = (f"import json; {_ACPX}validate_node_dependencies as v, node_toolchain as t; p = v(); n, m = t(); "
              "print(json.dumps({'prefix': str(p), 'node': str(n), 'npm': str(m)}))")
_RELEASE_INVENTORY = ("from importlib.resources import files; "
                      "print(files('lu_machinery').joinpath('agent_deploy/release.v1.json').read_text())")
_NODE_SEAL = f"from pathlib import Path; import sys; {_ACPX}seal_node_dependencies as s; print(s(cache=Path(sys.argv[1])))"
_SITE_PATHS = "import sysconfig; print(sysconfig.get_path('purelib')); print(sysconfig.get_path('platlib'))"
_ADOPTION_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)
_ADOPTION_OUTPUT = re.compile(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.json")
_RUN_ADOPTION_SNAPSHOT = textwrap.dedent("""\
    import importlib.util
    import os
    import runpy
    import sys

    import lu_machinery

    sys.path.insert(0, os.getcwd())
    name = sys.argv[1]
    path = os.path.join(os.getcwd(), *name.split(".")) + ".py"
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin != path:
        raise SystemExit("ADOPTION_SNAPSHOT_MODULE: expected a module from the product checkout")
    runpy.run_module(name, run_name="__main__", alter_sys=True)
    """)


class CutError(Exception):
    """A refused precondition or a failed step; the message names the cause."""


def _validate_release_milestone(value: object, *, repo_label: str) -> str:
    """Validate one configured release milestone string, failing closed with a typed reason."""
    from lu_machinery.publish.github import valid_milestone_text

    if not isinstance(value, str) or not valid_milestone_text(value):
        raise CutError(
            f"MILESTONE_REFUSED: {repo_label} release_milestone must be a non-blank single-line string, got {value!r}"
        )
    return value


def configured_release_milestone(repo_config: Mapping[str, object], *, repo_label: str) -> str | None:
    """Read optional ``release_milestone`` from one fleet_repos row; omit means None, invalid refuses."""
    if "release_milestone" not in repo_config:
        return None
    return _validate_release_milestone(repo_config["release_milestone"], repo_label=repo_label)


class UvPreflightError(CutError):
    """A release runner or repository could not satisfy the uv precondition."""

    def __init__(self, side: Literal["machinery", "product", "runner"], message: str) -> None:
        self.side = side
        super().__init__(f"uv preflight ({side}): {message}")


@dataclass(frozen=True)
class Decision:
    """What the preflight allows: ``skip`` (unchanged), ``refuse`` or ``cut``."""

    action: str
    reason: str
    version: str


def x_agent_value(value: str, *, command: str) -> str:
    """Require an X-Agent trailer the review recorder can attribute on each destination PR."""
    if not _X_AGENT.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} must be <agent>/<task-or-seat>, for example {_X_AGENT_EXAMPLE}")

    from lu_machinery.common.repo_root import (
        MachineryConfigError,
        StateRootError,
        configured_github_slug,
    )
    from lu_machinery.common.task_store_paths import tasks_dir
    from lu_machinery.orchestration.fleet_repos import (
        FleetRepoError,
        default_repo_key,
        load_fleet_repos,
    )
    from lu_machinery.review.record_cf_verdict import RecordError, _attribute_trailer
    from lu_machinery.review.reviewer_resolver import UNKNOWN_AUTHOR_FAMILY

    error = f"{value!r} must resolve to an author family for review. Use a value such as {_X_AGENT_EXAMPLE}."
    try:
        try:
            task_root = tasks_dir()
        except StateRootError:
            # Without a state anchor, the recorder can only use the trailer itself.
            task_root = Path(os.devnull)

        repos = load_fleet_repos()
        destinations = [repos["machinery"].github]
        if command == "cut":
            destinations.append(configured_github_slug() or repos[default_repo_key(repos)].github)
        for repository in destinations:
            attribution = _attribute_trailer(None, value, repository=repository, task_root=task_root)
            if attribution.family == UNKNOWN_AUTHOR_FAMILY:
                raise argparse.ArgumentTypeError(f"{error} Unresolved in {repository}.")
    except (FleetRepoError, MachineryConfigError, OSError, RecordError) as exc:
        raise argparse.ArgumentTypeError(f"{error} {exc}") from exc
    return value


# --- Pure decisions -------------------------------------------------------------------------


def next_version(version: str) -> str:
    """The next patch version: ``0.1.21`` becomes ``0.1.22``."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise CutError(f"not a release version: {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def is_bump_title(title: str) -> bool:
    """True for the post-release PR's squash title, with or without its PR number."""
    return _BUMP.fullmatch(title) is not None


def ci_gate_state(runs: Iterable[Mapping[str, object]], sha: str) -> tuple[bool, str]:
    """Whether the newest CI Gate push run on ``sha`` finished successfully."""
    pushes = [run for run in runs if run.get("headSha") == sha and run.get("event") == "push"]
    if not pushes:
        return False, f"no {CI_WORKFLOW} push run on {sha}"
    newest = max(pushes, key=lambda run: int(str(run.get("databaseId", 0))))
    run_id = newest.get("databaseId")
    if newest.get("status") != "completed":
        return False, f"{CI_WORKFLOW} run {run_id} on {sha} is {newest.get('status')}"
    if newest.get("conclusion") != "success":
        return False, f"{CI_WORKFLOW} run {run_id} on {sha} concluded {newest.get('conclusion')}"
    return True, f"{CI_WORKFLOW} run {run_id} on {sha} succeeded"


def validate_ci_receipt(
    receipt: object, *, repository: str, target: Mapping[str, object], commit: str, tree: str,
    source: Mapping[str, object] | None = None, source_jobs: Sequence[Mapping[str, object]] = (),
) -> None:
    """Bind main's full/reused CI proof to live run attempts and the release tree."""
    workflow = ".github/workflows/ci-gate.yml"
    if not isinstance(receipt, dict) or set(receipt) != {"schema", "repository", "workflow", "mode", "target", "source"}:
        raise CutError("CI evidence receipt is missing or malformed")
    from lu_machinery.orchestration.fleet_repos import same_repository

    # The receipt names the repository as it was called when CI ran: either baked slug of the row. Live run
    # payloads below are read now, so they must name the active slug itself.
    if (receipt["schema"], receipt["workflow"]) != ("ci-gate-evidence.v1", workflow) or not same_repository(
        receipt["repository"], repository
    ):
        raise CutError("CI evidence receipt schema, repository or workflow mismatch")
    subjects = [(receipt["target"], target, "push")]
    if receipt["mode"] == "reuse":
        if source is None:
            raise CutError("CI evidence receipt has no live merge-group source")
        subjects.append((receipt["source"], source, "merge_group"))
        if not any(job.get("name") == "CI Gate" and job.get("status") == "completed"
                   and job.get("conclusion") == "success" for job in source_jobs):
            raise CutError("CI evidence source attempt has no successful CI Gate job")
    elif receipt["mode"] != "full" or receipt["source"] is not None:
        raise CutError("CI evidence receipt mode or source is invalid")
    for binding, run, event in subjects:
        expected = {"run_id": run.get("id"), "run_attempt": run.get("run_attempt"),
                    "commit": commit, "tree": tree}
        if (not isinstance(binding, dict) or binding != expected
                or any(type(binding.get(key)) is not int or binding[key] < 1 for key in ("run_id", "run_attempt"))):
            raise CutError(f"CI evidence {event} run, attempt, commit or tree mismatch")
        origin, head_origin, head_commit = run.get("repository"), run.get("head_repository"), run.get("head_commit")
        branch = run.get("head_branch")
        if (run.get("path") != workflow or run.get("event") != event
                or run.get("status") != "completed" or run.get("conclusion") != "success"
                or run.get("head_sha") != commit
                or not isinstance(origin, dict) or origin.get("full_name") != repository
                or not isinstance(head_origin, dict) or head_origin.get("full_name") != repository
                or not isinstance(head_commit, dict) or head_commit.get("tree_id") != tree
                or not isinstance(branch, str)
                or (branch != "main" if event == "push" else not branch.startswith("gh-readonly-queue/main/"))):
            raise CutError(f"CI evidence {event} live run does not prove this repository, workflow and tree")


def preflight(
    *,
    head: str,
    last_tag: str,
    last_tag_commit: str,
    baseline: str,
    titles: Sequence[str],
    versions: Mapping[str, str],
    tags: Iterable[str],
    runs: Iterable[Mapping[str, object]],
    changelog: str,
) -> Decision:
    """Decide whether main's HEAD may be tagged as the next version.

    ``baseline`` is the last full release and ``titles`` are the merges since it:
    a withdrawn pre-release shipped nothing, so its changes are still unreleased.
    Unchanged (HEAD is the last tag, or only post-release PRs merged since the
    baseline) skips. Otherwise every version file must hold the version after
    the last tag, that version's tag must be absent, ``changelog`` (CHANGELOG.md
    at HEAD) must have an empty Unreleased section and the CI Gate on HEAD must
    have succeeded.
    """
    expected = next_version(last_tag.removeprefix("v"))
    if head == last_tag_commit:
        return Decision("skip", f"unchanged: HEAD is {last_tag}", expected)
    if all(is_bump_title(title) for title in titles):
        return Decision("skip", f"unchanged: nothing but the version bump merged since {baseline}", expected)
    wrong = {name: value for name, value in sorted(versions.items()) if value != expected}
    if wrong:
        found = ", ".join(f"{name} {value}" for name, value in wrong.items())
        return Decision("refuse", f"version files must hold {expected}; found {found}", expected)
    if f"v{expected}" in set(tags):
        return Decision("refuse", f"tag v{expected} already exists", expected)
    try:
        unreleased_must_be_empty(changelog)
    except CutError as exc:
        return Decision("refuse", str(exc), expected)
    green, detail = ci_gate_state(runs, head)
    if not green:
        return Decision("refuse", detail, expected)
    return Decision("cut", f"{detail}; {len(titles)} commit(s) since {baseline}", expected)


def release_notes(titles: Sequence[str], repo: str) -> str:
    """Keep a Changelog sections built from merged PR squash titles, oldest first.

    ``feat`` is Added, ``fix`` is Fixed, any other change whose description starts
    with remove/retire/delete/drop is Removed, and the rest is Changed. The
    post-release PR is left out.
    """
    entries: dict[str, list[str]] = {section: [] for section in SECTIONS}
    for title in reversed(titles):
        if is_bump_title(title):
            continue
        text, number = title, None
        suffix = _PR_SUFFIX.match(title)
        if suffix:
            text, number = suffix.group("text"), suffix.group("pr")
        kind = ""
        conventional = _CONVENTIONAL.match(text)
        if conventional:
            kind, text = conventional.group("type"), conventional.group("desc")
        words = text.split(maxsplit=1)
        if kind == "feat":
            section = "Added"
        elif kind == "fix":
            section = "Fixed"
        elif words and words[0].lower() in _REMOVAL_WORDS:
            section = "Removed"
        else:
            section = "Changed"
        line = f"- {text[:1].upper()}{text[1:]}"
        if number:
            line += f" ([#{number}](https://github.com/{repo}/pull/{number}))"
        entries[section].append(line)
    blocks = [f"### {section}\n\n" + "\n".join(lines) for section, lines in entries.items() if lines]
    return "\n\n".join(blocks) + "\n"


def unreleased_must_be_empty(text: str) -> None:
    """Refuse a CHANGELOG.md whose Unreleased section holds anything.

    The release command is the file's only writer and inserts each release below the
    Unreleased heading, so hand-written lines there would be filed under that release.
    """
    if text.count(_UNRELEASED) != 1:
        raise CutError("CHANGELOG.md lacks one Unreleased heading")
    rest = text.split(_UNRELEASED, 1)[1].lstrip()
    lines = [line for line in rest.splitlines() if line.strip()]
    if not _RELEASE_START.match(rest) and not all(_LINK_DEFINITION.fullmatch(line) for line in lines):
        first = lines[0][:120]
        raise CutError(f"CHANGELOG.md Unreleased is not empty (starts {first!r}); the release command is its only "
                       "writer, so remove those lines on main first (docs/release.md, Changelog)")


def record_changelog(
    text: str, *, version: str, previous: str, date: str, notes: str, repo: str, yanked: bool = False,
    since: str | None = None,
) -> str:
    """Add the release's section under Unreleased and its compare link; idempotent per version.

    ``previous`` is the last tag, which the Unreleased link names. ``since`` (default ``previous``) is
    the release the notes start from, which the version's compare link starts from.
    """
    if f"\n## [{version}]" in text:
        return text
    from lu_machinery.orchestration.fleet_repos import repository_slugs

    unreleased = _UNRELEASED
    accepted_links = {
        f"[Unreleased]: https://github.com/{slug}/compare/v{previous}...HEAD\n" for slug in repository_slugs(repo)
    }
    lines = text.splitlines(keepends=True)
    current_links = [index for index, line in enumerate(lines) if line.startswith("[Unreleased]:")]
    if text.count(unreleased) != 1 or len(current_links) != 1 or lines[current_links[0]] not in accepted_links:
        raise CutError("CHANGELOG.md lacks one Unreleased heading and its compare link to the previous tag")
    unreleased_must_be_empty(text)
    heading = f'<a id="v{version}"></a>\n## [{version}] - {date}' + (" [YANKED]" if yanked else "")
    section = f"{heading}\n\n{notes.rstrip()}\n\n"
    lines[current_links[0]] = (
        f"[Unreleased]: https://github.com/{repo}/compare/v{version}...HEAD\n"
        f"[{version}]: https://github.com/{repo}/compare/v{since or previous}...v{version}\n"
    )
    return "".join(lines).replace(unreleased, unreleased + section, 1)


def version_of(name: str, text: str) -> dict[str, str]:
    """The version fields a version file holds, keyed by a readable location."""
    if name == "pyproject.toml":
        return {name: tomllib.loads(text)["project"]["version"]}
    data = json_depth.loads(text, max_depth=512)
    found = {name: data["version"]}
    if name == "package-lock.json":
        found[f'{name} packages[""]'] = data["packages"][""]["version"]
    return found


def bump_version_file(name: str, text: str, old: str, new: str) -> str:
    """Rewrite one version file from ``old`` to ``new``, changing nothing else."""
    if name == "pyproject.toml":
        line = re.compile(rf'^version = "{re.escape(old)}"$', re.MULTILINE)
        if len(line.findall(text)) != 1:
            raise CutError(f"{name}: expected one version = \"{old}\" line")
        return line.sub(f'version = "{new}"', text, count=1)
    data = json_depth.loads(text, max_depth=512)
    if json.dumps(data, indent=2, ensure_ascii=False) + "\n" != text:
        raise CutError(f"{name}: not in npm's JSON layout; refusing to rewrite it")
    if data["version"] != old:
        raise CutError(f"{name}: version is {data['version']}, expected {old}")
    data["version"] = new
    if name == "package-lock.json":
        data["packages"][""]["version"] = new
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def heredoc(text: str, opener: str) -> str:
    """The body of the shell heredoc that starts on the line ending with ``opener``."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip().endswith(opener):
            terminator = opener.rsplit("<<", 1)[1].strip("'\"")
            body: list[str] = []
            for candidate in lines[index + 1:]:
                if candidate.strip() == terminator:
                    return textwrap.dedent("".join(body))
                body.append(candidate)
    raise CutError(f"heredoc {opener!r} not found")


def _uv_packages(lock: Path) -> list[dict]:
    try:
        packages = tomllib.loads(lock.read_text(encoding="utf-8")).get("package")
    except (OSError, ValueError) as exc:
        raise CutError(f"cannot read the uv lock ({type(exc).__name__})") from exc
    if not isinstance(packages, list) or not all(isinstance(package, dict) for package in packages):
        raise CutError("uv.lock package must be an array of tables")
    for package in packages:
        wheels = package.get("wheels", [])
        if not isinstance(package.get("name"), str) or not isinstance(wheels, list) or any(
            not isinstance(wheel, dict) or not isinstance(wheel.get("hash"), str) for wheel in wheels
        ):
            raise CutError("uv.lock has an invalid package name or wheel record")
    return packages


def wheel_only_requirements(exported: str, lock: Path) -> str:
    """Keep wheel hashes in the legacy consumer lock that is used by pip."""
    packages = _uv_packages(lock)
    wheel_hashes = {wheel["hash"] for package in packages for wheel in package.get("wheels", [])}
    pins = []
    for stanza in exported.replace("\\\n", " ").splitlines():
        if not stanza.strip() or stanza.lstrip().startswith("#"):
            continue
        requirement, *hashes = stanza.split(" --hash=")
        hashes = [part.strip() for part in hashes if part.strip() in wheel_hashes]
        if not hashes:
            raise CutError(f"release runtime requirement has no locked wheel hash: {requirement.strip()}")
        pins.append(requirement.strip() + " \\\n    --hash=" + " \\\n    --hash=".join(hashes))
    return "\n".join(pins) + "\n"


def _manifest_table(container: dict, key: str) -> dict:
    table = container.get(key, {})
    if not isinstance(table, dict):
        raise CutError(f"pyproject.toml {key} must be a table")
    return table


def _adopt_cooldown_exemption(text: str, uv: dict) -> str:
    """Keep the lock's command-line exemption in the manifest used by locked installs."""
    from packaging.utils import canonicalize_name

    key = "exclude-newer-package"
    overrides = _manifest_table(uv, key)
    for name, value in overrides.items():
        if canonicalize_name(name) == "lu-machinery":
            if value is not False:
                raise CutError("pyproject.toml must exempt lu-machinery from the uv cooldown (set it to false)")
            return text
    if key not in uv:
        text = text.rstrip("\n") + "\n\n[tool.uv.exclude-newer-package]\nlu-machinery = false\n"
    else:
        text, inserted = re.subn(
            r"(?m)^([ \t]*\[tool\.uv\.exclude-newer-package\][ \t]*(?:#[^\n]*)?\n)",
            r"\g<1>lu-machinery = false\n", text, count=1,
        )
        if not inserted:
            separator = ", " if overrides else " "
            text = re.sub(
                r"(?m)^([ \t]*exclude-newer-package[ \t]*=[ \t]*\{)[ \t]*",
                rf"\g<1> lu-machinery = false{separator}", text, count=1,
            )
    uv[key] = {**overrides, "lu-machinery": False}
    return text


_GITHUB_RELEASE_BASE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/download")


def _release_base_pattern(base: str) -> str:
    """A regex for *base*: a GitHub release base matches its owner/name in any case (GitHub slugs ignore case)."""
    match = _GITHUB_RELEASE_BASE.fullmatch(base)
    if match is None:
        return re.escape(base)
    return rf"https://github\.com/(?i:{re.escape(match[1])})/releases/download"


def adopt_manifest(
    text: str, release_base: str, version: str, *, previous_bases: Iterable[str] = ()
) -> tuple[str, str]:
    """Update the release wheel pin and keep its cooldown exemption in the lock inputs.

    The new pin is under *release_base*; the old one may also be under any of *previous_bases* (the
    release repository's other baked slug, before or after a transfer).
    """
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    manifest = tomllib.loads(text)
    uv = _manifest_table(_manifest_table(manifest, "tool"), "uv")
    source = _manifest_table(uv, "sources").get("lu-machinery")
    if not isinstance(source, dict) or not isinstance(old_url := source.get("url"), str) or not old_url.isascii():
        raise CutError("pyproject.toml must declare one lu-machinery wheel URL")
    bases = "|".join(_release_base_pattern(base) for base in (release_base, *previous_bases))
    old = re.fullmatch(rf"(?:{bases})/v(\d+\.\d+\.\d+)/lu_machinery-\1-py3-none-any\.whl", old_url)
    if old is None or text.count(old_url) != 1:
        raise CutError("pyproject.toml must declare one lu-machinery wheel URL from this release repository")
    project = _manifest_table(manifest, "project")
    groups = [project.get("dependencies", []), *_manifest_table(project, "optional-dependencies").values(),
              *_manifest_table(manifest, "dependency-groups").values()]
    if not all(isinstance(group, list) for group in groups):
        raise CutError("pyproject.toml dependency groups must be arrays")
    new_url = f"{release_base}/v{version}/lu_machinery-{version}-py3-none-any.whl"
    updated = text.replace(old_url, new_url)
    replacements: dict[str, str] = {}
    for group in groups:
        for index, spec in enumerate(group):
            if not isinstance(spec, str):  # PEP 735 group include
                continue
            req = Requirement(spec)
            if canonicalize_name(req.name) == "lu-machinery" and str(req.specifier) == f"=={old[1]}":
                group[index] = replacements[spec] = re.sub(rf"==\s*{re.escape(old[1])}\b", f"=={version}", spec, count=1)
    for spec, new_spec in replacements.items():
        for quote in ("'", '"'):
            updated = updated.replace(f"{quote}{spec}{quote}", f"{quote}{new_spec}{quote}")
    source["url"] = new_url
    updated = _adopt_cooldown_exemption(updated, uv)
    if tomllib.loads(updated) != manifest:
        raise CutError("could not update only the lu-machinery pin and cooldown exemption in pyproject.toml")
    return updated, new_url


# --- Steps ------------------------------------------------------------------------------------


def _resolve_site_root(key: str, raw: str) -> Path | None:
    """Resolve one sysconfig site-packages path, returning None only when genuinely absent.

    Fails closed with ``CutError`` on dangling symlinks (at the path or any ancestor), symlink loops,
    permission errors, or non-directory ancestors or leaves.
    """
    if "\x00" in raw:
        raise CutError(f"running interpreter {key} path contains NUL byte: {raw!r}")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise CutError(f"running interpreter {key} path must be absolute: {raw!r}")
    for ancestor in reversed(candidate.parents):
        try:
            os.lstat(ancestor)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CutError(f"cannot inspect running interpreter {key} path {raw!r}: {exc}") from exc
        try:
            resolved_ancestor = ancestor.resolve(strict=True)
            ancestor_stat = os.stat(resolved_ancestor)
        except (OSError, RuntimeError, ValueError) as exc:
            raise CutError(f"cannot resolve running interpreter {key} path {raw!r}: {exc}") from exc
        if not stat.S_ISDIR(ancestor_stat.st_mode):
            raise CutError(f"running interpreter {key} path {raw!r} has non-directory ancestor {ancestor}")
    try:
        os.lstat(candidate)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CutError(f"cannot inspect running interpreter {key} path {raw!r}: {exc}") from exc
    try:
        resolved = candidate.resolve(strict=True)
        resolved_stat = os.stat(resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CutError(f"cannot resolve running interpreter {key} path {raw!r}: {exc}") from exc
    if not stat.S_ISDIR(resolved_stat.st_mode):
        raise CutError(f"running interpreter {key} path {raw!r} is not a directory")
    return resolved


def _running_publisher_base() -> tuple[list[str | Path], Path | None]:
    """Command prefix and optional PYTHONPATH for the lu_machinery package running this cut.

    Used only before ``install()`` builds ``work/accept`` (preflight CI-receipt reads) and in
    standalone ``post-release``, so publisher calls never fall back to the consumer's stale
    ``host_python`` when running from a newer venv or source checkout.
    """
    import lu_machinery

    if not sys.executable:
        raise CutError("cannot locate the running Python interpreter for lu_machinery.publish")
    python = Path(sys.executable).absolute()
    package_file: object = getattr(lu_machinery, "__file__", None)
    if not isinstance(package_file, str) or not package_file:
        raise CutError("cannot locate the running lu_machinery package for lu_machinery.publish")
    try:
        resolved_package = Path(package_file).resolve(strict=True)
        package_stat = os.stat(resolved_package)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CutError(f"cannot resolve running lu_machinery package {package_file!r}: {exc}") from exc
    if not stat.S_ISREG(package_stat.st_mode) or resolved_package.parent.name != "lu_machinery":
        raise CutError(f"unexpected lu_machinery package path: {resolved_package}")
    package_root = resolved_package.parents[1]
    site_roots: list[Path] = []
    for key in ("purelib", "platlib"):
        raw = sysconfig.get_path(key)
        if not raw:
            continue
        resolved = _resolve_site_root(key, raw)
        if resolved is not None:
            site_roots.append(resolved)
    if package_root in site_roots:
        return [python, "-I", "-m", "lu_machinery.publish"], None
    root_text = str(package_root)
    if os.pathsep in root_text or "\n" in root_text or "\r" in root_text:
        raise CutError(f"unsafe lu_machinery package root for PYTHONPATH: {root_text!r}")
    return [python, "-P", "-s", "-m", "lu_machinery.publish"], package_root


@dataclass
class Cut:
    """One run's state: the work directory, its log and what has been published."""

    work: Path
    dry_run: bool
    machinery_remote: str
    product_remote: str
    machinery_repo: str
    product_repo: str
    host_python: Path
    primary: Path
    alert: bool
    x_agent: str
    started: float = field(default_factory=time.monotonic)
    head: str = ""
    version: str = ""
    previous: str = ""
    baseline: str = ""  # the last full release, where the notes start; previous unless that was withdrawn
    step: str = "start"
    built: str = ""
    ci: str = ""
    section: str = ""
    notes: str = ""
    uv: Path | None = field(default=None, init=False)
    # Fresh clones carry no identity. Commits and the annotated tag pass the registered
    # machinery checkout's user.name and user.email.
    git_identity: tuple[str, ...] = ()
    tag_pushed: bool = False  # the tag push was attempted; only the remote says whether it landed
    adopt_pr: int | None = None
    machinery_milestone: str | None = None
    product_milestone: str | None = None
    publisher_python: Path | None = None
    _consumer_lock: Path | None = field(default=None, init=False)

    # Plumbing

    def say(self, message: str) -> None:
        elapsed = int(time.monotonic() - self.started)
        line = f"[{elapsed // 60:02d}:{elapsed % 60:02d}] {message}"
        print(line, flush=True)

    def run(
        self, argv: Sequence[str | Path], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
        timeout: int = 1800, check: bool = True, stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in argv]
        with (self.work / "cut.log").open("a", encoding="utf-8") as handle:
            handle.write(f"$ {shlex.join(command)}\n")
            # An argv list built in this module from fixed verbs and checked values; no shell runs.
            result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
                command, cwd=cwd or self.work, env=dict(env) if env is not None else self.env(),
                input=stdin.decode() if stdin is not None else None, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
            handle.write(result.stdout + result.stderr + f"(exit {result.returncode})\n")
        if check and result.returncode:
            tail = (result.stderr or result.stdout).strip().splitlines()[-15:]
            raise CutError(f"{shlex.join(command[:6])} exited {result.returncode}: " + "\n".join(tail))
        return result

    def env(self, **extra: str) -> dict[str, str]:
        env = {key: value for key, value in sanitized_git_env(strip_config_injection=True).items()
               if key not in {"PYTHONPATH", "PYTHONHOME"}}
        extra.setdefault(PRIMARY_ROOT_ENV, str(self.primary))
        if not env.get(PRODUCT_ROOT_ENV):
            env[PRODUCT_ROOT_ENV] = str(self.primary)  # Legacy colocated cut.
        for setting, alias in ENV_ALIASES.items():
            if setting in extra:
                env.pop(alias, None)
            elif alias in extra:
                env.pop(setting, None)
        env.update(extra)
        return env

    def scratch_env(self, **extra: str) -> dict[str, str]:
        """Package tools and candidate code get a scratch home and no ambient credentials."""
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.work / "home"),
               "TMPDIR": str(self.work / "tmp"), "LANG": "C.UTF-8"}
        # Network configuration only: the npm install still needs the host's proxy.
        env.update({key: value for key, value in os.environ.items() if key.lower() in _PROXY_VARIABLES})
        env.update(extra)
        return env

    def venv(self, path: Path) -> Path:
        """A venv with no group- or other-writable file; returns its interpreter.

        venv copies its activate scripts with the source's mode (shutil.copymode), which the umask
        does not mask, and some Python installs ship them 0664.
        """
        self.run([self.host_python, "-I", "-m", "venv", path], env=self.scratch_env())
        for script in (path / "bin").iterdir():
            mode = script.lstat().st_mode
            if stat.S_ISREG(mode) and mode & 0o022:
                script.chmod(stat.S_IMODE(mode) & ~0o022)
        return path / "bin/python"

    def git(self, repo: Path, *args: str, **kwargs) -> str:
        return self.run(["git", "-C", repo, *args], **kwargs).stdout.strip()

    def committer(self) -> tuple[str, ...]:
        if not self.git_identity:
            raise CutError("no git user.name and user.email in the registered machinery checkout")
        return self.git_identity

    def alert_cto(self, body: str, key: str) -> None:
        if not self.alert:
            self.say("alert suppressed (--no-alert): " + body.splitlines()[0])
            return
        # The plane root resolves from cwd; the work directory is not a checkout, so anchor at the primary.
        self.run(
            [self.host_python, "-I", "-m", "lu_machinery.fleet_comms", "channel", "publish", "cto", "-",
             "--sender", "release-cut", "--source", "release", "--recipient", ALERT_RECIPIENT,
             "--idempotency-key", key],
            cwd=self.primary, stdin=body.encode(), timeout=120,
        )

    def publisher_invocation(self, *args: str) -> tuple[list[str | Path], dict[str, str]]:
        """Command and environment for ``lu_machinery.publish`` using the release's own code."""
        if self.publisher_python is not None:
            return [self.publisher_python.absolute(), "-I", "-m", "lu_machinery.publish", *args], self.env()
        base, package_root = _running_publisher_base()
        if package_root is None:
            return [*base, *args], self.env()
        env = {key: value for key, value in self.env().items() if not key.startswith("PYTHON")}
        env["PYTHONPATH"] = str(package_root)
        return [*base, *args], env

    def publisher(self, *args: str) -> str:
        argv, env = self.publisher_invocation(*args)
        return self.run(argv, env=env, timeout=600).stdout

    def verify_milestone(self, repo: str, milestone: str | None, *, label: str) -> tuple[int, str] | None:
        """Verify one configured release milestone against live repository milestones before publishing."""
        if milestone is None:
            return None
        checked = _validate_release_milestone(milestone, repo_label=f"{label} ({repo})")
        from lu_machinery.opsec.prepublish import PublishBlocked
        from lu_machinery.publish import github as publish_github

        try:
            return publish_github.resolve_repo_milestone(
                repo, checked, cwd=self.primary, env=self.env(),
            )
        except PublishBlocked as exc:
            raise CutError(str(exc)) from None

    def report_release_milestones(self, *, include_adopt: bool = True) -> None:
        """Verify and print the release PR milestones configured for this cut."""
        self.step = "release milestones"
        adopt = (
            self.verify_milestone(self.product_repo, self.product_milestone, label="adopt")
            if include_adopt
            else None
        )
        post = self.verify_milestone(self.machinery_repo, self.machinery_milestone, label="post-release")
        parts: list[str] = []
        if include_adopt:
            adopt_desc = f"{adopt[1]} (#{adopt[0]})" if adopt is not None else "none"
            parts.append(f"adopt ({self.product_repo})={adopt_desc}")
        post_desc = f"{post[1]} (#{post[0]})" if post is not None else "none"
        parts.append(f"post-release ({self.machinery_repo})={post_desc}")
        self.say("release milestones: " + ", ".join(parts))

    # Preflight and build

    def check_uv_requirement(self, uv: Path, manifest: str, *, side: Literal["machinery", "product"]) -> None:
        try:
            tool = _manifest_table(tomllib.loads(manifest), "tool")
            required = _manifest_table(tool, "uv").get("required-version")
        except (CutError, tomllib.TOMLDecodeError) as exc:
            raise UvPreflightError(side, "could not read [tool.uv] required-version from pyproject.toml") from exc
        if not isinstance(required, str) or not required.strip():
            raise UvPreflightError(side, "pyproject.toml must declare a nonempty [tool.uv] required-version")

        # Let uv apply its own version semantics to just this setting. cache dir needs no project or network.
        with tempfile.TemporaryDirectory(dir=self.work) as scratch:
            config = Path(scratch) / "uv.toml"
            config.write_text(f"required-version = {json.dumps(required, ensure_ascii=False)}\n", encoding="utf-8")
            try:
                self.run([uv, "--offline", "--config-file", config, "cache", "dir"], env=self.scratch_env(), timeout=30)
            except (CutError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
                raise UvPreflightError(
                    side, f"uv on PATH could not satisfy [tool.uv] required-version {required!r} (see cut.log)",
                ) from exc

    def prepare_uv(self, machinery_manifest: str) -> None:
        executable = shutil.which("uv")
        if executable is None:
            raise UvPreflightError("runner", "uv is not on PATH")
        uv = Path(executable).absolute()
        self.check_uv_requirement(uv, machinery_manifest, side="machinery")

        product = self.work / "product"
        # --depth needs a URL; a local checkout path becomes file://.
        local = Path(self.product_remote)
        remote = local.resolve().as_uri() if local.is_dir() else self.product_remote
        self.run(["git", "clone", "--quiet", "--depth", "1", "--branch", "main", "--", remote, product], timeout=900)
        try:
            manifest = self.git(product, "show", "HEAD:pyproject.toml")
        except (CutError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise UvPreflightError("product", "could not read pyproject.toml at product main") from exc
        self.check_uv_requirement(uv, manifest, side="product")
        if not (product / "uv.lock").is_file():
            raise UvPreflightError("product", "uv.lock is required on product main")
        self.uv = uv

    def preflight(self) -> tuple[Decision, list[str], str]:
        self.step = "preflight"
        self.committer()  # tag and commits need it; refuse before the clone and the wheel build
        clone = self.work / "machinery"
        self.run(["git", "clone", "--quiet", "--no-checkout", self.machinery_remote, clone], timeout=900)
        self.head = self.git(clone, "rev-parse", "refs/remotes/origin/main^{commit}")
        last_tag = self.git(clone, "describe", "--tags", "--abbrev=0", "--match", _TAG_GLOB, self.head)
        self.previous = last_tag.removeprefix("v")
        tag_commit = self.git(clone, "rev-parse", f"{last_tag}^{{commit}}")
        baseline = self.last_full_release(clone, last_tag)
        self.baseline = baseline.removeprefix("v")
        titles = self.git(clone, "log", "--first-parent", "--format=%s", f"{baseline}..{self.head}").splitlines()
        manifests = {name: self.git(clone, "show", f"{self.head}:{name}") for name in VERSION_FILES}
        versions: dict[str, str] = {}
        for name, manifest in manifests.items():
            versions.update(version_of(name, manifest))
        tags = self.git(clone, "tag", "--list", "v*").splitlines()
        changelog = self.run(["git", "-C", clone, "show", f"{self.head}:CHANGELOG.md"]).stdout
        runs = self.run(
            ["gh", "run", "list", "-R", self.machinery_repo, "--commit", self.head, "--workflow", CI_WORKFLOW,
             "--json", "headSha,conclusion,status,event,databaseId", "--limit", "20"],
            check=not self.dry_run, timeout=120,
        )
        try:
            run_rows = json_depth.loads(runs.stdout, max_depth=512) if runs.returncode == 0 else []
        except json.JSONDecodeError:
            run_rows = []
        if runs.returncode:
            self.say(f"CI Gate unverified here: gh run list exited {runs.returncode}")
        self.ci = ci_gate_state(run_rows, self.head)[1]
        decision = preflight(head=self.head, last_tag=last_tag, last_tag_commit=tag_commit, baseline=baseline,
                             titles=titles, versions=versions, tags=tags, runs=run_rows, changelog=changelog)
        self.version = decision.version
        if decision.action == "cut":
            self.verify_ci_receipt(clone, run_rows)
        if decision.action == "cut" or (self.dry_run and decision.action == "refuse"):
            self.prepare_uv(manifests["pyproject.toml"])
        return decision, titles, versions["pyproject.toml"]

    def verify_ci_receipt(self, clone: Path, run_rows: Sequence[Mapping[str, object]]) -> None:
        """Read only the newest successful main push's attempt-specific artifact."""
        newest = max((run for run in run_rows if run.get("headSha") == self.head and run.get("event") == "push"),
                     key=lambda run: int(str(run.get("databaseId", 0))))
        run_id = str(newest["databaseId"])
        target = json_depth.loads(self.publisher("read", "run", "--repo", self.machinery_repo, "--number", run_id), max_depth=512)
        if not isinstance(target, dict) or str(target.get("id")) != run_id:
            raise CutError("CI evidence target run id mismatch")
        attempt = target.get("run_attempt")
        if type(attempt) is not int or attempt < 1:
            raise CutError("CI evidence target run attempt is unavailable")
        destination = self.work / "ci-evidence"
        self.run(["gh", "run", "download", run_id, "-R", self.machinery_repo,
                  "--name", f"ci-gate-evidence-{attempt}", "--dir", destination], timeout=120)
        try:
            receipt = json_depth.loads((destination / "ci-gate-evidence.json").read_text(encoding="utf-8"), max_depth=512)
        except (OSError, ValueError) as exc:
            raise CutError("CI evidence receipt is missing or malformed") from exc
        source, source_jobs = None, []
        if isinstance(receipt, dict) and receipt.get("mode") == "reuse":
            binding = receipt.get("source")
            if not isinstance(binding, dict) or type(binding.get("run_id")) is not int or binding["run_id"] < 1:
                raise CutError("CI evidence source run id is invalid")
            source_id = str(binding["run_id"])
            source = json_depth.loads(self.publisher("read", "run", "--repo", self.machinery_repo, "--number", source_id), max_depth=512)
            source_attempt = binding.get("run_attempt")
            if type(source_attempt) is not int or source_attempt < 1:
                raise CutError("CI evidence source run attempt is invalid")
            jobs = json_depth.loads(self.run(["gh", "run", "view", source_id, "-R", self.machinery_repo,
                                       "--attempt", str(source_attempt), "--json", "jobs"], timeout=120).stdout, max_depth=512)
            source_jobs = jobs.get("jobs", [])
            if not isinstance(source_jobs, list) or not all(isinstance(job, dict) for job in source_jobs):
                raise CutError("CI evidence source jobs are malformed")
        tree = self.git(clone, "rev-parse", f"{self.head}^{{tree}}")
        validate_ci_receipt(receipt, repository=self.machinery_repo, target=target, commit=self.head, tree=tree,
                            source=source, source_jobs=source_jobs)
        self.ci += f"; {receipt['mode']} receipt verified on tree {tree}"

    def build(self, built_version: str) -> dict[str, Path]:
        self.step = "build"
        src, dist = self.work / "src", self.work / "dist"
        src.mkdir()
        dist.mkdir()
        archive = self.run(["git", "-C", self.work / "machinery", "archive", "--format=tar", "-o",
                            self.work / "src.tar", self.head])
        del archive
        self.run(["tar", "-x", "-f", self.work / "src.tar", "-C", src])
        uv = self.uv or shutil.which("uv")
        if uv is None:
            raise CutError("uv is not on PATH; the release build tools are exported from uv.lock")
        pins = self.work / "build-requirements.txt"
        exported = self.run(
            [uv, "export", "--locked", "--quiet", "--format", "requirements.txt",
             "--no-emit-project", "--only-group", "build",
             "--no-python-downloads", "--python", self.host_python],
            cwd=src,
            env=self.scratch_env(),
        ).stdout
        pins.write_text(exported, encoding="utf-8")
        build_python = self.venv(self.work / "build-venv")
        self.run([build_python, "-I", "-m", "pip", "install", "--no-cache-dir",
                  "--disable-pip-version-check", "--no-input", "--require-hashes", "--only-binary=:all:",
                  "--no-deps", "--requirement", pins], env=self.scratch_env())
        self.run([build_python, "-I", "-m", "build", "--sdist", "--wheel", "--no-isolation",
                  "--outdir", dist, src], env=self.scratch_env())
        files = {"wheel": dist / f"lu_machinery-{built_version}-py3-none-any.whl",
                 "sdist": dist / f"lu_machinery-{built_version}.tar.gz"}
        if sorted(path.name for path in dist.iterdir()) != sorted(path.name for path in files.values()):
            raise CutError(f"expected exactly {[p.name for p in files.values()]} in dist")
        sums = "".join(f"{sha256(path)}  {path.name}\n" for path in files.values())
        (dist / "SHA256SUMS").write_text(sums, encoding="utf-8")
        files["sums"] = dist / "SHA256SUMS"
        self.say(f"built {files['wheel'].name} sha256:{sha256(files['wheel'])}")
        return files

    def wheelhouse(self, files: Mapping[str, Path], built_version: str) -> Path:
        self.step = "wheelhouse"
        house = self.work / "wheelhouse"
        house.mkdir()
        # Export core and the runtime extras with hashes, as in CI. The
        # consumer lock keeps the bare release pin used by product CI.
        uv = self.uv or shutil.which("uv")
        if uv is None:
            raise CutError("uv is not on PATH; the release wheelhouse is exported from uv.lock")
        lock = self.work / "runtime-requirements.txt"
        exported = self.run([uv, "export", "--locked", "--quiet", "--format", "requirements.txt",
                             "--no-emit-project", "--no-default-groups", "--all-extras", "--no-extra", "test",
                             "--no-python-downloads", "--python", self.host_python],
                            cwd=self.work / "src", env=self.scratch_env()).stdout
        lock.write_text(exported, encoding="utf-8")
        # The build venv's pip, as in CI: an interpreter from `uv sync` has no pip of its own.
        self.run([self.work / "build-venv/bin/python", "-I", "-m", "pip", "download", "--no-cache-dir",
                  "--disable-pip-version-check",
                  "--no-input", "--require-hashes", "--only-binary=:all:", "--no-deps", "--dest", house,
                  "--requirement", lock], env=self.scratch_env())
        shutil.copy2(files["wheel"], house)
        consumer = self.work / "consumer.lock"
        consumer.write_text(
            f"lu-machinery=={built_version} \\\n    --hash=sha256:{sha256(files['wheel'])}\n"
            + wheel_only_requirements(exported, self.work / "src/uv.lock"),
            encoding="utf-8",
        )
        return consumer

    def _install_venv(self, venv: Path, consumer_lock: Path, built_version: str) -> Path:
        """Install the locked wheelhouse offline into ``venv``, run pip check, and verify isolated import."""
        python = self.venv(venv).absolute()
        self.run([python, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall",
                  "--no-index", "--find-links", self.work / "wheelhouse", "--require-hashes", "--only-binary=:all:",
                  "-r", consumer_lock], env=self.scratch_env())
        self.run([python, "-I", "-m", "pip", "check", "--disable-pip-version-check"], env=self.scratch_env())
        shown = self.run([python, "-I", "-c", _SHOW_LU], env=self.scratch_env()).stdout.split()
        if shown[0] != built_version or not Path(shown[1]).resolve().is_relative_to(venv.resolve()):
            raise CutError(f"clean environment imported {shown}, not {built_version} from {venv}")
        return python

    def install(self, consumer_lock: Path, built_version: str) -> Path:
        """Clean machinery-only environment: install, pip check and isolated import."""
        self.step = "clean install"
        python = self._install_venv(self.work / "accept", consumer_lock, built_version)
        self.publisher_python = python
        self._consumer_lock = consumer_lock
        self.say(f"clean install and import: lu-machinery {built_version} (publisher: {python} -I)")
        return python

    # Publish

    def publish(self, files: Mapping[str, Path], notes: str) -> None:
        self.step = "tag and publish"
        tag = f"v{self.version}"
        clone = self.work / "machinery"
        self.git(clone, *self.committer(), "tag", "-a", tag, self.head, "-m", f"lu-machinery {self.version}")
        self.notes = notes
        self.tag_pushed = True
        self.run(["git", "-C", clone, "push", self.machinery_remote, f"refs/tags/{tag}"], timeout=300)
        notes_file = self.work / "notes-prerelease.md"
        notes_file.write_text(notes, encoding="utf-8")
        self.publisher("release-create", "--repo", self.machinery_repo, "--tag", tag,
                       "--title", f"lu-machinery {self.version}", "--notes-file", str(notes_file),
                       "--prerelease", "--assets", str(files["wheel"]), "--assets", str(files["sdist"]),
                       "--assets", str(files["sums"]))
        published = self.work / "published"
        self.run(["gh", "release", "download", tag, "-R", self.machinery_repo, "-D", published], timeout=600)
        if sorted(path.name for path in published.iterdir()) != sorted(path.name for path in files.values()):
            raise CutError(f"published assets differ from the built files: {sorted(os.listdir(published))}")
        self.run(["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=published)
        for path in files.values():
            if sha256(published / path.name) != sha256(path):
                raise CutError(f"published {path.name} differs from the built file")
        self.say(f"published pre-release {tag}; downloaded assets match SHA256SUMS")

    def refresh_adopt_snapshot(self, product: Path) -> str | None:
        """Refresh the optional product snapshot with the verified candidate wheel."""
        manifest = product / "pyproject.toml"
        if not manifest.exists():
            return None
        try:
            manifest_data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            raise CutError("ADOPTION_SNAPSHOT_CONFIG: could not parse product pyproject.toml") from exc
        tool = _manifest_table(manifest_data, "tool")
        settings = _manifest_table(tool, "lu-machinery")
        if "adoption-snapshot" not in settings:
            return None
        self.step = "adopt snapshot"
        config = _manifest_table(settings, "adoption-snapshot")
        module, output = config.get("module"), config.get("output")
        if (set(config) != {"module", "output"}
                or not isinstance(module, str) or _ADOPTION_MODULE.fullmatch(module) is None
                or not isinstance(output, str) or _ADOPTION_OUTPUT.fullmatch(output) is None
                or {".", "..", ".git"}.intersection(output.split("/"))):
            raise CutError("ADOPTION_SNAPSHOT_CONFIG: expected a dotted Python module and relative .json output")
        module_file = module.replace(".", "/") + ".py"
        for relative in (module_file, output):
            path = product / relative
            path_error = f"ADOPTION_SNAPSHOT_PATH: expected a regular file in the product checkout: {relative}"
            try:
                if path.resolve(strict=True) != product.resolve(strict=True) / relative or not path.is_file():
                    raise CutError(path_error)
            except (OSError, RuntimeError) as exc:
                raise CutError(path_error) from exc
            try:
                self.git(product, "ls-files", "--error-unmatch", "--", relative)
            except CutError as exc:
                raise CutError(f"ADOPTION_SNAPSHOT_PATH: file is not tracked: {relative}") from exc
        if self.publisher_python is None:
            raise CutError("ADOPTION_SNAPSHOT_WHEEL: the verified candidate environment is missing")
        python = self._consumer_interpreter(self.publisher_python, product)
        rendered = self.run(
            [python, "-I", "-B", "-c", _RUN_ADOPTION_SNAPSHOT, module], cwd=product, timeout=120,
            env=self.scratch_env(LU_PRODUCT_ROOT=str(product), LEARN_UKRAINIAN_PRIMARY_REPO_ROOT=str(product),
                                 LEARN_UKRAINIAN_PYTHON=str(python)),
        ).stdout
        if self.git(product, "status", "--porcelain", "--untracked-files=all"):
            raise CutError("ADOPTION_SNAPSHOT_DIRTY: the snapshot module must write only to stdout")
        try:
            snapshot = json_depth.loads(rendered, max_depth=32)
        except (ValueError, RecursionError) as exc:
            raise CutError("ADOPTION_SNAPSHOT_JSON: the module must print one JSON object") from exc
        if not isinstance(snapshot, dict):
            raise CutError("ADOPTION_SNAPSHOT_JSON: the module must print one JSON object")
        (product / output).write_text(rendered, encoding="utf-8")
        self.say(f"measured adoption snapshot with lu-machinery {self.built}: {output}")
        return output

    def update_adopt_inputs(self, product: Path) -> None:
        """Update the product's uv pin and verify it resolves to the published wheel."""
        lock = product / "uv.lock"
        uv = self.uv or shutil.which("uv")
        if uv is None:
            raise CutError("uv is not on PATH; cannot refresh the product's uv.lock")
        manifest = product / "pyproject.toml"
        from lu_machinery.orchestration.fleet_repos import repository_slugs

        release_base = f"https://github.com/{self.machinery_repo}/releases/download"
        previous = tuple(f"https://github.com/{slug}/releases/download" for slug in repository_slugs(self.machinery_repo))
        updated, url = adopt_manifest(
            manifest.read_text(encoding="utf-8"), release_base, self.version, previous_bases=previous
        )
        manifest.write_text(updated, encoding="utf-8")
        self.run([uv, "lock", "--upgrade-package", "lu-machinery", "--exclude-newer-package", "lu-machinery=false",
                  "--no-python-downloads", "--python", self.host_python], cwd=product, env=self.scratch_env())
        selected = [package for package in _uv_packages(lock) if package["name"] == "lu-machinery"]
        wheel = self.work / "published" / url.rsplit("/", 1)[-1]
        expected_hash = f"sha256:{sha256(wheel)}"
        if (len(selected) != 1 or selected[0].get("version") != self.version
                or selected[0].get("source") != {"url": url}
                or [(row.get("url"), row.get("hash")) for row in selected[0].get("wheels", [])]
                != [(url, expected_hash)]):
            raise CutError("product uv.lock does not pin the verified published lu-machinery wheel")

    def open_adopt_pr(self, product: Path) -> None:
        """Draft a PR adopting the verified release into the product's uv lock."""
        snapshot = self.refresh_adopt_snapshot(product)
        self.step = "adopt PR"
        branch = f"release/lu-machinery-{self.version}"
        self.git(product, "switch", "--quiet", "-c", branch)
        self.update_adopt_inputs(product)
        verified = _ADOPT_VERIFIED
        files: tuple[str, ...] = ("pyproject.toml", "uv.lock")
        if snapshot is not None:
            files = (*files, snapshot)
            verified += ", candidate adoption snapshot"
        self.git(product, "add", "--", *files)
        self.git(product, *self.committer(), "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
                 f"chore(deps): adopt lu-machinery {self.version}\n\nX-Agent: {self.x_agent}\n{verified}")
        self.run(["git", "-C", product, "push", self.product_remote, f"HEAD:refs/heads/{branch}"], timeout=300)
        body = self.work / "adopt-body.md"
        pin_summary = "`pyproject.toml` points to the release wheel and `uv.lock` pins its verified SHA-256."
        if snapshot is not None:
            pin_summary += f" The release cut measured `{snapshot}` from the candidate wheel."
        body.write_text(
            f"Adopts [lu-machinery {self.version}](https://github.com/{self.machinery_repo}/releases/tag/"
            f"v{self.version}). {pin_summary}\n\n"
            "Opened as a draft by the release cut. It is marked ready when release acceptance passes; "
            "it still needs a review of record and a green CI Gate.\n",
            encoding="utf-8",
        )
        argv = ["pr-create", "--repo", self.product_repo, "--title",
                f"chore(deps): adopt lu-machinery {self.version}", "--body-file", str(body),
                "--base", "main", "--head", branch, "--draft"]
        if self.product_milestone is not None:
            argv.extend(["--milestone", _validate_release_milestone(self.product_milestone, repo_label=self.product_repo)])
        out = self.publisher(*argv)
        self.adopt_pr = pr_number(out)
        self.say(f"opened draft adopt PR #{self.adopt_pr}")

    # Acceptance

    def node_provision(self, python: Path) -> dict[str, str]:
        """docs/release.md's install-and-seal block, then admission of the sealed prefix."""
        self.step = "Node provisioning"
        env = self.scratch_env()
        prefix = self.run([python, "-I", "-c", f"{_ACPX}prepare_node_dependencies as p; print(p() or '')"],
                          env=env).stdout.strip()
        if prefix:
            tools = self.run([python, "-I", "-c", f"{_ACPX}node_toolchain as t; n, m = t(); print(n); print(m)"],
                             env=env).stdout.split()
            cache = self.work / "npm-cache"
            cache.mkdir()
            # main() holds the 022 umask for the whole cut, so the prefix copy above is covered too.
            self.run([tools[0], tools[1], "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                      f"--prefix={prefix}", f"--cache={cache}"],
                     env=self.scratch_env(PATH=f"{Path(tools[0]).parent}:{env['PATH']}"))
            self.run([python, "-I", "-c", _NODE_SEAL, cache], env=env)
        info: dict[str, str] = json_depth.loads(self.run([python, "-I", "-c", _NODE_INFO], env=env).stdout, max_depth=512)
        info["node_version"] = self.run([info["node"], "--version"], env=env).stdout.strip()
        info["npm_version"] = self.run([info["node"], info["npm"], "--version"], env=env).stdout.strip()
        self.say(f"Node prefix sealed and admitted: node {info['node_version']}, npm {info['npm_version']}")
        return info

    def node_refusals(self, venv: Path, info: Mapping[str, str]) -> None:
        """A removed package, an incompatible version and a tampered executable each refuse admission."""
        self.step = "Node refusal checks"
        venv = venv.resolve()
        prefix = Path(info["prefix"]).resolve().relative_to(venv)
        lock = json_depth.loads((venv / prefix / "package-lock.json").read_text(encoding="utf-8"), max_depth=512)
        direct = sorted(lock["packages"][""]["dependencies"])[0]
        shim = (venv / prefix / "node_modules/.bin/acpx").resolve(strict=True).relative_to(venv)

        def remove(root: Path) -> None:
            shutil.rmtree(root / prefix / "node_modules" / direct)

        def downgrade(root: Path) -> None:
            manifest = root / prefix / "node_modules" / direct / "package.json"
            data = json_depth.loads(manifest.read_text(encoding="utf-8"), max_depth=512)
            data["version"] = "0.0.0"
            manifest.write_text(json.dumps(data), encoding="utf-8")

        def tamper(root: Path) -> None:
            with (root / shim).open("ab") as handle:
                handle.write(b"\n// tampered by the release cut\n")

        check = ("from lu_machinery.agent_runtime.adapters.acpx import AcpxShadowRefusalError, "
                 "validate_node_dependencies as v\ntry:\n    v()\nexcept AcpxShadowRefusalError as exc:\n"
                 "    print('REFUSED', exc)\nelse:\n    print('ADMITTED'); raise SystemExit(1)\n")
        # The unmutated copy is the control: a copy alone must still be admitted.
        for name, mutate in (("unchanged copy", None), ("removed package", remove),
                             ("incompatible version", downgrade), ("tampered executable", tamper)):
            copy = self.work / f"tamper-{name.replace(' ', '-')}"
            self.run(["cp", "-a", venv, copy])
            try:
                if mutate is not None:
                    mutate(copy)
                result = self.run([copy / "bin/python", "-I", "-c", check], check=False,
                                  env=self.scratch_env(npm_config_offline="true"))
                refused = result.stdout.startswith("REFUSED")
                if mutate is None and (refused or result.returncode != 1):
                    raise CutError(f"Node admission refused an unchanged copy: {result.stdout.strip()}")
                if mutate is not None and (result.returncode or not refused):
                    raise CutError(f"Node admission accepted a {name}: {result.stdout.strip()}")
            finally:
                shutil.rmtree(copy)
        self.say("Node admission refused a removed package, an incompatible version and a tampered executable")

    def node_isolation(self, python: Path, info: Mapping[str, str]) -> None:
        """Planted marker packages run from outside the prefix and never from inside it."""
        self.step = "Node isolation checks"
        prefix, node = Path(info["prefix"]), info["node"]
        lock = json_depth.loads((prefix / "package-lock.json").read_text(encoding="utf-8"), max_depth=512)["packages"]
        names = {location.rsplit("node_modules/", 1)[-1] for location in lock if location}
        present = {location.rsplit("node_modules/", 1)[-1] for location in lock if location and (prefix / location).exists()}
        peers = {name for package in lock.values()
                 for name in (*package.get("peerDependencies", {}), *package.get("peerDependenciesMeta", {}))}
        markers = sorted(peers | (names - present))
        parent_modules, node_path = prefix.parent / "node_modules", self.work / "node-path"
        if parent_modules.exists():
            raise CutError(f"{parent_modules} already exists; the prefix's ancestors must hold no modules")
        record = self.work / "markers.log"
        for root in (parent_modules, node_path):
            for name in markers:
                package = root / name
                package.mkdir(parents=True)
                (package / "package.json").write_text(json.dumps({"name": name, "version": "0.0.0", "main": "index.js"}))
                (package / "index.js").write_text(
                    f"require('fs').appendFileSync({json.dumps(str(record))}, {json.dumps(name + chr(10))});\n")
        require_all = "for (const name of JSON.parse(process.argv[1])) require(name);"

        def ran(argv: Sequence[str | Path], cwd: Path, **extra: str) -> list[str]:
            record.write_text("")
            self.run(argv, cwd=cwd, env=self.scratch_env(**extra))
            return sorted(record.read_text().splitlines())

        try:
            # Positive controls: every marker executes when required from outside the prefix.
            if ran([node, "-e", require_all, json.dumps(markers)], prefix.parent) != markers:
                raise CutError("an ancestor node_modules marker did not run from outside the prefix")
            if ran([node, "-e", require_all, json.dumps(markers)], self.work, NODE_PATH=str(node_path)) != markers:
                raise CutError("a NODE_PATH marker did not run from outside the prefix")
            # The prefix's lazy require sites and the ACPX shim never reach a marker.
            executed = ran([node, "-e", _LAZY_CJS], prefix, DEBUG="release-cut", DEBUG_COLORS="1", FORCE_COLOR="3")
            executed += ran([node, "--input-type=module", "-e", _LAZY_ESM], prefix)
            executed += ran([prefix / "node_modules/.bin/acpx", "--version"], prefix, PATH="/usr/bin:/bin")
            if executed:
                raise CutError(f"prefix code executed planted markers: {sorted(set(executed))}")
            # react-native is the one condition that selects an absent peer; a direct run proves it.
            if "react-native-b4a" not in ran([node, "-e", "require('b4a')"], prefix,
                                             NODE_OPTIONS="--conditions=react-native", NODE_PATH=str(node_path)):
                raise CutError("the react-native-b4a positive control did not run")
        finally:
            shutil.rmtree(parent_modules, ignore_errors=True)
            shutil.rmtree(node_path, ignore_errors=True)
        self.run([python, "-I", "-c", f"{_ACPX}validate_node_dependencies as v; v()"], env=self.scratch_env())
        self.say(f"Node isolation: {len(markers)} marker(s) ran only from outside; prefix still admitted")

    def resources(self, python: Path) -> None:
        """CI's installed-wheel import check, then the deployer's apply and check on a scratch consumer."""
        self.step = "packaged resources"
        workflow = (self.work / "src/.github/workflows/ci-gate.yml").read_text(encoding="utf-8")
        script = self.work / "import_all.py"
        script.write_text(heredoc(workflow, 'cat > "$work/import_all.py" <<\'PY\''), encoding="utf-8")
        venv = python.parent.parent
        self.run([python, "-I", "-B", script, venv],
                 env=self.scratch_env(PATH="/usr/bin:/bin", LEARN_UKRAINIAN_PYTHON=str(python)))
        consumer = self.work / "agent-consumer"
        shared = consumer / "agents_extensions/shared"
        shared.mkdir(parents=True)
        self.run(["git", "init", "--quiet", consumer])
        inventory = json_depth.loads(self.run([python, "-I", "-c", _RELEASE_INVENTORY], env=self.scratch_env()).stdout, max_depth=512)
        descriptor = {"providers": ["claude", "codex", "agy"], "preserve": [], "sources": {
            "shared": {"path": "agents_extensions/shared", "required": True},
            "codex": {"path": "agents_extensions/codex", "required": True}}}
        (consumer / "agents_extensions/deployment.v1.json").write_text(json.dumps(descriptor))
        for provider, configs in inventory["required_hooks"].items():
            for requirements in configs.values():
                data: dict = {}
                for requirement in requirements:
                    node = data
                    for key in requirement["path"][:-1]:
                        node = node.setdefault(key, {})
                    node.setdefault(requirement["path"][-1], []).append({**requirement["group"],
                                                                         "hooks": requirement["hooks"]})
                path = shared / "settings.json" if provider == "claude" else consumer / "agents_extensions/codex/hooks.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data))
        deploy: list[str | Path] = [python, "-I", "-m", "lu_machinery.agent_deploy", "--project-root", consumer]
        self.run(deploy, env=self.scratch_env())
        self.run([*deploy, "--check"], env=self.scratch_env())
        self.say("every module imports from the installed wheel; deployer apply and check pass on a scratch consumer")

    def _consumer_interpreter(self, python: Path, product: Path) -> Path:
        """Give product commands the verified candidate first and product-only dependencies after it."""
        # During a release cut, keep the verified publisher venv (work/accept) completely unmutated so
        # promotion and rollback always run a pure machinery-only environment; attach the consumer .pth
        # overlay to a disposable offline install under work/tmp instead.
        same_venv = False
        if self.publisher_python is not None:
            same_venv = python.absolute() == self.publisher_python.absolute()
            if not same_venv:
                try:
                    same_venv = python.parent.parent.resolve(strict=True) == self.publisher_python.parent.parent.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise CutError(f"cannot resolve candidate environment path for collection: {exc}") from exc
        if same_venv:
            tmp_dir = self.work / "tmp"
            tmp_dir.mkdir(exist_ok=True)
            overlay_venv = Path(tempfile.mkdtemp(prefix="collection-", dir=tmp_dir))
            python = self._install_venv(overlay_venv, self._consumer_lock or (self.work / "consumer.lock"), self.built)
        paths = self.run([self.host_python, "-I", "-c", _SITE_PATHS]).stdout.split()
        site = self.run([python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).stdout.strip()
        lines = "".join(f"import site; site.addsitedir({path!r})\n" for path in dict.fromkeys(paths))
        Path(site, "zz-release-cut-consumer.pth").write_text(lines, encoding="utf-8")
        shown = self.run([python, "-c", _SHOW_LU], cwd=product, env=self.scratch_env()).stdout.split()
        if shown[0] != self.built or not Path(shown[1]).resolve().is_relative_to(Path(site).resolve()):
            raise CutError(f"overlay resolved lu-machinery {shown}, not the candidate")
        return python

    def consumer_collection(self, python: Path, product: Path) -> str:
        """Collect the product's tests with the candidate first and the product venv's packages after it (R5)."""
        self.step = "consumer test collection"
        python = self._consumer_interpreter(python, product)
        # The product's project_interpreter() reads LEARN_UKRAINIAN_PYTHON; the candidate names itself rather
        # than inheriting the caller's value or none.
        result = self.run([python, "-m", "pytest", "--collect-only", "-q", "tests"], cwd=product, timeout=1800,
                          env=self.env(LU_PRODUCT_ROOT=str(product), LEARN_UKRAINIAN_PYTHON=str(python)), check=False)
        summary = (result.stdout.strip().splitlines() or ["no output"])[-1]
        if result.returncode:
            raise CutError(f"product test collection failed (exit {result.returncode}): {summary}")
        self.say(f"product collection at {self.git(product, 'rev-parse', 'HEAD')[:12]}: {summary}")
        return summary

    def contract_canary(self) -> subprocess.CompletedProcess[str]:
        """The candidate's canary orchestration and sealed candidate receipt gate."""
        argv: list[str | Path] = [self.host_python, "-m", "lu_machinery.review.contract_canary", "run", "--repo",
                "machinery", "--head", self.head]
        return self.run([*argv, "--dry-run"] if self.dry_run else argv, timeout=2400, check=False,
                        env=self.env(PYTHONPATH=str(self.work / "src")))

    # Promotion and post-release

    def remove_rebuildable_scratch(self) -> None:
        """After success, retain evidence and remove only the named scratch entries."""
        self.step = "remove rebuildable scratch"
        root = self.work.resolve(strict=True)
        paths = [root / name for name in ("accept", "product", "npm-cache", "build-venv", "agent-consumer",
                                          "src", "src.tar", "home", "tmp")]
        # Validate every target before deleting any of them. Never follow a target outside the cut.
        for path in paths:
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or resolved == root:
                raise CutError(f"refusing scratch cleanup outside the work directory: {path}")
        for path in paths:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                # Sealed Node trees are read-only. Restore owner access without following their symlinks.
                path.chmod(path.stat().st_mode | stat.S_IRWXU)
                for directory, children, _files in os.walk(path, followlinks=False):
                    for name in children:
                        child = Path(directory) / name
                        if not child.is_symlink():
                            child.chmod(child.stat().st_mode | stat.S_IRWXU)
                shutil.rmtree(path)
        self.say("removed rebuildable scratch; release evidence retained")

    def promote(self, notes: str) -> None:
        self.step = "promote"
        tag = f"v{self.version}"
        notes_file = self.work / "notes-release.md"
        notes_file.write_text(notes, encoding="utf-8")
        self.publisher("release-edit", "--repo", self.machinery_repo, "--tag", tag,
                       "--notes-file", str(notes_file), "--no-prerelease", "--latest")
        latest = self.probe(
            ["gh", "api", f"repos/{self.machinery_repo}/releases/latest", "--jq", ".tag_name"],
            lambda viewed: viewed.stdout.strip() if viewed.returncode == 0 else None,
        )
        if latest != tag:
            raise CutError(f"expected {tag} as the latest release; GitHub returned {latest or 'unknown'}")
        if self.adopt_pr is not None:
            self.step = "mark adopt PR ready"
            self.publisher("pr-ready", "--repo", self.product_repo, "--number", str(self.adopt_pr))
        self.say(f"{tag} is the latest release; adopt PR #{self.adopt_pr} is ready for review")

    def probe[T](self, argv: Sequence[str | Path], parse: Callable[[subprocess.CompletedProcess[str]], T]) -> T | None:
        """One read of remote state. Any failure, a timeout included, is unknown (None), never an exception."""
        try:
            return parse(self.run(argv, check=False, timeout=120))
        except Exception:  # an unreadable probe leaves only its own value unknown
            return None

    def release_state(self, tag: str) -> str | None:
        """GitHub's release for ``tag``: ``released``, ``prerelease`` (withdrawn ones too), ``absent`` or None."""

        def parse(viewed: subprocess.CompletedProcess[str]) -> str | None:
            if viewed.returncode == 0:
                return "prerelease" if json_depth.loads(viewed.stdout, max_depth=512)["isPrerelease"] else "released"
            return "absent" if "release not found" in viewed.stderr.lower() else None

        return self.probe(["gh", "release", "view", tag, "-R", self.machinery_repo, "--json", "isPrerelease"], parse)

    def last_full_release(self, clone: Path, tag: str) -> str:
        """The newest tag at or before ``tag`` whose release is a full release.

        A pre-release (withdrawn or still in acceptance) or a tag without a release shipped nothing, so the
        walk passes over it. An unknown state refuses rather than guess which changes have shipped.
        """
        while (state := self.release_state(tag)) != "released":
            if state is None:
                raise CutError(f"release state of {tag} unknown; cannot tell which changes have shipped")
            tag = self.git(clone, "describe", "--tags", "--abbrev=0", "--match", _TAG_GLOB, f"{tag}^")
        return tag

    def remote_state(self) -> tuple[bool | None, str | None, list[int] | None]:
        """What GitHub holds for this version: the tag, the release and the open adopt PRs; None is unknown."""
        tag = f"v{self.version}"
        tagged = self.probe(["git", "ls-remote", "--exit-code", "--tags", self.machinery_remote, f"refs/tags/{tag}"],
                            lambda listed: {0: True, 2: False}.get(listed.returncode))
        release = self.release_state(tag)
        adopt = self.probe(["gh", "pr", "list", "-R", self.product_repo, "--head", f"release/lu-machinery-{self.version}",
                            "--state", "open", "--json", "number"],
                           lambda prs: [int(row["number"]) for row in json_depth.loads(prs.stdout, max_depth=512)] if prs.returncode == 0 else None)
        return tagged, release, adopt

    def _recovery_env_prefix(self) -> str:
        from lu_machinery.common.git_credentials import SELECTOR_ENV

        env = self.env(LU_PYTHON=str(self.host_python.absolute()))
        context = (PRODUCT_ROOT_ENV, SELECTOR_ENV, "LU_TASKS_DIR", *MACHINERY_ENV_VARS)
        argv = ["env"]
        for key in context:
            if not env.get(key):
                argv.extend(("-u", key))
        argv.extend(f"{key}={env[key]}" for key in context if env.get(key))
        return shlex.join(argv)

    def _manual_post_release_command(self, argv: Sequence[str]) -> str | None:
        """Shell-quoted publisher command for installed-venv / work/accept runs only.

        Source-checkout runs return None so manual recovery never emits a shell bootstrap that
        could fall through to a stale installed package if the checkout moves or a venv preloads.
        """
        try:
            pub_argv, pub_env = self.publisher_invocation(*argv)
        except CutError:
            return None
        if "PYTHONPATH" in pub_env or pub_argv[1:4] != ["-I", "-m", "lu_machinery.publish"]:
            return None
        prefix = self._recovery_env_prefix()
        return f"{prefix} {shlex.join(str(part) for part in pub_argv)}"

    def clean_up(self, cause: str) -> str:
        """After a failure past the tag push: act only on confirmed remote state, each step on its own.

        A full release or an unknown state is never withdrawn. A pushed tag whose release is absent or still a
        pre-release consumed the version: the pre-release is marked withdrawn, open adopt PRs are closed and the
        post-release PR records the version as yanked so the next cut tags the next one. Every step is attempted
        whatever happened to the others, and each outcome is reported.
        """
        tagged, release, adopt = self.remote_state()
        tag = f"v{self.version}"
        if tagged is False:
            return f"{tag} is not on the remote; nothing was published"
        if tagged is None or release is None:
            return f"remote state of {tag} unknown (tag {tagged}, release {release}); nothing withdrawn"
        if release == "released":
            if cause == "promote":
                return f"{tag} is a full release. Verify GitHub reports it as Latest before continuing adoption."
            env_prefix = self._recovery_env_prefix()
            x_agent_flag = f" --x-agent {shlex.quote(self.x_agent)}" if self.x_agent else ""
            no_alert_flag = "" if self.alert else " --no-alert"
            default_remote = f"https://github.com/{self.machinery_repo}.git"
            custom_remote = bool(self.machinery_remote) and self.machinery_remote != default_remote
            remote_flag = " --machinery-remote <original-machinery-remote>" if custom_remote else ""
            resume = (
                f"{env_prefix} {shlex.quote(str(self.publisher_python.absolute()))} -I -m lu_machinery.release_cut "
                f"post-release{x_agent_flag}{no_alert_flag}{remote_flag}"
                if self.publisher_python is not None
                else f"{env_prefix} release_cut post-release{x_agent_flag}{no_alert_flag}{remote_flag}"
            )
            return (f"{tag} is a full release; nothing withdrawn. Remaining: mark the adopt PR "
                    f"{adopt if adopt is not None else 'unknown'} ready, then run {resume}")
        done: list[str] = []

        def attempt(label: str, action) -> None:
            try:
                action()
                done.append(f"{label}: done")
            except Exception as exc:  # each obligation is independent of the others
                detail = (str(exc) or type(exc).__name__).splitlines()[0][:200]
                done.append(f"{label}: FAILED ({detail})")

        def withdraw() -> None:
            notes_file = self.work / "notes-withdrawn.md"
            notes_file.write_text(f"**Withdrawn:** release acceptance failed ({cause}). Do not adopt this "
                                  "version; the next version replaces it.\n\n" + self.notes, encoding="utf-8")
            self.publisher("release-edit", "--repo", self.machinery_repo, "--tag", tag, "--notes-file", str(notes_file))

        if release == "prerelease":
            attempt("mark the pre-release withdrawn", withdraw)
        for number in adopt or []:
            attempt(f"close adopt PR #{number}", lambda number=number: self.publisher(
                "pr-close", "--repo", self.product_repo, "--number", str(number)))
        if adopt is None:
            done.append("adopt PRs: unknown, none closed")
        attempt("post-release PR (yanked)", lambda: self.post_release(
            f"Withdrawn: release acceptance failed at {cause}.\n\n" + self.section, yanked=True))
        return f"{tag} {release}: " + "; ".join(done)

    def post_release(self, section: str, *, yanked: bool = False) -> int:
        """One PR: record the released section in CHANGELOG.md and move main to the next version."""
        self.step = "post-release PR"
        clone = self.work / "machinery"
        upcoming = next_version(self.version)
        branch = f"release/start-{upcoming}"
        self.run(["git", "-C", clone, "fetch", "--quiet", self.machinery_remote,
                  "+refs/heads/main:refs/remotes/origin/main"], timeout=300)
        self.git(clone, "switch", "--quiet", "-c", branch, "refs/remotes/origin/main")
        changelog = clone / "CHANGELOG.md"
        changelog.write_text(record_changelog(changelog.read_text(encoding="utf-8"), version=self.version,
                                              previous=self.previous, date=utc_date(), notes=section,
                                              repo=self.machinery_repo, yanked=yanked, since=self.baseline),
                             encoding="utf-8")
        for name in VERSION_FILES:
            path = clone / name
            path.write_text(bump_version_file(name, path.read_text(encoding="utf-8"), self.version, upcoming),
                            encoding="utf-8")
        version_files = list(VERSION_FILES)
        if (clone / "uv.lock").is_file():
            uv = shutil.which("uv")
            if uv is None:
                raise CutError("uv is not on PATH; cannot refresh uv.lock for the next version")
            self.run([uv, "lock", "--no-python-downloads", "--python", self.host_python], cwd=clone,
                     env=self.scratch_env())
            version_files.append("uv.lock")
        title = BUMP_TITLE.format(released=self.version, next=upcoming)
        self.git(clone, "add", "CHANGELOG.md", *version_files)
        self.git(clone, *self.committer(), "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
                 f"{title}\n\nX-Agent: {self.x_agent}\n{_POST_RELEASE_VERIFIED}")
        body = self.work / "post-release-body.md"
        body.write_text(
            f"Records the {self.version} section of `CHANGELOG.md`, generated from the merged PR titles in "
            f"`v{self.baseline}..v{self.version}`, and updates the version files to {upcoming}, "
            "the version the next release cut tags.\n\n"
            "Opened by the release cut (`docs/release.md`). It needs a review of record and a green CI Gate.\n",
            encoding="utf-8",
        )
        argv = ["pr-create", "--repo", self.machinery_repo, "--title", title,
                "--body-file", str(body.absolute()), "--base", "main", "--head", branch]
        if self.machinery_milestone is not None:
            argv.extend(["--milestone", _validate_release_milestone(self.machinery_milestone, repo_label=self.machinery_repo)])
        try:
            self.run(["git", "-C", clone, "push", self.machinery_remote, f"HEAD:refs/heads/{branch}"], timeout=300)
        except (CutError, subprocess.SubprocessError, OSError) as exc:
            manual = self._manual_post_release_command(argv)
            if manual is not None:
                self.say(
                    f"post-release push failed; if {branch} is already pushed on {self.machinery_repo} "
                    f"with no open PR, verify its commit and open the PR manually with:\n  {manual}"
                )
                raise CutError(
                    f"{exc}\n"
                    f"Manual recovery: if {branch} is already pushed on {self.machinery_repo} with no open PR, "
                    f"verify its commit and open the PR with:\n  {manual}"
                ) from exc
            milestone_step = (
                f", --milestone {shlex.quote(self.machinery_milestone)}"
                if self.machinery_milestone is not None
                else ""
            )
            guidance = (
                f"if {branch} is already pushed on {self.machinery_repo} with no open PR:\n"
                f"  1. Verify the pushed commit on {branch} and confirm no open PR exists.\n"
                f"  2. Follow docs/release.md to open the PR manually with "
                f"--repo {shlex.quote(self.machinery_repo)}, --title {shlex.quote(title)}, "
                f"--body-file {shlex.quote(str(body.absolute()))}, --base main, --head {shlex.quote(branch)}"
                f"{milestone_step}."
            )
            self.say(f"post-release push failed; {guidance}")
            raise CutError(f"{exc}\nManual recovery: {guidance}") from exc
        number = pr_number(self.publisher(*argv))
        self.say(f"opened post-release PR #{number}")
        return number


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def utc_date() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


def pr_number(output: str) -> int:
    match = re.search(r"/pull/([1-9][0-9]{0,18})(?!\w)", output)
    if match is None:
        raise CutError(f"no pull request URL in publisher output: {output.strip()[:200]}")
    return int(match.group(1))


def release_body(cut: Cut, section: str, files: Mapping[str, Path], consumer_lock: Path, ci: str,
                 acceptance: str) -> str:
    """Release notes: the changelog section, then build, identity and acceptance."""
    return (
        f"{section.rstrip()}\n\n## Build\n\n- Commit: `{cut.head}`\n- {ci}\n\n## Identity\n\n"
        f"```text\n{files['sums'].read_text(encoding='utf-8')}```\n\nConsumer lock:\n\n"
        f"```text\n{consumer_lock.read_text(encoding='utf-8')}```\n\n## Acceptance\n\n{acceptance.rstrip()}\n"
    )


def execute(cut: Cut) -> int:
    """Run the cut; returns the process exit code."""
    decision, titles, built = cut.preflight()
    cut.built = built
    cut.say(f"preflight: {decision.action}: {decision.reason}")
    if decision.action == "skip":
        return 0
    if decision.action == "refuse" and not cut.dry_run:
        raise CutError(decision.reason)
    cut.report_release_milestones(include_adopt=True)
    if cut.dry_run:
        cut.version = built
    section = cut.section = release_notes(titles, cut.machinery_repo)
    cut.say(f"release notes: {len(titles)} title(s) since v{cut.baseline}")
    files = cut.build(built)
    consumer_lock = cut.wheelhouse(files, built)
    python = cut.install(consumer_lock, built)
    product = cut.work / "product"
    ci = cut.ci
    pending = "Pre-release: acceptance is running. Adopt only after this release is no longer a pre-release.\n"
    if cut.dry_run:
        cut.refresh_adopt_snapshot(product)
    else:
        cut.publish(files, release_body(cut, section, files, consumer_lock, ci, pending))
        cut.open_adopt_pr(product)
    canary: dict[str, subprocess.CompletedProcess[str] | BaseException] = {}

    def run_canary() -> None:
        try:
            canary["result"] = cut.contract_canary()
        except BaseException as exc:  # reported after the local checks join
            canary["result"] = exc

    thread = threading.Thread(target=run_canary, name="contract-canary")
    thread.start()
    try:
        info = cut.node_provision(python)
        cut.node_refusals(python.parent.parent, info)
        cut.node_isolation(python, info)
        cut.resources(python)
        collected = cut.consumer_collection(python, product)
    finally:
        thread.join()
    cut.step = "contract canary"
    outcome = canary["result"]
    if isinstance(outcome, BaseException):
        raise CutError(f"contract canary did not run: {outcome}")
    table = [line for line in outcome.stdout.splitlines() if re.match(r"^[a-z]+: (PASS|FAIL)", line)]
    if outcome.returncode:
        raise CutError("contract canary failed: " + "; ".join(table or outcome.stderr.strip().splitlines()[-5:]))
    cut.say("contract canary: " + ("; ".join(table) if table else "dry-run plan printed, no live proof"))
    if cut.dry_run:
        notes = release_body(cut, section, files, consumer_lock, ci, "Dry run: nothing tagged or published.")
        (cut.work / "notes-dry-run.md").write_text(notes, encoding="utf-8")
        pub_argv, _ = cut.publisher_invocation()
        cut.say(
            f"dry run complete; publisher: {shlex.join(str(part) for part in pub_argv)}; "
            f"notes preview at {cut.work / 'notes-dry-run.md'}"
        )
        return 0
    prefix = Path(info["prefix"])
    acceptance = (
        f"- Clean machinery-only install of the consumer lock: `pip check` and isolated import of {cut.version}.\n"
        f"- Node {info['node_version']}, npm {info['npm_version']}; prefix "
        f"`share/lu-machinery/node/{prefix.name}` sealed and admitted; a removed package, an incompatible "
        "version and a tampered executable each refused; planted markers ran only from outside the prefix "
        "(react-native-b4a positive control included).\n"
        "- Every module imports from the installed wheel; agent deployer apply and `--check` pass on a scratch "
        "consumer.\n"
        f"- Product test collection from a fresh `main` with the candidate first: {collected}.\n"
        f"- Review-contract canary at `{cut.head}`: {'; '.join(table)}; receipt check passed."
    )
    cut.promote(release_body(cut, section, files, consumer_lock, ci, acceptance))
    post = cut.post_release(section)
    cut.alert_cto(
        f"release-cut: lu-machinery {cut.version} released from {cut.head}. Adopt PR "
        f"{cut.product_repo}#{cut.adopt_pr} is ready and post-release PR {cut.machinery_repo}#{post} is open; "
        "both need a review of record.",
        f"release-cut-{cut.version}-released",
    )
    cut.remove_rebuildable_scratch()
    cut.say(f"done: v{cut.version} released in {int(time.monotonic() - cut.started)} s")
    return 0


def _locked_run(args: argparse.Namespace) -> int:
    from lu_machinery.dispatcher.git import resolve_repo

    try:
        checkout, _ = resolve_repo("machinery")
        common = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=True, capture_output=True, text=True, env=sanitized_git_env(),
        ).stdout.strip()
        lock_path = Path(common) / "release-cut.lock"
        lock = lock_path.open("a")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"release cut refused: cannot acquire release-cut.lock: {exc}", file=sys.stderr)
        return 1
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            print(f"release cut refused: cannot acquire {lock_path}: {exc}", file=sys.stderr)
            return 1
        return _run(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lu_machinery.release_cut",
        description=__doc__.splitlines()[0] + "\nRun on the host with gh and push credentials, outside a worker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        f"  .venv/bin/python -I -m lu_machinery.release_cut cut --x-agent {_X_AGENT_EXAMPLE}\n"
        f"  .venv/bin/python -I -m lu_machinery.release_cut cut --dry-run --x-agent {_X_AGENT_EXAMPLE}\n"
        f"  .venv/bin/python -I -m lu_machinery.release_cut post-release --x-agent {_X_AGENT_EXAMPLE}\n"
        "  accept/bin/python -I -m lu_machinery.release_cut consumer-collection --python accept/bin/python \\\n"
        "      --product product --product-python product-venv/bin/python --version 0.1.24\n"
        "  .venv/bin/python -m lu_machinery.release_cut check-changelog\n"
        "Outputs: step lines on stdout; successful default runs archive cut.log in XDG state and remove work;\n"
        "caller-supplied work, failed and interrupted runs keep their work directory.\n"
        "Exit codes: 0 released, skipped as unchanged, dry run passed, consumer collection passed or changelog\n"
        "check passed; 1 refused or failed (cut and post-release alert claude-cto); 2 invalid input.\n"
        "Related: docs/release.md#cut-a-release.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("cut", "Tag main's HEAD as the next version, publish, accept and open adoption"),
                       ("post-release", "Open the post-release PR for the last tag (record changelog, bump)")):
        command = sub.add_parser(name, help=text, description=text)
        command.add_argument("--x-agent", required=True, type=partial(x_agent_value, command=name),
                             help=f"Required <agent>/<task-or-seat> trailer. The review recorder must resolve its "
                                  f"author family. Example: {_X_AGENT_EXAMPLE}")
        command.add_argument("--work", type=Path, help="Empty work directory (default: a new ~/.lu-release-cut-*)")
        command.add_argument("--machinery-remote", help="Machinery Git remote (default: its GitHub URL)")
        command.add_argument("--product-remote", help="Product Git remote or local path (default: its GitHub URL)")
        command.add_argument("--no-alert", action="store_true", help="Print instead of alerting claude-cto (default: alert)")
        if name == "cut":
            command.add_argument("--dry-run", action="store_true",
                                 help="Build, install, notes, Node, resources and collection; no tag, publish or PR")
    text = "Run the cut's consumer test collection step alone, against a given product checkout (CI Gate)"
    collect = sub.add_parser("consumer-collection", help=text, description=text)
    collect.add_argument("--python", type=Path, required=True,
                         help="Candidate interpreter: a clean environment that holds the wheel under test")
    collect.add_argument("--product", type=Path, required=True, help="Product checkout whose tests are collected")
    collect.add_argument("--product-python", type=Path, required=True,
                         help="Product environment's interpreter; its site-packages follow the candidate's")
    collect.add_argument("--version", required=True, help="lu-machinery version the candidate must resolve to")
    collect.add_argument("--work", type=Path, help="Empty work directory (default: a new temporary directory)")
    check_text = "Verify that CHANGELOG.md has an empty Unreleased section (CI Gate)"
    check_changelog = sub.add_parser("check-changelog", help=check_text, description=check_text)
    check_changelog.add_argument("--path", type=Path, default=Path("CHANGELOG.md"),
                                 help="Changelog file to check (default: CHANGELOG.md)")
    args = parser.parse_args(argv)
    if args.command == "check-changelog":
        return _check_changelog(args)
    # Runtime admission refuses group- or other-writable Node and agent artifacts, and every path the cut
    # writes (venvs, pip and npm installs, the prepared Node prefix) is made by this process or a child that
    # inherits its umask. Add 022 to the caller's mask for the whole run; a stricter mask is kept.
    previous_umask = os.umask(0o022)
    os.umask(previous_umask | 0o022)
    try:
        if args.command == "consumer-collection":
            return _consumer_collection(args)
        return _locked_run(args)
    finally:
        os.umask(previous_umask)


def _check_changelog(args: argparse.Namespace) -> int:
    """Run ``unreleased_must_be_empty`` on one regular changelog file for CI Gate."""
    path: Path = args.path
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                print(f"check-changelog failed ({path}): not a regular file", file=sys.stderr)
                return 1
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                text = handle.read()
        finally:
            if fd >= 0:
                os.close(fd)
        unreleased_must_be_empty(text)
    except (CutError, OSError, UnicodeError, ValueError) as exc:
        print(f"check-changelog failed ({path}): {exc}", file=sys.stderr)
        return 1
    print(f"CHANGELOG.md check passed: {path} Unreleased section is empty")
    return 0


def _consumer_collection(args: argparse.Namespace) -> int:
    """The cut's consumer_collection, unchanged, for CI: the product environment stands in for the host's."""
    work = (args.work or Path(tempfile.mkdtemp(prefix="lu-consumer-collection-"))).absolute()
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        print(f"--work {work} must be empty", file=sys.stderr)
        return 2
    for name in ("home", "tmp"):
        (work / name).mkdir()
    product = args.product.absolute()
    # Not resolved: a venv interpreter is a symlink, and resolving it leaves the venv.
    cut = Cut(work=work, dry_run=True, machinery_remote="", product_remote="", machinery_repo="", product_repo="",
              host_python=args.product_python.absolute(), primary=product, alert=False, x_agent="",
              built=args.version)
    try:
        cut.consumer_collection(args.python.absolute(), product)
    except (CutError, OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        cut.say(f"FAILED at {cut.step}: {exc}")
        return 1
    return 0


def _release_remote(flag: str, remote: str | None, row: Mapping[str, object]) -> str | None:
    """The Git remote a release override names, admitted only as a local path or the row's active GitHub origin.

    A live destination is an allowlist, and Git receives exactly what was checked: an existing local path
    (fixtures, mirrors), or a ``file://`` URI naming one with no remote host, becomes the absolute ``file://`` URI
    of that path, so neither the subprocess's working directory nor Git's URL and remote-name parsing can pick
    another destination. Otherwise it must be one of the row's active-slug origins (``canonical_origins``; ASCII,
    owner/name in any case), returned in its canonical spelling. Any other remote, including an inactive slug or
    another host, refuses.
    """
    from urllib.parse import unquote, urlsplit

    from lu_machinery.common.git_credentials import canonical_origins

    if not remote:
        return remote
    if remote.casefold().startswith("file:"):
        parts = urlsplit(remote)
        local = Path(unquote(parts.path)) if parts.scheme == "file" and parts.netloc in {"", "localhost"} else None
        if local is None or not local.is_absolute() or parts.query or parts.fragment or not local.exists():
            raise CutError(f"{flag} is a file URI that names no existing local path")
        return local.resolve().as_uri()
    if Path(remote).exists():
        return Path(remote).resolve().as_uri()
    key = row.get("key")
    if remote.isascii():
        for origin in canonical_origins(dict(row), key=key if isinstance(key, str) else None):
            if remote.casefold() == origin.casefold():
                return origin
    raise CutError(f"{flag} is neither a local path nor an origin of its repository's active GitHub slug")


def _run(args: argparse.Namespace) -> int:
    from lu_machinery.common.git_credentials import SelectorError, active_slug
    from lu_machinery.common.repo_root import primary_root, project_interpreter
    from lu_machinery.dispatcher.git import resolve_repo

    machinery_checkout, machinery = resolve_repo("machinery")
    _, product = resolve_repo()
    try:
        # Each row is cut, cloned and published at its active slug; a dual-slug row needs the host selector.
        machinery_repo, selector = active_slug(machinery, key=machinery.get("key"))
        product_repo, _ = active_slug(product, key=product.get("key"))
        machinery_override = _release_remote("--machinery-remote", args.machinery_remote, machinery)
        product_override = _release_remote("--product-remote", args.product_remote, product)
    except CutError as exc:
        print(f"release cut refused: {exc}", file=sys.stderr)
        return 1
    except (SelectorError, ValueError, OSError) as exc:
        print(f"release cut refused: fleet repository selector: {exc}", file=sys.stderr)
        return 1
    try:
        machinery_milestone = configured_release_milestone(machinery, repo_label=f"machinery ({machinery_repo})")
        product_milestone = configured_release_milestone(product, repo_label=f"product ({product_repo})")
    except CutError as exc:
        print(f"release cut refused: {exc}", file=sys.stderr)
        return 1
    work = (args.work or Path(tempfile.mkdtemp(prefix=".lu-release-cut-", dir=Path.home()))).absolute()
    work.mkdir(parents=True, exist_ok=True)
    if any(path.name != "home" for path in work.iterdir()):
        print(f"--work {work} must be empty", file=sys.stderr)
        return 2
    for name in ("home", "tmp"):
        (work / name).mkdir(exist_ok=True)
    dry_run = getattr(args, "dry_run", False)
    if not dry_run and os.environ.get("LEARN_UKRAINIAN_DISPATCH_TASK_ID"):
        print("cut and post-release run on the host, outside a dispatched worker; use cut --dry-run here",
              file=sys.stderr)
        return 2
    cut = Cut(work=work, dry_run=dry_run,
              machinery_remote=machinery_override or f"https://github.com/{machinery_repo}.git",
              product_remote=product_override or f"https://github.com/{product_repo}.git",
              machinery_repo=machinery_repo, product_repo=product_repo, host_python=project_interpreter(),
              primary=primary_root(), alert=not args.no_alert, x_agent=args.x_agent,
              machinery_milestone=machinery_milestone, product_milestone=product_milestone)
    name, email = (cut.git(machinery_checkout, "config", "--get", key, check=False) for key in ("user.name", "user.email"))
    if name and email:
        cut.git_identity = ("-c", f"user.name={name}", "-c", f"user.email={email}")
    cut.say(f"work directory {work}")
    if selector is not None:
        # The release receipt: the step line and the archived cut.log both name the selector the cut relied on.
        line = "fleet_repo_selector: " + json.dumps(selector.receipt(machinery.get("key")), sort_keys=True)
        cut.say(line)
        with (work / "cut.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    try:
        result = open_post_release(cut) if args.command == "post-release" else execute(cut)
    except (CutError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        failed_step = cut.step
        cause = f"{failed_step}: {exc}"
        cut.say(f"FAILED at {cause}")
        if cut.tag_pushed and not cut.dry_run:
            try:
                cleanup = cut.clean_up(failed_step)
            except (CutError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
                cleanup = f"remote state unreadable ({error}); nothing withdrawn"
            cut.say(f"cleanup: {cleanup}")
            cause += f"; {cleanup}"
        try:
            cut.alert_cto(f"release-cut FAILED ({'dry run' if cut.dry_run else 'cut'}) at {cause}\n"
                          f"main {cut.head or 'unknown'}; work directory {work} (cut.log).",
                          f"release-cut-{cut.head or 'nohead'}-{'dry' if cut.dry_run else 'cut'}-{failed_step}")
        except (CutError, OSError, subprocess.SubprocessError) as exc:
            print(f"alert to {ALERT_RECIPIENT} failed: {exc}", file=sys.stderr)
        return 1
    if result == 0 and args.work is None:
        try:
            state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "lu-release-cut"
            state.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
            log = state / f"{cut.version or cut.head or 'unknown'}-{stamp}.log"
            with (work / "cut.log").open("rb") as source, log.open("xb") as destination:
                shutil.copyfileobj(source, destination)
            cut.remove_rebuildable_scratch()
            shutil.rmtree(work)
            print(f"release log: {log}")
        except (CutError, OSError) as exc:
            print(f"release completed but work cleanup failed: {exc}; work directory: {work}", file=sys.stderr)
            return 1
    return result


def open_post_release(cut: Cut) -> int:
    """Bootstrap or retry: the post-release PR for the newest tag on main."""
    cut.report_release_milestones(include_adopt=False)
    pub_argv, pub_env = cut.publisher_invocation()
    prefix = f"PYTHONPATH={shlex.quote(pub_env['PYTHONPATH'])} " if "PYTHONPATH" in pub_env else ""
    cut.say(f"post-release publisher: {prefix}{shlex.join(str(part) for part in pub_argv)}")
    clone = cut.work / "machinery"
    cut.run(["git", "clone", "--quiet", "--no-checkout", cut.machinery_remote, clone], timeout=900)
    head = cut.git(clone, "rev-parse", "refs/remotes/origin/main^{commit}")
    tag = cut.git(clone, "describe", "--tags", "--abbrev=0", "--match", _TAG_GLOB, head)
    cut.version = tag.removeprefix("v")
    previous = cut.git(clone, "describe", "--tags", "--abbrev=0", "--match", _TAG_GLOB, f"{tag}^")
    cut.previous = previous.removeprefix("v")
    baseline = cut.last_full_release(clone, previous)
    cut.baseline = baseline.removeprefix("v")
    titles = cut.git(clone, "log", "--first-parent", "--format=%s", f"{baseline}..{tag}").splitlines()
    cut.post_release(release_notes(titles, cut.machinery_repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
