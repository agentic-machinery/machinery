"""One opt-in fixture: run a module's tests, and every git or subprocess they start, without the host's environment.

The caller's environment reaches a test, and everything it spawns, in six ways:

- Git settings: inherited ``GIT_*`` variables (``GIT_DIR``, ``GIT_CONFIG_COUNT`` with its indexed keys and values,
  ``GIT_CONFIG_PARAMETERS``) and the global and system config files, including the default ignore and attributes
  files Git reads from ``$XDG_CONFIG_HOME/git`` or ``$HOME/.config/git`` when ``core.excludesFile`` and
  ``core.attributesFile`` are unset.
- User configuration: ``HOME``, every ``XDG_*`` base directory and the config-directory overrides
  (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``) the caller exported. The dispatcher reads private account bindings from
  these, for example ``codex_accounts.json`` under the XDG learn-ukrainian config directory. The suite's own
  ``CLAUDE_CONFIG_DIR`` (a session scratch directory, ``lu_machinery.common.pytest_scratch``) stays.
- Exported machinery and agent-shim settings: ``LU_*``, ``LEARN_UKRAINIAN_*`` and ``AGENT_*`` variables that the
  caller exported, including the product-root and primary-root variables and the shims' ``AGENT_ORIGINAL_PATH``.
- Python startup settings: ``PYTHON*`` variables the caller exported. ``PYTHONSAFEPATH`` keeps a child's working
  directory off ``sys.path``, so ``-m lu_machinery...`` loads another installed copy; ``PYTHONPATH`` selects which
  code a child imports. The suite prepends its own entry to ``PYTHONPATH`` (the cursor exec tripwire), so the
  caller's entries are removed from it and the suite's stay.
- Agent shims on ``PATH``: an agent session puts machinery's ``git``, ``gh`` and ``npm`` shims first on ``PATH``
  (``lu_machinery.dispatcher.site_deps.SHIM_DIRS``); its ``git`` shim refuses a fixture's ``git push``.
- Temporary files: ``TMPDIR``, which children use for their own scratch.

A module opts in by importing the fixture; it is autouse, so it runs before the module's other fixtures
(``configured_roots``, ``sandbox``) initialize a repository or copy ``os.environ`` into a child environment::

    from tests.helpers.host_env import host_isolated_env  # noqa: F401

An exported setting is one whose current value is the caller's: the environment this process started with
(``/proc/self/environ``) or, in an xdist worker, the one its controller started with (the worker's own start
environment already holds what the controller published, such as ``PYTHONDONTWRITEBYTECODE``). A worker is known by
its pytest ``workerinput``, never by an environment variable a nested run inherits, and it reads its controller only
after checking that its parent is the controller ``tests/conftest.py`` names there (``CONTROLLER_PID_KEY``). The
values the suite publishes itself, such as the telemetry redirect, scratch root and task store, differ from the
inherited ones and stay, so a child never loses a redirect and writes to production. Equal values cannot show who
set a variable, so the names in ``SUITE_PUBLISHED`` are never dropped: the suite publishes each of them for every
test, whatever the caller exported. Every other caller value goes, ``LU_TEST_*`` harness settings included: a
caller's test-slot pool (``LU_TEST_SLOTS_DIR``) would otherwise steer a sandbox child's dispatch admission to that
pool. The test interpreter (``LEARN_UKRAINIAN_PYTHON``) is set to this
interpreter, so a test needs no caller-provided value. ``monkeypatch`` restores everything after the test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lu_machinery.dispatcher.site_deps import SHIM_DIRS

GIT_PREFIX = "GIT_"
XDG_PREFIX = "XDG_"
CONFIG_DIR_VARIABLES = ("CODEX_HOME", "CLAUDE_CONFIG_DIR")
EXPORTED_PREFIXES = ("LU_", "LEARN_UKRAINIAN_", "AGENT_", "PYTHON")
# Values tests/conftest.py publishes for every test before this fixture runs that can equal an inherited value: the
# constants of the session fixture ``no_free_disk_floor`` and of the autouse ``_no_host_claude_default_account``,
# ``_no_host_usage_cache`` and ``_hermetic_dispatch_admission_host``, and the write-guard anchor
# ``_protected_store_paths`` sets once and xdist workers and nested sessions inherit on purpose; the checkout path
# of the autouse ``_scope_real_checkout_acp_execution_to_tmp``; and the ``PYTHONDONTWRITEBYTECODE=1`` that
# ``lu_machinery.common.pytest_scratch`` publishes for every session.
SUITE_PUBLISHED = frozenset({
    "LU_EVIDENCE_FREE_DISK_FLOOR_BYTES",
    "LU_CLAUDE_DEFAULT_ACCOUNT_FILE",
    "LU_USAGE_CACHE_FILE",
    "LU_TEST_DISPATCH_HEALTHY_HOST",
    "LU_TEST_PROTECTED_STORES",
    "LU_TEST_ACP_PRIMARY_ROOT",
    "PYTHONDONTWRITEBYTECODE",
})
SEARCH_PATH_VARIABLE = "PYTHONPATH"
CONTROLLER_PID_KEY = "lu_host_env_controller_pid"
INTERPRETER_VARIABLE = "LEARN_UKRAINIAN_PYTHON"


def inherited_environment(workerinput: dict[str, object] | None = None) -> dict[str, str]:
    """The caller's environment, before the suite or a test changed ``os.environ``.

    That is this process's start environment or, for an xdist worker (its ``workerinput``), the start environment of
    the controller that started it. Fails the test rather than guessing when the worker's parent is not the
    controller the workerinput names, or when the environment cannot be read.
    """
    source = "/proc/self/environ"
    if workerinput is not None:
        controller = workerinput.get(CONTROLLER_PID_KEY)
        if type(controller) is not int or controller != os.getppid():
            pytest.fail(
                f"host_isolated_env: this xdist worker's parent ({os.getppid()}) is not the controller its workerinput "
                f"names ({controller!r}), so the caller's environment is unknown"
            )
        source = f"/proc/{controller}/environ"
    try:
        raw = Path(source).read_bytes()
    except OSError as exc:
        pytest.fail(f"host_isolated_env needs {source} to tell the caller's settings apart: {exc}")
    pairs = (entry.split(b"=", 1) for entry in raw.split(b"\0") if b"=" in entry)
    return {os.fsdecode(name): os.fsdecode(value) for name, value in pairs}


def caller_exported(name: str, value: str, inherited: dict[str, str]) -> bool:
    """True for a machinery, shim, Python or config-directory setting that still holds the caller's value."""
    if not (name.startswith(EXPORTED_PREFIXES) or name in CONFIG_DIR_VARIABLES) or name in SUITE_PUBLISHED:
        return False
    return inherited.get(name) == value


