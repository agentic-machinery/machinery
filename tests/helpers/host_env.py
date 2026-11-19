"""One opt-in fixture: run a module's tests, and every git or subprocess they start, from an empty environment.

The fixture removes every environment variable and then sets back an explicit allowlist. A caller's setting
cannot reach a test unless its name is on that list, so no variable class needs to be known in advance: Git
settings, user-config directories, machinery, agent-shim, Python and proxy settings, credentials and anything
else the caller exported are absent by construction, in a single process and in every xdist worker, including
values a ``--tx env:`` specification sets.

The allowlist has two parts.

- ``SUITE_SETTINGS``: the values ``tests/conftest.py`` sets for every test, unconditionally, before this
  fixture runs (session hooks and fixtures, and autouse fixtures). Each is the suite's own redirect away from the
  host's stores and services. The suite overwrites any value the caller exported under one of these names, so the
  value kept is the suite's. A name the suite sets only when the caller has not (``setdefault`` or an
  ``if name not in os.environ`` guard) is not on the list. ``PYTEST_CURRENT_TEST`` is pytest's own, set for each
  phase. A redirect the suite adds later reaches these modules only once it is listed here.
- Values this fixture owns: ``HOME`` and ``TMPDIR`` in the test's temporary directories (the XDG base directories
  and the ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` defaults then resolve inside that home), ``PATH``, no system Git
  configuration, no bytecode files and the test interpreter (``LEARN_UKRAINIAN_PYTHON``).

``PATH`` holds the suite's stub directories, taken from the fixtures that create them (the ``claude doctor``
stub of ``tests/dispatcher/conftest.py``, and the failing ``gh`` and the fake Cursor of ``tests/conftest.py``
and ``tests/cursor_process_guard.py``), then this interpreter's directory, then the platform default
``os.defpath``. No caller entry, agent shim included, remains. The ``claude_doctor`` dependency ties the fixture
to modules under ``tests/dispatcher``; any other module that imports it fails at setup with a missing fixture.

The suite's cursor tripwire (``tests/cursor_exec_tripwire.py``) adds its ``PYTHONPATH`` entry and session token to
each child's environment when the child is spawned, so neither is kept here. ``monkeypatch`` restores the
environment after the test.

A module opts in by importing the fixture; it is autouse, so it runs before the module's other fixtures
(``configured_roots``, ``sandbox``) initialize a repository or copy ``os.environ`` into a child environment::

    from tests.helpers.host_env import host_isolated_env  # noqa: F401
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SUITE_SETTINGS = frozenset({
    # Session hooks and session fixtures, set in the controller and again in every xdist worker.
    "FLEET_COMMS_ROOT",
    "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT",
    "LEXICON_CAP_UNIT_PREFIX",
    "LU_EVIDENCE_FREE_DISK_FLOOR_BYTES",
    "LU_MCP_SOURCES_LOG_DIR",
    "LU_TELEMETRY_EVENTS_DIR",
    # Autouse fixtures, set for every test.
    "DASHBOARD_OVERVIEW_LAST_GOOD_PATH",
    "DELEGATE_OWNERSHIP_WAIT_S",
    "DISPATCH_ADMISSION_ENV_FILE",
    "LEARN_UKRAINIAN_BATCH_STATE_DIR",
    "LEARN_UKRAINIAN_LLM_QG_CIRCUIT",
    "LEARN_UKRAINIAN_LLM_QG_DB",
    "LEARN_UKRAINIAN_OWNERSHIP_LEDGER",
    "LEARN_UKRAINIAN_OWNERSHIP_TASK_STATE_DIR",
    "LU_CLAUDE_DEFAULT_ACCOUNT_FILE",
    "LU_GITHUB_CACHE_DIR",
    "LU_SCRATCH_ROOT",
    "LU_SCRATCH_SCAN_ROOT",
    "LU_TASKS_DIR",
    "LU_TEST_ACP_PRIMARY_ROOT",
    "LU_TEST_ACP_SCRATCH_ROOT",
    "LU_TEST_DISPATCH_HEALTHY_HOST",
    "LU_USAGE_CACHE_FILE",
    # pytest's own, set for each phase.
    "PYTEST_CURRENT_TEST",
})


@pytest.fixture(autouse=True)
def host_isolated_env(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    claude_doctor: Path,
    _fake_github_bin: Path,
    fake_cursor_bin: Path,
) -> None:
    """Empty the environment but for the suite's settings and this fixture's own values."""
    stubs = [claude_doctor.parent, _fake_github_bin, fake_cursor_bin]
    for name in [name for name in os.environ if name not in SUITE_SETTINGS]:
        monkeypatch.delenv(name)
    own = {
        "HOME": str(tmp_path_factory.mktemp("home")),
        "TMPDIR": str(tmp_path_factory.mktemp("tmpdir")),
        "PATH": os.pathsep.join([*map(str, stubs), str(Path(sys.executable).parent), os.defpath]),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LEARN_UKRAINIAN_PYTHON": sys.executable,
    }
    for name, value in own.items():
        monkeypatch.setenv(name, value)
