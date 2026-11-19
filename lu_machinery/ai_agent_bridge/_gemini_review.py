"""Gemini review eligibility checks shared by the bridge's command handlers."""

import json
import subprocess
from pathlib import Path, PurePosixPath

from lu_machinery.common import github_client, json_depth

# Native AGY admits low, medium and high risk code review through the reviewer resolver.
# ACP bridge reviews remain restricted to Ukrainian content.
GEMINI_REVIEW_PROFILE_CHOICES = ("code", "ukrainian")
GEMINI_REVIEW_AGENTS = frozenset({"agy", "gemini"})
GEMINI_CODE_REVIEW_FORBIDDEN = (
    "gemini_code_review_forbidden: this route requires Ukrainian content. "
    "Code review requires native AGY dispatch at low, medium or high risk "
    "through the reviewer resolver (model-assignment.md)"
)
AGY_REVIEW_PROFILE_REQUIRED = (
    "agy_review_profile_required: a review request to agy/gemini requires "
    "--review-profile {code,ukrainian}. "
    "Native AGY code review requires low, medium or high risk and resolver admission; "
    "the ACP bridge refuses code review. "
    "Ukrainian content review must pass --review-profile ukrainian."
)


def gemini_review_profile_error(profile: str | None, *, native_code_review: bool = False) -> str | None:
    """Require Ukrainian content unless native code review is explicitly enabled.

    ``None`` means the review may proceed. A missing profile names the flag.
    Native dispatch still needs the resolver's risk and security-path gates.
    ACP callers leave ``native_code_review`` false. Other profiles refuse.
    """
    normalized = (profile or "").strip().lower()
    if normalized == "ukrainian":
        return None
    if normalized == "code":
        return None if native_code_review else GEMINI_CODE_REVIEW_FORBIDDEN
    return AGY_REVIEW_PROFILE_REQUIRED


# Learner-content roots and the exact generated arc data files below are eligible
# for Gemini review; other site/src/data paths remain code/data surface.
_CONTENT_PREFIXES = (
    "curriculum/l2-uk-en/",
    "curriculum/l2-uk-direct/",
    "site/src/content/docs/",
    "wiki/",
)
# build_arc_landing.py consumes these schema-supported core levels. Its
# ARC_LANDING_LEVELS tracks landing ownership, not the accepted input levels.
_CONTENT_ARC_PATHS = frozenset(f"site/src/data/arc-{level}.json" for level in ("a1", "a2", "b1", "b2"))
# Track roots whose top level also carries code-imported manifests/data.
_CONTENT_TRACK_ROOTS = ("curriculum/l2-uk-en/", "curriculum/l2-uk-direct/")
# Data/code extensions that are never prose content anywhere under the roots.
_CONTENT_CODE_SUFFIXES = (".py", ".db", ".sqlite")
# Exact code-imported files inside the content roots.
_CONTENT_CODE_PATHS = frozenset(
    {
        "curriculum/l2-uk-direct/manifest.yaml",
        "curriculum/l2-uk-direct/bolshakova-letter-order.yaml",
        "curriculum/l2-uk-en/module-mapping.json",
        "curriculum/l2-uk-en/vocabulary.db",
    }
)


def _is_code_load_bearing_content(path: str) -> bool:
    """True for code-imported files inside the content roots.

    Importers (``grep -rln`` over ``scripts/``): ``curriculum/**/curriculum.yaml``
    (level_config, pipeline, generate_mdx, sync, manifest_utils); the
    l2-uk-direct ``manifest.yaml`` and ``bolshakova-letter-order.yaml``
    (direct-track build/validate, agent_router, audits);
    ``module-mapping.json`` (legacy TypeScript migrations);
    ``vocabulary.db`` (vocab, practice, audit checks, lexicon backfill). Any
    ``*.py`` / ``*.db`` / ``*.sqlite`` under the roots and any ``*.json`` at a
    track root is code/data surface by extension.
    """
    if path.startswith("curriculum/") and PurePosixPath(path).name == "curriculum.yaml":
        return True
    if path in _CONTENT_CODE_PATHS:
        return True
    if not path.startswith(_CONTENT_PREFIXES):
        return False
    if path.endswith(_CONTENT_CODE_SUFFIXES):
        return True
    return any(
        path.startswith(root) and "/" not in path[len(root) :] and path.endswith(".json")
        for root in _CONTENT_TRACK_ROOTS
    )


def is_content_class_path(path: str) -> bool:
    """True for learner content or an exact generated core-level arc data path."""
    normalized = path.replace("\\", "/")
    return normalized in _CONTENT_ARC_PATHS or (
        normalized.startswith(_CONTENT_PREFIXES) and not _is_code_load_bearing_content(normalized)
    )