def without_caller_entries(value: str, inherited: str | None) -> str:
    """``value`` without the entries of the caller's ``inherited`` search path, in order."""
    caller = set((inherited or "").split(os.pathsep))
    return os.pathsep.join(entry for entry in value.split(os.pathsep) if entry and entry not in caller)


def without_shims(search_path: str) -> str:
    """``search_path`` without machinery's agent-shim directories (the rule ``site_deps`` applies).

    Each entry is resolved strictly. An entry that cannot be resolved (missing, a symlink loop, unreadable) holds no
    executable, so it is dropped the same way on every supported Python: 3.12 raises ``RuntimeError`` for a loop,
    later versions ``OSError``. The order of the remaining entries is kept.
    """
    kept = []
    for entry in search_path.split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.parts[-3:] not in SHIM_DIRS:
            kept.append(entry)
    return os.pathsep.join(kept)


@pytest.fixture(autouse=True)
def host_isolated_env(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Drop the caller's Git, user-config, exported, Python, shim and temp settings; return the test's private home."""
    inherited = inherited_environment(getattr(request.config, "workerinput", None))
    for name, value in list(os.environ.items()):
        if name.startswith((GIT_PREFIX, XDG_PREFIX)) or caller_exported(name, value, inherited):
            monkeypatch.delenv(name)
    if SEARCH_PATH_VARIABLE in os.environ:
        search_path = without_caller_entries(os.environ[SEARCH_PATH_VARIABLE], inherited.get(SEARCH_PATH_VARIABLE))
        if search_path:
            monkeypatch.setenv(SEARCH_PATH_VARIABLE, search_path)
        else:
            monkeypatch.delenv(SEARCH_PATH_VARIABLE)
    monkeypatch.setenv("PATH", without_shims(os.environ.get("PATH", os.defpath)))

    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    temp = tmp_path_factory.mktemp("tmpdir")
    monkeypatch.setenv("TMPDIR", str(temp))
    config = tmp_path_factory.mktemp("git-config") / "gitconfig"
    # Unset, these two fall back to files under $XDG_CONFIG_HOME or $HOME (git/ignore, git/attributes).
    config.write_text(f"[core]\n\texcludesFile = {os.devnull}\n\tattributesFile = {os.devnull}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv(INTERPRETER_VARIABLE, sys.executable)
    return home
