"""Behavioral regressions for Monitor loading and honest queue evidence (#7684)."""

from __future__ import annotations

import importlib.metadata
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from lu_machinery.api import work_router
from lu_machinery.api.main import app
from lu_machinery.common.repo_root import machinery_path



def _node(script: str) -> None:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def _machinery_js_prelude() -> str:
    js_path = machinery_path("api/dashboards/machinery.js")
    return f"const Machinery = require({json.dumps(str(js_path))});\nglobalThis.Machinery = Machinery;\n"


def _page_script(page: str, *, until: str = "\nrunner.start();") -> str:
    html = (machinery_path("api/dashboards") / page).read_text()
    marker = '<script src="/machinery.js"></script>'
    after = html.split(marker, 1)[1]
    inline = after.split("<script>", 1)[1].split("</script>", 1)[0]
    return inline.split(until, 1)[0]


@pytest.mark.parametrize("source_status", ["ok", "degraded", "unavailable"])
def test_next_empty_queue_preserves_source_completeness(monkeypatch, source_status):
    source = {
        "source_id": "public-monitor",
        "status": source_status,
        "sections": {"issues": {"status": "ok" if source_status == "ok" else "timeout", "count": 0}},
    }
    denominator = {
        "issues_open": 0,
        "omissions": [] if source_status == "ok" else [{"class": "issues", "reason": "timeout"}],
    }
    payload = {
        "items": [],
        "sources": [source, {"source_id": "private-local-adapter", "status": "unavailable"}],
        "denominator": denominator,
        "generated_at": "2026-09-04T00:00:00Z",
    }
    monkeypatch.setattr(work_router, "_known_streams", lambda _ctx: ["infra-harness"])
    monkeypatch.setattr(work_router, "cache_get_with_age", lambda *_args: (payload, 1.0))
    response = TestClient(app).get("/api/work/v1/next?stream=infra-harness")
    assert response.status_code == 200
    assert response.json()["queue"] == []
    assert response.json()["sources"] == [source]
    assert response.json()["denominator"] == denominator


def test_projection_direct_json_retains_opsec_middleware(monkeypatch):
    payload = {
        "items": [
            {
                "title": "/home/synthetic-private/data.json",
                "projections": {"stream": {"streams": ["infra-harness"]}},
            }
        ],
        "attention": [],
        "cache_age_s": 0,
    }
    monkeypatch.setattr(work_router, "cache_get_with_age", lambda *_args: (payload, 1.0))
    response = TestClient(app).get("/api/work/v1/projection?stream=infra-harness")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["title"] == "[redacted-path]"
    assert "/home/synthetic-private" not in response.text
    assert response.json()["cache_age_s"] == 1.0
    assert payload["items"][0]["title"] == "/home/synthetic-private/data.json"


_DOM = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {querySelectorAll() { return []; }, getElementById(id) {
  if (!elements.has(id)) elements.set(id, {textContent: 'old data', innerHTML: 'old data', className: '',
    setAttribute() {}, classList: {toggle() {}, add() {}, remove() {}}});
  return elements.get(id);
}};
const tick = () => new Promise(resolve => setImmediate(resolve));
"""


def test_home_panels_paint_independently_and_a_failed_panel_clears_old_data():
    _node(
        _DOM
        + _machinery_js_prelude()
        + r"""
