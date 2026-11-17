"""Catch parser/renderer drift (trailer respelling) and malformed declarations."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lu_machinery.ci.check_agent_attribution import Attribution, extract_attribution

AGENT_SEGMENT = st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True)
AGENT = st.lists(AGENT_SEGMENT, min_size=1, max_size=3).map("-".join)
TASK = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-", min_size=1, max_size=24)
VALUE = st.tuples(AGENT, TASK).map("/".join)


@given(values=st.lists(VALUE, min_size=1, max_size=6), crlf=st.booleans(), repeat=st.booleans())
def test_declaration_parse_render_parse_identity(values: list[str], crlf: bool, repeat: bool) -> None:
    expected = tuple(dict.fromkeys(values))
    lines = [f"X-Agent: {value}" for value in values]
    if repeat:
        lines += lines
    message = "Subject\n\n" + "\n".join([*lines, "X-Agent-Model: model"]) + "\n\nBody."
    parsed = extract_attribution(message.replace("\n", "\r\n") if crlf else message)
    assert parsed == Attribution(expected, 0, None)
    rendered = "\n".join(f"X-Agent: {value}" for value in parsed.values)
    assert extract_attribution(rendered) == parsed


@pytest.mark.parametrize("mutation", ["key-case", "no-space", "extra-space", "trailing-space", "bad-agent", "bad-task", "extra-slash"])
@given(value=VALUE)
def test_malformed_declarations_never_silently_pass(mutation: str, value: str) -> None:
    line = {
        "key-case": f"x-agent: {value}",
        "no-space": f"X-Agent:{value}",
        "extra-space": f"X-Agent:  {value}",
        "trailing-space": f"X-Agent: {value} ",
        "bad-agent": f"X-Agent: 0{value}",
        "bad-task": f"X-Agent: {value}!",
        "extra-slash": f"X-Agent: {value}/extra",
    }[mutation]
    parsed = extract_attribution(line)
    assert parsed.values == ()
    assert parsed.malformed == 1 or parsed.refusal is not None


@pytest.mark.parametrize("wrapper", ["```\n{}\n```", "    {}", "> {}", "- {}", "# {}", "[ref]: {}"])
@given(value=VALUE)
def test_markdown_examples_are_never_declarations(wrapper: str, value: str) -> None:
    assert extract_attribution(wrapper.format(f"X-Agent: {value}")).values == ()


@pytest.mark.parametrize("forbidden", list("\0\v\f\x1c\x1d\x1e\x85\u2028\u2029\r") + ["<!--", "-->"])
@given(value=VALUE)
def test_refused_messages_cannot_hide_valid_declarations(forbidden: str, value: str) -> None:
    parsed = extract_attribution(f"X-Agent: {value}\n\n{forbidden}")
    assert parsed.values == ()
    assert parsed.refusal is not None
