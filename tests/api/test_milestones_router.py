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
    assert "6 of 8 closed" in rendered
    assert "<progress" not in rendered
    assert "75%" not in rendered
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
                        "title": "No due open",
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
                        "title": "No due blocked",
                        "description_first_line": "",
                        "due_on": None,
                        "open_issues": 1,
                        "closed_issues": 9,
                        "percent_complete": 90.0,
                        "issues_status": "ok",
                        "issues": [
                            {
                                "number": 110,
                                "title": "Undated blocked work",
                                "url": "https://github.com/Fixture/Machinery/issues/110",
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
                "open_issues": 7,
                "dependency_unknown": 0,
                "private_open_issues": 1,
            },
            "blockers": [
                {"ref": "fixture/machinery#88", "direct": 2, "total": 2},
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
                {
                    "ref": "fixture/machinery#110",
                    "blocked_by": ["fixture/machinery#88"],
                    "private_blockers": 0,
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

// Owner direction: no progress bars and no percentages anywhere on the page.
assert.doesNotMatch(html, /<progress\\b/i, "progress bar must not be rendered");
assert.doesNotMatch(html, /\\d+%/, "percentages must not be rendered");

// At-a-glance summary table leads with Blocked and Overdue counts after Milestone.
const summary = html.split("<h2>Milestone details</h2>")[0];
const headers = [...summary.matchAll(/<th scope="col"[^>]*>(.*?)<\\/th>/gs)].map(m => m[1]);
assert.deepEqual(headers, ["Milestone", "Blocked", "Overdue", "Due", "Open", "Closed", "Repo"]);

// Order in At-a-glance summary table: overdue earliest -> overdue later -> due soonest -> undated blocked -> undated open desc.
const expectedOrder = ["v0.3.9", "Overdue later", "v0.4.0", "No due blocked", "No due open", "Private milestone"];
const positions = expectedOrder.map(name => summary.indexOf(name));
assert.ok(positions.every(pos => pos >= 0), "every milestone appears in summary: " + JSON.stringify(positions));
for (let i = 1; i < positions.length; i++) {{
  assert.ok(positions[i - 1] < positions[i], `order mismatch: ${{expectedOrder[i - 1]}} before ${{expectedOrder[i]}}`);
}}

// Overdue text, anchors, plain-text closed count, closed issues link, freshness strip.
assert.match(summary, /OVERDUE 3 d/);
assert.match(summary, /16 of 20 closed/);
assert.match(summary, /href="#ms-machinery-2"/);
assert.match(html, /id="ms-machinery-2"/);
assert.match(html, /https:\\/\\/github\\.com\\/Fixture\\/Machinery\\/milestone\\/2\\?closed=1/);
assert.match(html, /data 2m, planning 4m/);

// Inside overdue v0.3.9: Overdue section holds unblocked overdue issue #88 with Ready now badge.
const overdueStart = html.indexOf('id="ms-machinery-1"');
const overdueEnd = html.indexOf("</article>", overdueStart);
const overdueCard = html.slice(overdueStart, overdueEnd);
assert.match(overdueCard, /9 of 10 closed/);
assert.match(overdueCard, /<h4>Overdue \\(1\\)<\\/h4>/);
assert.match(overdueCard, /#88 Blocker root/);
assert.match(overdueCard, /Ready now/);

// Inside non-overdue v0.4.0: Blocked -> Ready now -> Other open, plain-text "16 of 20 closed".
const cardStart = html.indexOf('id="ms-machinery-2"');
const cardEnd = html.indexOf("</article>", cardStart);
const card = html.slice(cardStart, cardEnd);
assert.match(card, /16 of 20 closed/);
assert.match(card, /<h4>Blocked \\(1\\)<\\/h4>/);
assert.match(card, /<h4>Ready now \\(1\\)<\\/h4>/);
assert.match(card, /<h4>Other open \\(2\\)<\\/h4>/);
assert.ok(card.indexOf("<h4>Blocked (1)</h4>") < card.indexOf("<h4>Ready now (1)</h4>"));
assert.ok(card.indexOf("<h4>Ready now (1)</h4>") < card.indexOf("<h4>Other open (2)</h4>"));
assert.ok(card.indexOf("#101 Blocked migration") < card.indexOf("#100 Ready feature"), "blocked issue appears first");
assert.ok(card.indexOf("#100 Ready feature") < card.indexOf("#102 Label only not dep-blocked"), "ready now appears before other open");
assert.match(card, /Blocked<\\/span> by <a href="https:\\/\\/github\\.com\\/fixture\\/machinery\\/issues\\/88">#88<\\/a>, <a href="https:\\/\\/github\\.com\\/fixture\\/other\\/issues\\/9">fixture\\/other#9<\\/a>, \\+1 private/);

// Label "blocked" on #102 does NOT make it a blocker (stays in Other open, badge shows Path conflict, label shown as label).
const blockedSection = card.slice(card.indexOf("<h4>Blocked (1)</h4>"), card.indexOf("<h4>Ready now (1)</h4>"));
const otherSection = card.slice(card.indexOf("<h4>Other open (2)</h4>"));
assert.doesNotMatch(blockedSection, /#102/);
assert.match(otherSection, /#102 Label only not dep-blocked/);
assert.match(otherSection, /Path conflict/);
assert.match(otherSection, /Scope unknown/);

// Top blockers and Ready now moved from model-stats.
assert.match(html, /<h2 id="top-blockers">Top blockers<\\/h2>/);
assert.match(html, /unblocks 2 \\(direct 2\\)/);
assert.match(html, /Private issue/);
assert.match(html, /<h2 id="ready-now">Ready now<\\/h2>/);

// Header chips lead with blocked and overdue, then ready now and open.
assert.match(chips, /<b>2<\\/b> blocked/);
assert.match(chips, /<b>2<\\/b> overdue/);
assert.match(chips, /<b>2<\\/b> ready now/);
assert.match(chips, /<b>6<\\/b> open/);
assert.ok(chips.indexOf("blocked") < chips.indexOf("overdue"));
assert.ok(chips.indexOf("overdue") < chips.indexOf("open"));
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

function summaryCellsFor(html, rowMarker) {{
  const summary = html.split("<h2>Milestone details</h2>")[0];
  const rows = [...summary.matchAll(/<tr>(.*?)<\\/tr>/gs)].map(m => m[1]);
  const row = rows.find(r => r.includes(rowMarker));
  assert.ok(row, "row missing for " + rowMarker);
  return [...row.matchAll(/<td[^>]*>(.*?)<\\/td>/gs)].map(m => m[1]);
}}

const unusableCases = [
  ["null/errored", null],
  ["status unknown", {{issues: {{status: "unknown", blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["status stale", {{issues: {{status: "stale", age_s: 500, blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["expired age_s", {{issues: {{status: "ok", age_s: 421, blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["missing age_s", {{issues: {{status: "ok", blocked: [], ready: [], ready_now: [], blockers: []}}}}],
  ["status pending", {{issues: {{status: "pending"}}}}],
];
for (const [name, planning] of unusableCases) {{
  const html = renderMilestones(data, planning, now);
  const pubCells = summaryCellsFor(html, "v0.4.0");
  const privCells = summaryCellsFor(html, "Private milestone");
  assert.equal(pubCells[1], "unknown", `${{name}}: public blocked must be unknown, never 0`);
  assert.equal(privCells[1], "withheld", `${{name}}: private blocked must be withheld`);
  assert.equal(privCells[2], "withheld", `${{name}}: private overdue must be withheld, never 0`);
  const chips = renderMilestoneChips({{milestones: data, planning}}, now);
  assert.match(chips, /<b>unknown<\\/b> blocked/, `${{name}}: blocked chip must be unknown`);
  assert.match(chips, /<b>unknown<\\/b> ready now/, `${{name}}: ready now chip must be unknown`);
}}

// All-private snapshot: overdue chip fails closed as withheld, never 0.
const privateOnly = {{
  ...data,
  repositories: [data.repositories[1]],
}};
const privateChips = renderMilestoneChips({{milestones: privateOnly, planning: null}}, now);
assert.match(privateChips, /<b>withheld<\\/b> overdue/);

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
assert.equal(summaryCellsFor(partialHtml, "v0.4.0")[1], "1");
assert.equal(summaryCellsFor(partialHtml, "Private milestone")[1], "withheld");
assert.equal(summaryCellsFor(partialHtml, "Private milestone")[2], "withheld");
// Workers unknown -> ready-now fails closed (never 0 ready now).
const partialChips = renderMilestoneChips({{milestones: data, planning: partialPlanning}}, now);
assert.match(partialChips, /<b>unknown<\\/b> ready now/);
assert.match(partialHtml, /Worker paths are unavailable; ready-now results are withheld/);

// F1: When planning misses a milestone issue (unaccounted in both blocked and ready),
// 0 observed blockers renders "unknown" (never "0"), and >0 observed renders "<n> observed".
const missedPlanning = {{
  issues: {{
    status: "partial",
    age_s: 60,
    workers: "ok",
    coverage: {{repositories_read: 2, repositories_total: 3, open_issues: 5, dependency_unknown: 0, private_open_issues: 0}},
    blockers: [],
    ready: [],
    ready_now: [],
    blocked: [],
  }},
}};
const missedHtml = renderMilestones(data, missedPlanning, now);
assert.equal(summaryCellsFor(missedHtml, "v0.4.0")[1], "unknown", "unaccounted milestone issue must never report 0 blocked");
assert.match(missedHtml, /no planning verdict; blocker count is incomplete/);
const missedChips = renderMilestoneChips({{milestones: data, planning: missedPlanning}}, now);
assert.match(missedChips, /<b>unknown<\\/b> blocked \\(partial\\)/);
assert.match(missedChips, /<b>unknown<\\/b> ready now \\(partial\\)/, "partial planning with an unaccounted issue never reports 0 ready now");

// F1 sibling: even when Planning status is "ok" with 0 dependency_unknown, an open milestone issue
// missing from Planning withholds the fleet 0 blocked chip (shows unknown blocked (incomplete), never 0).
const okEmptyPlanning = {{
  issues: {{
    status: "ok",
    age_s: 60,
    workers: "ok",
    coverage: {{repositories_read: 2, repositories_total: 2, open_issues: 0, dependency_unknown: 0, private_open_issues: 0}},
    blockers: [],
    ready: [],
    ready_now: [],
    blocked: [],
  }},
}};
const okMissedHtml = renderMilestones(data, okEmptyPlanning, now);
assert.equal(summaryCellsFor(okMissedHtml, "v0.4.0")[1], "unknown");
const okMissedChips = renderMilestoneChips({{milestones: data, planning: okEmptyPlanning}}, now);
assert.match(okMissedChips, /<b>unknown<\\/b> blocked \\(incomplete\\)/);
assert.doesNotMatch(okMissedChips, /<b>0<\\/b> blocked/);

const twoIssueData = JSON.parse(JSON.stringify(data));
twoIssueData.repositories[0].milestones[0].issues.push({{
  number: 11,
  title: "Task B missing from planning",
  url: "https://github.com/fixture/machinery/issues/11",
  labels: [],
}});
const observedHtml = renderMilestones(twoIssueData, partialPlanning, now);
assert.equal(summaryCellsFor(observedHtml, "v0.4.0")[1], "1 observed");

// F2: Unmatched blockers are classified outside open milestones ONLY when repo milestone & issue coverage is complete and fresh.
const completeWithOutside = {{
  issues: {{
    status: "ok", age_s: 30, workers: "ok",
    coverage: {{repositories_read: 2, repositories_total: 2, open_issues: 3, dependency_unknown: 0, private_open_issues: 0}},
    blockers: [],
    ready: [{{ref: "fixture/machinery#10", path_status: "ready_now"}}],
    ready_now: [{{ref: "fixture/machinery#10"}}],
    blocked: [{{ref: "fixture/machinery#99", blocked_by: ["fixture/machinery#1"], private_blockers: 0}}],
  }},
}};
const outsideHtml = renderMilestones(data, completeWithOutside, now);
assert.match(outsideHtml, /Blocked issues outside open milestones \\(1\\)/);
assert.doesNotMatch(outsideHtml, /milestone membership unknown/);

// F2 expiry: once the milestone reading's elapsed age crosses the 420s freshness bound,
// unmatched blockers immediately move from outside-open-milestones to milestone-membership-unknown.
const freshReading = {{
  data,
  ageHeader: "419.5",
  boundHeader: "420",
  state: "fresh",
  receivedAt: now,
}};
const beforeExpiryHtml = renderMilestones(data, completeWithOutside, now + 200, null, freshReading);
assert.match(beforeExpiryHtml, /Blocked issues outside open milestones \\(1\\)/);
const afterExpiryHtml = renderMilestones(data, completeWithOutside, now + 1000, null, freshReading);
assert.doesNotMatch(afterExpiryHtml, /Blocked issues outside open milestones/);
assert.match(afterExpiryHtml, /Blocked issues \\(milestone membership unknown: 1\\)/);
assert.match(afterExpiryHtml, /STALE/);

const partialListData = JSON.parse(JSON.stringify(data));
partialListData.repositories[0].milestones[0].issues_status = "partial";
const unknownMembershipHtml = renderMilestones(partialListData, completeWithOutside, now);
assert.doesNotMatch(unknownMembershipHtml, /Blocked issues outside open milestones/);
assert.match(unknownMembershipHtml, /Blocked issues \\(milestone membership unknown: 1\\)/);

// F3: Planning sections (Top blockers, Ready now, membership-unknown blockers) still render when milestones are pending or unavailable.
const pendingWithPlanning = renderMilestones({{status: "pending", repositories: []}}, completeWithOutside, now);
assert.match(pendingWithPlanning, /<h2 id="top-blockers">Top blockers<\\/h2>/);
assert.match(pendingWithPlanning, /<h2 id="ready-now">Ready now<\\/h2>/);
assert.match(pendingWithPlanning, /fixture\\/machinery#10/);
assert.match(pendingWithPlanning, /Blocked issues \\(milestone membership unknown: 1\\)/);
const outageWithPlanning = renderMilestones(null, completeWithOutside, now);
assert.match(outageWithPlanning, /<h2 id="top-blockers">Top blockers<\\/h2>/);
assert.match(outageWithPlanning, /<h2 id="ready-now">Ready now<\\/h2>/);
assert.match(outageWithPlanning, /Blocked issues \\(milestone membership unknown: 1\\)/);

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
assert.equal(summaryCellsFor(incompleteHtml, "v0.4.0")[1], "unknown");
assert.match(incompleteHtml, /Open issue list is incomplete or unavailable/);
// The header takes the same incomplete issue list as the card: never 0 blocked or 0 ready now.
const incompleteChips = renderMilestoneChips({{milestones: incompleteData, planning: okPlanning}}, now);
assert.match(incompleteChips, /<b>unknown<\\/b> blocked \\(incomplete\\)/);
assert.match(incompleteChips, /<b>unknown<\\/b> ready now \\(incomplete\\)/);
assert.doesNotMatch(incompleteChips, /<b>0<\\/b> (blocked|ready now)/);

// A missing open count reads unknown in the Open and Overdue cells, never 0 or "?".
const noCountData = JSON.parse(JSON.stringify(data));
noCountData.repositories[0].milestones[0].open_issues = null;
noCountData.repositories[0].milestones[0].due_on = "2030-01-05T00:00:00Z";
const noCountCells = summaryCellsFor(renderMilestones(noCountData, okPlanning, now), "v0.4.0");
assert.equal(noCountCells[2], "unknown");
assert.equal(noCountCells[4], "unknown");

// F2: a fresh partial snapshot (one complete public repo, one failed repo, X-Snapshot-State
// "unavailable") keeps the complete repo's membership and is never labelled STALE.
const partialSnapshot = JSON.parse(JSON.stringify(data));
partialSnapshot.status = "partial";
partialSnapshot.age_seconds = 30.0;
partialSnapshot.repositories.push({{
  repository: "fixture/broken", private: false, status: "unknown", reason: "github_unavailable",
  stale: false, age_seconds: 30.0, milestones: [],
}});
const partialReading = {{
  data: partialSnapshot, ageHeader: "30.000", boundHeader: "420", state: "unavailable", receivedAt: now,
}};
const partialSnapshotHtml = renderMilestones(partialSnapshot, completeWithOutside, now, null, partialReading);
assert.match(partialSnapshotHtml, /Blocked issues outside open milestones \\(1\\)/);
assert.doesNotMatch(partialSnapshotHtml, /milestone membership unknown/);
assert.doesNotMatch(partialSnapshotHtml, /STALE/);
assert.match(partialSnapshotHtml, /data 30s \\(partial\\)/);
assert.match(partialSnapshotHtml, /broken: github unavailable/);
// The failed repo's blocker still has unknown membership; the snapshot expires on elapsed age only.
const brokenBlocker = JSON.parse(JSON.stringify(completeWithOutside));
brokenBlocker.issues.blocked.push({{ref: "fixture/broken#5", blocked_by: ["fixture/broken#4"], private_blockers: 0}});
const mixedHtml = renderMilestones(partialSnapshot, brokenBlocker, now, null, partialReading);
assert.match(mixedHtml, /Blocked issues outside open milestones \\(1\\)/);
assert.match(mixedHtml, /Blocked issues \\(milestone membership unknown: 1\\)/);
const expiredPartialHtml = renderMilestones(partialSnapshot, completeWithOutside, now + 391000, null, partialReading);
assert.match(expiredPartialHtml, /STALE/);
assert.match(expiredPartialHtml, /Blocked issues \\(milestone membership unknown: 1\\)/);
// Partial repo coverage makes the overdue and open chips unknown at 0, never "0 observed".
const partialNoneOverdue = JSON.parse(JSON.stringify(partialSnapshot));
partialNoneOverdue.repositories = [partialNoneOverdue.repositories[2]];
partialNoneOverdue.repositories.unshift({{
  repository: "fixture/machinery", private: false, status: "ok", stale: false, age_seconds: 30.0, milestones: [],
}});
const partialChipsOverdue = renderMilestoneChips({{milestones: partialNoneOverdue, planning: completeWithOutside}}, now);
assert.match(partialChipsOverdue, /<b>unknown<\\/b> overdue \\(partial\\)/);
assert.match(partialChipsOverdue, /<b>unknown<\\/b> open \\(partial\\)/);
assert.doesNotMatch(partialChipsOverdue, /<b>0<\\/b> (overdue|open)/);
"""
    _run_milestones_node(body)
