"""Model value statistics from durable summary rows, landing trailers and allowance readings (#581)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest

from lu_machinery.agent_runtime import model_pause
from lu_machinery.api import model_stats_router as stats
from lu_machinery.api import model_stats_rows as rows
from lu_machinery.api import planning
from lu_machinery.api.delegate_router import active_snapshot
from lu_machinery.api.monitor_context import fixture_context, get_ctx
from lu_machinery.api.state_helpers import cache_get_or_compute, cache_invalidate, ctx_scoped_ttl_key
from lu_machinery.common import git_credentials, github_client
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.dispatcher import store, worker
from lu_machinery.github_rest_cache import RestPage, RestResult
from lu_machinery.orchestration.fleet_repos import FleetRepo

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=7)


@pytest.mark.parametrize("recorded", ["fixture/machinery", "moved/machinery"])
def test_model_stats_binds_both_recorded_slugs_to_the_public_row(monkeypatch, recorded):
    """Defect class: model statistics drop public review records anchored after a repository transfer."""
    row = FleetRepo(
        "machinery", "fixture/machinery", "machinery", "public-machinery",
        alternate_github="moved/machinery", repository_id=42, visibility="public",
    )
    repos = {row.key: row}
    monkeypatch.setattr(rows, "load_fleet_repos", lambda: repos)
    rows._fleet_repo_visibility.cache_clear()
    try:
        assert rows.repository_keys(repos)[recorded] == "machinery"
        assert rows._record_allows_sample({"repository": recorded, "repository_id": "fixture/machinery"})
    finally:
        rows._fleet_repo_visibility.cache_clear()


def test_planning_and_model_stats_reject_non_ascii_repository_spellings(monkeypatch):
    """Case conversion must not admit a repository look-alike as worker or public review evidence."""
    registered = "kit/machinery"
    lookalike = "\N{KELVIN SIGN}it/machinery"
    alternate = "moved/machinery"
    repo = FleetRepo(
        "machinery", registered, "machinery", "public-machinery",
        alternate_github=alternate, repository_id=42, visibility="public",
    )
    repos = {repo.key: repo}
    monkeypatch.setattr(rows, "load_fleet_repos", lambda: repos)
    claims = {"planning_claims": [(lookalike, ["src/work.py"])]}
    monkeypatch.setattr(planning, "active_snapshot", lambda _ctx: SimpleNamespace(read=lambda: (claims, 0)))
    aliases = {repo.key: alternate, registered: alternate, alternate: alternate}
    assert planning._workers(None, aliases) is None

    rows._fleet_repo_visibility.cache_clear()
    try:
        assert not rows._record_allows_sample({"repository": lookalike})
    finally:
        rows._fleet_repo_visibility.cache_clear()

    keys = rows.repository_keys(repos)
    raw = {"task_id": "review", "role": "review", "pinned_head": "a" * 40, "repo": repo.key}
    entry = {"head": raw["pinned_head"], "repository": lookalike, "pr": 42}
    assert stats._publication(raw, {"review": [entry]}, keys) is None
    invalid_catalog = {"other": FleetRepo("other", lookalike, "other", "public-machinery")}
    assert rows.repository_keys(invalid_catalog) == {}


def test_model_value_sections_explain_how_to_read_the_figures():
    """A dashboard section can keep showing numbers after losing the nearby reading guidance."""
    html = machinery_path("api/dashboards/model-stats.html").read_text()
    sections = re.findall(r"<section\b[^>]*>.*?</section>", html, re.DOTALL)
    explanations = {
        "Window summary": ("Landed share divides those merges by all write tasks", "Low sample means"),
        "Token use and waste": ("Codex fresh input is input minus cache reads", "Cache hit is cached input divided by fresh plus cached input"),
        "Value frontier": ("first-round approval with recorded hours per landed task", "Look toward the upper left"),
        "Best model per job": ("Compare landed share within each job class", "A star marks the highest positive share"),
        "Where the work went": ("how many write tasks settled", "A larger Done share"),
        "Week by week": ("Higher points mean a larger share of that UTC week's write tasks landed", "Use full weeks with enough tasks"),
        "Neon medium against high": ("write landings, reviewer approvals and typical runtime", "Use the counts"),
        "Reviewer strictness": ("points farther right report more P0–P2 findings", "Compare similar assignments"),
        "Time to finish": ("box spans the middle half of timed writes", "Shorter times are better"),
        "Ukrainian tasks": ("report merged writes and how often reviewers approved", "Prefer a higher landed share"),
        "Pool allowance": ("remaining allowance and reset time", "Cool means more headroom", "Lower points per landed task"),
        "Author or reviewer?": ("approved first time", "Read down one reviewer column"),
        "Write tasks": ("includes settled writes", "higher landed share"),
        "Never started": ("refused before execution by cause and role", "Fewer refusals"),
        "Reviews": ("includes settled reviews", "fewer unreadable or unfinished results"),
        "Compare reviewer levels": ("readable results, first-round approval", "lower time or allowance cost"),
        "Reviewed PRs": ("reviewed PRs for the shown authors", "higher first-round approval and fewer rounds"),
        "Reviewer accuracy": ("counts as fixed when the author changed its file", "check the settled count"),
        "Authors by risk tier": ("risk tier of each PR's first published review", "Compare authors within one tier"),
    }
    for section, phrases in explanations.items():
        rendered = next(fragment for fragment in sections if f"<h2>{section}" in fragment
                        or f'aria-label="{section}"' in fragment)
        for phrase in phrases:
            assert phrase in rendered, (section, phrase)


def _record(tasks: Path, task_id: str, **fields) -> dict:
    record = {"schema_version": 2, "task_id": task_id, "agent": "codex", "model": "gpt-6.1-sol", "effort": "high",
              "mode": "workspace-write", "status": "done", "started_at": (NOW - timedelta(hours=2)).isoformat(),
              "finished_at": (NOW - timedelta(hours=1)).isoformat(), **fields}
    (tasks / f"{task_id}.json").write_text(json.dumps(record))
    return record


@pytest.mark.parametrize("machinery", ["sample/machinery", "new-owner/machinery"])
def test_planning_ranks_product_blocker_and_withholds_colliding_cards(tmp_path, monkeypatch, machinery):
    """Diamond dependencies count once, and a ready issue cannot overlap a live grant."""
    product, historical_machinery, private = "sample/product", "sample/machinery", "sample/private.dev"
    repos = {
        product: FleetRepo(product, product, "product", "public-product"),
        "machinery": FleetRepo(
            "machinery", historical_machinery, "machinery", "public-machinery",
            alternate_github="new-owner/machinery", repository_id=42, visibility="public",
        ),
        private: FleetRepo(private, private, "private.dev", "private-infra"),
    }
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"machinery": machinery}, "d" * 64),
    )
    title = "Product: remove the classified in-tree machinery copy"
    product_ref = f"{product}#13032"

    def issue(value, name, deps, scope="src/later.py"):
        return {"number": value, "title": name, "state": "OPEN",
                "body": f"## In scope\n{scope}\n\n## Dependencies\n{deps}\n"}

    ambiguous_scopes = ["N/A", "CI/CD", machinery, private, "github.com/docs/readme.md"]
    inventories = {
        product: [issue(13032, title, "None", "src/shared.py")],
        machinery: [
            issue(503, "Redirect runtime callers", title),
            issue(505, "Remove duplicate API", product_ref),
            issue(519, "Remove mirrored readers", f"{historical_machinery}#503; #505"),
            issue(525, "Run issue checks", "#519"),
            issue(584, "Ready card with collision", "#581",
                  f"- src/elsewhere.py, **`{historical_machinery}/src/owned/worker.py:12`**"),
            issue(585, "Ready card on a sibling prefix", "#581", "src/owned-extra/file.py"),
            issue(586, "Unknown dependency", "#404", "docs/guide.md"),
            issue(587, "Scope is in prose", "None", "Refine the reader, use `owned_paths`, e.g. Ruff 0.15.21."),
            issue(588, "Unresolved cycle", "#589"),
            issue(589, "Unresolved cycle partner", "#588"),
            issue(590, "Blocked by a private issue", f"{private}#999"),
            issue(591, "Ready card with partial scope", "None", "- src/elsewhere.py\n- Its callers and tests"),
            *(issue(number, "Ready card with a slash in prose", "None", scope)
              for number, scope in enumerate(ambiguous_scopes, start=592)),
        ],
        private: [issue(999, "Private work", product_ref)],
    }
    monkeypatch.setattr(planning, "load_fleet_repos", lambda: repos)
    monkeypatch.setattr(planning.github, "list_open_issues",
                        lambda repo, **_: RestPage(inventories[repo], truncated=False))
    monkeypatch.setattr(planning.github, "issue_states", lambda repo, numbers, **_: {
        issue: "closed" for issue in numbers if (repo, issue) in {(machinery, 581), (product, 13032)}})

    def search(endpoint, **_):
        terms = parse_qs(urlsplit(endpoint).query)["q"][0]
        candidates = [{"title": title, "repository_url": f"https://api.github.com/repos/{product}", "number": 13032}
                      ] if title in terms else []
        return RestResult(200, {"items": candidates, "total_count": len(candidates), "incomplete_results": False},
                          None, None, False)

    monkeypatch.setattr(planning.github, "shared_cache", lambda: SimpleNamespace(get_json=search))
    context = fixture_context(tmp_path)
    tasks = context.roots.batch_state_dir / "tasks"
    tasks.mkdir(parents=True)
    _record(tasks, "live-writer", status="spawning", repository=historical_machinery,
            owned_paths=["src/shared.py"], run_nonce="run",
            owned_path_grants=[{"grant_id": "grant", "task_id": "live-writer", "run_nonce": "run",
                                "owned_paths": ["src/owned"]}])
    _record(tasks, "observer", status="spawning", mode="read-only", owned_paths=["src/owned-extra"])
    active_snapshot(context).refresh()
    monkeypatch.setattr(stats, "cached_snapshot", lambda _: None)
    app = FastAPI()
    app.state.ctx = context
    app.include_router(stats.router, prefix="/api/fleet/model-stats")

    key = ctx_scoped_ttl_key(context, "planning", "issues", context.roots)
    cache_get_or_compute(key, 300, planning.collect_issues)
    meter_key = ctx_scoped_ttl_key(context, "planning", "meters", context.roots)
    cache_get_or_compute(meter_key, 30, lambda: planning.MeterSnapshot({}, {}, {}))
    with TestClient(app) as client:
        response = client.get("/api/fleet/model-stats/planning")
        assert response.status_code == 200
        result = response.json()["issues"]
        assert result["coverage"]["repositories_read"] == result["coverage"]["repositories_total"] == 3
        assert result["coverage"]["open_issues"] == 19
        assert result["coverage"]["dependency_unknown"] == 1
        assert result["blockers"][0] == {"ref": product_ref, "direct": 3, "total": 6}
        assert {"ref": None, "direct": 1, "total": 1} in result["blockers"]
        assert result["blocked"][0] == {"ref": f"{machinery}#503", "blocked_by": [product_ref], "private_blockers": 0}
        assert result["ready_now"] == [{"ref": f"{machinery}#585"}, {"ref": product_ref}]
        assert result["coverage"]["path_conflicts"] == 1
        assert result["coverage"]["scope_unknown"] == 7
        assert {"ref": f"{machinery}#584", "path_status": "path_conflicts"} in result["ready"]
        for issue_number in range(591, 597):
            assert {"ref": f"{machinery}#{issue_number}", "path_status": "scope_unknown"} in result["ready"]
        assert private not in response.text
        assert "src/owned" not in response.text

        inventories[product] = []
        cache_invalidate(key)
        cache_get_or_compute(key, 300, planning.collect_issues)
        closed = client.get("/api/fleet/model-stats/planning").json()["issues"]
        assert all(row["ref"] != product_ref for row in closed["blockers"])
        assert {"ref": f"{machinery}#503"} in closed["ready_now"]
        assert {"ref": f"{machinery}#505"} in closed["ready_now"]


def test_planning_reads_a_registered_repository_prefix_as_that_repository():
    """Defect class: a registered owner/name prefix was a path in the issue's own repository.

    The planning view then missed a live worker in the named repository and marked the issue ready.
    """
    product, machinery, private = "sample/product", "sample/machinery", "sample/private.dev"
    work, tools = "oak/work", "oak/tools.dev"
    lookalike = "sample/ma\u212ainery/src/owned/file.py"
    folded = "oa\u212a/tools.dev"
    snap = planning.IssueSnapshot(
        frozenset({product, machinery, private, work, tools}),
        {"product": product, "machinery": machinery, "private": private, "work": work, "tools": tools},
        {
            (product, 1): planning.Issue("prefixed", f"## In scope\n{machinery}/src/owned/worker.py\n"),
            (product, 2): planning.Issue("plain", "## In scope\nsrc/owned/worker.py\n"),
            (product, 3): planning.Issue("explicit relative", f"## In scope\n./{machinery}/src/owned/worker.py\n"),
            (product, 4): planning.Issue("unregistered", "## In scope\nother/tree/src/file.py\n"),
            (product, 5): planning.Issue("dotted slug", f"## In scope\n{private}/docs/guide.md\n"),
            (product, 6): planning.Issue("case and letters", "## In scope\nSample/Machinery/файл.py\n"),
            (machinery, 7): planning.Issue("own prefix", f"## In scope\n{machinery}/src/owned/file.py\n"),
            (machinery, 8): planning.Issue("own plain", "## In scope\nsrc/elsewhere.py\n"),
            (product, 9): planning.Issue("lookalike", f"## In scope\n{lookalike}\n"),
            (product, 10): planning.Issue(
                "parent segment",
                f"## In scope\n{machinery}/docs/../src/owned/file.py\n{machinery}/src/owned/file.py\n",
            ),
            (product, 11): planning.Issue("dotted directory", f"## In scope\n{machinery}/release.v2/file.py\n"),
            (product, 12): planning.Issue("dotted subtree", f"## In scope\n{machinery}/release.v2/docs/**\n"),
            (product, 13): planning.Issue("repeated slash", f"## In scope\n{machinery}//src/owned/worker.py\n"),
            (product, 14): planning.Issue("folded remainder", f"## In scope\n{work}/{folded}\n"),
            (product, 15): planning.Issue("folded subtree", f"## In scope\n{work}/{folded}/**\n"),
            (product, 16): planning.Issue("ascii remainder", f"## In scope\n{work}/{tools}\n"),
            (product, 17): planning.Issue("bare slug", f"## In scope\n{tools}\n"),
        },
        {}, frozenset(), 5,
    )
    workers = {
        machinery: (planning.expand_claim("src/owned") + planning.expand_claim("файл.py")
                    + planning.expand_claim("release.v2/file.py") + planning.expand_claim("release.v2")),
        private: planning.expand_claim("docs"),
        product: planning.expand_claim(lookalike),
        work: planning.expand_claim(folded),
    }
    ready = {row["ref"]: row["path_status"] for row in planning.project_issues(snap, workers, stale=False)["ready"]}
    assert ready == {
        f"{product}#1": "path_conflicts",
        f"{product}#2": "ready_now",
        f"{product}#3": "ready_now",
        f"{product}#4": "ready_now",
        f"{product}#5": "path_conflicts",
        f"{product}#6": "path_conflicts",
        f"{machinery}#7": "path_conflicts",
        f"{machinery}#8": "ready_now",
        f"{product}#9": "path_conflicts",
        f"{product}#10": "scope_unknown",
        f"{product}#11": "path_conflicts",
        f"{product}#12": "path_conflicts",
        f"{product}#13": "scope_unknown",
        f"{product}#14": "path_conflicts",
        f"{product}#15": "path_conflicts",
        f"{product}#16": "scope_unknown",
        f"{product}#17": "scope_unknown",
    }
    withheld = planning.project_issues(snap, {machinery: None, private: [], product: []}, stale=False)
    assert {"ref": f"{product}#1", "path_status": "worker_scope_unknown"} in withheld["ready"]
    assert all(row["ref"] != f"{product}#1" for row in withheld["ready_now"])


def test_planning_forecast_matches_two_meter_readings(tmp_path, monkeypatch):
    """Recent burn comes from two observations of one reset window, independent of the reported average."""
    now = datetime.now(UTC)
    earlier = now - timedelta(hours=2, minutes=10)
    reset = now + timedelta(hours=25)

    def series(name, value, *, previous=False, pool=None):
        metric = {"__name__": name}
        if previous:
            metric["planning_sample"] = "previous"
        if pool is not None:
            metric["pool"] = pool
        return {"metric": metric, "value": [now.timestamp(), str(value)]}

    samples = []
    for previous, when, remaining in ((False, now, 20), (True, earlier, 21)):
        samples.extend([
            series("lu_cto_source_ok", 1, previous=previous),
            series("node_textfile_mtime_seconds", when.timestamp(), previous=previous),
            series("lu_cto_pool_left_percent", remaining, previous=previous, pool="claude-a"),
            series("lu_cto_pool_reset_days", round((reset - when).total_seconds() / 86400, 1),
                   previous=previous, pool="claude-a"),
        ])
    samples.extend([
        series("lu_cto_pool_burn_percent_per_hour", 0.9, pool="claude-a"),
        series("lu_cto_pool_sustainable_percent_per_hour", 0.8, pool="claude-a"),
    ])
    monkeypatch.setattr(planning, "_prometheus", lambda: samples)
    monkeypatch.setattr(planning.subscriptions, "subscriptions_snapshot", lambda **_: {"providers": [{
        "accounts": [{"account": "Claude a", "reading": {"status": "fresh"},
                      "weekly": {"status": "ok", "used_pct_exact": 80, "resets_at": reset.isoformat()}},
                     {"account": "Codex b", "reading": {"status": "fresh"},
                      "weekly": {"status": "ok", "used_pct_exact": 40, "resets_at": reset.isoformat()}}],
    }]})
    rates = {"claude:a": {"points_per_hour": 0.2, "tasks": 12}, "codex:b": {"points_per_hour": 0.35, "tasks": 4},
             "gemini": {"points_per_hour": 0.05, "tasks": 3}}
    monkeypatch.setattr(stats, "cached_snapshot", lambda _: ({"worker_allowance": rates}, 0.0))
    context = fixture_context(tmp_path)
    for name, value in (("issues", None), ("meters", planning.collect_meters())):
        key = ctx_scoped_ttl_key(context, "planning", name, context.roots)
        cache_get_or_compute(key, 300, lambda value=value: value)
    app = FastAPI()
    app.state.ctx = context
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    with TestClient(app) as client:
        data = client.get("/api/fleet/model-stats/planning").json()
    pools = {row["pool"]: row for row in data["capacity"]}
    assert set(pools) == {"claude:a", "claude:b", "codex:a", "codex:b", "agy:gemini", "agy:claude_gpt", "grok"}
    claude = pools["claude:a"]
    # 20 points left, 1 point used in 130 minutes, and 25 hours to reset.
    assert abs(claude["unused_pct"] - 8.46) < 0.1
    assert claude["burn_pct_per_hour"] == 0.462
    assert claude["reported_burn_pct_per_hour"] == 0.9
    assert claude["workers_needed"] == 2
    assert claude["worker_samples"] == 12
    assert pools["codex:b"]["remaining_pct"] == 60
    assert pools["codex:b"]["unused_pct"] is None

    meter_key = ctx_scoped_ttl_key(context, "planning", "meters", context.roots)
    for row in samples:
        if row["metric"] == {"__name__": "lu_cto_pool_left_percent", "planning_sample": "previous", "pool": "claude-a"}:
            row["value"][1] = "19"  # The remaining allowance rose between samples.
    cache_invalidate(meter_key)
    cache_get_or_compute(meter_key, 30, planning.collect_meters)
    with TestClient(app) as client:
        data = client.get("/api/fleet/model-stats/planning").json()
    assert next(row for row in data["capacity"] if row["pool"] == "claude:a")["unused_pct"] is None


def _review(tasks: Path, task_id: str, correctness: str, priorities: list[str], **fields) -> None:
    _record(tasks, task_id, **{"mode": "read-only", "agent": "claude", "model": "opus",
                               "resolved_model": "claude-opus-5-5", "resolved_model_known": True,
                               "review_profile": "code", "review_risk": "medium", **fields})
    findings = [{"id": f"F{i}", "priority": p, "body": f"finding body {i}"} for i, p in enumerate(priorities)]
    (tasks / f"{task_id}.result").write_text(json.dumps({"overall": {"correctness": correctness}, "findings": findings}))


def _state(tmp_path: Path) -> tuple[Path, Path]:
    tasks = tmp_path / "batch_state" / "tasks"
    tasks.mkdir(parents=True)
    return tmp_path / "batch_state", tasks


def _window(batch_state: Path, landing: stats.Landing, name: str = "7d") -> dict:
    return stats.collect(batch_state, batch_state.parent, NOW, landing=landing)["windows"][name]


def _row(payload: dict, model: str, role: str) -> dict:
    return next(r for r in payload["models"] if r["model"] == model and r["role"] == role)


def test_never_started_refusals_survive_backfill_and_keep_failed_review_counts_separate(tmp_path):
    """Pre-start refusals must stay out of failed reviews and runtime observations (#889)."""
    batch_state, tasks = _state(tmp_path)
    boundary = {"returncode": 125, "error": "write_boundary_refused: local diagnostic", "provider_pgid": 321}
    cases = {
        "account": {"never_started_reason": "account_check", "duration_s": 0},
        "capacity": {"never_started_reason": "admission_capacity"},
        "eligibility": {"never_started_reason": "route_eligibility"},
        "headroom": {"status": "rate_limited", "never_started_reason": "headroom"},
        "placement": {"returncode_reason": "dispatch_fallback_refused"},
        "setup": {"never_started_reason": "setup_refusal"},
        "review-setup": {"never_started_reason": "review_setup_refusal"},
        "model-selection": {"error": "agy_model_unavailable: unavailable"},
        "boundary-refused": {**boundary, "write_boundary": {"status": "refused"}, "duration_s": 99},
        "boundary-continuation": {**boundary, "write_boundary": {"status": "refused"}, "never_started_reason": None},
        "boundary-interrupted": {**boundary, "write_boundary": {"status": "interrupted"}},
        "boundary-missing": boundary,
        "model-ran": {"never_started_reason": "setup_refusal", "duration_s": 12,
                      "usage": {"input_tokens": 7, "cached_input_tokens": 0}},
    }
    for task_id, fields in cases.items():
        record = _record(tasks, task_id, **{"mode": "read-only", "review_profile": "code", "status": "failed", **fields})
        path = tasks / f"{task_id}.json"
        old_row = rows.summary_row(path, record, path.stat().st_mtime_ns)
        assert old_row is not None
        old_row["status"] = record["status"]
        del old_row["never_started_reason"]
        rows.append_row(batch_state, old_row)
    assert rows.backfill(tasks, batch_state)["refreshed"] == 9
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0
    payload = _row(_window(batch_state, stats.Landing()), "gpt-6.1-sol", "review")
    assert payload["status"]["never_started"] == 9
    assert payload["status"]["failed"] == payload["verdicts"]["unparsed"] == 4
    assert payload["wall_seconds"]["n"] == 1
    assert payload["wall_seconds"]["total_seconds"] == payload["wall_seconds"]["median"] == 12
    assert payload["never_started_reasons"] == {
        "account_check": 1, "admission_capacity": 1, "boundary_refusal": 1, "headroom": 1,
        "model_unavailable": 1, "review_setup_refusal": 1, "route_eligibility": 1,
        "setup_refusal": 1, "worker_placement": 1,
    }
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["boundary-refused"]["tokens"] == {"fresh_input": 0, "cached_input": 0, "output": 0}
    assert "local diagnostic" not in rows.rows_path(batch_state).read_text()


def test_per_model_figures_survive_settlement_backfill_and_retention(tmp_path, monkeypatch):
    """Defect class: wrong aggregation, or a summary row lost, stale, corrupted or dropped by record retention."""
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    # Codex output_tokens already include the reasoning tokens.
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100, "reasoning_output_tokens": 50}
    # Settled through the dispatcher store: the row is appended after the record's write.
    settled = _record(tasks, "w1", duration_s=600, usage=usage, research_task_family="code",
                      research_role="implementation")
    store.replace(settled, tasks / "w1.json")
    # A later terminal rewrite whose row write fails leaves the record as written and an older row stored.
    rewritten = _record(tasks, "w3", duration_s=1800, usage=usage)
    store.replace(rewritten, tasks / "w3.json")
    time.sleep(0.05)  # a later record version gets a later modification time
    monkeypatch.setattr(rows, "append_row", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    store.replace({**rewritten, "status": "no_deliverable"}, tasks / "w3.json")
    assert json.loads((tasks / "w3.json").read_text())["status"] == "no_deliverable"
    monkeypatch.undo()
    with rows.rows_path(batch_state).open("ab") as stream:
        stream.write(b'{"schema": "model-stats-row.v1", "task_id": "torn')  # an append cut short
    (tasks / "archive").mkdir()
    _record(tasks / "archive", "w2", duration_s=1200, usage=usage, research_task_family="code")
    # A legacy record's id is not checked on read: one that is no dispatcher task id is never stored.
    (tasks / "archive" / "legacy.json").write_text(json.dumps(
        {"schema_version": 1, "task_id": "/srv/private/model", "agent": "codex", "status": "done"}))
    # The next settlement closes the torn line first, so only the fragment is lost.
    store.replace(_record(tasks, "old", finished_at=(SINCE - timedelta(days=40)).isoformat(), duration_s=99),
                  tasks / "old.json")
    _record(tasks, "live", status="running")
    head1, head2 = "1" * 40, "2" * 40
    _review(tasks, "r1-review", "incorrect", ["P0", "P3"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head1, duration_s=300, started_at=(NOW - timedelta(hours=3)).isoformat())
    _review(tasks, "r2-review", "correct", ["P1", "P2"], pr=7, review_author_model="gpt-6.1-sol",
            pinned_head=head2, duration_s=500)
    reply = (tasks / "r2-review.result").read_text()
    (tasks / "r2-review.result").write_text(f"```json\n{reply}\n```\n")  # a fenced reply the contract accepts
    _review(tasks, "r3-review", "incorrect", [])
    (tasks / "r3-review.result").write_text('{"overall": {"correctness": "incorrect"}, "findings": 1}')

    first = rows.backfill(tasks, batch_state)
    again = rows.backfill(tasks, batch_state)
    shutil.rmtree(tasks)  # retention removes every task record; the rows remain
    landing = stats.Landing(task_ids={"w1", "r2-review"})
    landing.repositories["read"] = 1
    landing.prs["public", "7"] = stats._MergedPR(head2, NOW - timedelta(days=2), {"w1"}, set(), set())
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    payload = snapshot["windows"]["7d"]

    assert (first["added"], first["refreshed"], first["already_present"]) == (4, 1, 2)
    assert first["invalid_task_id"] == 1 and "/srv/private/model" not in rows.rows_path(batch_state).read_text()
    assert (again["added"], again["refreshed"]) == (0, 0)
    assert rows.read_rows(batch_state)[1]["malformed_lines"] == 1
    write = _row(payload, "gpt-6.1-sol", "write")
    assert write["tasks"] == 3 and write["low_sample"] is True
    assert write["status"]["done"] == 2 and write["status"]["no_deliverable"] == 1
    assert write["landed"] == 1 and write["landed_rate"] == {"value": 0.333, "n": 3, "low_sample": True}
    assert write["wall_seconds"] == {"p25": 600.0, "median": 1200.0, "p75": 1800.0, "p90": 1800.0,
                                     "total_hours": 1.0, "total_seconds": 3600.0, "n": 3}
    assert write["wall_hours_per_landed"]["value"] == 1.0
    assert write["tokens"] == {"fresh_input": 1800, "cached_input": 1200, "output": 300}
    assert write["output_tokens_per_landed"]["value"] == 300
    assert [(c["class"], c["tasks"], c["landed"]) for c in write["by_class"]] == [("code", 2, 1), ("unclassified", 1, 0)]
    token_use = payload["token_use"]
    assert (token_use["tasks"], token_use["reported_tasks"], token_use["merged_prs"]) == (6, 3, 1)
    assert token_use["totals"] == {"fresh_input": 1800, "cached_input": 1200, "output": 300}
    assert token_use["per_merged_pr"] == 3300
    assert snapshot["windows"]["24h"]["token_use"]["per_merged_pr"] is None
    assert token_use["by_work"]["implementation"]["tokens"] == 1100
    assert token_use["by_work"]["landing_helpers"] is None
    assert token_use["waste"]["failed_or_empty"]["share"] == 1 / 3
    assert token_use["waste"]["capped_and_replaced"] is token_use["avoidable_share"] is None
    assert token_use["cache_by_model"] == [
        {"model": "claude-opus-5-5", "tasks": 3, "reported_tasks": 0, "ratio": None},
        {"model": "gpt-6.1-sol", "tasks": 3, "reported_tasks": 3, "ratio": 0.4},
    ]
    assert [r["status"] for r in token_use["top_tasks"]] == ["no_deliverable", "done", "done"]
    ctx = fixture_context(tmp_path)
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx, **_scope: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))
    for window in ("7d", "24h"):
        labels = {"window": window, "model": "gpt-6.1-sol", "work_type": "implementation"}
        assert registry.get_sample_value("lu_tokens_fresh_input", labels) == 600
        assert registry.get_sample_value("lu_tokens_cached_input", labels) == 400
        assert registry.get_sample_value("lu_tokens_output", labels) == 100
        assert registry.get_sample_value("lu_tokens_per_merged_pr", labels) == (1100 if window == "7d" else None)
    assert registry.get_sample_value("lu_token_data_age_seconds") == 1
    assert registry.get_sample_value("lu_tokens_waste", {
        "window": "7d", "model": "gpt-6.1-sol", "waste_class": "failed_or_empty",
    }) == 1100
    assert registry.get_sample_value("lu_tokens_waste", {
        "window": "7d", "model": "gpt-6.1-sol", "waste_class": "capped_and_replaced",
    }) is None
    landing.repositories["unavailable"] = 1
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    partial = snapshot["page_windows"]["7d"]["token_use"]
    assert partial["totals"] == token_use["totals"]
    assert partial["merged_prs"] is partial["per_merged_pr"] is None
    assert partial["by_work"]["implementation"]["per_merged_pr"] is None
    labels = {"window": "7d", "model": "gpt-6.1-sol", "work_type": "implementation"}
    assert registry.get_sample_value("lu_tokens_per_merged_pr", labels) is None
    assert registry.get_sample_value("lu_tokens_fresh_input", labels) == 600

    review = _row(payload, "claude-opus-5-5", "review")
    assert review["tasks"] == 3 and "landed" not in review
    # r3's malformed reply is visible in the missing-verdict count, despite having no PR.
    assert review["verdicts"] == {"correct": 1, "incorrect": 1, "uncertain": 0, "unparsed": 1}
    assert review["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 0, "bad_fields": 1, "not_done": 0, "unknown": 0,
    }
    assert review["unparsed_samples"] == {"bad_fields": '{"overall": {"correctness": "incorrect"}, "findings": 1}'}
    assert review["blocking_per_review"]["value"] == 1.0 and review["p0_p2_per_review"]["value"] == 1.5
    assert review["by_class"][0]["class"] == "code/medium"

    (author,) = payload["authors"]
    assert author["model"] == "gpt-6.1-sol" and author["prs"] == 1
    assert author["rounds_per_pr"]["value"] == 2.0
    assert author["approved_eventually"]["value"] == 1.0
    assert author["first_round_approved"]["value"] == 0.0
    assert author["blocking_per_pr"]["value"] == 2.0 and author["p0_p2_per_pr"]["value"] == 3.0
    assert payload["records"] == 6
    assert _window(batch_state, stats.Landing(), "all")["records"] == 7

    # Split of unparsed reasons, the earliest escaped 200-char sample per model (model total only), archive
    # binding, the upgrade of a legacy row and fail-closed row validation. Sidecar outcomes: the table test below.
    u_state, u_tasks = _state(tmp_path / "unparsed_causes")
    # Conflicting unstamped result_file declaration must never attach another task's sidecar.
    (u_tasks / "other-secret.result").write_text("OTHER-TASK-REPLY-NEVER-SHOWN", encoding="utf-8")
    _record(u_tasks, "u-conflict-sidecar-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", started_at=_at(5.05), result_file=str(u_tasks / "other-secret.result"))
    # Later bad_format reply sits on a different effort and risk class: must not leak via by_effort or by_class.
    _record(u_tasks, "u-fmt-later-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            effort="medium", review_profile="infra", review_risk="high", started_at=_at(2))
    (u_tasks / "u-fmt-later-review.result").write_text("later-effort-reply", encoding="utf-8")
    _record(u_tasks, "u-fmt-first-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            effort="high", review_profile="code", review_risk="medium", started_at=_at(4))
    (u_tasks / "u-fmt-first-review.result").write_text(
        "first\n\t\x1b[31m\u202e<script>" + "B" * 240 + "TAIL-BEYOND-200", encoding="utf-8",
    )
    _record(u_tasks, "u-fields-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_branch="pinned-recon", research_task_family="recon", started_at=_at(3), duration_s=11,
            usage={"input_tokens": 10, "cache_read_input_tokens": 4, "output_tokens": 3})
    (u_tasks / "u-fields-review.result").write_text(
        '{"overall": {"correctness": "maybe"}, "findings": []}', encoding="utf-8",
    )
    _record(u_tasks, "u-envelope-fields-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            options={"review_profile": "code"}, started_at=_at(2.5))
    (u_tasks / "u-envelope-fields-review.result").write_text(
        'VERDICT: APPROVE\nSCORE: 9/10\n\n```json\n{"schema_version": "code-review-findings.v1", "overall": {}}\n```',
        encoding="utf-8",
    )
    # Valid JSON with a non-object root (bare or inside a valid envelope) classifies as bad_fields.
    assert rows.review_summary(b"[]")["unparsed_reason"] == "bad_fields"
    assert rows.review_summary(b'{"overall": {"correctness": "correct"}, "findings": [null]}')["unparsed_reason"] == (
        "bad_fields")
    assert rows.review_summary(b"VERDICT: APPROVE\nSCORE: 9/10\n\n```json\n[]\n```")["unparsed_reason"] == "bad_fields"
    # Stamped archive collision: renamed record pairs with its stamped sidecar, never an older unstamped basename.
    (u_tasks / "archive").mkdir()
    (u_tasks / "archive" / "u-stamped-review.result").write_text("WRONG-UNSTAMPED-ARCHIVE", encoding="utf-8")
    stamped_bytes = b"stamped archive reply"
    (u_tasks / "archive" / "u-stamped-review.20261115T050000Z.archived.result").write_bytes(stamped_bytes)
    (u_tasks / "archive" / "u-stamped-review.20261115T050000Z.archived.json").write_text(json.dumps({
        "schema_version": 2, "task_id": "u-stamped-review", "agent": "claude", "model": "claude-opus-5-5",
        "effort": "high", "mode": "read-only", "status": "done", "started_at": _at(4.5), "finished_at": _at(1),
        "review_profile": "code",
        "result_file": str(u_tasks / "u-stamped-review.result"),
        "result_sha256": hashlib.sha256(stamped_bytes).hexdigest(),
    }), encoding="utf-8")
    _record(u_tasks, "u-failed-review", mode="workspace-write", agent="claude", model="claude-opus-5-5",
            status="failed", started_at=_at(1), duration_s=5,
            usage={"input_tokens": 7, "cache_read_input_tokens": 1, "output_tokens": 2})
    # Seed a legacy pre-change unparsed row without unparsed_reason; reader marks it unknown, backfill upgrades it.
    legacy_path = u_tasks / "u-legacy-review.json"
    legacy_rec = _record(u_tasks, "u-legacy-review", mode="read-only", agent="claude", model="claude-opus-5-5",
                         review_profile="code", started_at=_at(1.5), duration_s=100)
    (u_tasks / "u-legacy-review.result").write_text("legacy prose reply", encoding="utf-8")
    legacy_row = rows.summary_row(legacy_path, legacy_rec, os.stat(legacy_path).st_mtime_ns)
    assert legacy_row is not None
    legacy_row["review"] = {"verdict": "unparsed", "priorities": None}
    rows.append_row(u_state, legacy_row)
    before_backfill = _row(_window(u_state, stats.Landing()), "claude-opus-5-5", "review")
    assert before_backfill["verdicts"]["unparsed"] == 1
    assert before_backfill["unparsed_reasons"]["unknown"] == 1 and before_backfill["unparsed_samples"] == {}

    bf1 = rows.backfill(u_tasks, u_state)
    assert (bf1["added"], bf1["refreshed"]) == (7, 1)
    assert rows.backfill(u_tasks, u_state)["refreshed"] == 0

    stored_u = {row["task_id"]: row for row in rows.read_rows(u_state)[0]}
    assert stored_u["u-legacy-review"]["review"] == {
        "verdict": "unparsed", "priorities": None,
        "unparsed_reason": "bad_format", "unparsed_sample": "legacy prose reply",
    }
    assert stored_u["u-stamped-review"]["review"]["unparsed_sample"] == "stamped archive reply"
    assert stored_u["u-conflict-sidecar-review"]["review"] == {
        "verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": None,
    }

    u_window = _window(u_state, stats.Landing())
    u_review = _row(u_window, "claude-opus-5-5", "review")
    assert u_review["tasks"] == 6 and u_review["verdicts"]["unparsed"] == 6
    assert u_review["review_evidence_absent"] == {}
    assert u_review["tokens"] == {"fresh_input": 0, "cached_input": 0, "output": 0}
    # The recon and the writer whose id merely contains "review" keep their tokens on the write row.
    assert stored_u["u-fields-review"]["role"] == "write" and stored_u["u-fields-review"]["class"] == "recon"
    assert stored_u["u-fields-review"]["review"] is None
    assert "review_evidence_absent" not in stored_u["u-fields-review"]
    assert stored_u["u-failed-review"]["role"] == "write" and stored_u["u-failed-review"]["review"] is None
    assert "review_evidence_absent" not in stored_u["u-failed-review"]
    u_write = _row(u_window, "claude-opus-5-5", "write")
    assert u_write["tasks"] == 2
    assert u_write["status"]["done"] == 1 and u_write["status"]["failed"] == 1
    assert u_write["tokens"] == {"fresh_input": 17, "cached_input": 5, "output": 5}
    totals = [0, 0, 0]
    for model in u_window["models"]:
        for index, key in enumerate(("fresh_input", "cached_input", "output")):
            totals[index] += model["tokens"][key]
    assert totals == [17, 5, 5]
    assert u_review["unparsed_reasons"] == {
        "missing_result": 1, "too_big": 0, "bad_format": 4, "bad_fields": 1, "not_done": 0, "unknown": 0,
    }
    assert set(u_review["unparsed_samples"]) == {"bad_format", "bad_fields"}
    # Samples are emitted only at model total, never on by_effort or by_class subgroups.
    assert u_review["unparsed_samples"]["bad_format"] == "stamped archive reply"
    assert all("unparsed_samples" not in item for item in (*u_review["by_effort"], *u_review["by_class"]))
    assert all("unparsed_samples" not in c for e in u_review["by_effort"] for c in e["by_class"])
    assert "later-effort-reply" not in json.dumps(u_window)
    assert "WRONG-UNSTAMPED-ARCHIVE" not in json.dumps(u_window)
    assert "OTHER-TASK-REPLY-NEVER-SHOWN" not in json.dumps(u_window)
    fmt_stored = stored_u["u-fmt-first-review"]["review"]["unparsed_sample"]
    assert fmt_stored.startswith(r"first\n\t\x1b[31m\u202e<script>B")
    assert "TAIL-BEYOND-200" not in fmt_stored and len(fmt_stored) <= rows.MAX_SAMPLE_CHARS
    assert u_review["unparsed_samples"]["bad_fields"].startswith(r"VERDICT: APPROVE\nSCORE: 9/10")
    page = stats.page_window(u_window)
    page_u = _row(page, "claude-opus-5-5", "review")
    assert page_u["unparsed_reasons"] == u_review["unparsed_reasons"]
    assert page_u["review_evidence_absent"] == u_review["review_evidence_absent"]
    assert "unparsed_samples" not in page_u
    assert "stamped archive reply" not in json.dumps(page)
    assert all("unparsed_samples" not in e for e in page_u["by_effort"])
    # Fail-closed cross-field validation rejects writer-impossible rows.
    base_row = stored_u["u-legacy-review"]
    bad_rows = [
        {**base_row, "role": "write"},
        {**base_row, "status": "failed"},
        {**base_row, "repo": "infra-private"},
        {**base_row, "review": {"verdict": "unparsed", "priorities": {"P0": 1}, "unparsed_reason": "bad_format", "unparsed_sample": "x"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bogus", "unparsed_sample": None}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": "x"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "x" * 201}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "raw\nline"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "bidi\u202e"}},
        {**base_row, "review": {"verdict": "unparsed", "priorities": None, "unparsed_reason": "bad_format", "unparsed_sample": "/home/ops/secret"}},
        {**base_row, "data_class": "private-code"},
        {**base_row, "data_class": ["private-code"]},
        {**base_row, "review": {"verdict": "correct", "priorities": {}, "unparsed_reason": "bad_format", "unparsed_sample": None}},
    ]
    assert all(not rows._well_formed(bad) for bad in bad_rows)


def test_each_sidecar_outcome_yields_one_review_and_never_carries_older_evidence(tmp_path, monkeypatch):
    """Defect class: retained review evidence outliving the current sidecar, so a verdict or reply sample read
    from an earlier sidecar survives a sidecar that is now absent, a link, a special file, unreadable or replaced."""
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setattr(rows, "MAX_RESULT_BYTES", 128)
    monkeypatch.setattr(rows, "MAX_SAMPLE_SCAN_BYTES", 64)
    body, big, long_prose = b"prose reply, not JSON", b"BIG\n" + b"A" * 156, b"long prose " * 9
    gone = {"verdict": "unparsed", "priorities": None, "unparsed_reason": "missing_result", "unparsed_sample": None}

    def sampled(reason: str, sample: str | None) -> dict:
        return {"verdict": "unparsed", "priorities": None, "unparsed_reason": reason, "unparsed_sample": sample}

    def unreadable(path: Path) -> None:
        path.write_bytes(body)
        path.chmod(0)

    def link(path: Path) -> None:
        path.with_name(f"{path.name}.target").write_bytes(body)
        path.symlink_to(f"{path.name}.target")

    parsed = json.dumps({"overall": {"correctness": "incorrect"}, "findings": [{"id": "F1", "priority": "P1"}]})
    # outcome: (what replaces the first readable sidecar, record fields, the one review that outcome yields)
    table = {
        "absent": (lambda p: None, {}, gone),
        "absent-recorded-verdict": (lambda p: None, {"verdict": "CHANGES_REQUESTED"},
                                    {"verdict": "incorrect", "priorities": None}),
        "regular": (lambda p: p.write_bytes(b"another prose reply"), {}, sampled("bad_format", "another prose reply")),
        "regular-parsed": (lambda p: p.write_text(parsed), {}, {"verdict": "incorrect", "priorities": {"P1": 1}}),
        "symlink": (link, {}, gone),
        "directory": (lambda p: p.mkdir(), {}, gone),
        "socket": (lambda p: os.mknod(p, stat.S_IFSOCK | 0o600), {}, gone),
        "fifo": (os.mkfifo, {}, gone),
        "unreadable": (unreadable, {}, gone),
        "replaced-bytes": (lambda p: p.write_bytes(b"replacement"), {"result_sha256": hashlib.sha256(body).hexdigest()},
                           gone),
        "over-scan-budget": (lambda p: p.write_bytes(long_prose), {}, sampled("bad_format", None)),
        "too-big": (lambda p: p.write_bytes(big), {}, sampled("too_big", None)),
        "too-big-replaced": (lambda p: p.write_bytes(big), {"result_sha256": hashlib.sha256(body).hexdigest()},
                             sampled("too_big", None)),
    }
    for outcome, (_, fields, expected) in table.items():
        # An unparsed sidecar is a review only when the task is typed. A parsed reply is the evidence.
        typed = {} if expected.get("verdict") in ("correct", "incorrect", "uncertain") else {"review_profile": "code"}
        _record(tasks, f"{outcome}-review", mode="read-only", **typed, **fields)
        (tasks / f"{outcome}-review.result").write_bytes(body)
    rows.backfill(tasks, batch_state)
    earlier = {row["task_id"]: row["review"] for row in rows.read_rows(batch_state)[0]}
    assert earlier["absent-review"] == sampled("bad_format", "prose reply, not JSON")  # evidence that could be kept
    for outcome, (replace, _, _) in table.items():
        sidecar = tasks / f"{outcome}-review.result"
        sidecar.unlink()
        replace(sidecar)
    # The same record version, then a rewritten one: both show only what the current sidecar proves.
    rows.backfill(tasks, batch_state)
    same_version = {row["task_id"]: row["review"] for row in rows.read_rows(batch_state)[0]}
    for outcome in table:
        record = json.loads((tasks / f"{outcome}-review.json").read_text())
        store.replace({**record, "duration_s": 7}, tasks / f"{outcome}-review.json")
    rewritten = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    for outcome, (_, _, expected) in table.items():
        assert (outcome, same_version[f"{outcome}-review"]) == (outcome, expected)
        assert (outcome, rewritten[f"{outcome}-review"]["review"]) == (outcome, expected)
        assert rewritten[f"{outcome}-review"]["duration_s"] == 7.0
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _git_fixture(tmp_path, monkeypatch) -> Path:
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main", "--template=")
    return repo


def test_landing_joins_only_exact_x_agent_trailer_lines(tmp_path, monkeypatch):
    """Defect class: false landing attribution from prose, quotes, code fences, suffixed values or another branch."""
    repo = _git_fixture(tmp_path, monkeypatch)
    messages = [
        # A squash merge appends its own paragraph after the trailer: the line still counts.
        "feat: real (#1)\n\nRefs #9\n\nX-Agent: codex/t-real\n\nCo-authored-by: A <a@example.invalid>\n",
        "docs: prose\n\nThe earlier run carried X-Agent: claude/t-prose and failed.\n",
        "docs: quoted\n\n> X-Agent: claude/t-quote\n",
        "fix: suffixed\n\nX-Agent: claude/t-suffix (retry of t-real)\n",
        "docs: example\n\n```text\nX-Agent: codex/t-fence\n```\n",
        "docs: unclosed example\n\n~~~\nX-Agent: claude/t-tilde\n",
        "docs: inline code is no fence\n\n```X-Agent``` names the task.\n\nX-Agent: codex/t-inline\n",
    ]
    for message in messages:
        _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unmerged\n\nX-Agent: codex/t-branch\n")
    monkeypatch.setattr(stats, "load_fleet_repos", lambda: {"fixture": FleetRepo("fixture", "example/repo", "repo", "test")})
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda key, **_: (None, repo))

    landing = stats.scan_landing(tmp_path, datetime.now(UTC) - timedelta(days=1))

    assert landing.task_ids == {"t-real", "t-inline"}
    assert landing.repositories == {"read": 1} and landing.by_repository == {"fixture": 2}
    batch_state, tasks = _state(tmp_path)
    for task_id in ("t-real", "t-real-2", "t-prose", "t-quote", "t-suffix", "t-fence", "t-tilde", "t-inline",
                    "t-branch"):
        _record(tasks, task_id)
    rows.backfill(tasks, batch_state)
    write = _row(_window(batch_state, landing), "gpt-6.1-sol", "write")
    assert write["tasks"] == 9 and write["landed"] == 2


def test_route_rows_and_metrics_carry_no_prompt_body_path_or_account(tmp_path, monkeypatch):
    """Defect class: data leak of prompts, result bodies, host paths, errors, task ids or account identities."""
    batch_state, tasks = _state(tmp_path)
    secrets = ["PROMPT-TEXT-x1", "/home/leaky/worktree", "kk", "ERROR-DETAIL-x2", "EXPLANATION-x3",
               "finding body 0", "/srv/private/model", "../escape", "acct-identity@example.invalid",
               "PRIVATE-BRIEF-x5", "INVALID-REPO-x6", "UNKNOWN-CLASS-x7", "AFTER-200-DIFF-TEXT-x9",
               "STRADDLE-x8"]
    _record(tasks, "leaky", prompt="PROMPT-TEXT-x1", prompt_file="/home/leaky/worktree/prompt.md",
            worktree_path="/home/leaky/worktree", claude_account="kk", error="ERROR-DETAIL-x2",
            research_task_family="../escape", allowance={"pool": "claude:kk", "start": {"remaining_pct": 50},
                                                        "finish": {"remaining_pct": 40}})
    _record(tasks, "odd-model", model="/srv/private/model", codex_account="acct-identity@example.invalid")
    _review(tasks, "leaky-review", "correct", ["P1"], pr=3, review_author_model="gpt-6.1-sol",
            worktree_path="/home/leaky/worktree")
    result = tasks / "leaky-review.result"
    payload = json.loads(result.read_text())
    payload["overall"]["explanation"] = "EXPLANATION-x3"
    result.write_text(json.dumps(payload))
    # Private-repo, invalid-repo, unknown-data-class, and OPSEC-bearing replies suppress the sample (F1).
    _record(tasks, "unparsed-private-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="infra-private", started_at=_at(5))
    (tasks / "unparsed-private-review.result").write_text("PRIVATE-BRIEF-x5 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-invalid-repo-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="/srv/private/repo", started_at=_at(4.5))
    (tasks / "unparsed-invalid-repo-review.result").write_text("INVALID-REPO-x6 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-bad-class-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", data_class="public-unadmitted", started_at=_at(4))
    (tasks / "unparsed-bad-class-review.result").write_text("UNKNOWN-CLASS-x7 quoted line", encoding="utf-8")
    _record(tasks, "unparsed-opsec-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", started_at=_at(3))
    (tasks / "unparsed-opsec-review.result").write_text(
        "failed for acct-identity@example.invalid in /home/leaky/worktree", encoding="utf-8",
    )
    # An identity that starts before the 200-character cut and ends after it withholds the whole sample.
    _record(tasks, "unparsed-straddle-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", started_at=_at(2.5))
    (tasks / "unparsed-straddle-review.result").write_text(
        "STRADDLE-x8 " + "s" * 178 + " acct-identity@example.invalid", encoding="utf-8",
    )
    _record(tasks, "unparsed-public-review", mode="read-only", agent="claude", model="claude-opus-5-5",
            review_profile="code", repo="public", data_class="public-code", started_at=_at(2))
    (tasks / "unparsed-public-review.result").write_text(
        "UNPARSED-PREFIX-x4 " + "z" * 220 + "AFTER-200-DIFF-TEXT-x9", encoding="utf-8",
    )
    rows.backfill(tasks, batch_state)
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=batch_state, live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx, **_scope: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))

    stored = rows.rows_path(batch_state).read_text()
    text = json.dumps(snapshot)
    metrics = generate_latest(registry).decode()

    assert [s for s in secrets if s in stored + text + metrics] == []
    assert "UNPARSED-PREFIX-x4" in text and "UNPARSED-PREFIX-x4" not in metrics
    assert "UNPARSED-PREFIX-x4" not in json.dumps(snapshot["metrics"])
    assert [t for t in ("leaky", "odd-model") if t in metrics] == []
    assert '"model": "unknown"' in text and '{effort="high",model="unknown",role="write",window="7d"} 1.0' in metrics
    assert snapshot["windows"]["7d"]["allowance"] == []  # an API-key label is no admitted pool


def test_pr_reviewed_for_two_author_models_exports_other_on_metrics(tmp_path, monkeypatch):
    """Defect class: an unbounded /metrics label value, from author ids joined after each one was bounded."""
    batch_state, tasks = _state(tmp_path)
    _review(tasks, "a-review", "incorrect", [], pr=5, review_author_model="gpt-6.1-sol", pinned_head="1" * 40)
    _review(tasks, "b-review", "correct", [], pr=5, review_author_model="claude-sonnet-5-5", pinned_head="2" * 40)
    rows.backfill(tasks, batch_state)
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    ctx = SimpleNamespace(roots=SimpleNamespace(batch_state_dir=batch_state, live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "cached_snapshot", lambda _ctx, **_scope: (snapshot, 1.0))
    registry = CollectorRegistry()
    registry.register(stats.ModelStatsCollector(ctx))

    metrics = generate_latest(registry).decode()

    assert [a["model"] for a in snapshot["windows"]["7d"]["authors"]] == ["claude-sonnet-5-5+gpt-6.1-sol"]
    assert 'model="other",role="author",window="7d"} 2.0' in metrics
    assert "claude-sonnet-5-5+gpt-6.1-sol" not in metrics


def test_page_buckets_unknown_labels_before_grouping(tmp_path, monkeypatch):
    """Defect class: grammar-valid hostnames relayed as page labels or aliased into duplicate buckets."""
    batch_state, tasks = _state(tmp_path)
    model, effort, task_class = "worker.example.invalid", "effort.example.invalid", "class.example.invalid"
    author, reviewer, risk = "author.example.invalid", "reviewer.example.invalid", "risk.example.invalid"
    repo = "repository.example.invalid"
    for i in range(10):
        _record(tasks, f"w{i}", model=model, resolved_model=model, resolved_model_known=True, effort="medium",
                research_task_family=task_class, duration_s=3600)
    _record(tasks, "w10", model="second-worker.example.invalid", effort=effort,
            research_task_family="second-class.example.invalid", duration_s=3600)
    for i in range(10):
        _record(tasks, f"known{i}", research_task_family="bounded", duration_s=3600)
    _record(tasks, "cursor-auto", agent="cursor", model="auto")
    _record(tasks, "agy-unreported", agent="agy", resolved_model="unrecognized", resolved_model_known=True)
    _review(tasks, "r1-review", "correct", [], pr=1, repo=repo, review_author_model=author,
            model=reviewer, resolved_model=reviewer, effort="medium", review_risk=risk)
    _review(tasks, "r2-review", "incorrect", [], pr=2, repo="public", review_author_model="gpt-6.1-sol")
    _review(tasks, "unknown-effort-review", "incorrect", [], pr=3, repo="public", review_author_model="gpt-6.1-sol",
            effort=None, pinned_head="1" * 40, started_at=_at(4))
    _review(tasks, "later-known-review", "correct", [], pr=3, repo="public", review_author_model="gpt-6.1-sol",
            pinned_head="2" * 40, started_at=_at(3))
    rows.backfill(tasks, batch_state)
    landing = stats.Landing(task_ids={"w0", "w10", "known0"})
    landing.by_repository.update({"public": 1, repo: 2, "second-repository.example.invalid": 3})
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=landing)
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=batch_state,
                                                live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "_compute", lambda _ctx: lambda: snapshot)
    app = FastAPI()
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    with TestClient(app) as client:
        full = client.get("/api/fleet/model-stats?view=full")
        page = client.get("/api/fleet/model-stats?view=page")

    assert full.status_code == page.status_code == 200
    assert all(label in full.text for label in (model, task_class, author, reviewer, risk, repo))
    assert effort not in full.text
    assert ".example.invalid" not in page.text
    payload = page.json()
    other = _row(payload, "other", "write")
    assert (other["tasks"], other["landed"], other["wall_seconds"]["total_seconds"]) == (10, 1, 36000)
    assert [(e["effort"], e["tasks"], e["landed"]) for e in other["by_effort"]] == [
        ("medium", 10, 1), ("unknown", 1, 1),
    ]
    assert _row(payload, "gpt-6.1-sol", "write")["tasks"] == 10
    assert _row(payload, "auto", "write")["identity"] == "harness_choice"
    assert _row(payload, "unrecognized", "write")["identity"] == "unreported"
    assert [(a["model"], a["prs"]) for a in payload["authors"]] == [("gpt-6.1-sol", 1), ("other", 1)]
    opus = _row(payload, "claude-opus-5-5", "review")
    assert (opus["tasks"], opus["approve_rate"]["n"]) == (2, 2)
    assert [(e["effort"], e["tasks"]) for e in opus["by_effort"]] == [("high", 2), ("unknown", 1)]
    assert {(c["author"], c["reviewer"], c["class"]) for c in payload["first_round_matrix"]} == {
        ("other", "other", "all"), ("other", "other", "other"),
        ("gpt-6.1-sol", "claude-opus-5-5", "all"), ("gpt-6.1-sol", "claude-opus-5-5", "code/medium"),
    }
    assert [(r["model"], [level["effort"] for level in r["levels"]])
            for r in payload["review_levels"] if r["model"] == "other"] == [("other", ["medium"])]
    assert [(job["class"], job["tasks"], job["models"][0]["model"], job["models"][0]["efforts"])
            for job in payload["charts"]["jobs"]] == [
        ("bounded", 10, "gpt-6.1-sol", ["high"]), ("other", 10, "other", ["medium"]),
    ]
    assert payload["coverage"]["landing"]["trailer_task_ids_by_repository"] == {"other": 5, "public": 1}
    # A joint author keeps its join on the page, each member bounded like a model label.
    assert stats._page_author(f"{author}+gpt-6.1-sol") == "gpt-6.1-sol+other"
    assert stats._page_author(f"{author}+{model}") == "other"


# Prose a failing reply can carry. It is not a label, a timestamp, a week or one of the page's fixed sentences.
_LEAK = "The reviewer wrote prose that is not a closed label."
_PAGE_SENTENCES = frozenset({
    "finished_at, else started_at, in [since, until)",
    "most first-round approvals per recorded wall hour, among catalog models "
    "with at least 10 landed tasks and 10 reviewed PRs",
    "not enough data yet",
    "Precision is inferred from files: a blocking finding counts as fixed when the author's "
    "later head changed its file before the same reviewer approved, else dismissed; a file "
    "changed for another reason (another finding, a merge of main) still counts as fixed, and "
    "a fix made in a different file counts as dismissed. Only verdicts the recorder published "
    "count; a blocking reply with no recorded publication (an unpublished run, or one from "
    "before the publication ledger and not imported) is of unknown publication and left out. "
    "Real precision uses the author's recorded label instead (fixed over fixed, wrong, "
    "invented and no live trigger); a label is the author's word unless a later review "
    "contested it.",
    "No planted-defect result is recorded in the summary rows.",
    "This snapshot does not include task ids.",
    "An allowance reading is the whole pool, not the one model. Overlapping tasks are charged "
    "that pool's net drop once. A cluster shared by more than one model or effort, or one that "
    "contains a reset or two readings at one timestamp that disagree, stays unknown. "
    "It is never reported as zero.",
    "There is no difficulty weight. Compare models inside one task class.",
    "Wall hours per landed stay blank when any task has no duration. The observed figure "
    "uses the recorded hours when at most one task, or one percent, is untimed.",
    "auto is Cursor Auto: the harness chose the model. A Cursor display name maps only "
    "where the shared cursor map admits it. A harness prefix resolves to its catalog id. "
    "Stored rows keep the old label until backfill.",
    "planted-defect results per reviewer: the summary rows record none. A review-eval run "
    "(lu_machinery/bakeoff/review_eval.py over docs/review-eval) writes per-seat recall to its "
    "own output directory, which this route does not read.",
})
_SCHEMA_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
_PAGE_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_PAGE_WEEK = re.compile(r"\d{4}-W\d{2}\Z")


def _leaky_snapshot(tmp_path: Path) -> dict:
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "write-one", duration_s=3600)
    _record(tasks, "odd-host", resolved_model="worker.example.invalid", resolved_model_known=True,
            effort="effort.example.invalid", research_task_family="class.example.invalid", duration_s=10)
    _review(tasks, "leaky-review", "correct", [], pr=1)
    (tasks / "leaky-review.result").write_text(_LEAK)
    rows.backfill(tasks, batch_state)
    return stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())


def _serve(snapshot: dict, tmp_path: Path, monkeypatch) -> TestClient:
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=tmp_path / "batch_state",
                                                live_repo_root=tmp_path))
    monkeypatch.setattr(stats, "_compute", lambda _ctx: lambda: snapshot)
    app = FastAPI()
    app.include_router(stats.router, prefix="/api/fleet/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    return TestClient(app)


def _page_label_ok(text: str, labels: set[str], models: set[str]) -> bool:
    if text in labels or text in models or text in _PAGE_SENTENCES:
        return True
    if rows.VARIANT_SUFFIX.fullmatch(text):
        return True
    if "+" in text:
        return all(part in models for part in text.split("+"))
    return False


def _page_strings(node: object, path: str = "$"):
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}<key>", key
            yield from _page_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _page_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def test_samples_view_returns_the_reply_text_view_page_omits(tmp_path, monkeypatch):
    """Defect class: view=page relaying reviewer reply text that view=samples is supposed to carry alone."""
    snapshot = _leaky_snapshot(tmp_path)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        page = client.get("/api/fleet/model-stats?window=7d&view=page")
        samples = client.get("/api/fleet/model-stats?window=7d&view=samples")
        full = client.get("/api/fleet/model-stats?window=7d&view=full")
        token_view = client.get("/api/fleet/model-stats?window=7d&view=tokens")
    assert page.status_code == samples.status_code == full.status_code == token_view.status_code == 200
    assert _LEAK not in page.text
    assert "unparsed_samples" not in page.text
    assert {"gemini-4-neon", "subq-savant-1.4"} <= set(page.json()["idle_catalog_models"])
    assert "savant" not in page.json()["charts"]["limits"]
    body = samples.json()
    assert body["status"] == "ok" and set(body) == {"schema_version", "status", "age_seconds", "window", "models"}
    found = [model for model in body["models"] if model["model"] == "claude-opus-5-5" and model["role"] == "review"]
    assert found == [{"model": "claude-opus-5-5", "role": "review",
                      "unparsed_samples": {"bad_format": _LEAK}, "unparsed_sample_efforts": {"bad_format": "high"}}]
    assert _LEAK in full.text
    assert _LEAK not in token_view.text and "leaky-review" not in token_view.text
    assert set(token_view.json()) == {"schema_version", "status", "age_seconds", "window", "token_use"}
    assert token_view.json()["token_use"] == page.json()["token_use"] == full.json()["token_use"]
    assert token_view.json()["token_use"]["totals"] is None
    review = _row(page.json(), "claude-opus-5-5", "review")
    assert review["unparsed_reasons"]["bad_format"] == 1


def test_page_view_string_leaves_stay_closed_for_every_window(tmp_path, monkeypatch):
    """Defect class: a free-text leaf on view=page, for any of the five windows, other than a closed label, a
    timestamp or week, or one of the page's fixed sentences."""
    from lu_machinery.orchestration.fleet_repos import FleetRepoError, load_fleet_repos

    snapshot = _leaky_snapshot(tmp_path)
    try:
        repo_keys = set(load_fleet_repos())
    except FleetRepoError:
        repo_keys = set()
    models = set(stats._known_models()) | set(stats._NOT_CONCRETE) | {stats.OTHER}
    labels = {
        *stats.WINDOWS, stats.SCHEMA_VERSION, "ok", *stats.STATUSES, "other", *rows.VERDICTS,
        *stats.UNPARSED_REASON_KEYS, *stats._PAGE_CLASSES, stats.ALL_CLASSES, rows.UNKNOWN_EFFORT,
        stats.ALL_EFFORTS, *rows.POOLS, *stats.FINDING_OUTCOMES, *stats.AFTER_BLOCK, *stats._RISK_ORDER,
        *stats.VALID_RISKS, *stats._SIZE_ORDER, *stats.START_REFUSAL_CODES, *{v for v in stats._NOT_CONCRETE.values()},
        "catalog", "joint", "not_in_catalog", "write", "review", *repo_keys, "read", "unavailable",
        "registry_unavailable",
    }
    assert not _page_label_ok(_LEAK, labels, models)
    assert not _page_label_ok("worker.example.invalid", labels, models)
    assert not _page_label_ok("file:///home/fixture/reply.txt", labels, models)
    assert not _page_label_ok("/home/fixture/reply.txt", labels, models)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        payloads = []
        for name in stats.WINDOWS:
            response = client.get(f"/api/fleet/model-stats?window={name}&view=page")
            assert response.status_code == 200, name
            assert _LEAK not in response.text and "example.invalid" not in response.text, name
            payloads.append((name, response.json()))
    offenders = []
    for name, payload in payloads:
        for path, text in _page_strings(payload):
            if path.endswith("<key>"):
                allowed = _SCHEMA_KEY.fullmatch(text) is not None or _page_label_ok(text, labels, models)
            else:
                allowed = (_page_label_ok(text, labels, models) or _PAGE_TIMESTAMP.fullmatch(text) is not None
                           or _PAGE_WEEK.fullmatch(text) is not None)
            if not allowed:
                offenders.append((name, path, text))
    assert not offenders, offenders[:8]


def test_untimed_landed_task_leaves_wall_hours_per_landed_unknown(tmp_path):
    """Defect class: a missing duration read as zero wall time, understating wall hours per landed task."""
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "timed", duration_s=3600)
    _record(tasks, "untimed")
    rows.backfill(tasks, batch_state)

    write = _row(_window(batch_state, stats.Landing(task_ids={"timed", "untimed"})), "gpt-6.1-sol", "write")

    assert write["landed"] == 2 and write["wall_seconds"]["n"] == 1
    assert write["wall_hours_per_landed"] == {"value": None, "n": 2, "low_sample": True}


def test_missing_meter_reading_records_unknown_never_zero(tmp_path, monkeypatch):
    """Defect class: fabricated allowance (a missing reading recorded or aggregated as 0), telemetry that blocks
    launch or settlement on an unready Monitor, the wrong window recorded as the weekly allowance, overlapping
    tasks on one pool each charged the pool's whole drop, a nested reset reported as zero spend, a rise
    between two tasks' readings reported as measured spend, or a number taken from two readings that share
    a timestamp and disagree."""
    readings = iter([None, 35.0])
    monkeypatch.setattr(worker.model_pause, "weekly_used_pct_reading", lambda lane, *, account: next(readings))
    record = {"agent": "codex", "codex_account": "sub", "model": "gpt-6.1-sol"}

    record["allowance"] = worker._allowance(record, "start")
    record["allowance"] = worker._allowance(record, "finish")

    assert record["allowance"]["pool"] == "codex:a"
    assert record["allowance"]["start"] == "unknown"
    assert record["allowance"]["finish"]["remaining_pct"] == 65.0
    assert worker._allowance({"agent": "opencode", "model": "deepseek-v4.1-flash"}, "start") == {
        "pool": None, "meter": "weekly_remaining_pct", "start": "unknown", "finish": "unknown"}
    monkeypatch.undo()
    # A Monitor answering 503 gets one request: no readiness wait, no retry, no admission down memo.
    requests = []

    def unready(url, timeout):
        requests.append(url)
        raise urllib.error.HTTPError(url, 503, "starting", None, None)

    monkeypatch.setattr(model_pause.urllib.request, "urlopen", unready)
    memo = model_pause._monitor_wait_spent_at
    assert worker._allowance({"agent": "cursor"}, "finish")["finish"] == "unknown"
    assert len(requests) == 1 and model_pause._monitor_wait_spent_at == memo
    # AGY Claude records its weekly window, never the stricter five-hour one.
    windows = {"five_hour": {"used_pct": 90.0}, "weekly": {"used_pct": 20.0}}
    snapshot = {"agents": {"gemini": {"status": "ok", "codexbar": {"stale": False, "freshness": "fresh"},
                                      "claude_gpt_windows": windows}}}
    monkeypatch.setattr(model_pause.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(json.dumps(snapshot).encode()))
    agy = worker._allowance({"agent": "agy", "model": "claude-opus-5-5"}, "start")
    assert agy["pool"] == "agy_claude" and agy["start"]["remaining_pct"] == 80.0

    batch_state, tasks = _state(tmp_path)
    _record(tasks, "half", allowance=record["allowance"])
    _record(tasks, "both", allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0},
                                      "finish": {"remaining_pct": 64.5}})
    rows.backfill(tasks, batch_state)
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["half"]["allowance"]["start"] == "unknown"
    _record(tasks, "overlap-a", model="gemini-4-hydrogen", effort="medium",
            started_at=(NOW - timedelta(hours=3)).isoformat(),
            finished_at=(NOW - timedelta(hours=1)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 60.0}})
    _record(tasks, "overlap-b", model="gemini-4-hydrogen", effort="medium",
            started_at=(NOW - timedelta(hours=3) + timedelta(minutes=3)).isoformat(),
            finished_at=(NOW - timedelta(hours=2)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "shared-a", model="grok-4.7",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "cursor", "start": {"remaining_pct": 100.0}, "finish": {"remaining_pct": 88.0}})
    _record(tasks, "shared-b", model="composer-2.5",
            started_at=(NOW - timedelta(hours=8) + timedelta(minutes=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "cursor", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 82.0}})
    _record(tasks, "reset-a", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "codex:b", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "reset-b", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8) + timedelta(minutes=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "codex:b", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "outer", model="claude-opus-5-5",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "agy_claude", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "inner", model="claude-opus-5-5",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "agy_claude", "start": {"remaining_pct": 75.0}, "finish": {"remaining_pct": 95.0}})
    # The reset is a different effort from the task it runs inside. Dropping it used to report 0 points spent.
    _record(tasks, "cross-outer", model="claude-sonnet-5-5", effort="high",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "claude:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 75.0}})
    _record(tasks, "cross-inner", model="claude-sonnet-5-5", effort="medium",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "claude:a", "start": {"remaining_pct": 75.0}, "finish": {"remaining_pct": 95.0}})
    # The inner reading rises, but stays below the cluster's first reading. That is still a reset.
    _record(tasks, "buried-outer", model="claude-haiku-5-5",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "claude:b", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "buried-inner", model="claude-haiku-5-5",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "claude:b", "start": {"remaining_pct": 40.0}, "finish": {"remaining_pct": 55.0}})
    # The pool falls, then a later reading from the outer task sits above the inner finish. Chronological
    # readings are 80, 40, 35, 70. That rise is a reset even though neither task's own finish rose.
    _record(tasks, "gap-outer", model="gemini-3.8-flash-high",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "gap-inner", model="gemini-3.8-flash-high",
            started_at=(NOW - timedelta(hours=7)).isoformat(),
            finished_at=(NOW - timedelta(hours=6, minutes=30)).isoformat(),
            allowance={"pool": "gemini", "start": {"remaining_pct": 40.0}, "finish": {"remaining_pct": 35.0}})
    # Crossing tasks: 80, 70, 50, 65. The rise from 50 to 65 is a reset.
    _record(tasks, "cross-read-a", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=8)).isoformat(),
            finished_at=(NOW - timedelta(hours=7)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 50.0}})
    _record(tasks, "cross-read-b", model="gpt-6.2-sol",
            started_at=(NOW - timedelta(hours=7, minutes=30)).isoformat(),
            finished_at=(NOW - timedelta(hours=6)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 70.0}, "finish": {"remaining_pct": 65.0}})
    # Same start timestamp, 80 and 95. Finish order does not say which start came first, so 15 points would
    # be a guess. The clock sits between the other codex:a clusters so this pair is its own row.
    _record(tasks, "tie-a", model="gpt-6.2-sol", effort="medium",
            started_at=(NOW - timedelta(hours=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=4)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 80.0}, "finish": {"remaining_pct": 70.0}})
    _record(tasks, "tie-b", model="gpt-6.2-sol", effort="medium",
            started_at=(NOW - timedelta(hours=5)).isoformat(),
            finished_at=(NOW - timedelta(hours=3)).isoformat(),
            allowance={"pool": "codex:a", "start": {"remaining_pct": 95.0}, "finish": {"remaining_pct": 65.0}})
    rows.backfill(tasks, batch_state)
    landed = {"both", "half", "overlap-a", "overlap-b", "shared-a", "shared-b", "reset-a", "reset-b", "outer",
              "inner", "cross-outer", "cross-inner", "buried-outer", "buried-inner", "gap-outer", "gap-inner",
              "cross-read-a", "cross-read-b", "tie-a", "tie-b"}
    window = _window(batch_state, stats.Landing(task_ids=landed))
    unknown = {"value": None, "n": 1, "low_sample": True}
    assert window["allowance"] == [
        {"pool": "agy_claude", "model": "claude-opus-5-5", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "claude:a", "model": "claude-sonnet-5-5", "effort": "high", "tasks": 1, "landed": 1,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "claude:a", "model": "claude-sonnet-5-5", "effort": "medium", "tasks": 1, "landed": 1,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "claude:b", "model": "claude-haiku-5-5", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 1, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.1-sol", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 0, "pct_points_spent": 5.5,
         "pct_points_per_landed": {"value": 5.5, "n": 1, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.2-sol", "effort": "high", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:a", "model": "gpt-6.2-sol", "effort": "medium", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "codex:b", "model": "gpt-6.2-sol", "effort": "high", "tasks": 2, "landed": 2, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "cursor", "model": "composer-2.5", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "cursor", "model": "grok-4.7", "effort": "high", "tasks": 1, "landed": 1, "reset_crossed": 0,
         "low_sample": True, "overlapped_tasks": 1, "pct_points_spent": None,
         "pct_points_per_landed": unknown},
        {"pool": "gemini", "model": "gemini-3.8-flash-high", "effort": "high", "tasks": 2, "landed": 2,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": None,
         "pct_points_per_landed": {"value": None, "n": 2, "low_sample": True}},
        {"pool": "gemini", "model": "gemini-4-hydrogen", "effort": "medium", "tasks": 2, "landed": 2,
         "reset_crossed": 0, "low_sample": True, "overlapped_tasks": 2, "pct_points_spent": 20.0,
         "pct_points_per_landed": {"value": 10.0, "n": 2, "low_sample": True}},
    ]
    assert _row(window, "gpt-6.1-sol", "write")["allowance_pct_points_per_landed"]["value"] == 5.5
    # view=page is what the dashboard prints. None is a dash; 0.0 would be shown as measured spend.
    page = stats.page_window(window)
    medium = next(row for row in page["allowance"] if row["model"] == "claude-sonnet-5-5" and row["effort"] == "medium")
    assert medium["pct_points_spent"] is None and medium["tasks"] == 1 and medium["overlapped_tasks"] == 1
    sonnet = {item["effort"]: item for item in _row(window, "claude-sonnet-5-5", "write")["by_effort"]}
    assert sonnet["medium"]["allowance_pct_points_per_landed"]["value"] is None
    assert sonnet["high"]["allowance_pct_points_per_landed"]["value"] is None
    assert _row(window, "claude-haiku-5-5", "write")["allowance_pct_points_per_landed"] == {
        "value": None, "n": 2, "low_sample": True}


def test_model_version_and_effort_are_never_collapsed_or_defaulted(tmp_path, monkeypatch):
    """Defect class: effort or model version collapsed into a family, a default or another class's figures."""
    batch_state, tasks = _state(tmp_path)
    cases = {
        "grok": dict(agent="cursor", model="grok-4.7-high", effort=None),
        "flash-medium": dict(agent="agy", model="gemini-3.8-flash-medium", effort=None),
        "flash-id": dict(agent="agy", model="gemini-3.8-flash-high", effort=None),
        "bare-alias": dict(agent="claude", model="opus", effort=None),
        "observed": dict(agent="claude", model="opus", effort="xhigh", resolved_model="claude-opus-5-5",
                         resolved_model_known=True),
        "unobserved": dict(agent="claude", model="opus", effort="high", resolved_model="claude-sonnet-5-5",
                           resolved_model_known=False),
        "context": dict(agent="claude", model="claude-opus-5-5[1m]", effort="not-exposed"),
        "unknown-version": dict(agent="claude", model="claude-opus-5-6", effort="High"),
        "free-effort": dict(agent="codex", model="gpt-6.1-sol", effort="request-0", research_task_family="docs"),
        "cursor-display": dict(agent="cursor", model="Grok 4.7 256K High", resolved_model="Grok 4.7 256K High",
                               resolved_model_known=True, effort=None),
        "cursor-fast": dict(agent="cursor", model="Grok 4.7 256K High Fast",
                            resolved_model="Grok 4.7 256K High Fast", resolved_model_known=True, effort=None),
        "composer": dict(agent="cursor", model="Composer 2.5", resolved_model="Composer 2.5",
                         resolved_model_known=True, effort=None),
        "kimi-slash": dict(agent="kimi", model="kimi-code/k3-256k", effort=None),
        "kimi-alias": dict(agent="kimi", model="k3-256k", effort=None),
        "glm-alias": dict(agent="glm", model="zai-coding-plan/glm-5.3", effort="high"),
        "glm-legacy": dict(agent="glm", model="retired_glm", effort="high"),
        "deepseek-prefix": dict(agent="deepseek", model="deepseek-direct/deepseek-v4-flash", effort=None),
        "attested-over-auto": dict(agent="cursor", model="auto", resolved_model="Grok 4.7 256K High",
                                   resolved_model_known=True, effort=None),
        "attested-fast-over-request": dict(agent="cursor", model="grok-4.7",
                                           resolved_model="Grok 4.7 256K High Fast",
                                           resolved_model_known=True, effort=None),
        "attested-fast-over-suffixed-request": dict(agent="cursor", model="grok-4.7-high",
                                                    resolved_model="Grok 4.7 256K High Fast",
                                                    resolved_model_known=True, effort="high"),
        "attested-unrecognized-over-request": dict(agent="agy", model="gemini-4-neon-high",
                                                   resolved_model="unrecognized",
                                                   resolved_model_known=True, effort="high"),
        "attested-other-model-over-suffixed-request": dict(agent="agy", model="gemini-4-neon-high",
                                                           resolved_model="gemini-4-hydrogen",
                                                           resolved_model_known=True, effort="high"),
        "attested-bare-over-suffixed-request": dict(agent="agy", model="gemini-4-neon-high",
                                                    resolved_model="gemini-4-neon",
                                                    resolved_model_known=True, effort="high"),
        "attested-agy-alias-other-model": dict(agent="agy", model="gemini-4-neon",
                                               resolved_model="gemini-4-hydrogen",
                                               resolved_model_known=True, effort="high"),
        "attested-agy-alias-bare-options-effort": dict(agent="agy", model="gemini-4-neon",
                                                       resolved_model="gemini-4-neon",
                                                       resolved_model_known=True, effort=None,
                                                       options={"effort": "high"}),
        "attested-cursor-auto-with-effort": dict(agent="cursor", model="auto",
                                                 resolved_model="Composer 2.5",
                                                 resolved_model_known=True, effort="high"),
        "attested-missing-agent-with-effort": dict(agent=None, model="gemini-4-neon",
                                                   resolved_model="gemini-4-hydrogen",
                                                   resolved_model_known=True, effort="high"),
        "attested-malformed-agent-options-effort": dict(agent=123, model="gemini-4-neon",
                                                        resolved_model="gemini-4-hydrogen",
                                                        resolved_model_known=True, effort=None,
                                                        options={"effort": "high"}),
        "attested-claude-mismatched-executor": dict(agent="claude", model="sonnet",
                                                    resolved_model="claude-sonnet-4-6",
                                                    resolved_model_known=True, effort="high"),
        "attested-claude-alias-only": dict(agent="claude", model="sonnet",
                                           resolved_model="sonnet",
                                           resolved_model_known=True, effort="high"),
        "attested-runtime-variant-missing-agent": dict(agent=None, model="gemini-4-hydrogen",
                                                       resolved_model="gemini-4-neon-medium",
                                                       resolved_model_known=True, effort="high"),
        "attested-opencode-deepseek-with-effort": dict(agent="opencode", provider="openrouter",
                                                       model="deepseek-v4.1-flash",
                                                       resolved_model="deepseek-v4.1-flash",
                                                       resolved_model_known=True, effort="medium"),
        "attested-cursor-high-with-suffixed-request": dict(agent="cursor", model="grok-4.7-high",
                                                           resolved_model="Grok 4.7 256K High",
                                                           resolved_model_known=True, effort=None),
        "attested-kimi-over-request": dict(agent="kimi", model="k3", resolved_model="kimi-code/k3-256k",
                                           resolved_model_known=True, effort=None),
    }
    for task_id, fields in cases.items():
        _record(tasks, task_id, **fields)
    rows.backfill(tasks, batch_state)

    stored = {row["task_id"]: (row["model"], row["effort"]) for row in rows.read_rows(batch_state)[0]}
    window = _window(batch_state, stats.Landing())

    assert stored == {
        "grok": ("grok-4.7", "high"),
        "flash-medium": ("gemini-3.8-flash-high", "medium"),
        "flash-id": ("gemini-3.8-flash-high", "unset"),
        "bare-alias": ("opus", "unset"),
        "observed": ("claude-opus-5-5", "xhigh"),
        "unobserved": ("opus", "high"),
        "context": ("claude-opus-5-5", "not-exposed"),
        "unknown-version": ("claude-opus-5-6", "high"),
        "free-effort": ("gpt-6.1-sol", "request-0"),
        "cursor-display": ("grok-4.7", "unset"),
        "cursor-fast": ("unknown", "unset"),
        "composer": ("composer-2.5", "unset"),
        "kimi-slash": ("kimi-code/k3-256k", "unset"),
        "kimi-alias": ("kimi-code/k3-256k", "unset"),
        "glm-alias": ("glm-5.3", "high"),
        "glm-legacy": ("retired_glm", "high"),
        "deepseek-prefix": ("deepseek-v4-flash", "unset"),
        "attested-over-auto": ("grok-4.7", "unset"),
        "attested-fast-over-request": ("unknown", "unset"),
        "attested-fast-over-suffixed-request": ("unknown", "unset"),
        "attested-unrecognized-over-request": ("unrecognized", "unset"),
        "attested-other-model-over-suffixed-request": ("gemini-4-hydrogen", "unset"),
        "attested-bare-over-suffixed-request": ("gemini-4-neon", "unset"),
        "attested-agy-alias-other-model": ("gemini-4-hydrogen", "unset"),
        "attested-agy-alias-bare-options-effort": ("gemini-4-neon", "unset"),
        "attested-cursor-auto-with-effort": ("composer-2.5", "unset"),
        "attested-missing-agent-with-effort": ("gemini-4-hydrogen", "unset"),
        "attested-malformed-agent-options-effort": ("gemini-4-hydrogen", "unset"),
        "attested-claude-mismatched-executor": ("claude-sonnet-4.6-thinking", "unset"),
        "attested-claude-alias-only": ("sonnet", "unset"),
        "attested-runtime-variant-missing-agent": ("gemini-4-neon", "medium"),
        "attested-opencode-deepseek-with-effort": ("deepseek-v4.1-flash", "unset"),
        "attested-cursor-high-with-suffixed-request": ("grok-4.7", "high"),
        "attested-kimi-over-request": ("kimi-code/k3-256k", "unset"),
    }
    flash = _row(window, "gemini-3.8-flash-high", "write")
    assert (flash["effort"], flash["efforts"], flash["tasks"]) == ("all", ["medium"], 1)
    assert [(e["effort"], e["tasks"]) for e in flash["by_effort"]] == [("medium", 1), ("unknown", 1)]
    assert {(r["model"], e["effort"]) for r in window["models"] for e in r["by_effort"]} == {
        ("grok-4.7", "high"), ("grok-4.7", "unknown"), ("gemini-3.8-flash-high", "medium"),
        ("gemini-3.8-flash-high", "unknown"), ("opus", "unknown"), ("claude-opus-5-5", "xhigh"),
        ("opus", "high"), ("claude-opus-5-5", "unknown"), ("claude-opus-5-6", "high"),
        ("gpt-6.1-sol", "unknown"), ("unknown", "unknown"), ("unrecognized", "unknown"),
        ("gemini-4-hydrogen", "unknown"), ("gemini-4-neon", "medium"), ("gemini-4-neon", "unknown"),
        ("claude-sonnet-4.6-thinking", "unknown"), ("sonnet", "unknown"), ("composer-2.5", "unknown"),
        ("deepseek-v4-flash", "unknown"), ("deepseek-v4.1-flash", "unknown"),
    }
    for model_name in ("unrecognized", "gemini-4-hydrogen", "claude-sonnet-4.6-thinking",
                       "sonnet", "composer-2.5", "deepseek-v4.1-flash"):
        assert _row(window, model_name, "write")["tasks"] == 0
    neon = _row(window, "gemini-4-neon", "write")
    assert (neon["tasks"], neon["efforts"]) == (1, ["medium"])
    sol = _row(window, "gpt-6.1-sol", "write")
    assert sol["tasks"] == 0
    assert [(e["effort"], [c["class"] for c in e["by_class"]]) for e in sol["by_effort"]] == [("unknown", ["docs"])]
    # /metrics keeps finite label sets: values outside the catalog's known set export as "other".
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        for view in ("full", "page", "samples", "tokens"):
            response = client.get(f"/api/fleet/model-stats?window=all&view={view}")
            assert response.status_code == 200
            assert not re.search(r"\b(?:glm|kimi)", response.text, re.I), view
    assert "24h" in snapshot["windows"] and "24h" not in snapshot["metrics"]["windows"]
    assert set(snapshot["metrics"]["token_windows"]) == {"24h", "7d"}
    metrics = snapshot["metrics"]["windows"]["all"]
    exported = {(r["model"], e["effort"]) for r in metrics["models"] for e in r["by_effort"]}
    assert ("gpt-6.1-sol", "unknown") in exported and ("other", "high") in exported
    assert not {"request-0", "claude-opus-5-6"} & {value for pair in exported for value in pair}
    page = stats.page_window(snapshot["windows"]["all"])
    dropped = ("by_class", "tokens", "fresh_input_tokens_per_landed", "output_tokens_per_landed",
               "unparsed_samples")
    assert all(key not in model and key not in effort
               for model in page["models"] for effort in model["by_effort"] for key in dropped)
    html = machinery_path("api/dashboards/model-stats.html").read_text()
    assert "view=page" in html and "view=samples" in html and "Best value needs more data" in html and "Low sample" in html
    assert "samples.window.until === pageWindow.until" in html
    assert "unparsed_reasons" in html and "unparsed_samples" in html
    assert "output_tokens_per_landed" not in html and "p0_p2 || 0" not in html
    assert all(token in html for token in ("pareto-class", "pareto-x", "trends-metric",
                                           "Not enough data", "pool_pace", "/subscriptions",
                                           "landed_ci"))
    assert "quota-alerts" not in html and "Cached snapshot fallback:" not in html
    assert all(token not in html for token in ("planning-blockers", "planning-ready", "planning-blocked"))
    assert "planning-pools" in html and 'href="/milestones"' in html
    # Ten short tasks must stay eligible, and the score is approvals over recorded hours.
    rank_root = tmp_path / "value"
    rank_tasks = rank_root / "tasks"
    rank_tasks.mkdir(parents=True)
    for index in range(10):
        _record(rank_tasks, f"short-{index}", model="gpt-6.2-sol", duration_s=3, research_task_family="code")
        _review(rank_tasks, f"short-review-{index}", "correct", [], pr=100 + index,
                review_author_model="gpt-6.2-sol")
        _record(rank_tasks, f"long-{index}", model="gpt-6.1-sol", duration_s=3600, research_task_family="code")
        _review(rank_tasks, f"long-review-{index}", "correct" if index < 5 else "incorrect", [], pr=200 + index,
                review_author_model="gpt-6.1-sol")
    # Per-class Pareto frontier: a faster model with lower landed share stays on the frontier beside the higher-rate
    # model, a dominated model does not, and a low-sample model never joins or dominates the frontier (#875).
    for index in range(10):
        _record(rank_tasks, f"fast-{index}", model="claude-sonnet-5-5", duration_s=600, research_task_family="code")
        _record(rank_tasks, f"slow-{index}", model="claude-opus-5-5", duration_s=7200, research_task_family="code")
    for index in range(4):
        _record(rank_tasks, f"tiny-{index}", model="grok-4.7", duration_s=1, research_task_family="code")
    for index in range(5):
        _record(rank_tasks, f"doc-a-{index}", model="gpt-6.2-sol", duration_s=300, research_task_family="docs")
        _record(rank_tasks, f"doc-b-{index}", model="grok-4.7", duration_s=300, research_task_family="docs")
    rows.backfill(rank_tasks, rank_root)
    landed_ids = (
        {f"short-{index}" for index in range(10)}
        | {f"long-{index}" for index in range(10)}
        | {f"fast-{index}" for index in range(8)}
        | {f"slow-{index}" for index in range(7)}
        | {f"tiny-{index}" for index in range(4)}
        | {f"doc-a-{index}" for index in range(5)}
    )
    ranked = stats.collect(rank_root, tmp_path, NOW, landing=stats.Landing(task_ids=landed_ids))
    charts = ranked["windows"]["7d"]["charts"]
    recommendation = charts["recommendation"]
    assert recommendation["model"] == "gpt-6.2-sol"
    assert recommendation["approvals"] == 10
    pareto_by_model = {p["model"]: p for p in charts["pareto"]}
    assert pareto_by_model["gpt-6.2-sol"]["hours_exact"] > 0
    speed_by_model = {p["model"]: p for p in charts["speed_quality"]}
    assert speed_by_model["gpt-6.2-sol"]["median_seconds"] == 3.0
    assert speed_by_model["gpt-6.2-sol"]["timed_n"] == 15
    assert speed_by_model["gpt-6.2-sol"]["low_sample"] is False
    assert speed_by_model["gpt-6.2-sol"]["frontier"] is True
    assert speed_by_model["gpt-6.1-sol"]["median_seconds"] == 3600.0
    assert speed_by_model["gpt-6.1-sol"]["timed_n"] == 10
    assert speed_by_model["gpt-6.1-sol"]["low_sample"] is False
    assert speed_by_model["gpt-6.1-sol"]["frontier"] is False
    durations_by_model = {d["model"]: d for d in charts["durations"]}
    assert (durations_by_model["claude-sonnet-5-5"]["tasks"], durations_by_model["claude-sonnet-5-5"]["landed"],
            durations_by_model["claude-sonnet-5-5"]["landed_rate"],
            durations_by_model["claude-sonnet-5-5"]["landed_ci"]) == (10, 8, 0.8, stats._wilson(8, 10))
    jobs_by_class = {j["class"]: {m["model"]: m for m in j["models"]} for j in charts["jobs"]}
    # In code: gpt-6.2-sol is 100% @ 3s (hours=0.0, hours_exact>0, frontier=True); gpt-6.1-sol is 100% @ 3600s
    # (dominated by gpt-6.2-sol); claude-sonnet-5-5 is 80% @ 750s (dominated); claude-opus-5-5 is 70% @ 10286s
    # (dominated); grok-4.7 is 100% @ 1s but n=4 (low_sample, never frontier).
    assert jobs_by_class["code"]["gpt-6.2-sol"]["hours"] == 0.0
    assert jobs_by_class["code"]["gpt-6.2-sol"]["hours_exact"] > 0
    assert jobs_by_class["code"]["gpt-6.2-sol"]["frontier"] is True
    assert jobs_by_class["code"]["gpt-6.1-sol"]["frontier"] is False
    assert jobs_by_class["code"]["claude-sonnet-5-5"]["frontier"] is False
    assert jobs_by_class["code"]["claude-opus-5-5"]["frontier"] is False
    assert jobs_by_class["code"]["grok-4.7"]["frontier"] is False
    # In docs: 10 total tasks, max n=5 per model, so no model reaches the frontier.
    assert all(m["frontier"] is False for m in jobs_by_class["docs"].values())
    # Two eligible non-dominated models in one class, plus: (1) unrounded sub-0.1s duration tie-breaking (F2),
    # (2) PR-only author admitted to weekly approval trends without write tasks (F4), and (3) speed_quality point
    # retained when wall_hours_observed is unavailable (>1% untimed writes, F5).
    split_root = tmp_path / "split_frontier"
    split_tasks = split_root / "tasks"
    split_tasks.mkdir(parents=True)
    for index in range(10):
        _record(split_tasks, f"hi-{index}", model="claude-opus-5-5", duration_s=3600, research_task_family="infra")
        _record(split_tasks, f"fast-{index}", model="grok-4.7", duration_s=600, research_task_family="infra")
        _record(split_tasks, f"dom-{index}", model="gpt-6.1-sol", duration_s=7200, research_task_family="infra")
        _record(split_tasks, f"tie-a-{index}", model="gpt-6.2-sol", duration_s=1.001, research_task_family="docs")
        _record(split_tasks, f"tie-b-{index}", model="claude-sonnet-5-5", duration_s=1.002, research_task_family="docs")
        _review(split_tasks, f"pr-only-{index}", "correct", [], pr=500 + index, review_author_model="claude-haiku-5-5")
        _review(split_tasks, f"pr-zero-{index}", "correct", [], pr=800 + index, review_author_model="gpt-6-luna")
    # Sub-cutoff counts on models qualifying in the other metric stay preserved, never zeroed out (F9).
    for index in range(3):
        _record(split_tasks, f"haiku-w-{index}", model="claude-haiku-5-5", duration_s=60, research_task_family="docs")
        _review(split_tasks, f"opus-pr-{index}", "correct", [], pr=700 + index, review_author_model="claude-opus-5-5")
    for index in range(20):
        _record(split_tasks, f"spd-t-{index}", model="gemini-4-hydrogen", duration_s=120, research_task_family="code")
    for index in range(2):
        _record(split_tasks, f"spd-u-{index}", model="gemini-4-hydrogen", research_task_family="code")
    for index in range(10):
        _review(split_tasks, f"spd-r-{index}", "correct", [], pr=600 + index, review_author_model="gemini-4-hydrogen")
        _record(split_tasks, f"zmed-w-{index}", model="gemini-4-neon",
                duration_s=0 if index < 6 else 1, research_task_family="code")
        _review(split_tasks, f"zmed-r-{index}", "correct", [], pr=900 + index, review_author_model="gemini-4-neon")
    rows.backfill(split_tasks, split_root)
    split_landed = (
        {f"hi-{index}" for index in range(9)}
        | {f"fast-{index}" for index in range(7)}
        | {f"dom-{index}" for index in range(6)}
        | {f"tie-a-{index}" for index in range(10)}
        | {f"tie-b-{index}" for index in range(10)}
        | {f"haiku-w-{index}" for index in range(2)}
        | {f"spd-t-{index}" for index in range(10)}
    )
    split_charts = stats.collect(split_root, tmp_path, NOW,
                                 landing=stats.Landing(task_ids=split_landed))["windows"]["7d"]["charts"]
    split_jobs = {j["class"]: {m["model"]: m for m in j["models"]} for j in split_charts["jobs"]}
    assert split_jobs["infra"]["claude-opus-5-5"]["frontier"] is True
    assert split_jobs["infra"]["grok-4.7"]["frontier"] is True
    assert split_jobs["infra"]["gpt-6.1-sol"]["frontier"] is False
    # Unrounded durations: 10.01s vs 10.02s both round to 10.0s, but only the faster model sits on the docs frontier.
    assert split_jobs["docs"]["gpt-6.2-sol"]["frontier"] is True
    assert split_jobs["docs"]["claude-sonnet-5-5"]["frontier"] is False
    assert split_jobs["docs"]["gpt-6.2-sol"]["hours_exact"] < split_jobs["docs"]["claude-sonnet-5-5"]["hours_exact"]
    # Weekly trends admit a model qualifying in either metric, including zero-write authors (F4, F11), and keep
    # sub-cutoff counts in the other metric (F9).
    haiku_trends = [t for t in split_charts["trends"] if t["model"] == "claude-haiku-5-5"]
    assert len(haiku_trends) == 1 and haiku_trends[0]["prs"] == 10 and haiku_trends[0]["first_approved"] == 10
    assert haiku_trends[0]["tasks"] == 3 and haiku_trends[0]["landed"] == 2
    luna_trends = [t for t in split_charts["trends"] if t["model"] == "gpt-6-luna"]
    assert len(luna_trends) == 1 and luna_trends[0]["prs"] == 10 and luna_trends[0]["first_approved"] == 10
    assert luna_trends[0]["tasks"] == 0 and luna_trends[0]["landed"] == 0
    opus_trends = [t for t in split_charts["trends"] if t["model"] == "claude-opus-5-5"]
    assert len(opus_trends) == 1 and opus_trends[0]["tasks"] == 10 and opus_trends[0]["prs"] == 3
    assert opus_trends[0]["first_approved"] == 3
    # Speed/quality keeps gemini-4-hydrogen (20 timed + 2 untimed writes -> no hours_observed, 10 PRs, F5)
    # and keeps finite zero medians (6x 0s + 4x 1s writes -> median 0.0, 10 PRs, F17).
    assert "gemini-4-hydrogen" not in {p["model"] for p in split_charts["pareto"]}
    split_speed = {p["model"]: p for p in split_charts["speed_quality"]}
    assert split_speed["gemini-4-hydrogen"]["median_seconds"] == 120.0
    assert split_speed["gemini-4-hydrogen"]["timed_n"] == 20
    assert split_speed["gemini-4-hydrogen"]["prs"] == 10
    assert split_speed["gemini-4-hydrogen"]["frontier"] is False
    assert split_speed["gemini-4-neon"]["median_seconds"] == 0.0
    assert split_speed["gemini-4-neon"]["timed_n"] == 10
    assert split_speed["gemini-4-neon"]["prs"] == 10
    assert split_speed["gemini-4-neon"]["frontier"] is True
    # Equal approval counts: the unrounded durations decide, not the one-decimal total or the model name.
    fine = tmp_path / "fine"
    fine_tasks = fine / "tasks"
    fine_tasks.mkdir(parents=True)
    for index in range(10):
        _record(fine_tasks, f"a-{index}", model="gpt-6.1-sol", duration_s=3.001)
        _review(fine_tasks, f"a-review-{index}", "correct", [], pr=300 + index, review_author_model="gpt-6.1-sol")
        _record(fine_tasks, f"b-{index}", model="gpt-6.2-sol", duration_s=3.004)
        _review(fine_tasks, f"b-review-{index}", "correct", [], pr=400 + index, review_author_model="gpt-6.2-sol")
    rows.backfill(fine_tasks, fine)
    fine_window = stats.collect(fine, tmp_path, NOW, landing=stats.Landing(
        task_ids={f"a-{index}" for index in range(10)} | {f"b-{index}" for index in range(10)}))
    assert fine_window["windows"]["7d"]["charts"]["recommendation"]["model"] == "gpt-6.1-sol"
    classes = []
    for index in range(9):
        classes.append({"role": "write", "model": "gpt-6.1-sol", "identity": "catalog", "tasks": 11, "landed": 11,
                        "landed_rate": {"value": 1.0, "n": 11, "low_sample": False}, "status": {"done": 11},
                        "wall_seconds": {"n": 0, "total_seconds": 0},
                        "by_class": [{"class": f"job-{index}", "tasks": 11, "landed": 11,
                                      "landed_rate": {"value": 1.0}}]})
    assert len(stats._charts(classes, [], [])["jobs"]) == 9


def _at(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_supplemental_reviews_enter_reviewer_figures_without_changing_author_outcomes(tmp_path):
    """Defect class: supplemental reviews dropped from reviewer totals or attributed to the wrong PR round."""
    batch_state, tasks = _state(tmp_path)
    _review(tasks, "u-review", "incorrect", [], pr=9, review_author_model="gpt-6.1-sol", pinned_head="1" * 40,
            started_at=_at(3))
    (tasks / "u-review.result").write_text("Looks fine to me.")  # no verdict the review contract reads
    _review(tasks, "p-review", "correct", [], pr=9, review_author_model="gpt-6.1-sol", pinned_head="2" * 40)
    _record(tasks, "neon-initial-review", mode="read-only", agent="agy", model="gemini-4-neon-high",
            effort="not-exposed", resolved_model="gemini-4-neon-medium", resolved_model_known=True,
            options={"pinned_head": "1" * 40}, duration_s=120,
            allowance={"pool": "gemini", "start": {"remaining_pct": 98}, "finish": {"remaining_pct": 96}})
    _record(tasks, "neon-rate-limited-review", mode="read-only", agent="agy", model="gemini-4-neon-medium",
            options={"pinned_head": "1" * 40}, status="rate_limited", started_at=_at(4), duration_s=60)
    _record(tasks, "neon-later-review", mode="read-only", agent="agy", model="gemini-4-neon-high",
            options={"pinned_head": "2" * 40}, duration_s=180)
    (tasks / "neon-later-review.result").write_text("VERDICT: APPROVE\nSCORE: 9/10\n\nReviewed the complete diff.")
    _review(tasks, "neon-unknown-review", "correct", [], agent="agy", model="gemini-4-neon",
            resolved_model="gemini-4-neon", effort=None, duration_s=10000)
    _record(tasks, "grok-review", mode="read-only", agent="cursor", model="grok-4.7-high",
            status="failed")
    # Reviewer-effort costs use the PR's landing even when the writer effort is unknown.
    _record(tasks, "landed", effort=None, delivery_pr="example/repo#9", delivery_merge_sha="3" * 40)
    rows.backfill(tasks, batch_state)
    # A retained structured result can repair an existing summary even when its task record did not change.
    (tasks / "neon-initial-review.result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": "The linked change passed its targeted checks.",
                    "confidence": 0.95},
        "findings": [{
            "id": "F1", "priority": "P3", "category": "docs", "confidence": 0.95,
            "title": "Name the denominator", "body": "The count label omits its denominator.",
            "location": {"path": "page.html", "start_line": 7, "end_line": 7, "claim_type": "present"},
            "verbatim": "Total", "why_wrong": "The label is ambiguous.",
            "smallest_fix": "Name the denominator in the label.", "sources": ["none"],
        }],
    }))
    assert rows.backfill(tasks, batch_state, dry_run=True)["would_refresh"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 0

    window = _window(batch_state, stats.Landing())

    review = _row(window, "claude-opus-5-5", "review")
    assert review["tasks"] == 2
    assert review["verdicts"] == {"correct": 1, "incorrect": 0, "uncertain": 0, "unparsed": 1}
    assert review["unparsed_reasons"] == {
        "missing_result": 0, "too_big": 0, "bad_format": 1, "bad_fields": 0, "not_done": 0, "unknown": 0,
    }
    assert review["unparsed_samples"] == {"bad_format": "Looks fine to me."}
    assert review["approve_rate"] == {"value": 1.0, "n": 1, "low_sample": True}
    assert review["blocking_per_review"]["value"] == 0.0
    assert [model for model in window["models"] if model["model"] == "grok-4.7" and model["role"] == "review"] == []
    grok = _row(window, "grok-4.7", "write")
    assert grok["status"]["failed"] == grok["tasks"] == 1
    neon = _row(window, "gemini-4-neon", "review")
    assert (neon["approve_rate"]["n"], neon["findings"], neon["findings_n"]) == (2, 1, 1)
    levels = {level["effort"]: level for model in window["review_levels"] if model["model"] == "gemini-4-neon"
              for level in model["levels"]}
    assert (levels["high"]["results"], levels["low"]["results"]) == (1, 0)
    assert levels["high"]["first_round_approved"]["n"] == 0
    assert levels["high"]["findings"] is None
    assert levels["medium"] == {
        "effort": "medium", "tasks": 1, "results": 1, "findings": 1, "findings_n": 1, "first_round_missing": 0,
        "first_round_approved": {"value": 1.0, "n": 1, "low_sample": True}, "median_seconds": 120.0,
        "wall_minutes_per_landed": {"value": 2.0, "n": 1, "low_sample": True},
        "allowance_per_landed": [{"pool": "gemini", "value": 2.0, "n": 1, "low_sample": True}],
    }
    (author,) = window["authors"]
    assert (author["prs"], author["rounds_per_pr"]["value"], author["first_round_approved"]["value"]) == (1, 1.0, 1.0)
    assert author["after_blocked_first_round"]["blocked"] == 0
    neon_chart = window["charts"]["neon"]
    assert neon_chart["enough"] is False
    unknown_arm = neon_chart["unknown_effort"]
    assert (unknown_arm["effort"], unknown_arm["review_tasks"], unknown_arm["review_results"]) == ("unknown", 1, 1)
    arms = {arm["effort"]: arm for arm in neon_chart["arms"]}
    assert {effort: arm["review_results"] for effort, arm in arms.items()} == {"medium": 1, "high": 1}
    assert (arms["medium"]["review_tasks"], arms["medium"]["blocking_per_review"], arms["medium"]["p0_p2_per_review"],
            arms["medium"]["findings_n"], arms["medium"]["review_median_seconds"],
            arms["medium"]["review_p90_seconds"], arms["medium"]["review_timed_n"],
            arms["medium"]["write_median_seconds"], arms["medium"]["write_timed_n"]) == (
                1, 0.0, 0.0, 1, 120.0, 120.0, 1, 60.0, 1)
    assert (arms["high"]["review_tasks"], arms["high"]["blocking_per_review"], arms["high"]["p0_p2_per_review"],
            arms["high"]["findings_n"], arms["high"]["review_median_seconds"],
            arms["high"]["review_p90_seconds"], arms["high"]["review_timed_n"],
            arms["high"]["write_median_seconds"], arms["high"]["write_timed_n"]) == (
                1, None, None, 0, 180.0, 180.0, 1, None, 0)


def test_first_round_matrix_splits_author_from_reviewer_and_class(tmp_path):
    """Defect class: one first-round figure per author that mixes author, reviewer and risk; the first round taken
    in row order instead of by start; or a blocked PR's later outcome misread (new head, same head, none)."""
    batch_state, tasks = _state(tmp_path)
    sol, grok, sonnet = "gpt-6.1-sol", "grok-4.7", {"model": "claude-sonnet-5-5", "resolved_model": "claude-sonnet-5-5"}
    critical = {"review_profile": "infra", "review_risk": "critical"}
    heads = {name: name[-1] * 40 for name in ("h1", "h2", "h3", "h4", "h5", "h6")}
    # PR 1: Sol, blocked by Opus, then a new head approved.
    _review(tasks, "p1a-review", "incorrect", ["P1"], pr=1, review_author_model=sol, pinned_head=heads["h1"],
            started_at=_at(6), **critical)
    _review(tasks, "p1b-review", "correct", [], pr=1, review_author_model=sol, pinned_head=heads["h2"],
            started_at=_at(4), **critical)
    # PR 2: Sol, approved by Sonnet at once.
    _review(tasks, "p2-review", "correct", [], pr=2, review_author_model=sol, pinned_head=heads["h3"], **sonnet)
    # PR 3: Grok, approved by Opus first; a later Sonnet review of the same head (stored first) is no first round.
    _review(tasks, "a3-review", "incorrect", [], pr=3, review_author_model=grok, pinned_head=heads["h4"],
            started_at=_at(2), **sonnet, **critical)
    _review(tasks, "p3-review", "correct", [], pr=3, review_author_model=grok, pinned_head=heads["h4"],
            started_at=_at(5), **critical)
    # PR 4: Grok, blocked by Opus, then the same head approved; PR 5: Grok, blocked by Sonnet, nothing after.
    _review(tasks, "p4a-review", "incorrect", [], pr=4, review_author_model=grok, pinned_head=heads["h5"],
            started_at=_at(5), **critical)
    _review(tasks, "p4b-review", "correct", [], pr=4, review_author_model=grok, pinned_head=heads["h5"],
            started_at=_at(3), **sonnet, **critical)
    _review(tasks, "p5-review", "uncertain", [], pr=5, review_author_model=grok, pinned_head=heads["h6"], **sonnet)
    rows.backfill(tasks, batch_state)

    window = _window(batch_state, stats.Landing())

    cells = {(c["author"], c["reviewer"], c["class"]): (c["approved"], c["first_round_approved"]["n"])
             for c in window["first_round_matrix"]}
    assert cells == {
        (sol, "claude-opus-5-5", "all"): (0, 1), (sol, "claude-opus-5-5", "infra/critical"): (0, 1),
        (sol, "claude-sonnet-5-5", "all"): (1, 1), (sol, "claude-sonnet-5-5", "code/medium"): (1, 1),
        (grok, "claude-opus-5-5", "all"): (1, 2), (grok, "claude-opus-5-5", "infra/critical"): (1, 2),
        (grok, "claude-sonnet-5-5", "all"): (0, 1), (grok, "claude-sonnet-5-5", "code/medium"): (0, 1),
    }
    after = {a["model"]: a["after_blocked_first_round"] for a in window["authors"]}
    assert after[sol]["blocked"] == 1 and after[sol]["approved_on_new_head"]["value"] == 1.0
    assert after[grok]["blocked"] == 2
    assert [after[grok][k]["value"] for k in stats.AFTER_BLOCK] == [0.0, 0.5, 0.5]


def test_aliases_map_to_the_model_they_ran_only_when_unambiguous(tmp_path):
    """Defect class: a harness alias shown as a separate model, mapped to a version it may not have run, or a
    routed catalog model missing from the page because it ran nothing in the window."""
    batch_state, tasks = _state(tmp_path)
    _record(tasks, "opus-observed", agent="claude", model="claude-opus-5-5", finished_at=_at(3))
    _record(tasks, "opus-alias", agent="claude", model="opus", finished_at=_at(2))
    _record(tasks, "sonnet-alias-early", agent="claude", model="sonnet", finished_at=_at(5))
    _record(tasks, "sonnet-observed", agent="claude", model="claude-sonnet-5-5", finished_at=_at(3))
    _record(tasks, "cursor-auto", agent="cursor", model="auto")
    _record(tasks, "no-model", agent="cursor", model=None)
    _record(tasks, "hydrogen", agent="agy", model="gemini-4-hydrogen")
    _review(tasks, "alias-review", "correct", [], model="opus", resolved_model_known=False, pr=1,
            review_author_model="opus")
    rows.backfill(tasks, batch_state)

    window = _window(batch_state, stats.Landing())

    seen = {(r["model"], r["role"]): (r["identity"], r["tasks"]) for r in window["models"]}
    assert seen == {
        ("claude-opus-5-5", "write"): ("catalog", 2), ("claude-opus-5-5", "review"): ("catalog", 1),
        ("claude-sonnet-5-5", "write"): ("catalog", 1), ("sonnet", "write"): ("alias", 1),
        ("auto", "write"): ("harness_choice", 1), ("unknown", "write"): ("unreported", 1),
        ("gemini-4-hydrogen", "write"): ("catalog", 1),
    }
    assert [(a["model"], a["identity"]) for a in window["authors"]] == [("claude-opus-5-5", "catalog")]
    idle = window["idle_catalog_models"]
    assert idle and not {"gemini-4-hydrogen", "claude-opus-5-5"} & set(idle)
    assert all(stats.identity(model) == "catalog" for model in idle)


def test_cold_request_waits_for_the_collect_instead_of_a_poll(tmp_path, monkeypatch):
    """Defect class: a fresh Monitor answering pending so the page waits a fixed poll interval although the collect
    finishes sooner; a second collect for one cold start (a background refresh that runs after the cold request's
    collect stored its snapshot); a slow collect holding the request without bound; or a failed collect, even one
    that raises TimeoutError itself, answered as pending so the page keeps polling a failure."""
    release, background, calls, failures = threading.Event(), threading.Event(), [], []

    def fake_collect(*_args, **_kwargs):
        calls.append(1)
        if failures:
            raise failures.pop()
        if len(calls) == 2:
            release.wait(10)
        return {"schema_version": stats.SCHEMA_VERSION, "windows": {"7d": {"records": len(calls)}}, "metrics": {}}

    real_refresh = stats.cache_get_or_compute

    def late_refresh(*args, **kwargs):  # the background refresh thread runs only once the test lets it
        background.wait(10)
        return real_refresh(*args, **kwargs)

    def settled() -> None:
        deadline = time.monotonic() + 5
        while stats._refreshing and time.monotonic() < deadline:
            time.sleep(0.01)

    monkeypatch.setattr(stats, "collect", fake_collect)
    monkeypatch.setattr(stats, "cache_get_or_compute", late_refresh)
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path, batch_state_dir=tmp_path,
                                                live_repo_root=tmp_path))
    app = FastAPI()
    app.include_router(stats.router, prefix="/model-stats")
    app.dependency_overrides[get_ctx] = lambda: ctx
    try:
        with TestClient(app) as client:
            assert stats.cached_snapshot(ctx) is None  # a /metrics scrape first: its refresh thread waits
            cold = client.get("/model-stats")
            assert cold.json()["status"] == "ok" and cold.json()["records"] == 1 and calls == [1]
            background.set()
            settled()
            assert calls == [1]  # the late background refresh found the stored snapshot fresh

            cache_invalidate(str(tmp_path.resolve()))
            monkeypatch.setattr(stats, "COLD_WAIT_SECONDS", 0.05)
            slow = client.get("/model-stats")
            assert slow.json()["status"] == "pending" and slow.headers["Retry-After"] == "1"
            release.set()
            deadline = time.monotonic() + 5
            while (body := client.get("/model-stats").json())["status"] != "ok" and time.monotonic() < deadline:
                time.sleep(0.02)
            assert body["records"] == 2 and calls == [1, 1]
            settled()

            cache_invalidate(str(tmp_path.resolve()))
            monkeypatch.setattr(stats, "COLD_WAIT_SECONDS", 5.0)
            failures.append(TimeoutError("the collect itself timed out"))
            failed = client.get("/model-stats")
            assert failed.status_code == 503 and "Retry-After" not in failed.headers
            assert failed.json()["status"] == "unavailable" and failed.json()["error"] == "TimeoutError"
            assert calls == [1, 1, 1]
            settled()
    finally:
        release.set()
        background.set()
        cache_invalidate(str(tmp_path.resolve()))


def test_pinned_astra_rows_counted_separately(tmp_path, monkeypatch):
    """Defect class: a pinned model's runs merged into, or dropped beside, the routed Sol rows.

    Astra 6.2 runs only on request (operator 2026-11-17); each pinned run, read-only security
    review or write, must stay its own model row next to Sol 6.2 and historical Sol 6.1 rows.
    """
    batch_state, tasks = _state(tmp_path)
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    _record(tasks, "astra-write", model="gpt-6.2-astra", duration_s=900)
    _record(tasks, "sol62-write", model="gpt-6.2-sol", duration_s=300)
    _record(tasks, "sol61-write", duration_s=600)
    _review(tasks, "astra-review", "correct", ["P2"], agent="codex", model="gpt-6.2-astra",
            resolved_model="gpt-6.2-astra", duration_s=400)
    added = rows.backfill(tasks, batch_state)["added"]
    payload = _window(batch_state, stats.Landing(task_ids={"astra-write"}))

    assert added == 4
    astra_write = _row(payload, "gpt-6.2-astra", "write")
    assert (astra_write["tasks"], astra_write["landed"]) == (1, 1)
    assert astra_write["wall_seconds"]["total_seconds"] == 900.0
    assert _row(payload, "gpt-6.2-astra", "review")["tasks"] == 1
    assert (_row(payload, "gpt-6.2-sol", "write")["tasks"], _row(payload, "gpt-6.2-sol", "write")["landed"]) == (1, 0)
    assert _row(payload, "gpt-6.1-sol", "write")["tasks"] == 1


def _findings_review(tasks: Path, task_id: str, correctness: str, findings: list[tuple[str, str]], **fields) -> None:
    """A review of record whose reply is a ``code-review-findings.v1`` object with (priority, path) findings."""
    _record(tasks, task_id, **{"mode": "read-only", "agent": "claude", "model": "opus",
                               "resolved_model": "claude-opus-5-5", "resolved_model_known": True,
                               "review_profile": "code", "review_risk": "medium", "repo": "machinery",
                               "review_author_model": "grok-4.7", **fields})
    (tasks / f"{task_id}.result").write_text(json.dumps({
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": correctness, "explanation": "Checked the change.", "confidence": 0.9},
        "findings": [{
            "id": f"F{index}", "priority": priority, "category": "bug", "confidence": 0.9,
            "title": "A defect", "body": "The change breaks a case.",
            "location": {"path": path, "start_line": 1, "end_line": 1, "claim_type": "present"},
            "verbatim": "x", "why_wrong": "It is wrong.", "smallest_fix": "Fix it.", "sources": ["none"],
        } for index, (priority, path) in enumerate(findings)],
    }))


def _publish(batch_state: Path, *published: tuple[str, int, str, str],
             repository: str = "learn-ukrainian/machinery") -> int:
    """Ledger lines, as the verdict recorder writes them, for (task id, PR, head, verdict) publications."""
    return rows.record_publications(batch_state, [rows.publication(
        task_id=task_id, repository=repository, pr=pr, head=head, verdict=verdict,
        published_at=NOW.isoformat()) for task_id, pr, head, verdict in published])


def test_finding_precision_follows_the_files_the_author_changed_before_the_same_reviewer_approved(
        tmp_path, monkeypatch):
    """Defect class: reviewer precision that counts a finding as real without the author changing its file, lets
    another reviewer's approval (or an unrecordable same-head approval, or one that finished first) settle it,
    counts a verdict that was never published (or settles a finding on an unpublished approval), trusts a
    publication from another repository, drops a review dispatched by branch alone, reads a path as a commit header,
    guesses an outcome for a head the checkout lacks, reports a planted-defect recall the rows cannot support, or
    states how many approved-later reviews had their files read from any rows but each reviewer's own.
    Every expected figure below is counted by hand."""
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "fixture")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "fixture@example.invalid")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "--template=")
    for name in ("a.py", "b.py", "c.py"):
        (repo / "src" / name).write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "blocked head")
    h1 = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "src" / "a.py").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "fix a.py only")
    h2 = _git(repo, "rev-parse", "HEAD").strip()
    missing = "e" * 40

    def resolve(key, **_):
        if key != "machinery":
            raise stats.FleetRepoError(f"no checkout for {key}")
        return None, repo

    monkeypatch.setattr(stats, "resolve_fleet_repo", resolve)
    batch_state, tasks = _state(tmp_path)
    sonnet = {"model": "claude-sonnet-5-5", "resolved_model": "claude-sonnet-5-5"}
    sol = {"agent": "codex", "model": "gpt-6.1-sol", "resolved_model": "gpt-6.1-sol"}
    blocked, approved = {"started_at": _at(7), "finished_at": _at(6)}, {"started_at": _at(5), "finished_at": _at(4)}
    # PR 1: two blocking findings; the next head changed a.py only, then Opus approved: 1 fixed, 1 dismissed.
    # The P2 finding is not blocking and never counts.
    _findings_review(tasks, "p1a-review", "incorrect", [("P1", "src/a.py"), ("P1", "src/b.py"), ("P2", "src/c.py")],
                     pr=1, pinned_head=h1, **blocked)
    _findings_review(tasks, "p1b-review", "correct", [], pr=1, pinned_head=h2, **approved)
    # PR 2: an approval of the blocked head itself can never be recorded, so the finding stays open.
    _findings_review(tasks, "p2a-review", "incorrect", [("P0", "src/a.py")], pr=2, pinned_head=h1, **blocked)
    _findings_review(tasks, "p2b-review", "correct", [], pr=2, pinned_head=h1, **approved)
    # PR 3: only another reviewer approved: Opus's finding stays open.
    _findings_review(tasks, "p3a-review", "incorrect", [("P1", "src/a.py")], pr=3, pinned_head=h1, **blocked)
    _findings_review(tasks, "p3b-review", "correct", [], pr=3, pinned_head=h2, **approved, **sonnet)
    # PR 4: the blocked head is not in the checkout: 1 unknown, never guessed.
    _findings_review(tasks, "p4a-review", "incorrect", [("P1", "src/a.py")], pr=4, pinned_head=missing, **blocked)
    _findings_review(tasks, "p4b-review", "correct", [], pr=4, pinned_head=h2, **approved)
    # PR 5: a Sol reply without finding locations: its two blocking findings are unknown.
    _review(tasks, "p5a-review", "incorrect", ["P1", "P1"], pr=5, review_author_model="grok-4.7",
            repo="machinery", pinned_head=h1, **blocked, **sol)
    _review(tasks, "p5b-review", "correct", [], pr=5, review_author_model="grok-4.7", repo="machinery",
            pinned_head=h2, **approved, **sol)
    # PR 6: the approval started after the block but finished before it: it settles nothing, the finding is open.
    _findings_review(tasks, "p6a-review", "incorrect", [("P1", "src/a.py")], pr=6, pinned_head=h1,
                     started_at=_at(7), finished_at=_at(2))
    _findings_review(tasks, "p6b-review", "correct", [], pr=6, pinned_head=h2, started_at=_at(5), finished_at=_at(3))
    # PR 7: two uncatalogued reviewers the page groups as "other": one's approval never settles the other's finding.
    _findings_review(tasks, "p7a-review", "incorrect", [("P1", "src/a.py")], pr=7, pinned_head=h1, **blocked,
                     model="pilot-model-a", resolved_model="pilot-model-a")
    _findings_review(tasks, "p7b-review", "correct", [], pr=7, pinned_head=h2, **approved,
                     model="pilot-model-b", resolved_model="pilot-model-b")
    # PR 8: a blocking review with only a P2 finding has no P0-P1 finding to settle and needs no head pair.
    _findings_review(tasks, "p8a-review", "incorrect", [("P2", "src/c.py")], pr=8, pinned_head=h2, **blocked)
    _findings_review(tasks, "p8b-review", "correct", [], pr=8, pinned_head=h1, **approved)
    # PR 9: a blocking reply whose verdict was never published: of unknown publication, none of its findings count.
    _findings_review(tasks, "p9a-review", "incorrect", [("P1", "src/a.py")], pr=9, pinned_head=h1, **blocked)
    _findings_review(tasks, "p9b-review", "correct", [], pr=9, pinned_head=h2, **approved)
    # PR 10: the ledger names the task at another head than the row's: still of unknown publication.
    _findings_review(tasks, "p10a-review", "incorrect", [("P1", "src/a.py")], pr=10, pinned_head=h1, **blocked)
    # PR 11: the same reviewer approved a new head, but that approval was never published: the finding stays open.
    _findings_review(tasks, "p11a-review", "incorrect", [("P1", "src/b.py")], pr=11, pinned_head=h1, **blocked)
    _findings_review(tasks, "p11b-review", "correct", [], pr=11, pinned_head=h2, **approved)
    # PR 12: reviews dispatched by branch alone keep no PR in their rows; the ledger names it: 1 fixed.
    _findings_review(tasks, "p12a-review", "incorrect", [("P1", "src/a.py")], pinned_head=h1, **blocked)
    _findings_review(tasks, "p12b-review", "correct", [], pinned_head=h2, **approved)
    # PR 13: the ledger names the task in another repository than the row's: of unknown publication.
    _findings_review(tasks, "p13a-review", "incorrect", [("P1", "src/a.py")], pr=13, pinned_head=h1, **blocked)
    # A rejection dispatched by branch alone with no publication: of unknown publication too.
    _findings_review(tasks, "p14a-review", "incorrect", [("P1", "src/a.py")], pinned_head=h1, **blocked)
    # PR 15: a published blocking review with unknown effort stays out of reviewer accuracy and head_pairs.
    _findings_review(tasks, "p15a-review", "incorrect", [("P1", "src/a.py")], pr=15, pinned_head=h2,
                     effort=None, **blocked)
    _findings_review(tasks, "p15b-review", "correct", [], pr=15, pinned_head=h1, **approved)
    rows.backfill(tasks, batch_state)
    assert "src/a.py" not in rows.rows_path(batch_state).read_text()
    blocked_by, approved_by = "CHANGES_REQUESTED", "APPROVED"
    published = [
        ("p1a-review", 1, h1, blocked_by), ("p1b-review", 1, h2, approved_by),
        ("p2a-review", 2, h1, blocked_by), ("p2b-review", 2, h1, approved_by),
        ("p3a-review", 3, h1, blocked_by), ("p3b-review", 3, h2, approved_by),
        ("p4a-review", 4, missing, blocked_by), ("p4b-review", 4, h2, approved_by),
        ("p5a-review", 5, h1, blocked_by), ("p5b-review", 5, h2, approved_by),
        ("p6a-review", 6, h1, blocked_by), ("p6b-review", 6, h2, approved_by),
        ("p7a-review", 7, h1, blocked_by), ("p7b-review", 7, h2, approved_by),
        ("p8a-review", 8, h2, blocked_by), ("p8b-review", 8, h1, approved_by),
        ("p9b-review", 9, h2, approved_by), ("p10a-review", 10, h2, blocked_by), ("p11a-review", 11, h1, blocked_by),
    ]
    published += [("p12a-review", 12, h1, blocked_by), ("p12b-review", 12, h2, approved_by),
                  ("p15a-review", 15, h2, blocked_by), ("p15b-review", 15, h1, approved_by)]
    assert _publish(batch_state, *published) == 23
    assert _publish(batch_state, ("p13a-review", 13, h1, blocked_by),
                    repository="learn-ukrainian/learn-ukrainian.github.io") == 1
    # The ledger holds each publication once: a recorder rerun adds nothing.
    assert _publish(batch_state, published[0]) == 0

    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())

    # Distinct (repository, blocked, approved) heads with finding paths: PR 1 and PR 7 share h1 to h2 (PR 5 keeps no
    # paths, so it needs no pair); PR 4's blocked head is missing; PR 15 has unknown effort and adds no pair.
    assert snapshot["coverage"]["finding_heads"] == {"pairs": 2, "read": 1}
    assert snapshot["coverage"]["publications"] == {"lines": 24, "tasks": 24}
    opus = {
        "model": "claude-opus-5-5", "identity": "catalog", "efforts": ["high"],
        "blocking_reviews": 8, "reviews_unknown_publication": 4,
        "reviews_without_paths": 0, "blocking_findings": 8, "fixed": 2, "dismissed": 1, "open": 4, "unknown": 1,
        # PRs 1, 4 and 12 were approved later by Opus at another head; PR 4's blocked head could not be read.
        "reviews_approved_later": 3, "reviews_approved_later_read": 2,
        "precision": stats._rate(2, 3), "precision_ci": stats._wilson(2, 3),
    }
    assert opus["precision"]["low_sample"] and opus["precision"]["n"] == 3
    pilot = {"blocking_reviews": 1, "blocking_findings": 1, "open": 1, "fixed": 0, "dismissed": 0, "unknown": 0,
             "reviews_approved_later": 0, "reviews_approved_later_read": 0}
    for payload, pilot_label in ((snapshot["windows"]["7d"], "pilot-model-a"),
                                 (snapshot["page_windows"]["7d"], "other")):
        accuracy = payload["reviewer_accuracy"]
        found = {row["model"]: row for row in accuracy["findings"]}
        assert set(found) == {"claude-opus-5-5", "gpt-6.1-sol", pilot_label}
        assert found["claude-opus-5-5"] == opus
        assert {key: found[pilot_label][key] for key in pilot} == pilot
        sol_row = found["gpt-6.1-sol"]
        assert (sol_row["blocking_reviews"], sol_row["reviews_without_paths"]) == (1, 1)
        assert (sol_row["blocking_findings"], sol_row["unknown"], sol_row["precision"]["value"]) == (2, 2, None)
        assert (sol_row["reviews_approved_later"], sol_row["reviews_approved_later_read"]) == (0, 0)
        recall = accuracy["planted_defect_recall"]
        assert recall["value"] is None and recall["status"] == "unknown"
        assert "review_eval" in recall["missing_input"]

    # A changed file named like the next pair's header is still a path: every path follows a raw record.
    record = b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M"
    output = b"\0".join([b"a" * 40, record, b"b" * 40, b"b" * 40, record, b"real.py", b""])
    assert stats._diff_blocks(output, ["a" * 40, "b" * 40]) == [["b" * 40], ["real.py"]]
    assert stats._diff_blocks(output, ["a" * 40]) is None


_HEAD_A = "a" * 40
_HEAD_B = "b" * 40
_START_A = _at(2)
_START_B = _at(1.5)


def _label(batch_state: Path, task_id: str, pr: int, finding_id: str, label: str, evidence: str | None = None,
           *, head: str = _HEAD_A, started_at: str = _START_A, reviewer_model: str = "claude-opus-5-5",
           repository: str = "learn-ukrainian/machinery", recorded_at: str = _at(1)) -> int:
    """An author label line, as the verdict recorder's finding-outcome verb writes it."""
    return rows.record_finding_outcomes(batch_state, [rows.finding_outcome(
        kind="label", task_id=task_id, repository=repository, pr=pr, finding_id=finding_id, head=head,
        started_at=started_at, reviewer_model=reviewer_model, label=label, evidence=evidence,
        recorded_by="author-task", recorded_at=recorded_at)])


def _contest(batch_state: Path, task_id: str, pr: int, finding_id: str, contested_by: str,
             *, head: str = _HEAD_A, started_at: str = _START_A, repository: str = "learn-ukrainian/machinery",
             recorded_at: str = _at(0.5)) -> int:
    """A contest line, as the verdict recorder writes it when a later review contests a label."""
    return rows.record_finding_outcomes(batch_state, [rows.finding_outcome(
        kind="contest", task_id=task_id, repository=repository, pr=pr, finding_id=finding_id, head=head,
        started_at=started_at, contested_by=contested_by, recorded_at=recorded_at)])


def test_finding_outcome_labels_count_per_reviewer_and_a_contest_holds_the_label_out(tmp_path):
    """Defect class: label stats depending on a rows or publications join, a contested author claim counted as
    settled, a contest from another run of a reused task id (different head or start) or an unbound legacy contest
    suppressing an earlier run's label, a future contest or label leaking into an earlier snapshot, a contest on
    another PR or repository holding out a finding, a contest without a label counted, a legacy line without
    reviewer_model or run binding rejected as malformed or misattributed, a retired model kept, or a non-catalog
    model leaking onto the page view. Every expected figure below is counted by hand."""
    batch_state, _ = _state(tmp_path)
    # No summary rows and no publications: label stats read only finding-outcomes.jsonl by stored reviewer_model.
    # Opus: fixed, wrong, invented (contested before NOW), out_of_scope, duplicate; plus one label contested only
    # after NOW (still counts as fixed at NOW) and one label recorded at NOW (outside [since, NOW)).
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 1
    assert _label(batch_state, "o1-review", 1, "F1", "wrong", "the guard on line 3 already returns early") == 1
    assert _label(batch_state, "o1-review", 1, "F2", "invented", "src/c.py has no such function") == 1
    assert _contest(batch_state, "o1-review", 1, "F2", "o1b-review") == 1
    assert _label(batch_state, "o2-review", 2, "F0", "out_of_scope", "outside the frozen obligation") == 1
    assert _label(batch_state, "o2-review", 2, "F1", "duplicate", "same defect as F0") == 1
    assert _label(batch_state, "o2-review", 2, "F2", "fixed") == 1
    assert _contest(batch_state, "o2-review", 2, "F2", "o2b-review",
                    recorded_at=(NOW + timedelta(hours=1)).isoformat()) == 1
    assert _label(batch_state, "o2-review", 2, "F3", "fixed", recorded_at=NOW.isoformat()) == 1
    # Contests on another PR, another repository, another run (different head or start of the same task id on the same
    # PR), or an unlabelled finding never affect PR 1's labels.
    assert _contest(batch_state, "o1-review", 9, "F0", "o9-review") == 1
    assert _contest(batch_state, "o1-review", 1, "F1", "o9-review", repository="other/repo") == 1
    assert _contest(batch_state, "o1-review", 1, "F0", "o1c-review", head=_HEAD_B, started_at=_START_B) == 1
    assert _contest(batch_state, "o1-review", 1, "F0", "o1d-review", head=_HEAD_A, started_at=_START_B) == 1
    assert _contest(batch_state, "o2-review", 2, "F9", "o2b-review") == 1
    # Sol: one wrong inside 7d, one fixed 10 days ago (inside 30d/all, outside 7d).
    assert _label(batch_state, "s1-review", 3, "F0", "wrong", "already handled", reviewer_model="gpt-6.2-sol") == 1
    assert _label(batch_state, "s2-review", 4, "F0", "fixed", reviewer_model="gpt-6.2-sol",
                  recorded_at=(NOW - timedelta(days=10)).isoformat()) == 1
    # Effort-suffixed Grok selector normalizes to catalog id grok-4.7; retired Kimi is excluded; an uncatalogued
    # spelling stays concrete in full windows and bounds to "other" on the page view.
    assert _label(batch_state, "g1-review", 5, "F0", "invented", "no such symbol", reviewer_model="grok-4.7-high") == 1
    assert _label(batch_state, "k1-review", 6, "F0", "fixed", reviewer_model="kimi-k3") == 1
    assert _label(batch_state, "u1-review", 7, "F0", "fixed", reviewer_model="custom-reviewer") == 1
    # Legacy review-finding-outcome.v1 lines without reviewer_model/head/started_at are not malformed, count in
    # unattributed_labels / unbound_contests, and never suppress bound labels.
    legacy_label = {
        "schema": rows.OUTCOME_SCHEMA, "kind": "label", "task_id": "legacy-review",
        "repository": "agentic-machinery/machinery", "pr": 8, "finding_id": "F1", "label": "fixed",
        "evidence_sha256": None, "recorded_by": "author-task", "recorded_at": _at(1),
    }
    legacy_contest = {
        "schema": rows.OUTCOME_SCHEMA, "kind": "contest", "task_id": "o1-review",
        "repository": "agentic-machinery/machinery", "pr": 1, "finding_id": "F0",
        "contested_by": "legacy-r2", "recorded_at": _at(0.5),
    }
    with (batch_state / rows.ROWS_DIR / rows.OUTCOMES_FILE).open("ab") as stream:
        stream.write(json.dumps(legacy_label).encode() + b"\n" + json.dumps(legacy_contest).encode() + b"\n")

    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    assert snapshot["coverage"]["finding_outcomes"] == {"lines": 21, "unattributed_labels": 1, "unbound_contests": 1}
    zero = dict.fromkeys(("fixed", "wrong", "invented", "no_live_trigger", "out_of_scope", "duplicate", "contested"), 0)
    full_7d = {row["model"]: row for row in snapshot["windows"]["7d"]["reviewer_accuracy"]["labels"]}
    assert set(full_7d) == {"claude-opus-5-5", "gpt-6.2-sol", "grok-4.7", "custom-reviewer"}
    assert full_7d["claude-opus-5-5"]["labels"] == {
        **zero, "fixed": 2, "wrong": 1, "out_of_scope": 1, "duplicate": 1, "contested": 1,
    }
    assert full_7d["claude-opus-5-5"]["real_precision"] == stats._rate(2, 3)
    assert full_7d["gpt-6.2-sol"]["labels"] == {**zero, "wrong": 1}
    assert full_7d["gpt-6.2-sol"]["real_precision"] == stats._rate(0, 1)
    assert full_7d["grok-4.7"]["labels"] == {**zero, "invented": 1}
    assert full_7d["grok-4.7"]["real_precision"] == stats._rate(0, 1)
    assert full_7d["custom-reviewer"]["labels"] == {**zero, "fixed": 1}
    assert full_7d["custom-reviewer"]["identity"] == "not_in_catalog"

    page_7d = {row["model"]: row for row in snapshot["page_windows"]["7d"]["reviewer_accuracy"]["labels"]}
    assert set(page_7d) == {"claude-opus-5-5", "gpt-6.2-sol", "grok-4.7", "other"}
    assert page_7d["other"]["labels"] == {**zero, "fixed": 1}

    full_30d = {row["model"]: row for row in snapshot["windows"]["30d"]["reviewer_accuracy"]["labels"]}
    assert full_30d["gpt-6.2-sol"]["labels"] == {**zero, "fixed": 1, "wrong": 1}
    assert full_30d["gpt-6.2-sol"]["real_precision"] == stats._rate(1, 2)
    full_all = {row["model"]: row for row in snapshot["windows"]["all"]["reviewer_accuracy"]["labels"]}
    assert full_all["gpt-6.2-sol"]["labels"] == {**zero, "fixed": 1, "wrong": 1}
    assert "reviewer_miss_proxy" not in snapshot["windows"]["30d"]
    assert "reviewer_miss_proxy" not in snapshot["page_windows"]["30d"]


def test_no_live_trigger_counts_in_precision_denominator_not_numerator(tmp_path):
    """Defect class: a finding whose trigger cannot occur on the real system either has no label (refused as wrong
    shape), counts as a real error (in the numerator: 2/2), or leaves real precision untouched (outside the
    denominator: 1/1). One fixed plus one no_live_trigger label must read 1/2 with the matching interval."""
    batch_state, _ = _state(tmp_path)
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 1
    assert _label(batch_state, "o1-review", 1, "F1", "no_live_trigger", "only main() calls it, after the guard") == 1
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    [row] = snapshot["windows"]["7d"]["reviewer_accuracy"]["labels"]
    assert row["labels"]["fixed"] == 1 and row["labels"]["no_live_trigger"] == 1
    assert row["real_precision"] == stats._rate(1, 2)
    assert row["real_precision_ci"] == stats._wilson(1, 2)


# Exact bytes of lu_machinery/api/model_stats_rows.py at 0152786d592213efee20cd453e9a147f62398253 (tag v0.1.84).
# Checked in so a depth-one checkout can still load that writer. sha256 274e872f6793c14f6c4f8f62674e400d599ad59fe22a1236cabea906db36e211.
_PRIOR_RELEASE_ROWS_B64 = """
IiIiRHVyYWJsZSBwZXItdGFzayBzdW1tYXJ5IHJvd3MgYmVoaW5kIHRoZSBtb2RlbCBzdGF0aXN0aWNzICgjNTgx
KSwgYW5kIHRoZWlyIG9uZS10aW1lIGJhY2tmaWxsLgoKT25lIEpTT04gbGluZSBwZXIgc2V0dGxlZCBkaXNwYXRj
aCB0YXNrIGluIGBgYmF0Y2hfc3RhdGUvbW9kZWwtc3RhdHMvcm93cy5qc29ubGBgOiB0aGUgY29uY3JldGUgbW9k
ZWwsIHRoZQplZmZlY3RpdmUgZWZmb3J0LCByb2xlLCBjbGFzcywgdGVybWluYWwgc3RhdHVzLCB0aW1pbmdzLCBu
b3JtYWxpc2VkIHRva2VucywgYWxsb3dhbmNlIHJlYWRpbmdzLCB0aGUgcmV2aWV3CnZlcmRpY3QgYW5kIGZpbmRp
bmcgcHJpb3JpdGllcywgYW5kIHRoZSBsYW5kaW5nIHJlZmVyZW5jZSB0aGUgdGFzayByZWNvcmRlZC4gVGhlIGZp
bGUgbGl2ZXMgYmVzaWRlIHRoZSB0YXNrCnN0b3JlLCBub3QgaW4gaXQsIHNvIHRhc2stcmVjb3JkIGFyY2hpdmlu
ZyBhbmQgcmV0ZW50aW9uIG5ldmVyIHRvdWNoIGl0LCBhbmQgdGhlIHN0YXRpc3RpY3MgY2FuIGNvdmVyIGFueQp3
aW5kb3cgYmFjayB0byB0aGUgZmlyc3QgcmVjb3JkLgoKV3JpdGVyczogdGhlIGRpc3BhdGNoZXIgYXBwZW5kcyBh
IHJvdyBlYWNoIHRpbWUgaXQgd3JpdGVzIGEgc2V0dGxlZCByZWNvcmQgKGBgcmVjb3JkX3NldHRsZWRgYDsgdGhl
IGxhc3Qgcm93Cm9mIGEgdGFzayBpZCB3aW5zLCBzbyB0aGUgZmluYWwgdXBkYXRlIGRlY2lkZXMpLCBhbmQgYGBi
YWNrZmlsbGBgIGFkZHMgYSByb3cgZm9yIGV2ZXJ5IHNldHRsZWQgcmVjb3JkIHRoYXQgaGFzCm5vbmUuIEJvdGgg
aG9sZCBgYHJvd3MubG9ja2BgOyBuZWl0aGVyIGV2ZXIgY2hhbmdlcyBhIHRhc2sgcmVjb3JkLiBSb3dzIGNhcnJ5
IGxhYmVscywgY291bnRzLCB0aW1lc3RhbXBzLCBQUgpudW1iZXJzIGFuZCBjb21taXQgU0hBcyBvbmx5LCBwbHVz
LCBmb3IgYSBzdHJ1Y3R1cmVkIHJldmlldywgYSAxNi1oZXggZGlnZXN0IG9mIGVhY2ggYmxvY2tpbmcgZmluZGlu
ZydzIGZpbGUgcGF0aAooYGBibG9ja2luZ19wYXRoc2BgLCBuZXZlciB0aGUgcGF0aCksIGFuZCBmb3IgYW4gdW5w
YXJzZWQgcmV2aWV3IHJlcGx5LCBpdHMgdHlwZWQgYGB1bnBhcnNlZF9yZWFzb25gYCBhbmQgdXAgdG8gMjAwCmVz
Y2FwZWQgY2hhcmFjdGVycyBvZiB0aGF0IHJlcGx5OiBubyBwcm9tcHQsIHBhcnNlZCBib2R5LCBlcnJvciB0ZXh0
LCBhY2NvdW50IGlkZW50aXR5IG9yIGhvc3QgcGF0aC4KCkJlc2lkZSB0aGUgcm93cywgYGBwdWJsaWNhdGlvbnMu
anNvbmxgYCBpcyB0aGUgcHVibGlzaGVkLXZlcmRpY3QgbGVkZ2VyICgjOTAyKTogb25lIGxpbmUgcGVyIHZlcmRp
Y3QgdGhlIHJlY29yZGVyCnB1Ymxpc2hlZCBvbiBhIFBSICh0YXNrIGlkLCByZXBvc2l0b3J5LCBQUiwgaGVhZCwg
dmVyZGljdCwgR2l0SHViJ3MgY29tbWVudCB0aW1lKS4gSXRzIG9uZSB3cml0ZXIgaXMKYGByZWNvcmRfY2ZfdmVy
ZGljdGBgIHVuZGVyIHRoZSBzYW1lIGBgcm93cy5sb2NrYGA7IHRoZSBzdGF0aXN0aWNzIGNvdW50IGEgcmV2aWV3
J3MgdmVyZGljdCBvbmx5IHdoZW4gdGhlIGxlZGdlcgpuYW1lcyBpdC4KCmBgZmluZGluZy1vdXRjb21lcy5qc29u
bGBgIGlzIHRoZSBmaW5kaW5nLW91dGNvbWUgbGVkZ2VyLCB1bmRlciB0aGUgc2FtZSBsb2NrIHdpdGggdGhlIHNh
bWUgb25lIHdyaXRlcjogb25lIGxpbmUgcGVyCmF1dGhvciBsYWJlbCBvZiBhIGJsb2NraW5nIGZpbmRpbmcgb2Yg
YSBwdWJsaXNoZWQgcmV2aWV3IChgYGZpeGVkYGAsIGBgd3JvbmdgYCwgYGBpbnZlbnRlZGBgLCBgYG91dF9vZl9z
Y29wZWBgLApgYGR1cGxpY2F0ZWBgOyB0aGUgZXZpZGVuY2UgbGluZSBhcyBpdHMgU0hBLTI1NiBvbmx5KSBhbmQg
b25lIHBlciBsYXRlciByZXZpZXcgdGhhdCBjb250ZXN0ZWQgYSBsYWJlbC4KIiIiCgpmcm9tIF9fZnV0dXJlX18g
aW1wb3J0IGFubm90YXRpb25zCgppbXBvcnQgYXJncGFyc2UKaW1wb3J0IGZjbnRsCmltcG9ydCBoYXNobGliCmlt
cG9ydCBqc29uCmltcG9ydCBsb2dnaW5nCmltcG9ydCBtYXRoCmltcG9ydCBvcwppbXBvcnQgcmUKaW1wb3J0IHN0
YXQKaW1wb3J0IHN5cwppbXBvcnQgdGltZQppbXBvcnQgdW5pY29kZWRhdGEKZnJvbSBjb2xsZWN0aW9ucyBpbXBv
cnQgQ291bnRlcgpmcm9tIGNvbGxlY3Rpb25zLmFiYyBpbXBvcnQgQ2FsbGFibGUsIEl0ZXJhYmxlLCBJdGVyYXRv
ciwgTWFwcGluZwpmcm9tIGNvbnRleHRsaWIgaW1wb3J0IGNvbnRleHRtYW5hZ2VyCmZyb20gZGF0ZXRpbWUgaW1w
b3J0IFVUQywgZGF0ZXRpbWUKZnJvbSBmdW5jdG9vbHMgaW1wb3J0IGxydV9jYWNoZQpmcm9tIHBhdGhsaWIgaW1w
b3J0IFBhdGgKZnJvbSB0eXBpbmcgaW1wb3J0IEFueSwgY2FzdAoKZnJvbSBsdV9tYWNoaW5lcnkuYWdlbnRfcnVu
dGltZS5hdHRlbXB0X2JvdW5kYXJ5IGltcG9ydCBDTEFVREVfU1VCU0NSSVBUSU9OX0FDQ09VTlRfTEFCRUxTCmZy
b20gbHVfbWFjaGluZXJ5LmFwaS5vcHNlY19zY2FuIGltcG9ydCBzY2FuX3RleHQKZnJvbSBsdV9tYWNoaW5lcnku
Y29tbW9uIGltcG9ydCBqc29uX2RlcHRoCmZyb20gbHVfbWFjaGluZXJ5LmNvbW1vbi5naXRfY3JlZGVudGlhbHMg
aW1wb3J0IF9pc192YWxpZF9zbHVnIGFzIF9pc19zZWxlY3Rvcl9zbHVnCmZyb20gbHVfbWFjaGluZXJ5LmNvbW1v
bi50YXNrX3N0b3JlX3BhdGhzIGltcG9ydCByZWFkX2NhY2hlZF90YXNrX3JlY29yZCwgdGFza3NfZGlyCmZyb20g
bHVfbWFjaGluZXJ5LmRpc3BhdGNoZXIuYWRtaXNzaW9uIGltcG9ydCByZXZpZXdfdHlwZWQKZnJvbSBsdV9tYWNo
aW5lcnkuZGlzcGF0Y2hlci50eXBlcyBpbXBvcnQgTkVWRVJfU1RBUlRFRCwgU3RhcnRSZWZ1c2FsCmZyb20gbHVf
bWFjaGluZXJ5LmZsZWV0X2NvbW1zLnJldmlld19wdWJsaWNhdGlvbiBpbXBvcnQgKAogICAgQkxPQ0tJTkdfUFJJ
T1JJVElFUywKICAgIFJldmlld1B1YmxpY2F0aW9uRXJyb3IsCiAgICBub3JtYWxpemVfdmVyZGljdCwKKQpmcm9t
IGx1X21hY2hpbmVyeS5tb2RlbF9mYW1pbGllcyBpbXBvcnQgY2Fub25pY2FsX2N1cnNvcl9tb2RlbApmcm9tIGx1
X21hY2hpbmVyeS5vcmNoZXN0cmF0aW9uLmZsZWV0X3JlcG9zIGltcG9ydCAoCiAgICBGbGVldFJlcG8sCiAgICBj
bGVhcl9yZXBvc2l0b3J5X2NhY2hlLAogICAgbG9hZF9mbGVldF9yZXBvcywKICAgIHJlcG9zaXRvcnlfaWRlbnRp
dHksCikKZnJvbSBsdV9tYWNoaW5lcnkub3JjaGVzdHJhdGlvbi50YXNrX3JlY29yZF9zdG9yZSBpbXBvcnQgUkVW
SUVXX1RBU0tfRkxBR19GSUVMRFMsIGl0ZXJfdGFza19yZWNvcmRzCmZyb20gbHVfbWFjaGluZXJ5LnJldmlldy5m
aW5kaW5ncyBpbXBvcnQgRVZJREVOQ0VEX09VVENPTUVfTEFCRUxTLCBGSU5ESU5HX09VVENPTUVfTEFCRUxTCmZy
b20gbHVfbWFjaGluZXJ5LnJldmlldy5tb2RlbF9jYXRhbG9nIGltcG9ydCBWQVJJQU5UX1NVRkZJWCwgY2Fub25p
Y2FsX21vZGVsX2lkLCBsb2FkX21vZGVsX2NhdGFsb2cKZnJvbSBsdV9tYWNoaW5lcnkucmV2aWV3LnJldmlld19j
b250cmFjdCBpbXBvcnQgU0NIRU1BX1ZFUlNJT04sIENvbnRyYWN0RXJyb3IsIHBhcnNlX3Jldmlld2VyX2pzb24K
ZnJvbSBsdV9tYWNoaW5lcnkucmV2aWV3LnZlcmRpY3RfcGFyc2VyIGltcG9ydCAoCiAgICBjaGVja2VkX2ZpbmRp
bmdzX3BheWxvYWQsCiAgICBkZWNsYXJlc19pbmNvbXBsZXRlX3JldmlldywKICAgIHVuc3VwcG9ydGVkX3ZlcmRp
Y3RfdG9rZW5zLAopCmZyb20gbHVfbWFjaGluZXJ5LnNlY3JldF9yZWRhY3RvciBpbXBvcnQgcmVkYWN0X3RleHQK
CiMgRGVlcGVzdCBKU09OIHRoaXMgbW9kdWxlIHJlYWRzIChqc29uX2RlcHRoKTogaXRzIG93biBtb2RlbC1zdGF0
cyByb3dzLgpfSlNPTl9NQVhfREVQVEggPSAxMjgKClJPV19TQ0hFTUEgPSAibW9kZWwtc3RhdHMtcm93LnYxIgpS
T1dTX0RJUiA9ICJtb2RlbC1zdGF0cyIKUk9XU19GSUxFID0gInJvd3MuanNvbmwiCkxPQ0tfRklMRSA9ICJyb3dz
LmxvY2siCk1BWF9SRVNVTFRfQllURVMgPSA0ICogMTAyNCAqIDEwMjQKTUFYX1NBTVBMRV9DSEFSUyA9IDIwMAoj
IEEgcmVwbHkgaXMgc2Nhbm5lZCB3aG9sZSBiZWZvcmUgYW55IG9mIGl0IGlzIHNob3duOyBhIGxvbmdlciBvbmUg
Z2l2ZXMgbm8gc2FtcGxlLCBzbyBhIHNjYW4gc3RheXMgc2hvcnQuCk1BWF9TQU1QTEVfU0NBTl9CWVRFUyA9IDE2
ICogMTAyNApBQ1RJVkUgPSBmcm96ZW5zZXQoeyJzcGF3bmluZyIsICJydW5uaW5nIn0pClZFUkRJQ1RTID0gKCJj
b3JyZWN0IiwgImluY29ycmVjdCIsICJ1bmNlcnRhaW4iLCAidW5wYXJzZWQiKQpVTlBBUlNFRF9SRUFTT05TID0g
KCJtaXNzaW5nX3Jlc3VsdCIsICJ0b29fYmlnIiwgImJhZF9mb3JtYXQiLCAiYmFkX2ZpZWxkcyIpCk5PX0RFTElW
RVJBQkxFX0NBVVNFUyA9ICgib3V0c2lkZV9vd25lZF9wYXRocyIsICJzY29wZV9icmVha2VyX3N0b3AiLCAibm9f
Y29tbWl0c19vcl91bmtub3duIiwgImRlYWRsaW5lIiwgIm90aGVyIikKX1JFQ09SREVEX1ZFUkRJQ1RTID0geyJB
UFBST1ZFRCI6ICJjb3JyZWN0IiwgIkNIQU5HRVNfUkVRVUVTVEVEIjogImluY29ycmVjdCIsICJCTE9DS0VEIjog
InVuY2VydGFpbiJ9ClBSSU9SSVRJRVMgPSAoIlAwIiwgIlAxIiwgIlAyIiwgIlAzIikKX0VTQ0FQRV9DQVRFR09S
SUVTID0gZnJvemVuc2V0KHsiQ2MiLCAiQ2YiLCAiQ3MiLCAiWmwiLCAiWnAifSkKX05BTUVEX0VTQ0FQRVMgPSB7
IlxuIjogciJcbiIsICJcciI6IHIiXHIiLCAiXHQiOiByIlx0In0KX0ZJRUxEX0NPTlRSQUNUX0VSUk9SUyA9ICgK
ICAgICJyZXZpZXdfcm9vdF9tdXN0X2JlX29iamVjdCIsCiAgICAic2NoZW1hX3Zpb2xhdGlvbjoiLAogICAgImR1
cGxpY2F0ZV9maW5kaW5nX2lkcyIsCiAgICAiaW52YWxpZF9saW5lX3JhbmdlOiIsCiAgICAicmV2aWV3X2VudmVs
b3BlX3ZlcmRpY3RfbWlzbWF0Y2giLAogICAgInJldmlld19lbnZlbG9wZV9pbnZhbGlkX3ZlcmRpY3Q6IiwKKQpf
U0hBMjU2ID0gcmUuY29tcGlsZShyIlswLTlhLWZdezY0fVxaIikKX1BBVEhfRElHRVNUID0gcmUuY29tcGlsZShy
IlswLTlhLWZdezE2fVxaIikKVU5TRVQgPSAidW5zZXQiClVOS05PV05fRUZGT1JUID0gInVua25vd24iCiMgTmF0
aXZlIENsYXVkZSBoYXJuZXNzIGFsaWFzZXMgdGhhdCBuYW1lIGEgZmFtaWx5IGJ1dCBubyB2ZXJzaW9uOyBzdG9y
ZWQgYXMgcmVjb3JkZWQsIG5ldmVyIGdpdmVuIG9uZSBoZXJlICh0aGUKIyBzdGF0aXN0aWNzIG1hcCBvbmUgdG8g
YSBjYXRhbG9nIGlkIG9ubHkgd2hlcmUgdGhlIHJvd3MgbWFrZSBpdCB1bmFtYmlndW91cykuCk5BVElWRV9BTElB
U0VTID0gKCJvcHVzIiwgInNvbm5ldCIsICJoYWlrdSIsICJmYWJsZSIpCl9MQUJFTCA9IHJlLmNvbXBpbGUociJb
QS1aYS16MC05XVtBLVphLXowLTkuXzpcLV17MCw2M31cWiIpCiMgQSBjYXRhbG9nIGlkIG1heSBjb250YWluIG9u
ZSBzbGFzaCAoYGBraW1pLWNvZGUvazMtMjU2a2BgKS4gVHdvIHNsYXNoZXMsIGEgbGVhZGluZyBzbGFzaCBvciBg
YC4uYGAgc3RpbGwgZmFpbCwKIyBzbyBhIHBhdGggY2Fubm90IHBhc3MgYXMgYSBtb2RlbCBuYW1lLgpfTU9ERUxf
TEFCRUwgPSByZS5jb21waWxlKAogICAgciJbQS1aYS16MC05XVtBLVphLXowLTkuXzpcLV17MCw2M30oPzovW0Et
WmEtejAtOV1bQS1aYS16MC05Ll86XC1dezAsNjN9KT9cWiIpCl9TSEEgPSByZS5jb21waWxlKHIiWzAtOWEtZl17
NDB9KD86WzAtOWEtZl17MjR9KT9cWiIpCiMgVGhlIGRpc3BhdGNoZXIncyB0YXNrLWlkIGdyYW1tYXIgKGRpc3Bh
dGNoZXIuc3RvcmUucmVjb3JkX3BhdGgpOyBhIGxlZ2FjeSByZWNvcmQncyBpZCBpcyBub3QgY2hlY2tlZCBvbiBy
ZWFkLgpfVEFTS19JRCA9IHJlLmNvbXBpbGUociJbQS1aYS16MC05XVtBLVphLXowLTlfLi1dezAsMTI3fVxaIikK
X0VGRk9SVF9TVUZGSVggPSByZS5jb21waWxlKHJmIi0oe1ZBUklBTlRfU1VGRklYLnBhdHRlcm59KVxaIikKX1JF
VElSRURfU1VCU0NSSVBUSU9OX01PREVMID0gcmUuY29tcGlsZShyIig/PCFbYS16MC05XSkoPzpnbG18a2ltaSko
PzpjYyk/KD8hW2Etel0pIiwgcmUuSSkKIyBQbGFuLW1ldGVyZWQgcG9vbHMgb25seTsgYW4gQVBJLWtleSBhY2Nv
dW50IGxhYmVsIG5hbWVzIG5vIGFsbG93YW5jZSBhbmQgaXMgbmV2ZXIgc3RvcmVkLgpQT09MUyA9IGZyb3plbnNl
dCh7KihmImNsYXVkZTp7bGFiZWx9IiBmb3IgbGFiZWwgaW4gQ0xBVURFX1NVQlNDUklQVElPTl9BQ0NPVU5UX0xB
QkVMUyksCiAgICAgICAgICAgICAgICAgICAiY29kZXg6YSIsICJjb2RleDpiIiwgImFneV9jbGF1ZGUiLCAiZ2Vt
aW5pIiwgImN1cnNvciJ9KQoKX2xvZyA9IGxvZ2dpbmcuZ2V0TG9nZ2VyKF9fbmFtZV9fKQoKX1JFRlVTQUxfQ09E
RVMgPSBmcm96ZW5zZXQoU3RhcnRSZWZ1c2FsKQpfUFJFX1NUQVJUX0VSUk9SUyA9IHsKICAgICJDTEFVREVfQUND
T1VOVF9SRUZVU0VEIjogU3RhcnRSZWZ1c2FsLkFDQ09VTlQsCiAgICAiZmFsbGJhY2tfYWRvcHRpb25fdW5hdmFp
bGFibGUiOiBTdGFydFJlZnVzYWwuUExBQ0VNRU5ULAogICAgInNldHRsZW1lbnRfcmVnaXN0cnlfdW5hdmFpbGFi
bGUiOiBTdGFydFJlZnVzYWwuU0VUVVAsCiAgICAidGFza19zY3JhdGNoX3VuYXZhaWxhYmxlIjogU3RhcnRSZWZ1
c2FsLlNFVFVQLAogICAgInJlYWRfb25seV9zY3JhdGNoX3VuYXZhaWxhYmxlIjogU3RhcnRSZWZ1c2FsLlNFVFVQ
LAogICAgIm91dHB1dCBzY2hlbWEgYmluZGluZyBpbnZhbGlkIjogU3RhcnRSZWZ1c2FsLlNFVFVQLAogICAgInJl
YWRfb25seV91bmVuZm9yY2VhYmxlIjogU3RhcnRSZWZ1c2FsLkJPVU5EQVJZLAogICAgIndyaXRlX2JvdW5kYXJ5
X3JlZnVzZWQiOiBTdGFydFJlZnVzYWwuQk9VTkRBUlksICAjIG5vIHdyYXBwZXIgUElEIGFuZCBubyBleGl0IGNv
ZGU6IGxhdW5jaGVyIGNvbnN0cnVjdGlvbgogICAgInJldmlld19hdHRlbXB0X3JlZnVzZWQiOiBTdGFydFJlZnVz
YWwuUkVWSUVXX1NFVFVQLAogICAgIk1PREVMX1BBVVNFRCI6IFN0YXJ0UmVmdXNhbC5IRUFEUk9PTSwKICAgICJN
T0RFTF9IRUFEUk9PTSI6IFN0YXJ0UmVmdXNhbC5IRUFEUk9PTSwKICAgICJNT0RFTF9IRUFEUk9PTV9VU0FHRV9V
TktOT1dOIjogU3RhcnRSZWZ1c2FsLkhFQURST09NLAogICAgIk1PREVMX0hFQURST09NX01PTklUT1JfVU5BVkFJ
TEFCTEUiOiBTdGFydFJlZnVzYWwuSEVBRFJPT00sCiAgICAiTU9ERUxfUk9MRV9CQU5ORUQiOiBTdGFydFJlZnVz
YWwuRUxJR0lCSUxJVFksCiAgICAiTU9ERUxfUk9MRV9SRVFVSVJFRCI6IFN0YXJ0UmVmdXNhbC5FTElHSUJJTElU
WSwKICAgICJNT0RFTF9ST0xFX1BPTElDWV9JTlZBTElEIjogU3RhcnRSZWZ1c2FsLkVMSUdJQklMSVRZLAogICAg
Ik1PREVMX1JPTEVfVFJJQUxfU0hBRE9XX1JFUVVJUkVEIjogU3RhcnRSZWZ1c2FsLkVMSUdJQklMSVRZLAp9CgoK
ZGVmIHN0YXJ0X3JlZnVzYWwocmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSwgY291bnRzOiB0dXBsZVtpbnQsIGlu
dCwgaW50XSkgLT4gc3RyIHwgTm9uZToKICAgICIiIlVzZSB0aGUgdHlwZWQgZGlzcGF0Y2hlciByZWFzb24sIG9y
IHJlcXVpcmUgcHJlLXN0YXJ0IGV2aWRlbmNlIGZyb20gYSBsZWdhY3kgcmVjb3JkLiIiIgogICAgc3RhdHVzID0g
cmVjb3JkLmdldCgic3RhdHVzIikKICAgIGlmIHN0YXR1cyBub3QgaW4gKCJmYWlsZWQiLCAicmF0ZV9saW1pdGVk
IiwgImNyYXNoZWQiKSBvciBhbnkoY291bnRzKToKICAgICAgICByZXR1cm4gTm9uZQogICAgaWYgIm5ldmVyX3N0
YXJ0ZWRfcmVhc29uIiBpbiByZWNvcmQ6CiAgICAgICAgIyBBIG1vZGVybiBzZXR0bGVtZW50IGNhbiBydWxlIG91
dCBhIHJlZnVzYWwgZXZlbiB3aGVuIGl0cyBsYXN0IGxhdW5jaCBleGl0ZWQgMTI1LgogICAgICAgIHJlYXNvbiA9
IHJlY29yZFsibmV2ZXJfc3RhcnRlZF9yZWFzb24iXQogICAgICAgIHJldHVybiByZWFzb24gaWYgaXNpbnN0YW5j
ZShyZWFzb24sIHN0cikgYW5kIHJlYXNvbiBpbiBfUkVGVVNBTF9DT0RFUyBlbHNlIE5vbmUKICAgIGlmIHN0YXR1
cyAhPSAiZmFpbGVkIiBvciByZWNvcmQuZ2V0KCJyZXN1bHRfc2hhMjU2Iikgb3IgcmVjb3JkLmdldCgicmVzb2x2
ZWRfbW9kZWxfa25vd24iKSBpcyBUcnVlOgogICAgICAgIHJldHVybiBOb25lCgogICAgZXJyb3IgPSByZWNvcmQu
Z2V0KCJlcnJvciIpCiAgICBjb2RlID0gZXJyb3IucGFydGl0aW9uKCI6IilbMF0gaWYgaXNpbnN0YW5jZShlcnJv
ciwgc3RyKSBlbHNlIE5vbmUKICAgIGV4aXRfY29kZSA9IHJlY29yZC5nZXQoInJldHVybmNvZGUiKQogICAgaWYg
ZXhpdF9jb2RlID09IDEyNToKICAgICAgICAjIDEyNSBjYW4gYWxzbyBiZSB0aGUgcHJvdmlkZXIncyBleGl0IG9y
IGEgYm91bmRhcnkgaW50ZXJydXB0ZWQgYWZ0ZXIgbGF1bmNoLgogICAgICAgIGJvdW5kYXJ5ID0gcmVjb3JkLmdl
dCgid3JpdGVfYm91bmRhcnkiIGlmIGNvZGUgPT0gIndyaXRlX2JvdW5kYXJ5X3JlZnVzZWQiIGVsc2UgInJlYWRf
b25seV9ib3VuZGFyeSIpCiAgICAgICAgaWYgKGNvZGUgaW4gKCJ3cml0ZV9ib3VuZGFyeV9yZWZ1c2VkIiwgImFn
eV9yZWFkX29ubHlfYm91bmRhcnkiKQogICAgICAgICAgICAgICAgYW5kIGlzaW5zdGFuY2UoYm91bmRhcnksIE1h
cHBpbmcpIGFuZCBib3VuZGFyeS5nZXQoInN0YXR1cyIpID09ICJyZWZ1c2VkIik6CiAgICAgICAgICAgIHJldHVy
biBTdGFydFJlZnVzYWwuQk9VTkRBUlkKICAgIGlmIGNvZGUgPT0gImFneV9tb2RlbF91bmF2YWlsYWJsZSI6CiAg
ICAgICAgcmV0dXJuIFN0YXJ0UmVmdXNhbC5NT0RFTF9VTkFWQUlMQUJMRSAgIyB0aGUgQUdZIHBhcnNlciBwcm92
ZWQgYSBjbGVhbiwgemVyby10dXJuIG1vZGVsIHNlbGVjdGlvbiBlcnJvcgogICAgaWYgZXhpdF9jb2RlIGlzIG5v
dCBOb25lIG9yIHJlY29yZC5nZXQoInByb3ZpZGVyX3BnaWQiKSBpcyBub3QgTm9uZToKICAgICAgICByZXR1cm4g
Tm9uZQogICAgbGF1bmNoX2NvZGUgPSByZWNvcmQuZ2V0KCJyZXR1cm5jb2RlX3JlYXNvbiIpCiAgICBpZiBsYXVu
Y2hfY29kZSBpbiAoImRpc3BhdGNoX2ZhbGxiYWNrX3JlZnVzZWQiLCAiZGlzcGF0Y2hfaXNvbGF0aW9uX2ZhaWxl
ZCIsICJ3b3JrZXJfc3Bhd25fZmFpbGVkIik6CiAgICAgICAgcmV0dXJuIFN0YXJ0UmVmdXNhbC5QTEFDRU1FTlQK
ICAgIGlmIGxhdW5jaF9jb2RlID09ICJzcGF3bl9zZXR1cF9mYWlsZWQiOgogICAgICAgIHJldHVybiBTdGFydFJl
ZnVzYWwuU0VUVVAKICAgIGlmIGlzaW5zdGFuY2UoZXJyb3IsIHN0cikgYW5kIGVycm9yLnN0YXJ0c3dpdGgoImZv
cm1hbCBhdHRlbXB0IGZpbGVzeXN0ZW0gYm91bmRhcnkgcmVmdXNlZDogcHJlcGFyZToiKToKICAgICAgICByZXR1
cm4gU3RhcnRSZWZ1c2FsLkJPVU5EQVJZCiAgICByZXR1cm4gX1BSRV9TVEFSVF9FUlJPUlMuZ2V0KGNvZGUpIGlm
IGNvZGUgaXMgbm90IE5vbmUgZWxzZSBOb25lCgoKZGVmIGxhYmVsKHZhbHVlOiBvYmplY3QsIGRlZmF1bHQ6IHN0
ciA9ICJ1bmtub3duIikgLT4gc3RyOgogICAgaWYgaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKSBhbmQgX0xBQkVMLmZ1
bGxtYXRjaCh2YWx1ZSkgYW5kICIuLiIgbm90IGluIHZhbHVlOgogICAgICAgIHJldHVybiB2YWx1ZQogICAgcmV0
dXJuIGRlZmF1bHQKCgpkZWYgbW9kZWxfbGFiZWwodmFsdWU6IG9iamVjdCwgZGVmYXVsdDogc3RyID0gInVua25v
d24iKSAtPiBzdHI6CiAgICAiIiJBIG1vZGVsIGlkLCBpbmNsdWRpbmcgYSBjYXRhbG9nIGlkIHRoYXQgY29udGFp
bnMgb25lIHNsYXNoLiIiIgogICAgaWYgaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKSBhbmQgX01PREVMX0xBQkVMLmZ1
bGxtYXRjaCh2YWx1ZSkgYW5kICIuLiIgbm90IGluIHZhbHVlOgogICAgICAgIHJldHVybiB2YWx1ZQogICAgcmV0
dXJuIGRlZmF1bHQKCgpkZWYgX251bWJlcih2YWx1ZTogb2JqZWN0KSAtPiBmbG9hdCB8IE5vbmU6CiAgICBpZiBp
c2luc3RhbmNlKHZhbHVlLCBib29sKSBvciBub3QgaXNpbnN0YW5jZSh2YWx1ZSwgaW50IHwgZmxvYXQpIG9yIG5v
dCBtYXRoLmlzZmluaXRlKHZhbHVlKSBvciB2YWx1ZSA8IDA6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHJldHVy
biBmbG9hdCh2YWx1ZSkKCgpkZWYgdGltZXN0YW1wKHZhbHVlOiBvYmplY3QpIC0+IGRhdGV0aW1lIHwgTm9uZToK
ICAgIGlmIG5vdCBpc2luc3RhbmNlKHZhbHVlLCBzdHIpIG9yIG5vdCB2YWx1ZToKICAgICAgICByZXR1cm4gTm9u
ZQogICAgdHJ5OgogICAgICAgIHBhcnNlZCA9IGRhdGV0aW1lLmZyb21pc29mb3JtYXQodmFsdWUucmVwbGFjZSgi
WiIsICIrMDA6MDAiKSkKICAgICAgICByZXR1cm4gKHBhcnNlZCBpZiBwYXJzZWQudHppbmZvIGVsc2UgcGFyc2Vk
LnJlcGxhY2UodHppbmZvPVVUQykpLmFzdGltZXpvbmUoVVRDKQogICAgZXhjZXB0IChWYWx1ZUVycm9yLCBPdmVy
Zmxvd0Vycm9yKTogICMgdW5wYXJzZWFibGUsIG9yIG91dHNpZGUgdGhlIGRhdGV0aW1lIHJhbmdlIG9uY2UgbW92
ZWQgdG8gVVRDCiAgICAgICAgcmV0dXJuIE5vbmUKCgpkZWYgdG9rZW5zKHVzYWdlOiBvYmplY3QpIC0+IHR1cGxl
W3R1cGxlW2ludCwgaW50LCBpbnRdLCBib29sXToKICAgICIiIk5vcm1hbGl6ZSBmcmVzaCBpbnB1dCwgY2FjaGUg
cmVhZHMgYW5kIG91dHB1dCwgYW5kIHNheSB3aGV0aGVyIGFsbCB0aHJlZSBhcmUga25vd24uIiIiCiAgICBpZiBu
b3QgaXNpbnN0YW5jZSh1c2FnZSwgTWFwcGluZyk6CiAgICAgICAgcmV0dXJuICgwLCAwLCAwKSwgRmFsc2UKCiAg
ICB2YWxpZCA9IFRydWUKCiAgICBkZWYgbihtYXBwaW5nOiBNYXBwaW5nW3N0ciwgQW55XSwga2V5OiBzdHIsICos
IG9wdGlvbmFsOiBib29sID0gRmFsc2UpIC0+IGludDoKICAgICAgICBub25sb2NhbCB2YWxpZAogICAgICAgIHZh
bHVlID0gbWFwcGluZy5nZXQoa2V5LCAwIGlmIG9wdGlvbmFsIGVsc2UgTm9uZSkKICAgICAgICBpZiBfY291bnRf
dmFsdWUodmFsdWUpOgogICAgICAgICAgICByZXR1cm4gY2FzdChpbnQsIHZhbHVlKQogICAgICAgIHZhbGlkID0g
RmFsc2UKICAgICAgICByZXR1cm4gMAoKICAgIHUgPSB1c2FnZQogICAgaWYgImNhY2hlX3JlYWRfaW5wdXRfdG9r
ZW5zIiBpbiB1OiAgIyBDbGF1ZGUKICAgICAgICBjb3VudHMgPSAobih1LCAiaW5wdXRfdG9rZW5zIikgKyBuKHUs
ICJjYWNoZV9jcmVhdGlvbl9pbnB1dF90b2tlbnMiLCBvcHRpb25hbD1UcnVlKSwKICAgICAgICAgICAgICAgICAg
bih1LCAiY2FjaGVfcmVhZF9pbnB1dF90b2tlbnMiKSwgbih1LCAib3V0cHV0X3Rva2VucyIpKQogICAgZWxpZiAi
Y2FjaGVkX2lucHV0X3Rva2VucyIgaW4gdTogICMgQ29kZXg6IGlucHV0IGluY2x1ZGVzIGNhY2hlIHJlYWRzLCBv
dXRwdXQgaW5jbHVkZXMgcmVhc29uaW5nCiAgICAgICAgZnJlc2gsIGNhY2hlZCA9IG4odSwgImlucHV0X3Rva2Vu
cyIpLCBuKHUsICJjYWNoZWRfaW5wdXRfdG9rZW5zIikKICAgICAgICBjb3VudHMgPSAobWF4KDAsIGZyZXNoIC0g
Y2FjaGVkKSwgY2FjaGVkLCBuKHUsICJvdXRwdXRfdG9rZW5zIikpCiAgICAgICAgdmFsaWQgPSB2YWxpZCBhbmQg
ZnJlc2ggPj0gY2FjaGVkCiAgICBlbGlmICJpbnB1dFRva2VucyIgaW4gdTogICMgQ3Vyc29yCiAgICAgICAgY291
bnRzID0gKG4odSwgImlucHV0VG9rZW5zIikgKyBuKHUsICJjYWNoZVdyaXRlVG9rZW5zIiksIG4odSwgImNhY2hl
UmVhZFRva2VucyIpLAogICAgICAgICAgICAgICAgICBuKHUsICJvdXRwdXRUb2tlbnMiKSArIG4odSwgInJlYXNv
bmluZ1Rva2VucyIsIG9wdGlvbmFsPVRydWUpKQogICAgZWxpZiAiY2FjaGVfcmVhZF90b2tlbnMiIGluIHU6ICAj
IEFHWQogICAgICAgIGNvdW50cyA9IChuKHUsICJpbnB1dF90b2tlbnMiKSwgbih1LCAiY2FjaGVfcmVhZF90b2tl
bnMiKSwKICAgICAgICAgICAgICAgICAgbih1LCAib3V0cHV0X3Rva2VucyIpICsgbih1LCAidGhpbmtpbmdfdG9r
ZW5zIiwgb3B0aW9uYWw9VHJ1ZSkpCiAgICBlbGlmIGlzaW5zdGFuY2UoY2FjaGUgOj0gdS5nZXQoImNhY2hlIiks
IE1hcHBpbmcpOiAgIyBPcGVuQ29kZQogICAgICAgIGNvdW50cyA9IChuKHUsICJpbnB1dCIpICsgbihjYWNoZSwg
IndyaXRlIiwgb3B0aW9uYWw9VHJ1ZSksIG4oY2FjaGUsICJyZWFkIiksCiAgICAgICAgICAgICAgICAgIG4odSwg
Im91dHB1dCIpICsgbih1LCAicmVhc29uaW5nIiwgb3B0aW9uYWw9VHJ1ZSkpCiAgICBlbHNlOgogICAgICAgIHJl
dHVybiAoMCwgMCwgMCksIEZhbHNlCiAgICBpZiBub3QgYWxsKF9jb3VudF92YWx1ZSh2YWx1ZSkgZm9yIHZhbHVl
IGluIGNvdW50cyk6CiAgICAgICAgcmV0dXJuICgwLCAwLCAwKSwgRmFsc2UKICAgIHJldHVybiBjb3VudHMsIHZh
bGlkCgoKQGxydV9jYWNoZShtYXhzaXplPTEpCmRlZiBtb2RlbF9jYXRhbG9nX3JlYWRpbmcoKSAtPiBkaWN0W3N0
ciwgQW55XSB8IE5vbmU6CiAgICAiIiJUaGUgbW9kZWwgY2F0YWxvZywgb3IgTm9uZSB3aGVuIGl0IGNhbm5vdCBi
ZSByZWFkIChtb2RlbHMgYXJlIHRoZW4gc3RvcmVkIGFuZCBjb3VudGVkIGFzIHJlY29yZGVkKS4KCiAgICBPbmUg
cmVhZGluZywga2VwdCB1bnRpbCBgYHJlZnJlc2hfcmVhZGluZ3NgYDogZXZlcnkgc3RhdGlzdGljcyBjb25zdW1l
ciBvZiB0aGUgY2F0YWxvZyByZWFkcyBpdCBoZXJlLCBzbyB3aXRoaW4KICAgIG9uZSBjb2xsZWN0aW9uIHRoZXkg
YWxsIHNlZSB0aGUgc2FtZSBjYXRhbG9nLCBvciBhbGwgc2VlIG5vbmUuCiAgICAiIiIKICAgIHRyeToKICAgICAg
ICByZXR1cm4gbG9hZF9tb2RlbF9jYXRhbG9nKCkKICAgIGV4Y2VwdCBFeGNlcHRpb246ICAjIG5vIGNhdGFsb2c6
IG1vZGVscyBhcmUgc3RvcmVkIGFzIHJlY29yZGVkCiAgICAgICAgcmV0dXJuIE5vbmUKCgpkZWYgY3VycmVudF9j
YXRhbG9nX21vZGVsX2lkcyhjYXRhbG9nOiBNYXBwaW5nW3N0ciwgQW55XSB8IE5vbmUpIC0+IGZyb3plbnNldFtz
dHJdIHwgTm9uZToKICAgICIiIk5vbi1yZXRpcmVkIGNhdGFsb2cgSURzIGZvciBkaXNwbGF5LCBvciBOb25lIHdo
ZW4gdGhlIGNhdGFsb2cgaXMgdW5hdmFpbGFibGUuIiIiCiAgICBpZiBjYXRhbG9nIGlzIE5vbmU6CiAgICAgICAg
cmV0dXJuIE5vbmUKICAgIHJldHVybiBmcm96ZW5zZXQobW9kZWxfaWQgZm9yIG1vZGVsX2lkLCBpbmZvIGluIGNh
dGFsb2dbIm1vZGVscyJdLml0ZW1zKCkKICAgICAgICAgICAgICAgICAgICAgaWYgaW5mby5nZXQoImxpZmVjeWNs
ZSIpICE9ICJyZXRpcmVkIiBhbmQgbW9kZWxfbGFiZWwobW9kZWxfaWQsICIiKSkKCgpAbHJ1X2NhY2hlKG1heHNp
emU9MjU2KQpkZWYgcmV0aXJlZF9zdWJzY3JpcHRpb25fbW9kZWwodmFsdWU6IHN0cikgLT4gYm9vbDoKICAgICIi
IkdMTSBhbmQgS2ltaSBsYWJlbHMsIGluY2x1ZGluZyBjYXRhbG9nIGFsaWFzZXMgYW5kIHByZS1jYXRhbG9nIGxl
ZGdlciBzcGVsbGluZ3MuIiIiCiAgICByZXR1cm4gYm9vbChfUkVUSVJFRF9TVUJTQ1JJUFRJT05fTU9ERUwuc2Vh
cmNoKHZhbHVlKSBvciBfUkVUSVJFRF9TVUJTQ1JJUFRJT05fTU9ERUwuc2VhcmNoKG1vZGVsX2lkKHZhbHVlKSkp
CgoKZGVmIF9zcGVsbGluZyhyYXc6IG9iamVjdCkgLT4gc3RyOgogICAgIiIiQSBtb2RlbCBzcGVsbGluZyB3aXRo
b3V0IGl0cyBicmFja2V0ZWQgY29udGV4dCBzdWZmaXggKGBgY2xhdWRlLW9wdXMtNS01WzFtXWBgKS4iIiIKICAg
IHJldHVybiByYXcuc3BsaXQoIlsiLCAxKVswXS5zdHJpcCgpIGlmIGlzaW5zdGFuY2UocmF3LCBzdHIpIGVsc2Ug
IiIKCgpkZWYgbW9kZWxfaWQocmF3OiBvYmplY3QpIC0+IHN0cjoKICAgICIiIlRoZSBjYXRhbG9nIGlkIGEgc3Bl
bGxpbmcgbmFtZXMsIGVsc2UgdGhlIHNwZWxsaW5nIGl0c2VsZjsgbmV2ZXIgYSBmYW1pbHkgb3IgYSBndWVzc2Vk
IHZlcnNpb24uCgogICAgQSBiYXJlIGhhcm5lc3MgYWxpYXMgKGBgb3B1c2BgKSBuYW1lcyBubyB2ZXJzaW9uLCBz
byBpdCBzdGF5cyBgYG9wdXNgYC4gQSBDdXJzb3IgZGlzcGxheSBuYW1lIG1hcHMgdGhyb3VnaAogICAgYGBjYW5v
bmljYWxfY3Vyc29yX21vZGVsYGAgZmlyc3QsIGFuZCBvbmx5IGZvciB0aGUgc3BlbGxpbmdzIHRoYXQgZnVuY3Rp
b24gYWRtaXRzLiBUaGUgY2F0YWxvZyBtYXRjaCBydW5zIG9uCiAgICB0aGUgcmF3IHNwZWxsaW5nOiBhIGhhcm5l
c3MgcHJlZml4IChgYGRlZXBzZWVrLWRpcmVjdC9kZWVwc2Vlay12NC1mbGFzaGBgKSBvciBhIGNhdGFsb2cgaWQg
dGhhdCBpdHNlbGYKICAgIGNvbnRhaW5zIGEgc2xhc2ggKGBga2ltaS1jb2RlL2szLTI1NmtgYCkgdXNlZCB0byBk
aWUgaW4gdGhlIGxhYmVsIGdyYW1tYXIgYW5kIGJlIHN0b3JlZCBhcyBgYHVua25vd25gYC4KICAgICIiIgogICAg
c3BlbGxpbmcgPSBfc3BlbGxpbmcocmF3KQogICAgaWYgbm90IHNwZWxsaW5nOgogICAgICAgIHJldHVybiAidW5r
bm93biIKICAgIGRpc3BsYXllZCA9IGNhbm9uaWNhbF9jdXJzb3JfbW9kZWwoc3BlbGxpbmcpCiAgICBpZiBkaXNw
bGF5ZWQ6CiAgICAgICAgc3BlbGxpbmcgPSBkaXNwbGF5ZWQKICAgIGNhdGFsb2cgPSBtb2RlbF9jYXRhbG9nX3Jl
YWRpbmcoKQogICAgaWYgY2F0YWxvZyBpcyBub3QgTm9uZSBhbmQgKG1hdGNoZWQgOj0gY2Fub25pY2FsX21vZGVs
X2lkKHNwZWxsaW5nLCBjYXRhbG9nKSk6CiAgICAgICAgcmV0dXJuIG1vZGVsX2xhYmVsKG1hdGNoZWQpCiAgICBy
ZXR1cm4gbW9kZWxfbGFiZWwoc3BlbGxpbmcpCgoKZGVmIGNvbmNyZXRlX21vZGVsKHJlY29yZDogTWFwcGluZ1tz
dHIsIEFueV0pIC0+IHN0cjoKICAgICIiIlRoZSBwcm92aWRlci1vYnNlcnZlZCBtb2RlbCB3aGVuIHRoZSBydW50
aW1lIGF0dGVzdGVkIG9uZSwgZWxzZSB0aGUgcmVxdWVzdGVkIG1vZGVsJ3MgY2F0YWxvZyBpZC4KCiAgICBUaGUg
YXR0ZXN0ZWQgc3BlbGxpbmcgZ29lcyB0aHJvdWdoIGBgbW9kZWxfaWRgYCBkaXJlY3RseS4gQSBkaXNwbGF5IG5h
bWUgdGhlIGxhYmVsIGdyYW1tYXIgd291bGQgcmVqZWN0CiAgICAoYSBzcGFjZSwgb3Igb25lIHNsYXNoKSBpcyBz
dGlsbCBhIGNhdGFsb2cgaWQgd2hlbiB0aGUgbWFwIGFkbWl0cyBpdCwgYW5kIHN0YXlzIGBgdW5rbm93bmBgIHdo
ZW4gaXQgZG9lcyBub3QuCiAgICBUaGUgcmVxdWVzdGVkIG1vZGVsIGlzIG5vdCBhIGZhbGxiYWNrIGZvciBhbiBh
dHRlc3RhdGlvbiB0aGUgY2F0YWxvZyBkb2VzIG5vdCBuYW1lLgogICAgIiIiCiAgICBpZiByZWNvcmQuZ2V0KCJy
ZXNvbHZlZF9tb2RlbF9rbm93biIpIGlzIFRydWU6CiAgICAgICAgcmV0dXJuIG1vZGVsX2lkKHJlY29yZC5nZXQo
InJlc29sdmVkX21vZGVsIikpCiAgICByZXR1cm4gbW9kZWxfaWQocmVjb3JkLmdldCgibW9kZWwiKSkKCgpkZWYg
X29wdGlvbnMocmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSkgLT4gTWFwcGluZ1tzdHIsIEFueV06CiAgICBvcHRp
b25zID0gcmVjb3JkLmdldCgib3B0aW9ucyIpCiAgICByZXR1cm4gb3B0aW9ucyBpZiBpc2luc3RhbmNlKG9wdGlv
bnMsIE1hcHBpbmcpIGVsc2Uge30KCgpkZWYgdmFyaWFudF9lZmZvcnQocmF3OiBvYmplY3QpIC0+IHN0ciB8IE5v
bmU6CiAgICAiIiJUaGUgc3VmZml4IG9mIGEgY2F0YWxvZyBtb2RlbCBzZWxlY3Rvciwgc3VjaCBhcyBgYGdlbWlu
aS00LW5lb24tbWVkaXVtYGAuIiIiCiAgICBzcGVsbGluZyA9IF9zcGVsbGluZyhyYXcpLmxvd2VyKCkKICAgIG1h
dGNoID0gX0VGRk9SVF9TVUZGSVguc2VhcmNoKHNwZWxsaW5nKQogICAgaWYgbWF0Y2ggYW5kIG1vZGVsX2lkKHNw
ZWxsaW5nKSBub3QgaW4ge3NwZWxsaW5nLCAidW5rbm93biJ9OgogICAgICAgIHJldHVybiBtYXRjaC5ncm91cCgx
KQogICAgcmV0dXJuIE5vbmUKCgojIEhhcm5lc3NlcyB0aGF0IHBhc3MgYW4gZXhwbGljaXQgZWZmb3J0IGZsYWcg
c2VwYXJhdGVseSBmcm9tIHRoZSBtb2RlbCBzZWxlY3Rvci4KIyBGb3IgT3BlbkNvZGUsIE1vZGVsIFN0YXRzIHVz
ZXMgdGhlIHJlcXVlc3RlZCBmbGFnIGVmZm9ydCBvbmx5IGZvciBhIG1hdGNoaW5nIFN1YlEgU2F2YW50IGF0dGVz
dGF0aW9uLgojIERlZXBTZWVrIHJvdXRlcyBwYXNzIHRoZSByZXF1ZXN0ZWQgZWZmb3J0IHZpYSAtLXZhcmlhbnQs
IG9yIHVzZSB0aGUgZW5kcG9pbnQgZGVmYXVsdCB3aGVuIG9taXR0ZWQuCl9FRkZPUlRfRkxBR19BR0VOVFMgPSBm
cm96ZW5zZXQoeyJjbGF1ZGUiLCAiY29kZXgiLCAibXVzZSJ9KQoKCmRlZiBfbmV3ZXN0X2NsYXVkZV9pZChmYW1p
bHk6IHN0cikgLT4gc3RyIHwgTm9uZToKICAgICIiIlRoZSBjb25jcmV0ZSBtb2RlbCBhIG5hdGl2ZSBDbGF1ZGUg
YWxpYXMgbmFtZXMgaW4gdGhlIGN1cnJlbnQgY2F0YWxvZyByZWFkaW5nLiIiIgogICAgY2F0YWxvZyA9IG1vZGVs
X2NhdGFsb2dfcmVhZGluZygpCiAgICBtb2RlbHMgPSBjYXRhbG9nLmdldCgibW9kZWxzIikgaWYgaXNpbnN0YW5j
ZShjYXRhbG9nLCBNYXBwaW5nKSBlbHNlIE5vbmUKICAgIGlmIG5vdCBpc2luc3RhbmNlKG1vZGVscywgTWFwcGlu
Zyk6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHByZWZpeCA9IGYiY2xhdWRlLXtmYW1pbHl9LSIKCiAgICBkZWYg
dmVyc2lvbihtaWQ6IHN0cikgLT4gdHVwbGVbaW50LCAuLi5dOgogICAgICAgIHJldHVybiB0dXBsZShpbnQocGFy
dCkgZm9yIHBhcnQgaW4gbWlkW2xlbihwcmVmaXgpOl0uc3BsaXQoIi0iKSBpZiBwYXJ0LmlzZGlnaXQoKSkKCiAg
ICBpZHMgPSBbCiAgICAgICAgbWlkCiAgICAgICAgZm9yIG1pZCwgaW5mbyBpbiBtb2RlbHMuaXRlbXMoKQogICAg
ICAgIGlmIGlzaW5zdGFuY2UobWlkLCBzdHIpCiAgICAgICAgYW5kIG1pZC5zdGFydHN3aXRoKHByZWZpeCkKICAg
ICAgICBhbmQgaXNpbnN0YW5jZShpbmZvLCBNYXBwaW5nKQogICAgICAgIGFuZCBpbmZvLmdldCgiZmFtaWx5Iikg
PT0gImFudGhyb3BpYyIKICAgIF0KICAgIHJldHVybiBtYXgoaWRzLCBrZXk9dmVyc2lvbikgaWYgaWRzIGVsc2Ug
Tm9uZQoKCmRlZiBfdXNlc19lZmZvcnRfZmxhZyhyZWNvcmQ6IE1hcHBpbmdbc3RyLCBBbnldKSAtPiBib29sOgog
ICAgcmF3X2FnZW50ID0gcmVjb3JkLmdldCgiYWdlbnQiKQogICAgaWYgbm90IGlzaW5zdGFuY2UocmF3X2FnZW50
LCBzdHIpOgogICAgICAgIHJldHVybiBGYWxzZQogICAgYWdlbnQgPSByYXdfYWdlbnQuc3RyaXAoKS5sb3dlcigp
CiAgICBpZiBhZ2VudCBpbiBfRUZGT1JUX0ZMQUdfQUdFTlRTOgogICAgICAgIHJldHVybiBUcnVlCiAgICBpZiBh
Z2VudCA9PSAib3BlbmNvZGUiOgogICAgICAgIHByb3ZpZGVyID0gcmVjb3JkLmdldCgicHJvdmlkZXIiKSBpZiBy
ZWNvcmQuZ2V0KCJwcm92aWRlciIpIGlzIG5vdCBOb25lIGVsc2UgX29wdGlvbnMocmVjb3JkKS5nZXQoInByb3Zp
ZGVyIikKICAgICAgICByZXR1cm4gaXNpbnN0YW5jZShwcm92aWRlciwgc3RyKSBhbmQgcHJvdmlkZXIuc3RyaXAo
KS5sb3dlcigpID09ICJzdWJxIgogICAgcmV0dXJuIEZhbHNlCgoKZGVmIF9vYnNlcnZlZF9tYXRjaGVzX3JlcXVl
c3QocmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSwgb2JzZXJ2ZWRfaWQ6IHN0ciwgcmVxdWVzdGVkX2lkOiBzdHIp
IC0+IGJvb2w6CiAgICBpZiByZXF1ZXN0ZWRfaWQgPT0gInVua25vd24iIG9yIG9ic2VydmVkX2lkLmNhc2Vmb2xk
KCkgaW4gTkFUSVZFX0FMSUFTRVM6CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBpZiBvYnNlcnZlZF9pZCA9PSBy
ZXF1ZXN0ZWRfaWQ6CiAgICAgICAgcmV0dXJuIFRydWUKICAgIHJhd19hZ2VudCA9IHJlY29yZC5nZXQoImFnZW50
IikKICAgIGlmIGlzaW5zdGFuY2UocmF3X2FnZW50LCBzdHIpIGFuZCByYXdfYWdlbnQuc3RyaXAoKS5sb3dlcigp
ID09ICJjbGF1ZGUiOgogICAgICAgIGFsaWFzID0gcmVxdWVzdGVkX2lkLmNhc2Vmb2xkKCkKICAgICAgICBpZiBh
bGlhcyBpbiBOQVRJVkVfQUxJQVNFUzoKICAgICAgICAgICAgdGFyZ2V0ID0gX25ld2VzdF9jbGF1ZGVfaWQoYWxp
YXMpCiAgICAgICAgICAgIHJldHVybiB0YXJnZXQgaXMgbm90IE5vbmUgYW5kIG9ic2VydmVkX2lkID09IHRhcmdl
dAogICAgcmV0dXJuIEZhbHNlCgoKZGVmIGVmZmVjdGl2ZV9lZmZvcnQocmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55
XSkgLT4gc3RyOgogICAgIiIiVXNlIHRoZSBydW50aW1lIHNlbGVjdG9yIHdoZW4gb2JzZXJ2ZWQsIHRoZW4gYSBt
YXRjaGluZyBzZWxlY3RvciBvciB2ZXJpZmllZCBmbGFnIGVmZm9ydC4iIiIKICAgIG9ic2VydmVkX2tub3duID0g
cmVjb3JkLmdldCgicmVzb2x2ZWRfbW9kZWxfa25vd24iKSBpcyBUcnVlCiAgICBvYnNlcnZlZCA9IF9zcGVsbGlu
ZyhyZWNvcmQuZ2V0KCJyZXNvbHZlZF9tb2RlbCIpKSBpZiBvYnNlcnZlZF9rbm93biBlbHNlICIiCiAgICByZXF1
ZXN0ZWQgPSByZWNvcmQuZ2V0KCJtb2RlbCIpCiAgICBpZiBsZXZlbCA6PSB2YXJpYW50X2VmZm9ydChvYnNlcnZl
ZCk6CiAgICAgICAgcmV0dXJuIGxldmVsCiAgICBpZiBvYnNlcnZlZF9rbm93bjoKICAgICAgICBvYnNlcnZlZF9p
ZCA9IG1vZGVsX2lkKG9ic2VydmVkKQogICAgICAgIGlmIG9ic2VydmVkX2lkLmNhc2Vmb2xkKCkgaW4geyJ1bmtu
b3duIiwgInVucmVjb2duaXplZCJ9OgogICAgICAgICAgICByZXR1cm4gVU5TRVQKICAgICAgICByZXF1ZXN0ZWRf
aWQgPSBtb2RlbF9pZChyZXF1ZXN0ZWQpCiAgICAgICAgcmVxX2xldmVsID0gdmFyaWFudF9lZmZvcnQocmVxdWVz
dGVkKQogICAgICAgIGlmIHJlcV9sZXZlbCBpcyBub3QgTm9uZToKICAgICAgICAgICAgIyBBIG1hcHBlZCBDdXJz
b3IgZGlzcGxheSBuYW1lIChzdWNoIGFzICJHcm9rIDQuNyAyNTZLIEhpZ2giKSBjYXJyaWVzIHRoZSB2YXJpYW50
IGFzIGEKICAgICAgICAgICAgIyB0cmFpbGluZyBzcGFjZS1zZXBhcmF0ZWQgd29yZCByYXRoZXIgdGhhbiBhIGh5
cGhlbiBzdWZmaXg7IGFueSBvdGhlciBiYXJlIGF0dGVzdGF0aW9uCiAgICAgICAgICAgICMgb3IgYSBkaWZmZXJl
bnQgYXR0ZXN0ZWQgbW9kZWwgbmV2ZXIgaW5oZXJpdHMgdGhlIHJlcXVlc3RlZCBzZWxlY3RvcidzIGVmZm9ydC4K
ICAgICAgICAgICAgaWYgKGNhbm9uaWNhbF9jdXJzb3JfbW9kZWwob2JzZXJ2ZWQpICE9IG9ic2VydmVkIGFuZCBv
YnNlcnZlZF9pZCA9PSByZXF1ZXN0ZWRfaWQKICAgICAgICAgICAgICAgICAgICBhbmQgcmUuc2VhcmNoKHJmIlsg
XHRde3JlLmVzY2FwZShyZXFfbGV2ZWwpfVxaIiwgb2JzZXJ2ZWQsIHJlLkFTQ0lJIHwgcmUuSUdOT1JFQ0FTRSkp
OgogICAgICAgICAgICAgICAgcmV0dXJuIHJlcV9sZXZlbAogICAgICAgICAgICByZXR1cm4gVU5TRVQKICAgICAg
ICAjIFJlcXVpcmUgcG9zaXRpdmUgZXZpZGVuY2UgYmVmb3JlIGluaGVyaXRpbmcgYSByZXF1ZXN0IGVmZm9ydCBm
bGFnIG9uIGFuIGF0dGVzdGVkIHJ1bjoKICAgICAgICAjIHRoZSBoYXJuZXNzIG11c3QgcGFzcyBhIHNlcGFyYXRl
IGVmZm9ydCBmbGFnIGFuZCB0aGUgYXR0ZXN0ZWQgbW9kZWwgbXVzdCBtYXRjaCB0aGUgcmVxdWVzdC4KICAgICAg
ICBpZiBub3QgKF91c2VzX2VmZm9ydF9mbGFnKHJlY29yZCkgYW5kIF9vYnNlcnZlZF9tYXRjaGVzX3JlcXVlc3Qo
cmVjb3JkLCBvYnNlcnZlZF9pZCwgcmVxdWVzdGVkX2lkKSk6CiAgICAgICAgICAgIHJldHVybiBVTlNFVAogICAg
ZWxpZiBsZXZlbCA6PSB2YXJpYW50X2VmZm9ydChyZXF1ZXN0ZWQpOgogICAgICAgIHJldHVybiBsZXZlbAogICAg
b3B0aW9ucyA9IF9vcHRpb25zKHJlY29yZCkKICAgIGZvciB2YWx1ZSBpbiAocmVjb3JkLmdldCgiZWZmb3J0Iiks
IG9wdGlvbnMuZ2V0KCJlZmZvcnQiKSk6CiAgICAgICAgaWYgaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKSBhbmQgdmFs
dWUuc3RyaXAoKToKICAgICAgICAgICAgcmV0dXJuIGxhYmVsKHZhbHVlLnN0cmlwKCkubG93ZXIoKSkKICAgIHJl
dHVybiBVTlNFVAoKCmRlZiBjb21wYXJpc29uX2VmZm9ydCh2YWx1ZTogb2JqZWN0KSAtPiBzdHI6CiAgICAiIiJB
IHJlY29yZGVkIGVmZm9ydCBsZXZlbCwgb3IgdGhlIGNvbW1vbiBidWNrZXQgZm9yIHVucmVwb3J0ZWQgYW5kIGlu
dmFsaWQgbGV2ZWxzLiIiIgogICAgZWZmb3J0ID0gdmFsdWUuc3RyaXAoKS5sb3dlcigpIGlmIGlzaW5zdGFuY2Uo
dmFsdWUsIHN0cikgZWxzZSAiIgogICAgcmV0dXJuIGVmZm9ydCBpZiBWQVJJQU5UX1NVRkZJWC5mdWxsbWF0Y2go
ZWZmb3J0KSBlbHNlIFVOS05PV05fRUZGT1JUCgoKZGVmIF9yZXZpZXdfZmxhZ3MocmVjb3JkOiBNYXBwaW5nW3N0
ciwgQW55XSkgLT4gZGljdFtzdHIsIEFueV06CiAgICAiIiJDb3B5IG9mIGBgcmVjb3JkYGAgd2hvc2UgZW1wdHkg
cmV2aWV3IGZsYWdzIGFyZSBmaWxsZWQgZnJvbSBpdHMgb3B0aW9ucy4KCiAgICBQcmVjZWRlbmNlIGZvciBlYWNo
IDpkYXRhOmBSRVZJRVdfVEFTS19GTEFHX0ZJRUxEU2AgbmFtZSBpcyBhIG5vbi1lbXB0eSB0b3AtbGV2ZWwgdmFs
dWUsCiAgICB0aGVuIHRoZSBzYW1lIGtleSBvbiBvcHRpb25zLiBgYHJldmlld19yaXNrYGAgYWxzbyBhY2NlcHRz
IGBgb3B0aW9ucy5yaXNrYGAgd2hlbgogICAgYGBvcHRpb25zLnJldmlld19yaXNrYGAgaXMgZW1wdHkuIEEgcHJl
c2VudCBudWxsIGlzIGVtcHR5OiBgYGRpY3QuZ2V0YGAncyBkZWZhdWx0IGlzIG5vdC4KICAgICIiIgogICAgc3Vi
amVjdCA9IGRpY3QocmVjb3JkKQogICAgb3B0aW9ucyA9IF9vcHRpb25zKHJlY29yZCkKICAgIGZvciBuYW1lIGlu
IFJFVklFV19UQVNLX0ZMQUdfRklFTERTOgogICAgICAgIGlmIHN1YmplY3QuZ2V0KG5hbWUpIGluIChOb25lLCBG
YWxzZSwgIiIsIFtdKToKICAgICAgICAgICAgdmFsdWUgPSBvcHRpb25zLmdldChuYW1lKQogICAgICAgICAgICBp
ZiB2YWx1ZSBpbiAoTm9uZSwgRmFsc2UsICIiLCBbXSkgYW5kIG5hbWUgPT0gInJldmlld19yaXNrIjoKICAgICAg
ICAgICAgICAgIHZhbHVlID0gb3B0aW9ucy5nZXQoInJpc2siKQogICAgICAgICAgICBpZiB2YWx1ZSBub3QgaW4g
KE5vbmUsIEZhbHNlLCAiIiwgW10pOgogICAgICAgICAgICAgICAgc3ViamVjdFtuYW1lXSA9IHZhbHVlCiAgICBy
ZXR1cm4gc3ViamVjdAoKCmRlZiBfdHlwZWRfcmV2aWV3KHJlY29yZDogTWFwcGluZ1tzdHIsIEFueV0pIC0+IGJv
b2w6CiAgICAiIiJXaGV0aGVyIGBgcmV2aWV3X3R5cGVkYGAgdHlwZXMgdGhpcyB0YXNrLCBpbmNsdWRpbmcgYSBm
bGFnIHN0b3JlZCBvbmx5IG9uIGl0cyBvcHRpb25zLiIiIgogICAgcmV0dXJuIHJldmlld190eXBlZChfcmV2aWV3
X2ZsYWdzKHJlY29yZCkpCgoKZGVmIF92YWxpZGF0ZWRfcmV2aWV3KHJldmlldzogTWFwcGluZ1tzdHIsIEFueV0g
fCBOb25lKSAtPiBib29sOgogICAgIiIiQSBwYXJzZWQgdmVyZGljdCBpcyByZXZpZXcgZXZpZGVuY2UuIEFuIHVu
cGFyc2VkIG9yIG1pc3NpbmcgcmVwbHkgaXMgbm90LiIiIgogICAgaWYgbm90IGlzaW5zdGFuY2UocmV2aWV3LCBN
YXBwaW5nKToKICAgICAgICByZXR1cm4gRmFsc2UKICAgIHZlcmRpY3QgPSByZXZpZXcuZ2V0KCJ2ZXJkaWN0IikK
ICAgIHJldHVybiBpc2luc3RhbmNlKHZlcmRpY3QsIHN0cikgYW5kIHZlcmRpY3QgaW4gVkVSRElDVFNbOjNdCgoK
ZGVmIHJvbGVfb2YocmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSwgcmV2aWV3OiBNYXBwaW5nW3N0ciwgQW55XSB8
IE5vbmUgPSBOb25lKSAtPiBzdHI6CiAgICAiIiJSZXZpZXcgb25seSB3aGVuIHRoZSBkaXNwYXRjaCBjbGFzc2lm
aWVyIHR5cGVzIHRoZSB0YXNrLCBvciBpdHMgcmVwbHkgaXMgYSB2YWxpZGF0ZWQgcmV2aWV3LgoKICAgIFJlYWQt
b25seSBtb2RlIGFuZCBhIHRhc2sgaWQgdGhhdCBjb250YWlucyBgYHJldmlld2BgIGFyZSBub3QgYSByZXZpZXcu
IEEgdmFsaWRhdGVkIHJlcGx5CiAgICBpcyByZXZpZXcgZXZpZGVuY2Ugb24gaXRzIG93biwgd2hpY2hldmVyIG1v
ZGUgdGhlIHJlY29yZCBoYXMuIGBgcmV2aWV3YGAgaXMgdGhlIHBhcnNlZAogICAgcmVwbHkgZnJvbSA6ZnVuYzpg
X3Jldmlld2AsIHdoZW4gdGhlIGNhbGxlciBoYXMgb25lLgogICAgIiIiCiAgICBpZiBfdHlwZWRfcmV2aWV3KHJl
Y29yZCkgb3IgX3ZhbGlkYXRlZF9yZXZpZXcocmV2aWV3KToKICAgICAgICByZXR1cm4gInJldmlldyIKICAgIHJl
dHVybiAid3JpdGUiCgoKQGxydV9jYWNoZShtYXhzaXplPTEpCmRlZiBfZmxlZXRfcmVwb192aXNpYmlsaXR5KCkg
LT4gdHVwbGVbZnJvemVuc2V0W3N0cl0sIGRpY3Rbc3RyLCBzdHJdLCBzdHIgfCBOb25lXToKICAgICIiIihwdWJs
aWNfa2V5cywgZ2l0aHViX3NsdWdfdG9fa2V5LCBkZWZhdWx0X2tleSk7IGZhaWwgY2xvc2VkIHRvIGVtcHR5IHdo
ZW4gdW5yZWFkYWJsZS4iIiIKICAgIHRyeToKICAgICAgICByZXBvcyA9IGxvYWRfZmxlZXRfcmVwb3MoKQogICAg
ZXhjZXB0IEV4Y2VwdGlvbjogICMgZmFpbCBjbG9zZWQ6IGFuIHVucmVhZGFibGUgcmVwbyBjYXRhbG9nIGFkbWl0
cyBubyByZXBseSBzYW1wbGVzCiAgICAgICAgcmV0dXJuIGZyb3plbnNldCgpLCB7fSwgTm9uZQogICAgcHVibGlj
X2tleXMgPSBmcm96ZW5zZXQoa2V5IGZvciBrZXksIHJlcG8gaW4gcmVwb3MuaXRlbXMoKSBpZiByZXBvLnJvbGUu
c3RhcnRzd2l0aCgicHVibGljLSIpKQogICAgYnlfZ2l0aHViID0gcmVwb3NpdG9yeV9rZXlzKHJlcG9zKQogICAg
ZGVmYXVsdF9rZXkgPSBuZXh0KChrZXkgZm9yIGtleSwgcmVwbyBpbiByZXBvcy5pdGVtcygpIGlmIHJlcG8uZGVm
YXVsdCksIE5vbmUpCiAgICByZXR1cm4gcHVibGljX2tleXMsIGJ5X2dpdGh1YiwgZGVmYXVsdF9rZXkKCgpAbHJ1
X2NhY2hlKG1heHNpemU9MSkKZGVmIF9wdWJsaWNfZGF0YV9jbGFzc2VzKCkgLT4gZnJvemVuc2V0W3N0cl06CiAg
ICAiIiJFeGFjdCBjYXRhbG9ndWVkIHB1YmxpYyBkYXRhIGNsYXNzZXMgKGBgc3RvcmU6IHRydWVgYCk7IGZhaWwg
Y2xvc2VkIHRvIGVtcHR5IHdoZW4gdW5yZWFkYWJsZS4iIiIKICAgIGNhdGFsb2cgPSBtb2RlbF9jYXRhbG9nX3Jl
YWRpbmcoKQogICAgbW9kZWxzID0gY2F0YWxvZy5nZXQoIm1vZGVscyIpIGlmIGlzaW5zdGFuY2UoY2F0YWxvZywg
TWFwcGluZykgZWxzZSBOb25lCiAgICBpZiBub3QgaXNpbnN0YW5jZShtb2RlbHMsIE1hcHBpbmcpOgogICAgICAg
IHJldHVybiBmcm96ZW5zZXQoKQogICAgYWRtaXR0ZWQ6IHNldFtzdHJdID0gc2V0KCkKICAgIGZvciBpbmZvIGlu
IG1vZGVscy52YWx1ZXMoKToKICAgICAgICBjbGFzc2VzID0gaW5mby5nZXQoImRhdGFfY2xhc3NlcyIpIGlmIGlz
aW5zdGFuY2UoaW5mbywgTWFwcGluZykgZWxzZSBOb25lCiAgICAgICAgaWYgaXNpbnN0YW5jZShjbGFzc2VzLCBN
YXBwaW5nKToKICAgICAgICAgICAgYWRtaXR0ZWQudXBkYXRlKAogICAgICAgICAgICAgICAgbmFtZSBmb3IgbmFt
ZSwgYWxsb3dlZCBpbiBjbGFzc2VzLml0ZW1zKCkKICAgICAgICAgICAgICAgIGlmIGlzaW5zdGFuY2UobmFtZSwg
c3RyKSBhbmQgYWxsb3dlZCBpcyBUcnVlCiAgICAgICAgICAgICkKICAgIHJldHVybiBmcm96ZW5zZXQoYWRtaXR0
ZWQpCgoKZGVmIF9yZXBvX2lzX3B1YmxpYyhyZXBvOiBvYmplY3QpIC0+IGJvb2w6CiAgICBwdWJsaWNfa2V5cywg
XywgXyA9IF9mbGVldF9yZXBvX3Zpc2liaWxpdHkoKQogICAgcmV0dXJuIGlzaW5zdGFuY2UocmVwbywgc3RyKSBh
bmQgcmVwbyBpbiBwdWJsaWNfa2V5cwoKCmRlZiBfcmVjb3JkX2FsbG93c19zYW1wbGUocmVjb3JkOiBNYXBwaW5n
W3N0ciwgQW55XSkgLT4gYm9vbDoKICAgICIiIldoZXRoZXIgYSBmYWlsaW5nIHJlcGx5IGZyb20gYGByZWNvcmRg
YCBtYXkgZ2l2ZSBhIGJvdW5kZWQgc3RhdHMgc2FtcGxlIChleGFjdCBhbGxvd2xpc3RzLCBmYWlsIGNsb3NlZCku
IiIiCiAgICBwdWJsaWNfa2V5cywgYnlfZ2l0aHViLCBkZWZhdWx0X2tleSA9IF9mbGVldF9yZXBvX3Zpc2liaWxp
dHkoKQogICAgaWYgbm90IHB1YmxpY19rZXlzOgogICAgICAgIHJldHVybiBGYWxzZQogICAgb3B0aW9ucyA9IF9v
cHRpb25zKHJlY29yZCkKICAgIGRlY2xhcmVkX3JlcG9zID0gW3ZhbHVlIGZvciB2YWx1ZSBpbiAocmVjb3JkLmdl
dCgicmVwbyIpLCBvcHRpb25zLmdldCgicmVwbyIpKSBpZiB2YWx1ZSBpcyBub3QgTm9uZV0KICAgIHJlc29sdmVk
X3JlcG86IHN0ciB8IE5vbmUgPSBOb25lCiAgICBmb3IgdmFsdWUgaW4gZGVjbGFyZWRfcmVwb3M6CiAgICAgICAg
aWYgbm90IGlzaW5zdGFuY2UodmFsdWUsIHN0cikgb3IgdmFsdWUgbm90IGluIHB1YmxpY19rZXlzOgogICAgICAg
ICAgICByZXR1cm4gRmFsc2UKICAgICAgICBpZiByZXNvbHZlZF9yZXBvIGlzIG5vdCBOb25lIGFuZCB2YWx1ZSAh
PSByZXNvbHZlZF9yZXBvOgogICAgICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICByZXNvbHZlZF9yZXBvID0g
dmFsdWUKICAgIHNsdWdzID0gWwogICAgICAgIHZhbHVlIGZvciB2YWx1ZSBpbiAoCiAgICAgICAgICAgIHJlY29y
ZC5nZXQoInJlcG9zaXRvcnkiKSwgcmVjb3JkLmdldCgicmVwb3NpdG9yeV9pZCIpLAogICAgICAgICAgICBvcHRp
b25zLmdldCgicmVwb3NpdG9yeSIpLCBvcHRpb25zLmdldCgicmVwb3NpdG9yeV9pZCIpLAogICAgICAgICkKICAg
ICAgICBpZiB2YWx1ZSBpcyBub3QgTm9uZQogICAgXQogICAgZm9yIHNsdWcgaW4gc2x1Z3M6CiAgICAgICAgaWYg
bm90IGlzaW5zdGFuY2Uoc2x1Zywgc3RyKSBvciBub3Qgc2x1Zy5pc2FzY2lpKCk6CiAgICAgICAgICAgIHJldHVy
biBGYWxzZQogICAgICAgIHNsdWdfa2V5ID0gYnlfZ2l0aHViLmdldChzbHVnLnN0cmlwKCkubG93ZXIoKSkKICAg
ICAgICBpZiBzbHVnX2tleSBpcyBOb25lIG9yIHNsdWdfa2V5IG5vdCBpbiBwdWJsaWNfa2V5czoKICAgICAgICAg
ICAgcmV0dXJuIEZhbHNlCiAgICAgICAgaWYgcmVzb2x2ZWRfcmVwbyBpcyBub3QgTm9uZSBhbmQgc2x1Z19rZXkg
IT0gcmVzb2x2ZWRfcmVwbzoKICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAgcmVzb2x2ZWRfcmVwbyA9
IHNsdWdfa2V5CiAgICBpZiByZXNvbHZlZF9yZXBvIGlzIE5vbmUgYW5kIGRlZmF1bHRfa2V5IG5vdCBpbiBwdWJs
aWNfa2V5czoKICAgICAgICByZXR1cm4gRmFsc2UKICAgIGRlY2xhcmVkX2NsYXNzZXMgPSBbdmFsdWUgZm9yIHZh
bHVlIGluIChyZWNvcmQuZ2V0KCJkYXRhX2NsYXNzIiksIG9wdGlvbnMuZ2V0KCJkYXRhX2NsYXNzIikpIGlmIHZh
bHVlIGlzIG5vdCBOb25lXQogICAgaWYgZGVjbGFyZWRfY2xhc3NlczoKICAgICAgICBwdWJsaWNfY2xhc3NlcyA9
IF9wdWJsaWNfZGF0YV9jbGFzc2VzKCkKICAgICAgICBpZiBub3QgcHVibGljX2NsYXNzZXM6CiAgICAgICAgICAg
IHJldHVybiBGYWxzZQogICAgICAgIGZpcnN0X2NsYXNzOiBzdHIgfCBOb25lID0gTm9uZQogICAgICAgIGZvciB2
YWx1ZSBpbiBkZWNsYXJlZF9jbGFzc2VzOgogICAgICAgICAgICBpZiBub3QgaXNpbnN0YW5jZSh2YWx1ZSwgc3Ry
KSBvciB2YWx1ZSBub3QgaW4gcHVibGljX2NsYXNzZXM6CiAgICAgICAgICAgICAgICByZXR1cm4gRmFsc2UKICAg
ICAgICAgICAgaWYgZmlyc3RfY2xhc3MgaXMgbm90IE5vbmUgYW5kIHZhbHVlICE9IGZpcnN0X2NsYXNzOgogICAg
ICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAgICAgIGZpcnN0X2NsYXNzID0gdmFsdWUKICAgIHJldHVy
biBUcnVlCgoKZGVmIF9uZWVkc19lc2NhcGUoY2g6IHN0cikgLT4gYm9vbDoKICAgIHJldHVybiBjaCBpbiBfTkFN
RURfRVNDQVBFUyBvciB1bmljb2RlZGF0YS5jYXRlZ29yeShjaCkgaW4gX0VTQ0FQRV9DQVRFR09SSUVTCgoKZGVm
IF9lc2NhcGVfY2hhcihjaDogc3RyKSAtPiBzdHI6CiAgICBpZiBjaCBpbiBfTkFNRURfRVNDQVBFUzoKICAgICAg
ICByZXR1cm4gX05BTUVEX0VTQ0FQRVNbY2hdCiAgICBjb2RlID0gb3JkKGNoKQogICAgaWYgY29kZSA8IDB4MTAw
OgogICAgICAgIHJldHVybiBmIlxceHtjb2RlOjAyeH0iCiAgICBpZiBjb2RlIDwgMHgxMDAwMDoKICAgICAgICBy
ZXR1cm4gZiJcXHV7Y29kZTowNHh9IgogICAgcmV0dXJuIGYiXFxVe2NvZGU6MDh4fSIKCgpkZWYgX2VzY2FwZV9z
YW1wbGUocHJlZml4OiBzdHIpIC0+IHN0cjoKICAgIHBhcnRzOiBsaXN0W3N0cl0gPSBbXQogICAgdXNlZCA9IDAK
ICAgIGZvciBjaCBpbiBwcmVmaXg6CiAgICAgICAgcGllY2UgPSBfZXNjYXBlX2NoYXIoY2gpIGlmIF9uZWVkc19l
c2NhcGUoY2gpIGVsc2UgY2gKICAgICAgICBpZiB1c2VkICsgbGVuKHBpZWNlKSA+IE1BWF9TQU1QTEVfQ0hBUlM6
CiAgICAgICAgICAgIGJyZWFrCiAgICAgICAgcGFydHMuYXBwZW5kKHBpZWNlKQogICAgICAgIHVzZWQgKz0gbGVu
KHBpZWNlKQogICAgcmV0dXJuICIiLmpvaW4ocGFydHMpCgoKZGVmIF9zYW1wbGVfdGV4dF9jbGVhcmVkKHRleHQ6
IHN0cikgLT4gYm9vbDoKICAgICIiIlJlamVjdCBzYW1wbGVzIGNhcnJ5aW5nIE9QU0VDIHRva2VucyAocGF0aHMs
IElQcywgZW1haWxzLCBTU0gpIG9yIHNlY3JldC9jcmVkZW50aWFsIHBhdHRlcm5zLiIiIgogICAgcmV0dXJuIG5v
dCBzY2FuX3RleHQodGV4dCkgYW5kIHJlZGFjdF90ZXh0KHRleHQpID09IHRleHQKCgpkZWYgX3NhbXBsZShyYXc6
IGJ5dGVzKSAtPiBzdHIgfCBOb25lOgogICAgIiIiRmlyc3QgYGBNQVhfU0FNUExFX0NIQVJTYGAgZXNjYXBlZCBj
aGFyYWN0ZXJzIG9mIGEgZmFpbGluZyByZXBseSB3aGVuIGNsZWFyZWQgZm9yIHN0YXRzLCBlbHNlIE5vbmUuCgog
ICAgVGhlIHNjYW5zIHJ1biBvdmVyIHRoZSB3aG9sZSByZXBseSBhcyByZWFkLCBuZXZlciB0aGUgY3V0IHByZWZp
eCBhbG9uZTogYSBjcmVkZW50aWFsIG9yIGhvc3QgZGV0YWlsIHRoYXQgc3RhcnRzCiAgICBpbiB0aGUgcHJlZml4
IGFuZCBlbmRzIHBhc3QgdGhlIGN1dCBpcyBmb3VuZCBpbiBmdWxsLCBhbmQgYW55IG1hdGNoIHRoYXQgc3RhcnRz
IGluIHRoZSBwcmVmaXggd2l0aGhvbGRzIGl0LgogICAgIiIiCiAgICBpZiBsZW4ocmF3KSA+IE1BWF9TQU1QTEVf
U0NBTl9CWVRFUzoKICAgICAgICByZXR1cm4gTm9uZQogICAgdGV4dCA9IHJhdy5kZWNvZGUoInV0Zi04IiwgZXJy
b3JzPSJyZXBsYWNlIikKICAgIHByZWZpeCA9IHRleHRbOk1BWF9TQU1QTEVfQ0hBUlNdCiAgICByZWRhY3RlZCA9
IHJlZGFjdF90ZXh0KHRleHQpIG9yICIiCiAgICBpZiBhbnkoZmluZGluZy5zdGFydCA8IGxlbihwcmVmaXgpIGZv
ciBmaW5kaW5nIGluIHNjYW5fdGV4dCh0ZXh0KSkgb3IgcmVkYWN0ZWRbOmxlbihwcmVmaXgpXSAhPSBwcmVmaXg6
CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGVzY2FwZWQgPSBfZXNjYXBlX3NhbXBsZShwcmVmaXgpCiAgICByZXR1
cm4gZXNjYXBlZCBpZiBfc2FtcGxlX3RleHRfY2xlYXJlZChwcmVmaXgpIGFuZCBfc2FtcGxlX3RleHRfY2xlYXJl
ZChlc2NhcGVkKSBlbHNlIE5vbmUKCgpkZWYgX3VucGFyc2VkKHJlYXNvbjogc3RyLCBzYW1wbGU6IHN0ciB8IE5v
bmUgPSBOb25lKSAtPiBkaWN0W3N0ciwgQW55XToKICAgIHJldHVybiB7InZlcmRpY3QiOiAidW5wYXJzZWQiLCAi
cHJpb3JpdGllcyI6IE5vbmUsICJ1bnBhcnNlZF9yZWFzb24iOiByZWFzb24sICJ1bnBhcnNlZF9zYW1wbGUiOiBz
YW1wbGV9CgoKZGVmIF9zaWRlY2FyX3BhdGgocGF0aDogUGF0aCwgcmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSkg
LT4gUGF0aCB8IE5vbmU6CiAgICAiIiJEZXJpdmUgdGhlIG93bmluZyBgYC5yZXN1bHRgYCBzaWRlY2FyIGZyb20g
YGBwYXRoLnN0ZW1gYCBhbmQgcmVqZWN0IGNvbmZsaWN0aW5nL21hbGZvcm1lZCBkZWNsYXJhdGlvbnMuIiIiCiAg
ICBleHBlY3RlZF9uYW1lID0gZiJ7cGF0aC5zdGVtfS5yZXN1bHQiCiAgICBkZWNsYXJlZCA9IHJlY29yZC5nZXQo
InJlc3VsdF9maWxlIikKICAgIGlmIGRlY2xhcmVkIGlzIG5vdCBOb25lOgogICAgICAgIGlmIG5vdCBpc2luc3Rh
bmNlKGRlY2xhcmVkLCBzdHIpIG9yIG5vdCBkZWNsYXJlZC5lbmRzd2l0aCgiLnJlc3VsdCIpOgogICAgICAgICAg
ICByZXR1cm4gTm9uZQogICAgICAgIGRlY2xhcmVkX25hbWUgPSBQYXRoKGRlY2xhcmVkKS5uYW1lCiAgICAgICAg
aWYgZGVjbGFyZWRfbmFtZSAhPSBleHBlY3RlZF9uYW1lOgogICAgICAgICAgICB0YXNrX2lkID0gcmVjb3JkLmdl
dCgidGFza19pZCIpCiAgICAgICAgICAgIHN0YW1wZWRfYXJjaGl2ZSA9ICgKICAgICAgICAgICAgICAgIGlzaW5z
dGFuY2UodGFza19pZCwgc3RyKQogICAgICAgICAgICAgICAgYW5kIHBhdGguc3RlbS5zdGFydHN3aXRoKGYie3Rh
c2tfaWR9LiIpCiAgICAgICAgICAgICAgICBhbmQgcGF0aC5zdGVtLmVuZHN3aXRoKCIuYXJjaGl2ZWQiKQogICAg
ICAgICAgICAgICAgYW5kIGRlY2xhcmVkX25hbWUgPT0gZiJ7dGFza19pZH0ucmVzdWx0IgogICAgICAgICAgICAp
CiAgICAgICAgICAgIGlmIG5vdCBzdGFtcGVkX2FyY2hpdmU6CiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZQog
ICAgcmV0dXJuIHBhdGgud2l0aF9uYW1lKGV4cGVjdGVkX25hbWUpCgoKZGVmIF9yZXN1bHQocGF0aDogUGF0aCwg
cmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSkgLT4gZGljdFtzdHIsIEFueV06CiAgICAiIiJWZXJkaWN0IGFuZCBm
aW5kaW5nIHByaW9yaXRpZXMgZnJvbSB0aGUgcmVjb3JkJ3MgYm91bmQgYGAucmVzdWx0YGAgc2lkZWNhciwgb3Ig
YSB0eXBlZCB1bnBhcnNlZCBjYXVzZS4KCiAgICBPbmx5IGEgcmVndWxhciBmaWxlLCBvcGVuZWQgd2l0aG91dCBm
b2xsb3dpbmcgYSBsaW5rLCB3aG9zZSBieXRlcyBtYXRjaCB0aGUgcmVjb3JkJ3MgYGByZXN1bHRfc2hhMjU2YGAg
KHdoZW4KICAgIGRlY2xhcmVkKSBpcyBhIHJlc3VsdC4gQW55dGhpbmcgZWxzZSAoYWJzZW50LCBhIHN5bWxpbmss
IGRpcmVjdG9yeSwgc29ja2V0IG9yIEZJRk8sIHVucmVhZGFibGUsIHJlcGxhY2VkIGJ5dGVzKQogICAgaXMgYGBt
aXNzaW5nX3Jlc3VsdGBgIHdpdGggbm8gc2FtcGxlLiBBIGZpbGUgcGFzdCBgYE1BWF9SRVNVTFRfQllURVNgYCBp
cyBub3QgcmVhZCB3aG9sZTogYGB0b29fYmlnYGAsIG5vIHNhbXBsZS4KICAgICIiIgogICAgc2lkZWNhciA9IF9z
aWRlY2FyX3BhdGgocGF0aCwgcmVjb3JkKQogICAgZXhwZWN0ZWQgPSByZWNvcmQuZ2V0KCJyZXN1bHRfc2hhMjU2
IikKICAgIGlmIHNpZGVjYXIgaXMgTm9uZSBvciAoZXhwZWN0ZWQgaXMgbm90IE5vbmUgYW5kIG5vdCAoaXNpbnN0
YW5jZShleHBlY3RlZCwgc3RyKSBhbmQgX1NIQTI1Ni5mdWxsbWF0Y2goZXhwZWN0ZWQpKSk6CiAgICAgICAgcmV0
dXJuIF91bnBhcnNlZCgibWlzc2luZ19yZXN1bHQiKQogICAgdHJ5OgogICAgICAgIGZkID0gb3Mub3BlbihzaWRl
Y2FyLCBvcy5PX1JET05MWSB8IG9zLk9fTk9OQkxPQ0sgfCBvcy5PX05PRk9MTE9XKQogICAgZXhjZXB0IE9TRXJy
b3I6CiAgICAgICAgcmV0dXJuIF91bnBhcnNlZCgibWlzc2luZ19yZXN1bHQiKQogICAgdHJ5OgogICAgICAgIGlm
IG5vdCBzdGF0LlNfSVNSRUcob3MuZnN0YXQoZmQpLnN0X21vZGUpOgogICAgICAgICAgICByZXR1cm4gX3VucGFy
c2VkKCJtaXNzaW5nX3Jlc3VsdCIpCiAgICAgICAgd2l0aCBvcy5mZG9wZW4oZmQsICJyYiIsIGNsb3NlZmQ9RmFs
c2UpIGFzIHN0cmVhbToKICAgICAgICAgICAgcmF3ID0gc3RyZWFtLnJlYWQoTUFYX1JFU1VMVF9CWVRFUyArIDEp
CiAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICByZXR1cm4gX3VucGFyc2VkKCJtaXNzaW5nX3Jlc3VsdCIpCiAg
ICBmaW5hbGx5OgogICAgICAgIG9zLmNsb3NlKGZkKQogICAgaWYgbGVuKHJhdykgPiBNQVhfUkVTVUxUX0JZVEVT
OgogICAgICAgIHJldHVybiBfdW5wYXJzZWQoInRvb19iaWciKQogICAgaWYgZXhwZWN0ZWQgaXMgbm90IE5vbmUg
YW5kIGhhc2hsaWIuc2hhMjU2KHJhdykuaGV4ZGlnZXN0KCkgIT0gZXhwZWN0ZWQ6CiAgICAgICAgcmV0dXJuIF91
bnBhcnNlZCgibWlzc2luZ19yZXN1bHQiKQogICAgcmV0dXJuIHJldmlld19zdW1tYXJ5KHJhdywgYWxsb3dfc2Ft
cGxlPV9yZWNvcmRfYWxsb3dzX3NhbXBsZShyZWNvcmQpLAogICAgICAgICAgICAgICAgICAgICAgICAgIGFsbG93
X21hcmtkb3duPXJlY29yZC5nZXQoInJlcXVpcmVfcmV2aWV3X3ZlcmRpY3QiKSBpcyBub3QgVHJ1ZSkKCgpkZWYg
X3JlY29yZGVkX3Jldmlldyh2ZXJkaWN0OiBvYmplY3QpIC0+IGRpY3Rbc3RyLCBBbnldIHwgTm9uZToKICAgICIi
IkEgc3RydWN0dXJlZCB2ZXJkaWN0IHJlY29yZCBkb2VzIG5vdCBhdHRlc3QgYSBmaW5kaW5ncyBjb3VudC4iIiIK
ICAgIHRyeToKICAgICAgICByZXR1cm4geyJ2ZXJkaWN0IjogX1JFQ09SREVEX1ZFUkRJQ1RTW25vcm1hbGl6ZV92
ZXJkaWN0KHZlcmRpY3QpXSwgInByaW9yaXRpZXMiOiBOb25lfQogICAgZXhjZXB0IFJldmlld1B1YmxpY2F0aW9u
RXJyb3I6CiAgICAgICAgcmV0dXJuIE5vbmUKCgpkZWYgcGF0aF9kaWdlc3QocGF0aDogc3RyKSAtPiBzdHI6CiAg
ICAiIiJUaGUgc3RvcmVkIGZvcm0gb2YgYSBmaW5kaW5nJ3MgcmVwb3NpdG9yeS1yZWxhdGl2ZSBwYXRoOiBlcXVh
bCBwYXRocyBjb21wYXJlIGVxdWFsLCB0aGUgcGF0aCBuZXZlciBzaG93cy4iIiIKICAgIHJldHVybiBoYXNobGli
LnNoYTI1NihwYXRoLmVuY29kZSgidXRmLTgiLCAic3Vycm9nYXRlZXNjYXBlIikpLmhleGRpZ2VzdCgpWzoxNl0K
CgpkZWYgcmV2aWV3X3N1bW1hcnkocmF3OiBieXRlcywgKiwgYWxsb3dfc2FtcGxlOiBib29sID0gRmFsc2UsIGFs
bG93X21hcmtkb3duOiBib29sID0gRmFsc2UpIC0+IGRpY3Rbc3RyLCBBbnldOgogICAgIiIiUmVhZCBzdHJ1Y3R1
cmVkIHJldmlldyBvdXRwdXQgdGhyb3VnaCB0aGUgcmV2aWV3IGNvbnRyYWN0LCByZXRhaW5pbmcgb25seSBjb3Vu
dHMsIHZlcmRpY3QsIG9yIHBhcnNlIGNhdXNlLgoKICAgIEEgYGBjb2RlLXJldmlldy1maW5kaW5ncy52MWBgIHJl
cGx5IGFsc28ga2VlcHMgdGhlIHBhdGggZGlnZXN0IG9mIGVhY2ggYmxvY2tpbmcgKFAwLVAxKSBmaW5kaW5nJ3Mg
bG9jYXRpb24sIHNvCiAgICB0aGUgc3RhdGlzdGljcyBjYW4gdGVsbCB3aGV0aGVyIHRoZSBhdXRob3IncyBuZXh0
IGNvbW1pdHMgdG91Y2hlZCB0aGF0IGZpbGUuCiAgICAiIiIKICAgIGRlZiBmYWlsKHJlYXNvbjogc3RyKSAtPiBk
aWN0W3N0ciwgQW55XToKICAgICAgICByZXR1cm4gX3VucGFyc2VkKHJlYXNvbiwgX3NhbXBsZShyYXcpIGlmIGFs
bG93X3NhbXBsZSBlbHNlIE5vbmUpCgogICAgdHJ5OgogICAgICAgIHRleHQgPSByYXcuZGVjb2RlKCJ1dGYtOCIp
CiAgICBleGNlcHQgVW5pY29kZURlY29kZUVycm9yOgogICAgICAgIHJldHVybiBmYWlsKCJiYWRfZm9ybWF0IikK
ICAgIHRyeToKICAgICAgICBwYXlsb2FkID0gcGFyc2VfcmV2aWV3ZXJfanNvbih0ZXh0KQogICAgZXhjZXB0IENv
bnRyYWN0RXJyb3IgYXMgZXhjOgogICAgICAgIGlmIGFsbG93X21hcmtkb3duIGFuZCBzdHIoZXhjKS5zdGFydHN3
aXRoKCgKICAgICAgICAgICAgIm5vbl9qc29uX29yX2NoYXR0ZXI6IiwgInJldmlld19lbnZlbG9wZV9pbnZhbGlk
X2hlYWRlcnMiLCAicmV2aWV3X2VudmVsb3BlX3JlcXVpcmVzX29uZV9qc29uX2Jsb2NrIiwKICAgICAgICAgICAg
InJldmlld19lbnZlbG9wZV9pbnZhbGlkX2pzb25fZmVuY2UiLAogICAgICAgICkpOgogICAgICAgICAgICAjIFRo
ZSBleGlzdGluZyByZWNvcmRlciBjaGVja3MgdmlzaWJpbGl0eSBhbmQgcmVmdXNlcyBjb25mbGljdGluZwogICAg
ICAgICAgICAjIHRva2VucyBvciBtYWxmb3JtZWQgZmluZGluZ3MgYmxvY2tzIGluIGhpc3RvcmljYWwgdGV4dCBy
ZXBsaWVzLgogICAgICAgICAgICBmcm9tIGx1X21hY2hpbmVyeS5yZXZpZXcgaW1wb3J0IHJlY29yZF9jZl92ZXJk
aWN0ICAjIG5vcWE6IFBMQzA0MTUgIFRoZSByZWNvcmRlciBhbHNvIGxvYWRzIHRoZXNlIHJvd3MuCgogICAgICAg
ICAgICB0cnk6CiAgICAgICAgICAgICAgICBpZiBub3QgdW5zdXBwb3J0ZWRfdmVyZGljdF90b2tlbnModGV4dCk6
CiAgICAgICAgICAgICAgICAgICAgcmVjb3JkZWRfdmVyZGljdCA9IHJlY29yZF9jZl92ZXJkaWN0Lm5vcm1hbGl6
ZV92ZXJkaWN0KHRleHQpCiAgICAgICAgICAgICAgICAgICAgcmV0dXJuIHsidmVyZGljdCI6IF9SRUNPUkRFRF9W
RVJESUNUU1tyZWNvcmRlZF92ZXJkaWN0XSwgInByaW9yaXRpZXMiOiBOb25lfQogICAgICAgICAgICBleGNlcHQg
KHJlY29yZF9jZl92ZXJkaWN0LlJlY29yZEVycm9yLCBWYWx1ZUVycm9yLCBSZWN1cnNpb25FcnJvcik6CiAgICAg
ICAgICAgICAgICBwYXNzCiAgICAgICAgcmVhc29uID0gImJhZF9maWVsZHMiIGlmIHN0cihleGMpLnN0YXJ0c3dp
dGgoX0ZJRUxEX0NPTlRSQUNUX0VSUk9SUykgZWxzZSAiYmFkX2Zvcm1hdCIKICAgICAgICByZXR1cm4gZmFpbChy
ZWFzb24pCiAgICBleGNlcHQgKFZhbHVlRXJyb3IsIFJlY3Vyc2lvbkVycm9yLCBUeXBlRXJyb3IpOgogICAgICAg
IHJldHVybiBmYWlsKCJiYWRfZm9ybWF0IikKICAgIHRyeToKICAgICAgICBlbnZlbG9wZSA9IHRleHQubHN0cmlw
KCIgXHRcclxuIikuc3RhcnRzd2l0aCgiVkVSRElDVDoiKQogICAgICAgIGlmIGRlY2xhcmVzX2luY29tcGxldGVf
cmV2aWV3KHRleHQgaWYgZW52ZWxvcGUgZWxzZSAiIiwgZmluZGluZ3NfcGF5bG9hZHM9KHBheWxvYWQsKSk6CiAg
ICAgICAgICAgIHJldHVybiBmYWlsKCJiYWRfZmllbGRzIikKICAgICAgICBpZiBwYXlsb2FkLmdldCgic2NoZW1h
X3ZlcnNpb24iKSA9PSBTQ0hFTUFfVkVSU0lPTjoKICAgICAgICAgICAgZGVjaXNpb24sIGV2aWRlbmNlID0gY2hl
Y2tlZF9maW5kaW5nc19wYXlsb2FkKHBheWxvYWQpCiAgICAgICAgICAgIGNvdW50cyA9IENvdW50ZXIoZmluZGlu
Zy5wcmlvcml0eSBmb3IgZmluZGluZyBpbiBldmlkZW5jZS5maW5kaW5ncykKICAgICAgICAgICAgIyBGcm9tIHRo
ZSBzYW1lIGZpbmRpbmdzIGFzIHRoZSBjb3VudHMsIHNvIHRoZSByZWFkZXIncyBjaGVjayAob25lIHBhdGggcGVy
IFAwLVAxIGZpbmRpbmcpIGFsd2F5cyBob2xkcy4KICAgICAgICAgICAgcGF0aHMgPSBzb3J0ZWQocGF0aF9kaWdl
c3QoZmluZGluZy5wYXRoKSBmb3IgZmluZGluZyBpbiBldmlkZW5jZS5maW5kaW5ncwogICAgICAgICAgICAgICAg
ICAgICAgICAgICBpZiBmaW5kaW5nLnByaW9yaXR5IGluIEJMT0NLSU5HX1BSSU9SSVRJRVMpCiAgICAgICAgICAg
IHJldHVybiB7InZlcmRpY3QiOiBfUkVDT1JERURfVkVSRElDVFNbZGVjaXNpb25dLCAicHJpb3JpdGllcyI6IGRp
Y3QoY291bnRzKSwgImJsb2NraW5nX3BhdGhzIjogcGF0aHN9CiAgICAgICAgaWYgInZlcmRpY3QiIGluIHBheWxv
YWQgYW5kICJvdmVyYWxsIiBub3QgaW4gcGF5bG9hZCBhbmQgInNjaGVtYV92ZXJzaW9uIiBub3QgaW4gcGF5bG9h
ZDoKICAgICAgICAgICAgcmVjb3JkZWQgPSBfcmVjb3JkZWRfcmV2aWV3KHBheWxvYWRbInZlcmRpY3QiXSkKICAg
ICAgICAgICAgcmV0dXJuIHJlY29yZGVkIGlmIHJlY29yZGVkIGlzIG5vdCBOb25lIGVsc2UgZmFpbCgiYmFkX2Zp
ZWxkcyIpCiAgICBleGNlcHQgKENvbnRyYWN0RXJyb3IsIFJldmlld1B1YmxpY2F0aW9uRXJyb3IsIFZhbHVlRXJy
b3IsIFJlY3Vyc2lvbkVycm9yLCBUeXBlRXJyb3IsIEtleUVycm9yKToKICAgICAgICByZXR1cm4gZmFpbCgiYmFk
X2ZpZWxkcyIpCiAgICAjIE9sZGVyIGRpc3BhdGNoZXMgYWxzbyByZWNvcmRlZCB0aGUgdW52ZXJzaW9uZWQgY29y
cmVjdG5lc3MgYW5kIHByaW9yaXR5IGZpZWxkcy4KICAgIGlmICJzY2hlbWFfdmVyc2lvbiIgaW4gcGF5bG9hZDoK
ICAgICAgICByZXR1cm4gZmFpbCgiYmFkX2ZpZWxkcyIpCiAgICBvdmVyYWxsID0gcGF5bG9hZC5nZXQoIm92ZXJh
bGwiKQogICAgdmVyZGljdCA9IG92ZXJhbGwuZ2V0KCJjb3JyZWN0bmVzcyIpIGlmIGlzaW5zdGFuY2Uob3ZlcmFs
bCwgZGljdCkgZWxzZSBOb25lCiAgICBmaW5kaW5ncyA9IHBheWxvYWQuZ2V0KCJmaW5kaW5ncyIsIFtdKQogICAg
aWYgdmVyZGljdCBub3QgaW4gVkVSRElDVFNbOjNdIG9yIG5vdCBpc2luc3RhbmNlKGZpbmRpbmdzLCBsaXN0KSBv
ciBub3QgYWxsKAogICAgICAgICAgICBpc2luc3RhbmNlKGZpbmRpbmcsIGRpY3QpIGFuZCBmaW5kaW5nLmdldCgi
cHJpb3JpdHkiKSBpbiBQUklPUklUSUVTIGZvciBmaW5kaW5nIGluIGZpbmRpbmdzKToKICAgICAgICByZXR1cm4g
ZmFpbCgiYmFkX2ZpZWxkcyIpCiAgICBwcmlvcml0aWVzID0gQ291bnRlcihmaW5kaW5nWyJwcmlvcml0eSJdIGZv
ciBmaW5kaW5nIGluIGZpbmRpbmdzKQogICAgcmV0dXJuIHsidmVyZGljdCI6IHZlcmRpY3QsICJwcmlvcml0aWVz
IjogZGljdChzb3J0ZWQocHJpb3JpdGllcy5pdGVtcygpKSl9CgoKZGVmIF9yZXZpZXcocGF0aDogUGF0aCwgcmVj
b3JkOiBNYXBwaW5nW3N0ciwgQW55XSkgLT4gZGljdFtzdHIsIEFueV06CiAgICByZXBseSA9IF9yZXN1bHQocGF0
aCwgcmVjb3JkKQogICAgaWYgcmVwbHlbInZlcmRpY3QiXSAhPSAidW5wYXJzZWQiOgogICAgICAgIHJldHVybiBy
ZXBseQogICAgcmV0dXJuIF9yZWNvcmRlZF9yZXZpZXcocmVjb3JkLmdldCgidmVyZGljdCIpKSBvciByZXBseQoK
CmRlZiBfcmVhZGluZyh2YWx1ZTogb2JqZWN0KSAtPiBkaWN0W3N0ciwgZmxvYXRdIHwgc3RyOgogICAgcGN0ID0g
X251bWJlcih2YWx1ZS5nZXQoInJlbWFpbmluZ19wY3QiKSkgaWYgaXNpbnN0YW5jZSh2YWx1ZSwgTWFwcGluZykg
ZWxzZSBOb25lCiAgICByZXR1cm4geyJyZW1haW5pbmdfcGN0IjogcGN0fSBpZiBwY3QgaXMgbm90IE5vbmUgYW5k
IHBjdCA8PSAxMDAgZWxzZSAidW5rbm93biIKCgpkZWYgX2FsbG93YW5jZShyZWNvcmQ6IE1hcHBpbmdbc3RyLCBB
bnldKSAtPiBkaWN0W3N0ciwgQW55XSB8IE5vbmU6CiAgICBibG9jayA9IHJlY29yZC5nZXQoImFsbG93YW5jZSIp
CiAgICBpZiBub3QgaXNpbnN0YW5jZShibG9jaywgTWFwcGluZyk6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHBv
b2wgPSBibG9jay5nZXQoInBvb2wiKQogICAgcmV0dXJuIHsicG9vbCI6IHBvb2wgaWYgcG9vbCBpbiBQT09MUyBl
bHNlIE5vbmUsICJzdGFydCI6IF9yZWFkaW5nKGJsb2NrLmdldCgic3RhcnQiKSksCiAgICAgICAgICAgICJmaW5p
c2giOiBfcmVhZGluZyhibG9jay5nZXQoImZpbmlzaCIpKX0KCgpkZWYgX2xhbmRpbmcocmVjb3JkOiBNYXBwaW5n
W3N0ciwgQW55XSkgLT4gZGljdFtzdHIsIEFueV0gfCBOb25lOgogICAgIiIiVGhlIG1lcmdlZCBkZWxpdmVyeSB0
aGUgZGlzcGF0Y2hlciBvYnNlcnZlZCBmb3IgdGhpcyB0YXNrIChgYGRlbGl2ZXJ5X3ByYGApLCBQUiBudW1iZXIg
YW5kIFNIQSBvbmx5LiIiIgogICAgc2hhID0gcmVjb3JkLmdldCgiZGVsaXZlcnlfbWVyZ2Vfc2hhIikKICAgIHBy
ID0gc3RyKHJlY29yZC5nZXQoImRlbGl2ZXJ5X3ByIikgb3IgIiIpLnJwYXJ0aXRpb24oIiMiKVsyXQogICAgIyBt
ZXJnZWRfZGVsaXZlcnkgcHJvdmVzIGEgbWVyZ2UgaW50byB0aGUgdGFzaydzIGJhc2U7IG9ubHkgYSBtYWluLWJh
c2VkIHRhc2sncyBtZXJnZSBpcyBhIGxhbmRpbmcuCiAgICBpZiByZWNvcmQuZ2V0KCJ3b3JrdHJlZV9iYXNlX3Jl
ZiIsICJvcmlnaW4vbWFpbiIpIG5vdCBpbiAoIm9yaWdpbi9tYWluIiwgIm1haW4iKToKICAgICAgICByZXR1cm4g
Tm9uZQogICAgaWYgbm90IGlzaW5zdGFuY2Uoc2hhLCBzdHIpIG9yIG5vdCBfU0hBLmZ1bGxtYXRjaChzaGEpIG9y
IG5vdCBwci5pc2RpZ2l0KCk6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHJldHVybiB7InByIjogaW50KHByKSwg
Im1lcmdlX3NoYSI6IHNoYX0KCgpkZWYgdmFsaWRfdGFza19pZCh2YWx1ZTogb2JqZWN0KSAtPiBib29sOgogICAg
IiIiV2hldGhlciBgYHZhbHVlYGAgaXMgYSBkaXNwYXRjaGVyIHRhc2sgaWQ7IGFueSBvdGhlciB2YWx1ZSBpcyBu
ZXZlciBzdG9yZWQsIHNvIG5vIGZyZWUgdGV4dCBlbnRlcnMgYSByb3cuIiIiCiAgICByZXR1cm4gaXNpbnN0YW5j
ZSh2YWx1ZSwgc3RyKSBhbmQgX1RBU0tfSUQubWF0Y2godmFsdWUpIGlzIG5vdCBOb25lIGFuZCAiLi4iIG5vdCBp
biB2YWx1ZQoKCmRlZiBfaXNvKHZhbHVlOiBvYmplY3QpIC0+IHN0ciB8IE5vbmU6CiAgICBtb21lbnQgPSB0aW1l
c3RhbXAodmFsdWUpCiAgICByZXR1cm4gbW9tZW50Lmlzb2Zvcm1hdCgpIGlmIG1vbWVudCBlbHNlIE5vbmUKCgpk
ZWYgX25vX2RlbGl2ZXJhYmxlX2NhdXNlKHJlY29yZDogTWFwcGluZ1tzdHIsIEFueV0pIC0+IHN0ciB8IE5vbmU6
CiAgICAiIiJSZXRhaW4gdGhlIHN0YXRlZCBjYXVzZSBjYXRlZ29yeSwgZHJvcHBpbmcgZnJlZSB0ZXh0IHN1Y2gg
YXMgYSBsaXN0IG9mIHBhdGhzLiIiIgogICAgcmVhc29uID0gcmVjb3JkLmdldCgibm9fZGVsaXZlcmFibGVfcmVh
c29uIikgb3IgcmVjb3JkLmdldCgiZXJyb3IiKQogICAgaWYgbm90IGlzaW5zdGFuY2UocmVhc29uLCBzdHIpIG9y
IG5vdCByZWFzb24uc3RyaXAoKToKICAgICAgICByZXR1cm4gTm9uZQogICAgY29kZSA9IHJlLnN1YihyIlstXHNd
KyIsICJfIiwgcmVhc29uLnBhcnRpdGlvbigiOiIpWzBdLnN0cmlwKCkuY2FzZWZvbGQoKSkKICAgIGlmIGNvZGUg
aW4geyJzY29wZV9icmVha2VyIiwgInNjb3BlX2JyZWFrZXJfc3RvcCJ9OgogICAgICAgIHJldHVybiAic2NvcGVf
YnJlYWtlcl9zdG9wIgogICAgaWYgY29kZSBpbiB7ImRlYWRsaW5lIiwgImhhcmRfZGVhZGxpbmUiLCAiZGVhZGxp
bmVfZXhjZWVkZWQiLCAicHJvY2Vzc19kZWFkbGluZV9leGNlZWRlZCJ9OgogICAgICAgIHJldHVybiAiZGVhZGxp
bmUiCiAgICBpZiBjb2RlIGluIHsib3V0c2lkZV9vd25lZF9wYXRocyIsICJub19jb21taXRzX29yX3Vua25vd24i
fToKICAgICAgICByZXR1cm4gY29kZQogICAgcmV0dXJuICJvdGhlciIKCgpkZWYgc3VtbWFyeV9yb3cocGF0aDog
UGF0aCwgcmVjb3JkOiBNYXBwaW5nW3N0ciwgQW55XSwgc291cmNlX210aW1lX25zOiBpbnQgfCBOb25lID0gTm9u
ZSkgLT4gZGljdFtzdHIsIEFueV0gfCBOb25lOgogICAgIiIiVGhlIHN1bW1hcnkgcm93IG9mIGEgc2V0dGxlZCB0
YXNrIHJlY29yZCwgb3IgTm9uZSBmb3IgYW4gYWN0aXZlIG9yIGZvcmVpZ24gcmVjb3JkIG9yIGFuIGludmFsaWQg
dGFzayBpZC4KCiAgICBgYHNvdXJjZV9tdGltZV9uc2BgIGlzIHRoZSByZWNvcmQgZmlsZSdzIG1vZGlmaWNhdGlv
biB0aW1lIHdoZW4gaXQgd2FzIHJlYWQ6IG9mIHR3byByb3dzIGZvciBvbmUgdGFzayBpZCwgdGhlCiAgICBvbmUg
ZnJvbSB0aGUgbGF0ZXIgcmVjb3JkIHZlcnNpb24gaXMgdGhlIGN1cnJlbnQgb25lLgogICAgIiIiCiAgICB0YXNr
X2lkID0gcmVjb3JkLmdldCgidGFza19pZCIpCiAgICBzdGF0dXMgPSByZWNvcmQuZ2V0KCJzdGF0dXMiKQogICAg
aWYgbm90IHZhbGlkX3Rhc2tfaWQodGFza19pZCkgb3Igbm90IGlzaW5zdGFuY2Uoc3RhdHVzLCBzdHIpIG9yIHN0
YXR1cyBpbiBBQ1RJVkU6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGNvdW50cywgdXNhZ2Vfa25vd24gPSB0b2tl
bnMocmVjb3JkLmdldCgidXNhZ2UiKSkKICAgIHJlZnVzYWwgPSBzdGFydF9yZWZ1c2FsKHJlY29yZCwgY291bnRz
KQogICAgb3B0aW9ucyA9IF9vcHRpb25zKHJlY29yZCkKICAgICMgT25lIHJlc29sdmVkIGNvcHkgZHJpdmVzIHR5
cGluZywgY2xhc3MsIFBSIGFzc29jaWF0aW9uIGFuZCByZXBseSBwYXJzaW5nLiBBIHR5cGVkIHJldmlldwogICAg
IyB0aGF0IGRpZCBub3QgZmluaXNoIGhhcyBubyByZXBseSB0byByZWFkLiBBbnkgb3RoZXIgc2V0dGxlZCB0YXNr
IGlzIHJlYWQgb25jZSwgc28gYSBsZWdhY3kKICAgICMgcmVjb3JkIGtlZXBzIHJldmlldyBhdHRyaWJ1dGlvbiBv
bmx5IHdoZW4gdGhlIHJlcGx5IHZhbGlkYXRlcywgaW4gZWl0aGVyIG1vZGUuCiAgICByZXNvbHZlZCA9IF9yZXZp
ZXdfZmxhZ3MocmVjb3JkKQogICAgdHlwZWQgPSByZXZpZXdfdHlwZWQocmVzb2x2ZWQpCiAgICByZWFkaW5nID0g
Tm9uZSBpZiByZWZ1c2FsIG9yICh0eXBlZCBhbmQgc3RhdHVzICE9ICJkb25lIikgZWxzZSBfcmV2aWV3KHBhdGgs
IHJlc29sdmVkKQogICAgcm9sZSA9IHJvbGVfb2YocmVzb2x2ZWQsIHJlYWRpbmcpCiAgICBpZiByb2xlID09ICJy
ZXZpZXciOgogICAgICAgIHRhc2tfY2xhc3MgPSAoZiJ7bGFiZWwocmVzb2x2ZWQuZ2V0KCdyZXZpZXdfcHJvZmls
ZScpLCAnY29kZScpfS8iCiAgICAgICAgICAgICAgICAgICAgICBmIntsYWJlbChyZXNvbHZlZC5nZXQoJ3Jldmll
d19yaXNrJykpfSIpCiAgICBlbHNlOgogICAgICAgIHRhc2tfY2xhc3MgPSBsYWJlbChyZWNvcmQuZ2V0KCJyZXNl
YXJjaF90YXNrX2ZhbWlseSIpLCAidW5jbGFzc2lmaWVkIikKICAgIHByID0gcmVzb2x2ZWQuZ2V0KCJwciIpCiAg
ICBoZWFkID0gcmVjb3JkLmdldCgicGlubmVkX2hlYWQiKSBvciBvcHRpb25zLmdldCgicGlubmVkX2hlYWQiKSBv
ciByZWNvcmQuZ2V0KCJ3b3JrdHJlZV9iYXNlX3JlZiIpCiAgICBhdXRob3IgPSByZXNvbHZlZC5nZXQoInJldmll
d19hdXRob3JfbW9kZWwiKQogICAgcmF3X3JlcG8gPSByZWNvcmQuZ2V0KCJyZXBvIikgaWYgcmVjb3JkLmdldCgi
cmVwbyIpIGlzIG5vdCBOb25lIGVsc2Ugb3B0aW9ucy5nZXQoInJlcG8iKQogICAgcmVwbyA9ICJwdWJsaWMiIGlm
IHJhd19yZXBvIGlzIE5vbmUgZWxzZSBsYWJlbChyYXdfcmVwbywgInVua25vd24iKQogICAgcmF3X2NsYXNzID0g
cmVjb3JkLmdldCgiZGF0YV9jbGFzcyIpIGlmIHJlY29yZC5nZXQoImRhdGFfY2xhc3MiKSBpcyBub3QgTm9uZSBl
bHNlIG9wdGlvbnMuZ2V0KCJkYXRhX2NsYXNzIikKICAgIHJlcG9zaXRvcnkgPSByZWNvcmQuZ2V0KCJyZXBvc2l0
b3J5IikKICAgIGZyZXNoLCBjYWNoZWQsIG91dHB1dCA9IGNvdW50cwogICAgIyBUZXJtaW5hbCBzdGF0dXMgc3Rp
bGwgZGVjaWRlcyB3aGV0aGVyIGEgdmVyZGljdCBpcyBzdG9yZWQuIEEgcmV2aWV3IHRoYXQgaXMgdHlwZWQsIG9y
CiAgICAjIHdob3NlIHJlcGx5IHZhbGlkYXRlZCwgaXMgbm90IHJlcG9ydGVkIGFzIG1pc3NpbmcgcmV2aWV3IGV2
aWRlbmNlLgogICAgcmV2aWV3ID0gcmVhZGluZyBpZiByb2xlID09ICJyZXZpZXciIGFuZCBub3QgcmVmdXNhbCBh
bmQgc3RhdHVzID09ICJkb25lIiBlbHNlIE5vbmUKICAgIHJldHVybiB7CiAgICAgICAgInNjaGVtYSI6IFJPV19T
Q0hFTUEsCiAgICAgICAgInRhc2tfaWQiOiB0YXNrX2lkLAogICAgICAgICJoYXJuZXNzIjogbGFiZWwocmVjb3Jk
LmdldCgiYWdlbnQiKSksCiAgICAgICAgIm1vZGVsIjogY29uY3JldGVfbW9kZWwocmVjb3JkKSwKICAgICAgICAi
ZWZmb3J0IjogZWZmZWN0aXZlX2VmZm9ydChyZWNvcmQpLAogICAgICAgICJyb2xlIjogcm9sZSwKICAgICAgICAi
Y2xhc3MiOiB0YXNrX2NsYXNzLAogICAgICAgICJzdGF0dXMiOiBORVZFUl9TVEFSVEVEIGlmIHJlZnVzYWwgZWxz
ZSBsYWJlbChzdGF0dXMsICJvdGhlciIpLAogICAgICAgICJuZXZlcl9zdGFydGVkX3JlYXNvbiI6IHJlZnVzYWws
CiAgICAgICAgIm5vX2RlbGl2ZXJhYmxlX2NhdXNlIjogX25vX2RlbGl2ZXJhYmxlX2NhdXNlKHJlY29yZCkgaWYg
c3RhdHVzID09ICJub19kZWxpdmVyYWJsZSIgYW5kIG5vdCByZWZ1c2FsIGVsc2UgTm9uZSwKICAgICAgICAic3Rh
cnRlZF9hdCI6IF9pc28ocmVjb3JkLmdldCgic3RhcnRlZF9hdCIpKSwKICAgICAgICAiZmluaXNoZWRfYXQiOiBf
aXNvKHJlY29yZC5nZXQoImZpbmlzaGVkX2F0IikpLAogICAgICAgICJkdXJhdGlvbl9zIjogX251bWJlcihyZWNv
cmQuZ2V0KCJkdXJhdGlvbl9zIikpLAogICAgICAgICJ0b2tlbnMiOiB7ImZyZXNoX2lucHV0IjogZnJlc2gsICJj
YWNoZWRfaW5wdXQiOiBjYWNoZWQsICJvdXRwdXQiOiBvdXRwdXR9LAogICAgICAgICJ1c2FnZV9rbm93biI6IHVz
YWdlX2tub3duIG9yIHJlZnVzYWwgaXMgbm90IE5vbmUsCiAgICAgICAgImltcGxlbWVudGF0aW9uIjogcm9sZSA9
PSAid3JpdGUiIGFuZCByZWNvcmQuZ2V0KCJtb2RlIikgPT0gIndvcmtzcGFjZS13cml0ZSIKICAgICAgICBhbmQg
cmVjb3JkLmdldCgicmVzZWFyY2hfcm9sZSIpID09ICJpbXBsZW1lbnRhdGlvbiIsCiAgICAgICAgImFsbG93YW5j
ZSI6IE5vbmUgaWYgcmVmdXNhbCBlbHNlIF9hbGxvd2FuY2UocmVjb3JkKSwKICAgICAgICAicmV2aWV3IjogcmV2
aWV3LAogICAgICAgICJyZXBvIjogcmVwbywKICAgICAgICAjIFRoZSBHaXRIdWIgb3duZXIvbmFtZSB0aGUgdGFz
ayByYW4gb24sIHNvIGEgcmVhZGVyIGNhbiBiaW5kIGEgcHVibGljYXRpb24gd2l0aG91dCBhIGZsZWV0IGNhdGFs
b2cuCiAgICAgICAgInJlcG9zaXRvcnkiOiByZXBvc2l0b3J5IGlmIGlzaW5zdGFuY2UocmVwb3NpdG9yeSwgc3Ry
KSBhbmQgUkVQT1NJVE9SWV9TTFVHLmZ1bGxtYXRjaChyZXBvc2l0b3J5KSBlbHNlIE5vbmUsCiAgICAgICAgImRh
dGFfY2xhc3MiOiBOb25lIGlmIHJhd19jbGFzcyBpcyBOb25lIGVsc2UgbGFiZWwocmF3X2NsYXNzKSwKICAgICAg
ICAicHIiOiBpbnQocHIpIGlmIGlzaW5zdGFuY2UocHIsIGludCB8IHN0cikgYW5kIG5vdCBpc2luc3RhbmNlKHBy
LCBib29sKSBhbmQgc3RyKHByKS5pc2RpZ2l0KCkgZWxzZSBOb25lLAogICAgICAgICJwaW5uZWRfaGVhZCI6IGhl
YWQgaWYgaXNpbnN0YW5jZShoZWFkLCBzdHIpIGFuZCBfU0hBLmZ1bGxtYXRjaChoZWFkKSBlbHNlIE5vbmUsCiAg
ICAgICAgImF1dGhvcl9tb2RlbCI6IG1vZGVsX2lkKGF1dGhvcikgaWYgYXV0aG9yIGVsc2UgTm9uZSwKICAgICAg
ICAibGFuZGluZyI6IF9sYW5kaW5nKHJlY29yZCksCiAgICAgICAgInNvdXJjZV9tdGltZV9ucyI6IHNvdXJjZV9t
dGltZV9ucywKICAgIH0KCgpkZWYgcm93c19wYXRoKGJhdGNoX3N0YXRlX2RpcjogUGF0aCkgLT4gUGF0aDoKICAg
IHJldHVybiBiYXRjaF9zdGF0ZV9kaXIgLyBST1dTX0RJUiAvIFJPV1NfRklMRQoKCiMgQSBzZXR0bGVtZW50IG5l
dmVyIHdhaXRzIGxvbmdlciB0aGFuIHRoaXMgZm9yIHRoZSByb3dzIGxvY2s7IHRoZSBiYWNrZmlsbCByZXBhaXJz
IGEgcm93IGl0IHNraXBwZWQuClNFVFRMRU1FTlRfTE9DS19XQUlUX1MgPSA1LjAKX05PRk9MTE9XID0gb3MuT19O
T0ZPTExPVyB8IG9zLk9fQ0xPRVhFQwoKCmRlZiBfb3BlbihwYXRoOiBQYXRoLCBmbGFnczogaW50KSAtPiBpbnQ6
CiAgICAiIiJPcGVuIHdpdGhvdXQgZm9sbG93aW5nIGEgc3ltbGluaywgc28gYSBwbGFudGVkIGxpbmsgbmV2ZXIg
cmVkaXJlY3RzIGEgd3JpdGUgb3IgYSByZWFkLiIiIgogICAgcmV0dXJuIG9zLm9wZW4ocGF0aCwgZmxhZ3MgfCBf
Tk9GT0xMT1csIDBvNjAwKQoKCkBjb250ZXh0bWFuYWdlcgpkZWYgX2xvY2tlZChiYXRjaF9zdGF0ZV9kaXI6IFBh
dGgsIHdhaXRfczogZmxvYXQgfCBOb25lID0gTm9uZSkgLT4gSXRlcmF0b3JbUGF0aF06CiAgICAiIiJIb2xkIGBg
cm93cy5sb2NrYGA7IGBgd2FpdF9zYGAgYm91bmRzIHRoZSB3YWl0IChUaW1lb3V0RXJyb3IpLCBOb25lIHdhaXRz
IGFzIGxvbmcgYXMgaXQgdGFrZXMuIiIiCiAgICBkaXJlY3RvcnkgPSBiYXRjaF9zdGF0ZV9kaXIgLyBST1dTX0RJ
UgogICAgZGlyZWN0b3J5Lm1rZGlyKG1vZGU9MG83MDAsIHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSkKICAg
IGlmIG5vdCBzdGF0LlNfSVNESVIob3MubHN0YXQoZGlyZWN0b3J5KS5zdF9tb2RlKToKICAgICAgICByYWlzZSBO
b3RBRGlyZWN0b3J5RXJyb3IoInRoZSBtb2RlbC1zdGF0cyBkaXJlY3RvcnkgaXMgbm90IGEgZGlyZWN0b3J5IikK
ICAgIGZkID0gX29wZW4oZGlyZWN0b3J5IC8gTE9DS19GSUxFLCBvcy5PX1dST05MWSB8IG9zLk9fQ1JFQVQgfCBv
cy5PX0FQUEVORCkKICAgIHRyeToKICAgICAgICBpZiB3YWl0X3MgaXMgTm9uZToKICAgICAgICAgICAgZmNudGwu
ZmxvY2soZmQsIGZjbnRsLkxPQ0tfRVgpCiAgICAgICAgZWxzZToKICAgICAgICAgICAgZGVhZGxpbmUgPSB0aW1l
Lm1vbm90b25pYygpICsgd2FpdF9zCiAgICAgICAgICAgIHdoaWxlIFRydWU6CiAgICAgICAgICAgICAgICB0cnk6
CiAgICAgICAgICAgICAgICAgICAgZmNudGwuZmxvY2soZmQsIGZjbnRsLkxPQ0tfRVggfCBmY250bC5MT0NLX05C
KQogICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgICAgICBleGNlcHQgQmxvY2tpbmdJT0Vycm9y
OgogICAgICAgICAgICAgICAgICAgIGlmIHRpbWUubW9ub3RvbmljKCkgPj0gZGVhZGxpbmU6CiAgICAgICAgICAg
ICAgICAgICAgICAgIHJhaXNlIFRpbWVvdXRFcnJvcigidGhlIG1vZGVsLXN0YXRzIHJvd3MgbG9jayBpcyBidXN5
IikgZnJvbSBOb25lCiAgICAgICAgICAgICAgICAgICAgIyBmbG9jayBoYXMgbm8gdGltZW91dDsgdGhpcyBwb2xs
IGludGVydmFsIGJvdW5kcyBhIHNldHRsZW1lbnQncyB3YWl0IHRvIHdhaXRfcy4KICAgICAgICAgICAgICAgICAg
ICB0aW1lLnNsZWVwKDAuMDUpICAjIG5vc2VtZ3JlcDogcHl0aG9uLmxhbmcuYmVzdC1wcmFjdGljZS5hcmJpdHJh
cnktc2xlZXAKICAgICAgICB5aWVsZCBkaXJlY3RvcnkgLyBST1dTX0ZJTEUKICAgIGZpbmFsbHk6CiAgICAgICAg
b3MuY2xvc2UoZmQpCgoKZGVmIF9saW5lKHJvdzogTWFwcGluZ1tzdHIsIEFueV0pIC0+IGJ5dGVzOgogICAgcmV0
dXJuIChqc29uLmR1bXBzKHJvdywgc29ydF9rZXlzPVRydWUsIHNlcGFyYXRvcnM9KCIsIiwgIjoiKSwgYWxsb3df
bmFuPUZhbHNlKSArICJcbiIpLmVuY29kZSgpCgoKZGVmIF93cml0ZV9hbGwoZmQ6IGludCwgZGF0YTogYnl0ZXMp
IC0+IE5vbmU6CiAgICB2aWV3ID0gbWVtb3J5dmlldyhkYXRhKQogICAgd2hpbGUgdmlldzoKICAgICAgICB2aWV3
ID0gdmlld1tvcy53cml0ZShmZCwgdmlldyk6XQoKCmRlZiBfYXBwZW5kX2xpbmUocGF0aDogUGF0aCwgbGluZTog
Ynl0ZXMpIC0+IE5vbmU6CiAgICAiIiJBcHBlbmQgb25lIHdob2xlIGxpbmU7IGEgZnJhZ21lbnQgYW4gZWFybGll
ciBmYWlsZWQgYXBwZW5kIGxlZnQgaXMgY2xvc2VkIGZpcnN0LCBzbyBpdCBzdGF5cyBvbmUgYmFkIGxpbmUuIiIi
CiAgICBmZCA9IF9vcGVuKHBhdGgsIG9zLk9fUkRXUiB8IG9zLk9fQVBQRU5EIHwgb3MuT19DUkVBVCkKICAgIHRy
eToKICAgICAgICBzaXplID0gb3MuZnN0YXQoZmQpLnN0X3NpemUKICAgICAgICBvcGVuX2ZyYWdtZW50ID0gc2l6
ZSA+IDAgYW5kIG9zLnByZWFkKGZkLCAxLCBzaXplIC0gMSkgIT0gYiJcbiIKICAgICAgICBfd3JpdGVfYWxsKGZk
LCAoYiJcbiIgaWYgb3Blbl9mcmFnbWVudCBlbHNlIGIiIikgKyBsaW5lKQogICAgZmluYWxseToKICAgICAgICBv
cy5jbG9zZShmZCkKCgpkZWYgYXBwZW5kX3JvdyhiYXRjaF9zdGF0ZV9kaXI6IFBhdGgsIHJvdzogTWFwcGluZ1tz
dHIsIEFueV0gfCBDYWxsYWJsZVtbXSwgTWFwcGluZ1tzdHIsIEFueV0gfCBOb25lXSwKICAgICAgICAgICAgICAg
d2FpdF9zOiBmbG9hdCB8IE5vbmUgPSBOb25lKSAtPiBOb25lOgogICAgIiIiQXBwZW5kIG9uZSB3aG9sZSByb3cg
bGluZSB1bmRlciBgYHJvd3MubG9ja2BgLgoKICAgIGBgcm93YGAgbWF5IGJlIGEgZnVuY3Rpb24sIGNhbGxlZCB1
bmRlciB0aGUgbG9jazogdGhlIHNpZGVjYXIgaXQgcmVhZHMgaXMgdGhlbiBuZXZlciBvbGRlciB0aGFuIGEgcm93
IGFub3RoZXIKICAgIHdyaXRlciBoYXMgYWxyZWFkeSBzdG9yZWQuCiAgICAiIiIKICAgIHdpdGggX2xvY2tlZChi
YXRjaF9zdGF0ZV9kaXIsIHdhaXRfcykgYXMgcGF0aDoKICAgICAgICBsaW5lID0gcm93KCkgaWYgY2FsbGFibGUo
cm93KSBlbHNlIHJvdwogICAgICAgIGlmIGxpbmUgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIF9hcHBlbmRfbGlu
ZShwYXRoLCBfbGluZShsaW5lKSkKCgpkZWYgcmVjb3JkX3NldHRsZWQocGF0aDogUGF0aCwgcmVjb3JkOiBNYXBw
aW5nW3N0ciwgQW55XSwgYmF0Y2hfc3RhdGVfZGlyOiBQYXRoLCBzb3VyY2VfbXRpbWVfbnM6IGludCkgLT4gTm9u
ZToKICAgICIiIkFwcGVuZCB0aGUgcm93IG9mIGEganVzdC13cml0dGVuIHNldHRsZWQgcmVjb3JkLiBOZXZlciBy
YWlzZXM6IHRoZSByZWNvcmQncyBzdGF0dXMgc3RheXMgYXMgd3JpdHRlbi4KCiAgICBgYHNvdXJjZV9tdGltZV9u
c2BgIGlzIHRoZSBtb2RpZmljYXRpb24gdGltZSBvZiB0aGUgZmlsZSB0aGUgd3JpdGVyIHJlbmFtZWQgaW50byBw
bGFjZSAoaXRzIGNvbnRlbnQncyB2ZXJzaW9uKS4KICAgICIiIgogICAgdHJ5OgogICAgICAgIGFwcGVuZF9yb3co
YmF0Y2hfc3RhdGVfZGlyLCBsYW1iZGE6IHN1bW1hcnlfcm93KHBhdGgsIHJlY29yZCwgc291cmNlX210aW1lX25z
KSwgU0VUVExFTUVOVF9MT0NLX1dBSVRfUykKICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZXhjOgogICAgICAgIF9s
b2cud2FybmluZygibW9kZWwtc3RhdHMgc3VtbWFyeSByb3cgbm90IHdyaXR0ZW4gZm9yIGEgc2V0dGxlZCB0YXNr
OiAlcyIsIHR5cGUoZXhjKS5fX25hbWVfXykKCgojIFRoZSBwdWJsaXNoZWQtdmVyZGljdCBsZWRnZXIgKCM5MDIp
OiBvbmUgbGluZSBwZXIgdmVyZGljdCB0aGUgcmVjb3JkZXIgcHVibGlzaGVkIG9uIGEgUFIgYXMgdGhlIHJldmll
dyBvZiByZWNvcmQsCiMgaW4gYGA8YmF0Y2hfc3RhdGU+L21vZGVsLXN0YXRzL3B1YmxpY2F0aW9ucy5qc29ubGBg
LiBJdHMgb25lIHdyaXRlciBpcyBgYHJlY29yZF9jZl92ZXJkaWN0YGAgKGVhY2ggYGByZWNvcmRgYCBhbmQKIyB0
aGUgb25lLXNob3QgYGBpbXBvcnQtcHVibGljYXRpb25zYGApOyB0aGUgc3RhdGlzdGljcyByZWFkIGl0IGFuZCBu
ZXZlciByZWFjaCBHaXRIdWIuIEEgcmV2aWV3IHdpdGhvdXQgYSBsaW5lIGlzCiMgb2YgdW5rbm93biBwdWJsaWNh
dGlvbi4gQSBsaW5lIG9mIGEgZHVhbC1zbHVnIHJlcG9zaXRvcnkgYWxzbyBjYXJyaWVzIGBgZmxlZXRfcmVwb19z
ZWxlY3RvcmBgLCB0aGUgc2VsZWN0b3IgcmVjZWlwdAojICh2YWx1ZSBhbmQgc2hhMjU2KSB0aGUgd3JpdGVyIHJl
c29sdmVkIHRoZSByZXBvc2l0b3J5IHdpdGggKFNFTEVDVE9SLURVUkFCTEUtUkVDRUlQVFMpOyBsaW5lcyB3aXRo
b3V0IGl0IChzaW5nbGUtc2x1ZwojIHJvd3MsIGxpbmVzIHdyaXR0ZW4gYmVmb3JlIGl0KSByZWFkIGFzIGhhdmlu
ZyBub25lLiBUaGUgcmVjZWlwdCBpcyBwcm92ZW5hbmNlLCBuZXZlciBhdXRob3JpdHk6IG5vIHJlYWRlciBtYXkg
dXNlCiMgaXQgdG8gYXV0aG9yaXplIGEgd3JpdGUsIGFuZCBpdCBpcyBubyBwYXJ0IG9mIHdoYXQgbWFrZXMgdHdv
IGxpbmVzIHRoZSBzYW1lIHB1YmxpY2F0aW9uLgpQVUJMSUNBVElPTl9TQ0hFTUEgPSAicmV2aWV3LXB1YmxpY2F0
aW9uLnYxIgpQVUJMSUNBVElPTlNfRklMRSA9ICJwdWJsaWNhdGlvbnMuanNvbmwiClBVQkxJU0hFRF9WRVJESUNU
UyA9IGZyb3plbnNldCh7IkFQUFJPVkVEIiwgIkNIQU5HRVNfUkVRVUVTVEVEIiwgIkJMT0NLRUQifSkKX1BVQkxJ
Q0FUSU9OX0tFWVMgPSBmcm96ZW5zZXQoeyJzY2hlbWEiLCAidGFza19pZCIsICJyZXBvc2l0b3J5IiwgInByIiwg
ImhlYWQiLCAidmVyZGljdCIsICJwdWJsaXNoZWRfYXQifSkKX1BVQkxJQ0FUSU9OX1NFTEVDVE9SID0gImZsZWV0
X3JlcG9fc2VsZWN0b3IiClJFUE9TSVRPUllfU0xVRyA9IHJlLmNvbXBpbGUociJbQS1aYS16MC05XVtBLVphLXow
LTktXXswLDM4fS9bQS1aYS16MC05Ll8tXXsxLDEwMH1cWiIpCgoKZGVmIF91bmlxdWVfbWVtYmVycyhwYWlyczog
bGlzdFt0dXBsZVtzdHIsIEFueV1dKSAtPiBkaWN0W3N0ciwgQW55XToKICAgICIiIkEgSlNPTiBvYmplY3Qgd2hv
c2UgbWVtYmVyIG5hbWVzIHJlcGVhdCBpcyByZWZ1c2VkIChWYWx1ZUVycm9yKTogdGhlIGRlY29kZXIgd291bGQg
a2VlcCBvbmx5IHRoZSBsYXN0IHZhbHVlLiIiIgogICAgbWVtYmVycyA9IGRpY3QocGFpcnMpCiAgICBpZiBsZW4o
bWVtYmVycykgIT0gbGVuKHBhaXJzKToKICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJkdXBsaWNhdGUgSlNPTiBv
YmplY3QgbWVtYmVyIikKICAgIHJldHVybiBtZW1iZXJzCgoKZGVmIF9sb2FkX2xpbmUobGluZTogYnl0ZXMpIC0+
IEFueToKICAgICIiIk9uZSBsZWRnZXIgb3Igcm93IGxpbmUsIHJlZnVzaW5nIHJlcGVhdGVkIG1lbWJlciBuYW1l
cyAoVmFsdWVFcnJvcikgYW5kIGRlZXAgbmVzdGluZyAoUmVjdXJzaW9uRXJyb3IpLiIiIgogICAgcmV0dXJuIGpz
b25fZGVwdGgubG9hZHMobGluZSwgbWF4X2RlcHRoPV9KU09OX01BWF9ERVBUSCwgb2JqZWN0X3BhaXJzX2hvb2s9
X3VuaXF1ZV9tZW1iZXJzKQoKCmRlZiBfc2VsZWN0b3Jfb2socmVjZWlwdDogb2JqZWN0KSAtPiBib29sOgogICAg
IiIiVGhlIG9wdGlvbmFsIHJlY2VpcHQgdXNlcyB0aGUgc2VsZWN0b3IgbG9hZGVyJ3Mgc2x1ZyBncmFtbWFyIGFu
ZCBhIGxvd2VyY2FzZSBTSEEtMjU2LiIiIgogICAgaWYgcmVjZWlwdCBpcyBOb25lOgogICAgICAgIHJldHVybiBU
cnVlCiAgICBpZiBub3QgaXNpbnN0YW5jZShyZWNlaXB0LCBkaWN0KSBvciByZWNlaXB0LmtleXMoKSAhPSB7InNl
bGVjdG9yX3ZhbHVlIiwgInNlbGVjdG9yX3NoYTI1NiJ9OgogICAgICAgIHJldHVybiBGYWxzZQogICAgZGlnZXN0
ID0gcmVjZWlwdFsic2VsZWN0b3Jfc2hhMjU2Il0KICAgIHJldHVybiBfaXNfc2VsZWN0b3Jfc2x1ZyhyZWNlaXB0
WyJzZWxlY3Rvcl92YWx1ZSJdKSBhbmQgaXNpbnN0YW5jZShkaWdlc3QsIHN0cikgYW5kIF9TSEEyNTYuZnVsbG1h
dGNoKGRpZ2VzdCkgaXMgbm90IE5vbmUKCgpkZWYgX3B1YmxpY2F0aW9uX29rKGVudHJ5OiBvYmplY3QpIC0+IGJv
b2w6CiAgICBpZiBub3QgaXNpbnN0YW5jZShlbnRyeSwgZGljdCkgb3IgZW50cnkua2V5cygpIC0ge19QVUJMSUNB
VElPTl9TRUxFQ1RPUn0gIT0gX1BVQkxJQ0FUSU9OX0tFWVM6CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBpZiBu
b3QgX3NlbGVjdG9yX29rKGVudHJ5LmdldChfUFVCTElDQVRJT05fU0VMRUNUT1IpKToKICAgICAgICByZXR1cm4g
RmFsc2UKICAgIHByLCByZXBvc2l0b3J5ID0gZW50cnlbInByIl0sIGVudHJ5WyJyZXBvc2l0b3J5Il0KICAgIHJl
dHVybiAoZW50cnlbInNjaGVtYSJdID09IFBVQkxJQ0FUSU9OX1NDSEVNQSBhbmQgdmFsaWRfdGFza19pZChlbnRy
eVsidGFza19pZCJdKQogICAgICAgICAgICBhbmQgaXNpbnN0YW5jZShyZXBvc2l0b3J5LCBzdHIpIGFuZCBSRVBP
U0lUT1JZX1NMVUcuZnVsbG1hdGNoKHJlcG9zaXRvcnkpIGlzIG5vdCBOb25lCiAgICAgICAgICAgIGFuZCBfY291
bnRfdmFsdWUocHIpIGFuZCBwciA+IDAgYW5kIF9zaGEoZW50cnlbImhlYWQiXSkKICAgICAgICAgICAgYW5kIGlz
aW5zdGFuY2UoZW50cnlbInZlcmRpY3QiXSwgc3RyKSBhbmQgZW50cnlbInZlcmRpY3QiXSBpbiBQVUJMSVNIRURf
VkVSRElDVFMKICAgICAgICAgICAgYW5kIHRpbWVzdGFtcChlbnRyeVsicHVibGlzaGVkX2F0Il0pIGlzIG5vdCBO
b25lKQoKCmRlZiBwdWJsaWNhdGlvbigqLCB0YXNrX2lkOiBzdHIsIHJlcG9zaXRvcnk6IHN0ciwgcHI6IGludCwg
aGVhZDogc3RyLCB2ZXJkaWN0OiBzdHIsCiAgICAgICAgICAgICAgICBwdWJsaXNoZWRfYXQ6IG9iamVjdCwgZmxl
ZXRfcmVwb19zZWxlY3RvcjogTWFwcGluZ1tzdHIsIEFueV0gfCBOb25lID0gTm9uZSkgLT4gZGljdFtzdHIsIEFu
eV06CiAgICAiIiJPbmUgbGVkZ2VyIGxpbmU6IHdoaWNoIHJldmlldyB0YXNrJ3MgdmVyZGljdCB3YXMgcHVibGlz
aGVkIG9uIHdoaWNoIFBSIGhlYWQsIGFuZCB3aGVuIEdpdEh1YiBzdG9yZWQgaXQuCgogICAgYGBwdWJsaXNoZWRf
YXRgYCBpcyBHaXRIdWIncyBjb21tZW50IHRpbWUgYXMgcmVhZCBiYWNrIChhbnkgdmFsdWU7IG9ubHkgYW4gSVNP
IHRpbWVzdGFtcCBpcyBhY2NlcHRlZCkuCiAgICBgYGZsZWV0X3JlcG9fc2VsZWN0b3JgYCBpcyB0aGUgc2VsZWN0
b3IgcmVjZWlwdCB0aGUgd3JpdGVyIHJlc29sdmVkIGEgZHVhbC1zbHVnIHJlcG9zaXRvcnkgd2l0aDsgdGhlIGxp
bmUgY2FycmllcwogICAgaXQgb25seSB3aGVuIGl0IGlzIG5vdCBOb25lLiBSYWlzZXMgVmFsdWVFcnJvciB3aGVu
IGEgZmllbGQgaGFzIHRoZSB3cm9uZyBzaGFwZSwgc28gYSBtYWxmb3JtZWQgbGluZSBpcyBuZXZlcgogICAgd3Jp
dHRlbi4KICAgICIiIgogICAgZW50cnk6IGRpY3Rbc3RyLCBBbnldID0geyJzY2hlbWEiOiBQVUJMSUNBVElPTl9T
Q0hFTUEsICJ0YXNrX2lkIjogdGFza19pZCwgInJlcG9zaXRvcnkiOiByZXBvc2l0b3J5LCAicHIiOiBwciwKICAg
ICAgICAgICAgICAgICAgICAgICAgICAgICAiaGVhZCI6IGhlYWQsICJ2ZXJkaWN0IjogdmVyZGljdCwgInB1Ymxp
c2hlZF9hdCI6IHB1Ymxpc2hlZF9hdH0KICAgIGlmIGZsZWV0X3JlcG9fc2VsZWN0b3IgaXMgbm90IE5vbmU6CiAg
ICAgICAgaWYgbm90IGlzaW5zdGFuY2UoZmxlZXRfcmVwb19zZWxlY3RvciwgTWFwcGluZyk6CiAgICAgICAgICAg
IHJhaXNlIFZhbHVlRXJyb3IoInB1YmxpY2F0aW9uIGZpZWxkIGhhcyB0aGUgd3Jvbmcgc2hhcGUiKQogICAgICAg
IHJlY2VpcHQ6IGRpY3Rbc3RyLCBzdHJdID0ge30KICAgICAgICBmb3Iga2V5LCB2YWx1ZSBpbiBkaWN0KGZsZWV0
X3JlcG9fc2VsZWN0b3IpLml0ZW1zKCk6CiAgICAgICAgICAgIGlmIG5vdCBpc2luc3RhbmNlKGtleSwgc3RyKSBv
ciBub3QgaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKToKICAgICAgICAgICAgICAgIHJhaXNlIFZhbHVlRXJyb3IoInB1
YmxpY2F0aW9uIGZpZWxkIGhhcyB0aGUgd3Jvbmcgc2hhcGUiKQogICAgICAgICAgICAjIE1hdGNoIHRoZSBKU09O
IHJlcHJlc2VudGF0aW9uIHdpdGhvdXQgaW52b2tpbmcgb3ZlcnJpZGRlbiBtZXRob2RzIG9uIGEgc3RyaW5nIHN1
YmNsYXNzLgogICAgICAgICAgICBuYW1lID0gc3RyLl9fc3RyX18oa2V5KQogICAgICAgICAgICBpZiBuYW1lIGlu
IHJlY2VpcHQ6CiAgICAgICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJwdWJsaWNhdGlvbiBmaWVsZCBoYXMg
dGhlIHdyb25nIHNoYXBlIikKICAgICAgICAgICAgcmVjZWlwdFtuYW1lXSA9IHN0ci5fX3N0cl9fKHZhbHVlKQog
ICAgICAgIGVudHJ5W19QVUJMSUNBVElPTl9TRUxFQ1RPUl0gPSByZWNlaXB0CiAgICBpZiBub3QgX3B1YmxpY2F0
aW9uX29rKGVudHJ5KToKICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJwdWJsaWNhdGlvbiBmaWVsZCBoYXMgdGhl
IHdyb25nIHNoYXBlIikKICAgIHJldHVybiBlbnRyeQoKCmRlZiBfcHVibGljYXRpb25fbGluZXMocGF0aDogUGF0
aCwgY291bnRzOiBDb3VudGVyW3N0cl0pIC0+IGxpc3RbZGljdFtzdHIsIEFueV1dOgogICAgdHJ5OgogICAgICAg
IGZkID0gX29wZW4ocGF0aCwgb3MuT19SRE9OTFkpCiAgICBleGNlcHQgRmlsZU5vdEZvdW5kRXJyb3I6CiAgICAg
ICAgY291bnRzWyJtaXNzaW5nIl0gKz0gMQogICAgICAgIHJldHVybiBbXQogICAgZW50cmllcyA9IFtdCiAgICB3
aXRoIG9zLmZkb3BlbihmZCwgInJiIikgYXMgc3RyZWFtOgogICAgICAgIGZvciBsaW5lIGluIHN0cmVhbToKICAg
ICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgZW50cnkgPSBfbG9hZF9saW5lKGxpbmUpCiAgICAgICAgICAg
IGV4Y2VwdCAoVmFsdWVFcnJvciwgUmVjdXJzaW9uRXJyb3IpOgogICAgICAgICAgICAgICAgZW50cnkgPSBOb25l
CiAgICAgICAgICAgIGlmIG5vdCBfcHVibGljYXRpb25fb2soZW50cnkpOgogICAgICAgICAgICAgICAgY291bnRz
WyJtYWxmb3JtZWRfbGluZXMiXSArPSAxCiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBlbnRy
aWVzLmFwcGVuZChlbnRyeSkKICAgIGNvdW50c1sibGluZXMiXSA9IGxlbihlbnRyaWVzKQogICAgcmV0dXJuIGVu
dHJpZXMKCgpkZWYgcHVibGljYXRpb25fa2V5KGVudHJ5OiBNYXBwaW5nW3N0ciwgQW55XSkgLT4gdHVwbGVbc3Ry
LCBpbnQgfCBzdHIsIGludCwgc3RyLCBzdHJdOgogICAgIiIiSWRlbnRpZnkgb25lIHRhc2sncyB2ZXJkaWN0IGZv
ciBvbmUgUFIgaGVhZCwgaW5jbHVkaW5nIGFjcm9zcyBhIHJlcG9zaXRvcnkgcmVuYW1lLiIiIgogICAgcmV0dXJu
IChlbnRyeVsidGFza19pZCJdLCByZXBvc2l0b3J5X2lkZW50aXR5KGVudHJ5WyJyZXBvc2l0b3J5Il0pLCBlbnRy
eVsicHIiXSwgZW50cnlbImhlYWQiXSwgZW50cnlbInZlcmRpY3QiXSkKCgpkZWYgcHVibGljYXRpb25fa2V5cyhi
YXRjaF9zdGF0ZV9kaXI6IFBhdGgpIC0+IHNldFt0dXBsZVtzdHIsIGludCB8IHN0ciwgaW50LCBzdHIsIHN0cl1d
OgogICAgIiIiVGhlIGtleXMgb2YgZXZlcnkgd2VsbC1mb3JtZWQgbGVkZ2VyIGxpbmUuIiIiCiAgICByZXR1cm4g
e3B1YmxpY2F0aW9uX2tleShlKSBmb3IgZSBpbiBfcHVibGljYXRpb25fbGluZXMoYmF0Y2hfc3RhdGVfZGlyIC8g
Uk9XU19ESVIgLyBQVUJMSUNBVElPTlNfRklMRSwgQ291bnRlcigpKX0KCgpkZWYgcmVjb3JkX3B1YmxpY2F0aW9u
cyhiYXRjaF9zdGF0ZV9kaXI6IFBhdGgsIGVudHJpZXM6IEl0ZXJhYmxlW01hcHBpbmdbc3RyLCBBbnldXSwKICAg
ICAgICAgICAgICAgICAgICAgICAgd2FpdF9zOiBmbG9hdCB8IE5vbmUgPSBOb25lKSAtPiBpbnQ6CiAgICAiIiJB
cHBlbmQsIHVuZGVyIGBgcm93cy5sb2NrYGAsIGVhY2ggZW50cnkgdGhlIGxlZGdlciBkb2VzIG5vdCBob2xkIHll
dCAoYnkgYGBwdWJsaWNhdGlvbl9rZXlgYCk7IHRoZSBjb3VudCBhZGRlZC4KCiAgICBFdmVyeSBlbnRyeSBpcyBj
aGVja2VkIGJlZm9yZSBhbnl0aGluZyBpcyB3cml0dGVuOiBvbmUgbWFsZm9ybWVkIGVudHJ5IHJhaXNlcyBWYWx1
ZUVycm9yIGFuZCB3cml0ZXMgbm90aGluZy4KICAgIEVhY2ggZW50cnkgaXMgdmFsaWRhdGVkIGZyb20gdGhlIGV4
YWN0IEpTT04gYnl0ZXMgdGhhdCB3aWxsIGJlIHdyaXR0ZW4uCiAgICBUaW1lb3V0RXJyb3Igd2hlbiBgYHdhaXRf
c2BgIHBhc3NlcyBiZWZvcmUgdGhlIGxvY2sgaXMgZnJlZTsgT1NFcnJvciB3aGVuIHRoZSBsZWRnZXIgY2Fubm90
IGJlIHdyaXR0ZW4uCiAgICAiIiIKICAgIGNoZWNrZWQgPSBbXQogICAgZm9yIGVudHJ5IGluIGVudHJpZXM6CiAg
ICAgICAgdHJ5OgogICAgICAgICAgICByb3cgPSBkaWN0KGVudHJ5KQogICAgICAgICAgICAjIEpTT04gZW5jb2Rl
cyBpbnRlZ2VyIHN1YmNsYXNzZXMgYXMgcGxhaW4gaW50ZWdlcnMuIFRoZSBsZWRnZXIgcmVxdWlyZXMgYSBidWls
dC1pbiBpbnQgZm9yIFBScy4KICAgICAgICAgICAgaWYgbm90IF9jb3VudF92YWx1ZShyb3cuZ2V0KCJwciIpKToK
ICAgICAgICAgICAgICAgIHJhaXNlIFZhbHVlRXJyb3IoInB1YmxpY2F0aW9uIFBSIG11c3QgYmUgYW4gaW50ZWdl
ciBpbiByYW5nZSIpCiAgICAgICAgICAgIGxpbmUgPSBfbGluZShyb3cpCiAgICAgICAgICAgIHNuYXBzaG90ID0g
X2xvYWRfbGluZShsaW5lKQogICAgICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBWYWx1ZUVycm9yLCBSZWN1cnNpb25F
cnJvcikgYXMgZXhjOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJwdWJsaWNhdGlvbiBmaWVsZCBoYXMg
dGhlIHdyb25nIHNoYXBlIikgZnJvbSBleGMKICAgICAgICBpZiBub3QgX3B1YmxpY2F0aW9uX29rKHNuYXBzaG90
KToKICAgICAgICAgICAgcmFpc2UgVmFsdWVFcnJvcigicHVibGljYXRpb24gZmllbGQgaGFzIHRoZSB3cm9uZyBz
aGFwZSIpCiAgICAgICAgY2hlY2tlZC5hcHBlbmQoKHB1YmxpY2F0aW9uX2tleShzbmFwc2hvdCksIGxpbmUpKQog
ICAgd2l0aCBfbG9ja2VkKGJhdGNoX3N0YXRlX2Rpciwgd2FpdF9zKSBhcyByb3dzX2ZpbGU6CiAgICAgICAgcGF0
aCA9IHJvd3NfZmlsZS53aXRoX25hbWUoUFVCTElDQVRJT05TX0ZJTEUpCiAgICAgICAgcHJlc2VudCA9IHtwdWJs
aWNhdGlvbl9rZXkoZSkgZm9yIGUgaW4gX3B1YmxpY2F0aW9uX2xpbmVzKHBhdGgsIENvdW50ZXIoKSl9CiAgICAg
ICAgYWRkZWQgPSAwCiAgICAgICAgZm9yIGtleSwgbGluZSBpbiBjaGVja2VkOgogICAgICAgICAgICBpZiBrZXkg
bm90IGluIHByZXNlbnQ6CiAgICAgICAgICAgICAgICBfYXBwZW5kX2xpbmUocGF0aCwgbGluZSkKICAgICAgICAg
ICAgICAgIHByZXNlbnQuYWRkKGtleSkKICAgICAgICAgICAgICAgIGFkZGVkICs9IDEKICAgICAgICByZXR1cm4g
YWRkZWQKCgpkZWYgcmVhZF9wdWJsaWNhdGlvbnMoYmF0Y2hfc3RhdGVfZGlyOiBQYXRoKSAtPiB0dXBsZVtkaWN0
W3N0ciwgbGlzdFtkaWN0W3N0ciwgQW55XV1dLCBDb3VudGVyW3N0cl1dOgogICAgIiIiRWFjaCByZXZpZXcgdGFz
aydzIHB1Ymxpc2hlZCB2ZXJkaWN0cywgbmV3ZXN0IGZpcnN0IGJ5IEdpdEh1YidzIGNvbW1lbnQgdGltZSAob2Yg
dHdvIGF0IHRoZSBzYW1lIHRpbWUsIHRoZQogICAgbGF0ZXIgbGluZSBmaXJzdCwgc28gYSBoaXN0b3J5IGltcG9y
dCBhcHBlbmRlZCBhZnRlciBhIG5ld2VyIHB1YmxpY2F0aW9uIG5ldmVyIGNvbWVzIGJlZm9yZSBpdCksIGFuZCBy
ZWFkCiAgICBjb3VudHMuIEEgcmVhZGVyIHRha2VzIHRoZSBmaXJzdCBlbnRyeSB0aGF0IG1hdGNoZXMgaXRzIHJv
dydzIHJlcG9zaXRvcnksIFBSIGFuZCBoZWFkLiBBIG1pc3NpbmcgbGVkZ2VyIGdpdmVzCiAgICBubyBlbnRyaWVz
OiBldmVyeSByZXZpZXcgaXMgdGhlbiBvZiB1bmtub3duIHB1YmxpY2F0aW9uLiIiIgogICAgY291bnRzOiBDb3Vu
dGVyW3N0cl0gPSBDb3VudGVyKCkKICAgIGJ5X3Rhc2s6IGRpY3Rbc3RyLCBsaXN0W3R1cGxlW2RhdGV0aW1lLCBp
bnQsIGRpY3Rbc3RyLCBBbnldXV1dID0ge30KICAgIGxpbmVzID0gX3B1YmxpY2F0aW9uX2xpbmVzKGJhdGNoX3N0
YXRlX2RpciAvIFJPV1NfRElSIC8gUFVCTElDQVRJT05TX0ZJTEUsIGNvdW50cykKICAgIGZvciBpbmRleCwgZW50
cnkgaW4gZW51bWVyYXRlKGxpbmVzKToKICAgICAgICBieV90YXNrLnNldGRlZmF1bHQoZW50cnlbInRhc2tfaWQi
XSwgW10pLmFwcGVuZCgKICAgICAgICAgICAgKGNhc3QoZGF0ZXRpbWUsIHRpbWVzdGFtcChlbnRyeVsicHVibGlz
aGVkX2F0Il0pKSwgaW5kZXgsIGVudHJ5KSkKICAgIGNvdW50c1sidGFza3MiXSA9IGxlbihieV90YXNrKQogICAg
cmV0dXJuIHt0YXNrOiBbZW50cnkgZm9yICpfLCBlbnRyeSBpbiBzb3J0ZWQoZm91bmQsIGtleT1sYW1iZGEgaXRl
bTogaXRlbVs6Ml0sIHJldmVyc2U9VHJ1ZSldCiAgICAgICAgICAgIGZvciB0YXNrLCBmb3VuZCBpbiBieV90YXNr
Lml0ZW1zKCl9LCBjb3VudHMKCgpPVVRDT01FX1NDSEVNQSA9ICJyZXZpZXctZmluZGluZy1vdXRjb21lLnYxIgpP
VVRDT01FU19GSUxFID0gImZpbmRpbmctb3V0Y29tZXMuanNvbmwiCl9GSU5ESU5HX0lEID0gcmUuY29tcGlsZShy
IltBLVphLXowLTldW0EtWmEtejAtOS5fOi1dezAsNjN9XFoiKQpfR0lUX0hFQURfU0hBID0gcmUuY29tcGlsZShy
IlswLTlhLWZdezQwfVxaIikKX09VVENPTUVfUkVWSUVXRVJfTU9ERUwgPSAicmV2aWV3ZXJfbW9kZWwiCl9PVVRD
T01FX1JVTl9LRVlTID0gZnJvemVuc2V0KHsiaGVhZCIsICJzdGFydGVkX2F0In0pCl9PVVRDT01FX0tFWVMgPSB7
CiAgICAibGFiZWwiOiBmcm96ZW5zZXQoeyJzY2hlbWEiLCAia2luZCIsICJ0YXNrX2lkIiwgInJlcG9zaXRvcnki
LCAicHIiLCAiZmluZGluZ19pZCIsICJsYWJlbCIsICJldmlkZW5jZV9zaGEyNTYiLAogICAgICAgICAgICAgICAg
ICAgICAgICAicmVjb3JkZWRfYnkiLCAicmVjb3JkZWRfYXQifSksCiAgICAiY29udGVzdCI6IGZyb3plbnNldCh7
InNjaGVtYSIsICJraW5kIiwgInRhc2tfaWQiLCAicmVwb3NpdG9yeSIsICJwciIsICJmaW5kaW5nX2lkIiwgImNv
bnRlc3RlZF9ieSIsCiAgICAgICAgICAgICAgICAgICAgICAgICAgInJlY29yZGVkX2F0In0pLAp9CgoKZGVmIF9v
dXRjb21lX29rKGVudHJ5OiBvYmplY3QsICosIHJlcXVpcmVfcnVuX2JpbmRpbmc6IGJvb2wgPSBGYWxzZSkgLT4g
Ym9vbDoKICAgICMgRXZlcnkgbWVtYmVyc2hpcCB0ZXN0IGJlbG93IHJlYWRzIGEgc3RyaW5nIGZpcnN0OiBhIGxp
c3Qgb3Igb2JqZWN0IGluIGEgZGFtYWdlZCBsaW5lIGlzIHVuaGFzaGFibGUuCiAgICBpZiBub3QgaXNpbnN0YW5j
ZShlbnRyeSwgZGljdCk6CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBraW5kID0gZW50cnkuZ2V0KCJraW5kIikK
ICAgIGlmIG5vdCBpc2luc3RhbmNlKGtpbmQsIHN0cikgb3Iga2luZCBub3QgaW4gX09VVENPTUVfS0VZUzoKICAg
ICAgICByZXR1cm4gRmFsc2UKICAgIGV4cGVjdGVkID0gX09VVENPTUVfS0VZU1traW5kXQogICAgb3B0aW9uYWwg
PSBfT1VUQ09NRV9SVU5fS0VZUyB8ICh7X09VVENPTUVfUkVWSUVXRVJfTU9ERUx9IGlmIGtpbmQgPT0gImxhYmVs
IiBlbHNlIGZyb3plbnNldCgpKQogICAgaWYgbm90IChleHBlY3RlZCA8PSBlbnRyeS5rZXlzKCkgPD0gKGV4cGVj
dGVkIHwgb3B0aW9uYWwpKToKICAgICAgICByZXR1cm4gRmFsc2UKICAgIGhhc19oZWFkLCBoYXNfc3RhcnRlZCA9
ICJoZWFkIiBpbiBlbnRyeSwgInN0YXJ0ZWRfYXQiIGluIGVudHJ5CiAgICBpZiBoYXNfaGVhZCAhPSBoYXNfc3Rh
cnRlZDoKICAgICAgICByZXR1cm4gRmFsc2UKICAgIGlmIHJlcXVpcmVfcnVuX2JpbmRpbmcgYW5kIChub3QgaGFz
X2hlYWQgb3IgKGtpbmQgPT0gImxhYmVsIiBhbmQgX09VVENPTUVfUkVWSUVXRVJfTU9ERUwgbm90IGluIGVudHJ5
KSk6CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBpZiBoYXNfaGVhZCBhbmQgbm90ICgKICAgICAgICBpc2luc3Rh
bmNlKGVudHJ5WyJoZWFkIl0sIHN0cikgYW5kIF9HSVRfSEVBRF9TSEEuZnVsbG1hdGNoKGVudHJ5WyJoZWFkIl0p
IGlzIG5vdCBOb25lCiAgICAgICAgYW5kIHRpbWVzdGFtcChlbnRyeVsic3RhcnRlZF9hdCJdKSBpcyBub3QgTm9u
ZQogICAgKToKICAgICAgICByZXR1cm4gRmFsc2UKICAgIGlmIF9PVVRDT01FX1JFVklFV0VSX01PREVMIGluIGVu
dHJ5OgogICAgICAgIHJldmlld2VyID0gZW50cnlbX09VVENPTUVfUkVWSUVXRVJfTU9ERUxdCiAgICAgICAgaWYg
bm90IGlzaW5zdGFuY2UocmV2aWV3ZXIsIHN0cikgb3Igbm90IHJldmlld2VyIG9yIG1vZGVsX2xhYmVsKHJldmll
d2VyLCAiIikgIT0gcmV2aWV3ZXIgb3IgcmV2aWV3ZXIgPT0gInVua25vd24iOgogICAgICAgICAgICByZXR1cm4g
RmFsc2UKICAgIHByLCByZXBvc2l0b3J5LCBmaW5kaW5nX2lkID0gZW50cnlbInByIl0sIGVudHJ5WyJyZXBvc2l0
b3J5Il0sIGVudHJ5WyJmaW5kaW5nX2lkIl0KICAgIGlmIG5vdCAoZW50cnlbInNjaGVtYSJdID09IE9VVENPTUVf
U0NIRU1BIGFuZCB2YWxpZF90YXNrX2lkKGVudHJ5WyJ0YXNrX2lkIl0pCiAgICAgICAgICAgIGFuZCBpc2luc3Rh
bmNlKHJlcG9zaXRvcnksIHN0cikgYW5kIFJFUE9TSVRPUllfU0xVRy5mdWxsbWF0Y2gocmVwb3NpdG9yeSkgaXMg
bm90IE5vbmUKICAgICAgICAgICAgYW5kIF9jb3VudF92YWx1ZShwcikgYW5kIHByID4gMCBhbmQgaXNpbnN0YW5j
ZShmaW5kaW5nX2lkLCBzdHIpIGFuZCBfRklORElOR19JRC5mdWxsbWF0Y2goZmluZGluZ19pZCkKICAgICAgICAg
ICAgYW5kIHRpbWVzdGFtcChlbnRyeVsicmVjb3JkZWRfYXQiXSkgaXMgbm90IE5vbmUpOgogICAgICAgIHJldHVy
biBGYWxzZQogICAgaWYga2luZCA9PSAiY29udGVzdCI6CiAgICAgICAgcmV0dXJuIHZhbGlkX3Rhc2tfaWQoZW50
cnlbImNvbnRlc3RlZF9ieSJdKSBhbmQgZW50cnlbImNvbnRlc3RlZF9ieSJdICE9IGVudHJ5WyJ0YXNrX2lkIl0K
ICAgIGV2aWRlbmNlLCBsYWJlbCA9IGVudHJ5WyJldmlkZW5jZV9zaGEyNTYiXSwgZW50cnlbImxhYmVsIl0KICAg
IHJldHVybiAoaXNpbnN0YW5jZShsYWJlbCwgc3RyKSBhbmQgbGFiZWwgaW4gRklORElOR19PVVRDT01FX0xBQkVM
UwogICAgICAgICAgICBhbmQgKGV2aWRlbmNlIGlzIE5vbmUgb3IgKGlzaW5zdGFuY2UoZXZpZGVuY2UsIHN0cikg
YW5kIF9TSEEyNTYuZnVsbG1hdGNoKGV2aWRlbmNlKSBpcyBub3QgTm9uZSkpCiAgICAgICAgICAgIGFuZCAoZXZp
ZGVuY2UgaXMgbm90IE5vbmUgb3IgbGFiZWwgbm90IGluIEVWSURFTkNFRF9PVVRDT01FX0xBQkVMUykKICAgICAg
ICAgICAgYW5kIChlbnRyeVsicmVjb3JkZWRfYnkiXSBpcyBOb25lIG9yIHZhbGlkX3Rhc2tfaWQoZW50cnlbInJl
Y29yZGVkX2J5Il0pKSkKCgpkZWYgZmluZGluZ19vdXRjb21lKCosIGtpbmQ6IHN0ciwgdGFza19pZDogc3RyLCBy
ZXBvc2l0b3J5OiBzdHIsIHByOiBpbnQsIGZpbmRpbmdfaWQ6IHN0ciwgcmVjb3JkZWRfYXQ6IHN0ciwKICAgICAg
ICAgICAgICAgICAgICBoZWFkOiBzdHIgfCBOb25lID0gTm9uZSwgc3RhcnRlZF9hdDogc3RyIHwgTm9uZSA9IE5v
bmUsIHJldmlld2VyX21vZGVsOiBzdHIgfCBOb25lID0gTm9uZSwKICAgICAgICAgICAgICAgICAgICBsYWJlbDog
c3RyIHwgTm9uZSA9IE5vbmUsIGV2aWRlbmNlOiBzdHIgfCBOb25lID0gTm9uZSwgcmVjb3JkZWRfYnk6IHN0ciB8
IE5vbmUgPSBOb25lLAogICAgICAgICAgICAgICAgICAgIGNvbnRlc3RlZF9ieTogc3RyIHwgTm9uZSA9IE5vbmUp
IC0+IGRpY3Rbc3RyLCBBbnldOgogICAgIiIiT25lIG91dGNvbWUtbGVkZ2VyIGxpbmU6IHRoZSBhdXRob3IncyBg
YGxhYmVsYGAgZm9yIG9uZSBibG9ja2luZyBmaW5kaW5nIG9mIHRoZSBwdWJsaXNoZWQgcmV2aWV3IGBgdGFza19p
ZGBgCiAgICAoY2FycnlpbmcgYGBoZWFkYGAsIGBgc3RhcnRlZF9hdGBgLCBhbmQgYGByZXZpZXdlcl9tb2RlbGBg
IGF0IHdyaXRlIHRpbWUpLCBvciBhIGxhdGVyIHJldmlldydzIGBgY29udGVzdGBgIG9mIHRoYXQKICAgIGxhYmVs
IChjYXJyeWluZyB0aGUgdGFyZ2V0IHJ1bidzIGBgaGVhZGBgIGFuZCBgYHN0YXJ0ZWRfYXRgYCkuIFRoZSBldmlk
ZW5jZSBsaW5lIGlzIGtlcHQgb25seSBhcyBpdHMgU0hBLTI1NjogdGhlCiAgICB0ZXh0IHN0YXlzIGluIHRoZSBQ
UiBhbmQgdGhlIGRlbHRhIGJyaWVmLiBSYWlzZXMgVmFsdWVFcnJvciB3aGVuIGEgZmllbGQgaGFzIHRoZSB3cm9u
ZyBzaGFwZSwgc28gYSBtYWxmb3JtZWQgbGluZQogICAgaXMgbmV2ZXIgd3JpdHRlbi4iIiIKICAgIGVudHJ5OiBk
aWN0W3N0ciwgQW55XSA9IHsic2NoZW1hIjogT1VUQ09NRV9TQ0hFTUEsICJraW5kIjoga2luZCwgInRhc2tfaWQi
OiB0YXNrX2lkLCAicmVwb3NpdG9yeSI6IHJlcG9zaXRvcnksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAg
InByIjogcHIsICJmaW5kaW5nX2lkIjogZmluZGluZ19pZCwgImhlYWQiOiBoZWFkLCAic3RhcnRlZF9hdCI6IHN0
YXJ0ZWRfYXQsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgInJlY29yZGVkX2F0IjogcmVjb3JkZWRfYXR9
CiAgICBpZiBraW5kID09ICJsYWJlbCI6CiAgICAgICAgZW50cnkudXBkYXRlKHJldmlld2VyX21vZGVsPXJldmll
d2VyX21vZGVsLCBsYWJlbD1sYWJlbCwgcmVjb3JkZWRfYnk9cmVjb3JkZWRfYnksCiAgICAgICAgICAgICAgICAg
ICAgIGV2aWRlbmNlX3NoYTI1Nj1Ob25lIGlmIGV2aWRlbmNlIGlzIE5vbmUgZWxzZSBoYXNobGliLnNoYTI1Nihl
dmlkZW5jZS5lbmNvZGUoKSkuaGV4ZGlnZXN0KCkpCiAgICBlbHNlOgogICAgICAgIGlmIHJldmlld2VyX21vZGVs
IGlzIG5vdCBOb25lOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJmaW5kaW5nIG91dGNvbWUgZmllbGQg
aGFzIHRoZSB3cm9uZyBzaGFwZSIpCiAgICAgICAgZW50cnlbImNvbnRlc3RlZF9ieSJdID0gY29udGVzdGVkX2J5
CiAgICBpZiBub3QgX291dGNvbWVfb2soZW50cnksIHJlcXVpcmVfcnVuX2JpbmRpbmc9VHJ1ZSk6CiAgICAgICAg
cmFpc2UgVmFsdWVFcnJvcigiZmluZGluZyBvdXRjb21lIGZpZWxkIGhhcyB0aGUgd3Jvbmcgc2hhcGUiKQogICAg
cmV0dXJuIGVudHJ5CgoKZGVmIF9vdXRjb21lX2xpbmVzKHBhdGg6IFBhdGgsIGNvdW50czogQ291bnRlcltzdHJd
KSAtPiBsaXN0W2RpY3Rbc3RyLCBBbnldXToKICAgIHRyeToKICAgICAgICBmZCA9IF9vcGVuKHBhdGgsIG9zLk9f
UkRPTkxZKQogICAgZXhjZXB0IEZpbGVOb3RGb3VuZEVycm9yOgogICAgICAgIGNvdW50c1sibWlzc2luZyJdICs9
IDEKICAgICAgICByZXR1cm4gW10KICAgIGVudHJpZXMgPSBbXQogICAgd2l0aCBvcy5mZG9wZW4oZmQsICJyYiIp
IGFzIHN0cmVhbToKICAgICAgICBmb3IgbGluZSBpbiBzdHJlYW06CiAgICAgICAgICAgIHRyeToKICAgICAgICAg
ICAgICAgIGVudHJ5ID0gX2xvYWRfbGluZShsaW5lKQogICAgICAgICAgICBleGNlcHQgKFZhbHVlRXJyb3IsIFJl
Y3Vyc2lvbkVycm9yKToKICAgICAgICAgICAgICAgIGVudHJ5ID0gTm9uZQogICAgICAgICAgICBpZiBub3QgX291
dGNvbWVfb2soZW50cnkpOgogICAgICAgICAgICAgICAgY291bnRzWyJtYWxmb3JtZWRfbGluZXMiXSArPSAxCiAg
ICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBlbnRyaWVzLmFwcGVuZChlbnRyeSkKICAgIGNvdW50
c1sibGluZXMiXSA9IGxlbihlbnRyaWVzKQogICAgcmV0dXJuIGVudHJpZXMKCgpjbGFzcyBPdXRjb21lQ29uZmxp
Y3QoVmFsdWVFcnJvcik6CiAgICAiIiJUaGUgZmluZGluZyBhbHJlYWR5IGhhcyBhbm90aGVyIGxhYmVsOiBhIGZp
bmRpbmcgdGFrZXMgb25lIGxhYmVsLiIiIgoKCmRlZiByZWNvcmRfZmluZGluZ19vdXRjb21lcyhiYXRjaF9zdGF0
ZV9kaXI6IFBhdGgsIGVudHJpZXM6IEl0ZXJhYmxlW01hcHBpbmdbc3RyLCBBbnldXSwKICAgICAgICAgICAgICAg
ICAgICAgICAgICAgIHdhaXRfczogZmxvYXQgfCBOb25lID0gTm9uZSkgLT4gaW50OgogICAgIiIiQXBwZW5kLCB1
bmRlciBgYHJvd3MubG9ja2BgLCBlYWNoIGVudHJ5IHRoZSBvdXRjb21lIGxlZGdlciBkb2VzIG5vdCBob2xkIHll
dDsgdGhlIGNvdW50IGFkZGVkLgoKICAgIEEgbGFiZWwgbGluZSBpcyBvbmUgcGVyIChyZXZpZXcgdGFzaywgcmVw
b3NpdG9yeSwgUFIsIGZpbmRpbmcsIGhlYWQsIHN0YXJ0ZWRfYXQpLCB0aGUgcmVwb3NpdG9yeSBieSBpdHMgaWRl
bnRpdHkKICAgIGFjcm9zcyBhIHRyYW5zZmVyOiB0aGUgc2FtZSBsYWJlbCBhZ2FpbiBhZGRzIG5vdGhpbmcgYW5k
IGtlZXBzIHRoZSBmaXJzdCBsaW5lIGFuZCBpdHMgZXZpZGVuY2UsIGFuZCBhbm90aGVyIGxhYmVsCiAgICByYWlz
ZXMgT3V0Y29tZUNvbmZsaWN0IGJlZm9yZSBhbnl0aGluZyBpcyB3cml0dGVuLiBFdmVyeSBuZXdseSB3cml0dGVu
IGxpbmUgbXVzdCBjYXJyeSBgYGhlYWRgYCBhbmQKICAgIGBgc3RhcnRlZF9hdGBgIChhbmQgYGByZXZpZXdlcl9t
b2RlbGBgIG9uIGxhYmVscyk7IGV4aXN0aW5nIGxlZ2FjeSBsaW5lcyB3aXRob3V0IHRoZW0gYXJlIHN0aWxsIHJl
YWQgc28gbGVnYWN5CiAgICBsYWJlbHMgcmVtYWluIGNvbmZsaWN0LXByb3RlY3RlZC4gQSBjb250ZXN0IGxpbmUg
aXMgb25lIHBlciAocmV2aWV3IHRhc2ssIHJlcG9zaXRvcnksIFBSLCBmaW5kaW5nLCBjb250ZXN0aW5nCiAgICB0
YXNrLCBoZWFkLCBzdGFydGVkX2F0KS4gRXZlcnkgZW50cnkgaXMgY2hlY2tlZCBmcm9tIHRoZSBleGFjdCBKU09O
IGJ5dGVzIHRoYXQgd2lsbCBiZSB3cml0dGVuOiBvbmUgbWFsZm9ybWVkCiAgICBlbnRyeSByYWlzZXMgVmFsdWVF
cnJvciBhbmQgd3JpdGVzIG5vdGhpbmcuIFRpbWVvdXRFcnJvciB3aGVuIGBgd2FpdF9zYGAgcGFzc2VzIGZpcnN0
OyBPU0Vycm9yIHdoZW4gdGhlIGxlZGdlcgogICAgY2Fubm90IGJlIHdyaXR0ZW4uCiAgICAiIiIKICAgIGNoZWNr
ZWQgPSBbXQogICAgZm9yIGVudHJ5IGluIGVudHJpZXM6CiAgICAgICAgdHJ5OgogICAgICAgICAgICByb3cgPSBk
aWN0KGVudHJ5KQogICAgICAgICAgICBpZiBub3QgX2NvdW50X3ZhbHVlKHJvdy5nZXQoInByIikpOgogICAgICAg
ICAgICAgICAgcmFpc2UgVmFsdWVFcnJvcigiZmluZGluZyBvdXRjb21lIFBSIG11c3QgYmUgYW4gaW50ZWdlciBp
biByYW5nZSIpCiAgICAgICAgICAgIGxpbmUgPSBfbGluZShyb3cpCiAgICAgICAgICAgIHNuYXBzaG90ID0gX2xv
YWRfbGluZShsaW5lKQogICAgICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBWYWx1ZUVycm9yLCBSZWN1cnNpb25FcnJv
cikgYXMgZXhjOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJmaW5kaW5nIG91dGNvbWUgZmllbGQgaGFz
IHRoZSB3cm9uZyBzaGFwZSIpIGZyb20gZXhjCiAgICAgICAgaWYgbm90IF9vdXRjb21lX29rKHNuYXBzaG90LCBy
ZXF1aXJlX3J1bl9iaW5kaW5nPVRydWUpOgogICAgICAgICAgICByYWlzZSBWYWx1ZUVycm9yKCJmaW5kaW5nIG91
dGNvbWUgZmllbGQgaGFzIHRoZSB3cm9uZyBzaGFwZSIpCiAgICAgICAgY2hlY2tlZC5hcHBlbmQoKHNuYXBzaG90
LCBsaW5lKSkKCiAgICBkZWYgYmFzZV9rZXkoZW50cnk6IE1hcHBpbmdbc3RyLCBBbnldKSAtPiB0dXBsZVtzdHIs
IHN0ciwgaW50IHwgc3RyLCBpbnQsIHN0ciwgc3RyXToKICAgICAgICByZXR1cm4gKGVudHJ5WyJraW5kIl0sIGVu
dHJ5WyJ0YXNrX2lkIl0sIHJlcG9zaXRvcnlfaWRlbnRpdHkoZW50cnlbInJlcG9zaXRvcnkiXSksIGVudHJ5WyJw
ciJdLAogICAgICAgICAgICAgICAgZW50cnlbImZpbmRpbmdfaWQiXSwgZW50cnkuZ2V0KCJjb250ZXN0ZWRfYnki
KSBvciAiIikKCiAgICBkZWYgcnVuX2tleShlbnRyeTogTWFwcGluZ1tzdHIsIEFueV0pIC0+IHR1cGxlW3N0ciwg
ZGF0ZXRpbWVdIHwgTm9uZToKICAgICAgICByZXR1cm4gKGVudHJ5WyJoZWFkIl0sIGNhc3QoZGF0ZXRpbWUsIHRp
bWVzdGFtcChlbnRyeVsic3RhcnRlZF9hdCJdKSkpIGlmICJoZWFkIiBpbiBlbnRyeSBlbHNlIE5vbmUKCiAgICBk
ZWYga2V5KGVudHJ5OiBNYXBwaW5nW3N0ciwgQW55XSkgLT4gdHVwbGVbc3RyLCBzdHIsIGludCB8IHN0ciwgaW50
LCBzdHIsIHN0ciwgdHVwbGVbc3RyLCBkYXRldGltZV0gfCBOb25lXToKICAgICAgICByZXR1cm4gKCpiYXNlX2tl
eShlbnRyeSksIHJ1bl9rZXkoZW50cnkpKQoKICAgIHdpdGggX2xvY2tlZChiYXRjaF9zdGF0ZV9kaXIsIHdhaXRf
cykgYXMgcm93c19maWxlOgogICAgICAgIHBhdGggPSByb3dzX2ZpbGUud2l0aF9uYW1lKE9VVENPTUVTX0ZJTEUp
CiAgICAgICAgcHJlc2VudDogZGljdFt0dXBsZVtzdHIsIHN0ciwgaW50IHwgc3RyLCBpbnQsIHN0ciwgc3RyLCB0
dXBsZVtzdHIsIGRhdGV0aW1lXSB8IE5vbmVdLCBkaWN0W3N0ciwgQW55XV0gPSB7fQogICAgICAgIGZvciBleGlz
dGluZyBpbiBfb3V0Y29tZV9saW5lcyhwYXRoLCBDb3VudGVyKCkpOgogICAgICAgICAgICBwcmVzZW50LnNldGRl
ZmF1bHQoa2V5KGV4aXN0aW5nKSwgZXhpc3RpbmcpCgogICAgICAgIGRlZiBsZWdhY3lfbWF0Y2goc25hcHNob3Q6
IE1hcHBpbmdbc3RyLCBBbnldKSAtPiBNYXBwaW5nW3N0ciwgQW55XSB8IE5vbmU6CiAgICAgICAgICAgIGlmIHNu
YXBzaG90WyJraW5kIl0gIT0gImxhYmVsIjoKICAgICAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAgICAg
IGxlZ2FjeSA9IHByZXNlbnQuZ2V0KCgqYmFzZV9rZXkoc25hcHNob3QpLCBOb25lKSkKICAgICAgICAgICAgaWYg
bGVnYWN5IGlzIE5vbmU6CiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgICAgICAjIEEgbGVnYWN5
IGxhYmVsIHJlY29yZGVkIGJlZm9yZSB0aGlzIGNhbmRpZGF0ZSBydW4gc3RhcnRlZCBiZWxvbmdzIHRvIGFuIGVh
cmxpZXIgcnVuLCBub3QgdGhpcyBvbmUuCiAgICAgICAgICAgIGxlZ2FjeV9yZWNvcmRlZCA9IGNhc3QoZGF0ZXRp
bWUsIHRpbWVzdGFtcChsZWdhY3lbInJlY29yZGVkX2F0Il0pKQogICAgICAgICAgICBjYW5kaWRhdGVfc3RhcnRl
ZCA9IGNhc3QoZGF0ZXRpbWUsIHRpbWVzdGFtcChzbmFwc2hvdFsic3RhcnRlZF9hdCJdKSkKICAgICAgICAgICAg
cmV0dXJuIE5vbmUgaWYgbGVnYWN5X3JlY29yZGVkIDwgY2FuZGlkYXRlX3N0YXJ0ZWQgZWxzZSBsZWdhY3kKCiAg
ICAgICAgcGxhbm5lZDogZGljdFt0dXBsZVtzdHIsIHN0ciwgaW50IHwgc3RyLCBpbnQsIHN0ciwgc3RyLCB0dXBs
ZVtzdHIsIGRhdGV0aW1lXSB8IE5vbmVdLCBNYXBwaW5nW3N0ciwgQW55XV0gPSB7fQogICAgICAgIGZvciBzbmFw
c2hvdCwgXyBpbiBjaGVja2VkOgogICAgICAgICAgICBleGFjdCA9IGtleShzbmFwc2hvdCkKICAgICAgICAgICAg
aGVsZCA9IHByZXNlbnQuZ2V0KGV4YWN0KSBvciBwbGFubmVkLmdldChleGFjdCkgb3IgbGVnYWN5X21hdGNoKHNu
YXBzaG90KQogICAgICAgICAgICBpZiBoZWxkIGlzIG5vdCBOb25lIGFuZCBzbmFwc2hvdFsia2luZCJdID09ICJs
YWJlbCIgYW5kIGhlbGRbImxhYmVsIl0gIT0gc25hcHNob3RbImxhYmVsIl06CiAgICAgICAgICAgICAgICByYWlz
ZSBPdXRjb21lQ29uZmxpY3QoCiAgICAgICAgICAgICAgICAgICAgZiJmaW5kaW5nIHtzbmFwc2hvdFsnZmluZGlu
Z19pZCddfSBvZiB7c25hcHNob3RbJ3Rhc2tfaWQnXX0gaXMgYWxyZWFkeSBsYWJlbGxlZCB7aGVsZFsnbGFiZWwn
XX0iKQogICAgICAgICAgICBwbGFubmVkLnNldGRlZmF1bHQoZXhhY3QsIHNuYXBzaG90KQogICAgICAgIGFkZGVk
ID0gMAogICAgICAgIGZvciBzbmFwc2hvdCwgbGluZSBpbiBjaGVja2VkOgogICAgICAgICAgICBleGFjdCA9IGtl
eShzbmFwc2hvdCkKICAgICAgICAgICAgaWYgZXhhY3Qgbm90IGluIHByZXNlbnQgYW5kIGxlZ2FjeV9tYXRjaChz
bmFwc2hvdCkgaXMgTm9uZToKICAgICAgICAgICAgICAgIF9hcHBlbmRfbGluZShwYXRoLCBsaW5lKQogICAgICAg
ICAgICAgICAgcHJlc2VudFtleGFjdF0gPSBzbmFwc2hvdAogICAgICAgICAgICAgICAgYWRkZWQgKz0gMQogICAg
ICAgIHJldHVybiBhZGRlZAoKCk91dGNvbWVGaW5kaW5nS2V5ID0gdHVwbGVbc3RyLCBpbnQgfCBzdHIsIGludCwg
c3RyLCBzdHIsIGRhdGV0aW1lXQoKCmRlZiByZWFkX2ZpbmRpbmdfb3V0Y29tZXMoYmF0Y2hfc3RhdGVfZGlyOiBQ
YXRoKSAtPiB0dXBsZVsKICAgICAgICBkaWN0W091dGNvbWVGaW5kaW5nS2V5LCB0dXBsZVtzdHIsIHN0ciwgZGF0
ZXRpbWVdXSwgZGljdFtPdXRjb21lRmluZGluZ0tleSwgZGF0ZXRpbWVdLCBDb3VudGVyW3N0cl1dOgogICAgIiIi
VGhlIGF0dHJpYnV0ZWQgcnVuLWJvdW5kIGxhYmVscyAoZWFjaCBrZXllZCBieSAocmV2aWV3IHRhc2ssIHJlcG9z
aXRvcnkgaWRlbnRpdHksIFBSLCBmaW5kaW5nIGlkLCBoZWFkLAogICAgc3RhcnRlZF9hdCkgdG8gKHJldmlld2Vy
IG1vZGVsLCBsYWJlbCwgcmVjb3JkZWQgdGltZSkpLCB0aGUgZWFybGllc3QgY29udGVzdCB0aW1lIG9mIGVhY2gg
cnVuLWJvdW5kIGNvbnRlc3RlZAogICAgZmluZGluZywgYW5kIHJlYWQgY291bnRzLiBBIG1pc3NpbmcgbGVkZ2Vy
IGdpdmVzIG5vbmUuIExlZ2FjeSBsaW5lcyB3cml0dGVuIHdpdGhvdXQgYGByZXZpZXdlcl9tb2RlbGBgIG9yIHJ1
bgogICAgYmluZGluZyAoYGBoZWFkYGAgYW5kIGBgc3RhcnRlZF9hdGBgKSBhcmUgbm90IG1hbGZvcm1lZCBhbmQg
Y291bnQgaW4gYGB1bmF0dHJpYnV0ZWRfbGFiZWxzYGAgb3IKICAgIGBgdW5ib3VuZF9jb250ZXN0c2BgIHdpdGhv
dXQgam9pbmluZyBhbm90aGVyIGxlZGdlciBvciBzdXBwcmVzc2luZyBhbm90aGVyIHJ1bi4gRmxlZXRSZXBvRXJy
b3Igd2hlbiB0aGUgZmxlZXQKICAgIHJlZ2lzdHJ5IGNhbm5vdCBiZSByZWFkLiIiIgogICAgY291bnRzOiBDb3Vu
dGVyW3N0cl0gPSBDb3VudGVyKCkKICAgIGxhYmVsczogZGljdFtPdXRjb21lRmluZGluZ0tleSwgdHVwbGVbc3Ry
LCBzdHIsIGRhdGV0aW1lXV0gPSB7fQogICAgY29udGVzdHM6IGRpY3RbT3V0Y29tZUZpbmRpbmdLZXksIGRhdGV0
aW1lXSA9IHt9CiAgICBmb3IgZW50cnkgaW4gX291dGNvbWVfbGluZXMoYmF0Y2hfc3RhdGVfZGlyIC8gUk9XU19E
SVIgLyBPVVRDT01FU19GSUxFLCBjb3VudHMpOgogICAgICAgIHJlcG9faWQgPSByZXBvc2l0b3J5X2lkZW50aXR5
KGVudHJ5WyJyZXBvc2l0b3J5Il0pCiAgICAgICAgbW9tZW50ID0gY2FzdChkYXRldGltZSwgdGltZXN0YW1wKGVu
dHJ5WyJyZWNvcmRlZF9hdCJdKSkKICAgICAgICBpZiBlbnRyeVsia2luZCJdID09ICJsYWJlbCI6CiAgICAgICAg
ICAgIHJldmlld2VyID0gZW50cnkuZ2V0KF9PVVRDT01FX1JFVklFV0VSX01PREVMKQogICAgICAgICAgICBpZiBy
ZXZpZXdlciBpcyBOb25lIG9yICJoZWFkIiBub3QgaW4gZW50cnk6CiAgICAgICAgICAgICAgICBjb3VudHNbInVu
YXR0cmlidXRlZF9sYWJlbHMiXSArPSAxCiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBmaW5k
aW5nX2tleTogT3V0Y29tZUZpbmRpbmdLZXkgPSAoCiAgICAgICAgICAgICAgICBlbnRyeVsidGFza19pZCJdLCBy
ZXBvX2lkLCBlbnRyeVsicHIiXSwgZW50cnlbImZpbmRpbmdfaWQiXSwKICAgICAgICAgICAgICAgIGVudHJ5WyJo
ZWFkIl0sIGNhc3QoZGF0ZXRpbWUsIHRpbWVzdGFtcChlbnRyeVsic3RhcnRlZF9hdCJdKSksCiAgICAgICAgICAg
ICkKICAgICAgICAgICAgbGFiZWxzLnNldGRlZmF1bHQoZmluZGluZ19rZXksIChtb2RlbF9pZChyZXZpZXdlciks
IGVudHJ5WyJsYWJlbCJdLCBtb21lbnQpKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGlmICJoZWFkIiBub3Qg
aW4gZW50cnk6CiAgICAgICAgICAgICAgICBjb3VudHNbInVuYm91bmRfY29udGVzdHMiXSArPSAxCiAgICAgICAg
ICAgICAgICBjb250aW51ZQogICAgICAgICAgICBmaW5kaW5nX2tleSA9ICgKICAgICAgICAgICAgICAgIGVudHJ5
WyJ0YXNrX2lkIl0sIHJlcG9faWQsIGVudHJ5WyJwciJdLCBlbnRyeVsiZmluZGluZ19pZCJdLAogICAgICAgICAg
ICAgICAgZW50cnlbImhlYWQiXSwgY2FzdChkYXRldGltZSwgdGltZXN0YW1wKGVudHJ5WyJzdGFydGVkX2F0Il0p
KSwKICAgICAgICAgICAgKQogICAgICAgICAgICBwcmV2aW91cyA9IGNvbnRlc3RzLmdldChmaW5kaW5nX2tleSkK
ICAgICAgICAgICAgaWYgcHJldmlvdXMgaXMgTm9uZSBvciBtb21lbnQgPCBwcmV2aW91czoKICAgICAgICAgICAg
ICAgIGNvbnRlc3RzW2ZpbmRpbmdfa2V5XSA9IG1vbWVudAogICAgcmV0dXJuIGxhYmVscywgY29udGVzdHMsIGNv
dW50cwoKCmRlZiByZWZyZXNoX3JlYWRpbmdzKCkgLT4gTm9uZToKICAgICIiIkZvcmdldCBldmVyeSBjYXRhbG9n
IHJlYWRpbmcgdGhpcyBtb2R1bGUsIHRoZSBtb2RlbCBjYXRhbG9nIGxvYWRlciBhbmQgdGhlIGZsZWV0IHJlZ2lz
dHJ5IGtlZXAsIHNvIHRoZWlyIG5leHQKICAgIHVzZSByZWFkcyB0aGUgZmlsZXMuCgogICAgVGhlIHN0YXRpc3Rp
Y3MgY29sbGVjdG9yIGNhbGxzIGl0IG9uY2UgYXMgZWFjaCBjb2xsZWN0aW9uIHN0YXJ0czogYSBjYXRhbG9nIHRo
YXQgd2FzIHVucmVhZGFibGUsIG9yIHZhbGlkLCBhdAogICAgYW4gZWFybGllciBjb2xsZWN0aW9uIGlzIG5ldmVy
IHJldXNlZCwgYW5kIGEgZmFpbGVkIHJlYWRpbmcgbGFzdHMgb25seSBmb3IgdGhlIGNvbGxlY3Rpb24gdGhhdCBt
YWRlIGl0LgogICAgIiIiCiAgICBsb2FkX21vZGVsX2NhdGFsb2cuY2FjaGVfY2xlYXIoKQogICAgbW9kZWxfY2F0
YWxvZ19yZWFkaW5nLmNhY2hlX2NsZWFyKCkKICAgIHJldGlyZWRfc3Vic2NyaXB0aW9uX21vZGVsLmNhY2hlX2Ns
ZWFyKCkKICAgIF9mbGVldF9yZXBvX3Zpc2liaWxpdHkuY2FjaGVfY2xlYXIoKQogICAgX3B1YmxpY19kYXRhX2Ns
YXNzZXMuY2FjaGVfY2xlYXIoKQogICAgY2xlYXJfcmVwb3NpdG9yeV9jYWNoZSgpCgoKZGVmIGZsZWV0X2NhdGFs
b2coKSAtPiBkaWN0W3N0ciwgRmxlZXRSZXBvXSB8IE5vbmU6CiAgICAiIiJUaGUgZmxlZXQgcmVwb3NpdG9yeSBj
YXRhbG9nLCBvciBOb25lIHdoZW4gaXQgY2Fubm90IGJlIHJlYWQgKG5vIHB1YmxpY2F0aW9uIHRoZW4gbWF0Y2hl
cyBhIHJvdykuIiIiCiAgICB0cnk6CiAgICAgICAgcmV0dXJuIGxvYWRfZmxlZXRfcmVwb3MoKQogICAgZXhjZXB0
IEV4Y2VwdGlvbjogICMgZmFpbCBjbG9zZWQ6IGFuIHVucmVhZGFibGUgY2F0YWxvZyBtYXRjaGVzIG5vIHB1Ymxp
Y2F0aW9uCiAgICAgICAgcmV0dXJuIE5vbmUKCgpkZWYgcmVwb3NpdG9yeV9rZXlzKHJlcG9zOiBNYXBwaW5nW3N0
ciwgRmxlZXRSZXBvXSB8IE5vbmUpIC0+IGRpY3Rbc3RyLCBzdHJdOgogICAgIiIiRWFjaCByZWdpc3RlcmVkIEdp
dEh1YiBgYG93bmVyL25hbWVgYCwgbG93ZXJjYXNlZCwgdG8gaXRzIGZsZWV0IHJlcG9zaXRvcnkga2V5ICh0aGUg
cm93cycgYGByZXBvYGApOyBlbXB0eQogICAgd2l0aG91dCBhIGNhdGFsb2cuIiIiCiAgICByZXR1cm4gewogICAg
ICAgIHNsdWcubG93ZXIoKToga2V5CiAgICAgICAgZm9yIGtleSwgcmVwbyBpbiAocmVwb3Mgb3Ige30pLml0ZW1z
KCkKICAgICAgICBmb3Igc2x1ZyBpbiByZXBvLnNsdWdzCiAgICAgICAgaWYgc2x1Zy5pc2FzY2lpKCkKICAgIH0K
CgpfTEFCRUxfRklFTERTID0gKCJoYXJuZXNzIiwgIm1vZGVsIiwgImVmZm9ydCIsICJyb2xlIiwgImNsYXNzIiwg
InN0YXR1cyIsICJyZXBvIikKCgpkZWYgX3Nhbml0aXNlZChyb3c6IGRpY3Rbc3RyLCBBbnldKSAtPiBkaWN0W3N0
ciwgQW55XToKICAgICIiIkEgcm93IGFzIHJlYWQsIHdpdGggZXZlcnkgbGFiZWwgcmV2YWxpZGF0ZWQsIHNvIGEg
ZGFtYWdlZCBmaWxlIG5ldmVyIHB1dHMgZnJlZSB0ZXh0IGluIGEgbGFiZWwuIiIiCiAgICBmb3IgbmFtZSBpbiBf
TEFCRUxfRklFTERTOgogICAgICAgIHZhbHVlID0gcm93LmdldChuYW1lKQogICAgICAgIGlmIG5hbWUgPT0gIm1v
ZGVsIjoKICAgICAgICAgICAgcm93W25hbWVdID0gbW9kZWxfbGFiZWwodmFsdWUpCiAgICAgICAgZWxpZiBuYW1l
ID09ICJjbGFzcyIgYW5kIGlzaW5zdGFuY2UodmFsdWUsIHN0cikgYW5kIHZhbHVlLmNvdW50KCIvIikgPT0gMToK
ICAgICAgICAgICAgcm93W25hbWVdID0gIi8iLmpvaW4obGFiZWwocGFydCkgZm9yIHBhcnQgaW4gdmFsdWUuc3Bs
aXQoIi8iKSkKICAgICAgICBlbHNlOgogICAgICAgICAgICByb3dbbmFtZV0gPSBsYWJlbCh2YWx1ZSkKICAgIGF1
dGhvciA9IHJvdy5nZXQoImF1dGhvcl9tb2RlbCIpCiAgICByb3dbImF1dGhvcl9tb2RlbCJdID0gbW9kZWxfbGFi
ZWwoYXV0aG9yKSBpZiBhdXRob3IgaXMgbm90IE5vbmUgZWxzZSBOb25lCiAgICBkYXRhX2NsYXNzID0gcm93Lmdl
dCgiZGF0YV9jbGFzcyIpCiAgICByb3dbImRhdGFfY2xhc3MiXSA9IGxhYmVsKGRhdGFfY2xhc3MpIGlmIGRhdGFf
Y2xhc3MgaXMgbm90IE5vbmUgZWxzZSBOb25lCiAgICByZXR1cm4gcm93CgoKIyBFdmVyeSBzdG9yZWQgbnVtYmVy
IGlzIGF0IG1vc3QgYSBzaWduZWQgNjQtYml0IGludGVnZXIsIHNvIG5vIHN1bSBvciByYXRpbyBvdmVyIGFueSBu
dW1iZXIgb2Ygcm93cyBvdmVyZmxvd3MgYQojIGZsb2F0IG9yIHNlcmlhbGl6ZXMgYXMgaW5maW5pdHkuCk1BWF9W
QUxVRSA9IDIqKjYzIC0gMQoKCmRlZiBfY291bnRfdmFsdWUodmFsdWU6IG9iamVjdCkgLT4gYm9vbDoKICAgIHJl
dHVybiB0eXBlKHZhbHVlKSBpcyBpbnQgYW5kIDAgPD0gdmFsdWUgPD0gTUFYX1ZBTFVFCgoKZGVmIF9tZWFzdXJl
KHZhbHVlOiBvYmplY3QsIHRvcDogZmxvYXQgPSBNQVhfVkFMVUUpIC0+IGJvb2w6CiAgICBpZiBpc2luc3RhbmNl
KHZhbHVlLCBib29sKSBvciBub3QgaXNpbnN0YW5jZSh2YWx1ZSwgaW50IHwgZmxvYXQpOgogICAgICAgIHJldHVy
biBGYWxzZQogICAgcmV0dXJuIDAgPD0gdmFsdWUgPD0gdG9wICAjIE5hTiBmYWlscyBib3RoIGNvbXBhcmlzb25z
OyBhbiBpbnRlZ2VyIG9mIGFueSBzaXplIGNvbXBhcmVzIGV4YWN0bHkKCgpkZWYgX29wdGlvbmFsKHZhbHVlOiBv
YmplY3QsIGNoZWNrOiBBbnkpIC0+IGJvb2w6CiAgICByZXR1cm4gdmFsdWUgaXMgTm9uZSBvciBjaGVjayh2YWx1
ZSkKCgpkZWYgX3JlYWRpbmdfb2sodmFsdWU6IG9iamVjdCkgLT4gYm9vbDoKICAgIHJldHVybiB2YWx1ZSA9PSAi
dW5rbm93biIgb3IgKGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpIGFuZCB2YWx1ZS5rZXlzKCkgPT0geyJyZW1haW5p
bmdfcGN0In0KICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGFuZCBfbWVhc3VyZSh2YWx1ZVsicmVt
YWluaW5nX3BjdCJdLCAxMDApKQoKCmRlZiBfc2hhKHZhbHVlOiBvYmplY3QpIC0+IGJvb2w6CiAgICByZXR1cm4g
aXNpbnN0YW5jZSh2YWx1ZSwgc3RyKSBhbmQgX1NIQS5mdWxsbWF0Y2godmFsdWUpIGlzIG5vdCBOb25lCgoKZGVm
IF9zYW1wbGVfb2sodmFsdWU6IG9iamVjdCkgLT4gYm9vbDoKICAgIHJldHVybiAoCiAgICAgICAgaXNpbnN0YW5j
ZSh2YWx1ZSwgc3RyKQogICAgICAgIGFuZCBsZW4odmFsdWUpIDw9IE1BWF9TQU1QTEVfQ0hBUlMKICAgICAgICBh
bmQgbm90IGFueShfbmVlZHNfZXNjYXBlKGNoKSBmb3IgY2ggaW4gdmFsdWUpCiAgICAgICAgYW5kIF9zYW1wbGVf
dGV4dF9jbGVhcmVkKHZhbHVlKQogICAgKQoKCmRlZiBfcmV2aWV3X29rKHJldmlldzogb2JqZWN0LCAqLCByb2xl
OiBvYmplY3QsIHN0YXR1czogb2JqZWN0LCByZXBvOiBvYmplY3QsIGRhdGFfY2xhc3M6IG9iamVjdCwKICAgICAg
ICAgICAgICAgY2hlY2tfc2FtcGxlX3Zpc2liaWxpdHk6IGJvb2wgPSBUcnVlKSAtPiBib29sOgogICAgaWYgcmV2
aWV3IGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIFRydWUKICAgIGlmIHJvbGUgIT0gInJldmlldyIgb3Igc3RhdHVz
ICE9ICJkb25lIiBvciBub3QgaXNpbnN0YW5jZShyZXZpZXcsIGRpY3QpOgogICAgICAgIHJldHVybiBGYWxzZQog
ICAgdmVyZGljdCA9IHJldmlldy5nZXQoInZlcmRpY3QiKQogICAgaWYgbm90IGlzaW5zdGFuY2UodmVyZGljdCwg
c3RyKSBvciB2ZXJkaWN0IG5vdCBpbiBWRVJESUNUUzoKICAgICAgICByZXR1cm4gRmFsc2UKICAgIHByaW9yaXRp
ZXMgPSByZXZpZXcuZ2V0KCJwcmlvcml0aWVzIikKICAgIGlmIG5vdCBfb3B0aW9uYWwocHJpb3JpdGllcywgbGFt
YmRhIHA6IGlzaW5zdGFuY2UocCwgZGljdCkKICAgICAgICAgICAgICAgICAgICAgYW5kIGFsbChrIGluIFBSSU9S
SVRJRVMgYW5kIF9jb3VudF92YWx1ZShuKSBmb3IgaywgbiBpbiBwLml0ZW1zKCkpKToKICAgICAgICByZXR1cm4g
RmFsc2UKICAgIGlmIHJldmlldy5rZXlzKCkgPT0geyJ2ZXJkaWN0IiwgInByaW9yaXRpZXMifToKICAgICAgICBy
ZXR1cm4gVHJ1ZQogICAgaWYgcmV2aWV3LmtleXMoKSA9PSB7InZlcmRpY3QiLCAicHJpb3JpdGllcyIsICJibG9j
a2luZ19wYXRocyJ9OgogICAgICAgIHBhdGhzID0gcmV2aWV3WyJibG9ja2luZ19wYXRocyJdCiAgICAgICAgcmV0
dXJuICh2ZXJkaWN0ICE9ICJ1bnBhcnNlZCIgYW5kIGlzaW5zdGFuY2UocHJpb3JpdGllcywgZGljdCkgYW5kIGlz
aW5zdGFuY2UocGF0aHMsIGxpc3QpCiAgICAgICAgICAgICAgICBhbmQgYWxsKGlzaW5zdGFuY2UocCwgc3RyKSBh
bmQgX1BBVEhfRElHRVNULmZ1bGxtYXRjaChwKSBmb3IgcCBpbiBwYXRocykKICAgICAgICAgICAgICAgIGFuZCBs
ZW4ocGF0aHMpID09IHN1bShwcmlvcml0aWVzLmdldChwLCAwKSBmb3IgcCBpbiBCTE9DS0lOR19QUklPUklUSUVT
KSkKICAgIGlmIHZlcmRpY3QgIT0gInVucGFyc2VkIiBvciBwcmlvcml0aWVzIGlzIG5vdCBOb25lIG9yIHJldmll
dy5rZXlzKCkgIT0gewogICAgICAgICJ2ZXJkaWN0IiwgInByaW9yaXRpZXMiLCAidW5wYXJzZWRfcmVhc29uIiwg
InVucGFyc2VkX3NhbXBsZSIsCiAgICB9OgogICAgICAgIHJldHVybiBGYWxzZQogICAgcmVhc29uID0gcmV2aWV3
WyJ1bnBhcnNlZF9yZWFzb24iXQogICAgc2FtcGxlID0gcmV2aWV3WyJ1bnBhcnNlZF9zYW1wbGUiXQogICAgaWYg
bm90IGlzaW5zdGFuY2UocmVhc29uLCBzdHIpIG9yIHJlYXNvbiBub3QgaW4gVU5QQVJTRURfUkVBU09OUzoKICAg
ICAgICByZXR1cm4gRmFsc2UKICAgIGlmIHJlYXNvbiBpbiAoIm1pc3NpbmdfcmVzdWx0IiwgInRvb19iaWciKToK
ICAgICAgICByZXR1cm4gc2FtcGxlIGlzIE5vbmUKICAgICMgVGhlIHdyaXRlcidzIGVsaWdpYmlsaXR5IHJ1bGUg
YWdhaW46IGEgcHVibGljIHJlcG9zaXRvcnksIGFuZCBhIGNhdGFsb2d1ZWQgcHVibGljIGRhdGEgY2xhc3Mgd2hl
biBvbmUgaXMgbmFtZWQuCiAgICAjIEJvdGggYXJlIGNhdGFsb2cgZmFjdHM7IGEgcmVhZGVyIHRoYXQgbXVzdCBu
b3QgZGVwZW5kIG9uIGEgY2F0YWxvZyBza2lwcyB0aGVtIGFuZCBrZWVwcyB0aGUgc2FtcGxlIHNoYXBlIGNoZWNr
LgogICAgaWYgY2hlY2tfc2FtcGxlX3Zpc2liaWxpdHkgYW5kIChub3QgX3JlcG9faXNfcHVibGljKHJlcG8pIG9y
ICgKICAgICAgICAgICAgZGF0YV9jbGFzcyBpcyBub3QgTm9uZSBhbmQgKG5vdCBpc2luc3RhbmNlKGRhdGFfY2xh
c3MsIHN0cikgb3IgZGF0YV9jbGFzcyBub3QgaW4gX3B1YmxpY19kYXRhX2NsYXNzZXMoKSkpKToKICAgICAgICBy
ZXR1cm4gc2FtcGxlIGlzIE5vbmUKICAgIHJldHVybiBfb3B0aW9uYWwoc2FtcGxlLCBfc2FtcGxlX29rKQoKCmRl
ZiBfd2VsbF9mb3JtZWQocm93OiBNYXBwaW5nW3N0ciwgQW55XSwgKiwgY2hlY2tfc2FtcGxlX3Zpc2liaWxpdHk6
IGJvb2wgPSBUcnVlKSAtPiBib29sOgogICAgIiIiV2hldGhlciBldmVyeSBuZXN0ZWQgdmFsdWUgaGFzIHRoZSBz
aGFwZSBgYHN1bW1hcnlfcm93YGAgd3JpdGVzOyBhIGRhbWFnZWQgcm93IGlzIHNraXBwZWQsIG5ldmVyIHRydXN0
ZWQuCgogICAgYGBjaGVja19zYW1wbGVfdmlzaWJpbGl0eT1GYWxzZWBgIHJlYWRzIG5vIGNhdGFsb2c6IGFuIHVu
cGFyc2VkLXJlcGx5IHNhbXBsZSBpcyBjaGVja2VkIGZvciBpdHMgb3duIHNoYXBlIG9ubHksCiAgICBub3QgZm9y
IHRoZSByZXBvc2l0b3J5IGFuZCBkYXRhLWNsYXNzIHZpc2liaWxpdHkgdGhlIHdyaXRlciBhcHBsaWVkIHdoZW4g
aXQgc3RvcmVkIGl0LgogICAgIiIiCiAgICB0b2tlbnNfID0gcm93LmdldCgidG9rZW5zIikKICAgIGFsbG93YW5j
ZSA9IHJvdy5nZXQoImFsbG93YW5jZSIpCiAgICByZXZpZXcgPSByb3cuZ2V0KCJyZXZpZXciKQogICAgbGFuZGlu
ZyA9IHJvdy5nZXQoImxhbmRpbmciKQogICAgcmV0dXJuIGFsbCgoCiAgICAgICAgYWxsKF9vcHRpb25hbChyb3cu
Z2V0KG5hbWUpLCBsYW1iZGEgdjogdGltZXN0YW1wKHYpIGlzIG5vdCBOb25lKSBmb3IgbmFtZSBpbiAoInN0YXJ0
ZWRfYXQiLCAiZmluaXNoZWRfYXQiKSksCiAgICAgICAgX29wdGlvbmFsKHJvdy5nZXQoImR1cmF0aW9uX3MiKSwg
X21lYXN1cmUpLAogICAgICAgIF9vcHRpb25hbChyb3cuZ2V0KCJuZXZlcl9zdGFydGVkX3JlYXNvbiIpLCBsYW1i
ZGEgdjogaXNpbnN0YW5jZSh2LCBzdHIpIGFuZCB2IGluIF9SRUZVU0FMX0NPREVTCiAgICAgICAgICAgICAgICAg
IGFuZCByb3cuZ2V0KCJzdGF0dXMiKSA9PSBORVZFUl9TVEFSVEVEKSwKICAgICAgICBfb3B0aW9uYWwocm93Lmdl
dCgibm9fZGVsaXZlcmFibGVfY2F1c2UiKSwgbGFtYmRhIHY6IGlzaW5zdGFuY2Uodiwgc3RyKSBhbmQgdiBpbiBO
T19ERUxJVkVSQUJMRV9DQVVTRVMKICAgICAgICAgICAgICAgICAgYW5kIHJvdy5nZXQoInN0YXR1cyIpID09ICJu
b19kZWxpdmVyYWJsZSIpLAogICAgICAgIGlzaW5zdGFuY2UodG9rZW5zXywgZGljdCkgYW5kIHRva2Vuc18ua2V5
cygpID09IHsiZnJlc2hfaW5wdXQiLCAiY2FjaGVkX2lucHV0IiwgIm91dHB1dCJ9CiAgICAgICAgYW5kIGFsbChf
Y291bnRfdmFsdWUodikgZm9yIHYgaW4gdG9rZW5zXy52YWx1ZXMoKSksCiAgICAgICAgX29wdGlvbmFsKHJvdy5n
ZXQoInVzYWdlX2tub3duIiksIGxhbWJkYSB2OiB0eXBlKHYpIGlzIGJvb2wpLAogICAgICAgIF9vcHRpb25hbChy
b3cuZ2V0KCJpbXBsZW1lbnRhdGlvbiIpLCBsYW1iZGEgdjogdHlwZSh2KSBpcyBib29sIGFuZCAobm90IHYgb3Ig
cm93LmdldCgicm9sZSIpID09ICJ3cml0ZSIpKSwKICAgICAgICBfb3B0aW9uYWwoYWxsb3dhbmNlLCBsYW1iZGEg
YTogaXNpbnN0YW5jZShhLCBkaWN0KSBhbmQgYS5rZXlzKCkgPT0geyJwb29sIiwgInN0YXJ0IiwgImZpbmlzaCJ9
CiAgICAgICAgICAgICAgICAgIGFuZCAoYVsicG9vbCJdIGlzIE5vbmUgb3IgKGlzaW5zdGFuY2UoYVsicG9vbCJd
LCBzdHIpIGFuZCBhWyJwb29sIl0gaW4gUE9PTFMpKQogICAgICAgICAgICAgICAgICBhbmQgX3JlYWRpbmdfb2so
YVsic3RhcnQiXSkgYW5kIF9yZWFkaW5nX29rKGFbImZpbmlzaCJdKSksCiAgICAgICAgX3Jldmlld19vayhyZXZp
ZXcsIHJvbGU9cm93LmdldCgicm9sZSIpLCBzdGF0dXM9cm93LmdldCgic3RhdHVzIiksIHJlcG89cm93LmdldCgi
cmVwbyIpLAogICAgICAgICAgICAgICAgICAgZGF0YV9jbGFzcz1yb3cuZ2V0KCJkYXRhX2NsYXNzIiksIGNoZWNr
X3NhbXBsZV92aXNpYmlsaXR5PWNoZWNrX3NhbXBsZV92aXNpYmlsaXR5KSwKICAgICAgICBfb3B0aW9uYWwocm93
LmdldCgicmV2aWV3X2V2aWRlbmNlX2Fic2VudCIpLCBsYW1iZGEgZmxhZzogZmxhZyBpcyBUcnVlIGFuZCByb3cu
Z2V0KCJyb2xlIikgPT0gInJldmlldyIKICAgICAgICAgICAgICAgICAgYW5kIHJvdy5nZXQoInN0YXR1cyIpICE9
IE5FVkVSX1NUQVJURUQgYW5kIChyZXZpZXcgaXMgTm9uZSBvciAoCiAgICAgICAgICAgICAgICAgICAgICBpc2lu
c3RhbmNlKHJldmlldywgZGljdCkgYW5kIHJldmlldy5nZXQoInZlcmRpY3QiKSA9PSAidW5wYXJzZWQiKSkpLAog
ICAgICAgIF9vcHRpb25hbChyb3cuZ2V0KCJwciIpLCBfY291bnRfdmFsdWUpLAogICAgICAgIF9vcHRpb25hbChy
b3cuZ2V0KCJyZXBvc2l0b3J5IiksIGxhbWJkYSB2OiBpc2luc3RhbmNlKHYsIHN0cikgYW5kIFJFUE9TSVRPUllf
U0xVRy5mdWxsbWF0Y2godikgaXMgbm90IE5vbmUpLAogICAgICAgIF9vcHRpb25hbChyb3cuZ2V0KCJwaW5uZWRf
aGVhZCIpLCBfc2hhKSwKICAgICAgICBfb3B0aW9uYWwobGFuZGluZywgbGFtYmRhIGQ6IGlzaW5zdGFuY2UoZCwg
ZGljdCkgYW5kIGQua2V5cygpID09IHsicHIiLCAibWVyZ2Vfc2hhIn0KICAgICAgICAgICAgICAgICAgYW5kIF9j
b3VudF92YWx1ZShkWyJwciJdKSBhbmQgX3NoYShkWyJtZXJnZV9zaGEiXSkpLAogICAgICAgIF9vcHRpb25hbChy
b3cuZ2V0KCJzb3VyY2VfbXRpbWVfbnMiKSwgX2NvdW50X3ZhbHVlKSwKICAgICkpCgoKZGVmIHJlYWRfcm93cyhi
YXRjaF9zdGF0ZV9kaXI6IFBhdGgpIC0+IHR1cGxlW2xpc3RbZGljdFtzdHIsIEFueV1dLCBDb3VudGVyW3N0cl1d
OgogICAgIiIiRXZlcnkgdGFzaydzIGxhdGVzdCByb3cgKGEgbGF0ZXIgbGluZSBvZiBhIHRhc2sgaWQgc3VwZXJz
ZWRlcyBhbiBlYXJsaWVyIG9uZSksIGFuZCByZWFkIGNvdW50cy4iIiIKICAgIGNvdW50czogQ291bnRlcltzdHJd
ID0gQ291bnRlcigpCiAgICBsYXRlc3Q6IGRpY3Rbc3RyLCBkaWN0W3N0ciwgQW55XV0gPSB7fQogICAgdHJ5Ogog
ICAgICAgIGZkID0gX29wZW4ocm93c19wYXRoKGJhdGNoX3N0YXRlX2RpciksIG9zLk9fUkRPTkxZKQogICAgZXhj
ZXB0IEZpbGVOb3RGb3VuZEVycm9yOgogICAgICAgIGNvdW50c1sibWlzc2luZyJdICs9IDEKICAgICAgICByZXR1
cm4gW10sIGNvdW50cwogICAgd2l0aCBvcy5mZG9wZW4oZmQsICJyYiIpIGFzIHN0cmVhbToKICAgICAgICBmb3Ig
bGluZSBpbiBzdHJlYW06CiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHJvdyA9IF9sb2FkX2xpbmUo
bGluZSkKICAgICAgICAgICAgZXhjZXB0IChWYWx1ZUVycm9yLCBSZWN1cnNpb25FcnJvcik6CiAgICAgICAgICAg
ICAgICBjb3VudHNbIm1hbGZvcm1lZF9saW5lcyJdICs9IDEKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAg
ICAgICAgIGlmIChub3QgaXNpbnN0YW5jZShyb3csIGRpY3QpIG9yIHJvdy5nZXQoInNjaGVtYSIpICE9IFJPV19T
Q0hFTUEgb3Igbm90IHZhbGlkX3Rhc2tfaWQocm93LmdldCgidGFza19pZCIpKQogICAgICAgICAgICAgICAgICAg
IG9yIG5vdCBfd2VsbF9mb3JtZWQocm93KSk6CiAgICAgICAgICAgICAgICBjb3VudHNbIm1hbGZvcm1lZF9saW5l
cyJdICs9IDEKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIGNvdW50c1sibGluZXMiXSArPSAx
CiAgICAgICAgICAgIGxhdGVzdFtyb3dbInRhc2tfaWQiXV0gPSBfc2FuaXRpc2VkKHJvdykKICAgIGNvdW50c1si
cm93cyJdID0gbGVuKGxhdGVzdCkKICAgIHJldHVybiBsaXN0KGxhdGVzdC52YWx1ZXMoKSksIGNvdW50cwoKCmRl
ZiBfc2V0dGxlZF9yZWNvcmRzKHRhc2tzX2RpcjogUGF0aCwgY291bnRzOiBDb3VudGVyW3N0cl0pIC0+IEl0ZXJh
dG9yW3R1cGxlW1BhdGgsIGRpY3Rbc3RyLCBBbnldLCBpbnRdXToKICAgIHNlZW46IHNldFtzdHJdID0gc2V0KCkK
ICAgIGZvciBwYXRoIGluIGl0ZXJfdGFza19yZWNvcmRzKHRhc2tzX2RpciwgaW5jbHVkZV9hcmNoaXZlPVRydWUp
OgogICAgICAgIGlmIHBhdGgubmFtZS5lbmRzd2l0aCgiLndyaXRlLWJvdW5kYXJ5Lmpzb24iKSBvciAiLnNhbmRi
b3guIiBpbiBwYXRoLm5hbWU6CiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgdHJ5OgogICAgICAgICAgICAj
IFN0YXQgZmlyc3Q6IGEgd3JpdGUgYWZ0ZXIgdGhlIHN0YXQgaXMgbmV3ZXIgdGhhbiB0aGlzIHJlYWQsIHNvIGl0
cyBvd24gcm93IG91dHJhbmtzIHRoaXMgb25lLgogICAgICAgICAgICBtdGltZV9ucyA9IG9zLnN0YXQocGF0aCku
c3RfbXRpbWVfbnMKICAgICAgICAgICAgcmVjb3JkID0gcmVhZF9jYWNoZWRfdGFza19yZWNvcmQocGF0aCwgdGFz
a3NfZGlyPXRhc2tzX2RpcikKICAgICAgICBleGNlcHQgKE9TRXJyb3IsIFZhbHVlRXJyb3IsIFR5cGVFcnJvciwg
UmVjdXJzaW9uRXJyb3IpOgogICAgICAgICAgICBjb3VudHNbInVucmVhZGFibGUiXSArPSAxCiAgICAgICAgICAg
IGNvbnRpbnVlCiAgICAgICAgaWYgbm90IGlzaW5zdGFuY2UocmVjb3JkLCBkaWN0KSBvciBub3QgaXNpbnN0YW5j
ZShyZWNvcmQuZ2V0KCJ0YXNrX2lkIiksIHN0cik6CiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgaWYgbm90
IHZhbGlkX3Rhc2tfaWQocmVjb3JkWyJ0YXNrX2lkIl0pOgogICAgICAgICAgICBjb3VudHNbImludmFsaWRfdGFz
a19pZCJdICs9IDEKICAgICAgICAgICAgY29udGludWUKICAgICAgICBpZiByZWNvcmRbInRhc2tfaWQiXSBpbiBz
ZWVuOiAgIyB0aGUgaG90IHJlY29yZCBvdXRyYW5rcyBhbiBhcmNoaXZlZCBjb3B5CiAgICAgICAgICAgIGNvdW50
c1siZHVwbGljYXRlX3Rhc2tfaWRzIl0gKz0gMQogICAgICAgICAgICBjb250aW51ZQogICAgICAgIHNlZW4uYWRk
KHJlY29yZFsidGFza19pZCJdKQogICAgICAgIHlpZWxkIHBhdGgsIHJlY29yZCwgbXRpbWVfbnMKCgpkZWYgX3N0
YWxlKHByZXNlbnQ6IE1hcHBpbmdbc3RyLCBBbnldIHwgTm9uZSwgY2FuZGlkYXRlOiBNYXBwaW5nW3N0ciwgQW55
XSkgLT4gYm9vbDoKICAgICIiIlJlZnJlc2ggbWlzc2luZyBvciBvbGRlciByb3dzLCBvciBhIHJvdyBvZiB0aGUg
c2FtZSByZWNvcmQgdmVyc2lvbiB0aGF0IGl0cyBzaWRlY2FyIG5vIGxvbmdlciBzdXBwb3J0cy4KCiAgICBBIHJv
dyBzaG93cyBvbmx5IHdoYXQgaXRzIHJlY29yZCBhbmQgc2lkZWNhciBwcm92ZSB3aGVuIGl0IGlzIHdyaXR0ZW46
IG5vdGhpbmcgaXMgY2FycmllZCBvdmVyIGZyb20gYSBzdG9yZWQKICAgIHJvdywgc28gYSBzaWRlY2FyIHRoYXQg
aXMgZ29uZSwgcmVwbGFjZWQgb3IgdW5yZWFkYWJsZSBub3cgeWllbGRzIHRoZSByb3cgaXQgeWllbGRzIG9uIGl0
cyBvd24uCiAgICAiIiIKICAgIGlmIHByZXNlbnQgaXMgTm9uZToKICAgICAgICByZXR1cm4gVHJ1ZQogICAgc3Rv
cmVkID0gcHJlc2VudC5nZXQoInNvdXJjZV9tdGltZV9ucyIpCiAgICBzb3VyY2UgPSBjYW5kaWRhdGVbInNvdXJj
ZV9tdGltZV9ucyJdCiAgICBpZiBub3QgaXNpbnN0YW5jZShzdG9yZWQsIGludCkgb3Igc3RvcmVkIDwgc291cmNl
OgogICAgICAgIHJldHVybiBUcnVlCiAgICByZXR1cm4gc3RvcmVkID09IHNvdXJjZSBhbmQgYW55KHByZXNlbnQu
Z2V0KGtleSkgIT0gY2FuZGlkYXRlLmdldChrZXkpIGZvciBrZXkgaW4gKAogICAgICAgICJyb2xlIiwgImNsYXNz
IiwgInJldmlldyIsICJlZmZvcnQiLCAicGlubmVkX2hlYWQiLCAibmV2ZXJfc3RhcnRlZF9yZWFzb24iLCAibm9f
ZGVsaXZlcmFibGVfY2F1c2UiLAogICAgICAgICJyZXZpZXdfZXZpZGVuY2VfYWJzZW50IiwgInVzYWdlX2tub3du
IiwgImltcGxlbWVudGF0aW9uIiwgInRva2VucyIsCiAgICApKQoKCmRlZiBiYWNrZmlsbCh0YXNrc19kaXI6IFBh
dGgsIGJhdGNoX3N0YXRlX2RpcjogUGF0aCwgKiwgZHJ5X3J1bjogYm9vbCA9IEZhbHNlKSAtPiBkaWN0W3N0ciwg
aW50XToKICAgICIiIkFkZCBhIHJvdyBmb3IgZXZlcnkgc2V0dGxlZCByZWNvcmQgd2l0aG91dCBhIGN1cnJlbnQg
b25lOyBpZGVtcG90ZW50LiBSZWNvcmRzIGFyZSByZWFkIG91dHNpZGUgdGhlIGxvY2suCgogICAgQSBuZXcgbGFz
dCBsaW5lIHN1cGVyc2VkZXMgYW4gb2xkZXIgcmVjb3JkIHZlcnNpb24sIG9yIGEgcm93IHdob3NlIHJldmlldyBp
dHMgcmVjb3JkJ3Mgc2lkZWNhciBubyBsb25nZXIgZ2l2ZXMuCiAgICAiIiIKICAgIGNvdW50czogQ291bnRlcltz
dHJdID0gQ291bnRlcigpCiAgICBjYW5kaWRhdGVzOiBsaXN0W3R1cGxlW1BhdGgsIGRpY3Rbc3RyLCBBbnldLCBk
aWN0W3N0ciwgQW55XV1dID0gW10KICAgIGZvciBwYXRoLCByZWNvcmQsIG10aW1lX25zIGluIF9zZXR0bGVkX3Jl
Y29yZHModGFza3NfZGlyLCBjb3VudHMpOgogICAgICAgIHJvdyA9IHN1bW1hcnlfcm93KHBhdGgsIHJlY29yZCwg
bXRpbWVfbnMpCiAgICAgICAgaWYgcm93IGlzIE5vbmU6CiAgICAgICAgICAgIGNvdW50c1siYWN0aXZlX29yX2Zv
cmVpZ24iXSArPSAxCiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgY2FuZGlkYXRlcy5hcHBlbmQoKHBhdGgs
IHJlY29yZCwgcm93KSkKICAgIGNvdW50c1sic2V0dGxlZF9yZWNvcmRzIl0gPSBsZW4oY2FuZGlkYXRlcykKICAg
IGlmIGRyeV9ydW46CiAgICAgICAgcHJlc2VudCA9IHtyb3dbInRhc2tfaWQiXTogcm93IGZvciByb3cgaW4gcmVh
ZF9yb3dzKGJhdGNoX3N0YXRlX2RpcilbMF19CiAgICAgICAgY291bnRzWyJ3b3VsZF9hZGQiXSA9IHN1bShyb3db
InRhc2tfaWQiXSBub3QgaW4gcHJlc2VudCBmb3IgXywgXywgcm93IGluIGNhbmRpZGF0ZXMpCiAgICAgICAgY291
bnRzWyJ3b3VsZF9yZWZyZXNoIl0gPSBzdW0ocm93WyJ0YXNrX2lkIl0gaW4gcHJlc2VudCBhbmQgX3N0YWxlKHBy
ZXNlbnRbcm93WyJ0YXNrX2lkIl1dLCByb3cpCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg
Zm9yIF8sIF8sIHJvdyBpbiBjYW5kaWRhdGVzKQogICAgICAgIHJldHVybiBkaWN0KHNvcnRlZChjb3VudHMuaXRl
bXMoKSkpCiAgICB3aXRoIF9sb2NrZWQoYmF0Y2hfc3RhdGVfZGlyKSBhcyBwYXRoOgogICAgICAgIHByZXNlbnQg
PSB7cm93WyJ0YXNrX2lkIl06IHJvdyBmb3Igcm93IGluIHJlYWRfcm93cyhiYXRjaF9zdGF0ZV9kaXIpWzBdfQog
ICAgICAgIHdyaXR0ZW4gPSBbXQogICAgICAgIGZvciBzb3VyY2UsIHJlY29yZCwgcm93IGluIGNhbmRpZGF0ZXM6
CiAgICAgICAgICAgIHN0b3JlZCA9IHByZXNlbnQuZ2V0KHJvd1sidGFza19pZCJdKQogICAgICAgICAgICBpZiBf
c3RhbGUoc3RvcmVkLCByb3cpOgogICAgICAgICAgICAgICAgIyBSb2xlIGFuZCB0aGUgcGF5bG9hZCBjYW4gYm90
aCBmb2xsb3cgdGhlIHNpZGVjYXIsIGluY2x1ZGluZyB3aGVuIHRoZSB1bmxvY2tlZAogICAgICAgICAgICAgICAg
IyBwYXlsb2FkIGlzIG51bGwuIFJlLXJlYWQgdW5kZXIgdGhlIGxvY2sgYmVmb3JlIHRoaXMgcm93IHJlcGxhY2Vz
IGEgc3RvcmVkIG9uZS4KICAgICAgICAgICAgICAgIHJvdyA9IHN1bW1hcnlfcm93KHNvdXJjZSwgcmVjb3JkLCBy
b3dbInNvdXJjZV9tdGltZV9ucyJdKSBvciByb3cKICAgICAgICAgICAgaWYgX3N0YWxlKHN0b3JlZCwgcm93KToK
ICAgICAgICAgICAgICAgIHdyaXR0ZW4uYXBwZW5kKHJvdykKICAgICAgICBjb3VudHNbImFkZGVkIl0gPSBzdW0o
cm93WyJ0YXNrX2lkIl0gbm90IGluIHByZXNlbnQgZm9yIHJvdyBpbiB3cml0dGVuKQogICAgICAgIGNvdW50c1si
cmVmcmVzaGVkIl0gPSBsZW4od3JpdHRlbikgLSBjb3VudHNbImFkZGVkIl0KICAgICAgICBjb3VudHNbImFscmVh
ZHlfcHJlc2VudCJdID0gbGVuKGNhbmRpZGF0ZXMpIC0gbGVuKHdyaXR0ZW4pCiAgICAgICAgaWYgd3JpdHRlbjoK
ICAgICAgICAgICAgIyBUaGUgZXhpc3RpbmcgbGluZXMsIHRoZW4gdGhlIG5ldyBvbmVzIGxhc3QsIHNvIGEgbmV3
IGxpbmUgc3VwZXJzZWRlcyBhbiBvbGRlciByb3cgb2YgaXRzIHRhc2sgaWQuCiAgICAgICAgICAgIHRlbXBvcmFy
eSA9IHBhdGgud2l0aF9uYW1lKGYiLntST1dTX0ZJTEV9Lntvcy5nZXRwaWQoKX0ue3RpbWUudGltZV9ucygpfS50
bXAiKQogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICB3aXRoIG9zLmZkb3Blbihfb3Blbih0ZW1wb3Jh
cnksIG9zLk9fV1JPTkxZIHwgb3MuT19DUkVBVCB8IG9zLk9fRVhDTCksICJ3YiIpIGFzIHN0cmVhbToKICAgICAg
ICAgICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICAgICAgICAgIHdpdGggb3MuZmRvcGVuKF9vcGVuKHBh
dGgsIG9zLk9fUkRPTkxZKSwgInJiIikgYXMgZXhpc3Rpbmc6CiAgICAgICAgICAgICAgICAgICAgICAgICAgICB0
YWlsID0gYiJcbiIKICAgICAgICAgICAgICAgICAgICAgICAgICAgIHdoaWxlIGNodW5rIDo9IGV4aXN0aW5nLnJl
YWQoMSA8PCAyMCk6CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgc3RyZWFtLndyaXRlKGNodW5rKQog
ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHRhaWwgPSBjaHVua1stMTpdCiAgICAgICAgICAgICAgICAg
ICAgICAgICAgICBpZiB0YWlsICE9IGIiXG4iOgogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHN0cmVh
bS53cml0ZShiIlxuIikKICAgICAgICAgICAgICAgICAgICBleGNlcHQgRmlsZU5vdEZvdW5kRXJyb3I6CiAgICAg
ICAgICAgICAgICAgICAgICAgIHBhc3MKICAgICAgICAgICAgICAgICAgICBmb3Igcm93IGluIHdyaXR0ZW46CiAg
ICAgICAgICAgICAgICAgICAgICAgIHN0cmVhbS53cml0ZShfbGluZShyb3cpKQogICAgICAgICAgICAgICAgICAg
IHN0cmVhbS5mbHVzaCgpCiAgICAgICAgICAgICAgICAgICAgb3MuZnN5bmMoc3RyZWFtLmZpbGVubygpKQogICAg
ICAgICAgICAgICAgb3MucmVwbGFjZSh0ZW1wb3JhcnksIHBhdGgpCiAgICAgICAgICAgIGZpbmFsbHk6CiAgICAg
ICAgICAgICAgICB0ZW1wb3JhcnkudW5saW5rKG1pc3Npbmdfb2s9VHJ1ZSkKICAgIHJldHVybiBkaWN0KHNvcnRl
ZChjb3VudHMuaXRlbXMoKSkpCgoKZGVmIF9wYXJzZXIoKSAtPiBhcmdwYXJzZS5Bcmd1bWVudFBhcnNlcjoKICAg
IHBhcnNlciA9IGFyZ3BhcnNlLkFyZ3VtZW50UGFyc2VyKAogICAgICAgIHByb2c9InB5dGhvbiAtbSBsdV9tYWNo
aW5lcnkuYXBpLm1vZGVsX3N0YXRzX3Jvd3MiLAogICAgICAgIGRlc2NyaXB0aW9uPSgKICAgICAgICAgICAgIkJh
Y2tmaWxsIHRoZSBkdXJhYmxlIG1vZGVsLXN0YXRzIHN1bW1hcnkgcm93cyAoIzU4MSkgZnJvbSBldmVyeSBkaXNw
YXRjaCB0YXNrIHJlY29yZCwgaG90IGFuZCBhcmNoaXZlZC5cbiIKICAgICAgICAgICAgIlJlcnVuIGFmdGVyIGFu
IHVwZGF0ZSB0byByZWNvdmVyIHJldmlldyBldmlkZW5jZSBmcm9tIHJldGFpbmVkIHRhc2sgcmVzdWx0cy4gIgog
ICAgICAgICAgICAiTm90IGZvciBsaXZlIHN0YXRpc3RpY3M6IHRoZSBNb25pdG9yIHJvdXRlIC9hcGkvZmxlZXQv
bW9kZWwtc3RhdHMgcmVhZHMgdGhlIHJvd3MuIgogICAgICAgICksCiAgICAgICAgZm9ybWF0dGVyX2NsYXNzPWFy
Z3BhcnNlLlJhd0Rlc2NyaXB0aW9uSGVscEZvcm1hdHRlciwKICAgICAgICBlcGlsb2c9KAogICAgICAgICAgICAi
RXhhbXBsZXM6XG4iCiAgICAgICAgICAgICIgIC52ZW52L2Jpbi9weXRob24gLW0gbHVfbWFjaGluZXJ5LmFwaS5t
b2RlbF9zdGF0c19yb3dzIGJhY2tmaWxsIC0tZHJ5LXJ1blxuIgogICAgICAgICAgICAiICAudmVudi9iaW4vcHl0
aG9uIC1tIGx1X21hY2hpbmVyeS5hcGkubW9kZWxfc3RhdHNfcm93cyBiYWNrZmlsbFxuIgogICAgICAgICAgICAi
ICAudmVudi9iaW4vcHl0aG9uIC1tIGx1X21hY2hpbmVyeS5hcGkubW9kZWxfc3RhdHNfcm93cyBiYWNrZmlsbCAt
LWJhdGNoLXN0YXRlLWRpciBiYXRjaF9zdGF0ZVxuXG4iCiAgICAgICAgICAgICJPdXRwdXRzOiBhcHBlbmRzIG1p
c3Npbmcgcm93cyB0byA8YmF0Y2gtc3RhdGUtZGlyPi9tb2RlbC1zdGF0cy9yb3dzLmpzb25sIChhdG9taWMgcmVw
bGFjZSB1bmRlclxuIgogICAgICAgICAgICAicm93cy5sb2NrKSBhbmQgcHJpbnRzIG9uZSBKU09OIG9iamVjdCBv
ZiBjb3VudHMgKHNldHRsZWRfcmVjb3JkcywgYWRkZWQsIHJlZnJlc2hlZCwgYWxyZWFkeV9wcmVzZW50LFxuIgog
ICAgICAgICAgICAidW5yZWFkYWJsZSwgZHVwbGljYXRlX3Rhc2tfaWRzLCBhY3RpdmVfb3JfZm9yZWlnbjsgd291
bGRfYWRkIGFuZCB3b3VsZF9yZWZyZXNoIHdpdGggLS1kcnktcnVuKS5cbiIKICAgICAgICAgICAgIkEgcmVmcmVz
aGVkIHJvdyByZXBsYWNlcyBvbmUgdGFrZW4gZnJvbSBhbiBvbGRlciB2ZXJzaW9uIG9mIGl0cyByZWNvcmQsIG9y
IG9uZSB3aG9zZSByZXZpZXcgdGhlXG4iCiAgICAgICAgICAgICJyZWNvcmQncyBzaWRlY2FyIG5vIGxvbmdlciBn
aXZlcyAocm93cyBjYXJyeSBub3RoaW5nIG92ZXIgZnJvbSB0aGUgc3RvcmVkIHJvdykuIFRhc2sgcmVjb3JkcyBh
cmUgbmV2ZXJcbiIKICAgICAgICAgICAgImNoYW5nZWQuXG5cbiIKICAgICAgICAgICAgIkV4aXQgY29kZXM6IDAg
cm93cyB3cml0dGVuIG9yIG5vdGhpbmcgdG8gYWRkOyAxIHRoZSB0YXNrIHN0b3JlIG9yIHRoZSByb3dzIGZpbGUg
Y291bGQgbm90IGJlIHVzZWQuXG5cbiIKICAgICAgICAgICAgIlJlbGF0ZWQ6IGx1X21hY2hpbmVyeS9hcGkvbW9k
ZWxfc3RhdHNfcm91dGVyLnB5LCBkb2NzL3J1bmJvb2tzL21vZGVsLXN0YXRzLm1kLCBpc3N1ZSAjNTgxLiIKICAg
ICAgICApLAogICAgKQogICAgY29tbWFuZHMgPSBwYXJzZXIuYWRkX3N1YnBhcnNlcnMoZGVzdD0iY29tbWFuZCIs
IHJlcXVpcmVkPVRydWUsIG1ldGF2YXI9ImJhY2tmaWxsIikKICAgIHJ1biA9IGNvbW1hbmRzLmFkZF9wYXJzZXIo
CiAgICAgICAgImJhY2tmaWxsIiwgaGVscD0iQWRkIGEgc3VtbWFyeSByb3cgZm9yIGV2ZXJ5IHNldHRsZWQgdGFz
ayByZWNvcmQgdGhhdCBoYXMgbm9uZSAoaWRlbXBvdGVudCkuIiwKICAgICAgICBkZXNjcmlwdGlvbj0iQWRkIGEg
c3VtbWFyeSByb3cgZm9yIGV2ZXJ5IHNldHRsZWQgdGFzayByZWNvcmQgKGhvdCBkaXJlY3RvcnksIHRoZW4gYXJj
aGl2ZSkgdGhhdCBoYXMgbm9uZS4iLAogICAgKQogICAgcnVuLmFkZF9hcmd1bWVudCgKICAgICAgICAiLS1iYXRj
aC1zdGF0ZS1kaXIiLCB0eXBlPVBhdGgsIGRlZmF1bHQ9Tm9uZSwKICAgICAgICBoZWxwPSJUaGUgYmF0Y2hfc3Rh
dGUgZGlyZWN0b3J5IGhvbGRpbmcgdGFza3MvIGFuZCBtb2RlbC1zdGF0cy8gKGRlZmF1bHQ6IHRoZSBkaXNwYXRj
aCB0YXNrIHN0b3JlJ3MgIgogICAgICAgICAgICAgInBhcmVudCwgTFVfVEFTS1NfRElSJ3MgcGFyZW50IHdoZW4g
c2V0KS4gRXhhbXBsZTogLS1iYXRjaC1zdGF0ZS1kaXIgYmF0Y2hfc3RhdGUiLAogICAgKQogICAgcnVuLmFkZF9h
cmd1bWVudCgKICAgICAgICAiLS1kcnktcnVuIiwgYWN0aW9uPSJzdG9yZV90cnVlIiwKICAgICAgICBoZWxwPSJD
b3VudCB0aGUgcm93cyB0aGF0IHdvdWxkIGJlIGFkZGVkIG9yIHJlZnJlc2hlZCBhbmQgd3JpdGUgbm90aGluZyAo
ZGVmYXVsdDogd3JpdGUpLiIsCiAgICApCiAgICByZXR1cm4gcGFyc2VyCgoKZGVmIG1haW4oYXJndjogbGlzdFtz
dHJdIHwgTm9uZSA9IE5vbmUpIC0+IGludDoKICAgIGFyZ3MgPSBfcGFyc2VyKCkucGFyc2VfYXJncyhhcmd2KQog
ICAgYmF0Y2hfc3RhdGVfZGlyID0gYXJncy5iYXRjaF9zdGF0ZV9kaXIgaWYgYXJncy5iYXRjaF9zdGF0ZV9kaXIg
aXMgbm90IE5vbmUgZWxzZSB0YXNrc19kaXIoKS5wYXJlbnQKICAgIHRhc2tzID0gYmF0Y2hfc3RhdGVfZGlyIC8g
InRhc2tzIgogICAgaWYgbm90IHRhc2tzLmlzX2RpcigpOgogICAgICAgIHByaW50KGpzb24uZHVtcHMoeyJlcnJv
ciI6ICJ0YXNrIHN0b3JlIG5vdCBmb3VuZCJ9KSwgZmlsZT1zeXMuc3RkZXJyKQogICAgICAgIHJldHVybiAxCiAg
ICB0cnk6CiAgICAgICAgY291bnRzID0gYmFja2ZpbGwodGFza3MsIGJhdGNoX3N0YXRlX2RpciwgZHJ5X3J1bj1h
cmdzLmRyeV9ydW4pCiAgICBleGNlcHQgT1NFcnJvciBhcyBleGM6CiAgICAgICAgcHJpbnQoanNvbi5kdW1wcyh7
ImVycm9yIjogZiJyb3dzIGZpbGUgdW5hdmFpbGFibGU6IHt0eXBlKGV4YykuX19uYW1lX199In0pLCBmaWxlPXN5
cy5zdGRlcnIpCiAgICAgICAgcmV0dXJuIDEKICAgIHByaW50KGpzb24uZHVtcHMoY291bnRzLCBzb3J0X2tleXM9
VHJ1ZSkpCiAgICByZXR1cm4gMAoKCmlmIF9fbmFtZV9fID09ICJfX21haW5fXyI6CiAgICByYWlzZSBTeXN0ZW1F
eGl0KG1haW4oKSkK
"""


def _prior_release_rows(tmp_path: Path):
    """The finding-outcome writer from release 0.1.84, with that release's label vocabulary."""
    import base64
    import importlib.util

    source = base64.b64decode("".join(_PRIOR_RELEASE_ROWS_B64.split()))
    blob = tmp_path / "model_stats_rows_0184.py"
    blob.write_bytes(source)
    spec = importlib.util.spec_from_file_location("model_stats_rows_0184", blob)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.FINDING_OUTCOME_LABELS = {
        "fixed": "a real error; the author changed the code",
        "wrong": "the code was already correct",
        "invented": "the finding cites a file, line, symbol or behaviour that does not exist",
        "out_of_scope": "outside the change's frozen obligation",
        "duplicate": "another finding of the same review names the same defect",
    }
    return module


def _prior_release_fixed(old, batch_state: Path, finding_id: str, *, head: str, started_at: str) -> None:
    """What release 0.1.84 does with a ``fixed`` label for one run."""
    old.record_finding_outcomes(batch_state, [old.finding_outcome(
        kind="label", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
        finding_id=finding_id, head=head, started_at=started_at, reviewer_model="claude-opus-5-5",
        label="fixed", recorded_by="author-task", recorded_at=_at(0.25))])


def test_rollback_and_roll_forward_keep_one_label_for_one_finding(tmp_path):
    """Defect class: rolling a finding-outcome label back to a writer whose vocabulary does not include it appends a
    second label for that finding, and rolling forward shows the other label.

    Release 0.1.84 admits only ``fixed``, ``wrong``, ``invented``, ``out_of_scope`` and ``duplicate``. A
    ``no_live_trigger`` line is invisible to that writer's conflict check. An occupying line that is not bound to
    the run shares one legacy key with every earlier line for that finding, so a later run of the same task and
    finding appends ``fixed`` and that reader attributes it.
    """
    prior = frozenset({"fixed", "wrong", "invented", "out_of_scope", "duplicate"})
    batch_state, _ = _state(tmp_path)
    assert _label(batch_state, "o1-review", 1, "F1", "no_live_trigger",
                  "only main() calls it, after the guard") == 1
    path = batch_state / rows.ROWS_DIR / rows.OUTCOMES_FILE
    # An earlier unbound compatibility row, and an earlier legacy label, both recorded before a later run of the
    # same task and finding. The later run has to keep its own label when rolled back.
    later = _at(0.5)
    earlier = {
        "schema": rows.OUTCOME_SCHEMA, "kind": "label", "task_id": "o1-review",
        "repository": "learn-ukrainian/machinery", "pr": 1, "recorded_by": "author-task", "recorded_at": _at(3),
    }
    planted = [
        {**earlier, "finding_id": "F2", "label": "duplicate",
         "evidence_sha256": hashlib.sha256(b"prior-release finding-outcome hold").hexdigest()},
        {**earlier, "finding_id": "F3", "label": "fixed", "evidence_sha256": None},
    ]
    with path.open("ab") as stream:
        stream.write(b"".join(json.dumps(line).encode() + b"\n" for line in planted))
    evidence = "only main() calls it, after the guard"
    assert _label(batch_state, "o1-review", 1, "F2", "no_live_trigger", evidence,
                  head=_HEAD_B, started_at=later, recorded_at=_at(0.25)) == 1
    assert _label(batch_state, "o1-review", 1, "F3", "no_live_trigger", evidence,
                  head=_HEAD_B, started_at=later, recorded_at=_at(0.25)) == 1
    old = _prior_release_rows(tmp_path)
    for finding_id in ("F1", "F2", "F3"):
        head, started = (_HEAD_A, _START_A) if finding_id == "F1" else (_HEAD_B, later)
        with pytest.raises(old.OutcomeConflict):
            _prior_release_fixed(old, batch_state, finding_id, head=head, started_at=started)
    assert [json.loads(line)["finding_id"] for line in path.read_bytes().splitlines()
            if json.loads(line).get("label") == "fixed" and json.loads(line).get("head") == _HEAD_B] == []
    finding = [json.loads(line) for line in path.read_bytes().splitlines()
               if json.loads(line).get("finding_id") == "F1"]
    visible = [line for line in finding if line.get("kind") == "label" and line.get("label") in prior
               and line.get("head") == _HEAD_A and line.get("started_at") == _START_A
               and "reviewer_model" not in line]
    assert len(visible) == 1
    assert rows.timestamp(visible[0]["recorded_at"]) >= rows.timestamp(_START_A)
    assert visible[0]["label"] != "fixed"
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F1", "fixed")
    finding = [json.loads(line) for line in path.read_bytes().splitlines()
               if json.loads(line).get("finding_id") == "F1"]
    assert [line["label"] for line in finding if line.get("label") == "fixed"] == []
    repo = rows.repository_identity("learn-ukrainian/machinery")
    key = ("o1-review", repo, 1, "F1", _HEAD_A, rows.timestamp(_START_A))
    labels, _, counts = rows.read_finding_outcomes(batch_state)
    assert labels[key][1] == "no_live_trigger"
    later_key = ("o1-review", repo, 1, "F2", _HEAD_B, rows.timestamp(later))
    assert labels[later_key][1] == "no_live_trigger"
    assert labels[("o1-review", repo, 1, "F3", _HEAD_B, rows.timestamp(later))][1] == "no_live_trigger"
    assert counts.get("conflicting_labels", 0) == 0
    # A second run-bound label, the line 0.1.84 appends when it cannot see the new one. Roll-forward keeps the
    # original label and counts the extra line instead of switching to it.
    original = next(line for line in finding if line.get("label") == "no_live_trigger")
    damaged = {**original, "label": "fixed", "evidence_sha256": None}
    with path.open("ab") as stream:
        stream.write(json.dumps(damaged).encode() + b"\n")
    labels, _, counts = rows.read_finding_outcomes(batch_state)
    assert labels[key][1] == "no_live_trigger"
    assert counts["conflicting_labels"] == 1


def test_finding_outcome_ledger_keeps_one_label_per_finding_and_skips_damaged_lines(tmp_path):
    """Defect class: a relabel, a same-label retry refused for its new evidence, a repeated contest, a label or
    contest without required run binding (head and started_at) or reviewer_model, a half-bound line accepted, a
    legacy line without run binding losing conflict protection or counted as malformed, a self-contest, or a damaged
    ledger line (including one whose member names repeat) silently changing what the statistics count."""
    batch_state, _ = _state(tmp_path)
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 1
    # The same label again adds nothing; another label for the finding is refused and writes nothing.
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 0
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F0", "wrong", "already correct")
    # A retry with the same label and other evidence is a no-op too: the first line and its evidence stay.
    assert _label(batch_state, "o1-review", 1, "F0", "fixed", "now with evidence") == 0
    # The repository's other slug names the same repository: still one label per finding. Another repository's
    # publication at the same PR number is another key.
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F0", "wrong", "already correct", repository="agentic-machinery/machinery")
    assert _label(batch_state, "o1-review", 1, "F0", "fixed", repository="agentic-machinery/machinery") == 0
    assert _label(batch_state, "o1-review", 1, "F0", "wrong", "already correct", repository="other/repo") == 1
    # Every label but fixed needs its evidence; every new line needs head + started_at; labels need reviewer_model.
    for label in ("wrong", "invented", "no_live_trigger", "out_of_scope", "duplicate"):
        with pytest.raises(ValueError, match="wrong shape"):
            _label(batch_state, "o1-review", 1, "F1", label)
    for bad_model in (None, "", "unknown", "has space", "../escape"):
        with pytest.raises(ValueError, match="wrong shape"):
            rows.finding_outcome(kind="label", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                                 finding_id="F1", head=_HEAD_A, started_at=_START_A, reviewer_model=bad_model,
                                 label="fixed", recorded_at=_at(1))
    for bad_head, bad_start in (
        (None, _START_A), ("not-a-sha", _START_A), ("a" * 64, _START_A), ("A" * 40, _START_A),
        (_HEAD_A, None), (_HEAD_A, "not-a-time"),
    ):
        with pytest.raises(ValueError, match="wrong shape"):
            rows.finding_outcome(kind="label", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                                 finding_id="F1", head=bad_head, started_at=bad_start,
                                 reviewer_model="claude-opus-5-5", label="fixed", recorded_at=_at(1))
        with pytest.raises(ValueError, match="wrong shape"):
            rows.finding_outcome(kind="contest", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                                 finding_id="F0", head=bad_head, started_at=bad_start, contested_by="o1b-review",
                                 recorded_at=_at(1))
    with pytest.raises(ValueError, match="wrong shape"):
        rows.finding_outcome(kind="contest", task_id="o1-review", repository="learn-ukrainian/machinery", pr=1,
                             finding_id="F0", head=_HEAD_A, started_at=_START_A, reviewer_model="claude-opus-5-5",
                             contested_by="o1b-review", recorded_at=_at(1))
    with pytest.raises(ValueError, match="wrong shape"):
        _label(batch_state, "o1-review", 1, "F1", "dismissed", "a word outside the vocabulary")
    with pytest.raises(ValueError, match="wrong shape"):
        _contest(batch_state, "o1-review", 1, "F0", "o1-review")
    assert _contest(batch_state, "o1-review", 1, "F0", "o1b-review") == 1
    assert _contest(batch_state, "o1-review", 1, "F0", "o1b-review") == 0
    path = batch_state / rows.ROWS_DIR / rows.OUTCOMES_FILE
    good = path.read_bytes()
    assert b"already correct" not in good and b"now with evidence" not in good
    machinery = rows.repository_identity("learn-ukrainian/machinery")
    # Writing a raw legacy label line without reviewer_model/head/started_at via record_finding_outcomes is refused.
    first = json.loads(good.splitlines()[0])
    legacy = {k: v for k, v in first.items() if k not in ("reviewer_model", "head", "started_at")}
    legacy["finding_id"] = "F9"
    with pytest.raises(ValueError, match="wrong shape"):
        rows.record_finding_outcomes(batch_state, [legacy])
    extra = {**first, "note": "free text"}
    half_bound = {k: v for k, v in first.items() if k != "started_at"}
    # A 64-hex head is refused on raw write as well as on read (only 40-hex Git commit SHAs are valid).
    sha256_head = {**first, "finding_id": "F8", "head": "a" * 64}
    with pytest.raises(ValueError, match="wrong shape"):
        rows.record_finding_outcomes(batch_state, [sha256_head])
    # Unhashable values, half-bound run keys, 64-hex heads, invalid reviewer_model, and wrong types are damaged lines.
    damaged = [extra, half_bound, sha256_head, {**first, "label": ["fixed"]}, {**first, "kind": {"label": 1}},
               {**first, "pr": "1"}, {**first, "reviewer_model": "unknown"},
               {**first, "reviewer_model": ["claude-opus-5-5"]}]
    # A repeated member name: the decoder would keep "wrong" and hide "fixed".
    repeated = json.dumps({**first, "finding_id": "F5"})[:-1].encode() + b', "label": "wrong"}'
    with path.open("ab") as stream:
        stream.write(json.dumps(legacy).encode() + b"\n" + b"{not json\n"
                     + b"".join(json.dumps(line).encode() + b"\n" for line in damaged) + repeated + b"\n")
    labels, contests, counts = rows.read_finding_outcomes(batch_state)
    t_label = rows.timestamp(_at(1))
    t_contest = rows.timestamp(_at(0.5))
    t_start = rows.timestamp(_START_A)
    assert labels == {
        ("o1-review", machinery, 1, "F0", _HEAD_A, t_start): ("claude-opus-5-5", "fixed", t_label),
        ("o1-review", "other/repo", 1, "F0", _HEAD_A, t_start): ("claude-opus-5-5", "wrong", t_label),
    }
    assert contests == {("o1-review", machinery, 1, "F0", _HEAD_A, t_start): t_contest}
    assert counts == {"lines": 4, "unattributed_labels": 1, "malformed_lines": 10}
    # A writer that finds damaged and legacy lines still keeps one label per finding (legacy F9 recorded at _at(1)
    # protects a run that started at _START_A = _at(2) <= _at(1)).
    assert _label(batch_state, "o1-review", 1, "F0", "fixed") == 0
    assert _label(batch_state, "o1-review", 1, "F9", "fixed") == 0
    with pytest.raises(rows.OutcomeConflict):
        _label(batch_state, "o1-review", 1, "F9", "wrong", "already correct")
    # PR1553-R2-F1: a newer run of the same task id that started AFTER legacy F9 was recorded (_at(0.5) > _at(1))
    # gets its own run-bound label without conflicting with or being suppressed by the earlier run's legacy line.
    assert _label(batch_state, "o1-review", 1, "F9", "wrong", "new run finding is wrong",
                  head=_HEAD_B, started_at=_at(0.5), recorded_at=_at(0.25)) == 1
    labels_after, _, _ = rows.read_finding_outcomes(batch_state)
    assert ("o1-review", machinery, 1, "F9", _HEAD_B, rows.timestamp(_at(0.5))) in labels_after


def test_author_results_split_by_risk_tier(tmp_path):
    """Defect class: author first-round approval and findings per PR that mix risk tiers, so an author given the
    critical work looks worse than its peers, that take a later round for the first when a window starts mid-PR, or
    that read a reply instead of the published verdict (a P3-only rejection published as an approval, an unpublished
    run taken for the first round, its window or its author, a published review whose reply is unparsed left out),
    or that miscount the PRs the tiers leave out of a window. Every expected figure below is counted by hand."""
    batch_state, tasks = _state(tmp_path)
    grok, sol = {"review_author_model": "grok-4.7"}, {"review_author_model": "gpt-6.1-sol"}
    critical = {"review_profile": "infra", "review_risk": "critical"}
    _review(tasks, "r10-review", "correct", [], pr=10, pinned_head="a" * 40, **grok, **critical)
    _review(tasks, "r11-review", "incorrect", ["P1", "P2"], pr=11, pinned_head="b" * 40, **grok, **critical)
    _review(tasks, "r12-review", "correct", ["P3"], pr=12, pinned_head="c" * 40, review_risk="low", **grok)
    _review(tasks, "r13-review", "incorrect", ["P0", "P1"], pr=13, pinned_head="d" * 40, **grok)
    _review(tasks, "r14-review", "correct", [], pr=14, pinned_head="f" * 40, review_risk="high", **sol)
    # PR 15 was first blocked at high risk before the 7-day window; its later medium approval is inside it.
    _review(tasks, "r15a-review", "incorrect", ["P1"], pr=15, pinned_head="1" * 40, review_risk="high",
            started_at=(NOW - timedelta(days=9)).isoformat(), finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    _review(tasks, "r15b-review", "correct", [], pr=15, pinned_head="2" * 40, **grok)
    # PR 16: a reply with only a P3 finding, published as an approval: a first-round approval.
    _review(tasks, "r16-review", "incorrect", ["P3"], pr=16, pinned_head="3" * 40, review_risk="low", **grok)
    # PR 17: an unpublished high-risk run by another author's PR came first; the first review of record is the medium
    # approval, and its author is the published review's alone.
    _review(tasks, "r17a-review", "incorrect", ["P1"], pr=17, pinned_head="4" * 40, review_risk="high", **sol)
    _review(tasks, "r17b-review", "correct", [], pr=17, pinned_head="5" * 40, **grok)
    # PR 18: no review of it was published: left out of the tiers and counted.
    _review(tasks, "r18-review", "correct", [], pr=18, pinned_head="6" * 40, **grok)
    # PR 19: an unpublished high-risk run 8 days ago, then a published medium approval inside the week: the PR is in
    # the week's tiers, from its first published review.
    _review(tasks, "r19a-review", "incorrect", ["P1"], pr=19, pinned_head="7" * 40, review_risk="high",
            started_at=(NOW - timedelta(days=8, hours=1)).isoformat(),
            finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    _review(tasks, "r19b-review", "correct", [], pr=19, pinned_head="8" * 40, **grok)
    # PR 20: its only review was published as an approval, but its reply is not parsed: counted, findings unknown.
    _review(tasks, "r20-review", "correct", [], pr=20, pinned_head="9" * 40, review_risk="low", **grok)
    (tasks / "r20-review.result").unlink()
    # PR 21: an approval dispatched by branch alone (its row names no PR), published on PR 21: it joins the published
    # cohort, and never the legacy author figures, which read only rows that name their PR.
    _review(tasks, "r21-review", "correct", [], pinned_head="e" * 40, review_risk="low", **grok)
    # PR 22: its first published review, a low-risk approval with an unparsed reply, finished 8 days ago; a parsed but
    # unpublished approval inside the week puts it in the week's author totals, and outside the week's tiers.
    _review(tasks, "r22a-review", "correct", [], pr=22, pinned_head="0" * 40, review_risk="low",
            started_at=(NOW - timedelta(days=8, hours=1)).isoformat(),
            finished_at=(NOW - timedelta(days=8)).isoformat(), **grok)
    (tasks / "r22a-review.result").unlink()
    _review(tasks, "r22b-review", "correct", [], pr=22, pinned_head="0" * 40, **grok)
    rows.backfill(tasks, batch_state)
    approved, blocked = "APPROVED", "CHANGES_REQUESTED"
    assert _publish(batch_state, ("r10-review", 10, "a" * 40, approved), ("r11-review", 11, "b" * 40, blocked),
                    ("r12-review", 12, "c" * 40, approved), ("r13-review", 13, "d" * 40, blocked),
                    ("r14-review", 14, "f" * 40, approved), ("r15a-review", 15, "1" * 40, blocked),
                    ("r15b-review", 15, "2" * 40, approved), ("r16-review", 16, "3" * 40, approved),
                    ("r17b-review", 17, "5" * 40, approved), ("r19b-review", 19, "8" * 40, approved),
                    ("r20-review", 20, "9" * 40, approved), ("r21-review", 21, "e" * 40, approved),
                    ("r22a-review", 22, "0" * 40, approved),
                    repository="learn-ukrainian/learn-ukrainian.github.io") == 13

    snapshot = stats.collect(batch_state, batch_state.parent, NOW, landing=stats.Landing())

    def tiers(window: str, author: str, view: str = "windows") -> list[tuple]:
        (found,) = [a for a in snapshot[view][window]["author_risks"] if a["model"] == author]
        return [(r["risk"], r["prs"], r["first_round_approved"]["value"], r["blocking_per_pr"]["value"],
                 r["p0_p2_per_pr"]["value"], r["low_sample"]) for r in found["by_risk"]]

    # Medium: r13 blocked (P0, P1), r17b and r19b approved without findings. Low: r12, r16, r20 and r21 approved;
    # r20's findings are unknown, so the per-PR findings rates count r12, r16 and r21 only.
    week = [("critical", 2, 0.5, 0.5, 1.0, True),
            ("medium", 3, stats._rate(2, 3)["value"], stats._rate(2, 3, 2)["value"],
             stats._rate(2, 3, 2)["value"], True),
            ("low", 4, 1.0, 0.0, 0.0, True)]
    assert tiers("7d", "grok-4.7") == week == tiers("7d", "grok-4.7", "page_windows")
    # Over 30 days PR 15 adds the high tier and PR 22 a fifth low-tier approval with unknown findings.
    assert tiers("30d", "grok-4.7") == [week[0], ("high", 1, 0.0, 1.0, 1.0, True), week[1],
                                        ("low", 5, 1.0, 0.0, 0.0, True)]
    # Sol authored only the unpublished r17a and the published r14.
    assert [r[:3] for r in tiers("7d", "gpt-6.1-sol")] == [("high", 1, 1.0)]
    # Left out of the week's tiers though in the week's author totals: PR 18 (never published) and PR 22 (first
    # published before the week).
    for view in ("windows", "page_windows"):
        without = {a["model"]: a["prs_without_review_of_record"] for a in snapshot[view]["7d"]["author_risks"]}
        assert without == {"grok-4.7": 2, "gpt-6.1-sol": 0}
    # The legacy cohort is the parsed replies of rows that name their PR: grok's PRs 10-13, 16, 18 and 22 this week
    # (PR 17 is a joint author's, PR 19 started before the week, PR 20 is unparsed, PR 21's row names no PR).
    (legacy,) = [a for a in snapshot["windows"]["7d"]["authors"] if a["model"] == "grok-4.7"]
    assert legacy["prs"] == 7
    # PR 17's joint author stays joint on the page too, so either member's picker selection finds it.
    for view in ("windows", "page_windows"):
        assert ("gpt-6.1-sol+grok-4.7", 1) in [(a["model"], a["prs"]) for a in snapshot[view]["7d"]["authors"]]


def _merged_history(tmp_path, monkeypatch):
    repo = _git_fixture(tmp_path, monkeypatch)
    fleet_repo = FleetRepo("machinery", "learn-ukrainian/machinery", "repo", "test",
                           alternate_github="agentic-machinery/machinery")
    monkeypatch.setattr(stats, "load_fleet_repos", lambda: {"machinery": fleet_repo})
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda key, **_: (fleet_repo, repo))

    def merge(title: str, day: float, *, lines: int = 0, body: str = "") -> str:
        if lines:
            path = f"change-{title.rpartition('#')[2].rstrip(')')}.txt"
            (repo / path).write_text("new line\n" * lines)
            _git(repo, "add", "--", path)
        at = (NOW + timedelta(days=day)).isoformat()
        monkeypatch.setenv("GIT_COMMITTER_DATE", at)
        _git(repo, "commit", "-q", "--allow-empty", "--date", at, "-m", title, "-m", body)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "update-ref", "refs/remotes/origin/main", sha)
        return sha

    merge("Initial commit", -31)
    return merge


def test_author_quality_stratifies_published_rounds_by_risk_and_landed_diff_size(tmp_path, monkeypatch):
    """Defect class: combining PRs on opposite size boundaries or dropping rejected PRs without a landed diff."""
    merge = _merged_history(tmp_path, monkeypatch)
    batch_state, tasks = _state(tmp_path)
    for number, size in ((31, 100), (32, 101), (33, 1000), (34, 1001), (36, 10)):
        merge(f"Feature (#{number})", -2, lines=size)
    grok = {"repo": "machinery", "review_author_model": "grok-4.7"}
    _review(tasks, "size31a", "incorrect", ["P1", "P2"], pr=31, pinned_head="a" * 40, **grok,
            started_at=(NOW - timedelta(days=4)).isoformat(), finished_at=(NOW - timedelta(days=3)).isoformat())
    _review(tasks, "size31b", "correct", [], pr=31, pinned_head="b" * 40, **grok, review_risk="high")
    for number, sha in ((32, "c"), (33, "d"), (34, "e")):
        _review(tasks, f"size{number}", "correct", [], pr=number, pinned_head=sha * 40, **grok)
    _review(tasks, "size35", "incorrect", ["P2"], pr=35, pinned_head="f" * 40, **grok)
    _review(tasks, "size36", "correct", [], pr=36, pinned_head="1" * 40, **grok, review_risk="critical")
    rows.backfill(tasks, batch_state)
    approve, changes = "APPROVED", "CHANGES_REQUESTED"
    assert _publish(batch_state, ("size31a", 31, "a" * 40, changes), ("size31b", 31, "b" * 40, approve),
                    ("size32", 32, "c" * 40, approve), ("size33", 33, "d" * 40, approve),
                    ("size34", 34, "e" * 40, approve), ("size35", 35, "f" * 40, changes),
                    ("size36", 36, "1" * 40, approve)) == 7

    snapshot = stats.collect(batch_state, tmp_path, NOW)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        response = client.get("/api/fleet/model-stats?window=7d&view=page")
    assert response.status_code == 200
    (author,) = response.json()["author_risks"]
    critical, medium = author["by_risk"]
    assert (critical["risk"], critical["prs"], critical["by_size"][0]["size"]) == ("critical", 1, "small")
    assert (medium["risk"], medium["prs"]) == ("medium", 5)
    assert medium["rounds_per_pr"] == {"value": 1.2, "n": 5, "low_sample": True}
    assert [(band["size"], band["prs"], band["rounds_per_pr"]["value"], band["first_round_approved"]["value"],
             band["approved_eventually"]["value"], band["p0_p2_per_pr"]["value"], band["low_sample"])
            for band in medium["by_size"]] == [
        ("small", 1, 2.0, 0.0, 1.0, 2.0, True), ("medium", 2, 1.0, 1.0, 1.0, 0.0, True),
        ("large", 1, 1.0, 1.0, 1.0, 0.0, True), ("unknown", 1, 1.0, 0.0, 0.0, 1.0, True),
    ]
    # The shallow boundary contains only PR 36. Its tree is large, but its parent and exact diff are missing.
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", (tmp_path / "repo").as_uri(), str(shallow))
    monkeypatch.setattr(stats, "resolve_fleet_repo", lambda *args, **kwargs: (None, shallow))
    (author,) = stats.collect(batch_state, tmp_path, NOW)["page_windows"]["7d"]["author_risks"]
    assert author["by_risk"][0]["by_size"][0]["size"] == "unknown"


def test_post_merge_rework_deduplicates_same_repo_fixes_in_a_complete_week(tmp_path, monkeypatch):
    """Defect class: counting references from another repo, unrelated PRs, duplicate fixes or incomplete weeks."""
    merge = _merged_history(tmp_path, monkeypatch)
    batch_state, tasks = _state(tmp_path)
    terra, sol = "gpt-6.1-terra", "gpt-6.2-sol"
    for number, day, model in ((11, -13, terra), (12, -12, sol), (13, -11, terra), (14, -10, sol), (15, -1, terra)):
        _record(tasks, f"written-{number}", repo="machinery", model=model,
                started_at=(NOW - timedelta(days=20)).isoformat())
        if number == 15:
            continue
        sha = merge(f"Feature (#{number})", day, body=f"X-Agent: codex/written-{number}")
        if number == 14:
            reverted_sha = sha
    _record(tasks, "written-16", repo="machinery", model=terra, started_at=(NOW - timedelta(days=20)).isoformat())
    merge("Feature (#16)", -10, body="X-Agent: codex/written-16")
    merge("fix: repair agentic-machinery/machinery#16 (#20)", -10)
    merge("fix: repair a prior change (#21)", -9,
          body="Fixes https://github.com/agentic-machinery/machinery/pull/11\nRefs other/repo#12\n\n```\nExample #13\n```")
    merge("docs: cite #12 (#22)", -8, body="https://example.invalid/leaky-commit-body")
    merge("fix: repair #11 again (#23)", -6)
    merge("fix: late repair for #12 (#24)", -4)
    merge("Revert feature (#25)", -3, body=f"This reverts commit {reverted_sha}.")
    merge("Feature (#15)", -1, body="X-Agent: codex/written-15")
    merge("fix: repair recent #15 (#26)", -0.5)
    rows.backfill(tasks, batch_state)

    snapshot = stats.collect(batch_state, tmp_path, NOW)
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        response = client.get("/api/fleet/model-stats?window=7d&view=page")
        day = client.get("/api/fleet/model-stats?window=24h&view=page").json()
    assert response.status_code == 200 and "leaky-commit-body" not in response.text
    assert day["post_merge_rework_proxy"] == []
    assert [(r["model"], r["reworked"], r["rate"]) for r in response.json()["post_merge_rework_proxy"]] == [
        (terra, 2, {"value": 0.667, "n": 3, "low_sample": True}), (sol, 1, {"value": 0.5, "n": 2, "low_sample": True}),
    ]


def test_no_deliverable_counts_closed_cause_labels_from_settled_records(tmp_path, monkeypatch):
    """Defect class: collapsing stated causes, leaking their details, or making old rows unreadable."""
    batch_state, tasks = _state(tmp_path)
    for task, reason, extra in (
        ("outside", "outside_owned_paths: /private/escaped-path", {}), ("scope", "scope-breaker stop: note", {}),
        ("empty", "no_commits_or_unknown", {}), ("unknown", None, {}),
        ("deadline", None, {"error": "process deadline exceeded"}),
        ("other", "worktree_dirty_or_unknown", {}),
    ):
        _record(tasks, task, status="no_deliverable", no_deliverable_reason=reason, **extra)
    _record(tasks, "landed-with-old-error", no_deliverable_reason="outside_owned_paths")
    path = tasks / "outside.json"
    old = rows.summary_row(path, json.loads(path.read_text()), path.stat().st_mtime_ns)
    assert old is not None
    old.pop("no_deliverable_cause")
    rows.append_row(batch_state, old)
    assert _row(_window(batch_state, stats.Landing()), "gpt-6.1-sol", "write")["no_deliverable_causes"][
        "no_commits_or_unknown"] == 1
    assert rows.backfill(tasks, batch_state)["refreshed"] == 1
    snapshot = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())
    with _serve(snapshot, tmp_path, monkeypatch) as client:
        response = client.get("/api/fleet/model-stats?window=7d&view=page")
    assert response.status_code == 200 and "escaped-path" not in response.text
    model = _row(response.json(), "gpt-6.1-sol", "write")
    assert model["status"]["no_deliverable"] == 6
    assert model["no_deliverable_causes"] == model["by_effort"][0]["no_deliverable_causes"] == {
        "outside_owned_paths": 1, "scope_breaker_stop": 1, "no_commits_or_unknown": 2, "deadline": 1, "other": 1,
    }


def test_publication_ledger_writes_only_well_formed_lines_and_trusts_no_damaged_one(tmp_path, monkeypatch):
    """Defect class: a malformed publication is written, or a damaged ledger line is trusted, so a review counts as
    published without proof; a missing ledger reads as anything but "no publication known"; or an older publication
    appended later (a history import) supersedes a newer one; or a line for another repository, PR or head blocks or
    hides the real publication; or a collection reuses a fleet or model catalog reading of an earlier one (an
    unreadable catalog then hides publications and reply samples after its repair, or a valid one outlives the
    file)."""
    from collections import Counter

    import pytest

    batch_state, tasks = _state(tmp_path)
    assert rows.read_publications(batch_state) == ({}, Counter({"missing": 1, "tasks": 0}))
    fields = {"task_id": "t-review", "repository": "learn-ukrainian/machinery", "pr": 3, "head": "a" * 40,
              "verdict": "APPROVED", "published_at": NOW.isoformat()}
    good = rows.publication(**fields)
    for bad in ({"verdict": "APPROVE"}, {"pr": 0}, {"head": "a" * 39}, {"published_at": None},
                {"repository": "../x"}, {"task_id": "../x"}):
        with pytest.raises(ValueError):
            rows.publication(**{**fields, **bad})
    # One malformed entry in a batch writes nothing at all.
    with pytest.raises(ValueError):
        rows.record_publications(batch_state, [good, {**good, "pr": True}])
    ledger = batch_state / rows.ROWS_DIR / rows.PUBLICATIONS_FILE
    assert not ledger.exists()
    assert rows.record_publications(batch_state, [good]) == 1
    current = {**good, "repository": "agentic-machinery/machinery"}
    assert rows.publication_key(good) == rows.publication_key(current) == (
        "t-review", 1445647449, 3, "a" * 40, "APPROVED",
    )
    assert rows.record_publications(batch_state, [current]) == 0
    with ledger.open("ab") as stream:
        stream.write(b'{"schema": "review-publication.v1", "task_id": "t-review"}\nnot json\n')
    later = {**good, "verdict": "CHANGES_REQUESTED"}
    assert rows.record_publications(batch_state, [later, good]) == 1
    # Newest first; of two at the same comment time, the later line first.
    assert rows.read_publications(batch_state) == (
        {"t-review": [later, good]}, Counter({"lines": 2, "malformed_lines": 2, "tasks": 1}))
    # An import appends an older publication after the newer one: the newer one still stands.
    older = {**good, "verdict": "BLOCKED", "published_at": (NOW - timedelta(days=1)).isoformat()}
    assert rows.record_publications(batch_state, [older]) == 1
    assert rows.read_publications(batch_state)[0] == {"t-review": [later, good, older]}
    # Newer lines naming the task on another PR, repository or head are other publications: each is stored, and the
    # row's join still takes the newest entry that matches its own repository, PR and head.
    newer = (NOW + timedelta(hours=1)).isoformat()
    elsewhere = [{**later, "pr": 4, "published_at": newer}, {**later, "head": "b" * 40, "published_at": newer},
                 {**later, "repository": "learn-ukrainian/learn-ukrainian.github.io", "published_at": newer}]
    assert rows.record_publications(batch_state, elsewhere) == 3
    row = {"task_id": "t-review", "role": "review", "repo": "machinery", "pr": 3, "pinned_head": "a" * 40}
    keys = rows.repository_keys(rows.fleet_catalog())
    assert stats._publication(row, rows.read_publications(batch_state)[0], keys) == later

    # Each collection reads the catalogs afresh through every cache, the real loader included: one that cannot be
    # read matches no publication and admits no reply sample in that collection only, and a valid reading is never
    # kept once the file breaks.
    from lu_machinery.orchestration import fleet_repos

    _review(tasks, "c-review", "incorrect", ["P1"], pr=3, pinned_head="c" * 40, review_author_model="grok-4.7",
            repo="machinery", usage={"input_tokens": 5, "cache_read_input_tokens": 8,
                                     "cache_creation_input_tokens": 2, "output_tokens": 3})
    _review(tasks, "supp-review", "correct", [], pinned_head="c" * 40, repo="machinery", agent="agy",
            model="gemini-4-neon", usage={"input_tokens": 7, "cache_read_tokens": 9, "output_tokens": 4,
                                         "thinking_tokens": 6})
    _review(tasks, "same-head-other-repo", "correct", [], pinned_head="c" * 40, repo="public", agent="cursor",
            model="grok-4.7", usage={"inputTokens": 10, "cacheReadTokens": 12, "cacheWriteTokens": 2,
                                     "outputTokens": 2, "reasoningTokens": 1})
    _record(tasks, "missing-output", usage={"input_tokens": 20, "cached_input_tokens": 10})
    _record(tasks, "zero-usage", usage={"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
    _record(tasks, "u-review", mode="read-only", agent="claude", model="claude-opus-5-5", data_class="public-code",
            review_profile="code")
    (tasks / "u-review.result").write_text("prose reply", encoding="utf-8")
    rows.backfill(tasks, batch_state)
    stored = {row["task_id"]: row for row in rows.read_rows(batch_state)[0]}
    assert stored["u-review"]["review"]["unparsed_sample"] == "prose reply"
    assert stored["missing-output"]["usage_known"] is False and stored["zero-usage"]["usage_known"] is True
    assert _publish(batch_state, ("c-review", 3, "c" * 40, "CHANGES_REQUESTED")) == 1
    token_use = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())["page_windows"]["7d"]["token_use"]
    assert token_use["totals"] == {"fresh_input": 26, "cached_input": 29, "output": 16}
    assert token_use["reported_tasks"] == 4 and token_use["merged_prs"] is None
    assert token_use["by_work"]["review_of_record"]["tokens"] == 18
    assert token_use["waste"]["supplemental_review"]["share"] == 26 / 71
    assert token_use["by_work"]["unclassified_review"]["tokens"] == 27
    # A label line is read through the registry too: unreadable, the collection counts no label and still answers.
    assert _label(batch_state, "c-review", 3, "F0", "fixed") == 1
    registry = fleet_repos._CONFIG_PATH.read_text(encoding="utf-8")
    assert "\n    github: agentic-machinery/machinery\n" in registry
    catalog = tmp_path / "fleet_repos.yaml"
    monkeypatch.setattr(fleet_repos, "_CONFIG_PATH", catalog)

    def collected() -> tuple[int, int, int, bool]:
        snapshot = stats.collect(batch_state, batch_state.parent, NOW, landing=stats.Landing())
        (found,) = snapshot["windows"]["7d"]["reviewer_accuracy"]["findings"]
        return (found["blocking_reviews"], found["reviews_unknown_publication"],
                snapshot["coverage"]["rows"].get("malformed_lines", 0),
                snapshot["windows"]["7d"]["idle_catalog_models"] is not None)

    unreadable, valid = (0, 1, 1, True), (1, 0, 0, True)
    for text, expected in (
            (registry.replace("github: agentic-machinery/machinery\n", "github: agentic-machinery-machinery\n"), unreadable),
            (registry, valid), ("repos: [", unreadable), (registry, valid)):
        catalog.write_text(text, encoding="utf-8")
        assert collected() == expected, text[:40]
    # The model catalog through its own loader's cache: unreadable, it admits no public data class (the sample row
    # is skipped) and leaves the idle models unknown (null, never an empty list), in that collection only.
    from lu_machinery.review import model_catalog

    models_file = tmp_path / "model_catalog.yaml"
    models_text = model_catalog.CATALOG_PATH.read_text(encoding="utf-8")
    monkeypatch.setattr(model_catalog.load_model_catalog.__wrapped__, "__defaults__", (models_file,))
    for text, expected in ((models_text, valid), ("models: [", (1, 0, 1, False)), (models_text, valid)):
        models_file.write_text(text, encoding="utf-8")
        assert collected() == expected, text[:40]
    # Leave no reading of the scratch catalog to later tests.
    monkeypatch.undo()
    rows.refresh_readings()


# Report-only Ruff check-attempt statistics (#1615): classification, the attempt ledger, settlement attribution and
# the forward-only collector against a fake GitHub. Approved by claude-cto for issue #1615 (AC-01 to AC-14).
RUFF_REPO = "org/repo"
RUFF_REPO_ID = 77
PILOT = NOW - timedelta(days=2)
_SETUP_STEPS = ("Set up job", "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "Run ./.github/actions/uv-python", "Install Ruff (uv.lock lint group)")


def _ruff_steps(*, setup: str = "success", lint: str = "success", guards: tuple[str, ...] = ("success",) * 3,
                truncated: bool = False) -> list[dict]:
    """The CI Gate ``Ruff`` job's steps (.github/workflows/ci-gate.yml); a failed setup step skips every later one."""
    names = [*_SETUP_STEPS, rows.RUFF_STEP, *rows.GUARD_STEPS]
    conclusions = (["success", setup] + ["skipped"] * 6 if setup != "success"
                   else ["success"] * 4 + [lint, *guards])
    steps = [{"number": index, "name": name, "status": "completed", "conclusion": conclusion}
             for index, (name, conclusion) in enumerate(zip(names, conclusions, strict=True), start=1)]
    if truncated:
        steps[-1] = {**steps[-1], "status": "in_progress", "conclusion": None}
    return steps


def _ruff_receipt(**fields) -> dict:
    receipt = {
        "schema": rows.RUFF_SCHEMA, "repository": RUFF_REPO, "repository_id": RUFF_REPO_ID, "pr": 7,
        "measured_head_sha": "a" * 40, "check_name": "Ruff", "check_run_id": 9001, "run_id": 501, "run_attempt": 1,
        "started_at": (NOW - timedelta(hours=3)).isoformat(), "completed_at": (NOW - timedelta(hours=3)).isoformat(),
        "status": "completed", "conclusion": "success", "steps": _ruff_steps(), "findings": None,
        "base_sha": "0" * 40, "tested_sha": None, "head_commits": ["1" * 40], "diff_size": 12,
        "check_version": "ruff-attempt.v1:ci-gate.yml", "observed_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    receipt.update(fields)
    return receipt


def _failed_lint(*origins: str, **fields) -> dict:
    return _ruff_receipt(conclusion="failure", steps=_ruff_steps(lint="failure", guards=("skipped",) * 3),
                         findings=[{"code": "F401", "origin": origin} for origin in origins], **fields)


@pytest.mark.parametrize(("receipt", "bucket"), [
    # AC-03: a proven pre-Ruff setup failure is known not-executed, even with no findings at all.
    (_ruff_receipt(conclusion="failure", steps=_ruff_steps(setup="failure")), "infrastructure_failure"),
    (_ruff_receipt(steps=None), "missing_or_reused_evidence"),
    (_ruff_receipt(steps=_ruff_steps(truncated=True)), "missing_or_reused_evidence"),
    (_ruff_receipt(conclusion="skipped", steps=[]), "missing_or_reused_evidence"),
    # AC-04: the lint succeeded and a guard failed: a guard failure only.
    *((_ruff_receipt(conclusion="failure", steps=_ruff_steps(guards=tuple(
        "failure" if index == failed else "success" if index < failed else "skipped" for index in range(3)))),
       "guard_failure") for failed in range(3)),
    # AC-06: a pass needs positive evidence for the lint and every guard.
    (_ruff_receipt(), "ruff_pass"),
    (_ruff_receipt(steps=_ruff_steps(guards=("success", "success", "skipped"))), "missing_or_reused_evidence"),
    (_failed_lint("pr", "unresolved"), "ruff_code_failure"),
    (_failed_lint("base", "base"), "base_only_ruff_failure"),
    # AC-05 / A1: complete findings with unresolved attribution are ambiguous, never missing evidence.
    (_failed_lint("base", "unresolved"), "ambiguous_base_or_merge_context"),
    (_failed_lint(), "missing_or_reused_evidence"),
    (_ruff_receipt(conclusion="failure", steps=_ruff_steps(lint="failure", guards=("skipped",) * 3)),
     "missing_or_reused_evidence"),
    (_ruff_receipt(conclusion="cancelled", steps=None), "cancelled_or_superseded"),
], ids=["setup-failure", "no-steps", "truncated-steps", "main-proof-reuse", "guard-json", "guard-paths",
        "guard-changelog", "pass", "guard-skipped", "pr-finding", "base-only", "ambiguous", "no-findings",
        "findings-unread", "cancelled"])
def test_ruff_attempts_take_the_first_bucket_their_own_evidence_proves(receipt, bucket):
    """Defect class: an attempt counted in a Ruff outcome its evidence does not prove (a pass without step proof, a
    guard failure as a pass, a setup failure or unresolved base attribution masked as missing evidence)."""
    assert rows._ruff_ok(receipt)
    assert rows.classify_ruff_attempt(receipt) == bucket
    assert rows.ruff_complete(receipt) is (bucket != "missing_or_reused_evidence")


@pytest.mark.parametrize("complete", [
    _ruff_receipt(), _ruff_receipt(conclusion="failure", steps=_ruff_steps(setup="failure")),
    _failed_lint("base", "unresolved"),
], ids=["pass", "checkout-failure", "ambiguous"])
def test_ruff_ledger_keeps_one_complete_observation_through_incomplete_complete_and_replay(tmp_path, complete):
    """Defect class: a late base or tested SHA or a replayed incomplete receipt duplicating or downgrading a
    complete attempt (AC-07, A1 replay fixture)."""
    batch_state, _ = _state(tmp_path)
    incomplete = {**complete, "base_sha": None, "steps": _ruff_steps(truncated=True), "findings": None,
                  "observed_at": (NOW - timedelta(hours=3)).isoformat()}
    late = {**complete, "base_sha": "9" * 40, "tested_sha": "8" * 40}
    assert not rows.ruff_complete(incomplete) and rows.ruff_complete(late)
    for batch, winner in (([incomplete], incomplete), ([late], late), ([incomplete, late], late),
                          ([late, incomplete], late)):
        rows.record_ruff_attempts(batch_state, batch)
        attempts, counts = rows.read_ruff_attempts(batch_state)
        assert (attempts, counts["attempts"]) == ([winner], 1)
    assert len(rows.ruff_path(batch_state).read_bytes().splitlines()) == 2
    assert rows.record_ruff_attempts(batch_state, [incomplete, late]) == 0
    # A revisit that observed the same attempt again is no new observation either.
    assert rows.record_ruff_attempts(batch_state, [{**late, "observed_at": NOW.isoformat()}]) == 0


def _credited_row(tasks: Path, task_id: str, model: str, commits: list[str], **fields) -> None:
    _record(tasks, task_id, **{
        "resolved_model": model, "resolved_model_known": True, "repository": RUFF_REPO,
        "run_nonce": hashlib.sha256(task_id.encode()).hexdigest()[:32], "credited_commits": commits,
        "research_task_family": "bounded_implementation", **fields})


def test_ruff_head_credit_needs_every_commit_in_one_models_settled_credited_sets(tmp_path):
    """Defect class: a head credited to a model on anything but dispatcher-computed settlement sets bound to the
    repository, writer task, run nonce and a runtime-observed model (AC-08 to AC-12): main imports, another
    branch's commits, hook-only or trailer-only commits, takeover predecessors and copied trailers."""
    batch_state, tasks = _state(tmp_path)
    own, pred, other, imported, hook_only = "1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40
    _credited_row(tasks, "writer-sol", "gpt-6.2-sol", [own])
    _credited_row(tasks, "pred-sol", "gpt-6.2-sol", [pred])
    _credited_row(tasks, "pred-opus", "claude-opus-5-5", [other])
    # Settlement records missing one binding each never credit their commits.
    _credited_row(tasks, "no-nonce", "gpt-6.2-sol", [imported], run_nonce=None)
    _credited_row(tasks, "requested-only", "gpt-6.2-sol", [imported], resolved_model_known=False)
    _credited_row(tasks, "no-repository", "gpt-6.2-sol", [imported], repository=None)
    _record(tasks, "no-credited-set", resolved_model="gpt-6.2-sol", resolved_model_known=True, repository=RUFF_REPO,
            run_nonce="f" * 32)
    rows.backfill(tasks, batch_state)
    stored, _ = rows.read_rows(batch_state)
    assert {row["task_id"]: row["credited_commits"] for row in stored}["writer-sol"] == [own]
    heads = {
        9001: ([own], "model"), 9002: ([own, pred], "model"), 9003: ([own, other], "joint"),
        9004: ([own, imported], "unknown"), 9005: ([own, hook_only], "unknown"), 9006: (None, "unknown"),
    }
    rows.record_ruff_attempts(batch_state, [
        _failed_lint("pr", check_run_id=run, head_commits=commits) if run == 9003 else
        _ruff_receipt(check_run_id=run, head_commits=commits) for run, (commits, _) in heads.items()])
    (batch_state / "model-stats" / rows.RUFF_STATE_FILE).write_text(json.dumps({
        "schema": rows.RUFF_STATE_SCHEMA, "repository": RUFF_REPO, "pilot_window_start": PILOT.isoformat(),
        "watermark": None, "incomplete_intervals": [], "known_runs": {}}))
    quality = stats.ruff_quality(batch_state, stored)
    assert quality["attribution"] == {"model": 2, "joint": 1, "unknown": 3}
    assert [(model["model"], model["attempts"], model["ruff_code_failure_rate"]) for model in quality["models"]] == [
        ("gpt-6.2-sol", 2, {"numerator": 0, "denominator": 2})]
    assert quality["joint"] == {"attempts": 1, "buckets": {**dict.fromkeys(rows.RUFF_BUCKETS, 0),
                                                           "ruff_code_failure": 1}}
    assert quality["unknown"]["attempts"] == 3 and quality["unknown"]["buckets"]["ruff_pass"] == 3
    assert quality["denominator"] == 6 and quality["ruff_code_failure_rate"] == {"numerator": 0, "denominator": 2}


def test_red_first_head_is_kept_beside_the_green_head_that_followed_it(tmp_path):
    """Defect class: a PR's red first head overwritten as a pass by its later green head (AC-01)."""
    batch_state, _ = _state(tmp_path)
    rows.record_ruff_attempts(batch_state, [
        _failed_lint("pr", measured_head_sha="a" * 40, check_run_id=9001),
        _ruff_receipt(measured_head_sha="b" * 40, check_run_id=9101)])
    (batch_state / "model-stats" / rows.RUFF_STATE_FILE).write_text(json.dumps({
        "schema": rows.RUFF_STATE_SCHEMA, "repository": RUFF_REPO, "pilot_window_start": PILOT.isoformat(),
        "watermark": None, "incomplete_intervals": [], "known_runs": {}}))
    buckets = stats.ruff_quality(batch_state, [])["buckets"]
    assert (buckets["ruff_code_failure"], buckets["ruff_pass"]) == (1, 1)


_PATCH = "@@ -10,2 +10,3 @@\n context\n+import os\n context"


@pytest.mark.parametrize(("annotation", "files", "base_files", "origin"), [
    ({"path": "lu_machinery/x.py", "start_line": 11, "title": "ruff (F401)"}, [("lu_machinery/x.py", _PATCH)], [],
     "pr"),
    ({"path": "lu_machinery/y.py", "start_line": 3, "title": "Ruff (E501)"}, [("lu_machinery/x.py", _PATCH)], [],
     "base"),
    ({"path": "lu_machinery/y.py", "start_line": 3, "title": "ruff (E501)"}, [("lu_machinery/x.py", _PATCH)],
     ["lu_machinery/y.py"], "base"),
    ({"path": "lu_machinery/x.py", "start_line": 10, "title": "ruff (F401)"}, [("lu_machinery/x.py", _PATCH)], [],
     "unresolved"),
    # The base changed the file too: the merge tree's line numbers are not the pull request's.
    ({"path": "lu_machinery/x.py", "start_line": 11, "title": "ruff (F401)"}, [("lu_machinery/x.py", _PATCH)],
     ["lu_machinery/x.py"], "unresolved"),
    ({"path": "lu_machinery/x.py", "start_line": 11, "title": "ruff (F401)"}, [("lu_machinery/x.py", _PATCH)],
     None, "unresolved"),
    ({"path": "lu_machinery/x.py", "start_line": 11, "title": "ruff (F401)"}, [("lu_machinery/x.py", _PATCH)],
     [f"lu_machinery/f{index}.py" for index in range(300)], "unresolved"),
    ({"path": "lu_machinery/y.py", "start_line": 3, "title": "ruff (E501)"},
     [("lu_machinery/x.py", _PATCH), ("pyproject.toml", None)], [], "unresolved"),
    ({"path": "lu_machinery/y.py", "start_line": 3, "title": "ruff (E501)"},
     [(f"lu_machinery/f{index}.py", None) for index in range(300)], [], "unresolved"),
    ({"path": "/home/runner/work/lu_machinery/y.py", "start_line": 3, "title": "ruff (E501)"},
     [("lu_machinery/x.py", _PATCH)], [], "unresolved"),
    ({"path": "lu_machinery/x.py", "start_line": 11, "title": "ruff (F401)"}, [("lu_machinery/x.py", None)], [],
     "unresolved"),
], ids=["added-line", "untouched-file", "untouched-file-base-changed", "unchanged-line", "both-changed",
        "base-side-unread", "base-side-capped", "config-changed", "file-list-capped", "outside-tree", "no-patch"])
def test_ruff_findings_are_attributed_to_the_pr_only_on_lines_it_added(annotation, files, base_files, origin):
    """Defect class: a Ruff finding blamed on the pull request, or cleared as a base finding, without whole
    comparisons that prove where it came from in the merge tree the job linted (AC-05, AC-06)."""
    comparison = {"total_commits": 1, "commits": [{"sha": "1" * 40}],
                  "files": [{"filename": name, "additions": 1, "deletions": 0, "patch": patch}
                            for name, patch in files]}
    base_side = None if base_files is None else {
        "total_commits": 1, "commits": [{"sha": "2" * 40}],
        "files": [{"filename": name, "additions": 1, "deletions": 0, "patch": None} for name in base_files]}
    noise = {"path": ".github", "start_line": 66, "title": ""}  # the step's exit-code annotation, not a finding
    assert rows.finding_origins([noise, annotation], comparison, base_side) == [
        {"code": annotation["title"].split("(")[1].rstrip(")"), "origin": origin}]
    assert rows.finding_origins([annotation], None, base_side)[0]["origin"] == "unresolved"


def test_a_later_receipt_with_less_evidence_never_replaces_a_proved_outcome(tmp_path):
    """Defect class: a later read of one attempt that lost its comparison (no head commits, unresolved findings)
    outranking the receipt that proved a PR-introduced failure and its attribution (round 1 F1, AC-01, AC-07)."""
    batch_state, _ = _state(tmp_path)
    proved = _failed_lint("pr", observed_at=(NOW - timedelta(hours=3)).isoformat())
    weaker = _failed_lint("unresolved", head_commits=None, observed_at=(NOW - timedelta(hours=1)).isoformat())
    assert rows.classify_ruff_attempt(weaker) == "ambiguous_base_or_merge_context" and rows.ruff_complete(weaker)
    for batch in ([proved, weaker], [weaker, proved]):
        rows.record_ruff_attempts(batch_state, batch)
        assert rows.read_ruff_attempts(batch_state)[0] == [proved]


class _RuffGitHub:
    """GitHub's REST answers for one repository's CI ``Ruff`` attempts; records every endpoint it served."""

    def __init__(self, documents: dict[str, object]):
        self.documents = documents
        self.sent: list[str] = []

    def __call__(self, method, endpoint, headers, body, timeout):
        assert method == "GET" and body is None, endpoint
        self.sent.append(endpoint)
        parts = urlsplit(endpoint)
        key = parts.path.removeprefix("/")
        query = parse_qs(parts.query)
        if key.endswith("/actions/workflows/ci-gate.yml/runs"):
            assert query["event"] == ["pull_request"] and query["created"][0].startswith(">="), query
            key += "?inventory"
        budget = {"X-RateLimit-Resource": "core", "X-RateLimit-Remaining": "4000",
                  "X-RateLimit-Reset": str(int(time.time()) + 1800)}
        value = self.documents.get(key)
        if value is None:
            return github_client.Response(404, budget, b'{"message": "Not Found"}')
        return github_client.Response(200, budget, json.dumps(value).encode())


def _run(run_id: int, head: str, attempts: int, created: datetime, pulls: list | None = None) -> dict:
    return {"id": run_id, "event": "pull_request", "head_sha": head, "head_branch": "feat", "run_attempt": attempts,
            "created_at": created.isoformat(), "repository": {"id": RUFF_REPO_ID}, "pull_requests": pulls or []}


def _job(job_id: int, started: datetime, *, lint: str) -> dict:
    steps = _ruff_steps(lint=lint, guards=("success",) * 3 if lint == "success" else ("skipped",) * 3)
    return {"id": job_id, "name": "Ruff", "status": "completed", "started_at": started.isoformat(),
            "completed_at": started.isoformat(), "conclusion": lint, "steps": steps}


def _check_run(check_run_id: int, run_id: int, head: str) -> dict:
    return {"id": check_run_id, "name": "Ruff", "head_sha": head, "app": {"id": 15368, "slug": "github-actions"},
            "details_url": f"https://github.com/{RUFF_REPO}/actions/runs/{run_id}/job/{check_run_id}",
            "check_suite": {"id": run_id * 10}, "status": "completed", "conclusion": "success"}


def test_ruff_collector_reads_forward_every_attempt_and_reports_incomplete_intervals(tmp_path, monkeypatch):
    """Defect class: attempts lost to latest-run collapse, to a force push between two collections, to a pre-pilot
    attempt entering the denominator, or to a read that silently stopped short (AC-02, AC-14)."""
    batch_state, _ = _state(tmp_path)
    head_a, head_b, head_c, base = "a" * 40, "b" * 40, "c" * 40, "0" * 40
    root = f"repos/{RUFF_REPO}"
    after = PILOT + timedelta(hours=1)
    patch = "@@ -0,0 +1,3 @@\n+import os\n+import sys\n+x = 1"
    compare = {"total_commits": 1, "commits": [{"sha": "1" * 40}],
               "files": [{"filename": "lu_machinery/x.py", "additions": 3, "deletions": 0, "patch": patch}]}
    documents: dict[str, object] = {
        # Red head A, rerun green on the same head, then force-pushed to green head B: only B is the PR's head now.
        f"{root}/actions/workflows/ci-gate.yml/runs?inventory": {"total_count": 2, "workflow_runs": [
            _run(501, head_a, 2, after), _run(502, head_b, 1, after + timedelta(minutes=5))]},
        f"{root}/pulls": [
            {"number": 7, "updated_at": (after + timedelta(minutes=5)).isoformat(),
             "head": {"ref": "feat", "sha": head_b, "repo": {"full_name": RUFF_REPO}}, "base": {"sha": base}},
            {"number": 3, "updated_at": (PILOT - timedelta(days=9)).isoformat(),
             "head": {"ref": "old", "sha": head_c, "repo": {"full_name": RUFF_REPO}}, "base": {"sha": base}}],
        f"{root}/actions/runs/501/attempts/1/jobs": {"total_count": 1, "jobs": [_job(9001, after, lint="failure")]},
        f"{root}/actions/runs/501/attempts/2/jobs": {"total_count": 1, "jobs": [
            _job(9002, after + timedelta(minutes=2), lint="success")]},
        f"{root}/actions/runs/502/attempts/1/jobs": {"total_count": 1, "jobs": [
            _job(9101, after + timedelta(minutes=5), lint="success")]},
        f"{root}/check-runs/9001": {"output": {"annotations_count": 2}},
        f"{root}/check-runs/9001/annotations": [
            {"path": "lu_machinery/x.py", "start_line": 2, "title": "ruff (F401)"},
            {"path": ".github", "start_line": 1, "title": ""}],
        f"{root}/compare/{base}...{head_a}": compare, f"{root}/compare/{base}...{head_b}": compare,
        # What the base changed since the merge-base (the lint ran on the merge): nothing here.
        f"{root}/compare/{head_a}...{base}": {"total_commits": 0, "commits": [], "files": []},
        f"{root}/compare/{head_b}...{base}": {"total_commits": 0, "commits": [], "files": []},
        f"{root}/commits/{head_a}/check-runs": {"total_count": 2, "check_runs": [
            _check_run(9001, 501, head_a), _check_run(9002, 501, head_a)]},
        # Head B also carries a rerun of a run created before the pilot: one attempt before it, one after.
        f"{root}/commits/{head_b}/check-runs": {"total_count": 2, "check_runs": [
            _check_run(9101, 502, head_b), _check_run(9301, 503, head_b)]},
        f"{root}/actions/runs/503": _run(503, head_b, 2, PILOT - timedelta(hours=5)),
        f"{root}/actions/runs/503/attempts/1/jobs": {"total_count": 1, "jobs": [
            _job(9300, PILOT - timedelta(hours=5), lint="failure")]},
        f"{root}/actions/runs/503/attempts/2/jobs": {"total_count": 1, "jobs": [
            _job(9301, after + timedelta(minutes=9), lint="success")]},
        f"{root}/check-runs/9300": {"output": {"annotations_count": 0}},
        f"{root}/check-runs/9300/annotations": [],
    }
    fake = _RuffGitHub(documents)
    client = github_client.GitHubClient(cache_dir=tmp_path / "github-cache", transport=fake,
                                        env={"GH_HOST": "github.com", "GH_TOKEN": "fixture-token"})
    counts = rows.import_ruff_attempts(batch_state, client, repository=RUFF_REPO, pilot_window_start=PILOT, now=NOW)
    assert counts["receipts_observed"] == 5 and counts["incomplete_intervals"] == 0, counts
    paths = [urlsplit(endpoint).path for endpoint in fake.sent]
    assert all("filter=all" in endpoint for endpoint in fake.sent if "/check-runs?" in endpoint)
    assert any("sort=updated" in endpoint for endpoint in fake.sent if urlsplit(endpoint).path.endswith("/pulls"))
    assert f"/{root}/actions/runs" not in paths and f"{root}/actions/runs" not in paths  # no backfill search
    attempts, _ = rows.read_ruff_attempts(batch_state)
    by_id = {receipt["check_run_id"]: receipt for receipt in attempts}
    assert sorted(by_id) == [9001, 9002, 9101, 9300, 9301]
    assert by_id[9001]["measured_head_sha"] == by_id[9002]["measured_head_sha"] == head_a
    assert by_id[9001]["pr"] == 7 and by_id[9001]["findings"] == [{"code": "F401", "origin": "pr"}]
    assert [rows.classify_ruff_attempt(by_id[key]) for key in (9001, 9002, 9101)] == [
        "ruff_code_failure", "ruff_pass", "ruff_pass"]
    quality = stats.collect(batch_state, tmp_path, NOW, landing=stats.Landing())["ruff_quality"]
    assert quality["pilot_window_start"] == PILOT.isoformat() and quality["incomplete_intervals"] == []
    assert quality["denominator"] == 4 and quality["coverage"]["before_pilot"] == 1
    assert quality["buckets"]["ruff_code_failure"] == 1 and quality["buckets"]["ruff_pass"] == 3

    # The pilot start is frozen.
    with pytest.raises(ValueError, match="frozen"):
        rows.import_ruff_attempts(batch_state, client, repository=RUFF_REPO,
                                  pilot_window_start=PILOT + timedelta(hours=1), now=NOW)
    # An inventory GitHub cannot prove whole leaves the watermark and is reported as an incomplete interval.
    documents[f"{root}/actions/workflows/ci-gate.yml/runs?inventory"] = {"total_count": 3, "workflow_runs": []}
    later = NOW + timedelta(hours=1)
    # A revisit that cannot read a run's comparison records nothing for that run (round 1 F1).
    del documents[f"{root}/compare/{base}...{head_b}"]
    revisit = rows.import_ruff_attempts(batch_state, client, repository=RUFF_REPO, now=later)
    assert (revisit["receipts_observed"], revisit["runs_deferred"]) == (0, 1), revisit
    assert len(rows.read_ruff_attempts(batch_state)[0]) == 5
    state = rows.read_ruff_state(batch_state)
    assert state["watermark"] == NOW.isoformat()
    assert state["incomplete_intervals"] == [
        {"since": NOW.isoformat(), "until": later.isoformat(), "reason": "github_collection_incomplete"}]
    snapshot = stats.collect(batch_state, tmp_path, later, landing=stats.Landing())
    with _serve(snapshot, tmp_path, monkeypatch) as served:
        full = served.get("/api/fleet/model-stats?window=7d&view=full").json()
        page = served.get("/api/fleet/model-stats?window=7d&view=page").json()
    assert full["ruff_quality"]["incomplete_intervals"] == state["incomplete_intervals"]
    assert full["ruff_quality"]["report_only"] is True and "ruff_quality" not in page
    # The existing figures are still served beside it (AC-13).
    assert {"models", "coverage", "weeks", "first_record"} <= set(full) and "models" in page