class GeminiChangedPathListError(RuntimeError):
    """The changed-file list for a Gemini PR or branch review could not be read."""


def gemini_content_paths_error(paths: list[str]) -> str | None:
    """Allow a Gemini PR/branch review only when every path is Ukrainian content.

    The classifier is ``is_content_class_path``. An empty list is a listing
    failure: there is nothing to prove the diff is content. The first
    non-content path is named in listed order.
    """
    if not paths:
        raise GeminiChangedPathListError("changed-file list was empty")
    for path in paths:
        normalized = str(path).strip().replace("\\", "/")
        if not normalized or not is_content_class_path(normalized):
            shown = normalized or "<empty>"
            return f"{GEMINI_CODE_REVIEW_FORBIDDEN}; first non-content path: {shown}"
    return None


def _run_changed_path_command(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
) -> str:
    try:
        proc = github_client.run(
            command,
            fresh=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GeminiChangedPathListError(str(exc)) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        raise GeminiChangedPathListError(detail or f"exit {proc.returncode}")
    return proc.stdout or ""


def resolve_same_repo_pr_head(pr_number: int, *, repo_root: str) -> tuple[str, str]:
    """Return a same-repository PR branch and its live canonical head pin.

    The SHA is the only commit the path gate and the later dispatch may use.
    A second lookup is a different moment: callers keep this pair and pass the
    SHA through instead of asking GitHub again.
    """
    number = int(pr_number)
    if number < 1:
        raise GeminiChangedPathListError(f"refusing to list files for PR {pr_number!r}")
    from lu_machinery.review.target_resolution import (
        TargetResolutionError,
        _origin_github_repo,
        live_canonical_branch_head,
    )

    slug = _origin_github_repo(Path(repo_root))
    if slug is None:
        raise GeminiChangedPathListError("checkout origin is not a GitHub owner/name; refusing an unpinned gh pr view")
    raw = _run_changed_path_command(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            slug,
            "--json",
            "headRefName,headRefOid,isCrossRepository",
        ],
        cwd=repo_root,
    )
    try:
        payload = json_depth.loads(raw or "", max_depth=512)
    except json.JSONDecodeError as exc:
        raise GeminiChangedPathListError("gh pr view returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GeminiChangedPathListError("gh pr view returned a non-object payload")
    if payload.get("isCrossRepository") is not False:
        raise GeminiChangedPathListError("cross-repository PR")
    branch = payload.get("headRefName")
    sha = _full_git_sha(str(payload.get("headRefOid") or ""))
    if not isinstance(branch, str) or not branch.strip() or sha is None:
        raise GeminiChangedPathListError("PR payload has no head branch and full head SHA")
    from lu_machinery.common.git_context import UnsafeBranchNameError, validate_plain_branch_name

    try:
        branch = validate_plain_branch_name(branch, repo_root=repo_root)
    except UnsafeBranchNameError as exc:
        raise GeminiChangedPathListError(f"PR head branch {branch!r} is not a local branch name: {exc}") from exc
    try:
        sha = live_canonical_branch_head(Path(repo_root), branch, repository=slug)
    except TargetResolutionError as exc:
        raise GeminiChangedPathListError(str(exc)) from exc
    return branch, sha


def list_commit_changed_paths(sha: str, *, repo_root: str) -> list[str]:
    """Changed paths of one fetched commit against ``origin/main``.

    ``git fetch origin <sha>`` downloads that commit and does not move a branch
    ref. ``--no-renames`` keeps the pre-rename path, so a rename of
    ``scripts/x.py`` onto a content path still reports the old name.
    """
    pinned = _full_git_sha(sha)
    if pinned is None:
        raise GeminiChangedPathListError(f"refusing to diff non-SHA {sha!r}")
    from lu_machinery.common.git_context import sanitized_git_env

    git_env = sanitized_git_env()
    _run_changed_path_command(
        ["git", "-c", "core.hooksPath=/dev/null", "fetch", "origin", pinned],
        cwd=repo_root,
        env=git_env,
    )
    raw = _run_changed_path_command(
        ["git", "diff", "--name-only", "--no-renames", f"origin/main...{pinned}"],
        cwd=repo_root,
        env=git_env,
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _full_git_sha(value: str) -> str | None:
    sha = value.strip().lower()
    if len(sha) == 40 and all(char in "0123456789abcdef" for char in sha):
        return sha
    return None


def list_branch_changed_paths(
    branch: str,
    *,
    repo_root: str,
    head_out: list[str] | None = None,
) -> list[str]:
    """Changed paths on the remote branch Gemini actually reads.

    ``ask-agy --branch`` and ``delegate --branch`` mean the remote head.
    Fetch with an explicit refspec so a narrow clone updates
    ``refs/remotes/origin/<name>`` instead of only FETCH_HEAD, then diff the
    SHA that fetch resolved. A stale tracking ref is never the diff base.
    """
    from lu_machinery.common.git_context import (
        UnsafeBranchNameError,
        origin_tracking_refspec,
        sanitized_git_env,
        validate_plain_branch_name,
    )

    try:
        name = validate_plain_branch_name(branch, repo_root=repo_root)
    except UnsafeBranchNameError as exc:
        raise GeminiChangedPathListError(f"refusing to diff branch {branch!r}: {exc}") from exc
    git_env = sanitized_git_env()
    _run_changed_path_command(
        ["git", "-c", "core.hooksPath=/dev/null", "fetch", "origin", origin_tracking_refspec(name)],
        cwd=repo_root,
        env=git_env,
    )
    sha = _full_git_sha(
        _run_changed_path_command(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{name}"],
            cwd=repo_root,
            env=git_env,
        )
    )
    if sha is None:
        raise GeminiChangedPathListError(f"fetch of {name!r} did not resolve an exact SHA")
    if head_out is not None:
        head_out.append(sha)
    # ``--no-renames`` keeps the pre-rename path. Without it, a rename of
    # ``scripts/x.py`` onto a content path reports only the new name.
    raw = _run_changed_path_command(
        ["git", "diff", "--name-only", "--no-renames", f"origin/main...{sha}"],
        cwd=repo_root,
        env=git_env,
    )
    return [line.strip() for line in raw.splitlines() if line.strip()]


def gemini_pr_or_branch_content_error(
    *,
    pr_number: int | None,
    branch: str | None,
    repo_root: str,
    head_out: list[str] | None = None,
    head_sha: str | None = None,
) -> str | None:
    """Refuse a Gemini PR/branch target unless every changed path is content.

    A PR head is resolved once. The changed paths are ``git diff`` of that
    exact SHA (``origin/main...<sha>``), never a file list that can move
    between the request and the checkout. ``head_sha`` is that already
    resolved commit; when it is omitted and a PR number is set, this resolves
    it once. A branch with no PR fetches that remote head by explicit refspec
    and diffs the SHA the fetch resolved. ``head_out`` receives the SHA the
    diff used. Any failure to list files refuses. ``None`` means the target
    may proceed.
    """
    if head_sha is None and pr_number is None and not (branch and str(branch).strip()):
        return None
    try:
        if pr_number is not None and int(pr_number) < 1:
            raise GeminiChangedPathListError(f"refusing to list files for PR {pr_number!r}")
        if head_sha is not None or pr_number is not None:
            pinned = _full_git_sha(head_sha or "")
            if pinned is None:
                if pr_number is None:
                    raise GeminiChangedPathListError(f"refusing to diff non-SHA {head_sha!r}")
                _branch, pinned = resolve_same_repo_pr_head(int(pr_number), repo_root=repo_root)
            if head_out is not None:
                head_out.append(pinned)
            paths = list_commit_changed_paths(pinned, repo_root=repo_root)
        else:
            paths = list_branch_changed_paths(str(branch), repo_root=repo_root, head_out=head_out)
        return gemini_content_paths_error(paths)
    except GeminiChangedPathListError as exc:
        return f"{GEMINI_CODE_REVIEW_FORBIDDEN}; could not list changed files: {exc}"


_GEMINI_MODEL_ALIASES = frozenset({"gemini", "gemini-flash", "gemini-pro"})


def gemini_model_leaf(model: str | None) -> str:
    """Last slash segment of a model id, lowercased.

    ``google/gemini-3.1-pro`` and ``openrouter/google/gemini-3.1-pro`` share
    the leaf ``gemini-3.1-pro``.
    """
    text = (model or "").strip()
    if not text:
        return ""
    return text.casefold().rsplit("/", 1)[-1]


def is_gemini_family_model(model: str | None) -> bool:
    """True when the model id is Gemini-family, including provider-prefixed ids."""
    leaf = gemini_model_leaf(model)
    return leaf.startswith("gemini-") or leaf in _GEMINI_MODEL_ALIASES


def gemini_review_targets_model(
    *,
    agent: str,
    model: str | None = None,
    resolved_model: str | None = None,
) -> bool:
    """True when this review would run a Gemini-family model on any harness."""
    if (agent or "").strip().lower() in GEMINI_REVIEW_AGENTS:
        return True
    return is_gemini_family_model(model) or is_gemini_family_model(resolved_model)
