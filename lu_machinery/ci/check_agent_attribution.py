"""Fail a landed or proposed commit that carries no ``X-Agent`` attribution (#10736).

The contract is a declaration PARAGRAPH, not a Git trailer block. GitHub appends its own
``Co-authored-by`` paragraph to a squash message and copies each source body verbatim, so
an attribution is rarely the final paragraph and ``%(trailers)`` parsing cannot see it.

Declaration grammar
===================
1. The whole message is refused when it is larger than ``MAX_MESSAGE_BYTES``, contains
   NUL, VT, FF, U+001C-U+001E, NEL, U+2028 or U+2029, a bare CR (CRLF is normalised to
   LF first), the literal ``<!--`` or ``-->`` anywhere, or any raw HTML block. Refusal
   comes before every exemption.
2. The message is parsed with markdown-it-py's CommonMark preset. A declaration paragraph
   is a top-level paragraph whose every source line has the trailer shape
   ``Key: value`` (ASCII key ``[A-Za-z][A-Za-z0-9-]*``, one space, no outer whitespace),
   taken from the parser's line map over ``text.split("\\n")``, never rendered text.
3. Each exact ``X-Agent: <agent>/<task-id>`` line in a declaration paragraph is one
   value, unless an inline construct (code span, link, emphasis, image, multi-line HTML)
   spans into that line. Declaration paragraphs may appear anywhere; repeated values
   count once. ``X-Agent-Model`` and other keys may share the paragraph.
4. Any line whose key (the text before its first ``:``) lowercases to ``x-agent`` and is
   not an exact line is malformed, wherever it sits.

Fenced, indented, quoted, listed, heading and link-reference text is therefore an
example, never a declaration. Inline HTML outside a declaration line is allowed.

Modes
=====
``--merge-group BASE HEAD``
    ``BASE``/``HEAD`` are ``merge_group.base_sha``/``head_sha``. Each first-parent commit
    in ``BASE..HEAD`` is one pull request's squash commit with a terminal ``(#N)`` in its
    subject. For EACH commit this requires (a) that ``refs/pull/N/head`` is verifiably the
    squash's own source: merging that head into the squash's first parent with
    ``git merge-tree --write-tree`` must reproduce the squash's tree exactly; (b) at least
    one well-formed line; and (c) that every distinct value found over the PR's own
    commits also appears in the landed message, so one PR's attribution cannot mask
    another's omission and a requeue with new heads is checked afresh.
``--pr BASE HEAD``
    ``BASE`` and ``HEAD`` must share history (``git merge-base``); every non-merge commit
    in ``BASE..HEAD`` of the pull request then carries a well-formed line.

Bot exemption
=============
A commit with no line is exempt only when GitHub's REST commit object for that exact SHA
proves an allowlisted app bot (``scripts.review.app_bot_authorship``). A Git author name or
a ``[bot]`` suffix proves nothing. A merge-group commit with no line is exempt only when
EVERY commit of its PR is so verified; a PR that mixes bot and agent commits keeps the
agent attribution requirement.

Fail closed
===========
A git or fetch error, a missing ``(#N)``, an empty chain, a non-ancestor base, a
non-squash commit shape, a source history that cannot be tied to the squash
(``UNVERIFIABLE_SOURCE``), an unverifiable exemption, a refused message
(``REFUSED_MESSAGE`` and its reason) or a malformed value is a typed violation. Messages
carry codes, counts, PR numbers and short SHAs only; no message content (task ids
included) is ever echoed.

Report-only switch
==================
``X-Agent`` violations fail the run (exit 1) only when ``ATTRIBUTION_ENFORCE=1``. Any
other value prints the same findings as warnings and exits 0; invalid usage always
exits 2. The switch downgrades violations only: a crash of the checker itself (an
import error, an unexpected exception) exits non-zero in every mode. The step is
documented in ``docs/runbooks/agent-attribution.md``.

Verification line
=================
``X-Verified`` is a second key in the same declaration paragraph, not a Git trailer.
``git log --format=%(trailers)`` and ``git interpret-trailers`` walk backward from the
end of the message and miss a line once prose or GitHub's ``Co-authored-by`` block
follows it. This checker parses the declaration paragraph instead.

An exact line is one of:

    X-Verified: <commands or receipt ids>
    X-Verified: none (<reason>)

``<reason>`` is non-empty, has no surrounding whitespace, and contains no parenthesis.
A line whose key lowercases to ``x-verified`` and is not one of those two forms is
malformed wherever it sits, including inside a fence. A fence, quote, list or heading
never counts as a declaration. Commands and an explicit ``none`` cannot share a
message, and a message carries at most one ``none`` line. Several command lines are
one declaration; a repeated value counts once.

``--verified-mode off|observe|enforce`` (default ``observe``) applies only to this key.
``off`` does not check it. ``observe`` logs what ``enforce`` would block and exits 0.
``enforce`` fails the run on a missing, malformed, mixed or refused line. It does not
read ``ATTRIBUTION_ENFORCE``.

``--verified-report RANGE`` classifies each first-parent commit in ``RANGE``. A git
error or an empty range fails the query in every mode. ``observe`` prints ``status=missing``
and does not fail; ``enforce`` fails when any commit is missing, malformed, mixed or refused.

Examples
========

    .venv/bin/python -m lu_machinery.ci.check_agent_attribution --pr origin/main HEAD
    .venv/bin/python -m lu_machinery.ci.check_agent_attribution --merge-group "$BASE_SHA" "$HEAD_SHA"
    .venv/bin/python -m lu_machinery.ci.check_agent_attribution --verified-mode observe \\
        --verified-report origin/main~20..origin/main
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

from lu_machinery.common import github_client, json_depth
from lu_machinery.review.app_bot_authorship import verified_app_bot_login

ENFORCE_ENV = "ATTRIBUTION_ENFORCE"
DEFAULT_VERIFIED_MODE = "observe"
VERIFIED_MODES = ("off", "observe", "enforce")
BLOCKING_VERIFIED_STATUS = frozenset({"missing", "malformed", "mixed", "refused"})
GIT_TIMEOUT_SECONDS = 120
API_TIMEOUT_SECONDS = 30
_GITHUB_HTTPS = "https://github.com/"

# The largest landed message seen is under 48 KiB; the parser is quadratic on some inputs
# (a 64 KiB run of "*[" takes about a second), so larger messages are refused unparsed.
MAX_MESSAGE_BYTES = 64 * 1024
# Characters that some tool treats as a line break (or rewrites) while the parser does not.
_FORBIDDEN_CHARACTERS = re.compile("[\x00\x0b\x0c\x1c\x1d\x1e\x85  ]")
_COMMENT_MARKERS = ("<!--", "-->")
_MARKDOWN = MarkdownIt("commonmark")
_TRAILER_LINE = re.compile(r"[A-Za-z][A-Za-z0-9-]*: \S(?:.*\S)?", re.ASCII)
_ATTRIBUTION_KEY = "x-agent"
_VERIFIED_KEY = "x-verified"
_VERIFIED_NONE = re.compile(r"^X-Verified: none \((?P<reason>[^()\s](?:[^()]*[^()\s])?)\)\Z", re.ASCII)
_VERIFIED_COMMANDS = re.compile(r"^X-Verified: (?P<value>\S(?:.*\S)?)\Z", re.ASCII)
_WELL_FORMED = re.compile(r"^X-Agent: (?P<value>[a-z][a-z0-9]*(?:-[a-z0-9]+)*/[A-Za-z0-9._-]+)$")
_LINE_BREAKS = ("softbreak", "hardbreak")
# The squash headline ends in the PR number; earlier "(#N)" references are issues.
_PR_SUFFIX = re.compile(r"\(#(?P<number>[1-9][0-9]*)\)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# GET path -> decoded JSON; raises on any transport or API failure.
GitHubGet = Callable[[str], object]


class GitFailure(Exception):
    """A git command failed; carries a value-light description."""


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str
    commit: str | None = None
    pr: int | None = None

    def render(self) -> str:
        parts = [self.code]
        if self.pr is not None:
            parts.append(f"pr=#{self.pr}")
        if self.commit is not None:
            parts.append(f"commit={self.commit[:12]}")
        parts.append(self.detail)
        return " ".join(parts)


@dataclass(frozen=True)
class Attribution:
    """One message's well-formed values, malformed-line count and whole-message refusal code."""

    values: tuple[str, ...]
    malformed: int
    refusal: str | None = None


