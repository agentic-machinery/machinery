"""Tests for scripts/orchestration/fleet_repos.py (#672 P2.1)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from lu_machinery.common import git_credentials
from lu_machinery.orchestration.fleet_repos import (
    SELECTOR_ENV,
    FleetRepoError,
    canonical_repository,
    clear_repository_cache,
    load_fleet_repos,
    resolve_active_fleet_repo,
    resolve_fleet_repo,
    resolve_fleet_repo_by_slug,
    selector_receipt,
    verify_repository_identity,
)


def test_resolve_public_uses_primary_root(tmp_path: Path):
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    (primary / ".git").mkdir()
    repo, checkout = resolve_fleet_repo("public", primary_root=primary)
    assert repo.key == "public"
    assert checkout == primary.resolve()


def test_resolve_hramatka_sibling(tmp_path: Path):
    primary = tmp_path / "learn-ukrainian"
    sibling = tmp_path / "hramatka"
    primary.mkdir()
    sibling.mkdir()
    (primary / ".git").mkdir()
    (sibling / ".git").mkdir()
    repo, checkout = resolve_fleet_repo("hramatka", primary_root=primary)
    assert repo.github.endswith("/hramatka")
    assert checkout == sibling.resolve()


def test_padded_and_uppercase_keys_resolve_to_the_catalog_key(tmp_path: Path):
    primary = tmp_path / "learn-ukrainian"
    sibling = tmp_path / "hramatka"
    primary.mkdir()
    sibling.mkdir()
    (primary / ".git").mkdir()
    (sibling / ".git").mkdir()
    for spelling in ("  hramatka  ", "HRAMATKA", "Hramatka"):
        repo, checkout = resolve_fleet_repo(spelling, primary_root=primary)
        assert repo.key == "hramatka"
        assert checkout == sibling.resolve()
        assert str(tmp_path) not in repo.key


def test_empty_repo_key_is_not_the_default(tmp_path: Path):
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    (primary / ".git").mkdir()
    messages = []
    for spelling in ("", "   "):
        with pytest.raises(FleetRepoError) as caught:
            resolve_fleet_repo(spelling, primary_root=primary)
        messages.append(str(caught.value))
        assert str(primary) not in messages[-1]
    assert messages[0] == messages[1]
    assert "unknown --repo" in messages[0]


def test_resolve_unknown_key_fails(tmp_path: Path):
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    (primary / ".git").mkdir()
    with pytest.raises(FleetRepoError, match="unknown --repo"):
        resolve_fleet_repo("not-a-repo", primary_root=primary)


def test_resolve_missing_sibling_fails_closed(tmp_path: Path):
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    (primary / ".git").mkdir()
    with pytest.raises(FleetRepoError, match="expects sibling checkout"):
        resolve_fleet_repo("hramatka", primary_root=primary)


def test_catalog_requires_exactly_one_default(tmp_path: Path):
    path = tmp_path / "fleet_repos.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "repos": {
                    "a": {"github": "o/a", "local_name": "a", "default": True},
                    "b": {"github": "o/b", "local_name": "b", "default": True},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FleetRepoError, match="exactly one default"):
        load_fleet_repos(path)


def test_malformed_yaml_and_missing_file_raise_fleet_repo_error(tmp_path: Path):
    """Config-load failures are normalized so every caller's ``except FleetRepoError`` covers them."""
    malformed = tmp_path / "fleet_repos.yaml"
    malformed.write_text("repos: {a: [unclosed\n", encoding="utf-8")
    with pytest.raises(FleetRepoError, match="catalog unreadable"):
        load_fleet_repos(malformed)
    with pytest.raises(FleetRepoError, match="catalog unreadable"):
        load_fleet_repos(tmp_path / "absent.yaml")


