"""Scan the text a git push publishes, from Git's own pre-push hook (#9339).

The agent git shim runs every push with core.hooksPath pinned to push_hooks/,
so Git runs this scanner on the exact ref updates it is about to send: one
stdin line per update, "<local ref> <local id> <remote ref> <remote id>".
Every line is read and validated before anything is allowed, and objects are
read by the local id Git supplied, never by resolving a name again, so a ref
that moves during the scan changes nothing. For a public destination the
remote ref names, the names and messages of the annotated tags on the way from
each sent id (nested tags included) and the whole raw messages of every commit
reachable from the sent ids, with the tag names and messages embedded in their
mergetag headers, are scanned through check_texts, except commits reachable
from the default branch of the registered public destination or of the
registered public parent of a fork owned by the authenticated publisher.
The scanner excludes that history only when every commit object reachable
from the GitHub-reported head hashes to its id. Nothing cached between pushes
or advertised by the Git transport establishes what is public. Enumeration
ignores replacement objects, grafts and commit-graph files. A grafted repository is refused, a shallow one
is refused when a boundary commit is inside the scan set, an alternate shallow
file is refused, and the scan's own git calls run without tracing or prompts.
File contents are not scanned. A deletion publishes no text.

A destination is exempt (private) only when is_private says so and Git reaches
it on the trusted transport route (trusted_route); otherwise it is scanned as
public. A malformed record, a scanner error, a missing matcher or the deadline
refuses the push. After a clean scan the caller's own pre-push hook runs with
the same arguments and input, and its status is the hook's status.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

# Executed by the hook as a file, so imports do not depend on the hook's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lu_machinery.common import json_depth
from lu_machinery.opsec import prepublish as gate

# The hooks directory the shim pins; chain runs the caller's own hook of a name.
HOOKS = Path(__file__).resolve().parent / "push_hooks"
COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# Hex length of an object id per repository object format.
ID_LENGTH = {"sha1": 40, "sha256": 64}
# The local ref Git names for a deletion, whose local id is all zeros.
DELETE = b"(delete)"
# The canonical public repository's default branch and the id form GitHub reports for its head.
PUBLIC_DEFAULT_BRANCH = "main"
PUBLIC_HEAD = re.compile(r"[0-9a-f]{40}")
# Timeout for the typed public repository reads.
API_TIMEOUT = 10
# Seconds the whole scan may take before the push is refused; LU_OPSEC_PUSH_SCAN_TIMEOUT overrides it.
SCAN_TIMEOUT = 300


class ScanTimeout(BaseException):
    """The deadline passed. Not an Exception, so no fail-soft handler on the way can absorb it."""


@contextmanager
def deadline(seconds: float):
    def expire(signum, frame):
        raise ScanTimeout

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def scan_timeout(environment: dict[str, str]) -> float:
    value = environment.get("LU_OPSEC_PUSH_SCAN_TIMEOUT")
    if value is None:
        return SCAN_TIMEOUT
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0
    if not 0 < seconds < float("inf"):
        raise gate.PublishBlocked("OPSEC: LU_OPSEC_PUSH_SCAN_TIMEOUT is not a positive number; push refused.")
    return seconds


def _is_local(url: str) -> bool:
    """git's url_is_local_not_ssh: no colon, or a slash before the first colon."""
    colon, slash = url.find(":"), url.find("/")
    return colon < 0 or 0 <= slash < colon


# RFC 3986 characters outside a query or fragment, less "?", "#", "[", "]" and "%". Each excluded character
# (those five, backslash, space, controls, non-ASCII) is one that curl, Git, ssh or urlsplit may read as a
# boundary differently, or drop; Git percent-decodes an ssh URL before it finds the host, so an encoded "/" or
# "@" would move it. Either way the host the push reaches would not be the one classified.
URL_CHARACTERS = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@/-]+")
# Git matches its built-in schemes case-sensitively; "HTTPS://" or "Ssh://" runs a git-remote-<scheme> helper from PATH.
HOSTED_URL = re.compile(r"(?:https?|ssh|git|git\+ssh|ssh\+git)://(?:[^@/]+@)?([^/:@]+)(?::(\d*))?/(.+)")
# Git ends the host of [user@]host:path at the first colon and ssh splits user@host at an @, so userinfo has neither.
SCP_URL = re.compile(r"(?:[^@/:]+@)?([^/:@]+):([^:].*)")


