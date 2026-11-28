"""Milestone completion and private text exclusion at the HTTP boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import milestones_router as milestones
from lu_machinery.api import state_helpers
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.common import git_credentials, github_client
from lu_machinery.github_rest_cache import GitHubRestCache
from lu_machinery.orchestration.fleet_repos import FleetRepo


@pytest.mark.parametrize("active", ["learn-ukrainian/machinery", "agentic-machinery/machinery"])
def test_machinery_page_renders_the_active_changelog_repository(tmp_path, monkeypatch, active):
    """Defect class: the dashboard links to the baked repository after a selector flip."""
    from lu_machinery.api import main
    from lu_machinery.api.product_contribution import ProductContribution

    root = tmp_path / "dashboards"
    root.mkdir()
    shutil.copyfile(Path(main.__file__).with_name("dashboards") / "index.html", root / "index.html")
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"machinery": active}, "d" * 64),
    )
    app = main.create_app(fixture_context(tmp_path), product=ProductContribution())
    response = TestClient(app).get("/machinery")
    assert response.status_code == 200
    assert f'data-repository="{active}"' in response.text


@pytest.mark.parametrize("public_slug", ["fixture/public", "new-owner/public"])
def test_milestone_percent_and_private_counts_only(tmp_path, monkeypatch, public_slug):
    """Milestone lookups and issue links follow the selector while private details stay withheld."""
    repos = {
        "public": FleetRepo(
            "public", "fixture/public", "public", "public-content", True,
            alternate_github="new-owner/public", repository_id=42, visibility="public",
        ),
        "private": FleetRepo("private", "fixture/private", "private", "private-infra"),
    }
    monkeypatch.setattr(
        git_credentials, "load_selector",
        lambda **_kwargs: git_credentials.SelectorState({"public": public_slug}, "d" * 64),
    )
    calls = []
    milestone = {
        "number": 1, "state": "open", "title": "Ship <progress>", "description": "First line\nSecond line",
        "due_on": "2030-01-01T00:00:00Z", "open_issues": 2, "closed_issues": 6,
    }

    def transport(method, path, headers, body, timeout):
        assert method == "GET"
        calls.append((path, headers.get("If-None-Match")))
        if headers.get("If-None-Match") == '"fixture"':
            return github_client.Response(304, {}, b"")
        if "/milestones?" in path:
            row = {**milestone}
            if "fixture/private/" in path:
                row.update(title="private-title", description="private-description", html_url="private-link")
            body = [row]
        else:
            assert "fixture/private" not in path
            body = [
                {"number": number, "state": "open", "title": f"Work {number}", "milestone": {"number": 1},
                 "html_url": "javascript:untrusted", "labels": [{"name": "bug"}]}
                for number in (7, 8)
            ]
            # GitHub's issues collection also returns pull requests.
            body.append({**body[0], "number": 9, "pull_request": {}})
        return github_client.Response(200, {"etag": '"fixture"'}, json.dumps(body).encode())

    cache = GitHubRestCache(
        github_client.GitHubClient(
            cache_dir=tmp_path / "github",
            env={"GH_CONFIG_DIR": str(tmp_path)},
            transport=transport,
        )
    )
    monkeypatch.setattr(milestones, "shared_cache", lambda: cache)
    monkeypatch.setattr(milestones, "load_fleet_repos", lambda: repos)
    monkeypatch.setattr(milestones, "schedule_cache_refresh",
                        lambda key, ttl, compute: state_helpers.cache_set(key, compute()))
    app = FastAPI()
    app.state.ctx = fixture_context(tmp_path)
    app.include_router(milestones.router, prefix="/api/milestones")
    with TestClient(app) as client:
        assert client.get("/api/milestones").json()["status"] == "pending"
        response = client.get("/api/milestones")
        data = response.json()
        assert response.status_code == 200
        assert data["status"] == "ok"
        assert data["refresh_seconds"] == 300
        assert float(response.headers["X-Snapshot-Age-S"]) >= 0
        public, private = data["repositories"]
        assert public["repository"] == public_slug
        item = public["milestones"][0]
        assert (item["open_issues"], item["closed_issues"], item["percent_complete"]) == (2, 6, 75.0)
        assert item["description_first_line"] == "First line"
        assert item["due_on"] == "2030-01-01T00:00:00Z"
        assert [issue["number"] for issue in item["issues"]] == [7, 8]
        assert item["issues"][0] == {"number": 7, "title": "Work 7", "labels": ["bug"],
                                     "url": f"https://github.com/{public_slug}/issues/7"}
        assert private["milestones"] == [{
            "number": None, "title": None, "description_first_line": None, "due_on": None,
            "open_issues": 2, "closed_issues": 6, "percent_complete": 75.0, "issues_status": "withheld", "issues": [],
        }]
        assert "private-title" not in response.text
        assert "private-description" not in response.text
        assert "private-link" not in response.text
        assert len(calls) == 3  # One milestones list each; only the public repo needs issue details.
        assert {path.split("?", 1)[0] for path, _ in calls} == {
            f"repos/{public_slug}/milestones", f"repos/{public_slug}/issues", "repos/fixture/private/milestones",
        }
        warm = client.get("/api/milestones").json()["repositories"]
        for original, refreshed in zip(data["repositories"], warm, strict=True):
            assert original["age_seconds"] >= 0
            assert refreshed.pop("age_seconds") >= 0
            assert refreshed == {key: value for key, value in original.items() if key != "age_seconds"}
        assert len(calls) == 3  # Warm page polls never reach GitHub.

    # The same representative artifact proves validators are reused on the next collection.
    assert milestones.collect_milestones()["status"] == "ok"
    assert len(calls) == 6
    assert all(etag == '"fixture"' for _, etag in calls[3:])

    dashboards = Path(milestones.__file__).with_name("dashboards")
    page = dashboards.joinpath("milestones.html").read_text()
    marker = '<script src="/machinery.js"></script>'
    after = page.split(marker, 1)[1]
    script = after.split("<script>", 1)[1].split("const PANELS = [", 1)[0]
    rendered = subprocess.run(
        ["node", "-e",
         'global.document = {querySelectorAll: () => []};\n'
         f'global.Machinery = require({json.dumps(str(dashboards / "machinery.js"))});\n'
         + script
         + '\nprocess.stdout.write(renderMilestones(' + json.dumps(data) + '));'],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    assert "6/8 (75%)" in rendered
    assert "6 of 8 closed (75%)" in rendered
    assert 'value="75"' in rendered
    assert "Ship &lt;progress&gt;" in rendered
    assert "Work 7" in rendered
    assert "private-title" not in rendered
    assert "Private milestone" in rendered
    assert "withheld" in rendered
    assert "unknown" in rendered


def _run_milestones_node(body: str) -> str:
    dashboards = Path(milestones.__file__).with_name("dashboards")
    page = dashboards.joinpath("milestones.html").read_text()
    marker = '<script src="/machinery.js"></script>'
    after = page.split(marker, 1)[1]
    script = after.split("<script>", 1)[1].split("const PANELS = [", 1)[0]
    code = (
        'const assert = require("node:assert/strict");\n'
        'global.document = {querySelectorAll: () => [], querySelector: () => null, getElementById: () => null};\n'
        f'global.Machinery = require({json.dumps(str(dashboards / "machinery.js"))});\n'
        + script
        + "\n"
        + body
    )
    return subprocess.run(
        ["node", "-e", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


def test_milestones_page_sorts_overdue_first_and_joins_blockers():
    """Defect class: wrong order or a blocked issue missing its blockers."""
    now_iso = "2030-01-10T00:00:00Z"
    milestones_fixture = {
        "status": "ok",
        "age_seconds": 120.0,
        "stale": False,
        "repositories": [
            {
                "repository": "Fixture/Machinery",
                "private": False,
                "status": "ok",
                "stale": False,
                "milestones": [
                    {
                        "number": 10,
                        "title": "No due low",
                        "description_first_line": "",
                        "due_on": None,
                        "open_issues": 6,
                        "closed_issues": 4,
                        "percent_complete": 40.0,
                        "issues_status": "ok",
                        "issues": [],
                    },
                    {
                        "number": 2,
                        "title": "v0.4.0",
                        "description_first_line": "Next release",
                        "due_on": "2030-01-17T00:00:00Z",
                        "open_issues": 4,
                        "closed_issues": 16,
                        "percent_complete": 80.0,
                        "issues_status": "ok",
                        "issues": [
                            {
                                "number": 100,
                                "title": "Ready feature",
                                "url": "https://github.com/Fixture/Machinery/issues/100",
                                "labels": ["feature"],
                            },
                            {
                                "number": 101,
                                "title": "Blocked migration",
                                "url": "https://github.com/Fixture/Machinery/issues/101",
                                "labels": ["infra"],
                            },
                            {
                                "number": 102,
                                "title": "Label only not dep-blocked",
                                "url": "https://github.com/Fixture/Machinery/issues/102",
                                "labels": ["blocked"],
                            },
                            {
                                "number": 103,
                                "title": "Scope missing",
                                "url": "https://github.com/Fixture/Machinery/issues/103",
                                "labels": [],
                            },
                        ],
                    },
                    {
                        "number": 1,
                        "title": "v0.3.9",
                        "description_first_line": "Patch release",
                        "due_on": "2030-01-07T00:00:00Z",
                        "open_issues": 1,
                        "closed_issues": 9,
                        "percent_complete": 90.0,
                        "issues_status": "ok",
                        "issues": [
                            {
                                "number": 88,
                                "title": "Blocker root",
                                "url": "https://github.com/Fixture/Machinery/issues/88",
                                "labels": ["p0"],
                            },
                        ],
                    },
                    {
                        "number": 3,
                        "title": "Overdue later",
                        "description_first_line": "",
                        "due_on": "2030-01-09T00:00:00Z",
                        "open_issues": 1,
                        "closed_issues": 1,
                        "percent_complete": 50.0,
                        "issues_status": "ok",
                        "issues": [],
                    },
                    {
                        "number": 11,
                        "title": "No due high",
                        "description_first_line": "",
                        "due_on": None,
                        "open_issues": 1,
                        "closed_issues": 9,
                        "percent_complete": 90.0,
                        "issues_status": "ok",
                        "issues": [],
                    },
                ],
            },
            {
                "repository": "fixture/private",
                "private": True,
                "status": "ok",
                "stale": False,
                "milestones": [
                    {
                        "number": None,
                        "title": None,
                        "description_first_line": None,
                        "due_on": None,
                        "open_issues": 4,
                        "closed_issues": 6,
                        "percent_complete": 60.0,
                        "issues_status": "withheld",
                        "issues": [],
                    },
                ],
            },
        ],
    }
    planning_fixture = {
        "schema_version": "model-planning.v1",
        "issues": {
            "status": "ok",
            "age_s": 240.0,
            "workers": "ok",
            "coverage": {
                "repositories_read": 2,
                "repositories_total": 2,
                "open_issues": 6,
                "dependency_unknown": 0,
                "private_open_issues": 1,
            },
            "blockers": [
                {"ref": "fixture/machinery#88", "direct": 1, "total": 2},
                {"ref": None, "direct": 1, "total": 1},
            ],
            "ready": [
                {"ref": "fixture/machinery#88", "path_status": "ready_now"},
                {"ref": "fixture/machinery#100", "path_status": "ready_now"},
                {"ref": "fixture/machinery#102", "path_status": "path_conflicts"},
                {"ref": "fixture/machinery#103", "path_status": "scope_unknown"},
            ],
            "ready_now": [
                {"ref": "fixture/machinery#88"},
                {"ref": "fixture/machinery#100"},
            ],
            "blocked": [
                {
                    "ref": "fixture/machinery#101",
                    "blocked_by": ["fixture/machinery#88", "fixture/other#9"],
                    "private_blockers": 1,
                },
            ],
        },
        "capacity": [],
    }

    body = f"""
