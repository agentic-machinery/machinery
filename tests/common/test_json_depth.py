"""Bounded JSON decoding: ordinary documents stay fast and deep ones stay refused."""

import json
import json.scanner

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
    """Defect class: a slow path regression, where shallow documents fall back to the per-match Python scan (3x a plain decode)."""

    class Scan:
        def finditer(self, *args):
            raise AssertionError("per-match scan reached")

    monkeypatch.setattr(json_depth, "_TOKEN", Scan())
    text = json.dumps(document)
    assert json_depth.loads(text, max_depth=128) == document
    assert json_depth.raw_decode(text, max_depth=128) == (document, len(text))
    assert json_depth.loads(text, max_depth=128, object_pairs_hook=dict) == document


@pytest.mark.parametrize("limit", [4, 128])
@pytest.mark.parametrize("ensure_ascii", [True, False])
@pytest.mark.parametrize("decoys", [_DECOYS, ["]]]}}}", "т]"]], ids=["escapes", "no-escapes"])
def test_deep_document_with_brackets_inside_strings_is_refused_at_its_first_too_deep_bracket(limit, ensure_ascii, decoys):
    """Defect class: fail-open depth check, where brackets, quotes or backslashes inside strings hide real nesting."""
    opener = "[" + ", ".join(json.dumps(decoy, ensure_ascii=ensure_ascii) for decoy in decoys) + ", "
    text = opener * (limit + 1) + "0" + "]" * (limit + 1)
    message = f"JSON nesting exceeds {limit} levels at char {limit * len(opener)}"

    with pytest.raises(RecursionError, match=message):
        json_depth.loads(text, max_depth=limit)
    with pytest.raises(RecursionError, match=message):
        json_depth.raw_decode(text, max_depth=limit)
    assert json_depth.loads(text, max_depth=limit + 1) == json.loads(text)


def _hook_error(pos):
    def hook(pairs):
        raise json.JSONDecodeError("hook", "hook document", pos)

    return hook


def _python_scanner_decoder():
    decoder = json.JSONDecoder(object_pairs_hook=list)
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    return decoder


def test_decoder_sees_only_text_through_the_first_too_deep_bracket():
    """Defect class: fast-path drift, where the C-speed precheck vouches for text the per-match scan finds too deep.

    Then the decoder reads the whole document instead of the text through the first too-deep bracket, and a hook's or
    the decoder's error escapes unnormalized, or with another position or context.
    """
    limit = 4
    # A backslash outside any string, then an escaped backslash: deleting escape pairs would hide the brackets after it.
    head = '[{"a": 1}] \\"abc\\\\"'
    text = head + "[" * 10
    deep = len(head) + limit
    for call in (
        lambda hook: json_depth.loads(text, max_depth=limit, object_pairs_hook=hook),
        lambda hook: json_depth.raw_decode(text, max_depth=limit, decoder=json.JSONDecoder(object_pairs_hook=hook)),
    ):
        with pytest.raises(json.JSONDecodeError) as raised:
            call(_hook_error(0))
        assert (raised.value.msg, raised.value.doc, raised.value.pos) == ("hook", text, 0)
        with pytest.raises(RecursionError, match=f"JSON nesting exceeds {limit} levels at char {deep}$"):
            call(_hook_error(deep + 1))

    # A negative idx: slicing reads a shallow suffix, but the per-match scan starts at 0 and finds the deep prefix.
    text = "[" * 8 + "0" + "]" * 8 + "[0]" * 5
    with pytest.raises(json.JSONDecodeError) as raised:
        json_depth.raw_decode(text, -15, max_depth=limit, decoder=_python_scanner_decoder())
    assert (raised.value.msg, raised.value.doc, raised.value.pos) == ("Expecting value", text, -15)
    assert raised.value.__context__.doc == text[: limit + 1]