@dataclass(frozen=True)
class Paragraph:
    """A declaration paragraph: its parser line map and the LF-split source lines it covers."""

    map: tuple[int, int]
    lines: tuple[str, ...]
    declared: tuple[int, ...]  # indexes into ``lines`` outside every multi-line inline construct


@dataclass
class Report:
    mode: str
    checked: int = 0
    violations: list[Violation] = field(default_factory=list)


def _normalised(message: str) -> str:
    return message.replace("\r\n", "\n")


def _refusal(message: str) -> str | None:
    """The whole-message refusal code that needs no parse, else None."""
    if len(message.encode("utf-8", "surrogatepass")) > MAX_MESSAGE_BYTES:
        return "MESSAGE_TOO_LARGE"
    if _FORBIDDEN_CHARACTERS.search(message):
        return "FORBIDDEN_CHARACTER"
    if "\r" in _normalised(message):
        return "BARE_CR"
    if any(marker in message for marker in _COMMENT_MARKERS):
        return "COMMENT_MARKER"
    return None


def _declared_indexes(children: list[Token], count: int) -> tuple[int, ...]:
    """Lines of an inline token that no multi-line inline construct opens before or closes after.

    A code span, image or inline HTML that spans a line break swallows it, so the break count
    no longer matches the line count and no line of the paragraph is trusted. Links and
    emphasis keep their breaks as children; a line counts only at nesting depth zero on both
    of its ends.
    """
    if sum(child.type in _LINE_BREAKS for child in children) != count - 1:
        return ()
    declared: list[int] = []
    line = depth = depth_at_start = 0
    for child in children:
        if child.type in _LINE_BREAKS:
            if depth_at_start == depth == 0:
                declared.append(line)
            line, depth_at_start = line + 1, depth
        else:
            depth += child.nesting
    if depth_at_start == depth == 0:
        declared.append(line)
    return tuple(declared)


