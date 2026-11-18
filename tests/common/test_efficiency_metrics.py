"""Sol PR-M efficiency metrics (no content leakage)."""

from __future__ import annotations

from lu_machinery.fleet_comms.efficiency_metrics import (
    _fetch_merge_facts,
)


def test_merge_fact_query_uses_production_rest_transport(github_transport) -> None:
    from lu_machinery.common.github_client import Response

    def transport(method, endpoint, headers, body, timeout):
        assert method == "GET" and body is None
        return Response(200, {}, b'{"merged_at":"2026-09-20T12:34:56Z"}')

    calls = github_transport(transport)
    result = _fetch_merge_facts([("learn-ukrainian/learn-ukrainian.github.io", 123)])

    assert result[("learn-ukrainian/learn-ukrainian.github.io", 123)][0] is not None
    assert len(calls) == 1
    assert calls[0][1] == "repos/learn-ukrainian/learn-ukrainian.github.io/pulls/123"
    assert calls[0][4] == 30
