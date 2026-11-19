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

An exported setting is one whose current value is the caller's. In a single process, the caller's values are the
environment it started with (``/proc/self/environ``). An xdist worker starts with its controller's environment,
which holds the caller's values and the ones the controller published (``PYTHONDONTWRITEBYTECODE``, the scratch
``CLAUDE_CONFIG_DIR``, the cursor tripwire's ``PYTHONPATH`` entry), and then applies the ``env:`` settings of its
``--tx`` specification. So the controller names both in the worker's ``workerinput`` (``LAUNCH_KEY``, written by
``tests/conftest.py``): what it published (its environment minus its own start environment) and the specification's
settings. A worker's caller values are its start environment minus what the controller published, plus the
specification's settings. A worker is known by its ``workerinput``, never by an environment variable a nested run
inherits, and a worker without a well-formed launch record fails the test with the reason.

The values the suite publishes itself, such as the telemetry redirect, scratch root and task store, differ from the
caller's and stay, so a child never loses a redirect and writes to production; a caller value that replaced one the
controller published is set back to the controller's. Equal values cannot show who set a variable, so the names in
``SUITE_PUBLISHED`` are never dropped: the suite publishes each of them for every test, whatever the caller exported.
Every other caller value goes, ``LU_TEST_*`` harness settings included: a caller's test-slot pool
(``LU_TEST_SLOTS_DIR``) would otherwise steer a sandbox child's dispatch admission to that pool. The test interpreter
(``LEARN_UKRAINIAN_PYTHON``) is set to this interpreter, so a test needs no caller-provided value. ``monkeypatch``
restores everything after the test.

A start environment still holds the credentials ``tests/conftest.py`` stripped at import, so it is read only inside
the functions below, before which every input that could raise is checked; they return names and suite values, never
a caller value, and no fixture frame a failure report shows holds one.
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
LAUNCH_KEY = "lu_host_env_launch"
INTERPRETER_VARIABLE = "LEARN_UKRAINIAN_PYTHON"
START_ENVIRONMENT = "/proc/self/environ"


def judged(name: str) -> bool:
    """True for a machinery, shim, Python or config-directory setting, the names whose provenance decides."""
    return name.startswith(EXPORTED_PREFIXES) or name in CONFIG_DIR_VARIABLES


def search_path_entries(value: str | None) -> list[str]:
    """The non-empty entries of a ``PYTHONPATH`` value, in order."""
    return [entry for entry in (value or "").split(os.pathsep) if entry]


def start_environment() -> dict[str, str] | str:
    """This process's start environment, judged names only, or why it cannot be read."""
    try:
        raw = Path(START_ENVIRONMENT).read_bytes()
    except OSError as exc:
        return f"host_isolated_env needs {START_ENVIRONMENT} to tell the caller's settings apart: {exc}"
    pairs = (map(os.fsdecode, entry.split(b"=", 1)) for entry in raw.split(b"\0") if b"=" in entry)
    return {name: value for name, value in pairs if judged(name)}


def launch_record(specification: object) -> dict[str, object]:
    """What an xdist controller tells a worker it starts: its own published settings and the ``--tx`` ``env:`` ones.

    ``specification`` is the execnet ``XSpec.env`` mapping (``None`` when the specification sets none). Published means
    set by this process after it started: a judged name whose value differs from the start environment, and for
    ``PYTHONPATH`` the entries the start value lacks. Never raises: a failure is recorded under ``error`` and fails
    the worker's tests that use the fixture, not the session.
    """
    start = start_environment()
    if isinstance(start, str):
        return {"error": start}
    if specification is not None and not isinstance(specification, dict):
        return {"error": f"host_isolated_env: the --tx env settings are {type(specification).__name__}, not a mapping"}
    published = {
        name: value
        for name, value in os.environ.items()
        if judged(name) and name != SEARCH_PATH_VARIABLE and start.get(name) != value
    }
    caller_entries = set(search_path_entries(start.get(SEARCH_PATH_VARIABLE)))
    added = [entry for entry in search_path_entries(os.environ.get(SEARCH_PATH_VARIABLE)) if entry not in caller_entries]
    settings = {
        str(name): str(value) for name, value in (specification or {}).items() if judged(str(name))
    }
    return {"published": published, "published_search_path": added, "specification": settings}