def test_every_registered_repository_resolves_to_a_sibling_checkout_path(tmp_path: Path):
    """Every registry row maps to its own distinct sibling directory beside the primary (#12499)."""
    primary = tmp_path / "learn-ukrainian"
    primary.mkdir()
    catalog = load_fleet_repos()
    seen: dict[Path, str] = {}
    for key, row in catalog.items():
        _, checkout = resolve_fleet_repo(key, primary_root=primary, repos=catalog, require_checkout=False)
        expected = primary.resolve() if row.default else (tmp_path / row.local_name).resolve()
        assert checkout == expected, key
        assert checkout not in seen, f"{key} and {seen.get(checkout)} share a checkout"
        assert type(row.security_alerts) is bool, key
        seen[checkout] = key
    assert {"kube-dojo", "kubedojo-labs", "machinery"} <= set(catalog)
    assert catalog["machinery"].repository_id == 1445647449
    assert catalog["machinery"].visibility == "public"
    assert catalog["machinery"].alternate_github == "agentic-machinery/machinery"
    assert catalog["infra-private"].repository_id == 1282510453
    assert catalog["infra-private"].visibility == "private"
    assert catalog["infra-private"].alternate_github == "agentic-machinery/machinery-infra"
    for key in ("machinery", "infra-private"):
        row = catalog[key]
        for slug in row.slugs:
            assert resolve_fleet_repo_by_slug(slug, repos=catalog) == row
            assert canonical_repository(slug) == row.github.casefold()


def _write_readonly_selector(base_dir: Path, payload: object, *, dir_mode: int = 0o500, file_mode: int = 0o400) -> tuple[Path, Path, str]:
    sel_dir = base_dir / "host-selector"
    sel_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    sel_file = sel_dir / "selector.yaml"
    raw = payload if isinstance(payload, bytes) else (payload.encode("utf-8") if isinstance(payload, str) else yaml.safe_dump(payload).encode("utf-8"))
    sel_file.write_bytes(raw)
    sel_file.chmod(file_mode)
    sel_dir.chmod(dir_mode)
    return sel_dir, sel_file, hashlib.sha256(raw).hexdigest()


