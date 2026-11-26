"""Load and validate the single machinery.host.v1 host binding.

The default file is root-owned. LU_HOST_CONTEXT can select a trusted scratch
file for tests and canaries. Roles share the full schema and assertion checks.
The state role validates only the state directory and requires no Git or private
mounts. The wheel digest records the intended artifact for the installer. The
startup probe checks the installed distribution version and its import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from lu_machinery.common import json_depth

SCHEMA = "machinery.host.v1"
DEFAULT_PATH = Path("/etc/machinery/host.v1.json")
CONTEXT_ENV = "LU_HOST_CONTEXT"
REGISTRY_PATH = Path(__file__).with_name("config") / "fleet_repos.yaml"
_MAX_BYTES = 64 * 1024
_KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_DIGEST = re.compile(r"[a-fA-F0-9]{64}")
_CONFIG_KEYS = (
    r"^(remote\.origin\.url|include\..*|includeif\..*|url\..*|core\.worktree|extensions\.worktreeconfig)$"
)


class HostContextError(ValueError):
    """A refusal with a stable code and a diagnostic free of host-specific values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Runtime:
    python: Path
    lu_machinery: str
    wheel_sha256: str


@dataclass(frozen=True)
class Product:
    repo: str
    python: Path


@dataclass(frozen=True)
class PrivateTool:
    path: Path
    sha256: str


@dataclass(frozen=True)
class CTO:
    workspace: Path
    starter: Path
    config_dir: Path


@dataclass(frozen=True)
class HostContext:
    state_root: Path
    runtime: Runtime
    repos: Mapping[str, Path]
    products: Mapping[str, Product]
    default_product: str
    private_tools: Mapping[str, PrivateTool]
    cto: CTO
    selector_file: Path
    monitor_url: str
    host: str | None = None


def _text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value)
    ):
        raise HostContextError("invalid_schema", f"{field} must be a nonempty single-line string")
    return value


def _path(value: Any, field: str) -> Path:
    text = _text(value, field)
    path = Path(text)
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts or path.as_posix() != text:
        raise HostContextError("invalid_schema", f"{field} must be an absolute normalized path")
    return path


def _digest(value: Any, field: str) -> str:
    digest = _text(value, field)
    if not _DIGEST.fullmatch(digest):
        raise HostContextError("invalid_schema", f"{field} must be a SHA-256 hex digest")
    return digest.lower()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostContextError("invalid_schema", "context contains a duplicate JSON key")
        result[key] = value
    return result