def destination(url: str) -> str:
    """Canonical host/owner/name for a push URL, or unknown (never private by default).

    Accepts the URL, ssh and scp-like ([user@]host:path) forms git uses, with or
    without userinfo, only when the host is the one Git, curl and ssh connect to:
    the URL holds only URL_CHARACTERS (so no query, fragment, bracketed host,
    percent-encoding or whitespace) and has at most one @, and for the URL forms
    the scheme is one of Git's own, spelled in lower case, and urlsplit reads the
    same host, a valid port and the same path. The path must be owner/name under
    that host. Local paths, file URLs, remote helpers and anything else without
    such a hosted identity are unknown, which counts as public.
    """
    if not URL_CHARACTERS.fullmatch(url) or _is_local(url):
        return "unknown"
    if "://" not in url:
        match = SCP_URL.fullmatch(url)
        if not match:
            return "unknown"
        host, path = match.groups()
        return _hosted(host, path)
    match = HOSTED_URL.fullmatch(url)
    if not match:
        return "unknown"
    host, port, path = match.groups()
    parts = urlsplit(url)
    try:
        # .port raises ValueError for a port outside 0-65535, which curl and Git refuse too.
        same = (parts.hostname, parts.port, parts.path) == (host.lower(), int(port) if port else None, f"/{path}")
    except ValueError:
        return "unknown"
    return _hosted(host, path) if same else "unknown"


def _hosted(host: str, path: str) -> str:
    """host/owner/name for an explicit host, never the normalizer's github.com owner/name shorthand."""
    segments = path.strip("/").split("/")
    if len(segments) != 2:
        return "unknown"
    return gate.normalize_repository("/".join([host, *segments]))


def scan_environment(environment: dict[str, str]) -> dict[str, str]:
    """The hook's environment for the scan's own git calls.

    Git's repository variables stay, so the scan reads the repository and
    object stores the push reads. GIT_CONFIG goes: it narrows what git config
    reports to one file, while the push reads every configuration source, so
    the scan's configuration reads see what the push uses. No override,
    tracing, credential prompts or partial-clone object fetches; replacement
    objects and grafts disabled. A trace2 target of "0" in the environment
    outranks one set in system or global configuration. Messages are
    untranslated.
    """
    scrubbed = {
        key: value
        for key, value in environment.items()
        if key not in (gate.OVERRIDE, "GIT_CONFIG", "GIT_CURL_VERBOSE") and not key.startswith("GIT_TRACE")
    }
    scrubbed.update(
        GIT_TRACE2="0",
        GIT_TRACE2_EVENT="0",
        GIT_TRACE2_PERF="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_GRAFT_FILE=os.devnull,
        LC_ALL="C",
    )
    return scrubbed


