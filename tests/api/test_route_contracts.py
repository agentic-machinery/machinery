"""Fleet-comms observer routes keep their observer contract."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from lu_machinery.api.route_contracts import contract_for_route


@pytest.mark.parametrize("path", ["/api/fleet", "/api/fleet/requests", "/api/fleet/v10/now"])
def test_fleet_comms_routes_keep_observer_contract(path: str) -> None:
    contract = contract_for_route(path)

    assert contract is not None
    assert contract.pattern == "/api/fleet"
    assert contract.locality == "cluster_authoritative"
    assert contract.response_schema_version == "comms.v2"


def test_product_home_replaces_machinery_index_and_contract(tmp_path, monkeypatch):
    """Defect class: a packaged fallback shadows a declared product home and duplicates its contract."""
    from lu_machinery.api import main, route_contracts
    from lu_machinery.api.product_contribution import EMPTY, ProductContribution

    monkeypatch.setattr(main.config, "PRODUCT_ROOT", tmp_path)
    product_index = tmp_path / "product.html"
    product_index.write_text("product home")
    machinery_index = tmp_path / "index.html"
    machinery_index.write_text("machinery home")
    page = replace(route_contracts.PAGE_CONTRACTS[0], url="/", purpose="Product home")
    product = ProductContribution(static_files={"index.html": product_index}, page_contracts=(page,))
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=product))})
    context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=tmp_path))
    for path in ("", "index.html"):
        response = main.serve_static(path, request, context)
        assert response.path == product_index
    contracts = [c for c in route_contracts.all_page_contracts(product) if c.file == "index.html"]
    # The product home replaces nothing at /machinery; a product contract for the same URL would.
    assert contracts == [route_contracts.PAGE_CONTRACTS[0], page]
    same_url = replace(page, url="/machinery")
    assert [c for c in route_contracts.all_page_contracts(
        ProductContribution(static_files={}, page_contracts=(same_url,))) if c.file == "index.html"] == [same_url]
    request.app.state.product = EMPTY
    assert main.serve_static("", request, context).body == b"machinery home"


def test_machinery_path_serves_machinery_home_when_product_owns_root(tmp_path, monkeypatch):
    """Defect class: a product that declares its own home at ``/`` hides the machinery page."""
    from lu_machinery.api import main
    from lu_machinery.api.product_contribution import ProductContribution

    monkeypatch.setattr(main.config, "PRODUCT_ROOT", tmp_path)
    product_index = tmp_path / "product.html"
    product_index.write_text("product home")
    machinery_index = tmp_path / "index.html"
    machinery_index.write_text("machinery home")
    product = ProductContribution(static_files={"index.html": product_index})
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=product))})
    context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=tmp_path))
    assert main.serve_static("", request, context).path == product_index
    for path in ("machinery", "machinery/"):
        assert main.serve_static(path, request, context).body == b"machinery home"


def test_machinery_split_pages_contracts_shared_assets_and_scoped_reads():
    """Defect class: a split dashboard page misses its contract, shared nav/assets, or fetches another area's API."""
    import json
    import re
    import subprocess

    from lu_machinery.api import main, route_contracts
    from lu_machinery.api.product_contribution import EMPTY
    from lu_machinery.common.repo_root import machinery_path

    dashboards = machinery_path("api/dashboards")
    html_files = sorted(path.name for path in dashboards.glob("*.html"))
    contract_files = sorted(page.file for page in route_contracts.PAGE_CONTRACTS)
    assert html_files == contract_files
    assert route_contracts.PAGE_CONTRACTS[0].file == "index.html"
    assert route_contracts.PAGE_CONTRACTS[0].url == "/machinery"

    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=EMPTY))})
    context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=dashboards))

    # Every contracted page and shared asset serves directly from dashboards_dir.
    for asset in ("machinery.css", "machinery.js"):
        response = main.serve_static(asset, request, context)
        assert response.path == dashboards / asset
    for page in route_contracts.PAGE_CONTRACTS:
        slug = page.url.lstrip("/")
        response = main.serve_static(slug, request, context)
        if page.file == "index.html":
            assert b"__MACHINERY_VERSION__" not in response.body
            assert b"/machinery.css" in response.body
            assert b"/machinery.js" in response.body
            root_response = main.serve_static("", request, context)
            assert b"/machinery.css" in root_response.body
        else:
            assert response.path == dashboards / page.file
            html_response = main.serve_static(page.file, request, context)
            assert html_response.path == dashboards / page.file

    # Every page loads the shared stylesheet, script, landmarks, and only its own API endpoints.
    expected_apis = {
        "index.html": {
            "/api/ops/v1/alerts",
            "/api/runtime/subscriptions",
            "/api/runtime/logins",
            "/api/git/pull-requests",
        },
        "subscriptions.html": {
            "/api/runtime/subscriptions",
            "/api/state/routing-budget",
            "/api/runtime/logins",
        },
        "fleet.html": {
            "/api/delegate/active",
            "/api/delegate/recent",
            "/api/comms/v1/metrics",
            "/api/git/pull-requests",
        },
        "pull-requests.html": {"/api/git/pull-requests"},
        "milestones.html": {"/api/milestones"},
        "model-stats.html": {"/api/fleet/model-stats", "/api/state/routing-budget"},
    }
    assert set(expected_apis) == set(html_files)
    for filename, allowed in expected_apis.items():
        text = (dashboards / filename).read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/machinery.css">' in text, filename
        assert '<script src="/machinery.js"></script>' in text, filename
        assert 'class="skip" href="#main"' in text, filename
        assert '<main id="main" tabindex="-1">' in text, filename
        assert 'id="machinery-nav"' in text, filename
        assert 'id="machinery-sites"' in text, filename
        assert 'id="theme-toggle"' in text, filename
        found_apis = set(re.findall(r"/api/[a-z0-9/_-]+", text))
        assert found_apis == allowed, (filename, found_apis, allowed)

    # Shared navigation covers every contracted page, marks active page, handles storage errors, and rejects unsafe URLs.
    urls = [page.url for page in route_contracts.PAGE_CONTRACTS]
    node_script = r"""
const assert = require('node:assert/strict');
const M = require(process.argv[1]);
const urls = JSON.parse(process.argv[2]);
assert.deepEqual(M.NAV_PAGES.map(p => p.href), urls);
for (const path of ['/', '/index.html', '/machinery', '/machinery/']) {
  const html = M.renderNavHtml(path);
  assert.match(html, /href="\/machinery" aria-current="page">Overview</, path);
  assert.equal((html.match(/aria-current="page"/g) || []).length, 1, path);
}
for (const url of urls.filter(u => u !== '/machinery')) {
  const html = M.renderNavHtml(url);
  assert.ok(html.includes(`href="${url}" aria-current="page"`), url);
  assert.equal((html.match(/aria-current="page"/g) || []).length, 1, url);
}
assert.equal(M.safeHttpUrl('https://github.com/learn-ukrainian/machinery/pull/1'), 'https://github.com/learn-ukrainian/machinery/pull/1');
assert.equal(M.safeHttpUrl('javascript:alert(1)'), null);
assert.equal(M.safeHttpUrl('data:text/html,x'), null);
assert.equal(M.prLink('machinery', {number: 9, url: 'javascript:alert(1)'}), 'machinery#9');
assert.equal(M.ageSpan('not-a-date'), null);
assert.equal(M.ageSpan(null), null);

// Regression checks for F1 (Fleet PR enrichment clears on failure), F2 (Overview keeps unknown reset forecast
// in attention list), and F3 (Overview qualifies merge queue when PR coverage is unreadable or truncated).
const fs = require('node:fs');
const path = require('node:path');
const dashboardsDir = path.dirname(process.argv[1]);
const extractScript = (file, until) => {
  const html = fs.readFileSync(path.join(dashboardsDir, file), 'utf8');
  const marker = '<script src="/machinery.js"></script>';
  const after = html.slice(html.indexOf(marker) + marker.length);
  const inline = after.slice(after.indexOf('<script>') + '<script>'.length, after.indexOf('</script>'));
  return inline.slice(0, inline.indexOf(until));
};

// F2 + F3 against Overview (index.html)
const overviewBody = extractScript('index.html', '\nconst PANELS = [');
const overview = new Function('Machinery', overviewBody + '\nreturn {renderCapacitySummary, renderQueueSummary};')(M);

const unknownForecastSubs = {
  stale_after_s: 900,
  providers: [{
    provider: 'claude',
    accounts: [{
      account: 'Claude a',
      reading: {status: 'fresh', age_s: 30, read_at: '2030-01-01T00:00:00Z'},
      weekly: {status: 'ok', left_pct: 65, resets_at: '2030-01-05T00:00:00Z'},
      pace: {status: 'ok', state: 'on_pace', lasts_to_reset: null},
      plan: {status: 'ok'},
    }],
  }],
};
const signedIn = {state: 'ok', accounts: [{account: 'Claude a', logged_in: true}]};
const capUnknown = overview.renderCapacitySummary(unknownForecastSubs, signedIn);
assert.match(capUnknown, /Claude a/);
assert.match(capUnknown, /1<\/b> unknown \/ stale/);
assert.doesNotMatch(capUnknown, /All 1 active subscriptions are on pace/);

const knownForecastSubs = {
  ...unknownForecastSubs,
  providers: [{
    provider: 'claude',
    accounts: [{
      ...unknownForecastSubs.providers[0].accounts[0],
      pace: {status: 'ok', state: 'on_pace', lasts_to_reset: true},
    }],
  }],
};
const capHealthy = overview.renderCapacitySummary(knownForecastSubs, signedIn);
assert.match(capHealthy, /All 1 active subscriptions are on pace with fresh readings\./);

const unreadableRepoPrs = {
  status: 'ok',
  repositories: [
    {name: 'machinery', status: 'ok', open_total: 1, pull_requests: [{number: 1, ci: 'success', review: 'success', queue: null}]},
    {name: 'infra-private', status: 'unknown', reason: 'not_readable', open_total: null, pull_requests: []},
  ],
};
const queueUnreadable = overview.renderQueueSummary(unreadableRepoPrs);
assert.doesNotMatch(queueUnreadable, /No PRs currently in the merge queue/);
assert.match(queueUnreadable, /0<\/b> queued observed/);
assert.match(queueUnreadable, /coverage is incomplete/i);
assert.match(M.prChips(unreadableRepoPrs).join(''), /0 queued observed.*a repository is unknown/);

const fiftyPrs = Array.from({length: 50}, (_, i) => ({number: i + 1, ci: 'success', review: 'success', queue: null}));
const truncatedRepoPrs = {
  status: 'ok',
  repositories: [{name: 'machinery', status: 'ok', open_total: 51, pull_requests: fiftyPrs}],
};
const queueTruncated = overview.renderQueueSummary(truncatedRepoPrs);
assert.doesNotMatch(queueTruncated, /No PRs currently in the merge queue/);
assert.match(queueTruncated, /showing 50 of 51/);
assert.match(queueTruncated, /0<\/b> queued observed/);
assert.match(M.prChips(truncatedRepoPrs).join(''), /0 queued observed.*coverage truncated/);

const completeEmptyPrs = {
  status: 'ok',
  repositories: [{name: 'machinery', status: 'ok', open_total: 1, pull_requests: [{number: 1, ci: 'success', review: 'success', queue: null}]}],
};
assert.match(overview.renderQueueSummary(completeEmptyPrs), /No PRs currently in the merge queue\./);

// F1 against Fleet (fleet.html): PR enrichment clears when PR endpoint later fails with HTTP 503.
(async () => {
  const elements = new Map();
  globalThis.document = {
    querySelectorAll() { return []; },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {textContent: '', innerHTML: '', className: '', hidden: true, setAttribute() {}, dataset: {}});
      return elements.get(id);
    },
  };
  let prFails = false;
  globalThis.fetch = async (url) => {
    if (String(url).includes('/api/git/pull-requests')) {
      if (prFails) return {ok: false, status: 503, text: async () => '{}', headers: {get: () => null}};
      return {
        ok: true,
        status: 200,
        headers: {get: () => null},
        text: async () => JSON.stringify({
          status: 'ok',
          observed_at: new Date().toISOString(),
          repositories: [{
            name: 'machinery',
            status: 'ok',
            open_total: 1,
            pull_requests: [{number: 42, url: 'https://github.com/learn-ukrainian/machinery/pull/42', ci: 'success', queue: {position: 1}, task_reference: 't1'}],
          }],
        }),
      };
    }
    return {
      ok: true,
      status: 200,
      headers: {get: () => null},
      text: async () => JSON.stringify({
        tasks: [{task_id: 't1', agent: 'codex', model: 'gpt-6.2-sol', age_s: 10, delivery_pr: '#42'}],
        deliveries: {queued: 0},
        jobs: {queued: 0},
        store: {reachable: true},
      }),
    };
  };
  const fleetBody = extractScript('fleet.html', '\nrunner.start();');
  const fleetRunner = new Function('Machinery', fleetBody + '\nreturn runner;')(M);
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(document.getElementById('workers-content').innerHTML, /CI green/);
  assert.match(document.getElementById('workers-content').innerHTML, /queued 1/);
  prFails = true;
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  const afterFailure = document.getElementById('workers-content').innerHTML;
  assert.doesNotMatch(afterFailure, /CI green/);
  assert.doesNotMatch(afterFailure, /queued 1/);
  assert.match(afterFailure, /#42/);
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    ran = subprocess.run(
        ["node", "-e", node_script, str(dashboards / "machinery.js"), json.dumps(urls)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert ran.returncode == 0, ran.stderr
