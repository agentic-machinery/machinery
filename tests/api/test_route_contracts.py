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
"""
    ran = subprocess.run(
        ["node", "-e", node_script, str(dashboards / "machinery.js"), json.dumps(urls)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert ran.returncode == 0, ran.stderr
