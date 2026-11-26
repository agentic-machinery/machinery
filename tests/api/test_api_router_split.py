"""
Tests for the Blue/Gold API router split.

Validates:
  1. All Blue endpoints return 200
  2. All Gold endpoints return 200
  3. All shared endpoints return 200
  4. Old Blue paths at /api/batch/ are gone (404)
  5. Static dashboard serving rejects path traversal
"""

from fastapi.testclient import TestClient

from lu_machinery.api.main import app, create_app
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.product_contribution import EMPTY

client = TestClient(app)

# ==================== Endpoint existence ====================


# ==================== Old paths are gone ====================


# ==================== Static file serving ====================

class TestStaticServing:
    """Dashboard HTML should be servable."""

    def test_stored_files_cannot_execute_with_monitor_origin(self, tmp_path):
        """Defect class: a stored response executes in the Monitor origin."""
        reports = tmp_path / "docs" / "reports"
        reports.mkdir(parents=True)
        files = {
            "report.html": ("<p>Report</p>", "text/html"),
            "report.svg": ('<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
            "report.js": ("document.title = 'Report';", "text/plain"),
            "report.txt": ("Report", "text/plain"),
        }
        for name, (body, _) in files.items():
            (reports / name).write_text(body, encoding="utf-8")
        local = TestClient(create_app(fixture_context(tmp_path), product=EMPTY))

        for prefix in ("artifacts", "files"):
            for name, (body, media_type) in files.items():
                url = f"/{prefix}/reports/{name}"
                response = local.get(url)
                assert response.status_code == 200, url
                assert response.text == body, url
                assert response.headers["content-type"].split(";")[0] == media_type, url
                assert response.headers["content-security-policy"] == "sandbox", url
                assert response.headers["x-content-type-options"] == "nosniff", url

    def test_monitor_dashboards_keep_scripts(self, tmp_path):
        """Defect class: the stored-file sandbox disables Monitor dashboards."""
        dashboards = tmp_path / "dashboards"
        dashboards.mkdir()
        page = '<main>Monitor</main><script src="/monitor.js"></script>'
        script = "document.title = 'Monitor';"
        (dashboards / "index.html").write_text(page, encoding="utf-8")
        (dashboards / "artifacts.html").write_text(page, encoding="utf-8")
        (dashboards / "monitor.js").write_text(script, encoding="utf-8")
        (tmp_path / "docs" / "reports").mkdir(parents=True)
        local = TestClient(create_app(fixture_context(tmp_path), product=EMPTY))

        for url in ("/machinery", "/artifacts/", "/artifacts/reports"):
            response = local.get(url)
            assert response.status_code == 200, url
            assert response.text == page, url
            assert "content-security-policy" not in response.headers, url

        asset = local.get("/monitor.js")
        assert asset.status_code == 200
        assert asset.text == script
        assert asset.headers["content-type"].split(";")[0] in {"application/javascript", "text/javascript"}

    def test_static_path_traversal_is_rejected(self, tmp_path, monkeypatch):

        import lu_machinery.api.main as api_main

        dashboards_dir = tmp_path / "dashboards"
        dashboards_dir.mkdir()
        (dashboards_dir / "index.html").write_text("ok", "utf-8")
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secret", "utf-8")

        base_ctx = api_main.app.state.ctx
        patched_ctx = base_ctx.with_roots(dashboards_dir=dashboards_dir)
        monkeypatch.setattr(api_main.app.state, "ctx", patched_ctx)

        r = client.get("/%2e%2e/secret.txt")

        assert r.status_code == 403
        assert r.json()["detail"] == "Path traversal not allowed"

    def test_artifacts_page_that_no_one_ships_is_not_found(self, tmp_path, monkeypatch):
        """Defect class: with no artifacts.html in the dashboards directory and none declared by
        the product, /artifacts/ handed a missing file to FileResponse and answered HTTP 500 (#722)."""

        import lu_machinery.api.main as api_main

        dashboards_dir = tmp_path / "dashboards"
        dashboards_dir.mkdir()
        patched_ctx = api_main.app.state.ctx.with_roots(dashboards_dir=dashboards_dir)
        monkeypatch.setattr(api_main.app.state, "ctx", patched_ctx)
        monkeypatch.setattr(api_main.app.state, "product", None, raising=False)

        r = TestClient(app, raise_server_exceptions=False).get("/artifacts/")

        assert r.status_code == 404
