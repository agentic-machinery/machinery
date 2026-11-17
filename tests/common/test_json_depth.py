"""Reader behavior independent of the interpreter's accidental recursion limit."""

import io
import json

import pytest

from lu_machinery.common import json_depth


@pytest.mark.parametrize("limit", [128, 512])
@pytest.mark.parametrize("opener,closer", [("[", "]"), ('{"k":', "}")])
@pytest.mark.parametrize("reader", ["loads", "load", "raw_decode"])
def test_excess_depth_is_refused_before_the_interpreter_limit(limit, opener, closer, reader):
    def read(depth):
        text = opener * depth + "0" + closer * depth
        if reader == "load":
            return json_depth.load(io.StringIO(text), max_depth=limit)
        if reader == "raw_decode":
            return json_depth.raw_decode(text, max_depth=limit)[0]
        return json_depth.loads(text, max_depth=limit)

    # At the documented boundary, every container is retained.
    value = read(limit)
    for _ in range(limit):
        value = value[0] if opener == "[" else value["k"]
    assert value == 0
    with pytest.raises(RecursionError, match=f"JSON nesting exceeds {limit} levels"):
        read(limit + 1)


@pytest.mark.parametrize("value", [
    ' {"a":[true,null,1.25],"s":"\\\"[]{}"} ',
    '"\u2028\u2029"',
    b'\xef\xbb\xbf{"a":1}',
    '["x"]'.encode("utf-16"),
    bytearray(b'["x"]'),
])
def test_shallow_unicode_and_binary_inputs_keep_stdlib_values(value):
    assert json_depth.loads(value, max_depth=128) == json.loads(value)


@pytest.mark.parametrize("stream", [io.StringIO, io.BytesIO])
def test_stream_reader_preserves_decoder_hooks(stream):
    text = '{"n":12,"n":34}'
    source = stream(text if stream is io.StringIO else text.encode())
    assert json_depth.load(source, max_depth=128, object_pairs_hook=tuple, parse_int=str) == (("n", "12"), ("n", "34"))
    assert source.read() in ("", b"")


def test_raw_reader_keeps_offsets_and_decoder_options():
    decoder = json.JSONDecoder(parse_int=str)
    assert json_depth.raw_decode("xx[12] trailing", 2, max_depth=128, decoder=decoder) == (["12"], 6)


@pytest.mark.parametrize("text", ['{"a":1,}', '[x]', '"\\', '{} extra', '"\ufeff"'])
def test_syntax_and_string_handling_match_the_stdlib(text):
    try:
        expected = json.loads(text)
    except json.JSONDecodeError as original:
        with pytest.raises(json.JSONDecodeError) as bounded:
            json_depth.loads(text, max_depth=128)
        assert (bounded.value.msg, bounded.value.pos, bounded.value.doc) == (original.msg, original.pos, original.doc)
    else:
        assert json_depth.loads(text, max_depth=128) == expected


def test_syntax_before_excess_depth_keeps_its_original_error():
    text = "[!" + "[" * 600
    with pytest.raises(json.JSONDecodeError) as bounded:
        json_depth.loads(text, max_depth=128)
    assert bounded.value.pos == 1


def test_hook_failures_remain_typed_and_are_not_hidden_by_depth_refusal():
    class HookRefused(ValueError):
        pass

    def refuse(value):
        raise HookRefused("duplicate record")

    with pytest.raises(HookRefused, match="duplicate record"):
        json_depth.loads('[{} ,' + '[' * 600, max_depth=128, object_hook=refuse)