const now = Date.parse({json.dumps(now_iso)});
const data = {json.dumps(milestones_fixture)};
const planning = {json.dumps(planning_fixture)};
const html = renderMilestones(data, planning, now);
const chips = renderMilestoneChips({{milestones: data, planning}}, now);

// Order in At-a-glance summary table: overdue earliest first -> overdue later -> due soonest -> no-due % desc.
const summary = html.split("<h2>Milestone details</h2>")[0];
const expectedOrder = ["v0.3.9", "Overdue later", "v0.4.0", "No due high", "Private milestone", "No due low"];
const positions = expectedOrder.map(name => summary.indexOf(name));
assert.ok(positions.every(pos => pos >= 0), "every milestone appears in summary: " + JSON.stringify(positions));
for (let i = 1; i < positions.length; i++) {{
  assert.ok(positions[i - 1] < positions[i], `order mismatch: ${{expectedOrder[i - 1]}} before ${{expectedOrder[i]}}`);
}}

// Overdue text, anchors, th scope="col", closed issues link, freshness strip.
assert.match(summary, /OVERDUE 3 d/);
assert.match(summary, /href="#ms-machinery-2"/);
assert.match(summary, /<th scope="col">Milestone<\\/th>/);
assert.match(html, /id="ms-machinery-2"/);
assert.match(html, /https:\\/\\/github\\.com\\/Fixture\\/Machinery\\/milestone\\/2\\?closed=1/);
assert.match(html, /data 2m, planning 4m/);