const calls = [];
let loginPolls = 0;
const setTimeout = (fn, ms) => {if (ms === 3000) loginPolls++;};
globalThis.setTimeout = setTimeout;
let resolveSlow;
const slow = new Promise(resolve => {resolveSlow = resolve;});
const fakeFetch = async url => {
  calls.push(url);
  if (url === '/slow') return slow;
  if (url === '/broken') throw new Error('/broken: HTTP 503');
  if (url === '/login') return {data: {state: 'pending'}, age: null, snapshot: false};
  return {data: {tasks: []}, age: 900, bound: 150, snapshot: true};
};
const PANELS = [
  {id: 'fast', urls: ['/fast'], render: data => `fast ${data.tasks.length}`},
  {id: 'broken', urls: ['/broken'], render: () => 'never'},
  {id: 'slow', urls: ['/slow'], render: data => `slow ${data.marker}`},
  {id: 'logins', urls: ['/login'], render: data => data.state, retry: data => data.state === 'pending'},
];
const runner = Machinery.createPanelRunner({panels: PANELS, optionalUrls: new Set(), fetchJson: fakeFetch});
(async () => {
  const first = Promise.all(PANELS.map(runner.loadPanel));
  await tick();
  assert.equal(document.getElementById('fast-content').innerHTML, 'fast 0');
  assert.match(document.getElementById('fast-age').textContent, /^Fetched at \d{2}:\d{2}:\d{2} UTC · STALE · source age 900s · bound 150s$/);
  assert.equal(document.getElementById('fast-age').className, 'age bad');
  assert.equal(loginPolls, 1, 'pending login retries independently of another failed panel');
  const broken = document.getElementById('broken-content').innerHTML;
  assert.match(broken, /This panel couldn't load/);
  assert.match(broken, /\/broken: HTTP 503/);
  assert.doesNotMatch(broken.split('<details>')[0], /\/broken/);
  assert.doesNotMatch(broken, /old data/);
  assert.equal(document.getElementById('slow-content').innerHTML, 'old data');
  await runner.loadPanel(PANELS[2]);
  assert.equal(calls.filter(url => url === '/slow').length, 1, 'overlap must not duplicate requests');
  resolveSlow({data: {marker: 'new'}, age: null, snapshot: false});
  await first;
  assert.equal(document.getElementById('slow-content').innerHTML, 'slow new');
})().catch(e => {console.error(e); process.exitCode = 1;});
"""
    )
    fleet_script = _page_script("fleet.html", until="\nconst PANELS = [")
    _node(_DOM + _machinery_js_prelude() + fleet_script + r"""
const recent = renderRecent({tasks: [{task_id: 'finished', status: 'done'}],
  needs_attention: {total: 1530, tasks: [{task_id: 'unfinished', status: 'needs_finalize'},
    {task_id: 'crashed', status: 'zombie'}]}});
assert.match(recent, /1530 unfinished task records need a keep\/close decision/);
assert.match(recent, /Showing up to 2/);
assert.match(recent, /needs_finalize/);
assert.match(recent, /zombie/);
assert.match(recent, /finished/);
assert.doesNotMatch(renderWorkers({tasks: []}), /unfinished|zombie/);
""")


def test_home_hides_stale_subscription_quota_and_api_balances():
    """Stale or unverified readings must not render actionable quota, pace, reset or balance numbers."""
    subs_script = _page_script("subscriptions.html", until="\nconst PANELS = [")
    overview_script = _page_script("index.html", until="\nconst PANELS = [")
    _node(
        _DOM
        + _machinery_js_prelude()
        + "(() => {\n"
        + subs_script
        + "\nglobalThis.subscriptionRows = subscriptionRows; globalThis.renderUsage = renderUsage; globalThis.apiRows = apiRows;\n})();\n"
        + "(() => {\n"
        + overview_script
        + "\nglobalThis.renderAlerts = renderAlerts; globalThis.renderChips = renderChips;\n})();\n"
        + r"""
const row = {account: 'Fixture', reading: {status: 'fresh', age_s: 60, read_at: '2026-11-12T00:00:00Z'},
  weekly: {status: 'ok', left_pct: 70, resets_at: '2099-01-02T03:04:00Z'},
  pace: {status: 'ok', state: 'reserve', headroom_pct: 18, lasts_to_reset: true},
  session: {status: 'ok', left_pct: 61, resets_at: '2099-01-02T03:04:00Z'},
  extra_windows: [{label: 'Extra', status: 'ok', left_pct: 84}], breakdown: [{label: 'Used', value: '91 units'}],
  credits: {status: 'ok', balance: 37.2}, plan: {name: 'Fixture plan'},
  banked_resets: {status: 'ok', source: 'provider', count: 17,
    items: [{kind: 'reset', count: 17, expires_on: '2099-02-03'}]}};
const render = reading => subscriptionRows({stale_after_s: 900,
  providers: [{provider: 'fixture', accounts: [{...row, reading}]}]}, null).groups;
assert.match(render(row.reading), /70%/);
assert.match(render(row.reading), /17× reset/);
const chips = reading => renderChips({usage: {stale_after_s: 900,
  providers: [{accounts: [{...row, reading}]}]}});
assert.match(chips(row.reading), /<b>0<\/b> accounts run out before reset/);
for (const reading of [{...row.reading, age_s: 1800}, {...row.reading, status: 'stale'},
    {...row.reading, status: 'unknown'}, {...row.reading, age_s: null}]) {
  const html = render(reading);
  assert.equal(/17× reset/.test(html), false);
  assert.doesNotMatch(html, /70%|61%|84%|91 units|37\.2|2099-01-02|18 pp|lasts to reset/);
  assert.match(html, /plan · renewal unknown/);
  assert.match(chips(reading), /<b>0<\/b> accounts run out before reset, 1 unknown/);
  assert.doesNotMatch(chips(reading), /chip bad/);
}
const noReader = renderUsage({stale_after_s: 900, providers: [{provider: 'agy', label: 'AGY', accounts: [row]}], sources: {}},
  null, {state: 'ok', accounts: [{account: 'Claude a', logged_in: true}]});
assert.match(noReader, /no sign-in reader/);
assert.match(noReader, /Claude a, Claude b, Codex a and Codex b only/);
const configured = {...row, reading: {...row.reading, status: 'stale'},
  banked_resets: {...row.banked_resets, source: 'plan_facts'}};
assert.match(subscriptionRows({providers: [{accounts: [configured]}]}, null).groups, /17× reset/);
const apiAccountRow = {freshness: 'fresh', age_s: 60, total_balance: 42, currency: 'USD', usage_weekly_usd: 9};
const api = reading => apiRows({api_accounts: {Fixture: {...apiAccountRow, ...reading}},
  diagnostics: {stale_threshold_s: 900}}).flat().join('');
assert.match(api({}), /42\.00 USD balance/);
assert.match(api({}), /9\.00 this week/);
for (const reading of [{age_s: 1800}, {freshness: 'stale'}, {freshness: 'unknown'}, {age_s: null}])
  assert.doesNotMatch(api(reading), /42\.00|9\.00/);
const lastGood = api({freshness: 'stale_last_good', balance_probe_state: 'NEED_KEY'});
assert.match(lastGood, /42\.00 USD balance/);
assert.match(lastGood, /\$9\.00 this week \(last good\)/);
assert.match(lastGood, /Last good reading, 1m old/);
assert.match(lastGood, /needs an API key/);
assert.doesNotMatch(lastGood, /STALE_LAST_GOOD|NEED_KEY/);
assert.match(lastGood, /class="warn">Last good reading/);
const named = renderAlerts({status: 'ok', heartbeat: true, alerts: [
  {alertname: 'UnrecognizedAlert', severity: 'warning', starts_at: '2026-11-01T00:00:00Z', fingerprint: 'abc'},
  {alertname: 'ApprovedNotLanded30m', severity: 'warning', starts_at: '2026-11-12T00:00:00Z'},
  {alertname: 'ApprovedNotLanded30m', severity: 'warning', starts_at: '2026-11-11T00:00:00Z'},
  {alertname: 'UnrecognizedAlert', severity: 'warning', fingerprint: 'alpha'},
  {alertname: 'UnrecognizedAlert', severity: 'warning', fingerprint: 'beta'},
  {alertname: 'UnrecognizedAlert', severity: 'warning', alert_id: 17},
  {alertname: 'UnrecognizedAlert', severity: 'warning', fingerprint: 'alpha'}]});
assert.match(named, /Unrecognised alert \(fingerprint abc\)/);
assert.match(named, /ApprovedNotLanded30m/);
assert.doesNotMatch(named, /UnrecognizedAlert/);
const rowFor = marker => {
  const row = named.split("<tr>").find(part => part.includes(marker));
  assert.ok(row, marker);
  return row;
};
const countOf = row => row.match(/<td class="num">([^<]*)<\/td>/g).at(-1);
assert.equal((named.match(/fingerprint alpha/g) || []).length, 1);
assert.equal(countOf(rowFor('fingerprint alpha')), '<td class="num">2</td>');
assert.equal(countOf(rowFor('fingerprint beta')), '<td class="num">1</td>');
assert.equal(countOf(rowFor('alert id 17')), '<td class="num">1</td>');
assert.equal((named.match(/ApprovedNotLanded30m/g) || []).length, 1);
assert.equal(countOf(rowFor('ApprovedNotLanded30m')), '<td class="num">2</td>');
""")


def test_home_comms_collection_errors_are_unavailable_even_when_store_exists():
    """A collector failure must not appear as successful plane access just because the database exists."""
    fleet_script = _page_script("fleet.html", until="\nconst PANELS = [")
    _node(_DOM + _machinery_js_prelude() + fleet_script + r"""
const metrics = {source: 'authority', store: {reachable: true}, deliveries: {queued: 99}};
assert.match(renderComms(metrics), /authority reachable/);
for (const failure of [{db_error: 'DatabaseError'}, {db_missing: true}]) {
  const html = renderComms({...metrics, ...failure});
  assert.match(html, /unavailable/i);
  assert.doesNotMatch(html, /authority reachable|class="ok"|99/);
}
""")


@pytest.mark.parametrize("body", ["not-json", "null", "[]"])
def test_home_invalid_json_is_an_error(body):
    _node(
        _DOM
        + "globalThis.fetch = async () => ({ok: true, text: async () => "
        + json.dumps(body)
        + "});\n"
        + _machinery_js_prelude()
        + r"""
assert.rejects(Machinery.fetchJson('/fixture'), /Invalid/).catch(e => {console.error(e); process.exitCode = 1;});
"""
    )


def test_failing_panel_shows_a_calm_sentence_retry_and_hidden_detail():
    """Defect class: a failed panel shows a raw API path, omits Retry, or leaves its age blank."""
    _node(
        _DOM
        + _machinery_js_prelude()
        + r"""
const fakeFetch = async url => { throw new Error(`${url}: HTTP 503`); };
const PANELS = [{id: 'usage', label: 'Subscriptions', urls: ['/api/runtime/subscriptions'], render: () => 'never'}];
const runner = Machinery.createPanelRunner({panels: PANELS, optionalUrls: new Set(), fetchJson: fakeFetch});
(async () => {
  await runner.loadPanel(PANELS[0]);
  const html = document.getElementById('usage-content').innerHTML;
  const visible = html.split('<details>')[0];
  assert.match(visible, /This panel couldn't load: the source failed\./);
  assert.match(visible, /<button[^>]*>Retry<\/button>/);
  assert.doesNotMatch(visible, /\/api\/runtime\/subscriptions/);
  assert.match(html, /<details><summary>Technical detail<\/summary>/);
  assert.match(html, /\/api\/runtime\/subscriptions: HTTP 503/);
  const age = document.getElementById('usage-age');
  assert.match(age.textContent, /^Last attempt at \d{2}:\d{2}:\d{2} UTC · unavailable · source age unknown$/);
  assert.equal(age.className, 'age bad');
  assert.equal(document.getElementById('usage-status').textContent, "Subscriptions couldn't load.");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_refresh_header_names_a_failure_and_claims_updated_only_after_success():
    """Defect class: the header says Updated when a panel refresh failed, or hides the running lu-machinery release."""
    health = TestClient(app).get("/api/health")
    assert health.status_code == 200
    version = health.json()["machinery_version"]
    assert version == importlib.metadata.version("lu-machinery")
    served = TestClient(app).get("/machinery")
    assert served.status_code == 200
    assert f'id="machinery-version" data-version="{version}"' in served.text
    assert "__MACHINERY_VERSION__" not in served.text
    script = _page_script("index.html")
    _node(
        _DOM
        + f"const HEALTH = {json.dumps(health.text)};\nconst VERSION = {json.dumps(version)};\n"
        + r"""
globalThis.document = document;
document.getElementById('machinery-version').dataset = {version: VERSION};
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
let failSubscriptions = true;
globalThis.fetch = async (url) => {
  const refused = failSubscriptions && String(url).includes('/api/runtime/subscriptions');
  if (String(url) === '/api/health') throw new Error('the header must not request /api/health');
  const body = '{}';
  return {ok: !refused, status: refused ? 503 : 200, text: async () => body, headers: {get: name => ({'X-Snapshot-Age-S': '2', 'X-Snapshot-Stale-After-S': '60', 'X-Snapshot-State': 'fresh'}[name] ?? null)}};
};
"""
        + _machinery_js_prelude()
        + script
        + r"""
(async () => {
  await runner.refreshAll();
  assert.equal(document.getElementById('machinery-version').innerHTML,
    `<a href="https://github.com/learn-ukrainian/machinery/blob/main/CHANGELOG.md#v${VERSION}" title="What changed in this release">lu-machinery ${VERSION}</a>`);
  const status = document.getElementById('last-refresh');
  const retry = document.getElementById('refresh-retry');
  assert.match(status.textContent, /^Couldn't refresh, last attempt \d{2}:\d{2}:\d{2} UTC$/);
  assert.equal(retry.hidden, false);
  assert.doesNotMatch(status.textContent, /Updated/);
  assert.match(document.getElementById('usage-content').innerHTML, /couldn't load/i);
  assert.doesNotMatch(document.getElementById('alerts-content').innerHTML, /couldn't load/i);
  failSubscriptions = false;
  await runner.refreshAll();
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(retry.hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  assert.doesNotMatch(document.getElementById('usage-content').innerHTML, /couldn't load/i);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_home_announces_panel_status_and_has_skip_purpose_and_sites_label():
    """Defect class: a panel status change is silent, or the skip link, sites label, or purpose line is missing."""
    dashboards = machinery_path("api/dashboards")
    for name in ("index.html", "subscriptions.html", "fleet.html", "pull-requests.html", "milestones.html", "model-stats.html"):
        page = (dashboards / name).read_text()
        assert 'class="skip" href="#main"' in page, name
        assert '<main id="main" tabindex="-1">' in page, name
        assert 'aria-label="Sites"' in page, name
    assert (dashboards / "index.html").read_text().count('aria-live="polite"') == 3
    subs_page = (dashboards / "subscriptions.html").read_text()
    assert subs_page.count('aria-live="polite"') == 1
    assert "Which subscription is near its limit, when it resets, and whether anything is signed out." in subs_page
    assert (dashboards / "fleet.html").read_text().count('aria-live="polite"') == 3
    assert (dashboards / "pull-requests.html").read_text().count('aria-live="polite"') == 1
    assert (dashboards / "milestones.html").read_text().count('aria-live="polite"') == 1

    subs_script = _page_script("subscriptions.html")
    fleet_script = _page_script("fleet.html")
    _node(
        r"""
const assert = require('node:assert/strict');
const announced = [];
const elements = new Map();
const document = {querySelectorAll() { return []; }, getElementById(id) {
  if (!elements.has(id)) {
    let text = '';
    const el = {innerHTML: '', className: '', hidden: true, setAttribute() {}, classList: {toggle() {}, add() {}, remove() {}}};
    Object.defineProperty(el, 'textContent', {
      get() { return text; },
      set(next) { text = String(next); if (id.endsWith('-status')) announced.push(text); },
    });
    elements.set(id, el);
  }
  return elements.get(id);
}};
globalThis.document = document;
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
let failSubscriptions = true;
globalThis.fetch = async (url) => {
  const refused = failSubscriptions && String(url).includes('/api/runtime/subscriptions');
  return {ok: !refused, status: refused ? 403 : 200, text: async () => '{}', headers: {get: name => ({'X-Snapshot-Age-S': '2', 'X-Snapshot-Stale-After-S': '60', 'X-Snapshot-State': 'fresh'}[name] ?? null)}};
};
"""
        + _machinery_js_prelude()
        + "const subsRunner = (() => {\n" + subs_script + "\nreturn runner;\n})();\n"
        + "const fleetRunner = (() => {\n" + fleet_script + "\nreturn runner;\n})();\n"
        + r"""
(async () => {
  await subsRunner.refreshAll();
  await fleetRunner.refreshAll();
  assert.ok(announced.indexOf('Loading Subscriptions.') < announced.indexOf("Subscriptions isn't available in this view."));
  assert.equal(announced.filter(item => item === 'Agent fleet loaded.').length, 1);
  failSubscriptions = false;
  await subsRunner.refreshAll();
  await fleetRunner.refreshAll();
  assert.equal(announced.filter(item => item === 'Subscriptions loaded.').length, 1);
  await subsRunner.refreshAll();
  await fleetRunner.refreshAll();
  assert.equal(announced.filter(item => item === 'Subscriptions loaded.').length, 1);
  assert.equal(announced.filter(item => item === 'Agent fleet loaded.').length, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_refused_panels_stay_unavailable_and_do_not_fail_refresh():
    """Defect class: a panel refused in this view turns the page into Couldn't refresh, or shows Retry and a red age."""
    overview_script = _page_script("index.html")
    fleet_script = _page_script("fleet.html")
    _node(
        _DOM
        + r"""
globalThis.document = document;
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => ms === 1500 || ms === 3000 ? 0 : realSetTimeout(fn, ms, ...args);
const REFUSED = ['/api/delegate/active', '/api/delegate/recent', '/api/git/pull-requests'];
globalThis.fetch = async (url) => {
  const refused = REFUSED.some(item => String(url).includes(item));
  return {ok: !refused, status: refused ? 403 : 200, headers: {get: name => ({'X-Snapshot-Age-S': '2', 'X-Snapshot-Stale-After-S': '60', 'X-Snapshot-State': 'fresh'}[name] ?? null)}, text: async () => JSON.stringify({
    tasks: [], status: 'ok', alerts: [], heartbeat: true, providers: [], deliveries: {}, jobs: {},
    dead_letters: 0, store: {reachable: true}, repositories: []})};
};
"""
        + _machinery_js_prelude()
        + "const overviewRunner = (() => {\n" + overview_script + "\nreturn runner;\n})();\n"
        + "const fleetRunner = (() => {\n" + fleet_script + "\nreturn runner;\n})();\n"
        + r"""
(async () => {
  await overviewRunner.refreshAll();
  const status = document.getElementById('last-refresh');
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(document.getElementById('refresh-retry').hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  await fleetRunner.refreshAll();
  assert.match(status.textContent, /^Updated \d{2}:\d{2}:\d{2} UTC \u00b7 every 30 s$/);
  assert.equal(document.getElementById('refresh-retry').hidden, true);
  assert.doesNotMatch(status.textContent, /Couldn't refresh/);
  for (const id of ['workers', 'recent', 'prs']) {
    const html = document.getElementById(id + '-content').innerHTML;
    assert.match(html, /isn't available in this view/);
    assert.doesNotMatch(html, /Retry|couldn't load|class="error"/);
    const age = document.getElementById(id + '-age');
    assert.match(age.textContent, /^Last attempt at \d{2}:\d{2}:\d{2} UTC · unavailable · source age unknown$/);
    assert.equal(age.className, 'age');
  }
  assert.doesNotMatch(document.getElementById('alerts-content').innerHTML, /isn't available|couldn't load/);
  assert.match(document.getElementById('alerts-age').textContent, /Fetched at \d{2}:\d{2}:\d{2} UTC \u00b7 fresh \u00b7 source age \d+s \u00b7 bound 60s/);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )


def test_at_risk_chip_counts_fresh_run_outs_and_names_unknowns():
    """Defect class: one account with no reading replaces the run-out count with a question mark."""
    overview_script = _page_script("index.html", until="\nconst PANELS = [")
    _node(
        _DOM
        + _machinery_js_prelude()
        + overview_script
        + r"""
const fresh = {reading: {status: 'fresh', age_s: 30, read_at: '2026-11-13T00:00:00Z'},
  pace: {status: 'ok', lasts_to_reset: false}};
const unread = {reading: {status: 'unknown', age_s: null, reason_code: 'provider_error'}, pace: {status: 'unknown'}};
const safe = {reading: {status: 'fresh', age_s: 30, read_at: '2026-11-13T00:00:00Z'},
  pace: {status: 'ok', lasts_to_reset: true}};
const chipOf = (...accounts) => renderChips({usage: {stale_after_s: 900, providers: [{accounts}]}});
const mixed = chipOf(fresh, fresh, unread);
assert.match(mixed, /<b>2<\/b> accounts run out before reset, 1 unknown/);
assert.match(mixed, /class="chip bad"/);
assert.doesNotMatch(mixed, /\?/);
assert.match(chipOf(fresh), /<b>1<\/b> account runs out before reset/);
const none = chipOf(safe, unread);
assert.match(none, /<b>0<\/b> accounts run out before reset, 1 unknown/);
assert.match(none, /class="chip warn"/);
assert.doesNotMatch(none, /chip bad/);
const clear = chipOf(safe);
assert.match(clear, /<b>0<\/b> accounts run out before reset</);
assert.doesNotMatch(clear, /unknown|chip bad|chip warn/);
"""
    )


def test_panel_age_is_the_snapshot_header_plus_elapsed():
    """Defect class: client-side age from observed_at, or Number('') becoming 0, disagrees with the server snapshot age."""
    subs_script = _page_script("subscriptions.html", until="\nconst PANELS = [")
    _node(_DOM + _machinery_js_prelude() + subs_script + r"""
const {headingAge, panelAgeSeconds} = Machinery;
const now = Date.now();
const skewed = new Date(now + 120000).toISOString();
const line = (...readings) => headingAge(readings.map(reading => ({receivedAt: now, snapshot: true, ...reading})), false, null, undefined, now);
// A present header wins over a body age and a future observed_at.
assert.match(line({url: '/api/milestones', age: 40, bound: 420, state: 'fresh',
  data: {status: 'ok', age_seconds: 817, stale: true, observed_at: skewed}}).text, /· fresh · source age 40s · bound 420s$/);
assert.equal(panelAgeSeconds({ageHeader: '200', receivedAt: now - 11000, data: {observed_at: skewed}}, now), 211);
assert.equal(panelAgeSeconds({ageHeader: '30', receivedAt: now + 5000}, now), 30);
assert.equal(panelAgeSeconds({ageHeader: '30', receivedAt: now}, Number.NaN), null);
// Missing, empty, whitespace, and non-decimal headers are unknown, never 0.
for (const raw of [undefined, null, '', ' ', '\t', 'unknown', '-1', '1e2', '0x10', '+5']) {
  const reading = {ageHeader: raw, boundHeader: '210', state: 'fresh', receivedAt: now,
    data: {status: 'ok', observed_at: skewed, age_seconds: 12, stale: true}};
  assert.equal(panelAgeSeconds(reading, now), null, JSON.stringify(raw));
  assert.match(line(reading).text, /source age unknown/);
  assert.doesNotMatch(line(reading).text, /source age 0s/);
}
// A real zero stays fresh under a positive bound. Age past the bound is stale.
assert.match(line({ageHeader: '0', boundHeader: '60', state: 'fresh', receivedAt: now, data: {status: 'ok'}}).text,
  /· fresh · source age 0s · bound 60s$/);
assert.match(line({ageHeader: '0.000', boundHeader: '60.000', state: 'fresh', receivedAt: now, data: {status: 'ok'}}).text,
  /· fresh · source age 0s · bound 60s$/);
assert.match(line({age: 240, bound: 210, state: 'fresh', receivedAt: now, data: {status: 'ok', observed_at: skewed}}).text,
  /· STALE · source age 240s · bound 210s$/);
assert.equal(line({age: 240, bound: 210, state: 'fresh', receivedAt: now, data: {status: 'ok'}}).bad, true);
// Server state stale (partial coverage) keeps the heading stale at the header age, without observed_at.
assert.match(line({age: 30, bound: 210, state: 'stale', receivedAt: now, url: '/api/git/pull-requests',
  data: {status: 'ok', observed_at: skewed, repositories: [{status: 'ok'}, {status: 'unknown'}]}}).text,
  /· STALE · source age 30s · bound 210s$/);
assert.match(line({ageHeader: '30', boundHeader: ' 210 ', state: ' STALE ', receivedAt: now, url: '/api/git/pull-requests?limit=1',
  data: {status: 'ok', observed_at: skewed, repositories: [{status: 'ok'}]}}).text,
  /· STALE · source age 30s · bound 210s$/);
assert.match(line({ageHeader: '5', boundHeader: '210', state: ' Unavailable ', receivedAt: now, url: '/api/git/pull-requests',
  data: {status: 'ok', repositories: [{status: 'ok'}]}}).text, /· unavailable · source age 5s/);
// No header at all is unknown, including when the body carries its own age.
assert.match(line({url: '/api/delegate/active', data: {tasks: []}, age: null, bound: null, state: null}).text,
  /· freshness unknown · source age unknown · bound unknown$/);
assert.match(line({url: '/api/milestones', data: {status: 'ok', age_seconds: 12, stale: false, observed_at: skewed}}).text,
  /· freshness unknown · source age unknown/);
assert.match(line({data: null, age: null, snapshot: false}).text, /· unavailable · source age unknown/);
assert.match(line({url: '/api/ops/v1/alerts', age: 0, bound: 60, state: 'unavailable',
  data: {status: 'unknown', observed_at: skewed, stale_after_s: 60}}).text, /· unavailable · source age 0s/);
assert.match(line({url: '/api/delegate/active', data: {tasks: []}, age: 27, bound: 60, state: 'fresh'}).text,
  /· fresh · source age 27s · bound 60s$/);
// Account rows still show the server's age_s. That number is not the panel age.
const claudeRow = age => ({account: 'Claude a', reading: {status: 'fresh', age_s: age, read_at: '2030-01-01T00:00:00Z'}, plan: {status: 'ok'}});
const subs = {stale_after_s: 900, providers: [{provider: 'claude', accounts: [claudeRow(51)]}]};
assert.match(line({url: '/api/runtime/subscriptions', data: subs}).text, /source age unknown/);
assert.match(subscriptionRows(subs, null).groups, /fresh, 51s old/);
assert.match(subscriptionRows({stale_after_s: 900, providers: [{provider: 'claude', accounts: [claudeRow(1800)]}]}, null).groups, /STALE, 30m old/);
const partial = {stale_after_s: 900, providers: [{provider: 'claude', accounts: [claudeRow(51),
  {account: 'Claude b', reading: {status: 'unknown', age_s: null, reason_code: 'provider_error'}, plan: {status: 'ok'}}]}]};
assert.match(subscriptionRows(partial, null).groups, /NO READING/);
""")



def _swr_app(monkeypatch, tmp_path, handler_factory, *, ttl: float = 10.0, vary: tuple[str, ...] = ()):
    """A one-route app on ``state_helpers.swr_route`` with a fake cache clock."""
    from types import SimpleNamespace

    from fastapi import APIRouter, Depends, FastAPI

    from lu_machinery.api import state_helpers
    from lu_machinery.api.monitor_context import MonitorRuntime

    now = [1000.0]
    monkeypatch.setattr(state_helpers, "_ttl_clock", lambda: now[0])
    ctx = SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path), runtime=MonitorRuntime())
    router = APIRouter()
    router.get("/probe")(state_helpers.swr_route("probe", ttl, vary=vary)(handler_factory(Depends(lambda: ctx))))
    probe_app = FastAPI()
    probe_app.include_router(router)
    key_prefix = state_helpers.ctx_scoped_ttl_key(ctx, "swr", "probe", repr(ctx.roots))
    return TestClient(probe_app), now, key_prefix


def test_swr_route_serves_stale_value_with_true_age_and_one_refresh(monkeypatch, tmp_path):
    """Defect class: an expired value served without its age (or as fresh), a reader blocking on
    the recompute, or several stale reads starting more than one refresh."""
    import threading

    from lu_machinery.api import state_helpers

    calls: list[int] = []
    refreshing, release = threading.Event(), threading.Event()

    def factory(ctx_dependency):
        def handler(ctx=ctx_dependency):
            calls.append(len(calls) + 1)
            if len(calls) > 1:
                refreshing.set()
                assert release.wait(5)
            return {"n": len(calls)}

        return handler

    client, now, key = _swr_app(monkeypatch, tmp_path, factory)
    first = client.get("/probe")
    assert first.json() == {"n": 1}
    assert first.headers["X-Snapshot-Age-S"] == "0.000"
    assert first.headers["X-Snapshot-State"] == "fresh"

    now[0] += 25.0
    try:
        for _ in range(3):  # the refresh is held open: none of these may wait for it
            stale = client.get("/probe")
            assert stale.json() == {"n": 1}
            assert stale.headers["X-Snapshot-Age-S"] == "25.000"
            assert stale.headers["X-Snapshot-State"] == "stale"
        assert refreshing.wait(5)
    finally:
        release.set()
    # Join the one refresh flight; a second collection would fail this read.
    refreshed = state_helpers.cache_get_or_compute(key, 10.0, lambda: pytest.fail("second refresh"))
    assert refreshed == {"n": 2}
    assert calls == [1, 2]
    fresh = client.get("/probe")
    assert fresh.json() == {"n": 2}
    assert fresh.headers["X-Snapshot-Age-S"] == "0.000"


def test_swr_route_recomputes_a_dormant_value_instead_of_serving_it(monkeypatch, tmp_path):
    """Defect class: a value nobody read for longer than ttl + DEMAND_WINDOW_S handed to a
    one-shot caller (an hour-old board or task list) when a fresh computation fits the
    bounded dormant wait."""
    from lu_machinery.api import state_helpers

    calls: list[int] = []

    def factory(ctx_dependency):
        def handler(ctx=ctx_dependency):
            calls.append(1)
            return {"n": len(calls)}

        return handler

    client, now, _key = _swr_app(monkeypatch, tmp_path, factory)
    assert client.get("/probe").json() == {"n": 1}
    now[0] += 10.0 + state_helpers.DEMAND_WINDOW_S + 1.0
    dormant = client.get("/probe")
    assert dormant.json() == {"n": 2}
    assert dormant.headers["X-Snapshot-Age-S"] == "0.000"
    assert len(calls) == 2


def test_swr_route_caches_only_default_or_varied_parameters(monkeypatch, tmp_path):
    """Defect class: a filtered query answered from another filter's cache entry."""
    from fastapi import Query

    calls: list[tuple[str, str | None]] = []

    def factory(ctx_dependency):
        def handler(status: str = Query("all"), agent: str | None = Query(None), ctx=ctx_dependency):
            calls.append((status, agent))
            return {"status": status, "agent": agent, "n": len(calls)}

        return handler

    client, _now, _key = _swr_app(monkeypatch, tmp_path, factory, vary=("status",))
    assert client.get("/probe?status=done").json() == {"status": "done", "agent": None, "n": 1}
    assert client.get("/probe?status=failed").json() == {"status": "failed", "agent": None, "n": 2}
    cached = client.get("/probe?status=done")
    assert cached.json() == {"status": "done", "agent": None, "n": 1}
    assert "X-Snapshot-Age-S" in cached.headers
    # A parameter outside ``vary`` is never cached: every call runs the handler.
    for expected in (3, 4):
        direct = client.get("/probe?status=done&agent=codex")
        assert direct.json() == {"status": "done", "agent": "codex", "n": expected}
        assert "X-Snapshot-Age-S" not in direct.headers
    assert len(calls) == 4


def test_swr_route_cached_hit_ignores_saturated_default_executor(tmp_path):
    """Defect class: cached routes wait for a saturated executor or mix cache scopes."""
    import asyncio
    import concurrent.futures
    import threading
    from types import SimpleNamespace

    from fastapi import Response

    from lu_machinery.api.monitor_context import MonitorRuntime
    from lu_machinery.api.state_helpers import swr_route

    calls = []

    @swr_route("executor-saturation-probe", 60, vary=("choice",))
    def route(ctx, choice="a"):
        calls.append((ctx.roots.project_root, choice))
        return str(ctx.roots.project_root), choice

    async def exercise():
        loop = asyncio.get_running_loop()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        release = threading.Event()
        started = threading.Event()
        contexts = [
            SimpleNamespace(roots=SimpleNamespace(project_root=tmp_path / name), runtime=MonitorRuntime())
            for name in ("one", "two")
        ]
        combinations = [(contexts[0], "a"), (contexts[0], "b"), (contexts[1], "a")]
        blocked = None
        try:
            for ctx, choice in combinations:
                assert await route(ctx=ctx, choice=choice, _swr_response=Response()) == (
                    str(ctx.roots.project_root), choice
                )

            def block_worker():
                started.set()
                release.wait(timeout=5)

            blocked = loop.run_in_executor(None, block_worker)
            assert started.wait(1)
            for ctx, choice in combinations:
                assert await asyncio.wait_for(
                    route(ctx=ctx, choice=choice, _swr_response=Response()), timeout=0.5
                ) == (str(ctx.roots.project_root), choice)
            assert len(calls) == len(combinations)
        finally:
            release.set()
            if blocked is not None:
                await blocked
            executor.shutdown(wait=True)

    asyncio.run(exercise())
