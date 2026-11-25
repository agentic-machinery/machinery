"""Unit tests for canonical formal-review verdict and evidence parsing."""

from __future__ import annotations

import pytest

from lu_machinery.fleet_comms.review_publication import (
    ReviewPublicationError,
    normalize_verdict,
    parse_verdict_token,
    resolve_verdict_and_evidence,
)

# ── verdict parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("APPROVED", "APPROVED"),
        ("approved", "APPROVED"),
        ("CHANGES_REQUESTED", "CHANGES_REQUESTED"),
        ("changes_requested", "CHANGES_REQUESTED"),
        ("BLOCKED", "BLOCKED"),
        ("  blocked  ", "BLOCKED"),
    ],
)
def test_normalize_verdict_accepts_canonical_states(raw: str, expected: str) -> None:
    assert normalize_verdict(raw) == expected


def test_parse_verdict_token_from_text() -> None:
    body = "notes...\nVERDICT: CHANGES_REQUESTED\nmore"
    assert parse_verdict_token(body) == "CHANGES_REQUESTED"


def test_parse_verdict_token_missing() -> None:
    with pytest.raises(ReviewPublicationError, match="verdict_missing"):
        parse_verdict_token("no formal marker here")


@pytest.mark.parametrize("correctness", ["correct", "incorrect", "uncertain"])
def test_degenerate_review_evidence_is_always_blocked(correctness: str) -> None:
    """A bare reviewer label cannot grant or deny the formal merge gate."""
    verdict, evidence = resolve_verdict_and_evidence(
        {
            "schema_version": "code-review-findings.v1",
            "verdict": "BLOCKED",
            "overall": {
                "correctness": correctness,
                "explanation": "",
                "confidence": 0.9,
            },
            "findings": [],
        }
    )

    assert verdict == "BLOCKED"
    assert evidence is None


def test_degenerate_review_evidence_rejects_an_explicit_approved_verdict() -> None:
    with pytest.raises(ReviewPublicationError, match="review_evidence_verdict_mismatch"):
        resolve_verdict_and_evidence(
            {
                "schema_version": "code-review-findings.v1",
                "verdict": "APPROVED",
                "overall": {
                    "correctness": "correct",
                    "explanation": "",
                    "confidence": 0.9,
                },
                "findings": [],
            }
        )
