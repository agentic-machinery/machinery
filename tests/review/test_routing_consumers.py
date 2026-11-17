"""PR 2 consumer migration: references, rotations and file-path error identity."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
import pytest
import yaml
from lu_machinery.review import model_catalog
from lu_machinery.review.family_exclusions import family_exclusion
from lu_machinery.review.model_catalog import CATALOG_PATH, ModelCatalogError, bounded_execution_policy, cursor_pinned_models, load_model_catalog, validate_catalog
from lu_machinery.review.role_resolution import expand_routing_references, resolve_routing_reference
from tests.helpers import machinery_package as machinery_package
from lu_machinery import model_families
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.helpers import current_sol

CURRENT_SOL = current_sol()

def raw_catalog():
    return yaml.safe_load(CATALOG_PATH.read_text())


@pytest.mark.parametrize(
    "seat,new_id",
    [
        ("openai_frontier", "gpt-99-sol"),
        ("anthropic_authority", "claude-opus-99"),
        ("xai_reviewer", "grok-99"),
        ("bounded_worker", "gpt-99-luna"),
        ("google_bounded", "gemini-99-flash-high"),
        ("anthropic_practical", "claude-sonnet-99"),
        ("local_reviewer", "glm-99"),
        ("cursor_coder", "composer-99"),
        ("volume_reviewer", "poolside/laguna-s-99"),
        ("light_reviewer", "poolside/laguna-xs-99"),
    ],
)
def test_one_holder_edit_moves_all_consumer_references(seat, new_id):
    raw = raw_catalog()
    if seat not in raw["seats"]:
        with pytest.raises(ModelCatalogError, match="exactly one"):
            resolve_routing_reference({"role": "legacy_reviewers", "seat": seat}, raw)
        return
    old_id = raw["seats"][seat]["model_id"]
    replacement = deepcopy(raw["models"][old_id])
    replacement["aliases"] = []
    replacement["runtime_model_ids"] = [new_id]
    replacement["routing_wire_ids"] = {
        transport: new_id + ("-high" if wire.endswith("-high") and transport == "cursor" else "")
        for transport, wire in replacement["routing_wire_ids"].items()
    }
    if "glm_routes" in replacement:
        replacement["glm_routes"]["glmcc_alias"] = new_id
    raw["models"][new_id] = replacement
    raw["seats"][seat]["model_id"] = new_id
    result = validate_catalog(raw)
    baseline = validate_catalog(raw_catalog())

    # No migration caller rebinds a role, ladder, endpoint or default.
    def changed_ids(before, after):
        if isinstance(before, dict):
            for key in before:
                if key == "transport_fallback_for":
                    continue  # Receipt labels remain immutable until PR 4.
                yield from changed_ids(before[key], after[key])
        elif isinstance(before, list):
            for left, right in zip(before, after, strict=True):
                yield from changed_ids(left, right)
        elif isinstance(before, str) and before == old_id:
            yield after

    sections = (
        "execution_routing",
        "orchestrator_seats",
        "formal_cf_defaults",
        "review_candidates",
        "review_scheduler",
    )
    moved = [value for section in sections for value in changed_ids(baseline[section], result[section])]
    assert moved, "holder edit did not reach any consumer"
    assert set(moved) == {new_id}, "a consumer retained the predecessor's holder"
    for route in raw["seats"][seat]["routes"].values():
        label = route.get("legacy_name")
        if label:
            assert result["review_candidates"][label]["model_id"] == new_id
            assert result["review_candidates"][label]["route"] == baseline["review_candidates"][label]["route"]
    if seat == "openai_frontier":
        policy = bounded_execution_policy(result)
        assert policy.advisor_model_id == new_id
        assert result["execution_routing"]["sol_advised_bounded"]["preferred_worker"]["escalate_to"] == new_id
        assert result["formal_cf_defaults"]["codex"]["model_id"] == new_id
        assert result["review_scheduler"]["endpoints"]["codex"]["models"] == [new_id]
        assert result["budget_substitution_models"]["codex"][old_id] == new_id
    if seat == "anthropic_authority":
        # D-129 (#10464): no Claude model reviews through Cursor, so Opus has no Cursor substitution row.
        assert not {old_id, new_id} & set(result["budget_substitution_models"].get("cursor", {}))
    if seat == "bounded_worker":
        assert bounded_execution_policy(result).bounded_worker_model_id == new_id
    if seat == "google_bounded":
        assert result["orchestrator_seats"]["agy"]["model_id"] == new_id
        assert result["orchestrator_seats"]["agy"]["escalate_model_id"] == new_id
        assert result["formal_cf_defaults"]["agy"]["model_id"] == new_id
    # The xai_reviewer seat and the Cursor seat it pinned retired with Grok (GROK-CANCEL-1110).
    assert "cursor" not in result["orchestrator_seats"]
    assert cursor_pinned_models(result) == ()


@pytest.mark.parametrize(
    "reference",
    [
        {"role": "missing"},
        {"role": "legacy_reviewers"},
        {"role": "legacy_reviewers", "seat": "missing"},
        {"role": "openai_frontier_coder", "transport": "cursor"},
        {"role": "openai_frontier_coder", "field": "unsupported"},
        {"role": "openai_frontier_coder", "extra": True},
        {"role": "legacy_reviewers", "field": "candidate"},
    ],
)
def test_unresolved_or_ambiguous_references_fail_closed(reference):
    with pytest.raises(ModelCatalogError):
        resolve_routing_reference(reference, raw_catalog())


def test_expansion_preserves_inputs_and_explicit_identity_pins():
    raw = raw_catalog()
    before = deepcopy(raw)
    expanded = expand_routing_references(raw)
    assert raw == before
    assert CURRENT_SOL in expanded["budget_substitution_models"]["codex"]
    assert "openai_frontier_coder" not in expanded["budget_substitution_models"]["codex"]
    assert expanded["models"] == raw["models"]


@pytest.mark.parametrize("bad", ["bad-version", None])
@pytest.mark.parametrize("entrypoint", ["file", "module"])
def test_catalog_file_path_role_validation_is_clean_exit_two(tmp_path, bad, entrypoint):
    source = Path(model_catalog.__file__).resolve().parent
    package = machinery_package()
    review = tmp_path / package / "review"
    review.mkdir(parents=True)
    # A complete fixture package must outrank the installed regular package
    # when the CLI child resolves lu_machinery.review.model_catalog.
    (tmp_path / package / "__init__.py").write_text("")
    for name in ("model_catalog.py", "role_resolution.py", "family_exclusions.py", "ukrainian_seats.py", "__init__.py"):
        shutil.copyfile(source / name, review / name)
    config = tmp_path / package / "config"
    config.mkdir()
    raw = raw_catalog()
    raw["routing_schema_version"] = bad
    (config / "model_catalog.yaml").write_text(yaml.safe_dump(raw))
    command = [str(review / "model_catalog.py")] if entrypoint == "file" else ["-m", "lu_machinery.review.model_catalog"]
    result = subprocess.run(
        [sys.executable, *command, "--check-retired-model", CURRENT_SOL],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "routing_schema_version" in result.stderr
    assert "Traceback" not in result.stderr


def test_legacy_allowlist_cannot_expand_from_injected_cursor_candidates():
    # The approved Cursor allowlist is empty since Grok retired (GROK-CANCEL-1110); injecting an
    # active Cursor model, its review candidate and an allowlisted seat must not widen it.
    catalog = deepcopy(load_model_catalog())
    new_id = "grok-99"
    model = deepcopy(catalog["models"]["grok-4.7"])
    model.update(
        aliases=[], lifecycle="active", transports=["cursor"],
        routing_wire_ids={"cursor": new_id + "-high"}, roles=["coding", "critical_review"],
    )
    model.pop("retirement_reason", None)
    model.pop("runtime_model_ids", None)
    catalog["models"][new_id] = model
    catalog["review_candidates"][new_id] = {
        "model_id": new_id, "route": "cursor", "transport": "cursor",
        "invocation": f".venv/bin/python -m lu_machinery.dispatch dispatch --agent cursor --model {new_id}-high",
        "review_profiles": ["code", "infra"], "capabilities": ["code_review"], "suitability_roles": ["critical_review"],
    }
    catalog["orchestrator_seats"]["cursor"] = {
        "model_id": new_id, "effort": "high", "escalate_model_id": CURRENT_SOL, "escalate_effort": "high",
        "escalate_when": "deep_single_shot_or_architecture", "auto_allowlist": [new_id],
        "auto_scope": "write_implementation_dispatch_with_green_dor",
        "attestation_rule": "driver_of_record_requires_attested_resolved_model",
        "unknown_auto_family_resolution": "cursor_family", "unknown_auto_union_families": ["xai"],
    }
    with pytest.raises(ModelCatalogError, match="auto_allowlist must equal exactly"):
        validate_catalog(catalog)


def test_verdict_recorder_accepts_attested_grok_and_refuses_unpinned_cursor_models():
    # C-5307 restores Grok's holder and exact Cursor runtime identity.
    from lu_machinery.review import record_cf_verdict

    for agent in ("grok", "grok-build"):
        task = {
            "agent": agent, "model": "grok-4.7", "resolved_model": "grok-4.7-build",
            "resolved_model_known": True, "resolved_model_source": "grok-model-usage",
        }
        assert record_cf_verdict.reviewer_identity(task) == ("grok-4.7", "xai")
    for reported, model in (("grok-4.7-build", "grok-4.7"), ("Grok 4.7 256K High", "grok-4.7")):
        with pytest.raises(record_cf_verdict.RecordError, match="not a formal reviewer"):
            record_cf_verdict._require_formal_reviewer(cursor=False, reported=reported, model=model, family="xai")
        record_cf_verdict._require_formal_reviewer(cursor=True, reported=reported, model=model, family="xai")


@pytest.mark.parametrize(
    "family,route,excluded",
    [
        ("google", "codex", True),
        ("moonshot", "cursor", True),
        ("deepseek", "codex", True),
        ("openai", "agy", True),
        ("openai", "codex", False),
        ("anthropic", "claude", False),
    ],
)
def test_shared_role_family_exclusion(family, route, excluded):
    assert bool(family_exclusion(family=family, route=route, transport=route)) is excluded


def test_shared_author_independence_and_advisory_outcomes():
    params = {"route": "codex", "transport": "native_codex", "family": "openai"}
    assert family_exclusion(**params, author_family="anthropic") is None
    assert family_exclusion(**params, author_family="openai")[0] == "excluded"
    assert (
        family_exclusion(**params, author_family="openai", advisory_only_for_author_families=frozenset({"openai"}))[0]
        == "advisory_only"
    )


@pytest.mark.parametrize(
    "family,route,transport,excluded",
    [
        ("xai", "grok", "native_grok", False),
        ("moonshot", "kimi", "native_kimi", False),
        ("xai", "cursor", "cursor", True),
        ("xai", "other", "cursor", True),
        ("cursor", "other", "native", True),
    ],
)
def test_shared_cursor_author_policy_preserves_main_family_separation(family, route, transport, excluded):
    from lu_machinery.review.reviewer_resolver import CURSOR_AUTO_UNION_FAMILIES, CURSOR_AUTO_UNION_FAMILY

    result = family_exclusion(
        family=family,
        route=route,
        transport=transport,
        author_family=CURSOR_AUTO_UNION_FAMILY,
        union_family=CURSOR_AUTO_UNION_FAMILY,
        union_families=CURSOR_AUTO_UNION_FAMILIES,
    )
    assert bool(result) is excluded
    if excluded:
        assert result[0] == "excluded"


def test_legacy_adapter_expands_raw_authored_references():
    from lu_machinery.review.role_resolution import expanded_legacy_view
    from tests.review.test_model_catalog import assert_frozen_surfaces

    assert_frozen_surfaces({"catalog": expanded_legacy_view(raw_catalog())}, surfaces=("catalog",))


_UNKNOWN_TOKENS = (
    "composer",
    "cursor-fast",
    "mystery-model",
    "composer-2.4",
    "mystery-reviewer",
    "gravity",
    "anti",
)


@pytest.mark.usefixtures('configured_roots')
def test_canonical_normalize_family_unknown_is_first_class() -> None:
    from lu_machinery.review.record_cf_verdict import BranchFactsError
    from lu_machinery.review.record_cf_verdict import incoming_writer_family
    from lu_machinery.review.reviewer_resolver import resolve_author_family
    from lu_machinery.review.reviewer_resolver import resolve_family

    for token in _UNKNOWN_TOKENS:
        assert model_families.normalize_family(token) is model_families.Family.UNKNOWN, token
    assert model_families.normalize_family("") is model_families.Family.UNKNOWN
    assert model_families.normalize_family(None) is model_families.Family.UNKNOWN
    for token in ("muse", "muse-spark-1.3", "muse-wonder-full", "muse-wonder-full-2.0", "muse:mystery-model"):
        assert model_families.normalize_family(token) is model_families.Family.UNKNOWN, token
        assert resolve_family(token) == "unknown", token
        assert resolve_author_family(token) == "unknown", token
        with pytest.raises(BranchFactsError, match="incoming writer family unknown"):
            incoming_writer_family("muse", token)
    with pytest.raises(BranchFactsError, match="incoming writer family unknown"):
        incoming_writer_family("muse", None)


@pytest.mark.usefixtures('configured_roots')
def test_canonical_word_boundary_prevents_false_matches() -> None:
    # "gemmate" must NOT be read as gemma; "openrouter" has no openai marker;
    # "gravity" and "anti" alone stay unknown.
    assert model_families.normalize_family("gemmate-thing") is model_families.Family.UNKNOWN
    assert model_families.normalize_family("openrouter") is model_families.Family.UNKNOWN
    assert model_families.normalize_family("gravity") is model_families.Family.UNKNOWN
    assert model_families.normalize_family("anti") is model_families.Family.UNKNOWN
    for token in (
        "xmeta",
        "metaphor",
        "metax",
        "xmuse-wonder-full-1.0",
        "muse-wonder-full-1.0x",
        "muse-wonder-full-1.01",
    ):
        assert model_families.normalize_family(token) is model_families.Family.UNKNOWN, token


@pytest.mark.usefixtures('configured_roots')
def test_canonical_lineage_prefers_pin_over_cursor_seat() -> None:
    # cursor seat + pinned model -> the pin's family.
    assert model_families.normalize_lineage_family({"family": "cursor", "pin": "grok-4"}) is model_families.Family.XAI
    assert (
        model_families.normalize_lineage_family({"family": "cursor", "pin": "claude-opus-4-6"})
        is model_families.Family.ANTHROPIC
    )
    assert (
        model_families.normalize_lineage_family({"family": "cursor", "pin": "composer-2.5"})
        is model_families.Family.MOONSHOT
    )
    # Cursor Auto has its own family without a concrete pin.
    assert model_families.normalize_lineage_family({"family": "cursor"}) is model_families.Family.CURSOR


@pytest.mark.usefixtures('configured_roots')
def test_canonical_lineage_ambiguous_concrete_signals_fail_closed() -> None:
    from lu_machinery.review.reviewer_resolver import resolve_author_family

    assert (
        model_families.normalize_lineage_family({"family": "google", "pin": "openrouter/deepseek/deepseek-v4-pro"})
        is model_families.Family.UNKNOWN
    )
    assert model_families.normalize_lineage_family({"family": "meta", "pin": "muse-wonder-full-1.0"}) is (
        model_families.Family.META
    )
    assert model_families.normalize_lineage_family({"family": "google", "pin": "muse-wonder-full-1.0"}) is (
        model_families.Family.UNKNOWN
    )
    assert resolve_author_family("muse-wonder-full-1.0", "meta") == "meta"
    assert resolve_author_family("muse-wonder-full-1.0", "google") == "conflict"


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("display", ["Composer 2.5", "composer 2.5", "COMPOSER 2.5", "  Composer   2.5  "])
def test_cursor_display_name_resolves_to_the_concrete_composer_slug(display: str) -> None:
    assert model_families.canonical_cursor_model(display) == "composer-2.5"
    assert model_families.normalize_family(display) is model_families.Family.MOONSHOT
    assert model_families.normalize_lineage_family({"family": "cursor", "resolved_model": display}) is (
        model_families.Family.MOONSHOT
    )


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "value",
    [
        "unknown",
        "",
        "   ",
        "Composer",
        "Composer 2",
        "Composer 2.4",
        "Composer 2.50",
        "Composer 2.5 Fast",
        "Composer 3",
    ],
)
def test_cursor_display_name_normaliser_rejects_everything_else(value: str) -> None:
    assert model_families.canonical_cursor_model(value) == value
    assert model_families.normalize_family(value) is model_families.Family.UNKNOWN


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "selector",
    [
        "auto",
        "AUTO",
        "Auto",
        " auto ",
        "default",
        "DEFAULT",
        "Default",
        " default ",
        "cursor:auto",
        "CURSOR:AUTO",
        "Cursor:Auto",
        " cursor:auto ",
        "cursor/auto",
        "CURSOR/AUTO",
        "Cursor/Auto",
        " cursor/auto ",
        "cursor:default",
        "CURSOR:DEFAULT",
        "Cursor:Default",
        " cursor:default ",
        "cursor/default",
        "CURSOR/DEFAULT",
        "Cursor/Default",
        " cursor/default ",
    ],
)
def test_cursor_auto_selectors_share_canonical_family(selector):
    from lu_machinery.review.model_catalog import is_cursor_auto_selector

    assert is_cursor_auto_selector(selector)
    assert model_families.normalize_family(selector) is model_families.Family.CURSOR
    assert (
        model_families.normalize_lineage_family({"family": "cursor", "model": selector}) is model_families.Family.CURSOR
    )


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("field", ["pin", "pin_slug", "model", "model_id", "writer_model_id"])
@pytest.mark.parametrize("selector", ["auto", "DEFAULT", "cursor:auto", "cursor/default"])
@pytest.mark.parametrize("reported", ["Grok 4.7 256K High", "claude-opus-5-5", None])
def test_explicit_auto_selector_keeps_cursor_despite_runtime_telemetry(field, selector, reported):
    metadata = {"family": "cursor", field: selector, "resolved_model": reported}
    assert model_families.normalize_lineage_family(metadata) is model_families.Family.CURSOR


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "token,expected",
    [
        ("agy", "google"),
        ("ANTIGRAVITY", "google"),
        ("agy:mystery-model", "google"),
        ("antigravity:gemini-3.1-pro-high", "google"),
        ("gemma-agy", "google"),
        ("agy:deepseek-v4.1-flash", "deepseek"),
        ("agy:grok-4.7", "xai"),
        ("agy:composer-2.5", "moonshot"),
        ("agy:glm-5.3", "zhipu"),
        ("agy:laguna-s-2.1", "poolside"),
        ("agy:qwen", "qwen"),
        (f"cursor:agy:{CURRENT_SOL}", "unknown"),
        ("cursor:auto", "cursor"),
        ("fixture", "fixture"),
        ("adversarial-fixture", "fixture"),
        ("agyx:mystery", "unknown"),
        ("xantigravity:mystery", "unknown"),
        ("mystery:antigravityx", "unknown"),
    ],
)
def test_agy_route_fallback_and_other_family_controls(token, expected):
    assert model_families.normalize_family(token).value == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    "mixed,expected",
    [
        (f"claude-opus-5-5:{CURRENT_SOL}", "openai"),
        (f"{CURRENT_SOL}:claude-opus-5-5", "openai"),
        ("claude-opus-5-5:gemini-3.1-pro-high", "google"),
        (f"gemini-3.1-pro-high:{CURRENT_SOL}", "google"),
        ("deepseek-v4.1-flash:gemini-3.1-pro-high", "deepseek"),
        ("grok-4.7:claude-opus-5-5", "anthropic"),
    ],
)
def test_agy_does_not_change_existing_mixed_concrete_token_precedence(mixed, expected):
    for selector in (mixed, f"agy:{mixed}", f"{mixed}-antigravity"):
        assert model_families.normalize_family(selector).value == expected


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("model,family", [("claude-opus-5-5", "anthropic"), (CURRENT_SOL, "openai")])
def test_agy_decoration_does_not_override_conflicting_lineage_or_author_family(model, family):
    from lu_machinery.review.reviewer_resolver import resolve_author_family

    decorated = f"agy:{model}"
    assert model_families.normalize_lineage_family({"family": "google", "pin": decorated}).value == "unknown"
    assert model_families.normalize_lineage_family({"family": family, "pin": decorated}).value == family
    assert resolve_author_family(decorated, "google") == "conflict"
    assert resolve_author_family(decorated, family) == family
