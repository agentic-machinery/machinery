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


@pytest.mark.parametrize("path", [
    "/api/fleet/seats", "/api/fleet/deliveries",
    "/api/fleet/messages/{message_id}/body", "/api/fleet/messages/{message_id}/thread",
])
def test_fleet_panels_have_exact_read_only_contracts(path: str) -> None:
    """Defect class: a Fleet panel inherits the umbrella contract instead of its own read policy."""
    contract = contract_for_route(path)

    assert contract is not None
    assert contract.pattern == path
    assert contract.match == "exact"
    assert contract.locality == "cluster_authoritative"
    assert contract.mutates is False


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

    # A path the machinery dashboards do not hold still reaches the product's declared file.
    empty_dashboards = tmp_path / "empty"
    empty_dashboards.mkdir()
    bare = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=empty_dashboards))
    declared_product = ProductContribution(static_files={"index": product_index})
    declared_request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=declared_product))})
    assert main.serve_static("index", declared_request, bare).path == product_index


def test_monitor_navigation_lists_the_pages_the_product_declares(tmp_path, monkeypatch):
    """Defect class: a product page label appears in the shared machinery Monitor navigation."""
    import html
    import re

    from lu_machinery.api import main, route_contracts
    from lu_machinery.api.product_contribution import ProductContribution, checked_product

    monkeypatch.setattr(main.config, "PRODUCT_ROOT", tmp_path)
    for contract in route_contracts.PAGE_CONTRACTS:
        (tmp_path / contract.file).write_text('<nav id="machinery-nav">__MACHINERY_NAV__</nav>')
    product_home = tmp_path / "product-home.html"
    product_home.write_text("product home")
    base = replace(route_contracts.PAGE_CONTRACTS[0], label="")
    # The Monitor serves a product only as checked at load (create_app, load_product).
    product = checked_product(ProductContribution(
        static_files={"index.html": product_home},
        page_contracts=(
            replace(base, file="index.html", url="/"),
            replace(base, file="curriculum-dashboard.html", url="/curriculum-dashboard.html"),
            replace(base, file="labelled.html", url="/labelled", label='Q&A "x"'),
            replace(base, file="again.html", url="/labelled", label="Repeated URL"),
            replace(base, file="blank.html", url="/blank", label="  \t "),
            replace(base, file="bidi.html", url="/bidi", label="a\u202eb\n  c\x00"),
            # Contracts that reuse a machinery URL or alias replace the contract, never the machinery page.
            replace(base, file="product-fleet.html", url="/fleet", label="Product fleet"),
            replace(base, file="fleet.html", url="/fleet", label="Second fleet"),
            replace(base, file="alias.html", url="/fleet.html"),
            replace(base, file="overview.html", url="/index"),
            replace(base, file="elsewhere.html", url="//example.invalid/x"),
            replace(base, file="relative.html", url="relative"),
            replace(base, file="backslash.html", url="/\\example.invalid"),
            replace(base, file="space.html", url="/a b"),
            # F3: the route's own lookup decides, on the path a browser requests; URLs it answers with a
            # machinery page are not product links, and a product page the route leaves to it is one.
            replace(base, file="slash.html", url="/fleet.html/"),
            replace(base, file="home-slash.html", url="/index.html/"),
            replace(base, file="encoded.html", url="/fleet%2Ehtml?tab=x#y"),
            replace(base, file="dots.html", url="/x/../fleet"),
            replace(base, file="machinery.html", url="/machinery.html"),
            replace(base, file="reports.html", url="/a/%2e/%2E%2e/reports"),
            replace(base, file="notes.html", url="/notes?folder=a\\b#s one"),
            replace(base, file="again-notes.html", url="/x/../notes"),
            # The page is parsed as HTML first: a NUL in the attribute becomes U+FFFD.
            replace(base, file="nul.html", url="/fleet\x00"),
            replace(base, file="lead-nul.html", url="\x00/reports"),
            # Distinct declared URLs stay distinct links: the product may tell them apart.
            replace(base, file="tab.html", url="/reports?tab=second"),
            replace(base, file="dup-notes.html", url="/y/../notes?folder=a\\b#s one"),
            # F4: a file name or URL with nothing visible falls back to the next visible name.
            replace(base, file="----.html", url="/dashes"),
            replace(base, file="\u2067\u3164.html", url="/--"),
        ),
    ))
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=product))})
    context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=tmp_path))
    machinery = [(c.url, html.escape(c.nav_label)) for c in route_contracts.PAGE_CONTRACTS]
    for contract in route_contracts.PAGE_CONTRACTS:
        # The product's home owns /index.html, so the Overview's file alias is /index.
        for path in (contract.url.lstrip("/"), "index" if contract.file == "index.html" else contract.file):
            body = main.serve_static(path, request, context).body.decode("utf-8")
            links = re.findall(r'<a href="([^"]*)"([^>]*)>([^<]*)</a>', body)
            assert [(href, label) for href, _, label in links[: len(machinery)]] == machinery, path
            # Declared product pages are served, and none of their labels is a navigation entry.
            assert links[len(machinery) :] == [], path
            assert not any(label in {
                "Home", "Curriculum dashboard", "Q&amp;A &quot;x&quot;", "Blank", "ab c", "Space",
                "Machinery", "Reports", "Notes", "Again notes", "Nul", "Tab", "Dup notes", "Dashes", "/--",
            } for _, _, label in links), path
            assert [href for href, attrs, _ in links if 'aria-current="page"' in attrs] == [contract.url], path