def _object(value: Any, field: str, fields: str = "", *, optional: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HostContextError("invalid_schema", f"{field} must be an object")
    if fields:
        required = set(fields.split())
        if not required <= value.keys() <= required | set(optional.split()):
            raise HostContextError("invalid_schema", f"{field} has missing or unknown fields")
    elif any(not _KEY.fullmatch(key) for key in value):
        raise HostContextError("invalid_schema", f"{field} has an invalid key")
    return value


def _monitor_url(value: Any) -> str:
    url = _text(value, "monitor_url")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not (parsed.query or parsed.fragment)
            and (parsed.port is None or parsed.port > 0)
            and not any(char.isspace() or char in "\\?#" for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HostContextError("invalid_schema", "monitor_url must be an HTTP(S) base URL without credentials")
    return url


def _read(source: Path, *, override: bool) -> Any:
    try:
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            owners = {0, os.geteuid()} if override else {0}
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
                raise HostContextError("untrusted_context", "context file must be regular and protected from other users")
            if not override:
                for parent in source.parents:
                    info = parent.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                        raise HostContextError("untrusted_context", "default context requires protected root-owned directories")
            content = stream.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        raise HostContextError("missing_context", "host context file is missing") from None
    except OSError:
        raise HostContextError("unreadable_context", "host context file cannot be opened") from None
    if len(content) > _MAX_BYTES:
        raise HostContextError("invalid_schema", "host context file exceeds 64 KiB")
    try:
        return json_depth.loads(content.decode("utf-8"), max_depth=8, object_pairs_hook=_unique_object)
    except HostContextError:
        raise
    except (ValueError, RecursionError):
        raise HostContextError("invalid_schema", "host context must contain valid UTF-8 JSON") from None


def _parse(raw: Any) -> HostContext:
    data = _object(
        raw, "context",
        "schema state_root runtime repos products default_product private_tools cto selector_file monitor_url",
        optional="host",
    )
    if data["schema"] != SCHEMA:
        raise HostContextError("invalid_schema", f"schema must be {SCHEMA}")
    runtime = _object(data["runtime"], "runtime", "python lu_machinery wheel_sha256")
    repos = {key: _path(path, "repos path") for key, path in _object(data["repos"], "repos").items()}
    if "machinery" not in repos or len(set(repos.values())) != len(repos):
        raise HostContextError("invalid_schema", "repos must bind machinery and use distinct checkout paths")
    products = {}
    for key, value in _object(data["products"], "products").items():
        row = _object(value, "product", "repo python")
        repo = _text(row["repo"], "product repo")
        if repo not in repos:
            raise HostContextError("invalid_schema", "product repo must name a bound checkout")
        products[key] = Product(repo, _path(row["python"], "product python"))
    default = _text(data["default_product"], "default_product")
    if default not in products:
        raise HostContextError("invalid_schema", "default_product must name a bound product")
    tools = {}
    for key, value in _object(data["private_tools"], "private_tools").items():
        row = _object(value, "private tool", "path sha256")
        tools[key] = PrivateTool(_path(row["path"], "private tool path"), _digest(row["sha256"], "private tool sha256"))
    cto = _object(data["cto"], "cto", "workspace starter config_dir")
    return HostContext(
        state_root=_path(data["state_root"], "state_root"),
        runtime=Runtime(
            _path(runtime["python"], "runtime.python"),
            _text(runtime["lu_machinery"], "runtime.lu_machinery"),
            _digest(runtime["wheel_sha256"], "runtime.wheel_sha256"),
        ),
        repos=MappingProxyType(repos),
        products=MappingProxyType(products),
        default_product=default,
        private_tools=MappingProxyType(tools),
        cto=CTO(*(_path(cto[key], f"cto.{key}") for key in ("workspace", "starter", "config_dir"))),
        selector_file=_path(data["selector_file"], "selector_file"),
        monitor_url=_monitor_url(data["monitor_url"]),
        host=_text(data["host"], "host") if "host" in data else None,
    )


def _assert_legacy(
    context: HostContext, env: Mapping[str, str], registry: Mapping[str, dict[str, Any]],
) -> None:
    # Compare normalized spellings before filesystem probes, even for the state role.
    batch = context.state_root / "batch_state"
    primary = context.repos["machinery"]
    product = context.repos[context.products[context.default_product].repo]
    assertions: dict[str, Path | str | None] = {
        "LU_GITHUB_SLUG": registry["machinery"]["github"],
        "LEARN_UKRAINIAN_GITHUB_SLUG": registry["machinery"]["github"],
        "LU_STATE_ROOT": context.state_root,
        "LEARN_UKRAINIAN_STATE_ROOT": context.state_root,
        "LU_PRIMARY_REPO_ROOT": primary,
        "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT": primary,
        "LU_PRODUCT_ROOT": product,
        "LEARN_UK_REPO_ROOT": product,
        "LU_PYTHON": context.runtime.python,
        "LEARN_UKRAINIAN_PYTHON": context.runtime.python,
        "LU_TASKS_DIR": batch / "tasks",
        "LEARN_UKRAINIAN_BATCH_STATE_DIR": batch,
        "FLEET_COMMS_ROOT": batch / "fleet-comms" / "v1",
        "LEARN_UKRAINIAN_OWNERSHIP_LEDGER": batch / "tasks" / "write-ownership.sqlite3",
        "LU_GITHUB_CACHE_DIR": batch / "github-client",
        "LEARN_UKRAINIAN_INFRA_PRIVATE_ROOT": context.repos.get("infra-private"),
        "LU_FLEET_REPO_SELECTOR_FILE": context.selector_file,
        "MONITOR_PROJECT_STATE_URL": context.monitor_url.rstrip("/") + "/api/fleet/projects/v1/report",
        # Profiles can redirect packaged policy and cannot assert an equivalent host binding.
        "LU_PROFILE_PATH": None,
    }
    for name in ("LU_MONITOR_LOOPBACK", "MONITOR_API_BASE_URL", "MONITOR_API_URL", "AB_MONITOR_URL"):
        assertions[name] = context.monitor_url
    if "LU_REPOS_ROOT" in env:
        parents = {path.parent for path in context.repos.values()}
        same_layout = len(parents) == 1 and all(
            path.name == registry[key]["local_name"] for key, path in context.repos.items()
        )
        assertions["LU_REPOS_ROOT"] = next(iter(parents)) if same_layout else None
    for name, expected in assertions.items():
        if name in env and (expected is None or env[name] != str(expected)):
            raise HostContextError("legacy_conflict", f"{name} disagrees with host context")


def _outside_git(path: Path, *, code: str, label: str) -> None:
    # A checkout marker may be a directory, a linked-worktree gitfile or a symlink.
    for ancestor in (path, *path.parents):
        try:
            (ancestor / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise HostContextError(code, f"cannot inspect {label} ancestors") from None
        raise HostContextError(code, f"{label} must be outside Git work trees")


def _check_state(root: Path) -> None:
    try:
        resolved = root.resolve(strict=True)
        info = root.stat()
    except (OSError, RuntimeError):
        raise HostContextError("invalid_state_root", "state_root must be an accessible directory") from None
    if resolved != root or not stat.S_ISDIR(info.st_mode):
        raise HostContextError("invalid_state_root", "state_root must be a physical directory")
    _outside_git(root, code="state_root_in_git", label="state_root")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise HostContextError("invalid_state_root", "state_root must be owned by the service user with mode 0700")


def _git(path: Path, *args: str) -> bytes:
    from lu_machinery.orchestration.execution_safe_git import GIT, safe_git_env

    try:
        result = subprocess.run(
            [GIT, "--no-pager", "--no-lazy-fetch", "-C", str(path), *args],
            cwd="/", env=safe_git_env({}), stdin=subprocess.DEVNULL, capture_output=True,
            timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise HostContextError("git_check_failed", "checkout Git metadata could not be verified") from None
    return result.stdout


def _check_repo(path: Path, origins: tuple[str, ...]) -> None:
    try:
        physical = path.resolve(strict=True) == path
        git_dir = path / ".git"
        primary = physical and stat.S_ISDIR(git_dir.lstat().st_mode)
    except (OSError, RuntimeError):
        primary = False
    if not primary:
        raise HostContextError("repo_not_primary", "repo must be a primary checkout with its own Git directory")
    _outside_git(path.parent, code="repo_not_primary", label="checkout parent")
    config = _git(path, "config", "--file", str(git_dir / "config"), "--no-includes", "--null", "--get-regexp", _CONFIG_KEYS)
    entries = config.removesuffix(b"\0").split(b"\0")
    prefix = b"remote.origin.url\n"
    if len(entries) != 1 or not entries[0].startswith(prefix):
        raise HostContextError("repo_origin_mismatch", "checkout must declare exactly one unambiguous origin")
    origin = entries[0][len(prefix):]
    if origin.lower() not in {value.encode("ascii").lower() for value in origins}:
        raise HostContextError("repo_origin_mismatch", "checkout origin must match the packaged registry")
    layout = _git(
        path, "rev-parse", "--path-format=absolute", "--is-inside-work-tree",
        "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
    )
    if layout != b"\n".join([b"true", os.fsencode(path), os.fsencode(git_dir), os.fsencode(git_dir), b""]):
        raise HostContextError("repo_not_primary", "Git must report the bound checkout as its primary work tree")


_VERSION_PROBE = (
    "import importlib.metadata as m, pathlib, lu_machinery; "
    "d = m.distribution('lu-machinery'); "
    "p = pathlib.Path(lu_machinery.__file__).resolve(); "
    "print(d.version) if p == d.locate_file('lu_machinery/__init__.py').resolve() else exit(1)"
)


def _check_python(path: Path, version: str) -> None:
    if not path.is_file() or not os.access(path, os.X_OK):
        raise HostContextError("invalid_interpreter", "configured Python must be an executable file")
    try:
        result = subprocess.run(
            [path, "-I", "-B", "-c", _VERSION_PROBE],
            cwd="/", env={}, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise HostContextError("invalid_interpreter", "configured Python cannot import its installed machinery distribution") from None
    if result.stdout != (version + "\n").encode("utf-8"):
        raise HostContextError("interpreter_version_mismatch", "configured Python reports a different machinery version")


def _check_private_tool(tool: PrivateTool) -> None:
    try:
        if tool.path.parent.resolve(strict=True) != tool.path.parent:
            raise HostContextError("invalid_private_tool", "private tool must use a physical path")
        _outside_git(tool.path.parent, code="invalid_private_tool", label="private tool")
        with os.fdopen(os.open(tool.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise HostContextError("invalid_private_tool", "private tool must be a regular file")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise HostContextError("invalid_private_tool", "private tool cannot be opened") from None
    if digest != tool.sha256:
        raise HostContextError("private_tool_digest_mismatch", "private tool digest differs from its binding")


def load_host_context(*, role: str = "all", env: Mapping[str, str] | None = None) -> HostContext:
    """Read a fresh binding. A state caller may only use the validated state root."""
    if role not in {"all", "state"}:
        raise HostContextError("invalid_role", "role must be all or state")
    env = os.environ if env is None else env
    override = CONTEXT_ENV in env
    source = _path(env[CONTEXT_ENV], CONTEXT_ENV) if override else DEFAULT_PATH
    context = _parse(_read(source, override=override))
    from lu_machinery.common.git_credentials import canonical_origins, repository_registry

    try:
        registry = repository_registry(REGISTRY_PATH)
    except (OSError, ValueError):
        raise HostContextError("invalid_registry", "packaged fleet registry cannot be loaded") from None
    if not context.repos.keys() <= registry.keys():
        raise HostContextError("unknown_repo", "repos must use packaged fleet registry keys")
    _assert_legacy(context, env, registry)
    _check_state(context.state_root)
    if role == "state":
        return context

    for key, path in context.repos.items():
        try:
            # R1 verifies the packaged identity. Credential use still requires
            # the selector and its existing independent trust checks.
            row = registry[key]
            origins = canonical_origins({"github": row["github"], "transport": row.get("transport", "ssh")})
        except (OSError, ValueError):
            raise HostContextError("invalid_repo_binding", "cannot select the registered repository origin") from None
        _check_repo(path, origins)

    interpreters = dict.fromkeys((context.runtime.python, *(product.python for product in context.products.values())))
    for interpreter in interpreters:
        _check_python(interpreter, context.runtime.lu_machinery)
    for tool in context.private_tools.values():
        _check_private_tool(tool)
    return context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the machinery.host.v1 host binding.\n"
            "Run at startup or check a scratch binding selected by LU_HOST_CONTEXT."
        ),
        epilog=(
            "Examples:\n"
            "  .venv/bin/python -I -m lu_machinery.host_context check --json\n"
            "  .venv/bin/python -I -m lu_machinery.host_context check --role state\n"
            "Outputs: one receipt on stdout. No state is written.\n"
            "Exit codes: 0 valid, 1 refused, 2 CLI syntax error.\n"
            "Related: REBUILD-PLAN-1126 R1, machinery.host.v1."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["check"], help="check the binding and print a receipt")
    parser.add_argument("--role", default="all", help="validation scope: all (default), or state")
    parser.add_argument("--json", action="store_true", help="emit a JSON receipt; defaults to a text result")
    args = parser.parse_args(argv)
    receipt: dict[str, Any] = {"schema": SCHEMA, "ok": True}
    try:
        load_host_context(role=args.role)
    except HostContextError as exc:
        receipt.update(ok=False, code=exc.code, message=str(exc))
    if args.json:
        print(json.dumps(receipt))
    elif receipt["ok"]:
        print(f"{SCHEMA} {args.role} check passed")
    else:
        print(f"{receipt['code']}: {receipt['message']}")
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
