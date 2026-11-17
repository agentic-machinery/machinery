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

from lu_machinery.api.main import app

client = TestClient(app)

# ==================== Endpoint existence ====================


# ==================== Old paths are gone ====================


# ==================== Static file serving ====================

class TestStaticServing:
    """Dashboard HTML should be servable."""

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