// Inside v0.4.0: blocked issue #101 comes BEFORE unblocked #100, joins case-folded blockers + private count.
const cardStart = html.indexOf('id="ms-machinery-2"');
const cardEnd = html.indexOf("</article>", cardStart);
const card = html.slice(cardStart, cardEnd);
assert.match(card, /16 of 20 closed \\(80%\\)/);
assert.match(card, /<h4>Blocked \\(1\\)<\\/h4>/);
assert.match(card, /<h4>Open \\(3\\)<\\/h4>/);
assert.ok(card.indexOf("#101 Blocked migration") < card.indexOf("#100 Ready feature"), "blocked issue appears first");
assert.match(card, /Blocked<\\/span> by <a href="https:\\/\\/github\\.com\\/fixture\\/machinery\\/issues\\/88">#88<\\/a>, <a href="https:\\/\\/github\\.com\\/fixture\\/other\\/issues\\/9">fixture\\/other#9<\\/a>, \\+1 private/);

// Label "blocked" on #102 does NOT make it a blocker (stays in Open, badge shows Path conflict, label shown as label).
const blockedSection = card.slice(card.indexOf("<h4>Blocked (1)</h4>"), card.indexOf("<h4>Open (3)</h4>"));
const openSection = card.slice(card.indexOf("<h4>Open (3)</h4>"));
assert.doesNotMatch(blockedSection, /#102/);
assert.match(openSection, /#102 Label only not dep-blocked/);
assert.match(openSection, /Path conflict/);
assert.match(openSection, /Ready now/);
assert.match(openSection, /Scope unknown/);

// Top blockers and Ready now moved from model-stats.
assert.match(html, /<h2 id="top-blockers">Top blockers<\\/h2>/);
assert.match(html, /unblocks 2 \\(direct 1\\)/);
assert.match(html, /Private issue/);
assert.match(html, /<h2 id="ready-now">Ready now<\\/h2>/);

// Header chips reflect open, overdue, blocked, ready now.
assert.match(chips, /<b>6<\\/b> open/);
assert.match(chips, /<b>2<\\/b> overdue/);
assert.match(chips, /<b>1<\\/b> blocked/);
assert.match(chips, /<b>2<\\/b> ready now/);
"""
    _run_milestones_node(body)


def test_milestones_page_shows_unknown_or_withheld_never_zero():
    """Defect class: blocked count reads 0 when planning is down or the milestone is private."""
    now_iso = "2030-01-10T00:00:00Z"
    milestones_fixture = {
        "status": "ok",
        "age_seconds": 60.0,
        "stale": False,
        "repositories": [
            {
                "repository": "fixture/machinery",
                "private": False,
                "status": "ok",
                "stale": False,
                "milestones": [
                    {
                        "number": 4,
                        "title": "v0.4.0",
                        "description_first_line": "Release",
                        "due_on": "2030-01-20T00:00:00Z",
                        "open_issues": 2,
                        "closed_issues": 8,
                        "percent_complete": 80.0,
                        "issues_status": "ok",
                        "issues": [
                            {
                                "number": 10,
                                "title": "Task A",
                                "url": "https://github.com/fixture/machinery/issues/10",
                                "labels": [],
                            },
                        ],
                    },
                ],
            },
            {
                "repository": "fixture/private",
                "private": True,
                "status": "ok",
                "stale": False,
                "milestones": [
                    {
                        "number": None,
                        "title": None,
                        "description_first_line": None,
                        "due_on": None,
                        "open_issues": 3,
                        "closed_issues": 7,
                        "percent_complete": 70.0,
                        "issues_status": "withheld",
                        "issues": [],
                    },
                ],
            },
        ],
    }

    body = f"""
const now = Date.parse({json.dumps(now_iso)});
const data = {json.dumps(milestones_fixture)};

function blockedCellFor(html, rowMarker) {{
  const summary = html.split("<h2>Milestone details</h2>")[0];
  const rows = [...summary.matchAll(/<tr>(.*?)<\\/tr>/gs)].map(m => m[1]);
  const row = rows.find(r => r.includes(rowMarker));
  assert.ok(row, "row missing for " + rowMarker);
  const cells = [...row.matchAll(/<td[^>]*>(.*?)<\\/td>/gs)].map(m => m[1]);
  return cells[cells.length - 1];
}}

const unusableCases = [
  ["null/errored", null],
  ["status unknown", {{issues: {{status: "unknown", blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["status stale", {{issues: {{status: "stale", age_s: 500, blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["expired age_s", {{issues: {{status: "ok", age_s: 421, blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["status pending", {{issues: {{status: "pending"}}}}],
];
for (const [name, planning] of unusableCases) {{
  const html = renderMilestones(data, planning, now);
  const pubBlocked = blockedCellFor(html, "v0.4.0");
  const privBlocked = blockedCellFor(html, "Private milestone");
  assert.equal(pubBlocked, "unknown", `${{name}}: public blocked must be unknown, never 0`);
  assert.equal(privBlocked, "withheld", `${{name}}: private blocked must be withheld`);
  const chips = renderMilestoneChips({{milestones: data, planning}}, now);
  assert.match(chips, /<b>\\?<\\/b> blocked/, `${{name}}: blocked chip must be ?`);
  assert.match(chips, /<b>\\?<\\/b> ready now/, `${{name}}: ready now chip must be ?`);
}}

// Partial planning shows values AND "partial: N of M repos", while private stays "withheld".
const partialPlanning = {{
  issues: {{
    status: "partial",
    age_s: 90,
    workers: "unknown",
    coverage: {{repositories_read: 2, repositories_total: 3, open_issues: 5, dependency_unknown: 0, private_open_issues: 0}},
    blockers: [],
    ready: [],
    ready_now: [],
    blocked: [{{ref: "fixture/machinery#10", blocked_by: ["fixture/machinery#1"], private_blockers: 0}}],
  }},
}};
const partialHtml = renderMilestones(data, partialPlanning, now);
assert.match(partialHtml, /partial: 2 of 3 repos/);
assert.equal(blockedCellFor(partialHtml, "v0.4.0"), "1");
assert.equal(blockedCellFor(partialHtml, "Private milestone"), "withheld");
// Workers unknown -> ready-now fails closed (never 0 ready now).
const partialChips = renderMilestoneChips({{milestones: data, planning: partialPlanning}}, now);
assert.match(partialChips, /<b>\\?<\\/b> ready now/);
assert.match(partialHtml, /Worker paths are unavailable; ready-now results are withheld/);

// Incomplete milestone issue list with open_issues > 0 never reports 0 blocked even when planning is ok.
const incompleteData = JSON.parse(JSON.stringify(data));
incompleteData.repositories[0].milestones[0].issues_status = "unknown";
incompleteData.repositories[0].milestones[0].issues = [];
const okPlanning = {{
  issues: {{
    status: "ok", age_s: 30, workers: "ok",
    coverage: {{repositories_read: 2, repositories_total: 2, open_issues: 2, dependency_unknown: 0, private_open_issues: 0}},
    blockers: [], ready: [], ready_now: [], blocked: [],
  }},
}};
const incompleteHtml = renderMilestones(incompleteData, okPlanning, now);
assert.equal(blockedCellFor(incompleteHtml, "v0.4.0"), "unknown");
assert.match(incompleteHtml, /Open issue list is incomplete or unavailable/);
"""
    _run_milestones_node(body)
