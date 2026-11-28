"""Incomplete review replies cannot authorize a merge (#12254)."""

import json
import re

import pytest
from lu_machinery.dispatcher.worker import _format_correction
from lu_machinery.review.record_cf_verdict import RecordError, normalize_verdict





def test_truncated_findings_block_beside_approve_is_inconclusive():
    """Defect class: a cut-off review reads as an approval."""
    reply = (
        'VERDICT: APPROVE\n\n```json\n'
        '{"schema_version":"code-review-findings.v1",'
        '"findings":[{"priority":"P1","body":"cut off'
    )
    with pytest.raises(RecordError, match=r"inconclusive.*findings"):
        normalize_verdict(reply)
    review = {"schema_version": 2, "require_review_verdict": True, "review_profile": "code"}
    assert _format_correction(review, reply, 0) is None


def test_reply_stating_incomplete_review_is_inconclusive():
    """Defect class: an unfinished review reads as an approval."""
    reply = "VERDICT: APPROVE\n\nI could not finish checking the lesson activities."
    with pytest.raises(RecordError, match=r"inconclusive.*incomplete"):
        normalize_verdict(reply)


def _approval(explanation="All checks pass.", **overall):
    return {
        "schema_version": "code-review-findings.v1",
        "overall": {"correctness": "correct", "explanation": explanation, "confidence": 0.9, "review_complete": True,
                    **overall},
        "findings": [],
    }


_GOOD = json.dumps(_approval())
_FENCE = "```json\n" + _GOOD + "\n```"


@pytest.mark.parametrize("raw, reason", [
    ("VERDICT: APPROVE\n\nAll checks pass.", "review_envelope_invalid_headers"),
    ("Here is my review.\n" + _GOOD, "non_json_or_chatter"),
    (_GOOD.replace('"review_complete": true', '"review_complete": false, "review_complete": true'),
     "review_json_unreadable"),
    (_GOOD[:-1] + ', "findings": []}', "review_json_unreadable"),
    (json.dumps({key: value for key, value in _approval().items() if key != "schema_version"}),
     "schema_violation:\\(root\\)"),
    (_GOOD.replace("code-review-findings.v1", "code-review-findings.v2"), "schema_violation:schema_version"),
    (json.dumps({**_approval(), "findings": [], "nested": {"overall": {"review_complete": True}}}),
     "schema_violation:\\(root\\)"),
    (json.dumps(_approval('{"overall": {"review_complete": true}}', review_complete=False)),
     "review_completion_not_attested"),
    (_GOOD + "\n" + _GOOD, "non_json_or_chatter"),
    (_FENCE + "\n\n" + _FENCE, "non_json_or_chatter"),
    ("```json\n" + _GOOD + "\n", "non_json_or_chatter"),
    ("VERDICT: REQUEST_CHANGES\nSCORE: 9/10\n\n" + _FENCE + "\n", "review_envelope_verdict_mismatch"),
    ("VERDICT: APPROVE\nSCORE: 9/10\n\n" + _FENCE + "\n\n" + json.dumps(_approval(review_complete=False)),
     "review_envelope_requires_one_json_block"),
    ("VERDICT: APPROVE\nSCORE: 9/10\n\n" + json.dumps(_approval(correctness="incorrect")) + "\n\n" + _FENCE,
     "review_envelope_requires_one_json_block"),
    ("VERDICT: APPROVE\nSCORE: 9/10\n\n" + _FENCE + '\n\n{"missing_capabilities": ["shell"]}',
     "review_envelope_requires_one_json_block"),
    ("VERDICT: APPROVE\nSCORE: 9/10\n\n" + _FENCE + "\n\n" + _GOOD[:-1] + ', "findings": []}',
     "review_envelope_unreadable"),
], ids=["prose-only", "prose-then-object", "duplicate-key-nested", "duplicate-key-root", "no-schema-version",
        "unsupported-schema-version", "nested-attestation", "quoted-attestation", "two-objects", "two-fences",
        "unclosed-fence", "envelope-verdict-mismatch", "envelope-unfenced-false", "envelope-unfenced-incorrect",
        "envelope-unfenced-capabilities", "envelope-unfenced-duplicate-key"])
def test_strict_parser_refuses_untyped_or_ambiguous_approval(raw, reason):
    """Defect class: lossy or permissive decode admits an approval.

    Receipt sealing and approval recording read the reply with the same strict reader and refuse each shape
    by the same typed reason; bare JSON, one whole closed fence and the review envelope still approve.
    """
    from lu_machinery.review.record_cf_verdict import _completion_refusal
    from lu_machinery.review.review_contract import ContractError, canonical_receipt_payload

    with pytest.raises(ContractError, match=rf"^{reason}"):
        canonical_receipt_payload(raw)
    assert re.match(reason, _completion_refusal(raw) or "")
    envelope = "VERDICT: APPROVE\nSCORE: 9/10\n\n" + _FENCE + "\n"
    for accepted in (_GOOD, _FENCE, envelope):
        assert canonical_receipt_payload(accepted) == _approval()
        assert _completion_refusal(accepted) is None


_ADMISSION = "I could not finish the review."


@pytest.mark.parametrize("explanation, blocks", [
    ("Summary\n" + _ADMISSION, True),
    (f"<div>Summary<hgroup>{_ADMISSION}</hgroup></div>", True),
    (f"<div>Summary<fieldset>{_ADMISSION}</fieldset></div>", True),
    (f"|a|b|\n|-|-|\n|ok|{_ADMISSION}|", True),
    (f"a|b\n-|-\nok|{_ADMISSION}", True),
    (f"The fixture is `{_ADMISSION}` and passes.", False),
    (f'The fixture says: "Expected \\| {_ADMISSION}"', False),
    (f'| a | b |\n|---|---|\n| ok | The fixture says: "Expected \\| {_ADMISSION}" |', False),
], ids=["plain", "html-hgroup", "html-fieldset", "gfm-unpadded", "gfm-no-outer-pipes", "code-span",
        "escaped-pipe-quote", "escaped-pipe-cell"])
def test_admission_contradiction_blocks_and_quoted_example_passes(explanation, blocks):
    """Control: an attested approval whose own prose admits the review is unfinished still blocks; a quoted one passes."""
    from lu_machinery.review.record_cf_verdict import _completion_refusal
    from lu_machinery.review.review_contract import ContractError, canonical_receipt_payload

    raw = json.dumps(_approval(explanation))
    if blocks:
        with pytest.raises(ContractError, match=r"^review_declares_incomplete"):
            canonical_receipt_payload(raw)
        assert _completion_refusal(raw) == "review_declares_incomplete"
    else:
        assert canonical_receipt_payload(raw) == _approval(explanation)
        assert _completion_refusal(raw) is None