def test_dual_slug_alias_rejects_mismatched_repository_id_or_visibility(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Defect class: alias bound to a different repository id or visibility accepted instead of failing closed before any network call."""
    catalog = load_fleet_repos()
    machinery = catalog["machinery"]
    private = catalog["infra-private"]

    # Both slugs resolve to the exact same registry row before any network call.
    assert resolve_fleet_repo_by_slug("learn-ukrainian/machinery", repos=catalog, expected_repository_id=1445647449, expected_visibility="public") == machinery
    assert resolve_fleet_repo_by_slug("agentic-machinery/machinery", repos=catalog, expected_repository_id=1445647449, expected_visibility="public") == machinery
    assert resolve_fleet_repo_by_slug("learn-ukrainian/learn-ukrainian-infra-private", repos=catalog, expected_repository_id=1282510453, expected_visibility="private") == private
    assert resolve_fleet_repo_by_slug("agentic-machinery/machinery-infra", repos=catalog, expected_repository_id=1282510453, expected_visibility="private") == private

    # Wrong repository_id or visibility is refused before any network call.
    with pytest.raises(FleetRepoError, match="repository id mismatch"):
        resolve_fleet_repo_by_slug("agentic-machinery/machinery", repos=catalog, expected_repository_id=999999)
    with pytest.raises(FleetRepoError, match="repository id mismatch"):
        resolve_fleet_repo_by_slug("agentic-machinery/machinery-infra", repos=catalog, expected_repository_id=1445647449)
    with pytest.raises(FleetRepoError, match="repository visibility mismatch"):
        resolve_fleet_repo_by_slug("agentic-machinery/machinery-infra", repos=catalog, expected_visibility="public")

    # Authenticated payload verification checks id, full_name, and visibility (and private consistency).
    verify_repository_identity(
        machinery,
        {"id": 1445647449, "full_name": "agentic-machinery/machinery", "visibility": "public", "private": False},
        active_github="agentic-machinery/machinery",
    )
    verify_repository_identity(
        private,
        {"id": 1282510453, "full_name": "agentic-machinery/machinery-infra", "visibility": "private", "private": True},
        active_github="agentic-machinery/machinery-infra",
    )
    for bad_payload, expected_msg in (
        ({"id": 999999, "full_name": "agentic-machinery/machinery", "visibility": "public"}, "id mismatch"),
        ({"id": True, "full_name": "agentic-machinery/machinery", "visibility": "public"}, "id mismatch"),
        ({"full_name": "agentic-machinery/machinery", "visibility": "public"}, "id mismatch"),
        ({"id": 1445647449, "full_name": "learn-ukrainian/machinery", "visibility": "public"}, "full_name mismatch"),
        ({"id": 1445647449, "full_name": "agentic-machinery/machinery", "visibility": "private", "private": True}, "visibility mismatch"),
        ({"id": 1445647449, "full_name": "agentic-machinery/machinery", "visibility": "public", "private": True}, "visibility mismatch"),
        ({"id": 1445647449, "full_name": "agentic-machinery/machinery"}, "visibility missing"),
    ):
        with pytest.raises(FleetRepoError, match=expected_msg):
            verify_repository_identity(machinery, bad_payload, active_github="agentic-machinery/machinery")

    # Malformed dual-slug registry rows fail closed (missing id/visibility, role/visibility clash, duplicate id or slug).
    for idx, bad_repos in enumerate(
        (
            {
                "a": {"github": "o/a", "local_name": "a", "role": "public-monorepo", "default": True},
                "m": {"github": "o/m", "alternate_github": "n/m", "visibility": "public", "local_name": "m", "role": "public-machinery", "transport": "https"},
            },
            {
                "a": {"github": "o/a", "local_name": "a", "role": "public-monorepo", "default": True},
                "m": {"github": "o/m", "alternate_github": "n/m", "repository_id": 42, "local_name": "m", "role": "public-machinery", "transport": "https"},
            },
            {
                "a": {"github": "o/a", "local_name": "a", "role": "public-monorepo", "default": True},
                "m": {"github": "o/m", "alternate_github": "n/m", "repository_id": 42, "visibility": "public", "local_name": "m", "role": "private-infra", "transport": "https"},
            },
            {
                "a": {"github": "o/a", "local_name": "a", "role": "public-monorepo", "default": True, "repository_id": 42, "visibility": "public"},
                "m": {"github": "o/m", "alternate_github": "n/m", "repository_id": 42, "visibility": "public", "local_name": "m", "role": "public-machinery", "transport": "https"},
            },
            {
                "a": {"github": "o/a", "local_name": "a", "role": "public-monorepo", "default": True},
                "m": {"github": "o/m", "alternate_github": "o/a", "repository_id": 42, "visibility": "public", "local_name": "m", "role": "public-machinery", "transport": "https"},
            },
        )
    ):
        bad_cfg = tmp_path / f"bad-{idx}.yaml"
        bad_cfg.write_text(yaml.safe_dump({"version": 1, "repos": bad_repos}), encoding="utf-8")
        clear_repository_cache()
        with pytest.raises(FleetRepoError, match="catalog unreadable"):
            load_fleet_repos(bad_cfg)

    # Valid host-owned selector exposes value and sha256 digest without host paths.
    sel_dir, sel_file, expected_digest = _write_readonly_selector(
        tmp_path,
        {
            "version": 1,
            "active_slugs": {
                "machinery": "agentic-machinery/machinery",
                "infra-private": "learn-ukrainian/learn-ukrainian-infra-private",
            },
        },
    )
    try:
        monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        active = resolve_active_fleet_repo("learn-ukrainian/machinery", expected_repository_id=1445647449, expected_visibility="public")
        assert active.repo == machinery
        assert active.active_github == "agentic-machinery/machinery"
        assert active.selector_receipt() == {
            "selector_value": "agentic-machinery/machinery",
            "selector_sha256": expected_digest,
        }
        full_receipt = selector_receipt()
        assert full_receipt == {
            "selector_values": {
                "infra-private": "learn-ukrainian/learn-ukrainian-infra-private",
                "machinery": "agentic-machinery/machinery",
            },
            "selector_sha256": expected_digest,
        }
        assert str(sel_file) not in str(full_receipt)
    finally:
        sel_dir.chmod(0o700)
        sel_file.chmod(0o600)


@pytest.mark.parametrize(
    "case",
    [
        "unset",
        "empty",
        "relative",
        "whitespace",
        "control-char",
        "missing-file",
        "writable-file",
        "writable-dir",
        "group-writable-file",
        "symlink-file",
        "inside-git-checkout",
        "inside-worktrees-dir",
        "inside-configured-root",
        "unknown-slug",
        "missing-row",
        "extra-row",
        "duplicate-key",
        "bad-version",
    ],
)
def test_selector_missing_unreadable_unknown_or_worker_writable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
):
    """Defect class: selector missing, unreadable, unknown, inside a worktree, or worker-writable defaults to the old slug instead of failing closed."""
    valid_doc = {
        "version": 1,
        "active_slugs": {
            "machinery": "learn-ukrainian/machinery",
            "infra-private": "learn-ukrainian/learn-ukrainian-infra-private",
        },
    }
    cleanup_dirs: list[Path] = []
    cleanup_files: list[Path] = []
    try:
        if case == "unset":
            monkeypatch.delenv(SELECTOR_ENV, raising=False)
        elif case == "empty":
            monkeypatch.setenv(SELECTOR_ENV, "")
        elif case == "relative":
            monkeypatch.setenv(SELECTOR_ENV, "relative/selector.yaml")
        elif case == "whitespace":
            monkeypatch.setenv(SELECTOR_ENV, f" {tmp_path / 'selector.yaml'} ")
        elif case == "control-char":
            monkeypatch.setenv(SELECTOR_ENV, f"{tmp_path}\u202e/selector.yaml")
        elif case == "missing-file":
            sel_dir = tmp_path / "missing-dir"
            sel_dir.mkdir(mode=0o700)
            sel_dir.chmod(0o500)
            cleanup_dirs.append(sel_dir)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_dir / "absent.yaml"))
        elif case == "writable-file":
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, valid_doc, dir_mode=0o500, file_mode=0o600)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "writable-dir":
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, valid_doc, dir_mode=0o700, file_mode=0o400)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "group-writable-file":
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, valid_doc, dir_mode=0o500, file_mode=0o460)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "symlink-file":
            target_dir, target_file, _ = _write_readonly_selector(tmp_path / "real", valid_doc)
            cleanup_dirs.append(target_dir)
            cleanup_files.append(target_file)
            link_dir = tmp_path / "link-dir"
            link_dir.mkdir(mode=0o700)
            link_file = link_dir / "selector.yaml"
            link_file.symlink_to(target_file)
            link_dir.chmod(0o500)
            cleanup_dirs.append(link_dir)
            monkeypatch.setenv(SELECTOR_ENV, str(link_file))
        elif case == "inside-git-checkout":
            checkout = tmp_path / "checkout"
            (checkout / ".git").mkdir(parents=True)
            sel_dir, sel_file, _ = _write_readonly_selector(checkout, valid_doc)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "inside-worktrees-dir":
            wt = tmp_path / ".worktrees" / "dispatch" / "agy" / "task"
            wt.mkdir(parents=True)
            sel_dir, sel_file, _ = _write_readonly_selector(wt, valid_doc)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "inside-configured-root":
            root = tmp_path / "configured-root"
            root.mkdir()
            sel_dir, sel_file, _ = _write_readonly_selector(root, valid_doc)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv("LU_PRODUCT_ROOT", str(root))
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "unknown-slug":
            bad = {
                "version": 1,
                "active_slugs": {
                    "machinery": "attacker-org/machinery",
                    "infra-private": "learn-ukrainian/learn-ukrainian-infra-private",
                },
            }
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, bad)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "missing-row":
            bad = {"version": 1, "active_slugs": {"machinery": "learn-ukrainian/machinery"}}
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, bad)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "extra-row":
            bad = {
                "version": 1,
                "active_slugs": {
                    "machinery": "learn-ukrainian/machinery",
                    "infra-private": "learn-ukrainian/learn-ukrainian-infra-private",
                    "public": "learn-ukrainian/learn-ukrainian.github.io",
                },
            }
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, bad)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "duplicate-key":
            raw = (
                "version: 1\n"
                "active_slugs:\n"
                "  machinery: learn-ukrainian/machinery\n"
                "  machinery: agentic-machinery/machinery\n"
                "  infra-private: learn-ukrainian/learn-ukrainian-infra-private\n"
            )
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, raw)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
        elif case == "bad-version":
            bad = {**valid_doc, "version": 2}
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, bad)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))

        with pytest.raises(FleetRepoError) as exc_info:
            resolve_active_fleet_repo("machinery")
        assert str(tmp_path) not in str(exc_info.value)

        with pytest.raises(git_credentials.SelectorError):
            git_credentials.verification_credentials("https://github.com/learn-ukrainian/machinery.git")
        with pytest.raises(git_credentials.SelectorError):
            git_credentials.verification_credentials("https://github.com/agentic-machinery/machinery.git")
    finally:
        for d in cleanup_dirs:
            d.chmod(0o700)
        for f in cleanup_files:
            if f.exists():
                f.chmod(0o600)
