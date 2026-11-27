"""Release/product composition and descriptor-bound deployment.

The descriptor has exactly three fields: providers (names), sources (a mapping
from shared/provider names to {path, required}), and preserve (relative target
subtrees). Product enrollment, including legacy migration, is separate work.
The Python >=3.12 runtime floor guarantees importlib.resources availability.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import importlib.resources as resources  # nosemgrep: python.lang.compatibility.python37-compatibility-importlib2
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from lu_machinery.agent_runtime.agy_hook_config import (
    NATIVE_HOOK_DECLARATION,
    AgyHookConfigError,
    hook_mode_for_source,
    native_hook_command,
)
from lu_machinery.common import json_depth
from lu_machinery.common.repo_root import state_root
from lu_machinery.common.safe_open import UnsafeEntryError, safe_open_below
from lu_machinery.host_context import HostContextError, hook_interpreter
from lu_machinery.lint.lint_agent_skills import _parse_frontmatter, metadata_errors

DESCRIPTOR = "agents_extensions/deployment.v1.json"
STATE = ".deploy-state/agent-deploy.v1.json"
STAGING = ".deploy-state/agent-deploy.tmp"
WORKSPACE_RUNTIME = ".claude/epics-epic"
POLICY_DIR_MODE = 0o555
TARGETS = {"claude": ".claude", "codex": ".codex", "agy": ".agent", "cursor": ".cursor"}
# Output roots of retired targets. A descriptor can no longer enable them, but
# deployment state from an earlier release may still own files there; those
# roots stay owned so the next apply retires the files and no product source
# may overlap them. The Gemini CLI target retired with product 747d981354e1.
RETIRED_ROOTS = frozenset({".gemini"})
OWNED_ROOTS = frozenset({*TARGETS.values(), *RETIRED_ROOTS})

# Discovery/configuration names remain inventoried even without a planned output.
# Runtime siblings are outside ownership; explicit descriptor preservation still applies.
# Provider references: https://code.claude.com/docs/en/{claude-directory,settings,skills,sub-agents,output-styles,plugins,workflows}
# https://learn.chatgpt.com/docs/{config-file/config-basic,build-skills,hooks}
# https://antigravity.google/docs/{customization-discovery,rules,skills,ide-workflows}
# https://cursor.com/docs/{context/rules,cli/reference/configuration,configuration/worktrees,cloud-agent}
# https://cursor.com/docs/bugbot
# https://cursor.com/docs/reference/{sandbox,permissions}
DISCOVERY = {
    ".claude": {"agents", "agent-memory", "agent-memory-local", "commands", "output-styles", "plugins", "skills", "rules", "hooks", "workflows", "settings.json", "settings.local.json", "CLAUDE.md", "AGENTS.md"},
    ".codex": {"agents", "prompts", "plugins", "skills", "rules", "hooks", "config.toml", "hooks.json"},
    ".agent": {"agents", "plugins", "skills", "rules", "workflows", "hooks", "hooks.json", "mcp_config.json", "settings.json", "rules.json", "skills.json", "agents.json", "plugins.json", "AGENTS.md", "GEMINI.md"},
    ".agents": {"agents", "plugins", "skills", "rules", "workflows", "hooks", "hooks.json", "mcp_config.json", "rules.json", "skills.json", "agents.json", "plugins.json", "AGENTS.md", "GEMINI.md"},
    ".cursor": {"agents", "commands", "skills", "rules", "hooks", "hooks.json", "mcp.json", "cli.json", "environment.json", "worktrees.json", "BUGBOT.md", "sandbox.json", "permissions.json", "config"},
}


# Providers that discover workspace content in the shared .agents tree, so its
# discovery names stay inventoried whenever one is enabled: Codex and AGY (above)
# and Cursor (.agents/skills, https://cursor.com/docs/context/skills). The retired
# Gemini CLI target (.gemini) is no longer deployed; Gemini runs through AGY.
SHARED_DISCOVERY = frozenset({"codex", "agy", "cursor"})

# Workspace .claude/settings.json policy allowlists (FU-WS-SETTINGS-GUARD).
# A non-Git CTO workspace runs with session-writable files (safe_open requires geteuid),
# so workspace settings admit only the release's required hooks, benign runtime keys, and
# numeric/boolean Claude runtime env knobs — never command-executing helpers (apiKeyHelper,
# statusLine, awsAuthRefresh, awsCredentialExport, otelHeadersHelper, fileSuggestion),
# permission/MCP overrides, extra hooks, or guard/route/interpreter env overrides.
WORKSPACE_CLAUDE_SETTINGS_KEYS = frozenset({
    "hooks",
    "env",
    "disableAllHooks",
    "allowManagedHooksOnly",
    "language",
    "model",
    "advisorModel",
    "effortLevel",
    "respectGitignore",
    "acceptEdits",
    "yolo",
    "plansDirectory",
    "worktree",
})
WORKSPACE_CLAUDE_SETTINGS_ENV_KEYS = frozenset({
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "PYTHONDONTWRITEBYTECODE",
})
# Workspace source subtrees and filenames that can execute hooks/commands or register MCP outside release.v1.json.
WORKSPACE_FORBIDDEN_SOURCE_PREFIXES = ("hooks/", "plugins/", "workflows/")
WORKSPACE_FORBIDDEN_SOURCE_NAMES = frozenset({".mcp.json", "mcp_config.json", "mcp.json"})
# Markdown frontmatter subtrees where Claude Code loads inline hooks or MCP servers.
WORKSPACE_FRONTMATTER_PREFIXES = ("skills/", "agents/", "commands/", "output-styles/")
WORKSPACE_FORBIDDEN_FRONTMATTER_KEYS = frozenset({"hooks", "mcpservers"})


class Invalid(ValueError):
    """Invalid or unsafe input, exit 2."""


class Drift(ValueError):
    """Deployment conflict or failed verification, exit 1."""


def relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Invalid("expected a nonempty relative POSIX path")
    if any(unicodedata.category(c).startswith("C") or c in "\u2028\u2029" for c in value):
        raise Invalid("control/format characters in path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in value.split("/")):
        raise Invalid("absolute, traversal or noncanonical path")
    return value


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _entry_exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def decode(blob: bytes) -> Any:
    def members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Invalid("duplicate JSON member")
            result[key] = value
        return result
    try:
        return json_depth.loads(blob, object_pairs_hook=members, max_depth=512)
    except (ValueError, UnicodeError) as exc:
        raise Invalid("invalid JSON") from exc


# Python's bytecode cache directory. Importing a deployed or product script writes one beside it;
# it is never a deployment input or output, so both inventories skip it (a symlink of that name still
# refuses). Python uses a cached file only for a source beside it; a loose ``*.pyc`` outside one can be
# imported without its source, so it stays visible.
BYTECODE_CACHE = "__pycache__"


class Tree:
    """All filesystem operations remain below a held, no-follow root fd."""

    def __init__(self, fd: int, *, workspace: bool = False, staging: str = STAGING):
        self.fd = fd
        self.workspace = workspace
        self.staging = staging

    @contextlib.contextmanager
    def writable(self, fd: int) -> Iterator[None]:
        # Hold the same directory across chmod, the mutation and the relock.
        locked = self.workspace and stat.S_IMODE(os.fstat(fd).st_mode) == POLICY_DIR_MODE
        if locked:
            os.fchmod(fd, POLICY_DIR_MODE | stat.S_IWUSR)
        try:
            yield
        finally:
            if locked:
                os.fchmod(fd, POLICY_DIR_MODE)
                os.fsync(fd)

    @contextlib.contextmanager
    def directory(self, path: str, *, create: bool = False) -> Iterator[int]:
        fd = os.dup(self.fd)
        try:
            for part in relative(path).split("/"):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    with self.writable(fd), contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                    child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except NotADirectoryError as exc:
            raise Invalid("non-directory deployment path: " + path) from exc
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def parent(self, path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = relative(path).rsplit("/", 1)
        if len(parts) == 1:
            yield self.fd, parts[0]
        else:
            with self.directory(parts[0], create=create) as fd:
                yield fd, parts[1]

    def read(self, path: str) -> tuple[bytes, int] | None:
        try:
            with self.parent(path) as (parent, name):
                fd = safe_open_below(parent, name, os.O_RDONLY)
                with os.fdopen(fd, "rb") as file:
                    mode = stat.S_IMODE(os.fstat(file.fileno()).st_mode)
                    return file.read(), mode
        except FileNotFoundError:
            return None
        except UnsafeEntryError as exc:
            raise Invalid("non-regular deployment output or parent: " + path) from exc

    def has_directory(self, path: str) -> bool:
        try:
            with self.directory(path):
                return True
        except FileNotFoundError:
            return False

    def has_entry(self, path: str) -> bool:
        try:
            with self.parent(path) as (directory, name):
                return _entry_exists(directory, name)
        except FileNotFoundError:
            return False

    def inventory(self, path: str, *, exclude: Callable[[str], bool] | None = None,
                  visit_directory: Callable[[str], None] | None = None) -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        def visit(fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(fd)):
                key = prefix + name
                if exclude is not None and exclude(path + "/" + key):
                    continue
                relative(name)
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    if visit_directory is not None:
                        visit_directory(path + "/" + key)
                    elif name == BYTECODE_CACHE:
                        continue
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        visit(child, key + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    child = safe_open_below(fd, name, os.O_RDONLY)
                    with os.fdopen(child, "rb") as file:
                        found[key] = (file.read(), stat.S_IMODE(os.fstat(file.fileno()).st_mode))
                else:
                    raise Invalid("symlink or special file in deployment tree: " + path + "/" + key)
        try:
            with self.directory(path) as fd:
                visit(fd, "")
        except FileNotFoundError:
            pass
        return found

    def replace(self, path: str, blob: bytes, mode: int) -> None:
        # The per-target lock serializes all writes, so one reserved staging
        # slot suffices. SIGKILL can leave it behind without polluting a provider
        # tree. Recovery validates its bytes against the reconstructed plan.
        with self.parent(path, create=True) as (fd, name), self.parent(self.staging, create=True) as (stage_fd, temp):
            handle = safe_open_below(stage_fd, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(blob)
                    os.fchmod(file.fileno(), mode)
                    file.flush()
                    os.fsync(file.fileno())
                with self.writable(fd):
                    os.replace(temp, name, src_dir_fd=stage_fd, dst_dir_fd=fd)
                    os.fsync(fd)
                os.fsync(stage_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=stage_fd)

    def unlink(self, path: str) -> None:
        with self.parent(path) as (fd, name), self.writable(fd):
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)

    def rmdir(self, path: str) -> None:
        try:
            with self.parent(path) as (fd, name), self.writable(fd):
                os.rmdir(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise


class CommitInputs:
    """Deployment inputs read from one commit's Git objects (``--inputs-commit``), never the checkout.

    The commit's trees are walked object by object from the commit ID with ``cat-file --batch``, and every
    object read is hashed and must equal the ID it was asked for (machinery#22). Git resolves no path, so
    neither ``core.worktree``, the current-directory prefix nor any other path setting can redirect a lookup;
    replace refs are ignored; and since every object is checked against its ID, starting from the commit's,
    the result depends only on that ID: nothing written to the checkout, its refs, configuration or object
    store while a check runs changes the plan. Only regular-file entries count: a symlink, submodule or other
    entry is refused, as the checkout inventory refuses one. Outputs, provider control files and the
    ownership manifest are still read from the checkout ``Tree``.
    """

    TREE_MODE = b"40000"

    def __init__(self, tree: Tree, commit: str):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise Invalid("--inputs-commit must be a full object name")
        self.fd = tree.fd
        self.algorithm = "sha1" if len(commit) == 40 else "sha256"
        kind, body = self.objects([commit])[0]
        header = body.split(b"\n", 1)[0]
        if kind != b"commit" or not re.fullmatch(rb"tree [0-9a-f]{%d}" % len(commit), header):
            raise Invalid("--inputs-commit must name a commit")
        self.commit = commit
        self.root = header[5:].decode()

    def objects(self, oids: list[str]) -> list[tuple[bytes, bytes]]:
        """``(type, body)`` of each object, each verified to hash to the ID asked for."""
        if not oids:
            return []
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # fixed git argv on the held checkout fd, no shell; object reads by ID only
            ["git", "-C", f"/proc/self/fd/{self.fd}", "--no-replace-objects", "cat-file", "--batch"],
            pass_fds=(self.fd,), input="".join(oid + "\n" for oid in oids).encode(), capture_output=True,
            check=False, env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
        if result.returncode:
            raise Invalid("cannot read deployment inputs from the commit")
        output, found, offset = result.stdout, [], 0
        for oid in oids:
            header_end = output.find(b"\n", offset)
            header = output[offset:header_end].split(b" ") if header_end >= 0 else []
            if len(header) != 3 or header[0] != oid.encode() or not header[2].isdigit():
                raise Invalid("cannot read deployment inputs from the commit")
            kind, size = header[1], int(header[2])
            body = output[header_end + 1:header_end + 1 + size]
            if len(body) != size or hashlib.new(self.algorithm, b"%s %d\0" % (kind, size) + body).hexdigest() != oid:
                raise Invalid("cannot read deployment inputs from the commit")
            found.append((kind, body))
            offset = header_end + 1 + size + 1
        return found

    def listings(self, oids: list[str], paths: list[str]) -> list[dict[str, tuple[bytes, str]]]:
        """The ``{name: (mode, ID)}`` entries of each tree object in ``oids``, found at the matching ``paths``."""
        found = []
        for (kind, body), path in zip(self.objects(oids), paths, strict=True):
            if kind != b"tree":
                raise Invalid("non-directory deployment path: " + path)
            width, entries, offset = len(self.commit) // 2, {}, 0
            while offset < len(body):
                space, nul = body.find(b" ", offset), body.find(b"\0", offset)
                if not 0 <= space < nul or nul + 1 + width > len(body):
                    raise Invalid("cannot read deployment inputs from the commit")
                name = body[space + 1:nul].decode("utf-8", "surrogateescape")
                if "/" in name or name in entries:
                    raise Invalid("noncanonical deployment input: " + path)
                entries[name] = (body[offset:space], body[nul + 1:nul + 1 + width].hex())
                offset = nul + 1 + width
            found.append(entries)
        return found

    def lookup(self, path: str) -> tuple[bytes, str] | None:
        """``(mode, ID)`` of the entry at ``path`` in the commit's tree; ``None`` when there is none.

        A path below a non-directory entry is refused, as the checkout ``Tree`` refuses it, never read as absent.
        """
        mode, oid, walked = self.TREE_MODE, self.root, ""
        for part in relative(path).split("/"):
            if mode != self.TREE_MODE:
                raise Invalid("non-directory deployment path: " + path)
            entry = self.listings([oid], [walked or "."])[0].get(part)
            if entry is None:
                return None
            (mode, oid), walked = entry, (walked + "/" + part).lstrip("/")
        return mode, oid

    @staticmethod
    def regular(mode: bytes, path: str) -> int:
        if mode not in {b"100644", b"100755"}:
            raise Invalid("symlink or special file in deployment input: " + path)
        return 0o755 if mode == b"100755" else 0o644

    def blobs(self, oids: list[str]) -> list[bytes]:
        found = self.objects(oids)
        if any(kind != b"blob" for kind, _ in found):
            raise Invalid("cannot read deployment inputs from the commit")
        return [body for _, body in found]

    def read(self, path: str) -> tuple[bytes, int] | None:
        entry = self.lookup(path)
        if entry is None:
            return None
        permissions = self.regular(entry[0], path)
        return self.blobs([entry[1]])[0], permissions

    def has_directory(self, path: str) -> bool:
        entry = self.lookup(path)
        if entry is not None and entry[0] != self.TREE_MODE:
            raise Invalid("non-directory deployment path: " + path)
        return entry is not None

    def inventory(self, path: str) -> dict[str, tuple[bytes, int]]:
        top = self.lookup(path)
        if top is None:
            return {}
        if top[0] != self.TREE_MODE:
            raise Invalid("non-directory deployment path: " + path)
        items, level = [], [("", top[1])]
        while level:  # one object read per tree depth
            listed = self.listings([oid for _, oid in level], [path + "/" + prefix for prefix, _ in level])
            following = []
            for (prefix, _), entries in zip(level, listed, strict=True):
                for name, (mode, child) in sorted(entries.items()):
                    relative(name)  # only names the deployment reads; a lookup checks its own path
                    if mode == self.TREE_MODE and name == BYTECODE_CACHE:
                        continue
                    if mode == self.TREE_MODE:
                        following.append((prefix + name + "/", child))
                    else:
                        items.append((prefix + name, child, self.regular(mode, path + "/" + prefix + name)))
            level = following
        contents = self.blobs([oid for _, oid, _ in items])
        return {key: (blob, mode) for (key, _, mode), blob in zip(items, contents, strict=True)}


@contextlib.contextmanager
def selected_tree(selected: str | None, check: bool, *, workspace: bool = False) -> Iterator[Tree]:
    """Hold the selected target's directory and its per-target lock.

    A consumer target (the default) must be its Git checkout's root. A workspace target
    (``workspace=True``, the CTO workspace) is the opposite: a generated directory that is
    neither a checkout nor inside one, so no ancestor ``.git`` can make Claude Code load a
    checkout's project instructions, and a checkout can never be selected by accident.
    """
    if selected is None:
        raise Invalid("--project-root or LU_PRODUCT_ROOT is required")
    if selected != "/":
        relative(selected.removeprefix("/"))
    path = Path(selected)
    if not path.is_absolute():
        raise Invalid("project root must be absolute")
    if workspace and path == Path("/"):
        raise Invalid("a workspace target cannot be the filesystem root")
    relative(str(path)[1:])
    # Open each component: resolve() would silently accept symlink targets.
    base = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        if workspace and _entry_exists(base, ".git"):
            raise Invalid("a workspace target must be outside every Git checkout: / has a .git entry")
        walked = ""
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
            os.close(base)
            base = child
            walked += "/" + component
            # Checked on the held descriptors, so no path swap can hide a checkout.
            if workspace and _entry_exists(base, ".git"):
                raise Invalid(f"a workspace target must be outside every Git checkout: {walked} has a .git entry")
        tree = Tree(base, workspace=workspace)
        if not workspace:
            try:
                result = subprocess.run(
                    ["git", "-C", f"/proc/self/fd/{base}", "rev-parse", "--show-toplevel"],
                    pass_fds=(base,), capture_output=True, text=True, check=False, timeout=30,
                    env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                )
            except subprocess.TimeoutExpired as exc:
                raise Invalid("project root check timed out") from exc
            if result.returncode or result.stdout.strip() != str(path):
                raise Invalid("project root must be the Git checkout root")
        # flock on the checkout inode needs no lockfile and writes nothing in check mode.
        fcntl.flock(base, (fcntl.LOCK_SH if check else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        yield tree
    except BlockingIOError as exc:
        raise Drift("another deployment holds this target's lock") from exc
    finally:
        os.close(base)


@dataclass(frozen=True)
class Output:
    blob: bytes
    mode: int
    owner: str
    source: str

    def record(self) -> dict[str, object]:
        return {"type": "file", "sha256": digest(self.blob), "mode": self.mode,
                "owner": self.owner, "source": self.source}


@dataclass
class Plan:
    files: dict[str, Output]
    preserve: list[str]
    roots: set[str]
    release: str
    configuration: str
    input_digests: dict[str, str] = field(default_factory=dict)

    def directories(self) -> set[str]:
        return {str(parent) for path in self.files for parent in PurePosixPath(path).parents if str(parent) != "."}

    def manifest(self) -> bytes:
        return encoded({"version": 1, "release": self.release, "configuration": self.configuration,
                        "files": {p: out.record() for p, out in sorted(self.files.items())},
                        "directories": sorted(self.directories())})


def agy_parts(core: bytes) -> dict[str, bytes]:
    text = core.decode().rstrip() + "\n"
    bodies: list[str] = []
    current = ""
    # Reserve the same two-digit header budget as the previous renderer.
    header = "---\ntrigger: always_on\ndescription: Binding rules core, part 99 of 99.\n---\n"
    limit = 24000 - len(header.encode())
    for line in text.splitlines(keepends=True):
        if len(line.encode()) > limit:
            raise Invalid("AGY core line exceeds rule limit")
        if len((current + line).encode()) > limit:
            bodies.append(current)
            current = ""
        current += line
    if current:
        bodies.append(current)
    if not bodies or len(bodies) > 99:
        raise Invalid("AGY core cannot be split")
    return {f"rules/core-part-{i:02d}.md":
            (f"---\ntrigger: always_on\ndescription: Binding rules core, part {i} of {len(bodies)}.\n---\n" + body).encode()
            for i, body in enumerate(bodies, 1)}


def release_inputs() -> tuple[dict[str, Output], dict[str, Any], str]:
    raw = resources.files("lu_machinery.agent_deploy").joinpath("release.v1.json").read_bytes()
    inventory = decode(raw)
    # Anchor the sibling data tree to this package, never to the mergeable
    # agents_extensions namespace. No cwd, PYTHONPATH or checkout lookup.
    root = resources.files("lu_machinery").joinpath("..", "agents_extensions", "shared")
    result = {}
    for path, record in inventory["files"].items():
        relative(path)
        blob = root.joinpath(path).read_bytes()
        if record["owner"] != "fleet" or record["mode"] not in {0o644, 0o755}:
            raise Invalid("invalid release ownership or mode")
        result[path] = Output(blob, record["mode"], "fleet", "release:" + path)
    if "rules/core.md" not in result:
        raise Invalid("release inventory has no core")
    # Bind release identity to both routing metadata and the packaged bytes.
    # Consumer ownership records retain hashes for drift detection.
    identity = digest(encoded({"inventory": digest(raw),
                               "files": {path: out.record() for path, out in result.items()}}))
    return result, inventory, identity


def _validate_workspace_frontmatter(path: str, blob: bytes) -> None:
    """Refuse inline ``hooks`` or ``mcpServers`` frontmatter in workspace skills/agents/commands."""
    try:
        text = blob.decode("utf-8").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise Invalid("non-UTF-8 workspace markdown: " + path) from exc
    opener = re.match(r"^---[ \t]*\n", text)
    if opener is None:
        return
    closer = re.search(r"\n---[ \t]*(?:\n|$)", text[opener.end():])
    if closer is None:
        raise Invalid("unterminated workspace frontmatter: " + path)
    try:
        data = yaml.safe_load(text[opener.end():opener.end() + closer.start()])
    except yaml.YAMLError as exc:
        raise Invalid("invalid workspace frontmatter: " + path) from exc
    if not isinstance(data, dict):
        raise Invalid("workspace frontmatter must be a mapping: " + path)
    for key in data:
        if str(key).casefold() in WORKSPACE_FORBIDDEN_FRONTMATTER_KEYS:
            raise Invalid(f"workspace frontmatter cannot declare {key}: {path}")


def _native_hook_entries(settings: Any) -> Iterator[tuple[dict[str, Any], str]]:
    """Find the exact managed commands in provider hook settings."""
    if isinstance(settings, list):
        for entry in settings:
            yield from _native_hook_entries(entry)
    elif isinstance(settings, dict):
        command = settings.get("command")
        if settings.get("type") == "command" and isinstance(command, str):
            mode = hook_mode_for_source(command)
            if mode is not None:
                yield settings, mode
        for value in settings.values():
            yield from _native_hook_entries(value)


def _validate_workspace_claude_settings(settings: dict[str, Any], requirements: list[dict[str, Any]]) -> None:
    """Enforce the strict release-bound policy on a non-Git workspace's ``.claude/settings.json``."""
    unknown = sorted(set(settings) - WORKSPACE_CLAUDE_SETTINGS_KEYS)
    if unknown:
        raise Invalid("unapproved workspace settings key: " + ", ".join(unknown))
    if "env" in settings:
        env_block = settings["env"]
        if not isinstance(env_block, dict):
            raise Invalid("invalid workspace settings env: .claude/settings.json")
        for key, value in env_block.items():
            if key not in WORKSPACE_CLAUDE_SETTINGS_ENV_KEYS:
                raise Invalid("unapproved workspace settings env: " + str(key))
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
                raise Invalid("invalid workspace settings env value: " + key)
    hooks_section = settings.get("hooks")
    if not isinstance(hooks_section, dict):
        raise Invalid("invalid workspace hook settings: .claude/settings.json")
    allowed_by_event: dict[str, list[dict[str, Any]]] = {}
    for requirement in requirements:
        path = requirement.get("path")
        if not isinstance(path, list) or len(path) != 2 or path[0] != "hooks" or not isinstance(path[1], str):
            raise Invalid("invalid release required_hooks path")
        allowed_by_event.setdefault(path[1], []).append(requirement)
    for event, groups in hooks_section.items():
        if event not in allowed_by_event:
            raise Invalid("unapproved workspace hook event: " + str(event))
        if not isinstance(groups, list):
            raise Invalid("invalid workspace hook settings: " + event)
        for group in groups:
            if not isinstance(group, dict):
                raise Invalid("invalid workspace hook group: " + event)
            if group.get("enabled", True) is not True or group.get("disabled", False) is not False:
                raise Invalid("disabled workspace hook group: " + event)
            group_hooks = group.get("hooks")
            if not isinstance(group_hooks, list):
                raise Invalid("invalid workspace hook list: " + event)
            group_meta = {k: v for k, v in group.items() if k != "hooks"}
            matching = [req for req in allowed_by_event[event] if req["group"] == group_meta]
            if not matching:
                raise Invalid("unapproved workspace hook group: " + event)
            allowed_hooks = [
                {k: v for k, v in hook.items() if k != "statusMessage"}
                for req in matching
                for hook in req["hooks"]
            ]
            for candidate in group_hooks:
                if not isinstance(candidate, dict):
                    raise Invalid("invalid workspace hook entry: " + event)
                if "statusMessage" in candidate and not isinstance(candidate["statusMessage"], str):
                    raise Invalid("invalid workspace hook statusMessage: " + event)
                normalized = {k: v for k, v in candidate.items() if k != "statusMessage"}
                if normalized not in allowed_hooks:
                    raise Invalid("unapproved workspace hook command: " + event)


def build(tree: Tree, inputs: Tree | CommitInputs | None = None, *, workspace: bool = False) -> Plan:
    """The plan for ``tree``, composed from ``inputs`` (the descriptor and product sources); default: ``tree``."""
    inputs = tree if inputs is None else inputs
    entry = inputs.read(DESCRIPTOR)
    if entry is None:
        raise Invalid("missing " + DESCRIPTOR)
    input_digests = {DESCRIPTOR: digest(entry[0])} if workspace else {}
    descriptor = decode(entry[0])
    if not isinstance(descriptor, dict) or set(descriptor) != {"providers", "sources", "preserve"}:
        raise Invalid("descriptor requires only providers, sources and preserve")
    providers, sources, preserve = (descriptor[key] for key in ("providers", "sources", "preserve"))
    if not isinstance(providers, list) or not providers or any(p not in TARGETS for p in providers) or len(set(providers)) != len(providers):
        raise Invalid("invalid enabled providers")
    if workspace and "claude" not in providers:
        raise Invalid("a workspace target must enable the claude provider")
    if not isinstance(sources, dict) or set(sources) - {"shared", *providers}:
        raise Invalid("invalid source groups")
    roots = {TARGETS[p] for p in providers}
    if SHARED_DISCOVERY.intersection(providers):
        roots.add(".agents")
    if not isinstance(preserve, list):
        raise Invalid("preserve must be a list")
    if workspace and WORKSPACE_RUNTIME not in preserve:
        preserve = [*preserve, WORKSPACE_RUNTIME]
    for path in preserve:
        relative(path)
        if path.split("/")[0] not in roots or "/" not in path:
            raise Invalid("preserve must name a subtree within an enabled target")
        if path == ".codex/skills" or path.startswith(".codex/skills/"):
            raise Invalid("legacy Codex discovery cannot be preserved")
        if workspace and path != ".claude/settings.local.json" and path.split("/")[1].casefold() in {
            name.casefold() for name in DISCOVERY.get(path.split("/")[0], ())
        }:
            raise Invalid("workspace cannot preserve provider discovery path: " + path)
    shared, inventory, release = release_inputs()
    def replaces(provider: str, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in inventory["provider_replacements"].get(provider, []))
    skill_names: set[str] = set()
    for path, out in shared.items():
        if path.startswith("skills/") and path.endswith("/SKILL.md"):
            data, error = _parse_frontmatter(out.blob.decode())
            declared_release_name = data.get("name") if data is not None else None
            if error or not isinstance(declared_release_name, str):
                raise Invalid("invalid release skill metadata: " + path)
            skill_names.add(declared_release_name)
    groups: dict[str, dict[str, Output]] = {"shared": shared}
    used_sources: list[str] = []
    for group, spec in sources.items():
        if not isinstance(spec, dict) or set(spec) != {"path", "required"} or type(spec["required"]) is not bool:
            raise Invalid("source requires path and boolean required")
        source = relative(spec["path"])
        if source.split("/")[0] in {*OWNED_ROOTS, ".agents", ".deploy-state", ".git"}:
            raise Invalid("product source overlaps deployment output or Git state")
        if any(source == p or source.startswith(p + "/") or p.startswith(source + "/") for p in used_sources):
            raise Invalid("overlapping product sources")
        used_sources.append(source)
        if not inputs.has_directory(source):
            if spec["required"]:
                raise Invalid("missing required source: " + source)
            continue
        items = inputs.inventory(source)
        skills = {p.split("/")[1] for p in items if p.startswith("skills/") and len(p.split("/")) > 2}
        for name in skills:
            skill = items.get(f"skills/{name}/SKILL.md")
            if skill is None:
                raise Invalid("missing SKILL.md: " + name)
            metadata, _error = _parse_frontmatter(skill[0].decode())
            declared_name = metadata.get("name") if metadata is not None else None
            if isinstance(declared_name, str) and declared_name in skill_names:
                raise Invalid("duplicate skill name: " + declared_name)
            errors = metadata_errors(skill[0].decode(), name)
            if errors:
                raise Invalid("skill lint: " + "; ".join(errors))
            skill_names.add(name)
        # Product lint selection is code-side. Generic consumers never run curriculum checks.
        if inputs.read("curriculum/l2-uk-en/curriculum.yaml") is not None:
            from lu_machinery.lint.lint_prompts import RULES, rule_matches_path
            for path, (blob, _mode) in items.items():
                if not path.endswith(".md") or not (path.startswith("skills/") or path.startswith("phases/")):
                    continue
                for rule_id, severity, _description, file_glob, pattern, exception in RULES:
                    if severity != "error" or not rule_matches_path(path, file_glob):
                        continue
                    if any(pattern.search(line) and not (exception and exception.search(line)) for line in blob.decode().splitlines()):
                        raise Invalid("prompt lint: " + rule_id)
        destination = groups.setdefault(group, {})
        for path, (blob, mode) in items.items():
            if path in destination or (path in shared and
                    (shared[path].owner == "fleet" or not replaces(group, path))):
                raise Invalid("duplicate fleet/product logical path: " + path)
            if workspace:
                full_source = source + "/" + path
                input_digests[full_source] = digest(blob)
                logical_path = path.casefold()
                if any(logical_path == prefix.rstrip("/") or logical_path.startswith(prefix)
                       for prefix in WORKSPACE_FORBIDDEN_SOURCE_PREFIXES):
                    raise Invalid("workspace source cannot supply a hook, plugin or workflow: " + full_source)
                if PurePosixPath(path).name.casefold() in WORKSPACE_FORBIDDEN_SOURCE_NAMES:
                    raise Invalid("workspace source cannot supply control file: " + full_source)
                if logical_path.endswith(".md") and logical_path.startswith(WORKSPACE_FRONTMATTER_PREFIXES):
                    _validate_workspace_frontmatter(full_source, blob)
            if path.startswith("hooks/") and not mode & 0o111:
                raise Invalid("product hook is not executable: " + path)
            destination[path] = Output(blob, 0o755 if mode & 0o111 else 0o644, "product", source + "/" + path)
    outputs: dict[str, Output] = {}
    def add(target: str, path: str, out: Output) -> None:
        full = target + "/" + relative(path)
        if full == ".codex/skills" or full.startswith(".codex/skills/"):
            raise Invalid("planned legacy Codex discovery path")
        if full in outputs:
            raise Invalid("duplicate output: " + full)
        if any(full == p or full.startswith(p + "/") or p.startswith(full + "/") for p in preserve):
            raise Invalid("preserved runtime path overlaps an output")
        outputs[full] = out
    for provider in providers:
        target = TARGETS[provider]
        # A declared replacement permits an overlay; it must not erase shared
        # defaults when that consumer supplies no replacement source (#552).
        replacements = [prefix for prefix in inventory["provider_replacements"].get(provider, [])
                        if any(name == prefix or name.startswith(prefix + "/")
                               for name in groups.get(provider, {}))]
        for path, out in groups["shared"].items():
            if out.owner == "product" and any(path == prefix or path.startswith(prefix + "/")
                                              for prefix in replacements):
                continue
            if out.owner == "fleet":
                destinations = inventory["files"][path]["targets"].get(provider, [])
            elif provider == "claude" and path in inventory["claude_autoload_excludes"]:
                destinations = []
            elif provider == "codex" and path.startswith("skills/"):
                destinations = [".agents/" + path]
            else:
                destinations = [target + "/" + path]
                if provider == "agy" and path.startswith("skills/"):
                    destinations.append(".agents/" + path)
            for full in destinations:
                root, relative_path = full.split("/", 1)
                # Codex and AGY intentionally share one discovery tree.
                if full in outputs and outputs[full] == out:
                    continue
                add(root, relative_path, out)
        for path, out in groups.get(provider, {}).items():
            output_root = ".agents" if provider == "agy" or (provider == "codex" and path.startswith("skills/")) else target
            add(output_root, path, out)
    if "agy" in providers:
        for path, blob in agy_parts(shared["rules/core.md"].blob).items():
            add(".agent", path, Output(blob, 0o644, "fleet", "generated:core"))
        add(".agent", "rules.json", Output(encoded({"exclude": ["core.md", "model-assignment.md", "workflow.md"]}), 0o644, "fleet", "generated:agy-excludes"))
    hook_python = None
    native_configs = {"claude": ".claude/settings.json", "agy": ".agent/hooks.json", "codex": ".codex/hooks.json"}
    for provider in providers:
        required_configs = inventory["required_hooks"].get(provider, {})
        hook_configs = dict(required_configs)
        native_config = native_configs.get(provider)
        if native_config is not None and native_config in outputs:
            hook_configs.setdefault(native_config, [])
        for config, requirements in hook_configs.items():
            output = outputs.get(config)
            if output is None:
                raise Invalid("missing provider settings: " + config)
            try:
                settings = decode(output.blob)
                entries = list(_native_hook_entries(settings))
                for hook_entry, hook_mode in entries:
                    hook_entry["command"] = f"{NATIVE_HOOK_DECLARATION} {hook_mode}"
                if workspace and config == ".claude/settings.json":
                    if not isinstance(settings, dict):
                        raise Invalid("expected a settings object")
                    _validate_workspace_claude_settings(settings, requirements)
            except (Invalid, AgyHookConfigError) as exc:
                raise Invalid(f"{config}: {exc}") from exc
            for requirement in requirements:
                groups_for_event = settings
                for component in requirement["path"]:
                    if not isinstance(groups_for_event, dict):
                        raise Invalid("invalid provider hook settings")
                    groups_for_event = groups_for_event.get(component)
                if not isinstance(groups_for_event, list):
                    raise Invalid("missing mandatory hook event")
                for hook in requirement["hooks"]:
                    if not any(isinstance(group, dict)
                               and {k: v for k, v in group.items() if k != "hooks"} == requirement["group"]
                               and group.get("enabled", True) is True and group.get("disabled", False) is False
                               and isinstance(group.get("hooks"), list)
                               and any(isinstance(candidate, dict)
                                       and {k: v for k, v in candidate.items() if k != "statusMessage"}
                                       == {k: v for k, v in hook.items() if k != "statusMessage"}
                                       for candidate in group.get("hooks", [])) for group in groups_for_event):
                        raise Invalid("missing mandatory hook delegator: " + config)

            if entries:
                try:
                    if hook_python is None:
                        hook_python = hook_interpreter()
                    for hook_entry, hook_mode in entries:
                        hook_entry["command"] = native_hook_command(hook_mode, python=hook_python)
                except (AgyHookConfigError, HostContextError) as exc:
                    raise Invalid(f"{config}: {exc}") from exc
                outputs[config] = replace(output, blob=encoded(settings))

    # Local project settings can override mandatory hooks even when the file
    # is preserved. Only recognized control files are read; runtime scratch
    # remains excluded from the target inventory below.
    if "claude" in providers:
        local = ".claude/settings.local.json"
        if workspace and (any(path == local or path.startswith(local + "/") for path in outputs)
                          or tree.has_entry(local)):
            raise Invalid("a workspace target must not hold .claude/settings.local.json")
        if workspace:
            for path in (".mcp.json", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md"):
                if tree.has_entry(path):
                    raise Invalid("a workspace target must not hold " + path)
        configs = (".claude/settings.json",) if workspace else (".claude/settings.json", local)
        for config in configs:
            planned = outputs.get(config)
            existing = tree.read(config) if planned is None else None
            control_blob = planned.blob if planned is not None else existing[0] if existing is not None else None
            if control_blob is not None:
                settings = decode(control_blob)
                if (
                    not isinstance(settings, dict)
                    or settings.get("disableAllHooks", False) is not False
                    or settings.get("allowManagedHooksOnly", False) is not False
                ):
                    raise Invalid("mandatory Claude hooks disabled: " + config)
    if "codex" in providers:
        planned = outputs.get(".codex/config.toml")
        existing = tree.read(".codex/config.toml") if planned is None else None
        control_blob = planned.blob if planned is not None else existing[0] if existing is not None else None
        if control_blob is not None:
            features = tomllib.loads(control_blob.decode()).get("features", {})
            if not isinstance(features, dict) or any(key in features and features[key] is not True for key in ("hooks", "codex_hooks")):
                raise Invalid("mandatory Codex hooks disabled: .codex/config.toml")
    for path in outputs:
        if any(str(parent) in outputs for parent in PurePosixPath(path).parents):
            raise Invalid("file/directory collision in planned outputs")
    configuration = digest(encoded({
        "descriptor": descriptor,
        "sources": {g: {p: o.record() for p, o in items.items() if o.owner == "product"} for g, items in groups.items()},
    }))
    if workspace:
        outputs = {p: Output(o.blob, o.mode & ~0o222, o.owner, o.source) for p, o in outputs.items()}
    return Plan(outputs, preserve, roots, release, configuration, input_digests)


def preserved(path: str, plan: Plan) -> bool:
    return any(path == p or path.startswith(p + "/") for p in plan.preserve)


def old_state(tree: Tree) -> dict[str, Any]:
    read = tree.read(STATE)
    if read is None:
        return {"files": {}, "directories": []}
    return validate_ownership(decode(read[0]), modes={0o644, 0o755})


def validate_ownership(data: Any, *, modes: set[int]) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"version", "release", "configuration", "files", "directories"} or data["version"] != 1:
        raise Invalid("invalid ownership manifest")
    if not isinstance(data["files"], dict) or not isinstance(data["directories"], list):
        raise Invalid("invalid ownership records")
    for path, record in data["files"].items():
        relative(path)
        if path.split("/")[0] not in {*OWNED_ROOTS, ".agents"} or "/" not in path:
            raise Invalid("manifest output outside provider directories")
        if not isinstance(record, dict) or set(record) != {"type", "sha256", "mode", "owner", "source"}:
            raise Invalid("invalid file record")
        if record["type"] != "file" or record["owner"] not in {"fleet", "product"} or type(record["mode"]) is not int or record["mode"] not in modes:
            raise Invalid("invalid file ownership/mode")
        if not isinstance(record["sha256"], str) or len(record["sha256"]) != 64:
            raise Invalid("invalid file hash")
    ancestors = {str(p) for name in data["files"] for p in PurePosixPath(name).parents if str(p) != "."}
    if any(relative(p) not in ancestors for p in data["directories"]):
        raise Invalid("unowned directory in manifest")
    return data


def matches(actual: tuple[bytes, int] | None, record: dict[str, Any]) -> bool:
    return actual is not None and digest(actual[0]) == record["sha256"] and actual[1] == record["mode"]


def preflight(tree: Tree, plan: Plan, old: dict[str, Any], check: bool, *, workspace: bool = False) -> list[str]:
    problems: list[str] = []
    actual: tuple[bytes, int] | None
    staged = None if workspace and check else tree.read(STAGING)
    if staged is not None:
        # A killed buffered write may leave an empty or partial prefix, with
        # either its initial private mode or its final mode. Unrelated bytes,
        # special files and symlinks are never adopted or removed.
        candidates = [(out.blob, out.mode) for out in plan.files.values()]
        candidates.append((plan.manifest(), 0o644))
        if not any(blob.startswith(staged[0]) and staged[1] in {0o600, mode} for blob, mode in candidates):
            raise Invalid("unrecognized deployment staging content")
        if check:
            problems.append("interrupted deployment staging remains")
    roots = plan.roots | {path.split("/")[0] for path in old["files"]}
    owned = {(path.split("/")[0], path.split("/")[1].casefold()) for path in (*plan.files, *old["files"])}
    owned.update((root, name.casefold()) for root in roots for name in DISCOVERY.get(root, ()))
    def excluded(path: str) -> bool:
        root, name = path.split("/")[:2]
        return preserved(path, plan) or (root, name.casefold()) not in owned
    directories = plan.directories()
    known_directories = directories | set(old.get("directories", ()))

    def inspect_directory(path: str) -> None:
        if path not in known_directories:
            problems.append("unknown discovery directory: " + path)

    for root in sorted(roots):
        # Exclude runtime entries before stat/open/recursion. Non-preserved
        # links and special files still fail closed inside inventory().
        for path, actual in tree.inventory(root, exclude=excluded,
                                           visit_directory=inspect_directory if workspace else None).items():
            full = root + "/" + path
            expected = plan.files.get(full)
            if expected is not None:
                adopted = actual == (expected.blob, expected.mode)
                if workspace:
                    # Adopt a pre-integrity deployment only when installed bytes match.
                    adopted |= actual == (expected.blob, expected.mode | stat.S_IWUSR)
                if full not in old["files"] and not adopted and not (workspace and check):
                    raise Invalid("differing unowned output: " + full + "; see --help section: Recovery after an interrupted fresh apply followed by source edits")
            elif full not in old["files"]:
                problems.append("unknown unowned output: " + full)
    for path, out in sorted(plan.files.items()):
        actual = tree.read(path)
        if check and actual != (out.blob, out.mode):
            problems.append("output drift: " + path)
        elif (workspace and not check and actual is not None and actual != (out.blob, out.mode)
              and path in old["files"] and not matches(actual, old["files"][path])):
            problems.append("modified workspace output: " + path)
    for path, record in old["files"].items():
        if path in plan.files:
            continue
        if preserved(path, plan):
            raise Invalid("preserved runtime path was previously owned: " + path)
        actual = tree.read(path)
        if actual is not None and not matches(actual, record):
            problems.append("modified retirement candidate: " + path)
        elif check and actual is not None:
            problems.append("retired output remains: " + path)
    # The discovery directory itself is forbidden, even if empty.
    if ".codex" in roots:
        try:
            with tree.directory(".codex/skills"):
                problems.append("legacy Codex discovery path remains")
        except FileNotFoundError:
            pass
    if check and not workspace:
        state = tree.read(STATE)
        if state is None or state != (plan.manifest(), 0o644):
            problems.append("ownership manifest drift")
    if workspace:
        if check:
            for path in sorted(directories):
                try:
                    with tree.directory(path) as fd:
                        if stat.S_IMODE(os.fstat(fd).st_mode) != POLICY_DIR_MODE:
                            problems.append("policy directory mode drift: " + path)
                except FileNotFoundError:
                    problems.append("missing policy directory: " + path)
        if not tree.has_directory(WORKSPACE_RUNTIME) and check:
            problems.append("missing runtime directory: " + WORKSPACE_RUNTIME)
    return problems


@contextlib.contextmanager
def workspace_anchor(workspace: str) -> Iterator[tuple[Tree, str]]:
    root = state_root().resolve(strict=True)
    records = "batch_state/agent-deploy"
    if (root / records).is_relative_to(workspace):
        raise Invalid("workspace digest record requires a state root outside --project-root")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        key = records + "/workspace-" + digest(os.fsencode(workspace))
        yield Tree(fd, staging=key + ".tmp"), key + ".json"
    finally:
        os.close(fd)


def deploy_workspace(tree: Tree, plan: Plan, workspace: str, *, check: bool, print_manifest: bool) -> int:
    with workspace_anchor(workspace) as (anchor, path):
        entry = anchor.read(path)
        ownership = decode(plan.manifest())
        expected = encoded({"ownership": ownership, "inputs": plan.input_digests})
        old: dict[str, Any] = {"files": {}, "directories": []}
        problems: list[str] = []
        if entry is None:
            if check:
                problems.append("missing workspace digest record: " + path)
        else:
            record = decode(entry[0])
            if (not isinstance(record, dict) or set(record) != {"ownership", "inputs"}
                    or not isinstance(record["inputs"], dict) or entry[1] != 0o600):
                raise Invalid("invalid workspace digest record: " + path)
            old = validate_ownership(record["ownership"], modes={0o444, 0o555})
            if check:
                before = record["inputs"]
                for name in sorted(before.keys() | plan.input_digests.keys()):
                    if before.get(name) != plan.input_digests.get(name):
                        problems.append("workspace input drift: " + relative(name))
                if not problems and old != ownership:
                    problems.append("workspace deployment differs from " + path)
        problems.extend(preflight(tree, plan, old, check, workspace=True))
        if problems:
            raise Drift("; ".join(problems))
        if check:
            return report_check(plan, print_manifest)
        changed = apply(tree, plan, old, workspace=True)
        # Publish only after verifying the rendered files and locked directories.
        # This staging name is reserved per workspace under the same deployment lock.
        if anchor.read(anchor.staging) is not None:
            anchor.unlink(anchor.staging)
        if entry != (expected, 0o600):
            anchor.replace(path, expected, 0o600)
        print(f"deployed {len(plan.files)} outputs; {changed} changes")
        return 0


def report_check(plan: Plan, print_manifest: bool) -> int:
    if print_manifest:
        sys.stdout.buffer.write(plan.manifest())
        sys.stdout.flush()
    else:
        print(f"verified {len(plan.files)} outputs")
    return 0


def deploy(tree: Tree, *, check: bool, print_manifest: bool = False, inputs_commit: str | None = None,
           workspace: str | None = None) -> int:
    plan = build(tree, CommitInputs(tree, inputs_commit) if inputs_commit is not None else None,
                 workspace=workspace is not None)
    if workspace is not None:
        return deploy_workspace(tree, plan, workspace, check=check, print_manifest=print_manifest)
    old = old_state(tree)
    problems = preflight(tree, plan, old, check)
    if problems:
        raise Drift("; ".join(problems))
    if check:
        return report_check(plan, print_manifest)
    try:
        changed = apply(tree, plan, old)
    except OSError as exc:
        raise Drift(f"application failed before success publication: {exc}") from exc
    print(f"deployed {len(plan.files)} outputs; {changed} changes")
    return 0


def apply(tree: Tree, plan: Plan, old: dict[str, Any], *, workspace: bool = False) -> int:
    """Apply a fully admitted plan, then publish its verified ownership record."""
    changed = 0
    if tree.read(STAGING) is not None:
        tree.unlink(STAGING)
        changed += 1
    if workspace:
        # Create the mutable lease/pointer directory before securing provider directories.
        with tree.directory(WORKSPACE_RUNTIME, create=True):
            pass
        for path in sorted(plan.directories()):
            with tree.directory(path, create=True) as fd:
                if stat.S_IMODE(os.fstat(fd).st_mode) != POLICY_DIR_MODE:
                    os.fchmod(fd, POLICY_DIR_MODE)
                    os.fsync(fd)
    for path, out in sorted(plan.files.items()):
        actual = tree.read(path)
        if actual != (out.blob, out.mode):
            # Recheck ownership immediately before replacement, using held descriptors.
            legacy = workspace and actual == (out.blob, out.mode | stat.S_IWUSR)
            if actual is not None and path not in old["files"] and not legacy:
                raise Invalid("output appeared after preflight: " + path)
            tree.replace(path, out.blob, out.mode)
            changed += 1
    for path, record in sorted(old["files"].items()):
        if path not in plan.files:
            actual = tree.read(path)
            if actual is not None:
                if not matches(actual, record):
                    raise Drift("retirement candidate changed: " + path)
                tree.unlink(path)
                changed += 1
    for path in sorted(set(old["directories"]) - plan.directories(), key=lambda p: (-p.count("/"), p)):
        tree.rmdir(path)
    # Verify actual bytes/modes before publishing success. No manifest is a source of expected bytes.
    for path, out in plan.files.items():
        if tree.read(path) != (out.blob, out.mode):
            raise Drift("post-write verification failed: " + path)
    remaining = preflight(tree, plan, old, workspace, workspace=workspace)
    if remaining:
        raise Drift("; ".join(remaining))
    if tree.read(STATE) != (plan.manifest(), 0o644):
        tree.replace(STATE, plan.manifest(), 0o644)
        changed += 1
    return changed