def test_unusable_static_declaration_is_rejected_at_load_not_linked(tmp_path, monkeypatch):
    """Defect class: a product declaration the Monitor cannot serve or read is linked instead of refused at load.

    A declared static file that is missing, a directory, outside the product root or a symlink loop
    got a navigation link its route answered with 404 (F8); a page contract whose fields are not plain
    strings reached the navigation (F7). Both now refuse the product, naming the entry.
    """
    from lu_machinery.api import main, route_contracts
    from lu_machinery.api.monitor_context import fixture_context
    from lu_machinery.api.product_contribution import ProductContribution, ProductEntryError, checked_product

    root = tmp_path / "product"
    root.mkdir()
    monkeypatch.setattr(main.config, "PRODUCT_ROOT", root)
    (root / "reports.html").write_text("reports")
    (root / "folder").mkdir()
    (tmp_path / "outside.html").write_text("outside")
    (root / "escape.html").symlink_to(tmp_path / "outside.html")
    (root / "loop.html").symlink_to(root / "loop.html")
    page = replace(route_contracts.PAGE_CONTRACTS[0], file="reports.html", url="/reports", label="Reports")
    context = fixture_context(tmp_path)

    # A usable declaration loads. Its page label is not a navigation entry.
    static = {"reports.html": root / "reports.html"}
    good = checked_product(ProductContribution(static_files=static, page_contracts=(page,)))
    assert good.page_contracts[0].nav_label == "Reports"
    assert '<a href="/reports" class="product first">Reports</a>' not in main._machinery_nav_html(None)

    popped = replace(page)
    del vars(popped)["url"]
    unusable = {
        "static_files['reports.html']": {"reports.html": root / "missing.html"},
        "static_files['folder.html']": {"folder.html": root / "folder"},
        "static_files['escape.html']": {"escape.html": root / "escape.html"},
        "static_files['loop.html']": {"loop.html": root / "loop.html"},
        "static_files['text.html']": {"text.html": "reports.html"},
    }
    for entry, static_files in unusable.items():
        product = ProductContribution(static_files=static_files, page_contracts=(page,))
        with pytest.raises(ProductEntryError) as refused:
            main.create_app(context, product=product)
        assert str(refused.value).startswith(entry), refused.value
    malformed = {
        "page_contracts[0]": (None,),
        "page_contracts[0].label": (replace(page, label=None),),
        "page_contracts[0].url": (replace(page, url=type("Url", (str,), {})("/reports")),),
        "page_contracts[1].url": (page, popped),
        "page_contracts is list": [page],
    }
    for entry, pages in malformed.items():
        with pytest.raises(ProductEntryError) as refused:
            main.create_app(context, product=ProductContribution(page_contracts=pages))
        assert str(refused.value).startswith(entry), refused.value


def test_navigation_and_route_agree_on_a_broken_tree(tmp_path, monkeypatch):
    """Defect class: interpreter-dependent path resolution.

    Python's ``realpath`` and ``Path.is_file`` answer a broken tree (a symlink loop, ``..`` after a
    missing or non-directory component, a denied directory, an overlong name, a dangling link)
    differently on 3.12 and 3.13+, so a lookup through them gave the route, and the navigation that
    must match it, a different answer on each interpreter. The lookup now asks the kernel first, so
    each answer below is the same on every supported interpreter. The navigation lists no
    product page.
    """
    import os
    import re

    from fastapi import HTTPException

    from lu_machinery.api import main, route_contracts
    from lu_machinery.api.product_contribution import ProductContribution, checked_product

    dashboards = tmp_path / "dashboards"
    outside = tmp_path / "outside"
    product_root = tmp_path / "product"
    for directory in (dashboards, outside, product_root, dashboards / "denied"):
        directory.mkdir()
    monkeypatch.setattr(main.config, "PRODUCT_ROOT", product_root)
    (outside / "real.html").write_text("outside")
    (dashboards / "file.txt").write_text("file")
    (dashboards / "denied" / "x.html").write_text("denied")
    long_name = "l" * 300
    links = {
        "loop.html": "loop.html",
        "missing-cut.html": "missing/../loop.html/../leaf.html",
        "file-cut.html": "file.txt/../loop.html/../leaf.html",
        "leaf.html": str(outside / "real.html"),
        "denied-alias.html": "denied/x.html",
        "long.html": str(outside / f"{long_name}.html"),
        "dangling.html": str(outside / "missing.html"),
    }
    for index in range(41):  # one more link than the kernel follows
        links[f"chain{index}.html"] = f"chain{index + 1}.html"
    (dashboards / "chain41.html").write_text("chain end")
    for name, target in links.items():
        (dashboards / name).symlink_to(target)
    expected = {
        "/loop": 404,
        "/missing-cut": "product",
        "/file-cut": "product",
        "/denied/x": 404,
        "/denied-alias": 404,
        "/long": 404,
        f"/{long_name}": 404,
        "/dangling.html": "product",
        "/chain0": 404,
        "/leaf.html": 403,
    }
    (dashboards / "denied").chmod(0)
    try:
        if os.access(dashboards / "denied", os.X_OK):  # privileged: a mode cannot deny a lookup
            for url in ("/denied/x", "/denied-alias"):
                del expected[url]
        # The product declares a page at every path, so a path the route leaves to it is served from it.
        static = {}
        for index, url in enumerate(expected):
            (product_root / f"{index}.html").write_text(url)
            static[url.lstrip("/")] = product_root / f"{index}.html"
        base = route_contracts.PAGE_CONTRACTS[0]
        product = checked_product(ProductContribution(
            static_files=static,
            page_contracts=tuple(replace(base, file=f"{i}.html", url=url, label=url) for i, url in enumerate(expected)),
        ))
        request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(product=product))})
        context = SimpleNamespace(roots=SimpleNamespace(dashboards_dir=dashboards))
        answers: dict[str, int | str] = {}
        for url in expected:
            try:
                response = main.serve_static(url.lstrip("/"), request, context)
            except HTTPException as refused:
                answers[url] = refused.status_code
            else:
                answers[url] = "product" if response.path.is_relative_to(product_root) else str(response.path)
        listed = re.findall(r'<a href="([^"]*)" class="product', main._machinery_nav_html(None))
    finally:
        (dashboards / "denied").chmod(0o700)
    assert answers == expected
    assert listed == []


