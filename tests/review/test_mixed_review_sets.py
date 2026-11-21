"""The mixed-family review pair sets: one definition, and every family on a critical seat."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import combinations

from lu_machinery.identity import resolve_author_family
from lu_machinery.orchestration.integration_sweep import _mixed_pair_pending
from lu_machinery.review.model_catalog import load_model_catalog
from lu_machinery.review.reviewer_resolver import MIXED_REVIEW_SETS, is_mixed_family_set

AT = datetime(2026, 11, 21, tzinfo=UTC)


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
