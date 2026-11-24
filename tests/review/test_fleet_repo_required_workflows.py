"""Every registered repository's required CI workflows are ones its pull requests and merge queue can use."""

from __future__ import annotations

from pathlib import Path

import yaml

from lu_machinery.review import record_cf_verdict as recorder
from lu_machinery.review import repair_round

FIXTURE = Path(__file__).parent / "fixtures" / "fleet_repo_workflows.yaml"


def test_required_workflows_name_only_workflows_the_repository_runs_on_pull_requests() -> None:
    """Defect classes: a fleet_repos.yaml row whose required_workflow, or the ci.yml default, names a workflow that
    never runs on that repository's pull requests, so a repair round refuses every real CI failure there; and a
    merge-queue repository naming several workflows, which merge closeout refuses."""
    recorded = yaml.safe_load(FIXTURE.read_text())["repos"]
    registered = recorder.load_fleet_repos()
    # A newly registered repository has no recorded workflows until someone reads them from GitHub.
    assert set(recorded) == set(registered)
    required = {key: repair_round.required_workflows(repo.github) for key, repo in registered.items()}
    missing = {
        key: sorted(set(names) - set(recorded[key]["pull_request_workflows"])) for key, names in required.items()
    }
    assert {key: names for key, names in missing.items() if names} == {}
    several = {key: names for key, names in required.items() if recorded[key]["merge_queue"] and len(names) != 1}
    assert several == {}
