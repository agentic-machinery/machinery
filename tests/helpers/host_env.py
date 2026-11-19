"""One opt-in fixture: run a module's tests, and every git or subprocess they start, without the host's environment.

The caller's environment reaches a test, and everything it spawns, in five ways:

- Git settings: inherited ``GIT_*`` variables (``GIT_DIR``, ``GIT_CONFIG_COUNT`` with its indexed keys and values,
  ``GIT_CONFIG_PARAMETERS``) and the global and system config files, including the default ignore and attributes
  files Git reads from ``$XDG_CONFIG_HOME/git`` or ``$HOME/.config/git`` when ``core.excludesFile`` and
  ``core.attributesFile`` are unset.
- User configuration: ``HOME``, every ``XDG_*`` base directory and the config-directory overrides
  (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``). The dispatcher reads private account bindings from these, for example
  ``codex_accounts.json`` under the XDG learn-ukrainian config directory.
- Machinery and agent-shim settings: ``LU_*``, ``LEARN_UKRAINIAN_*`` and ``AGENT_*`` variables that the caller
  exported, including the product-root and primary-root variables and the shims' ``AGENT_ORIGINAL_PATH``.
- Agent shims on ``PATH``: an agent session puts machinery's ``git``, ``gh`` and ``npm`` shims first on ``PATH``
  (``lu_machinery.dispatcher.site_deps.SHIM_DIRS``); its ``git`` shim refuses a fixture's ``git push``.
- Temporary files: ``TMPDIR``, which children use for their own scratch.

A module opts in by importing the fixture; it is autouse, so it runs before the module's other fixtures
(``configured_roots``, ``sandbox``) initialize a repository or copy ``os.environ`` into a child environment::

    from tests.helpers.host_env import host_isolated_env  # noqa: F401

Exported settings are removed only when the caller exported them: a variable is dropped when its current value is
the one this process inherited (``/proc/self/environ``). The values the suite publishes itself, such as the telemetry
redirect, scratch root and task store, differ from the inherited ones and stay, so a child never loses a redirect and
writes to production. Stub directories the suite puts on ``PATH`` stay too. The test interpreter
(``LEARN_UKRAINIAN_PYTHON``) is set to this interpreter, so a test needs no caller-provided value. ``monkeypatch``
restores everything after the test.
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
EXPORTED_PREFIXES = ("LU_", "LEARN_UKRAINIAN_", "AGENT_")
INTERPRETER_VARIABLE = "LEARN_UKRAINIAN_PYTHON"


def inherited_environment() -> dict[str, str]:
    """The environment this process started with, before the suite or a test changed ``os.environ``.

    Fails the test rather than guessing when the start environment cannot be read.
    """
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError as exc:
        pytest.fail(f"host_isolated_env needs /proc/self/environ to tell inherited settings apart: {exc}")
    pairs = (entry.split(b"=", 1) for entry in raw.split(b"\0") if b"=" in entry)
    return {os.fsdecode(name): os.fsdecode(value) for name, value in pairs}


def without_shims(search_path: str) -> str:
    """``search_path`` without machinery's agent-shim directories, by the rule ``site_deps`` applies."""
    entries = search_path.split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if not entry or Path(entry).resolve().parts[-3:] not in SHIM_DIRS)


@pytest.fixture(autouse=True)
def host_isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop the caller's Git, user-config, exported, shim and temp settings; return the test's private home."""
    inherited = inherited_environment()
    for name in list(os.environ):
        exported = name.startswith(EXPORTED_PREFIXES) and inherited.get(name) == os.environ[name]
        if name.startswith((GIT_PREFIX, XDG_PREFIX)) or name in CONFIG_DIR_VARIABLES or exported:
            monkeypatch.delenv(name)
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
