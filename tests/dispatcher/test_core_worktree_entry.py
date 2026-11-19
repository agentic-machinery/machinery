"""A linked worktree's dispatcher entry points validate the configured primary and stay in place.

Adapted from learn-ukrainian ``tests/dispatcher/test_core_worktree_entry.py`` at 5aba2adb (machinery #904).
The product's in-tree copy re-executed the primary checkout's ``scripts/dispatch.py``. The ``lu_machinery``
package never does: ``in_tree_source()`` is false for every ``lu_machinery`` layout, so installed code stays
installed and only validates its configured primary (``lu_machinery/dispatcher/primary.py``, C-4843). The
re-exec and its unresolvable-gitdir refusal are reachable only in the in-tree layout; that refusal is tested
in-process in ``tests/orchestration/test_distinct_roots.py``.

Each case runs a machinery entry point (the ``lu_machinery/dispatch.py`` script or
``-m lu_machinery.dispatcher.cli``) as a subprocess from a real linked worktree of the sandbox checkout.

Defect classes: an entry point loses its bootstrap and runs with no configured primary; a linked worktree
hands its run to another checkout's dispatcher code.
"""

import subprocess
import sys

import pytest

from tests.dispatcher.conftest import prior_worktree
from tests.helpers.product_tree import configured_roots  # noqa: F401

ENTRIES = [("lu_machinery/dispatch.py",), ("-m", "lu_machinery.dispatcher.cli")]


def linked_entry(env, tree, *command):
    return subprocess.run([sys.executable, *command], cwd=tree, env=env, text=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("entry", ENTRIES)
def test_linked_worktree_entry_validates_the_primary_and_stays_in_place(sandbox, entry):
    """Both entry points refuse without a configured primary and otherwise run this tree's dispatcher."""
    tree = prior_worktree(sandbox, "older-branch")
    primary_entry = sandbox.repo.resolve() / "lu_machinery/dispatch.py"
    primary_entry.write_text("import sys\nprint('primary checkout entry ran')\nsys.exit(3)\n")

    ran = linked_entry(sandbox.env, tree, *entry, "list", "--status", "running")
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "[]"
    assert "primary checkout entry ran" not in ran.stdout

    unanchored = {key: value for key, value in sandbox.env.items() if key != "LEARN_UKRAINIAN_PRIMARY_REPO_ROOT"}
    refused = linked_entry(unanchored, tree, *entry, "list", "--status", "running")
    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert refused.stderr.startswith("dispatcher: no shared-state anchor is configured")
    assert refused.stdout == ""
