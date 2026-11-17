"""Route labels on the Monitor HTTP metrics.

Defect class: two routes with the same suffix share a label, or a path
parameter leaks into the label.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lu_machinery.api.main import _route_template, create_app
from lu_machinery.api.monitor_context import fixture_context
from lu_machinery.api.product_contribution import ProductContribution

# A value that must never become a route label. It is a path parameter on
# two mounts of ``/tasks/{task_id}`` and on two mounts of ``/{path:path}``.
_LEAK = "route-label-leak-9f3c"

# Five requests. Two pairs share a suffix; the fifth does not.
_REQUESTS = (
    ("/api/delegate/tasks/" + _LEAK, "/api/delegate/tasks/{task_id}"),
    ("/api/coordination/tasks/" + _LEAK, "/api/coordination/tasks/{task_id}"),
    ("/artifacts/" + _LEAK, "/artifacts/{path:path}"),
    ("/files/" + _LEAK, "/files/{path:path}"),
    ("/api/health", "/api/health"),
)


@asynccontextmanager
async def _no_lifespan(_app: FastAPI) -> Iterator[None]:
    """Skip the production lifespan. It warms live services."""
    yield


def _sample(body: str, name: str, route: str) -> str:
    """The counter or histogram-count line for one GET route label."""
    return f'{name}{{method="GET",route="{route}",status='


def test_route_label_is_full_template(tmp_path: Path) -> None:
    """Defect class: two routes with the same suffix share a label, or a path parameter leaks into the label."""
    app = create_app(fixture_context(tmp_path), lifespan=_no_lifespan, product=ProductContribution())
    with TestClient(app) as client:
        for path, _template in _REQUESTS:
            client.get(path)
        # An unknown path matches the catch-all template, not its own text.
        raw_path = "/opsec-raw-path-should-not-be-labelled"
        missing = client.get(raw_path)
        assert missing.status_code == 404
        body = client.get("/metrics").text

    assert _LEAK not in body
    assert raw_path not in body
    for _path, template in _REQUESTS:
        # One request, one series. A shared suffix would add both into one sample.
        for metric in (
            "monitor_http_requests_total",
            "monitor_http_request_duration_seconds_count",
        ):
            lines = [sample for sample in body.splitlines() if sample.startswith(_sample(body, metric, template))]
            assert len(lines) == 1 and lines[0].endswith(" 1.0"), lines

    # The collapsed suffixes are not labels. ``route="/tasks/{task_id}"`` is not
    # a prefix of the full template's label, because the value is quoted.
    assert 'route="/tasks/{task_id}"' not in body
    assert 'route="/{path:path}"' in body  # the unknown path's catch-all, once
    catch_all = [
        sample
        for sample in body.splitlines()
        if sample.startswith('monitor_http_requests_total{method="GET",route="/{path:path}",status=')
    ]
    assert len(catch_all) == 1 and catch_all[0].endswith(" 1.0"), catch_all

    # No matched template: one fixed label, not the request path.
    assert _route_template({"type": "http", "method": "GET", "path": "/" + _LEAK}) == "unmatched"
    assert _route_template({"type": "http", "method": "GET", "path": "/metrics"}) == "/metrics"
