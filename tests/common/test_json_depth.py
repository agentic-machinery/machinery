"""Bounded JSON decoding: ordinary documents stay fast and deep ones stay refused."""

import json

import pytest

from lu_machinery.common import json_depth

# Strings whose brackets, escaped quotes and escaped backslashes a string-blind count or a
# naive quote split would misread.
_DECOYS = ["[[[{{{", "]]]}}}", 'q"]]]]]]', "back\\", '\\"[{', "\\\\[", "\u0442[\ud800"]


@pytest.mark.parametrize(
    "document",
    [
        {"task": "t", "attempts": [1, 2, {"ok": True}]},
        [{"id": index, "note": _DECOYS[index % len(_DECOYS)], "tags": ["a", ["b"]]} for index in range(600)],
    ],
    ids=["under-the-bracket-count", "many-brackets-shallow"],
)
def test_shallow_documents_never_reach_the_per_token_scan(monkeypatch, document):
    """Defect class: a slow path regression, where shallow documents fall back to the per-token Python scan (3x a plain decode)."""

    def scan(*args):
        raise AssertionError("per-token scan reached")

    monkeypatch.setattr(json_depth, "_first_too_deep", scan)
    text = json.dumps(document)
    assert json_depth.loads(text, max_depth=128) == document
    assert json_depth.raw_decode(text, max_depth=128) == (document, len(text))
    assert json_depth.loads(text, max_depth=128, object_pairs_hook=dict) == document


@pytest.mark.parametrize("limit", [4, 128])
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_deep_document_with_brackets_inside_strings_is_refused_at_its_first_too_deep_bracket(limit, ensure_ascii):
    """Defect class: fail-open depth check, where brackets, quotes or backslashes inside strings hide real nesting."""
    opener = "[" + ", ".join(json.dumps(decoy, ensure_ascii=ensure_ascii) for decoy in _DECOYS) + ", "
    text = opener * (limit + 1) + "0" + "]" * (limit + 1)
    message = f"JSON nesting exceeds {limit} levels at char {limit * len(opener)}"

    with pytest.raises(RecursionError, match=message):
        json_depth.loads(text, max_depth=limit)
    with pytest.raises(RecursionError, match=message):
        json_depth.raw_decode(text, max_depth=limit)
    assert json_depth.loads(text, max_depth=limit + 1) == json.loads(text)