def checked_launch(workerinput: dict[str, object] | None) -> tuple[dict[str, str], list[str], dict[str, str]] | str:
    """The worker's launch record as (published, published search-path entries, specification), or why it is unusable.

    A single process (no ``workerinput``) has an empty record.
    """
    if workerinput is None:
        return {}, [], {}
    record = workerinput.get(LAUNCH_KEY)
    if not isinstance(record, dict):
        return (
            f"host_isolated_env: this xdist worker's workerinput has no launch record ({LAUNCH_KEY}), "
            f"only {type(record).__name__}"
        )
    if "error" in record:
        return f"host_isolated_env: the controller could not describe this worker's launch: {record['error']}"
    published, search_path, settings = (
        record.get("published"), record.get("published_search_path"), record.get("specification")
    )
    mappings_ok = all(
        isinstance(mapping, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
        for mapping in (published, settings)
    )
    if not mappings_ok or not isinstance(search_path, list) or not all(isinstance(e, str) for e in search_path):
        return f"host_isolated_env: this xdist worker's launch record ({LAUNCH_KEY}) is malformed"
    return published, search_path, settings


def caller_settings(workerinput: dict[str, object] | None) -> tuple[dict[str, str | None], set[str]] | str:
    """The settings in ``os.environ`` that hold the caller's value, or why that is unknown.

    Returns each such name mapped to the suite's value to put back (``None``: drop it), and the caller's ``PYTHONPATH``
    entries. ``PYTHONPATH`` itself is never in the mapping, and a ``SUITE_PUBLISHED`` name only when a ``--tx``
    setting replaced the suite's value. The caller's values stay inside this function: the launch record is checked
    before the start environment is read, and nothing after the read can raise.
    """
    launch = checked_launch(workerinput)
    if isinstance(launch, str):
        return launch
    published, published_entries, specification = launch
    start = start_environment()
    if isinstance(start, str):
        return start
    entries = set(search_path_entries(start.get(SEARCH_PATH_VARIABLE))) - set(published_entries)
    entries |= set(search_path_entries(specification.get(SEARCH_PATH_VARIABLE)))
    held: dict[str, str | None] = {}
    for name, value in os.environ.items():
        if not judged(name) or name == SEARCH_PATH_VARIABLE:
            continue
        if specification.get(name) == value:
            # Set by the --tx specification over the start value. The start value is the suite's when the controller
            # published it or the suite publishes the name; a SUITE_PUBLISHED name with no other suite value stays,
            # since a per-test fixture may have set the same value after the specification.
            prior = start.get(name)
            if prior is not None and (name in SUITE_PUBLISHED or published.get(name) == prior):
                held[name] = prior
            elif name in published:
                held[name] = published[name]
            elif name not in SUITE_PUBLISHED:
                held[name] = None
        elif name not in SUITE_PUBLISHED and start.get(name) == value and published.get(name) != value:
            held[name] = published.get(name)
    return held, entries


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
    settings = caller_settings(getattr(request.config, "workerinput", None))
    if isinstance(settings, str):
        pytest.fail(settings)
    held, caller_entries = settings
    for name in [name for name in os.environ if name.startswith((GIT_PREFIX, XDG_PREFIX))]:
        monkeypatch.delenv(name)
    for name, suite_value in held.items():
        if suite_value is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, suite_value)
    if SEARCH_PATH_VARIABLE in os.environ:
        entries = search_path_entries(os.environ[SEARCH_PATH_VARIABLE])
        search_path = os.pathsep.join(entry for entry in entries if entry not in caller_entries)
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
