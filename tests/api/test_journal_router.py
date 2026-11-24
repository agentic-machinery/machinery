"""Tests for GET /api/journal and the /journal dashboard page (JOURNAL-1124)."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api import journal_router
from lu_machinery.api.route_contracts import contract_for_page, contract_for_route
from lu_machinery.orchestration.fleet_repos import FleetRepo

_FIXTURE_REPOS = {
    "public": FleetRepo("public", "fixture/public", "learn-ukrainian", "public-monorepo", default=True),
    "machinery": FleetRepo("machinery", "fixture/machinery", "machinery", "public-machinery", visibility="public"),
    "infra-private": FleetRepo(
        "infra-private",
        "fixture/infra-private",
        "learn-ukrainian-infra-private",
        "private-infra",
        visibility="private",
    ),
}


def _sample_entry(
    entry_id: int,
    *,
    occurred_at: str = "2026-11-24T12:00:00Z",
    recorded_at: str = "2026-11-24T12:00:05Z",
    project: str = "machinery",
    entry_type: str = "landed",
    headline: str = "Shipped project journal API",
    body: str = "Added GET /api/journal and /journal page.\nPlain text only.",
    links: list[dict[str, str]] | None = None,
    author_seat: str = "claude-cto",
    source_key: str | None = None,
    source_item_id: str | None = None,
    corrects_id: int | None = None,
) -> dict[str, Any]:
    return {
        "id": entry_id,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "project": project,
        "type": entry_type,
        "headline": headline,
        "body": body,
        "links": links if links is not None else [{"label": "PR #1", "url": "https://github.com/fixture/machinery/pull/1"}],
        "author_seat": author_seat,
        "source_key": source_key,
        "source_item_id": source_item_id,
        "corrects_id": corrects_id,
    }


def _make_fake_cli(
    tmp_path: Path,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    argv_log: Path | None = None,
    name: str = "fake-lu-driver-state",
) -> Path:
    """Create a test-owned executable fake driver-state CLI (Design Charter Rule 2)."""
    script = tmp_path / name
    log_snippet = (
        f"Path({str(argv_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        if argv_log is not None
        else ""
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"{log_snippet}"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({returncode})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _build_client(*, client: tuple[str, int] = ("127.0.0.1", 50000), base_url: str = "http://127.0.0.1:8765") -> TestClient:
    app = FastAPI()
    app.include_router(journal_router.router, prefix="/api/journal")
    return TestClient(app, base_url=base_url, client=client)


def test_journal_route_and_page_contracts_registered() -> None:
    """Defect class: /api/journal or /journal missing from machine-readable route/page contracts."""
    route = contract_for_route("/api/journal", "http")
    assert route is not None
    assert route.pattern == "/api/journal"
    assert route.match == "exact"
    assert route.mutates is False
    assert route.response_schema_version == "journal.v1"

    page = contract_for_page("journal.html")
    assert page is not None
    assert page.url == "/journal"
    assert page.nav_label == "Journal"


def test_journal_available_with_entries_and_ordering_and_paging_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: CLI args dropped, ordering altered, or response envelope missing fields."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    argv_log = tmp_path / "argv.json"
    entries = [
        _sample_entry(
            42,
            occurred_at="2026-11-24T12:10:00Z",
            project="machinery",
            entry_type="decision",
            headline="Freeze journal.v1 contract",
            source_item_id="C-500",
        ),
        _sample_entry(
            41,
            occurred_at="2026-11-24T11:00:00Z",
            project="infra-private",
            entry_type="correction",
            headline="Clarify retention",
            corrects_id=39,
        ),
    ]
    cli_payload = {
        "schema": "journal.v1",
        "total": 5,
        "latest_id": 42,
        "entries": entries,
    }
    cli = _make_fake_cli(tmp_path, stdout=json.dumps(cli_payload), argv_log=argv_log)
    monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(cli))

    with _build_client() as client:
        response = client.get(
            "/api/journal",
            params={
                "project": "machinery",
                "type": "decision",
                "since": "2026-11-20T00:00:00+02:00",
                "after_id": "10",
                "limit": "2",
                "offset": "1",
            },
            headers={"Origin": "http://localhost:8765"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-snapshot-state"] == "fresh"
    body = response.json()
    assert body["response_schema_version"] == "journal.v1"
    assert body["availability"] == "available"
    assert body["total"] == 5
    assert body["latest_id"] == 42
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert body["next_offset"] == 3
    assert body["filters"] == {
        "project": "machinery",
        "type": "decision",
        "since": "2026-11-19T22:00:00Z",
        "after_id": 10,
    }
    assert body["projects"] == [
        {"key": "fleet", "label": "Fleet", "private": False},
        {"key": "public", "label": "learn-ukrainian", "private": False},
        {"key": "machinery", "label": "machinery", "private": False},
        {"key": "infra-private", "label": "learn-ukrainian-infra-private", "private": True},
    ]
    # Ordering preserved from CLI pass-through.
    assert [row["id"] for row in body["entries"]] == [42, 41]
    assert body["entries"][0]["source_item_id"] == "C-500"
    assert body["entries"][1]["corrects_id"] == 39

    recorded_argv = json.loads(argv_log.read_text(encoding="utf-8"))
    assert recorded_argv == [
        "journal",
        "list",
        "--json",
        "--limit",
        "2",
        "--offset",
        "1",
        "--project",
        "machinery",
        "--type",
        "decision",
        "--since",
        "2026-11-19T22:00:00Z",
        "--after-id",
        "10",
    ]


def test_journal_empty_available_and_since_all_omits_since_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: empty available result misclassified as unavailable or since=all passes --since."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    argv_log = tmp_path / "argv.json"
    cli_payload = {"schema": "journal.v1", "total": 0, "latest_id": None, "entries": []}
    cli = _make_fake_cli(tmp_path, stdout=json.dumps(cli_payload), argv_log=argv_log)
    monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(cli))

    with _build_client() as client:
        response = client.get("/api/journal", params={"since": "all"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["availability"] == "available"
    assert body["total"] == 0
    assert body["latest_id"] is None
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["next_offset"] is None
    assert body["filters"] == {"project": None, "type": None, "since": "all", "after_id": None}
    assert body["entries"] == []

    recorded_argv = json.loads(argv_log.read_text(encoding="utf-8"))
    assert "--since" not in recorded_argv
    assert recorded_argv == ["journal", "list", "--json", "--limit", "50", "--offset", "0"]


@pytest.mark.parametrize(
    "env_mode",
    ["unset", "empty", "missing_file", "non_executable", "exit_1"],
)
def test_journal_cli_unavailable_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_mode: str
) -> None:
    """Defect class: missing/unconfigured/broken CLI reported as empty success instead of cli_unavailable."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    if env_mode == "unset":
        monkeypatch.delenv("LU_DRIVER_STATE_CLI", raising=False)
    elif env_mode == "empty":
        monkeypatch.setenv("LU_DRIVER_STATE_CLI", "")
    elif env_mode == "missing_file":
        monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(tmp_path / "does-not-exist"))
    elif env_mode == "non_executable":
        non_exec = tmp_path / "not-exec"
        non_exec.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        non_exec.chmod(0o600)
        monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(non_exec))
    elif env_mode == "exit_1":
        cli = _make_fake_cli(tmp_path, stderr="boom", returncode=1)
        monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(cli))

    with _build_client() as client:
        response = client.get("/api/journal")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-snapshot-state"] == "unavailable"
    body = response.json()
    assert body["response_schema_version"] == "journal.v1"
    assert body["availability"] == "cli_unavailable"
    assert body["total"] is None
    assert body["latest_id"] is None
    assert body["next_offset"] is None
    assert body["entries"] == []


