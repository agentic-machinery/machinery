"""Catch argument-guard bypass across the publisher's declared refusal classes.

Only pure argument validators run here; no GitHub transport is invoked.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lu_machinery.opsec.prepublish import PublishBlocked
from lu_machinery.publish import github as pub

TEXT = st.text(alphabet="abcXYZ019 _-./", max_size=24)
WRONG_TYPE = st.one_of(st.none(), st.integers(), st.booleans(), st.lists(st.integers(), max_size=2))
VALID = {
    "text": TEXT,
    "path": TEXT,
    "bool": st.booleans(),
    "count": st.integers(min_value=0, max_value=100),
    "number": st.integers(min_value=1, max_value=10**9),
    "file": st.one_of(TEXT, TEXT.map(Path)),
    "texts": st.lists(TEXT, max_size=3),
    "assets": st.lists(st.builds(pub.Asset, st.just("artifact.txt")), min_size=1, max_size=2),
    "workflow_inputs": st.just({}),
    **{kind: st.sampled_from(sorted(values)) for kind, values in pub.ENUMS.items()},
    **{kind: st.from_regex(pattern, fullmatch=True) for kind, pattern in pub.PATTERNS.items()},
}
KINDS = sorted({kind for _, schema in pub.SCHEMAS.values() for kind in schema.values()})


def valid_fields(data, verb: str) -> dict:
    required, schema = pub.SCHEMAS[verb]
    fields = {name: data.draw(VALID[schema[name]], label=name) for name in sorted(required)}
    if verb == "pr-create":
        fields.update(base="main", head="feature")
    if verb == "ruleset-merge-queue":
        fields["merge_method"] = "SQUASH"
    return fields


def invalid_for(kind: str):
    if kind in pub.PATTERNS:
        return st.one_of(WRONG_TYPE, VALID[kind].map(lambda value: value + "\0"))
    if kind in pub.ENUMS:
        return st.one_of(WRONG_TYPE, st.just("not-an-enum-value"))
    return {
        "bool": st.one_of(st.none(), st.integers(), TEXT),
        "count": st.one_of(st.booleans(), st.integers(max_value=-1), TEXT),
        "number": st.one_of(st.booleans(), st.integers(max_value=0), TEXT),
        "file": WRONG_TYPE,
        "texts": st.one_of(TEXT, st.just([None]), st.just([1])),
        "assets": st.one_of(st.just([]), TEXT, st.just(["artifact.txt"])),
        "workflow_inputs": st.one_of(WRONG_TYPE, st.just({"unknown": "value"})),
        "text": st.one_of(WRONG_TYPE, TEXT.map(lambda value: value + "\0")),
        "path": st.one_of(WRONG_TYPE, TEXT.map(lambda value: value + "\0")),
    }[kind]


@pytest.mark.parametrize("kind", KINDS)
@given(data=st.data())
def test_each_field_class_accepts_valid_and_refuses_invalid(kind: str, data) -> None:
    # New schema kinds must get explicit generation rather than disappearing.
    assert set(KINDS) <= set(VALID)
    pairs = sorted((verb, key) for verb, (_, schema) in pub.SCHEMAS.items() for key, value in schema.items() if value == kind)
    verb, key = data.draw(st.sampled_from(pairs))
    fields = valid_fields(data, verb)
    fields[key] = data.draw(VALID[kind])
    if key in {"body_file", "notes_file"}:
        fields.pop(key.removesuffix("_file"), None)
    if verb == "pr-create" and key in {"base", "head"} and not fields[key]:
        fields[key] = "main"
    if verb == "pr-create" and key == "milestone" and not fields[key].strip():
        fields[key] = "v0.11.0"
    if verb == "pr-merge" and key == "subject" and not fields[key].strip():
        fields[key] = "Subject"
    if verb == "ruleset-merge-queue" and key in pub.MERGE_QUEUE_INT_BOUNDS:
        low, high = pub.MERGE_QUEUE_INT_BOUNDS[key]
        fields[key] = data.draw(st.integers(min_value=low, max_value=high))
    # Exercise workflow argument validation independently of checkout YAML.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pub, "_workflow_input_schema", lambda workflow: {})
        pub._validate(verb, fields)
        fields[key] = data.draw(invalid_for(kind))
        with pytest.raises(PublishBlocked):
            pub._validate(verb, fields)


@given(data=st.data())
def test_unknown_verbs_fields_and_missing_required_arguments_are_refused(data) -> None:
    verb = data.draw(st.sampled_from(sorted(pub.SCHEMAS)))
    fields = valid_fields(data, verb)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pub, "_workflow_input_schema", lambda workflow: {})
        with pytest.raises(PublishBlocked):
            pub._validate("unknown-verb", fields)
        with pytest.raises(PublishBlocked):
            pub._validate(verb, {**fields, "unknown_field": "value"})
        required = sorted(pub.SCHEMAS[verb][0])
        del fields[data.draw(st.sampled_from(required))]
        with pytest.raises(PublishBlocked):
            pub._validate(verb, fields)


@pytest.mark.parametrize("key", sorted(pub.MERGE_QUEUE_INT_BOUNDS))
@given(data=st.data())
def test_merge_queue_bounds_refuse_out_of_range_integers(key: str, data) -> None:
    low, high = pub.MERGE_QUEUE_INT_BOUNDS[key]
    value = data.draw(st.one_of(st.integers(max_value=low - 1), st.integers(min_value=high + 1)))
    with pytest.raises(PublishBlocked):
        pub._validate("ruleset-merge-queue", {"ruleset": 1, key: value})


@pytest.mark.parametrize("verb,fields", [
    ("pr-create", {"title": "T", "body": "B", "base": "", "head": "feature"}),
    ("pr-create", {"title": "T", "body": "B", "base": "main", "head": ""}),
    ("ruleset-merge-queue", {"ruleset": 1}),
    ("pr-comment", {"number": 1, "body": "B", "body_file": "body.md"}),
    ("release-create", {"tag": "v1", "notes": "N", "notes_file": "notes.md"}),
])
def test_cross_field_refusal_classes(verb: str, fields: dict) -> None:
    with pytest.raises(PublishBlocked):
        pub._validate(verb, fields)


@given(blank=st.text(alphabet=" \t\r\n", max_size=20))
def test_blank_merge_subjects_are_refused(blank: str) -> None:
    with pytest.raises(PublishBlocked):
        pub._validate("pr-merge", {"number": 1, "subject": blank})


@given(
    bad=st.one_of(
        st.text(alphabet=" \t\r\n", max_size=20),
        st.sampled_from(["v1\nline2", "v1\r", "v1\x00", "v1\x7f", "v1\u2028", "v1\u2029", "v1\u202e", "v1\u200b"]),
    )
)
def test_pr_create_blank_or_control_milestones_are_refused(bad: str) -> None:
    """Defect class: pr-create accepts blank or control/bidi-character milestone strings before transport."""
    with pytest.raises(PublishBlocked, match=r"MILESTONE_REFUSED|invalid publisher field milestone"):
        pub._validate(
            "pr-create",
            {"title": "T", "body": "B", "base": "main", "head": "feature", "milestone": bad},
        )


def test_pr_create_milestone_matching_and_closing_issue_extraction() -> None:
    """Defect class: missing, closed, or ambiguous milestones are accepted, or quoted code/comments link false issues."""
    pages = [[
        {"number": 13, "title": "v0.11.0 - CI and GitHub budget reliability", "state": "open"},
        {"number": 2, "title": "13", "state": "open"},
        {"number": 4, "title": "v0.9.0 - Shipped", "state": "closed"},
    ]]
    assert pub.match_milestone_pages(pages, "v0.11.0 - CI and GitHub budget reliability", repo="unit/public") == (
        13, "v0.11.0 - CI and GitHub budget reliability",
    )
    with pytest.raises(PublishBlocked, match="MILESTONE_AMBIGUOUS"):
        pub.match_milestone_pages(pages, "13", repo="unit/public")
    with pytest.raises(PublishBlocked, match="MILESTONE_REFUSED"):
        pub.match_milestone_pages(pages, "v0.9.0 - Shipped", repo="unit/public")
    with pytest.raises(PublishBlocked, match="MILESTONE_REFUSED"):
        pub.match_milestone_pages(pages, "nonexistent", repo="unit/public")

    body = (
        "Summary\n\n"
        "<!-- Closes #666 -->\n"
        "```md\nCloses #999\n```\n"
        "Example: `Fixes #888`\n"
        "Closes other/repo#777\n"
        "Fixes #42\n"
        "Resolves unit/public#43\n"
        "Closes https://github.com/unit/public/issues/42\n"
    )
    assert pub._pr_closing_issues(body, "unit/public") == [42, 43]


@given(name=st.text(alphabet="abcXYZ019_.-", min_size=1, max_size=20), bad=st.sampled_from([*map(chr, range(32)), "\x7f", "/", "\\", "#"]))
def test_asset_names_refuse_separator_control_and_fragment_injection(name: str, bad: str) -> None:
    with pytest.raises(PublishBlocked):
        pub.Asset("artifact.txt", name=name + bad)
    assert pub.Asset("artifact.txt", name="asset-" + name).name == "asset-" + name


@pytest.mark.parametrize("kind", ["bool", "choice", "sha"])
@given(data=st.data())
def test_workflow_input_types_and_choices_are_enforced(kind: str, data) -> None:
    schema = ("choice", ("one", "two")) if kind == "choice" else kind
    valid = data.draw(st.sampled_from(["one", "two"]) if kind == "choice" else VALID[kind])
    assert pub._workflow_input_accepted(schema, valid)
    invalid = data.draw(st.one_of(st.none(), st.integers(), st.just("invalid\0")))
    assert not pub._workflow_input_accepted(schema, invalid)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pub, "_workflow_input_schema", lambda workflow: {"required": (schema, True)})
        fields = {"workflow": "ci.yml", "ref": "main", "inputs": {"required": valid}}
        pub._validate("workflow-run", fields)
        for inputs in ({}, {"required": invalid}, {"required": valid, "unknown": valid}):
            with pytest.raises(PublishBlocked):
                pub._validate("workflow-run", {**fields, "inputs": inputs})


@pytest.mark.parametrize("verb", sorted(pub.COMMENT_FIELDS))
@given(prefix=TEXT, suffix=TEXT)
def test_verdict_markers_require_the_recorder_carrier(verb: str, prefix: str, suffix: str) -> None:
    text = prefix + "\n" + pub.CF_VERDICT_MARKER_PREFIX + suffix
    with pytest.raises(PublishBlocked):
        pub._refuse_verdict_markers(verb, {pub.COMMENT_FIELDS[verb]: text})
    record = pub.CfVerdictRecord(text)
    if verb == pub.CF_VERDICT_RECORD_VERB:
        pub._refuse_verdict_markers(verb, {pub.COMMENT_FIELDS[verb]: record})
    else:
        with pytest.raises(PublishBlocked):
            pub._refuse_verdict_markers(verb, {pub.COMMENT_FIELDS[verb]: record})
    for malformed in (prefix, text + "\ntrailing text", text + text):
        with pytest.raises(PublishBlocked):
            pub.CfVerdictRecord(malformed)


@given(label=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=15), bad=st.sampled_from(["@", ":443", " ", "\t", "\n", "\0", "\u202e"]))
def test_environment_hostname_rejects_argument_injection(label: str, bad: str) -> None:
    host = label + ".example"
    assert pub._validated_environment({"GH_HOST": host.upper()}) == {"GH_HOST": host}
    with pytest.raises(PublishBlocked):
        pub._validated_environment({"GH_HOST": host + bad})