def _parse(text: str) -> list[Token] | None:
    """CommonMark block tokens of a normalised message, or None when it holds an HTML block."""
    tokens = _MARKDOWN.parse(text)
    return None if any(token.type == "html_block" for token in tokens) else tokens


def _paragraphs(tokens: list[Token], lines: list[str]) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    for index, token in enumerate(tokens):
        if token.type != "paragraph_open" or token.level != 0 or token.map is None:
            continue
        start, end = token.map
        source = tuple(lines[start:end])
        inline = tokens[index + 1]
        if not source or not all(_TRAILER_LINE.fullmatch(line) for line in source):
            continue
        # The parser's own text for the paragraph must be these exact lines; anything else
        # means its line numbering and ours disagree, and nothing in it is trusted.
        if inline.type != "inline" or inline.content != "\n".join(source):
            continue
        declared = _declared_indexes(inline.children or [], len(source))
        paragraphs.append(Paragraph((start, end), source, declared))
    return paragraphs


def declaration_paragraphs(message: str) -> tuple[Paragraph, ...]:
    """Every top-level paragraph of ``message`` made only of ``Key: value`` lines."""
    if _refusal(message) is not None:
        return ()
    text = _normalised(message)
    tokens = _parse(text)
    return () if tokens is None else tuple(_paragraphs(tokens, text.split("\n")))


def _is_candidate(line: str) -> bool:
    key, colon, _ = line.partition(":")
    return bool(colon) and key.lower() == _ATTRIBUTION_KEY


def extract_attribution(message: str) -> Attribution:
    """Declared ``X-Agent`` values (first-seen order, distinct), malformed count and refusal.

    See "Declaration grammar" in the module docstring.
    """
    refusal = _refusal(message)
    if refusal is not None:
        return Attribution((), 0, refusal)
    text = _normalised(message)
    lines = text.split("\n")
    malformed = sum(1 for line in lines if _is_candidate(line) and not _WELL_FORMED.fullmatch(line))
    tokens = _parse(text)
    if tokens is None:
        return Attribution((), malformed, "HTML_BLOCK")
    values: list[str] = []
    for paragraph in _paragraphs(tokens, lines):
        for index in paragraph.declared:
            strict = _WELL_FORMED.fullmatch(paragraph.lines[index])
            if strict is not None and strict.group("value") not in values:
                values.append(strict.group("value"))
    return Attribution(tuple(values), malformed)