def test_machinery_split_pages_contracts_shared_assets_and_scoped_reads():
    """Defect class: a split dashboard page misses its contract, shared nav/assets, or fetches another area's API."""
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
        body = response.body.decode("utf-8")
        assert "__MACHINERY_" not in body, page.file
        if page.file == "index.html":
            assert "/machinery.css" in body
            assert "/machinery.js" in body
            root_response = main.serve_static("", request, context)
            assert b"/machinery.css" in root_response.body
            # /index is the same page with the same fill-ins.
            assert main.serve_static("index", request, context).body == response.body
        else:
            assert main.serve_static(page.file, request, context).body == response.body
        # The shared navigation is in the served HTML (no script needed), lists every machinery page in
        # contract order, and marks exactly the page being served.
        nav = re.search(r'<nav class="pages" id="machinery-nav"[^>]*>(.*?)</nav>', body, re.S)
        assert nav, page.file
        links = re.findall(r'<a href="([^"]*)"([^>]*)>([^<]*)</a>', nav.group(1))
        assert [href for href, _, _ in links] == [contract.url for contract in route_contracts.PAGE_CONTRACTS]
        assert [href for href, attrs, _ in links if 'aria-current="page"' in attrs] == [page.url], page.file
        assert not any("product" in attrs for _, attrs, _ in links)

    # Every page loads the shared stylesheet, script, landmarks, and only its own API endpoints.
    expected_apis = {
        "index.html": {
            "/api/ops/v1/alerts",
            "/api/runtime/subscriptions",
            "/api/runtime/logins",
            "/api/git/pull-requests",
        },
        "subscriptions.html": {
            "/api/state/routing-budget",
            "/api/runtime/logins",
        },
        "fleet.html": {
            "/api/delegate/active",
            "/api/delegate/recent",
            "/api/comms/v1/metrics",
            "/api/git/pull-requests",
            "/api/fleet/seats",
            "/api/fleet/deliveries",
            "/api/fleet/messages",
            "/api/fleet/model-stats",
            "/api/fleet/acp/conversations",
            "/api/runtime/acp/conversations",
        },
        "pull-requests.html": {"/api/git/pull-requests"},
        "milestones.html": {"/api/milestones"},
        "routing.html": {"/api/runtime/routing"},
        "model-stats.html": {"/api/fleet/model-stats", "/api/fleet/model-stats/planning", "/api/state/routing-budget"},
        "journal.html": {"/api/journal"},
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

    # The shared script rejects unsafe URLs, and its panel and rendering regressions stay fixed.
    node_script = r"""
const assert = require('node:assert/strict');
const M = require(process.argv[1]);
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

// F5: OK repository with open_total: null and 1 queued PR counts 1 open PR observed (lower bound) while per-repo Open stays '?'.
const unknownTotalPrs = {
  status: 'ok',
  repositories: [
    {name: 'machinery', status: 'ok', open_total: null, pull_requests: [{number: 7, url: 'https://github.com/learn-ukrainian/machinery/pull/7', ci: 'success', review: 'success', queue: {position: 1, state: 'queued'}}]},
    {name: 'learn-ukrainian', status: 'ok', open_total: 0, pull_requests: []},
  ],
};
const queueUnknownTotal = overview.renderQueueSummary(unknownTotalPrs);
assert.match(queueUnknownTotal, /1<\/b> open PRs observed/);
assert.match(queueUnknownTotal, /1<\/b> queued observed/);
assert.match(queueUnknownTotal, /<td>machinery<\/td><td class="num warn">\?<\/td>/);
assert.match(M.prChips(unknownTotalPrs).join(''), /<b>1<\/b> open PRs observed · 1 queued observed \(coverage truncated\)/);

// F1 + F4 against Fleet (fleet.html):
// 1. Fresh 30s snapshot with readable repo + unreadable sibling (X-Snapshot-State: stale) preserves CI green & queued 1 (F4).
// 2. Expired HTTP 200 cached snapshot (240s old, 210s bound, X-Snapshot-State: stale) clears CI green & queued 1 (F1).
// 3. Browser clock offsets (-120s behind / +240s ahead) honor X-Snapshot-Age-S over observed_at (F1).
// 4. HTTP 503 clears CI green & queued 1 and falls back to delivery_pr #42 (F1).
(async () => {
  const elements = new Map();
  globalThis.document = {
    querySelectorAll() { return []; },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        textContent: '', innerHTML: '', className: '', hidden: true, value: '', options: [],
        childElementCount: 0, dataset: {},
        setAttribute() {}, addEventListener() {}, querySelector() { return null; },
      });
      return elements.get(id);
    },
  };
  let mode = 'fresh_partial';
  globalThis.fetch = async (url) => {
    if (String(url).includes('/api/git/pull-requests')) {
      if (mode === 'http_503') return {ok: false, status: 503, text: async () => '{}', headers: {get: () => null}};
      const ageS = mode === 'expired_200' || mode === 'clock_behind_expired' ? 240 : 30;
      const clockOffsetS = mode === 'clock_behind_expired' ? -120 : mode === 'clock_ahead_fresh' ? 240 : 0;
      const observedAt = new Date(Date.now() - (ageS + clockOffsetS) * 1000).toISOString();
      const repos = [
        {
          name: 'machinery',
          status: 'ok',
          open_total: 1,
          pull_requests: [{number: 42, url: 'https://github.com/learn-ukrainian/machinery/pull/42', ci: 'success', queue: {position: 1}, task_reference: 't1'}],
        },
        ...(mode === 'fresh_partial'
          ? [{name: 'infra-private', status: 'unknown', reason: 'not_readable', open_total: null, pull_requests: []}]
          : []),
      ];
      const headers = {
        'X-Snapshot-Age-S': String(ageS),
        'X-Snapshot-Stale-After-S': '210',
        'X-Snapshot-State': 'stale',
      };
      return {
        ok: true,
        status: 200,
        headers: {get: name => headers[name] ?? null},
        text: async () => JSON.stringify({status: 'ok', observed_at: observedAt, repositories: repos}),
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
  const {runner: fleetRunner, prSnapshotFresh} = new Function('Machinery', fleetBody + '\nreturn {runner, prSnapshotFresh};')(M);
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  const partialHtml = document.getElementById('workers-content').innerHTML;
  assert.match(partialHtml, /CI green/, 'F4: readable repo preserves CI green despite unreadable sibling');
  assert.match(partialHtml, /queued 1/, 'F4: readable repo preserves queued 1 despite unreadable sibling');

  mode = 'expired_200';
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  const expiredHtml = document.getElementById('workers-content').innerHTML;
  assert.doesNotMatch(expiredHtml, /CI green/, 'F1: expired HTTP 200 snapshot clears CI green');
  assert.doesNotMatch(expiredHtml, /queued 1/, 'F1: expired HTTP 200 snapshot clears queued 1');
  assert.match(expiredHtml, /#42/);

  mode = 'clock_behind_expired';
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  const behindHtml = document.getElementById('workers-content').innerHTML;
  assert.doesNotMatch(behindHtml, /CI green/, 'F1: -120s browser clock offset still expires 240s server snapshot');
  assert.doesNotMatch(behindHtml, /queued 1/, 'F1: -120s browser clock offset still clears queued 1');
  assert.match(behindHtml, /#42/);

  mode = 'clock_ahead_fresh';
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  const aheadHtml = document.getElementById('workers-content').innerHTML;
  assert.match(aheadHtml, /CI green/, 'F1: +240s browser clock offset still trusts 30s server snapshot');
  assert.match(aheadHtml, /queued 1/, 'F1: +240s browser clock offset still keeps queued 1');

  const now = Date.now();
  const readable = {status: 'ok', repositories: [{name: 'machinery', status: 'ok', open_total: 0, pull_requests: []}]};
  const skewed = new Date(now + 90000).toISOString();
  // A missing, empty, or non-numeric age header is unknown. observed_at does not fill it in, and Number('') is not 0.
  for (const ageHeader of [null, '', ' ', 'unknown']) {
    const reading = {data: {...readable, observed_at: skewed}, ageHeader, boundHeader: '210', state: 'fresh', receivedAt: now};
    assert.equal(M.panelAgeSeconds(reading, now), null, `age header ${JSON.stringify(ageHeader)}`);
    assert.equal(prSnapshotFresh(reading, now), false);
    assert.equal(M.prSnapshotExpired(reading, now), false);
    assert.match(M.headingAge([reading], false, now, undefined, now).text, /source age unknown/);
    assert.doesNotMatch(M.headingAge([reading], false, now, undefined, now).text, /source age 0s/);
  }
  assert.equal(prSnapshotFresh({data: {...readable, observed_at: skewed}, ageHeader: '30', boundHeader: '210', state: 'fresh', receivedAt: now}, now), true);
  assert.equal(prSnapshotFresh({data: {...readable, observed_at: new Date(now).toISOString()}, ageHeader: '200', bound: 210, state: 'fresh', receivedAt: now - 20000}, now), false);

  mode = 'fresh_partial';
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(document.getElementById('workers-content').innerHTML, /CI green/);

  mode = 'http_503';
  await fleetRunner.refreshAll();
  await new Promise(resolve => setImmediate(resolve));
  const afterFailure = document.getElementById('workers-content').innerHTML;
  assert.doesNotMatch(afterFailure, /CI green/);
  assert.doesNotMatch(afterFailure, /queued 1/);
  assert.match(afterFailure, /#42/);

  // Defect classes: request on page load or poll, stale selection, and body content interpreted as HTML.
  {
    const listeners = new Map();
    const pending = new Map();
    const requests = [];
    const timers = [];
    const firstId = 'message_00000000000000000000000000000001';
    const secondId = 'message_00000000000000000000000000000002';
    document.createElement = tag => ({
      tag, children: [], dataset: {}, style: {}, textContent: '',
      set innerHTML(_) { assert.fail('Message reader inserted HTML'); },
      appendChild(child) { this.children.push(child); },
      addEventListener(type, listener) { this[type] = listener; },
    });
    for (const id of ['message-body', 'message-thread']) {
      document.getElementById(id).replaceChildren = function (...nodes) { this.children = nodes; };
    }
    document.getElementById('message-reader').focus = () => {};
    document.addEventListener = (type, handler) => listeners.set(type, [...(listeners.get(type) || []), handler]);
    const fetchPanel = globalThis.fetch;
    globalThis.fetch = (url, options) => {
      requests.push(url);
      if (!url.startsWith('/api/fleet/messages/')) return fetchPanel(url);
      assert.equal(options.cache, 'no-store');
      return new Promise(resolve => pending.set(url, resolve));
    };
    const originalInterval = globalThis.setInterval;
    globalThis.setInterval = callback => timers.push(callback);
    try {
      document._fleetFiltersBound = false;
      new Function('Machinery', fleetBody + '\nrunner.start();')(M);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(pending.size, 0);
      assert.ok(requests.some(url => url.startsWith('/api/fleet/deliveries')));
      await timers[0]();
      assert.equal(pending.size, 0);
      const select = id => listeners.get('click').forEach(listener => listener({
        target: {closest: selector => selector === 'button[data-open-message]' ? {dataset: {openMessage: id}} : null},
      }));
      const bodyUrl = id => `/api/fleet/messages/${id}/body`;
      const threadUrl = id => `/api/fleet/messages/${id}/thread?offset=0`;
      const resolve = (url, payload) => {
        pending.get(url)({ok: true, json: async () => payload});
        pending.delete(url);
      };
      select(firstId);
      select(secondId);
      assert.deepEqual([...pending.keys()], [
        bodyUrl(firstId), threadUrl(firstId), bodyUrl(secondId), threadUrl(secondId),
      ]);
      assert.equal(document.getElementById('message-reader').hidden, false);
      const literalBody = '<img src=x onerror="window.executed=true">';
      resolve(bodyUrl(secondId), {state: 'ok', message_id: secondId, body: literalBody});
      resolve(threadUrl(secondId), {
        messages: [{message_id: firstId, kind: 'message', created_at: '2026-11-26T11:00:01Z'}],
        limit: 50,
        next_offset: 50,
      });
      await new Promise(resolve => setImmediate(resolve));
      resolve(bodyUrl(firstId), {state: 'ok', message_id: firstId, body: 'late body'});
      resolve(threadUrl(firstId), {messages: [], limit: 50, next_offset: null});
      await new Promise(resolve => setImmediate(resolve));
      const bodyNode = document.getElementById('message-body').children[1];
      assert.equal(bodyNode.tag, 'pre');
      assert.equal(bodyNode.textContent, literalBody);
      assert.equal(document.getElementById('messages-status').textContent, 'Message loaded.');
      await timers[0]();
      assert.equal(pending.size, 0, 'A poll while the reader is open requests zero bodies and threads');
      const [members, next] = document.getElementById('message-thread').children;
      members.children[0].children[0].click();
      assert.deepEqual([...pending.keys()], [bodyUrl(firstId)], 'A thread member loads only its own body');
      next.click();
      const nextPage = `/api/fleet/messages/${secondId}/thread?offset=50`;
      assert.deepEqual([...pending.keys()], [bodyUrl(firstId), nextPage]);
      resolve(bodyUrl(firstId), {state: 'ok', message_id: firstId, body: 'first body'});
      resolve(nextPage, {messages: [], limit: 50, next_offset: null});
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(document.getElementById('message-body').children[1].textContent, 'first body');
    } finally {
      globalThis.setInterval = originalInterval;
      globalThis.fetch = fetchPanel;
    }
  }

  // F1 local-clock expiry + F6 on Overview and Pull requests (no extra API request on tick).
  let fakeNow = Date.now();
  const realDateNow = Date.now;
  const realPerfNow = performance.now.bind(performance);
  let monoNow = realPerfNow();
  let prFetchCount = 0;
  let queuedInResponse = true;
  let customAgeS = 200;
  let customOffsetS = 0;
  let customPartial = false;
  Date.now = () => fakeNow;
  performance.now = () => monoNow;
  try {
    globalThis.fetch = async (url) => {
      if (String(url).includes('/api/git/pull-requests')) {
        prFetchCount += 1;
        const observedAt = new Date(fakeNow - (customAgeS + customOffsetS) * 1000).toISOString();
        const repos = [
          {
            name: 'machinery',
            status: 'ok',
            open_total: 1,
            pull_requests: [{number: 42, url: 'https://github.com/learn-ukrainian/machinery/pull/42', ci: 'success', review: 'success', queue: queuedInResponse ? {position: 1, state: 'queued'} : null, task_reference: 't1'}],
          },
          ...(customPartial ? [{name: 'infra-private', status: 'unknown', reason: 'not_readable', open_total: null, pull_requests: []}] : []),
        ];
        const headers = {'X-Snapshot-Age-S': String(customAgeS), 'X-Snapshot-Stale-After-S': '210', 'X-Snapshot-State': customAgeS > 210 || customPartial ? 'stale' : 'fresh'};
        return {ok: true, status: 200, headers: {get: name => headers[name] ?? null}, text: async () => JSON.stringify({status: 'ok', observed_at: observedAt, repositories: repos})};
      }
      return {ok: true, status: 200, headers: {get: () => null}, text: async () => JSON.stringify({
        status: 'ok', heartbeat: true, alerts: [], providers: [],
        tasks: [{task_id: 't1', agent: 'codex', model: 'gpt-6.2-sol', age_s: 10, delivery_pr: '#42'}],
        deliveries: {queued: 0}, jobs: {queued: 0}, store: {reachable: true},
      })};
    };

    const fleetClock = new Function('Machinery', fleetBody + '\nreturn {runner, PANELS};')(M);
    await fleetClock.runner.refreshAll();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(prFetchCount, 1);
    assert.match(document.getElementById('workers-content').innerHTML, /CI green/);
    fakeNow += 11000;
    fleetClock.PANELS[0].onTick();
    assert.equal(prFetchCount, 1, 'F1: a wall-clock step makes no API request');
    assert.match(document.getElementById('workers-content').innerHTML, /CI green/, 'F1: a wall-clock step does not expire the snapshot');
    monoNow += 11000;
    fleetClock.PANELS[0].onTick();
    assert.equal(prFetchCount, 1, 'F1: monotonic expiry makes no API request');
    const tickedFleet = document.getElementById('workers-content').innerHTML;
    assert.doesNotMatch(tickedFleet, /CI green/, 'F1: monotonic elapsed clears CI green once 210s bound passes');
    assert.doesNotMatch(tickedFleet, /queued 1/, 'F1: monotonic elapsed clears queued 1 once 210s bound passes');
    assert.match(tickedFleet, /#42/);
    monoNow -= 20000;
    fleetClock.PANELS[0].onTick();
    const rolledFleet = document.getElementById('workers-content').innerHTML;
    assert.doesNotMatch(rolledFleet, /CI green/, 'F1: rolling the monotonic clock back does not freshen an expired snapshot');
    assert.doesNotMatch(rolledFleet, /queued 1/);

    // F6 Overview: 240s server age with -120s browser clock and empty queue must not say "No PRs currently in the merge queue."
    customAgeS = 240; customOffsetS = -120; queuedInResponse = false; customPartial = false;
    const overviewClock = new Function('Machinery', extractScript('index.html', '\nrunner.start();') + '\nreturn {runner, PANELS};')(M);
    await overviewClock.runner.refreshAll();
    assert.match(document.getElementById('prs-age').textContent, /STALE · source age 240s · bound 210s/);
    const ovExpiredEmpty = document.getElementById('prs-content').innerHTML;
    assert.doesNotMatch(ovExpiredEmpty, /No PRs currently in the merge queue/);
    assert.match(ovExpiredEmpty, /PR snapshot is expired \(cached\)/);
    assert.match(document.getElementById('chips').innerHTML, /cached/);

    // F6 Pull requests: 240s server age with -120s browser clock qualifies queued rows as cached.
    queuedInResponse = true;
    const prsClock = new Function('Machinery', extractScript('pull-requests.html', '\nrunner.start();') + '\nreturn {runner, PANELS};')(M);
    await prsClock.runner.refreshAll();
    assert.match(document.getElementById('prs-age').textContent, /STALE · source age 240s · bound 210s/);
    const prExpiredHtml = document.getElementById('prs-content').innerHTML;
    assert.match(prExpiredHtml, /PR snapshot is expired; showing cached observations/);
    assert.match(prExpiredHtml, /position 1 · queued \(cached\)/);
    assert.match(document.getElementById('chips').innerHTML, /cached/);

    // F6 Coverage separation: 30s snapshot with +240s clock offset and unreadable sibling still shows readable rows as non-expired.
    customAgeS = 30; customOffsetS = 240; customPartial = true;
    await overviewClock.runner.refreshAll();
    assert.match(document.getElementById('prs-content').innerHTML, /In merge queue \(observed — coverage incomplete\)/);
    assert.doesNotMatch(document.getElementById('prs-content').innerHTML, /snapshot is expired/);
    await prsClock.runner.refreshAll();
    assert.doesNotMatch(document.getElementById('prs-content').innerHTML, /snapshot is expired/);
    assert.match(document.getElementById('prs-content').innerHTML, /<td class="ok">position 1 · queued<\/td>/);

    // F6 boundary race: 209.999s age when renderNow is sampled; even if clock advances 2ms before onTick, body, chip, and tick stay consistent.
    const boundaryReading = {
      url: '/api/git/pull-requests', age: 209.999, bound: 210, state: 'fresh', receivedAt: fakeNow,
      data: {status: 'ok', observed_at: new Date(fakeNow - 209999).toISOString(), repositories: [{name: 'machinery', status: 'ok', open_total: 0, pull_requests: []}]},
    };
    const origNowFn = Date.now;
    let calls = 0;
    Date.now = () => { calls += 1; return calls <= 1 ? fakeNow : fakeNow + 2; };
    const racePrsPanel = {id: 'prs', label: 'Merge queue', urls: ['/api/git/pull-requests'], ok: true, readings: [boundaryReading]};
    const raceLatest = {prs: boundaryReading.data};
    const loadNow = Date.now();
    const initState = M.prSnapshotState(boundaryReading, loadNow);
    assert.equal(initState.status, 'fresh');
    raceLatest.prsStatus = initState.status;
    const initHtml = new Function('Machinery', extractScript('index.html', '\nrunner.start();') + '\nreturn renderQueueSummary;')(M)(boundaryReading.data, initState.status);
    assert.match(initHtml, /No PRs currently in the merge queue/);
    overviewClock.PANELS[2].onTick(racePrsPanel, raceLatest, () => {}, Date.now());
    assert.equal(raceLatest.prsStatus, 'expired');
    assert.match(document.getElementById('prs-content').innerHTML, /PR snapshot is expired \(cached\)/);
    Date.now = origNowFn;

    // A non-finite tick repaints a fresh body and chip to age unknown. It does not leave the previous fresh copy.
    customAgeS = 30; customOffsetS = 0; queuedInResponse = false; customPartial = false;
    await overviewClock.runner.refreshAll();
    assert.match(document.getElementById('prs-content').innerHTML, /No PRs currently in the merge queue/);
    overviewClock.PANELS[2].onTick(
      overviewClock.PANELS[2],
      overviewClock.runner.latest,
      () => {
        const status = overviewClock.runner.latest.prsStatus;
        document.getElementById('chips').innerHTML = M.prChips(overviewClock.runner.latest.prs, '/pull-requests', status).join('');
      },
      Number.NaN,
    );
    assert.equal(overviewClock.runner.latest.prsStatus, 'unknown');
    assert.match(document.getElementById('prs-content').innerHTML, /Snapshot age unknown/);
    assert.match(document.getElementById('chips').innerHTML, /age unknown/);
    await prsClock.runner.refreshAll();
    prsClock.PANELS[0].onTick(prsClock.PANELS[0], prsClock.runner.latest, () => {}, Number.NaN);
    assert.equal(prsClock.runner.latest.prsStatus, 'unknown');
    assert.match(document.getElementById('prs-content').innerHTML, /Snapshot age unknown/);

    // Missing and empty X-Snapshot-Age-S stay unknown. A skewed observed_at must not expire or freshen them.
    for (const ageHeader of [null, '']) {
      globalThis.fetch = async () => ({
        ok: true,
        status: 200,
        headers: {get: name => {
          if (name === 'X-Snapshot-Age-S') return ageHeader;
          return {'X-Snapshot-Stale-After-S': '210', 'X-Snapshot-State': 'stale'}[name] ?? null;
        }},
        text: async () => JSON.stringify({
          status: 'ok',
          observed_at: new Date(fakeNow + 90000).toISOString(),
          repositories: [{name: 'machinery', status: 'ok', open_total: 0, pull_requests: []}],
        }),
      });
      const reading = await M.fetchJson('/api/git/pull-requests');
      const state = M.prSnapshotState(reading, fakeNow);
      assert.equal(state.status, 'unknown', `header ${JSON.stringify(ageHeader)}`);
      assert.equal(state.expired, false);
      assert.equal(M.panelAgeSeconds(reading, fakeNow), null);
      assert.match(M.headingAge([reading], false, fakeNow, undefined, fakeNow).text, /freshness unknown · source age unknown/);
      assert.doesNotMatch(M.headingAge([reading], false, fakeNow, undefined, fakeNow).text, /source age 0s/);
      const html = new Function('Machinery', extractScript('index.html', '\nrunner.start();') + '\nreturn renderQueueSummary;')(M)(reading.data, state.status);
      assert.match(html, /Snapshot age unknown/);
      assert.doesNotMatch(html, /currently in the merge queue/);
      assert.doesNotMatch(html, /expired \(cached\)/);
      const prHtml = new Function('Machinery', extractScript('pull-requests.html', '\nrunner.start();') + '\nreturn renderPrs;')(M)(reading.data, state.status);
      assert.match(prHtml, /Snapshot age unknown/);
      assert.doesNotMatch(prHtml, /cached/);
    }

    // F8: status: ok with every repository unknown and X-Snapshot-State: unavailable renders unknown counts (never cached zeros) while retaining repo failures.
    const f8Reading = {
      url: '/api/git/pull-requests', age: null, bound: 210, state: 'unavailable', receivedAt: fakeNow,
      data: {status: 'ok', observed_at: new Date(fakeNow).toISOString(), repositories: [
        {name: 'machinery', status: 'unknown', reason: 'github_unavailable', open_total: null, pull_requests: []},
        {name: 'learn-ukrainian', status: 'unknown', reason: 'github_timeout', open_total: null, pull_requests: []},
      ]},
    };
    const f8State = M.prSnapshotState(f8Reading, fakeNow);
    assert.deepEqual(f8State, {status: 'unavailable', expired: false, unavailable: true});
    assert.match(M.headingAge([f8Reading], false, fakeNow, undefined, fakeNow).text, /unavailable/);
    assert.match(M.prChips(f8Reading.data, '/pull-requests', f8State.status).join(''), /\?.*PRs unknown/);
    assert.doesNotMatch(M.prChips(f8Reading.data, '/pull-requests', f8State.status).join(''), /cached/);
    const ovF8 = new Function('Machinery', extractScript('index.html', '\nrunner.start();') + '\nreturn renderQueueSummary;')(M)(f8Reading.data, f8State.status);
    assert.match(ovF8, /<b>\?<\/b> open PRs/);
    assert.match(ovF8, /<b>\?<\/b> queued/);
    assert.match(ovF8, /All repositories are unavailable/);
    assert.match(ovF8, /github unavailable/);
    assert.doesNotMatch(ovF8, /cached/);
    const prF8 = new Function('Machinery', extractScript('pull-requests.html', '\nrunner.start();') + '\nreturn renderPrs;')(M)(f8Reading.data, f8State.status);
    assert.match(prF8, /All repositories are unavailable/);
    assert.match(prF8, /github timeout/);
    assert.doesNotMatch(prF8, /cached/);

    // Unavailable snapshot state keeps a retained ok row from showing a numeric zero.
    const retained = {
      url: '/api/git/pull-requests', state: 'unavailable', receivedAt: fakeNow,
      data: {status: 'ok', repositories: [{name: 'machinery', status: 'ok', open_total: 0, pull_requests: [{number: 9, queue: {position: 1, state: 'queued'}}]}]},
    };
    const retainedHtml = new Function('Machinery', extractScript('index.html', '\nrunner.start();') + '\nreturn renderQueueSummary;')(M)(retained.data, 'unavailable');
    assert.match(retainedHtml, /<b>\?<\/b> open PRs/);
    assert.match(retainedHtml, /snapshot unavailable/);
    assert.doesNotMatch(retainedHtml, /0 open|position 1/);
    const retainedPr = new Function('Machinery', extractScript('pull-requests.html', '\nrunner.start();') + '\nreturn renderPrs;')(M)(retained.data, 'unavailable');
    assert.match(retainedPr, /snapshot unavailable/);
    assert.doesNotMatch(retainedPr, /0 open|position 1/);

    // A known age with a missing, empty, or zero bound is freshness unknown, not age unknown.
    for (const boundHeader of [null, '', '0']) {
      const reading = {
        url: '/api/git/pull-requests', ageHeader: '30', boundHeader, state: 'fresh', receivedAt: fakeNow,
        data: {status: 'ok', repositories: [{name: 'machinery', status: 'ok', open_total: 0, pull_requests: []}]},
      };
      const state = M.prSnapshotState(reading, fakeNow);
      assert.equal(state.status, 'unbounded', `bound ${JSON.stringify(boundHeader)}`);
      assert.match(M.headingAge([reading], false, fakeNow, undefined, fakeNow).text, /freshness unknown · source age 30s · bound unknown/);
      const html = new Function('Machinery', extractScript('index.html', '\nrunner.start();') + '\nreturn renderQueueSummary;')(M)(reading.data, state.status);
      assert.match(html, /Freshness unknown/);
      assert.doesNotMatch(html, /Snapshot age unknown/);
      assert.doesNotMatch(html, /currently in the merge queue/);
      const prHtml = new Function('Machinery', extractScript('pull-requests.html', '\nrunner.start();') + '\nreturn renderPrs;')(M)(reading.data, state.status);
      assert.match(prHtml, /Freshness unknown/);
      assert.doesNotMatch(prHtml, /Snapshot age unknown/);
      assert.match(M.prChips(reading.data, '/pull-requests', state.status).join(''), /freshness unknown/);
      assert.doesNotMatch(M.prChips(reading.data, '/pull-requests', state.status).join(''), /age unknown/);
    }

    // A monotonic clock that returns a non-finite stamp stays unknown. It is not treated as a missing clock.
    const finitePerf = performance.now;
    performance.now = () => Number.NaN;
    globalThis.fetch = async () => ({
      ok: true,
      status: 200,
      headers: {get: name => name === 'X-Snapshot-Age-S' ? '0' : (name === 'X-Snapshot-Stale-After-S' ? '10' : 'fresh')},
      text: async () => JSON.stringify({status: 'ok', repositories: [{name: 'machinery', status: 'ok', open_total: 0, pull_requests: []}]}),
    });
    const badStamp = await M.fetchJson('/api/git/pull-requests');
    performance.now = finitePerf;
    assert.equal(Number.isNaN(badStamp.receivedMono), true);
    assert.equal(M.panelAgeSeconds(badStamp, fakeNow), null);
    assert.equal(M.prSnapshotState(badStamp, fakeNow).status, 'unknown');

    // Model statistics stamps the age when the response arrives, then keeps the sentence moving.
    const savedInterval = globalThis.setInterval;
    const clocks = [];
    globalThis.setInterval = (fn) => { clocks.push(fn); return clocks.length; };
    try {
      const statsScript = extractScript('model-stats.html', '\n$("window").addEventListener');
      const stats = new Function('Machinery', statsScript + '\nreturn {load};')(M);
      const wallBefore = fakeNow;
      const monoBefore = monoNow;
      globalThis.fetch = async (url) => {
        const page = String(url).includes('view=page');
        if (page) { fakeNow += 5000; monoNow += 5000; }
        return {
          ok: true,
          status: 200,
          headers: {get: name => page && name === 'X-Snapshot-Age-S' ? '0.000' : null},
          json: async () => page
            ? {status: 'ok', records: 3, window: {since: '2026-01-01T00:00:00Z', name: '24h', until: '2026-01-02T00:00:00Z'}, models: [], review_levels: [], charts: {}, authors: [], author_risks: [], allowance: []}
            : {status: 'ok', models: []},
        };
      };
      let statsError = null;
      try { await stats.load(); } catch (err) { statsError = err; }
      assert.match(document.getElementById('age').textContent, /collected 0 seconds ago/, statsError && statsError.stack);
      assert.ok(fakeNow > wallBefore && monoNow > monoBefore);
      monoNow += 4000;
      assert.equal(clocks.length > 0, true);
      clocks[clocks.length - 1]();
      assert.match(document.getElementById('age').textContent, /collected 4 seconds ago/);
      // A window change that has not returned keeps the displayed snapshot's age moving.
      let releaseSecond;
      const secondGate = new Promise(resolve => { releaseSecond = resolve; });
      const shown = clocks[clocks.length - 1];
      globalThis.fetch = async (url) => {
        const page = String(url).includes('view=page');
        if (page) await secondGate;
        return {
          ok: true,
          status: 200,
          headers: {get: name => page && name === 'X-Snapshot-Age-S' ? '0.000' : null},
          json: async () => page
            ? {status: 'ok', records: 4, window: {since: '2026-01-01T00:00:00Z', name: '24h', until: '2026-01-02T00:00:00Z'}, models: [], review_levels: [], charts: {}, authors: [], author_risks: [], allowance: []}
            : {status: 'ok', models: []},
        };
      };
      const second = stats.load();
      monoNow += 45000;
      shown();
      assert.match(document.getElementById('age').textContent, /collected 49 seconds ago/);
      releaseSecond();
      try { await second; } catch { /* the page render is outside this age check */ }
      assert.match(document.getElementById('age').textContent, /collected 0 seconds ago/);
    } finally {
      globalThis.setInterval = savedInterval;
    }
  } finally {
    Date.now = realDateNow;
    performance.now = realPerfNow;
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
"""
    ran = subprocess.run(
        ["node", "-e", node_script, str(dashboards / "machinery.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert ran.returncode == 0, ran.stderr