def test_journal_db_unavailable_exit_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defect class: CLI exit 3 (database unavailable) not mapped to db_unavailable with total=null."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    cli = _make_fake_cli(tmp_path, stderr="postgres connection refused", returncode=3)
    monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(cli))

    with _build_client() as client:
        response = client.get("/api/journal")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-snapshot-state"] == "unavailable"
    body = response.json()
    assert body["availability"] == "db_unavailable"
    assert body["total"] is None
    assert body["latest_id"] is None
    assert body["next_offset"] is None
    assert body["entries"] == []


@pytest.mark.parametrize(
    "bad_stdout",
    [
        "",
        "not json at all",
        json.dumps({"schema": "journal.v1", "total": 0, "latest_id": None, "entries": []})
        + "\n"
        + json.dumps({"schema": "journal.v1", "total": 0, "latest_id": None, "entries": []}),
        json.dumps({"schema": "journal.v1", "total": 0, "latest_id": None, "entries": []}) + "{}",
        json.dumps({"schema": "journal.v2", "total": 0, "latest_id": None, "entries": []}),
        json.dumps([1, 2, 3]),
        json.dumps({"schema": "journal.v1", "total": True, "latest_id": None, "entries": []}),
        json.dumps({"schema": "journal.v1", "total": -1, "latest_id": None, "entries": []}),
        json.dumps({"schema": "journal.v1", "total": 0, "latest_id": None, "entries": [_sample_entry(1)]}),
        json.dumps({"schema": "journal.v1", "total": 1, "latest_id": None, "entries": [_sample_entry(1)]}),
        json.dumps(
            {
                "schema": "journal.v1",
                "total": 1,
                "latest_id": 1,
                "entries": [_sample_entry(1, entry_type="unknown-type")],
            }
        ),
        json.dumps(
            {
                "schema": "journal.v1",
                "total": 1,
                "latest_id": 1,
                "entries": [_sample_entry(1, links=[{"label": "xss", "url": "javascript:alert(1)"}])],
            }
        ),
        '{"schema":"journal.v1","total":NaN,"latest_id":null,"entries":[]}',
        '{"schema":"journal.v1","total":1,"latest_id":1,"entries":[{"id":1,"occurred_at":"2026-11-24T12:00:00Z","recorded_at":"2026-11-24T12:00:05Z","project":"machinery","type":"note","headline":"ok","body":"bad \\ud800 surrogate","links":[],"author_seat":"claude-cto","source_key":null,"source_item_id":null,"corrects_id":null}]}',
        '{"schema":"journal.v1","total":1,"latest_id":1,"entries":[{"id":1,"occurred_at":"2026-11-24T12:00:00Z","recorded_at":"2026-11-24T12:00:05Z","project":"machinery","type":"note","headline":"bad \\udfff","body":"ok","links":[],"author_seat":"claude-cto","source_key":null,"source_item_id":null,"corrects_id":null}]}',
    ],
)
def test_journal_invalid_payload_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_stdout: str
) -> None:
    """Defect class: non-JSON, concatenated JSON docs, or wrong schema accepted instead of invalid_payload."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    cli = _make_fake_cli(tmp_path, stdout=bad_stdout, returncode=0)
    monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(cli))

    with _build_client() as client:
        response = client.get("/api/journal")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-snapshot-state"] == "unavailable"
    body = response.json()
    assert body["availability"] == "invalid_payload"
    assert body["total"] is None
    assert body["latest_id"] is None
    assert body["next_offset"] is None
    assert body["entries"] == []


@pytest.mark.parametrize(
    "query_string",
    [
        "project=",
        "project=unknown-repo",
        "project=Fleet",
        "project=machinery&project=fleet",
        "type=",
        "type=merge",
        "type=LANDED",
        "type=note&type=decision",
        "since=",
        "since=90d",
        "since=2026-11-24",
        "since=2026-11-24T12:00:00",
        "since=2026-02-30T12:00:00Z",
        "since=2026-11-24T12:00:00+25:00",
        "since=0001-01-01T00:00:00+01:00",
        "since=9999-12-31T23:59:59-01:00",
        "since=7d&since=24h",
        "after_id=",
        "after_id=-1",
        "after_id=abc",
        "after_id=1.5",
        "after_id=9999999999999999999999",
        "after_id=" + "9" * 4301,
        "limit=",
        "limit=0",
        "limit=-5",
        "limit=101",
        "limit=abc",
        "limit=" + "9" * 4301,
        "limit=10&limit=20",
        "offset=",
        "offset=-1",
        "offset=abc",
        "offset=3000000000",
        "offset=" + "9" * 4301,
    ],
)
def test_journal_invalid_params_return_422_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query_string: str
) -> None:
    """Defect class: invalid query parameters reach subprocess or return 200 instead of 422."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    monkeypatch.delenv("LU_DRIVER_STATE_CLI", raising=False)

    with _build_client() as client:
        response = client.get(f"/api/journal?{query_string}")

    assert response.status_code == 422, (query_string, response.text)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("peer", "base_url", "headers"),
    [
        (("192.0.2.10", 50000), "http://127.0.0.1:8765", {}),
        (("testclient", 50000), "http://127.0.0.1:8765", {}),
        (("2001:db8::1", 50000), "http://127.0.0.1:8765", {}),
        (("127.0.0.1", 50000), "http://evil.example:8765", {}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Host": "127.0.0.1:99999"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Host": "bad host"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Host": "127.0.0.1:0"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Host": "[::1%lo]:8765"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://[::1%lo]:8765"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://[::1%25lo]:8765"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "https://evil.example"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "null"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": ""}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://localhost.evil.example"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://127.0.0.1:8765/path"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://127.0.0.1:8765?"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://127.0.0.1:8765#"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://user@localhost:8765"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "ftp://localhost:8765"}),
        (("127.0.0.1", 50000), "http://127.0.0.1:8765", {"Origin": "http://127.0.0.1:"}),
    ],
)
def test_journal_non_loopback_peer_or_origin_returns_403_no_store(
    monkeypatch: pytest.MonkeyPatch,
    peer: tuple[str, int],
    base_url: str,
    headers: dict[str, str],
) -> None:
    """Defect class: non-loopback peer, DNS-rebinding Host, or non-loopback Origin accesses journal."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    monkeypatch.delenv("LU_DRIVER_STATE_CLI", raising=False)

    with _build_client(client=peer, base_url=base_url) as client:
        # Even with an invalid param, access control must fail closed with 403 first.
        response = client.get("/api/journal?limit=999", headers=headers)

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


def test_journal_missing_or_duplicate_host_or_origin_headers_return_403() -> None:
    """Defect class: missing/duplicate Host or Origin headers bypass loopback authority guard."""
    from starlette.requests import Request

    def make_request(headers: list[tuple[bytes, bytes]]) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/api/journal",
                "query_string": b"",
                "headers": headers,
                "server": ("127.0.0.1", 8765),
                "client": ("127.0.0.1", 50000),
            }
        )

    assert not journal_router._request_is_allowed(make_request([]))
    assert not journal_router._request_is_allowed(
        make_request([(b"host", b"127.0.0.1:8765"), (b"host", b"127.0.0.1:8765")])
    )
    assert not journal_router._request_is_allowed(
        make_request(
            [
                (b"host", b"127.0.0.1:8765"),
                (b"origin", b"http://127.0.0.1:8765"),
                (b"origin", b"https://evil.example"),
            ]
        )
    )
    assert journal_router._request_is_allowed(
        make_request([(b"host", b"127.0.0.1:8765"), (b"origin", b"http://localhost:8765")])
    )


def test_journal_loopback_ipv6_and_loopback_origins_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect class: valid loopback IPv6 peer or loopback Origin falsely refused."""
    monkeypatch.setattr(journal_router, "load_fleet_repos", lambda: _FIXTURE_REPOS)
    cli = _make_fake_cli(
        tmp_path,
        stdout=json.dumps({"schema": "journal.v1", "total": 0, "latest_id": None, "entries": []}),
    )
    monkeypatch.setenv("LU_DRIVER_STATE_CLI", str(cli))

    with _build_client(client=("::1", 50000), base_url="http://[::1]:8765") as client:
        for origin in ("http://[::1]:8765", "http://127.0.0.1:8765", "http://localhost:8765"):
            response = client.get("/api/journal", headers={"Origin": origin})
            assert response.status_code == 200, origin
            assert response.headers["cache-control"] == "no-store"