@dataclass(frozen=True)
class Verification:
    """One message's ``X-Verified`` commands, explicit-none reasons, malformed count and refusal."""

    commands: tuple[str, ...]
    explicit_none: tuple[str, ...]
    malformed: int
    refusal: str | None = None


@dataclass(frozen=True)
class VerifiedRow:
    """Classification of one commit. ``status`` is missing, none, present, malformed, mixed or refused."""

    commit: str
    status: str


@dataclass
class VerifiedReport(Report):
    """Attribution report plus the ``X-Verified`` findings for the same commits."""

    verified: list[Violation] = field(default_factory=list)
    verified_rows: list[VerifiedRow] = field(default_factory=list)


def _is_verified_candidate(line: str) -> bool:
    key, colon, _ = line.partition(":")
    return bool(colon) and key.lower() == _VERIFIED_KEY


def _verified_kind(line: str) -> str | None:
    """``commands`` or ``none`` when ``line`` is an exact declaration, else None."""
    if _TRAILER_LINE.fullmatch(line) is None:
        return None
    if _VERIFIED_NONE.fullmatch(line) is not None:
        return "none"
    commands = _VERIFIED_COMMANDS.fullmatch(line)
    if commands is None:
        return None
    value = commands.group("value")
    if value == "none" or value.startswith("none "):
        return None
    return "commands"


def extract_verification(message: str) -> Verification:
    """Declared ``X-Verified`` commands and explicit-none reasons.

    Values come only from declaration paragraphs. A candidate that is not an exact
    line counts as malformed wherever it sits. See "Verification line" in the module docstring.
    """
    refusal = _refusal(message)
    if refusal is not None:
        return Verification((), (), 0, refusal)
    text = _normalised(message)
    lines = text.split("\n")
    malformed = sum(1 for line in lines if _is_verified_candidate(line) and _verified_kind(line) is None)
    tokens = _parse(text)
    if tokens is None:
        return Verification((), (), malformed, "HTML_BLOCK")
    commands: list[str] = []
    reasons: list[str] = []
    for paragraph in _paragraphs(tokens, lines):
        for index in paragraph.declared:
            line = paragraph.lines[index]
            kind = _verified_kind(line)
            if kind == "commands":
                value = line.split(": ", 1)[1]
                if value not in commands:
                    commands.append(value)
            elif kind == "none":
                match = _VERIFIED_NONE.fullmatch(line)
                reason = "" if match is None else match.group("reason")
                if reason and reason not in reasons:
                    reasons.append(reason)
    return Verification(tuple(commands), tuple(reasons), malformed)


def classify_from_found(found: Verification) -> str:
    """``missing``, ``none``, ``present``, ``malformed``, ``mixed`` or ``refused``."""
    if found.refusal is not None:
        return "refused"
    if found.malformed:
        return "malformed"
    if (found.commands and found.explicit_none) or len(found.explicit_none) > 1:
        return "mixed"
    if found.explicit_none:
        return "none"
    if found.commands:
        return "present"
    return "missing"


def classify_verification(message: str) -> str:
    """Classification of ``message``. See :func:`classify_from_found`."""
    return classify_from_found(extract_verification(message))


def verification_tokens(found: Verification) -> tuple[str, ...]:
    """Values a clean message declares, or nothing when the message is not clean."""
    if classify_from_found(found) not in {"present", "none"}:
        return ()
    if found.explicit_none:
        return tuple(f"none ({reason})" for reason in found.explicit_none)
    return found.commands


def verification_findings(message: str, sha: str, pr: int | None) -> list[Violation]:
    """What ``enforce`` would block for this message. Details carry no message text."""
    found = extract_verification(message)
    status = classify_from_found(found)
    if status == "refused":
        return [
            Violation(
                "REFUSED_MESSAGE",
                f"{found.refusal} (see the declaration grammar in docs/runbooks/agent-attribution.md)",
                sha,
                pr,
            )
        ]
    if status == "malformed":
        return [
            Violation(
                "MALFORMED_VERIFICATION",
                f"{found.malformed} X-Verified line(s) do not match the declaration grammar",
                sha,
                pr,
            )
        ]
    if status == "mixed":
        return [Violation("MIXED_VERIFICATION", "message mixes verification forms", sha, pr)]
    if status == "missing":
        return [Violation("MISSING_VERIFICATION", "no X-Verified line", sha, pr)]
    return []


