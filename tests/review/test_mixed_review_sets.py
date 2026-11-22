"""The mixed-family review pair sets: one definition, and every family on a critical seat."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from itertools import combinations

from lu_machinery.identity import resolve_author_family
from lu_machinery.orchestration.integration_sweep import _mixed_pair_pending
from lu_machinery.review.model_catalog import load_model_catalog
from lu_machinery.review.reviewer_resolver import MIXED_REVIEW_SETS, is_mixed_family_set

AT = datetime(2026, 11, 21, tzinfo=UTC)
# Adds one pair set before any reader imports the definition, then records and reads back a
# scoped marker for every pair set through the recorder and the sweep's parser.
_PAIR_TOKEN_PROBE = r"""
import json, sys
from lu_machinery.review import mixed_review_sets
models = json.loads(sys.argv[1])
mixed_review_sets.MIXED_REVIEW_SETS = mixed_review_sets.MIXED_REVIEW_SETS | {frozenset(sys.argv[2].split(","))}
from lu_machinery.orchestration.integration_sweep import parse_marker
from lu_machinery.review.record_cf_verdict import build_comment
accepted = {}
for pair in mixed_review_sets.MIXED_REVIEW_SETS:
    family = min(pair)
    body = build_comment(sha="a" * 40, task_id="pair-token-probe", started="2026-11-21T00:00:00.000000+00:00",
                         verdict="APPROVED", model=models[family], family=family, reply="ok",
                         author_models=[models[member] for member in sorted(pair)], scope_commits=["b" * 40])
    marker = parse_marker(body)
    accepted["_".join(sorted(pair))] = marker is not None and marker.get("scope_commits") == ("b" * 40,)
print(json.dumps(accepted, sort_keys=True))
"""


def test_sweep_pair_membership_drift_from_resolver_is_caught() -> None:
    """Defect: the sweep's marker reader and the resolver disagree on which author sets take the pair.

    Every subset of the catalog's attestable families goes through the sweep's real membership
    check (an unscoped trusted marker is pending only for a pair set) and the resolver's.
    """
    catalog = load_model_catalog()
    model_for: dict[str, str] = {}
    for model_id, model in catalog["models"].items():
        if resolve_author_family(model_id) == model["family"]:
            model_for.setdefault(model["family"], model_id)
    assert frozenset().union(*MIXED_REVIEW_SETS) <= model_for.keys()
    families = sorted(model_for)
    for size in range(1, len(families) + 1):
        for subset in combinations(families, size):
            marker = {"author_models": [model_for[family] for family in subset], "verdict": "APPROVED"}
            assert _mixed_pair_pending([(AT, (AT, 0), marker)]) is is_mixed_family_set(subset), subset


def test_marker_parser_accepts_the_token_of_every_pair_set() -> None:
    """Defect: parse_marker rejects the token of a pair set in MIXED_REVIEW_SETS.

    The sweep's marker pattern spelled the pair tokens by hand, a copy of the sets: a pair set
    added to the one definition had its recorded markers refused. A synthetic two-family set
    stands in for that addition.
    """
    catalog = load_model_catalog()
    model_for: dict[str, str] = {}
    for model_id, model in catalog["models"].items():
        if resolve_author_family(model_id) == model["family"]:
            model_for.setdefault(model["family"], model_id)
    added = next(
        frozenset(pair) for pair in combinations(sorted(model_for), 2) if frozenset(pair) not in MIXED_REVIEW_SETS
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PAIR_TOKEN_PROBE, json.dumps(model_for), ",".join(sorted(added))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    tokens = {"_".join(sorted(pair)) for pair in (*MIXED_REVIEW_SETS, added)}
    assert json.loads(result.stdout) == dict.fromkeys(sorted(tokens), True)


def test_pair_set_family_without_critical_seat_is_caught() -> None:
    """Defect: a pair set names a family that the catalog's critical review tier does not seat."""
    catalog = load_model_catalog()
    candidates = catalog["review_candidates"]
    seated = {
        catalog["models"][candidates[name]["model_id"]]["family"]
        for rung in catalog["review_ladders"]["critical"]
        for name in rung
    }
    for pair in MIXED_REVIEW_SETS:
        assert pair <= seated, sorted(pair - seated)