def test_journal_page_renders_plain_text_markers_and_unavailable_states() -> None:
    """Defect class: journal.html renders HTML from body, auto-moves last-seen, or shows 'no entries' when down."""
    dashboards = Path(journal_router.__file__).with_name("dashboards")
    page = (dashboards / "journal.html").read_text(encoding="utf-8")
    marker = '<script src="/machinery.js"></script>'
    after = page.split(marker, 1)[1]
    script = after.split("<script>", 1)[1].split("bindJournalControls();", 1)[0]

    payload = {
        "response_schema_version": "journal.v1",
        "availability": "available",
        "observed_at": "2026-11-24T12:30:00Z",
        "total": 3,
        "latest_id": 12,
        "limit": 50,
        "offset": 0,
        "next_offset": None,
        "filters": {"project": None, "type": None, "since": "7d", "after_id": None},
        "projects": [
            {"key": "fleet", "label": "Fleet", "private": False},
            {"key": "machinery", "label": "machinery", "private": False},
            {"key": "infra-private", "label": "learn-ukrainian-infra-private", "private": True},
        ],
        "entries": [
            _sample_entry(
                12,
                occurred_at="2026-11-24T12:20:00Z",
                project="machinery",
                entry_type="decision",
                headline="Adopt <script>alert(1)</script> headline",
                body="Line 1 <b>not html</b>\nLine 2 <img src=x onerror=alert(1)>",
                links=[
                    {"label": "Safe PR", "url": "https://github.com/fixture/machinery/pull/9"},
                    {"label": "Bad link", "url": "javascript:alert(1)"},
                ],
                author_seat="claude-cto",
            ),
            _sample_entry(
                11,
                occurred_at="2026-11-24T11:30:00Z",
                project="infra-private",
                entry_type="incident",
                headline="Pool quota paused",
                body="Paused Codex b until reset.",
                links=[],
                author_seat="claude-cto",
            ),
            _sample_entry(
                10,
                occurred_at="2026-11-24T09:00:00Z",
                project="fleet",
                entry_type="landed",
                headline="Previous cycle work",
                body="Earlier entry below divider.",
                links=[],
                author_seat="claude-cto",
            ),
        ],
    }

    node_code = (
        "const assert = require('node:assert/strict');\n"
        "const store = new Map();\n"
        "global.localStorage = {\n"
        "  getItem: k => store.has(k) ? store.get(k) : null,\n"
        "  setItem: (k, v) => { store.set(k, String(v)); },\n"
        "  removeItem: k => { store.delete(k); },\n"
        "};\n"
        "global.location = {search: '', pathname: '/journal'};\n"
        "global.document = {getElementById: () => null};\n"
        f"global.Machinery = require({json.dumps(str(dashboards / 'machinery.js'))});\n"
        + script
        + f"\nconst payload = {json.dumps(payload)};\n"
        r"""
// First visit: no last-seen key -> no divider, no is-new highlight, no "N new" chip.
const firstHtml = renderJournal(payload);
assert.doesNotMatch(firstHtml, /journal-divider/);
assert.doesNotMatch(firstHtml, /is-new/);
assert.doesNotMatch(renderChips({journal: payload}), /new/);
// Plain text body and escaped headline; unsafe javascript: link never rendered as <a>.
assert.match(firstHtml, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
assert.match(firstHtml, /&lt;b&gt;not html&lt;\/b&gt;/);
assert.doesNotMatch(firstHtml, /<script>alert/);
assert.doesNotMatch(firstHtml, /javascript:alert/);
assert.match(firstHtml, /href="https:\/\/github\.com\/fixture\/machinery\/pull\/9"/);
// Auto-refresh (re-rendering) never writes localStorage.
assert.equal(localStorage.getItem('machinery-journal-last-seen-id'), null);

// Seeded returning visit: lastSeenId = 10 -> 12 and 11 are new above Earlier divider; chip says "2 new".
localStorage.setItem('machinery-journal-last-seen-id', '10');
const returningHtml = renderJournal(payload);
assert.match(returningHtml, /data-entry-id="12"[\s\S]*journal-divider[\s\S]*Earlier[\s\S]*data-entry-id="10"/);
assert.equal((returningHtml.match(/class="journal-entry is-new"/g) || []).length, 2);
const returningChips = renderChips({journal: payload});
assert.match(returningChips, /<b>2<\/b> new/);
// Re-rendering still never moves localStorage.
assert.equal(localStorage.getItem('machinery-journal-last-seen-id'), '10');

// Mark caught up moves localStorage to 12 and clears markers & chip.
writeLastSeenId(highestEntryId(payload));
assert.equal(localStorage.getItem('machinery-journal-last-seen-id'), '12');
const caughtUpHtml = renderJournal(payload);
assert.doesNotMatch(caughtUpHtml, /journal-divider/);
assert.doesNotMatch(caughtUpHtml, /is-new/);
assert.doesNotMatch(renderChips({journal: payload}), /new/);

// Unavailable states show clear messages, never "No journal entries".
for (const state of ['cli_unavailable', 'db_unavailable', 'invalid_payload']) {
  const downHtml = renderJournal({...payload, availability: state, total: null, latest_id: null, entries: []});
  assert.match(downHtml, /Journal unavailable:/);
  assert.doesNotMatch(downHtml, /No journal entries/i);
}

// F2 regression: (a) filter change during an in-flight refresh aborts/supersedes the stale fetch;
// (b) Clear filters / same-URL reload during an in-flight fetch keeps it alive without erroring.
(async () => {
  const calls = [];
  let releaseSameUrl;
  global.fetch = (url, opts) => {
    calls.push(url);
    if (calls.length === 1) {
      return new Promise((resolve, reject) => {
        if (opts && opts.signal) {
          opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
        }
      });
    }
    if (calls.length === 2) {
      return Promise.resolve({
        ok: true,
        status: 200,
        text: async () => JSON.stringify({...payload, filters: {project: null, type: 'decision', since: '7d', after_id: null}}),
        headers: {get: () => null},
      });
    }
    return new Promise((resolve, reject) => {
      if (opts && opts.signal) {
        opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
      }
      releaseSameUrl = () => resolve({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(payload),
        headers: {get: () => null},
      });
    });
  };
  global.location = {search: '', pathname: '/journal'};
  const firstLoad = runner.loadPanel(PANELS[0]);
  assert.deepEqual(calls, ['/api/journal']);
  assert.equal(activeRequestUrl, '/api/journal');
  // (a) Changing URL during in-flight fetch aborts and supersedes.
  global.location.search = '?type=decision';
  reloadForCurrentUrl();
  await firstLoad;
  assert.deepEqual(calls, ['/api/journal', '/api/journal?type=decision']);
  assert.equal(PANELS[0].ok, true);
  assert.equal(PANELS[0].readings[0].url, '/api/journal?type=decision');
  assert.equal(runner.latest.journal.filters.type, 'decision');

  // (b) Same-URL reload (e.g. Clear filters while unfiltered load is pending) must NOT abort.
  global.location.search = '';
  const sameUrlLoad = runner.loadPanel(PANELS[0]);
  assert.equal(calls.length, 3);
  assert.equal(activeRequestUrl, '/api/journal');
  reloadForCurrentUrl();
  assert.equal(activeController.signal.aborted, false);
  releaseSameUrl();
  await sameUrlLoad;
  assert.equal(PANELS[0].ok, true);
  assert.equal(calls.length, 3);
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    )
    subprocess.run(["node", "-e", node_code], check=True, timeout=15)