def pr_number_from_subject(subject: str) -> int | None:
    """The terminal ``(#N)`` of a squash headline, else None."""
    match = _PR_SUFFIX.search(subject.rstrip())
    return int(match.group("number")) if match else None


def _git_bytes(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitFailure(f"git {args[0]} did not run: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise GitFailure(f"git {args[0]} exited {result.returncode}")
    return result.stdout


def _git(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    """Decoded output of a git command whose output is ours to parse (SHAs, trees, URLs)."""
    return _git_bytes(*args, env=env, cwd=cwd).decode("utf-8", "replace")


def _resolve_commit(ref: str) -> str:
    sha = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    if not _SHA.fullmatch(sha):
        raise GitFailure("rev-parse returned no commit")
    return sha


def _commit(sha: str) -> tuple[tuple[str, ...], str]:
    """(parents, raw message) of ``sha`` from the commit object, byte for byte.

    Read as bytes: a text-mode read would translate a bare CR into LF before the refusal
    rules could see it.
    """
    raw = _git_bytes("cat-file", "commit", sha)
    header, separator, message = raw.partition(b"\n\n")
    if not separator:
        raise GitFailure("commit object has no message separator")
    parents = tuple(
        line.split(b" ", 1)[1].decode("ascii") for line in header.split(b"\n") if line.startswith(b"parent ")
    )
    try:
        return parents, message.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitFailure("commit message is not UTF-8") from exc


def _bot_verified(get: GitHubGet | None, repository: str | None, sha: str) -> bool:
    """True only when GitHub's live REST object for exactly ``sha`` proves an allowlisted bot."""
    if get is None or repository is None or not _REPOSITORY.fullmatch(repository):
        return False
    try:
        commit = get(f"repos/{repository}/commits/{sha}")
    except Exception:
        return False
    return verified_app_bot_login(commit, sha, repository) is not None


def _fetch_pr_head(remote: str, pr: int) -> str:
    """Fetch ``refs/pull/N/head`` and return the commit it names."""
    env = None
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            url = _git("remote", "get-url", remote).strip()
        except GitFailure:
            url = ""
        if url.startswith(_GITHUB_HTTPS):
            # Header through the environment, never argv; credentials are not persisted.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env = {
                **os.environ,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{_GITHUB_HTTPS}.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
    _git("fetch", "--no-tags", "--quiet", remote, f"refs/pull/{pr}/head", env=env)
    return _resolve_commit("FETCH_HEAD")


def _range_commits(parent: str, head: str) -> tuple[str, ...]:
    """Non-merge commits reachable from ``head`` but not from ``parent``."""
    return tuple(_git("rev-list", "--no-merges", f"{parent}..{head}").split())


def _source_is_squash_of(squash: str, parent: str, head: str) -> bool:
    """True only when merging ``head`` into ``parent`` reproduces the tree of ``squash``.

    ``git merge-tree --write-tree`` performs the same three-way merge a squash does, from
    the merge base of ``parent`` and ``head``. Unrelated histories (no merge base), a
    conflict or any difference in the resulting tree make the fetched head unverifiable as
    the squash's source; the PR number alone never establishes that relation.
    """
    try:
        merged = _git("merge-tree", "--write-tree", parent, head).split("\n", 1)[0].strip()
        return _SHA.fullmatch(merged) is not None and merged == _git("rev-parse", f"{squash}^{{tree}}").strip()
    except GitFailure:
        return False


def _bad_attribution(attribution: Attribution, sha: str, pr: int | None) -> Violation | None:
    if attribution.refusal is not None:
        return Violation(
            "REFUSED_MESSAGE",
            f"{attribution.refusal} (see the declaration grammar in docs/runbooks/agent-attribution.md)",
            sha,
            pr,
        )
    if attribution.malformed:
        return Violation(
            "MALFORMED_ATTRIBUTION",
            f"{attribution.malformed} X-Agent line(s) do not match '<agent>/<task-id>'",
            sha,
            pr,
        )
    return None


def check_pr(base: str, head: str, *, github_get: GitHubGet | None = None, repository: str | None = None) -> VerifiedReport:
    """Every non-merge commit in ``base..head`` carries a well-formed line or is a verified bot's."""
    report = VerifiedReport("pr")
    try:
        base_sha, head_sha = _resolve_commit(base), _resolve_commit(head)
    except GitFailure as exc:
        report.violations.append(Violation("GIT_ERROR", str(exc)))
        return report
    try:
        _git("merge-base", base_sha, head_sha)
    except GitFailure:
        # No common history: BASE..HEAD would be the whole of an unrelated history.
        report.violations.append(
            Violation("UNVERIFIABLE_SOURCE", "base and head share no history; range is not the pull request's")
        )
        return report
    try:
        shas = _range_commits(base_sha, head_sha)
    except GitFailure as exc:
        report.violations.append(Violation("GIT_ERROR", str(exc)))
        return report
    if not shas:
        report.violations.append(Violation("EMPTY_CHAIN", "no non-merge commit in the range"))
        return report
    for sha in shas:
        report.checked += 1
        try:
            _, message = _commit(sha)
        except GitFailure as exc:
            report.violations.append(Violation("GIT_ERROR", str(exc), sha))
            continue
        attribution = extract_attribution(message)
        bad = _bad_attribution(attribution, sha, None)
        if bad is not None:
            report.violations.append(bad)
        elif not attribution.values and not _bot_verified(github_get, repository, sha):
            report.violations.append(
                Violation("MISSING_ATTRIBUTION", "no X-Agent line and no verified app-bot authorship", sha)
            )
        report.verified.extend(verification_findings(message, sha, None))
    return report


def check_merge_group(
    base: str,
    head: str,
    *,
    remote: str = "origin",
    github_get: GitHubGet | None = None,
    repository: str | None = None,
) -> VerifiedReport:
    """Each squash commit in the queue chain carries its PR's complete attribution."""
    report = VerifiedReport("merge-group")
    try:
        base_sha, head_sha = _resolve_commit(base), _resolve_commit(head)
        _git("merge-base", "--is-ancestor", base_sha, head_sha)
        chain = _git("rev-list", "--first-parent", f"{base_sha}..{head_sha}").split()
    except GitFailure as exc:
        report.violations.append(Violation("GIT_ERROR", f"base is not an ancestor of head or unreadable: {exc}"))
        return report
    if not chain:
        report.violations.append(Violation("EMPTY_CHAIN", "no commit between base and head"))
        return report
    for sha in chain:
        report.checked += 1
        try:
            parents, message = _commit(sha)
        except GitFailure as exc:
            report.violations.append(Violation("GIT_ERROR", str(exc), sha))
            continue
        if len(parents) != 1:
            report.violations.append(
                Violation(
                    "UNEXPECTED_SHAPE", f"queue commit has {len(parents)} parents; expected one squash commit", sha
                )
            )
            continue
        attribution_problems, verified_problems = _check_squash(
            sha, parents[0], message, remote=remote, github_get=github_get, repository=repository
        )
        report.violations.extend(attribution_problems)
        report.verified.extend(verified_problems)
    return report


def _check_squash(
    sha: str,
    parent: str,
    message: str,
    *,
    remote: str,
    github_get: GitHubGet | None,
    repository: str | None,
) -> tuple[list[Violation], list[Violation]]:
    """Attribution problems, then what ``X-Verified`` enforce would block.

    The attribution list is the same list this function returned before verification
    existed. Verification is collected on every return that has a message, including
    when attribution already failed.
    """
    subject = message.split("\n", 1)[0]
    pr = pr_number_from_subject(subject)
    landed_verified = verification_findings(message, sha, pr)

    def finish(
        problems: list[Violation], extra: list[Violation] | None = None
    ) -> tuple[list[Violation], list[Violation]]:
        verified = list(landed_verified)
        if extra:
            verified.extend(extra)
        return problems, verified

    if pr is None:
        return finish([Violation("MISSING_PR_REFERENCE", "subject does not end in (#N)", sha)])
    landed = extract_attribution(message)
    bad = _bad_attribution(landed, sha, pr)
    if bad is not None:
        return finish([bad])
    try:
        pr_head = _fetch_pr_head(remote, pr)
    except GitFailure as exc:
        return finish([Violation("FETCH_FAILED", f"cannot read refs/pull/{pr}/head: {exc}", sha, pr)])
    if not _source_is_squash_of(sha, parent, pr_head):
        return finish(
            [
                Violation(
                    "UNVERIFIABLE_SOURCE",
                    f"refs/pull/{pr}/head merged into the squash parent does not reproduce the squash tree",
                    sha,
                    pr,
                )
            ]
        )
    try:
        sources = _range_commits(parent, pr_head)
    except GitFailure as exc:
        return finish([Violation("FETCH_FAILED", f"cannot list refs/pull/{pr}/head: {exc}", sha, pr)])
    if not sources:
        return finish([Violation("EMPTY_SOURCE", f"refs/pull/{pr}/head adds no non-merge commit", sha, pr)])

    problems: list[Violation] = []
    expected: list[str] = []
    unattributed: list[str] = []
    source_verified: list[Violation] = []
    expected_verified: list[str] = []
    for source in sources:
        try:
            _, source_message = _commit(source)
        except GitFailure as exc:
            problems.append(Violation("GIT_ERROR", str(exc), source, pr))
            continue
        attribution = extract_attribution(source_message)
        bad = _bad_attribution(attribution, source, pr)
        if bad is not None:
            problems.append(bad)
        elif attribution.values:
            expected.extend(v for v in attribution.values if v not in expected)
        else:
            unattributed.append(source)
        source_verified.extend(verification_findings(source_message, source, pr))
        for token in verification_tokens(extract_verification(source_message)):
            if token not in expected_verified:
                expected_verified.append(token)
    landed_tokens = verification_tokens(extract_verification(message))
    omitted = [token for token in expected_verified if token not in landed_tokens]
    if omitted:
        source_verified.append(
            Violation(
                "INCOMPLETE_VERIFICATION",
                f"landed message omits {len(omitted)} of {len(expected_verified)} verification value(s)",
                sha,
                pr,
            )
        )
    if problems:
        return finish(problems, source_verified)

    if not landed.values:
        # Exempt only when every PR commit is a verified app bot's (no agent commit to attribute).
        if not expected and all(_bot_verified(github_get, repository, s) for s in sources):
            return finish([], source_verified)
        return finish(
            [Violation("MISSING_ATTRIBUTION", "landed message has no X-Agent line", sha, pr)],
            source_verified,
        )
    for source in unattributed:
        if not _bot_verified(github_get, repository, source):
            problems.append(
                Violation(
                    "SOURCE_UNATTRIBUTED",
                    "PR commit has no X-Agent line and no verified app-bot authorship",
                    source,
                    pr,
                )
            )
    missing = [value for value in expected if value not in landed.values]
    if missing:
        problems.append(
            Violation(
                "INCOMPLETE_ATTRIBUTION",
                f"landed message omits {len(missing)} of {len(expected)} PR value(s)",
                sha,
                pr,
            )
        )
    return finish(problems, source_verified)


def _github_get_from_env() -> GitHubGet | None:
    """Production getter: the shared GitHub client, authenticated from the CI environment.

    The client reads its credential from the environment, so it never appears on argv.
    A non-zero exit or an undecodable body raises, which callers treat as "not proven".
    """
    if not os.environ.get("GITHUB_TOKEN"):
        return None

    def get(path: str) -> object:
        # A commit's verification can change after it lands, so never answer from cache.
        completed = github_client.run(
            ["gh", "api", "-X", "GET", path],
            fresh=True,
            capture_output=True,
            text=True,
            timeout=API_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"GET {path} failed: gh exit {completed.returncode}")
        try:
            return json_depth.loads(completed.stdout, max_depth=512)
        except ValueError as exc:
            raise RuntimeError(f"GET {path} failed: undecodable response") from exc

    return get


def check_verified_report(range_spec: str) -> VerifiedReport:
    """Classify ``X-Verified`` on each first-parent commit in ``range_spec``.

    A git error or an empty range is a hard failure of the query (``report.violations``),
    not a missing line. The classification itself is in ``report.verified_rows``.
    """
    report = VerifiedReport("verified-report")
    if not range_spec or range_spec.startswith("-") or any(character in range_spec for character in "\n\r\t "):
        report.violations.append(Violation("GIT_ERROR", "range is empty or not a single revision range"))
        return report
    try:
        shas = tuple(sha for sha in _git("rev-list", "--first-parent", range_spec).split() if sha)
    except GitFailure as exc:
        report.violations.append(Violation("GIT_ERROR", str(exc)))
        return report
    if not shas:
        report.violations.append(Violation("EMPTY_CHAIN", "no commit in the range"))
        return report
    for sha in shas:
        report.checked += 1
        try:
            _, message = _commit(sha)
        except GitFailure as exc:
            report.violations.append(Violation("GIT_ERROR", str(exc), sha))
            continue
        status = classify_verification(message)
        report.verified_rows.append(VerifiedRow(sha, status))
        if status in BLOCKING_VERIFIED_STATUS:
            report.verified.extend(verification_findings(message, sha, None))
    return report


def _print_verified_report(report: VerifiedReport, mode: str) -> None:
    for violation in report.violations:
        print(f"::error title=verified-trailer::{violation.render()}")
    missing = 0
    blocked = 0
    for row in report.verified_rows:
        print(f"verified-report: commit={row.commit[:12]} status={row.status}")
        if row.status == "missing":
            missing += 1
        if row.status in BLOCKING_VERIFIED_STATUS:
            blocked += 1
    print(
        f"verified-report: mode={mode} commits={report.checked} missing={missing} "
        f"enforce_would_block={blocked} run_blocked={1 if mode == 'enforce' and blocked else 0}"
    )
    if mode == "observe" and blocked:
        print("verified-report: enforce would block these commits; observe does not fail the run")
    if mode == "off":
        print("verified-report: mode=off classifies only; it does not enforce")


def _print_verified_findings(report: VerifiedReport, mode: str) -> None:
    if mode == "off":
        print(f"verified-trailer: mode=off commits={report.checked} findings=0")
        return
    level = "error" if mode == "enforce" else "warning"
    for violation in report.verified:
        print(f"::{level} title=verified-trailer::{violation.render()}")
    print(f"verified-trailer: mode={mode} commits={report.checked} findings={len(report.verified)}")
    if report.verified and mode == "observe":
        print("verified-trailer: enforce would block these findings; observe does not fail the run")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--merge-group", nargs=2, metavar=("BASE", "HEAD"), help="check a merge queue chain")
    mode.add_argument("--pr", nargs=2, metavar=("BASE", "HEAD"), help="check every commit of a pull request")
    mode.add_argument(
        "--verified-report",
        metavar="RANGE",
        help="classify X-Verified on each first-parent commit in a git revision range",
    )
    parser.add_argument("--remote", default="origin", help="remote that serves refs/pull/N/head (default: origin)")
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="OWNER/REPO for app-bot verification (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--verified-mode",
        choices=VERIFIED_MODES,
        default=DEFAULT_VERIFIED_MODE,
        help="X-Verified check: off skips enforcement, observe logs it, enforce fails (default: observe)",
    )
    return parser


def main(argv: list[str] | None = None, *, github_get: GitHubGet | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verified_report:
        report = check_verified_report(args.verified_report)
        _print_verified_report(report, args.verified_mode)
        if report.violations:
            return 1
        blocked = sum(row.status in BLOCKING_VERIFIED_STATUS for row in report.verified_rows)
        return 1 if args.verified_mode == "enforce" and blocked else 0

    get = github_get if github_get is not None else _github_get_from_env()
    if args.merge_group:
        report = check_merge_group(*args.merge_group, remote=args.remote, github_get=get, repository=args.repository)
    else:
        report = check_pr(*args.pr, github_get=get, repository=args.repository)

    enforce = os.environ.get(ENFORCE_ENV) == "1"
    level = "error" if enforce else "warning"
    for violation in report.violations:
        print(f"::{level} title=agent-attribution::{violation.render()}")
    state = "enforce" if enforce else f"report-only ({ENFORCE_ENV}!=1)"
    print(f"agent-attribution: mode={report.mode} commits={report.checked} violations={len(report.violations)} {state}")
    if report.violations and not enforce:
        print(f"agent-attribution: not failing the run; set {ENFORCE_ENV}=1 to enforce")
    _print_verified_findings(report, args.verified_mode)
    verified_fails = bool(report.verified) and args.verified_mode == "enforce"
    return 1 if (report.violations and enforce) or verified_fails else 0


if __name__ == "__main__":
    sys.exit(main())