def _grafts_present(path: str) -> bool:
    """Whether git would read graft lines from path; anything unreadable counts as present."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISCHR(info.st_mode) and os.path.samefile(path, os.devnull):
        return False
    return not stat.S_ISREG(info.st_mode) or info.st_size > 0


def shallow_boundaries(path: str, length: int) -> set[str]:
    """The commit ids the shallow file at path lists; none when it is absent.

    git reads one id per line and treats each as parentless. Anything git might
    read differently (a non-regular or unreadable file, a line that is not
    exactly one lowercase id of this repository's length) refuses the push.
    """
    try:
        # Non-blocking: a FIFO here must not stall the scan; fstat then refuses it.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return set()
    except OSError:
        raise gate.PublishBlocked("OPSEC: shallow file unreadable; push refused.") from None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise gate.PublishBlocked("OPSEC: shallow file unreadable; push refused.")
            data = handle.read()
    except OSError:
        raise gate.PublishBlocked("OPSEC: shallow file unreadable; push refused.") from None
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()  # The newline ending the last id.
    identity = re.compile(rb"[0-9a-f]{%d}" % length)
    if not all(identity.fullmatch(line) for line in lines):
        raise gate.PublishBlocked("OPSEC: shallow file malformed; push refused.")
    return {line.decode("ascii") for line in lines}


class Transport(NamedTuple):
    """The Git a caller that scans before its own push sends with: binary, environment and leading options.

    A pre-push hook runs inside the push, so it inherits the push's environment and command-scope settings, and
    Git hands it the URL after rewriting. A caller that scans first (the write boundary's broker) names its
    transport here instead, and every Git call of the scan, the trusted-route proof included, runs with exactly
    this binary, environment and options: the push's configuration sources, command-scope settings and
    repository, never the scanner process's own. The scanner keeps its process environment for its own lookups
    (catalogue, publisher reads, override, deadline).
    """

    git: str
    environment: dict[str, str]
    options: tuple[str, ...]

    @classmethod
    def from_json(cls, text: str) -> Transport:
        value = json_depth.loads(text, max_depth=512)
        git, environment, options = value["git"], value["environment"], value["options"]
        if not (
            isinstance(git, str)
            and os.path.isabs(git)
            and isinstance(environment, dict)
            and all(isinstance(key, str) and isinstance(item, str) for key, item in environment.items())
            and isinstance(options, list)
            and all(isinstance(option, str) for option in options)
        ):
            raise gate.PublishBlocked("OPSEC: push transport malformed; push refused.")
        return cls(git, environment, tuple(options))


def real_git(environment: dict[str, str]) -> str:
    """The git the shim pushed with; otherwise git from PATH, which Git starts with its own exec path."""
    pinned = environment.get("LU_OPSEC_REAL_GIT", "")
    return pinned if os.path.isabs(pinned) and os.access(pinned, os.X_OK) else "git"


class Repository:
    """Read-only object queries through the real git in the hook's repository."""

    def __init__(
        self, git: str, environment: dict[str, str], *, pin_hooks: bool = True, options: tuple[str, ...] = ()
    ):
        # options are a caller transport's own leading options (Transport), so every query reads its
        # configuration and repository; the scan's settings below follow them and win for their own keys.
        # Replacement objects change what readers see but not what a pack sends,
        # and a commit-graph file is a local cache of parents that git trusts
        # without rehashing, so every query reads the original objects (the
        # flag and these -c options outrank caller configuration).
        self.base = [
            git,
            *options,
            "--no-replace-objects",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.commitGraph=false",
            *(["-c", f"core.hooksPath={os.devnull}"] if pin_hooks else []),
            "-c",
            "advice.graftFileDeprecated=false",
        ]
        self.caller = environment
        self.environment = scan_environment(environment)

    def run(
        self, *args: str, stdin: str | None = None, timeout: int = 60, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [*self.base, *args],
            input=stdin.encode() if stdin is not None else None,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            env=self.environment if environment is None else environment,
            check=False,
            timeout=timeout,
        )

    def raw(self, *args: str, stdin: str | None = None, environment: dict[str, str] | None = None) -> bytes:
        result = self.run(*args, stdin=stdin, environment=environment)
        if result.returncode:
            # Fixed text only: git output can quote ref names and messages.
            raise gate.PublishBlocked(
                f"OPSEC: push scan step git {args[0]} failed (exit {result.returncode}); push refused."
            )
        return result.stdout

    def text(self, *args: str, stdin: str | None = None, environment: dict[str, str] | None = None) -> str:
        return self.raw(*args, stdin=stdin, environment=environment).decode("utf-8", "replace")

    def config(self) -> list[tuple[str, str | None]]:
        """Every configuration entry in reading order; a valueless entry has value None."""
        entries = []
        for entry in self.text("config", "--list", "-z").split("\0"):
            if entry:
                key, newline, value = entry.partition("\n")
                entries.append((key, value if newline else None))
        return entries

    def object_format(self) -> str:
        found = self.text("rev-parse", "--show-object-format").strip()
        if found not in ID_LENGTH:
            raise gate.PublishBlocked("OPSEC: repository object format unknown; push refused.")
        return found

    def layout(self, object_format: str) -> tuple[bool, set[str]]:
        """Whether grafts alter commit parents, and the shallow boundary commits.

        git's own path resolution names both files, so a linked worktree reads
        the shallow file of the shared repository.
        """
        # Resolve the grafts path with the caller's GIT_GRAFT_FILE: the file the push reads.
        environment = {key: value for key, value in self.environment.items() if key != "GIT_GRAFT_FILE"}
        if "GIT_GRAFT_FILE" in self.caller:
            environment["GIT_GRAFT_FILE"] = self.caller["GIT_GRAFT_FILE"]
        output = self.text(
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/grafts",
            "--git-path",
            "shallow",
            environment=environment,
        )
        grafts, shallow = output.splitlines()
        return _grafts_present(grafts), shallow_boundaries(shallow, ID_LENGTH[object_format])


class CanonicalPublicRepository:
    """Resolve public history through the catalogue and the typed publisher."""

    def __init__(self, repo: str, environment: dict[str, str], *, runner=None, timeout: int = API_TIMEOUT):
        self.repo = repo
        self.environment = gate.internal_environment(environment)
        self.runner = runner
        self.timeout = timeout
        self.calls = 0
        self._fork_parent_head: str | None = None

    def from_catalog(self) -> CanonicalPublicRepository | None:
        """Select the public destination or its verified public parent."""
        destination = self.repo
        if destination == "unknown":
            return None
        rows = [
            (gate.normalize_repository(str(row.get("github", ""))), str(row.get("role", "")).startswith("public-"))
            for row in gate.catalog().values()
        ]
        matches = [public for repo, public in rows if repo == destination]
        if matches:
            # Keep the registered-destination rule: exactly one public row.
            return self if matches.count(True) == 1 else None
        if gate.normalize_repository(destination) != destination:
            return None
        host, owner, name = destination.split("/")
        if host != "github.com" or owner in {".", ".."} or name in {".", ".."}:
            return None

        try:
            from lu_machinery.common.github_client import parse_graphql_envelope

            result = self._read("fork-public-head")
            observation = getattr(result, "github_result", None)
            if result.returncode != 0 or (observation is not None and observation.stale):
                return None
            data = parse_graphql_envelope(result.stdout)["data"]
            repository = data["repository"]
            viewer = data["viewer"]["login"]
            slug = repository["nameWithOwner"]
            if (
                repository["isFork"] is not True
                or not viewer.isascii()
                or viewer.lower() != owner
                or not slug.isascii()
                or slug.lower() != f"{owner}/{name}"
            ):
                return None
            parent = repository["parent"]
            parent_slug = parent["nameWithOwner"]
            if not parent_slug.isascii() or parent["visibility"] != "PUBLIC":
                return None
            parent_repo = f"github.com/{parent_slug.lower()}"
            if [public for repo, public in rows if repo == parent_repo] != [True]:
                return None
            oid = parent["defaultBranchRef"]["target"]["oid"]
            if not isinstance(oid, str) or PUBLIC_HEAD.fullmatch(oid) is None:
                return None
        except Exception:
            return None
        self.repo = parent_repo
        self._fork_parent_head = oid
        return self

    def _read(self, operation: str) -> subprocess.CompletedProcess[str]:
        from lu_machinery.publish.github import read

        self.calls += 1
        result: subprocess.CompletedProcess[str] = read(
            operation,
            repo=self.repo,
            runner=self.runner,
            env=dict(self.environment),
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        return result

    def default_head(self) -> str | None:
        """Return the verified fork-parent head or read the registered destination's head.

        For a registered destination the reply must name the repository, the default branch
        by its expected name, and a 40-character lowercase hex commit id, with
        no GraphQL errors.
        """
        if self._fork_parent_head is not None:
            return self._fork_parent_head
        try:
            result = self._read("default-head")
            body = json_depth.loads(result.stdout, max_depth=512)
            repository = body["data"]["repository"]
            branch = repository["defaultBranchRef"]
            oid = branch["target"]["oid"]
            bound = (
                result.returncode == 0
                and "errors" not in body
                and isinstance(repository["nameWithOwner"], str)
                and gate.normalize_repository(repository["nameWithOwner"]) == self.repo
                and branch["name"] == PUBLIC_DEFAULT_BRANCH
                and isinstance(oid, str)
                and PUBLIC_HEAD.fullmatch(oid) is not None
            )
        except Exception:
            return None
        return oid if bound else None


def parse_updates(data: bytes, length: int) -> list[tuple[bytes, str, str, str]]:
    """(local ref, local id, remote ref, remote id) for every line Git wrote; anything malformed refuses.

    The local ref can be any expression the caller typed, spaces included, so
    the line is split from the right; ref names have no spaces. Ids are
    lowercase hex of this repository's length, a remote ref starts with refs/,
    and a deletion is exactly the (delete) local ref with the all-zero local id.
    Input that does not end with a newline is cut short and refuses as well.
    """
    if not data:
        return []
    malformed = gate.PublishBlocked("OPSEC: pre-push input malformed; push refused.")
    if not data.endswith(b"\n"):
        raise malformed
    identity = re.compile(rb"[0-9a-f]{%d}" % length)
    zero = b"0" * length
    updates = []
    for line in data[:-1].split(b"\n"):
        fields = line.rsplit(b" ", 3)
        if len(fields) != 4:
            raise malformed
        local_ref, local_id, remote_ref, remote_id = fields
        if (
            not local_ref
            or not identity.fullmatch(local_id)
            or not identity.fullmatch(remote_id)
            or not remote_ref.startswith(b"refs/")
            or any(byte < 0x20 or byte == 0x7F for byte in remote_ref)
            or (local_id == zero) != (local_ref == DELETE)
        ):
            raise malformed
        updates.append((local_ref, local_id.decode(), remote_ref.decode("utf-8", "replace"), remote_id.decode()))
    return updates


def trusted_route(repository: Repository, remote: str, url: str) -> bool:
    """Whether Git reaches url on the route the private exemption trusts.

    That is https with certificate verification, or ssh (URL or scp-like form)
    through the default ssh program, with Git's own exec path and no remote
    helper. A configured ssh program (GIT_SSH_COMMAND, GIT_SSH, core.sshCommand),
    a remote helper (<transport>::<address>, remote.<name>.vcs), another exec
    path or disabled certificate verification can deliver elsewhere, so such a
    destination is scanned as public, and so is a URL that a rewrite
    (url.<base>.insteadOf or pushInsteadOf) or a remote section of that name
    would redirect.
    """
    caller = repository.caller
    value = url.strip()
    lowered = value.lower()
    https = lowered.startswith("https://")
    ssh = lowered.startswith(("ssh://", "git+ssh://", "ssh+git://")) or ("://" not in value and not _is_local(value))
    if "::" in value or not (https or ssh):
        return False
    git = repository.base[0]
    if not os.path.isabs(git):
        return False
    default = {key: item for key, item in repository.environment.items() if key != "GIT_EXEC_PATH"}
    standard = repository.text("--exec-path", environment=default).strip()
    if "GIT_EXEC_PATH" in caller and os.path.realpath(caller["GIT_EXEC_PATH"]) != os.path.realpath(standard):
        return False
    entries = repository.config()
    keys = {key for key, _ in entries}
    if f"remote.{remote}.vcs" in keys:
        return False
    # A hook receives the URL Git pushes to, after rewriting; a Transport caller names the URL it asked for. A
    # rewrite that applies to it, or a remote section of its name (a URL push reads remote.<url>.pushurl),
    # sends the push somewhere the destination does not name. Git rewrites by plain prefix: any match counts.
    if any(
        key.startswith("url.")
        and key.endswith((".insteadof", ".pushinsteadof"))
        and (item is None or value.startswith(item))
        for key, item in entries
    ):
        return False
    if remote == url and any(key.startswith(f"remote.{remote}.") for key in keys):
        return False
    if ssh:
        return not ({"GIT_SSH_COMMAND", "GIT_SSH"} & caller.keys() or "core.sshcommand" in keys)
    if "GIT_SSL_NO_VERIFY" in caller:
        return False
    verify = repository.run("config", "--type=bool", "--get-urlmatch", "http.sslverify", value)
    if verify.returncode not in (0, 1):
        return False
    return verify.returncode == 1 or verify.stdout.strip() == b"true"


def _short_ref(target: str) -> tuple[str, str]:
    for prefix, kind in (("refs/heads/", "branch"), ("refs/tags/", "tag")):
        if target.startswith(prefix):
            return kind, target[len(prefix) :]
    return "ref", target


def split_object(raw: bytes, position: str) -> tuple[list[tuple[bytes, bytes]], str]:
    """Header fields and the whole message of a raw commit or tag object.

    The message is everything after the first empty line, as git reads it,
    NUL bytes included. It is decoded as UTF-8 and, when an encoding header
    names another codec that decodes it differently, that reading follows, so
    the scan covers what either kind of reader shows. A continuation line (one
    leading space) is unfolded into the value of the field it continues, as
    git reads a mergetag header. An object without that empty line, or whose
    headers open with a continuation, would deliver text no reader calls a
    message, so it is refused naming only position.
    """
    # A leading newline lets an object that opens with the empty line split like any other.
    header, separator, body = (b"\n" + raw).partition(b"\n\n")
    if not separator:
        raise gate.PublishBlocked(f"OPSEC: {position} has no header/body separator; push refused.")
    # Lines are collected and joined once per field, so unfolding stays linear in the object size.
    folded: list[tuple[bytes, list[bytes]]] = []
    for line in header[1:].split(b"\n"):
        if line.startswith(b" "):
            if not folded:
                raise gate.PublishBlocked(f"OPSEC: {position} headers malformed; push refused.")
            folded[-1][1].append(line[1:])
        elif line:
            key, _, value = line.partition(b" ")
            folded.append((key, [value]))
    fields = [(key, b"\n".join(lines)) for key, lines in folded]
    message = body.decode("utf-8", "replace")
    declared = next((value for key, value in fields if key == b"encoding"), None)
    if declared is not None:
        try:
            other = body.decode(declared.decode("ascii"))
        except (LookupError, UnicodeError, ValueError):
            other = message
        if other != message:
            message += "\n" + other
    return fields, message


def tag_name(fields: list[tuple[bytes, bytes]]) -> str:
    """Every tag header value of a split tag object."""
    return "\n".join(value.decode("utf-8", "replace") for key, value in fields if key == b"tag")


def commit_texts(repository: Repository, shas: list[str]) -> list[tuple[str, list[tuple[str, str]]]]:
    """(id, [(field, text)]) of each commit, read from the raw objects.

    %B in a --format stops at the first NUL, so a message with a NUL would be
    scanned only up to it. Reading the raw object scans every byte of the
    message. A merge of a signed tag carries the whole tag object in a mergetag
    header, so its name and message are published with the commit and scanned
    as its fields. Identity lines and signatures are not.
    """
    found: list[tuple[str, list[tuple[str, str]]]] = []
    for sha, raw in zip(shas, read_commits(repository, shas), strict=True):
        fields, message = split_object(raw, f"commit[{sha[:12]}]")
        parts = [("message", message)]
        embedded = [value for key, value in fields if key == b"mergetag"]
        for number, value in enumerate(embedded, start=1):
            name = f"mergetag[{number}]"
            tag_fields, tag_message = split_object(value, f"commit[{sha[:12]}].{name}")
            parts += [(f"{name}.tagname", tag_name(tag_fields)), (f"{name}.message", tag_message)]
        found.append((sha, parts))
    return found


def read_commits(repository: Repository, shas: list[str]) -> list[bytes]:
    """The raw bytes of each commit, in order, from one cat-file batch; anything else refuses."""
    output = repository.raw("cat-file", "--batch", stdin="".join(f"{sha}\n" for sha in shas))
    found: list[bytes] = []
    position = 0
    try:
        for sha in shas:
            end = output.index(b"\n", position)
            name, kind, size = output[position:end].decode("ascii").split(" ")
            start = end + 1
            stop = start + int(size)
            if name != sha or kind != "commit" or output[stop : stop + 1] != b"\n":
                raise ValueError
            found.append(output[start:stop])
            position = stop + 1
    except (ValueError, UnicodeError):
        raise gate.PublishBlocked("OPSEC: push scan commit read malformed; push refused.") from None
    if position != len(output):
        raise gate.PublishBlocked("OPSEC: push scan commit read malformed; push refused.")
    return found


def history_verified(repository: Repository, head: str, object_format: str) -> bool:
    """Whether every commit the walk from head reaches has bytes that hash to its id.

    git trusts stored bytes without rehashing them, and a local or alternate
    object store can hold altered bytes under a genuine id. A commit whose bytes
    match its id records its parents' true ids, so when every commit reached
    from the authoritative head verifies, the walk is the true public history.
    A missing or unreadable commit counts as unverified.
    """
    try:
        shas = repository.text("rev-list", head).split()
        objects = read_commits(repository, shas)
    except gate.PublishBlocked:
        return False
    return bool(shas) and all(
        hashlib.new(object_format, b"commit %d\0" % len(raw) + raw).hexdigest() == sha
        for sha, raw in zip(shas, objects, strict=True)
    )


def published_refs(
    repository: Repository, updates: list[tuple[bytes, str, str, str]]
) -> tuple[list[str], list[str], list[str]]:
    """Ref names and annotated tag names and messages the push publishes, and the commits its updates send.

    Objects are read by the local id Git supplied. The name inside a tag object
    (its tag header) is published with it and can differ from the ref it is
    pushed to, so every tag object on the way from a sent id to its target,
    nested tags included, has its name and message scanned. Only commits are
    ever excluded as already public; these texts are always scanned.
    """
    texts: list[str] = []
    names: list[str] = []
    tips: list[str] = []
    seen_tags: set[str] = set()
    for number, (local_ref, current, remote_ref, _) in enumerate(updates, start=1):
        if local_ref == DELETE:
            continue  # A deletion publishes no text.
        kind, short = _short_ref(remote_ref)
        texts.append(short)
        names.append(f"{kind}[{number}].name")
        while (kind_of := repository.text("cat-file", "-t", current).strip()) == "tag":
            fields, message = split_object(repository.raw("cat-file", "tag", current), f"tag[{current[:12]}]")
            if current not in seen_tags:
                seen_tags.add(current)
                texts.append(tag_name(fields))
                names.append(f"tag[{current[:12]}].tagname")
                texts.append(message)
                names.append(f"tag[{current[:12]}].message")
            targets = [value.decode("ascii", "replace") for key, value in fields if key == b"object"]
            if len(targets) != 1 or not COMMIT_ID.fullmatch(targets[0]):
                raise gate.PublishBlocked("OPSEC: pushed tag object malformed; push refused.")
            current = targets[0]
        if kind_of == "commit":
            tips.append(current)
    return texts, names, tips


def public_history(repository: Repository, client, object_format: str) -> tuple[str | None, str]:
    """(head, state): the canonical public default-branch head, and whether it may exclude history.

    Only "present" excludes: the head's commit is here and every commit
    reachable from it hashes to its id (history_verified). Object ids are
    content addressed and a commit id covers its parents' ids, so that walk is
    the public history, and no local ref, URL rewrite, altered object or
    alternate object store can put a commit inside it without a hash collision
    (replacement objects, grafts and commit-graph files are off). A shallow
    boundary only removes parents, so it can only make the excluded history
    smaller and the scan set larger (see scan_push). No answer ("unavailable"),
    a head that is not here ("absent here"; never fetched, as GIT_NO_LAZY_FETCH
    holds) or history whose bytes do not match their ids ("unverified here")
    excludes nothing.
    """
    head = client.default_head() if client is not None else None
    if head is None:
        return None, "unavailable"
    # cat-file -t reads the stored type without rehashing, so altered bytes report unverified, not absent.
    if repository.run("cat-file", "-t", head).stdout.strip() != b"commit":
        return head, "absent here"
    return head, "present" if history_verified(repository, head, object_format) else "unverified here"


def isolate_scanner(repository: Repository) -> None:
    """Keep Git's repository variables away from the scanner's own lookups.

    The hook runs with the pushed repository's GIT_DIR and related variables
    (a submodule push sets them). The scan's git calls keep them through
    Repository; the catalogue, matcher and override log belong to the
    scanner's checkout, so this process drops every variable git rev-parse
    --local-env-vars names.
    """
    for name in repository.text("rev-parse", "--local-env-vars").split():
        os.environ.pop(name, None)


def scan_push(
    arguments: list[str],
    data: bytes,
    environment: dict[str, str],
    *,
    public_repository=None,
    transport: Transport | None = None,
) -> None:
    """Raise PublishBlocked unless every public text of this push is clean, already public or overridden.

    arguments are the hook's (remote name, URL) and data its whole input.
    public_repository reports the canonical public default-branch head; by
    default it is selected by from_catalog only when the push sends commits.
    transport, for a caller that scans before its own push, is the Git every scan query runs
    with; by default that is the hook's own environment.
    """
    if len(arguments) != 2:
        raise gate.PublishBlocked("OPSEC: pre-push hook arguments malformed; push refused.")
    remote, url = arguments
    if "GIT_SHALLOW_FILE" in (environment if transport is None else transport.environment):
        # git walks the file this names, while git rev-parse --git-path still names the repository's own.
        raise gate.PublishBlocked("OPSEC: GIT_SHALLOW_FILE is not supported for a scanned push; push refused.")
    if transport is None:
        repository = Repository(real_git(environment), environment)
    else:
        repository = Repository(transport.git, transport.environment, options=transport.options)
    object_format = repository.object_format()
    updates = parse_updates(data, ID_LENGTH[object_format])
    isolate_scanner(repository)
    reason = environment.get(gate.OVERRIDE, "")
    dest = destination(url)
    if gate.is_private(dest):
        if trusted_route(repository, remote, url):
            return  # Nothing public is sent, so the override is neither used nor claimed.
        dest += "#untrusted-route"  # Never private: scanned as public.
    texts, names, tips = published_refs(repository, updates)
    owners: list[str] = []  # The commit each commit text belongs to, from texts[first] on.
    state = "unavailable"
    if tips:
        grafted, boundaries = repository.layout(object_format)
        if grafted:
            raise gate.PublishBlocked("OPSEC: grafted history present; push refused.")
        public_lookup = public_repository
        if public_lookup is None:
            public_lookup = CanonicalPublicRepository(dest, dict(os.environ))
            public_repository = public_lookup.from_catalog()
        head, state = public_history(repository, public_repository, object_format)
        exclusion = f"^{head}\n" if state == "present" else ""
        stdin = "".join(f"{sha}\n" for sha in tips) + exclusion
        pending = repository.text("rev-list", "--stdin", stdin=stdin).split()
        # git walks a shallow boundary commit as parentless. That truncates the
        # excluded history as much as the scanned one, so the excluded set is
        # never larger than the true public history, and a forged entry cannot
        # shrink the scan set. A commit the push sends but this walk misses
        # therefore sits behind a boundary that is itself in the scan set: on a
        # path from a sent tip to that commit, the first boundary is reached
        # and is not public, or the hidden commit, its ancestor, would be public
        # too. A boundary outside the scan set hides nothing the push sends.
        inside = sorted(boundaries.intersection(pending))
        if inside:
            positions = ",".join(f"commit[{sha[:12]}]" for sha in inside)
            raise gate.PublishBlocked(
                f"OPSEC: shallow boundary inside the scanned range ({positions}): history behind it "
                "cannot be scanned; deepen the clone (git fetch --deepen=<n>) or unshallow it "
                "(git fetch --unshallow); push refused."
            )
        for sha, parts in commit_texts(repository, pending):
            for part, text in parts:
                texts.append(text)
                names.append(f"commit[{sha[:12]}].{part}")
                owners.append(sha)
        print(
            f"OPSEC: push scan: {len(pending)} commit(s) scanned; public default-branch head {state} "
            f"({getattr(public_lookup, 'calls', 0)} public repository call(s)).",
            file=sys.stderr,
        )
    first = len(texts) - len(owners)
    try:
        gate.check_texts(dest, texts, environment={}, field_names=names)
    except gate.PublishBlocked as error:
        if reason.strip() and error.indices:
            # Only a flagged push offers the override: the blocked texts are
            # checked again with the reason, and the private override module
            # decides whether it admits them and audits it before anything is
            # sent. A clean push never reaches this, so no override failure can
            # refuse it.
            flagged = sorted(error.indices)
            gate.check_texts(
                dest,
                [texts[index] for index in flagged],
                environment={gate.OVERRIDE: reason},
                field_names=[names[index] for index in flagged],
            )
            return
        older = {owners[index - first] for index in error.indices if index >= first} - set(tips)
        if state == "absent here" and older:
            raise gate.PublishBlocked(
                f"{error} Hits are in history older than the pushed tips and the public default-branch "
                "head is not in this repository: fetching the public default branch lets the scan skip "
                "history that is already public."
            ) from None
        raise


def exec_caller_hook(arguments: list[str], data: bytes, environment: dict[str, str]) -> int:
    """Become the caller's own pre-push hook, as Git runs it without the pinned hooks path.

    The hook reads the same input from an unlinked temporary file, and its
    exit status goes to Git unchanged; like Git, nothing bounds its run time.
    Returns only when the hook cannot be started.
    """
    chain = str(HOOKS / "chain")
    try:
        with tempfile.TemporaryFile() as records:
            records.write(data)
            records.seek(0)
            os.dup2(records.fileno(), 0)
        os.execve(chain, [chain, "pre-push", *arguments], environment)
    except OSError:
        print("OPSEC: the caller's pre-push hook could not be started; push refused.", file=sys.stderr)
    return 1


def main(
    argv: list[str] | None = None,
    *,
    stdin: bytes | None = None,
    public_repository=None,
    chain=None,
    transport: str | None = None,
) -> int:
    """The pre-push hook: scan, then run the caller's hook; any failure refuses the push (non-zero).

    transport is a Transport as JSON, from a caller that scans before it runs its own push.
    """
    arguments = sys.argv[1:] if argv is None else argv
    environment = dict(os.environ)
    data = b""
    try:
        with deadline(scan_timeout(environment)):
            data = sys.stdin.buffer.read() if stdin is None else stdin
            parsed = None if transport is None else Transport.from_json(transport)
            scan_push(arguments, data, environment, public_repository=public_repository, transport=parsed)
    except ScanTimeout:
        print("OPSEC: push scan timed out; push refused.", file=sys.stderr)
        return 1
    except gate.PublishBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("OPSEC: push scan unavailable; push refused.", file=sys.stderr)
        return 1
    return (chain or exec_caller_hook)(arguments, data, gate.internal_environment(environment))


if __name__ == "__main__":
    raise SystemExit(main())
