"""Tests for scripts/orchestration/fleet_repos.py (#672 P2.1)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from lu_machinery.common import git_credentials
from lu_machinery.orchestration import fleet_repos
from lu_machinery.orchestration.fleet_repos import (
    SELECTOR_ENV,
    FleetRepo,
    FleetRepoError,
    canonical_repository,
    clear_repository_cache,
    load_fleet_repos,
    repository_slugs,
    resolve_active_fleet_repo,
    resolve_fleet_repo,
    resolve_fleet_repo_by_slug,
    selector_receipt,
    verify_repository_identity,
)
from lu_machinery.orchestration.fleet_repos import (
    main as fleet_repos_main,
)


def transferred_catalog(
    monkeypatch: pytest.MonkeyPatch,
    *,
    github: str = "org/repo",
    alternate: str = "neworg/repo",
    role: str = "public-labs",
) -> FleetRepo:
    """Make the cached fleet catalog one transferred row whose two baked slugs are *github* and *alternate*."""
    row = FleetRepo(
        key="case", github=github, local_name="repo", role=role, default=True,
        repository_id=4242, visibility="public", alternate_github=alternate,
    )
    monkeypatch.setattr(fleet_repos, "_repository_catalog", lambda _path: {"case": row})
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda path=None: {"case": row})
    return row


BOTH = ("org/repo", "neworg/repo")


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("org/repo", BOTH),
        ("neworg/repo", BOTH),
        ("NewOrg/Repo", BOTH),
        ("evil/repo", ("evil/repo",)),
        ("case", ()),
        (" org/repo", ()),
        ("org/repo\n", ()),
        ("org/repo/extra", ()),
        ("https://github.com/org/repo", ()),
        (None, ()),
        (["org/repo"], ()),
    ],
)
def test_repository_slugs_binds_both_baked_slugs_and_nothing_else(monkeypatch, spelling, expected):
    """Defect class: a payload naming the other baked slug of the same repository is refused, or a slug
    with the same repository name under another owner (or a key, URL or padded spelling) is accepted."""
    transferred_catalog(monkeypatch)

    assert repository_slugs(spelling) == expected


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
        monkeypatch.setattr(git_credentials, "_is_readonly_mount", lambda _fd: True)
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
        assert fleet_repos_main(["selector-check"]) == 0
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
        "same-uid-rw-file",
        "writable-file",
        "writable-dir",
        "group-writable-file",
        "symlink-file",
        "inside-git-checkout",
        "inside-worktrees-dir",
        "inside-configured-root",
        "configured-root-symlink-loop",
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
    if case in {"unknown-slug", "missing-row", "extra-row", "duplicate-key", "bad-version"}:
        monkeypatch.setattr(git_credentials, "_is_readonly_mount", lambda _fd: True)
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
        elif case == "same-uid-rw-file":
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, valid_doc, dir_mode=0o500, file_mode=0o400)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setenv(SELECTOR_ENV, str(sel_file))
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
        elif case == "configured-root-symlink-loop":
            loop_a = tmp_path / "loop-a"
            loop_b = tmp_path / "loop-b"
            loop_a.symlink_to(loop_b)
            loop_b.symlink_to(loop_a)
            sel_dir, sel_file, _ = _write_readonly_selector(tmp_path / case, valid_doc)
            cleanup_dirs.append(sel_dir)
            cleanup_files.append(sel_file)
            monkeypatch.setattr(git_credentials, "_is_readonly_mount", lambda _fd: True)
            monkeypatch.setenv("LU_PRODUCT_ROOT", str(loop_a))
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
        assert fleet_repos_main(["selector-check"]) == 2

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


def _selected(monkeypatch: pytest.MonkeyPatch, active: str, *, visibility: str) -> FleetRepo:
    """One transferred row (org/repo, neworg/repo; repository_id 4242) whose host selector chooses *active*."""
    row = FleetRepo(
        key="case", github="org/repo", local_name="repo", role=f"{visibility}-labs",
        repository_id=4242, visibility=visibility, alternate_github="neworg/repo",
    )
    default = FleetRepo(key="home", github="home/repo", local_name="home", role="public-monorepo", default=True)
    monkeypatch.setattr(fleet_repos, "_repository_catalog", lambda _path: {"case": row, "home": default})
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda path=None: {"case": row, "home": default})
    monkeypatch.setattr(
        git_credentials, "load_selector", lambda **_kwargs: git_credentials.SelectorState({"case": active}, "d" * 64),
    )
    return row


@pytest.mark.parametrize("active", BOTH)
def test_opsec_private_exemption_follows_only_the_active_slug(monkeypatch: pytest.MonkeyPatch, active: str):
    """Defect class: the OPSEC private-destination exemption matches only the registry's first slug, so after a
    transfer the active slug of a private repository is scanned as public while its inactive slug stays exempt."""
    from lu_machinery.opsec import prepublish

    _selected(monkeypatch, active, visibility="private")
    monkeypatch.setattr(prepublish, "_packaged_catalog", lambda: {
        "case": {"github": "org/repo", "alternate_github": "neworg/repo", "repository_id": 4242,
                 "visibility": "private", "role": "private-labs", "local_name": "repo"},
        "home": {"github": "home/repo", "role": "public-monorepo", "default": True, "local_name": "home"},
    })
    inactive = next(slug for slug in BOTH if slug != active)
    assert prepublish.is_private(active) and prepublish.is_private(f"https://github.com/{active}.git")
    assert not prepublish.is_private(inactive)
    assert not prepublish.is_private("evil/repo")


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("visibility", ["public", "private"])
@pytest.mark.parametrize("active", BOTH)
def test_sandbox_repository_scope_follows_only_the_active_slug(
    monkeypatch: pytest.MonkeyPatch, active: str, visibility: str, configured: bool,
):
    """Defect class: the AGY sandbox and write boundary scope a repository by the registry's first slug, so after a
    transfer the active slug loses its public or private scope and the inactive slug keeps it, including through the
    default repository's configured GitHub name when that name spells the inactive slug."""
    from lu_machinery.common import repo_root
    from lu_machinery.dispatcher import agy_sandbox

    _selected(monkeypatch, active, visibility=visibility)
    inactive = next(slug for slug in BOTH if slug != active)
    monkeypatch.setattr(repo_root, "configured_github_slug", lambda *_a, **_k: inactive if configured else None)
    assert agy_sandbox.public_repository(active) is (visibility == "public")
    assert agy_sandbox.private_repository(active) is (visibility == "private")
    for refused in (inactive, "evil/repo"):
        assert not agy_sandbox.public_repository(refused)
        assert not agy_sandbox.private_repository(refused)


@pytest.mark.parametrize("active", BOTH)
def test_dispatch_receipt_names_the_selector_a_dual_slug_row_was_resolved_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: str):
    """Defect class: a task record dispatched to a transferred repository keeps no trace of the host selector (value
    and sha256) that chose its active slug, so a review or audit cannot tell which slug the dispatch relied on; the
    stored repository identity stays the registry slug the lineage readers match."""
    from lu_machinery.dispatcher import store
    from lu_machinery.dispatcher.types import TaskSpec

    _selected(monkeypatch, active, visibility="public")
    (tmp_path / "p").write_text("prompt")
    spec = TaskSpec("task", "codex", "gpt-6.2-sol", "high", "read-only", tmp_path, tmp_path / "p", tmp_path / "r")
    entry = {"key": "case", "github": "org/repo", "alternate_github": "neworg/repo", "repository_id": 4242,
             "visibility": "public"}
    record = store.new_record(spec, entry, "codex/task", "a" * 40, {"research_owned_path": []})
    assert record["fleet_repo_selector"] == {"selector_value": active, "selector_sha256": "d" * 64}
    assert record["repository"] == "org/repo"
    single = store.new_record(spec, {"key": "home", "github": "home/repo"}, "codex/task", "a" * 40, {"research_owned_path": []})
    assert "fleet_repo_selector" not in single


@pytest.mark.parametrize("active", ["org/kit", "neworg/kit"])
def test_review_checkout_origin_must_be_the_active_ascii_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: str):
    """Defect class: review target resolution keeps a checkout whose origin is a transferred repository's inactive slug
    (a PR target inferred without --repo) or a non-ASCII look-alike that casefolds onto the active slug."""
    import subprocess

    from lu_machinery.review import target_resolution

    row = FleetRepo(key="case", github="org/kit", local_name="kit", role="public-labs", default=True,
                    repository_id=4242, visibility="public", alternate_github="neworg/kit")
    for module in (fleet_repos, target_resolution):
        monkeypatch.setattr(module, "load_fleet_repos", lambda path=None: {"case": row})
    monkeypatch.setattr(
        git_credentials, "load_selector", lambda **_kwargs: git_credentials.SelectorState({"case": active}, "d" * 64),
    )
    inactive = next(slug for slug in ("org/kit", "neworg/kit") if slug != active)

    def checkout(name: str, origin: str) -> Path:
        root = tmp_path / name
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=30)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", f"https://github.com/{origin}.git"],
                       check=True, capture_output=True, timeout=30)
        return root

    good, stale = checkout("good", active), checkout("stale", inactive)
    look_alike = checkout("look-alike", active.replace("k", "\u212a"))
    assert target_resolution.resolve_review_repository(good, None, mode="pr") == good
    assert target_resolution.resolve_review_repository(good, "case", mode="pr") == good
    with pytest.raises(target_resolution.TargetResolutionError, match="not its repository's active slug"):
        target_resolution.resolve_review_repository(stale, None, mode="pr")
    with pytest.raises(target_resolution.TargetResolutionError, match="not an exact owner/name"):
        target_resolution.resolve_review_repository(look_alike, None, mode="pr")
    for root in (stale, look_alike):
        with pytest.raises(target_resolution.TargetResolutionError):
            target_resolution.resolve_review_repository(root, "case", mode="pr", commit="a" * 40)


def test_non_ascii_slug_key_and_credentials_refused_before_fold_or_strip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Defect class: non-ASCII look-alikes (U+212A), whitespace (U+00A0), or Unicode digits alias repo slugs or keys."""
    assert fleet_repos.ascii_repo_slug("Learn-Ukrainian/Machinery") == "learn-ukrainian/machinery"
    assert fleet_repos.ascii_repo_slug("learn-u\u212arainian/machinery") is None
    assert fleet_repos.ascii_repo_slug("\u00a0learn-ukrainian/machinery") is None
    assert fleet_repos.ascii_repo_slug("owner/repo.git") is None
    assert fleet_repos.parse_ascii_positive_int("42") == 42
    assert fleet_repos.parse_ascii_positive_int("\u0664\u0662") is None
    assert fleet_repos.parse_ascii_positive_int("4\u0662") is None
    assert fleet_repos.parse_ascii_positive_int(True) is None
    assert fleet_repos.parse_ascii_positive_int("0") is None
    assert fleet_repos.parse_ascii_positive_int("9" * 4301) is None

    catalog = {
        "kit": FleetRepo(key="kit", github="org/kit", local_name="kit", role="public-labs", default=True),
    }
    assert fleet_repos.canonical_repo_key(" KIT ", repos=catalog) == "kit"
    with pytest.raises(fleet_repos.FleetRepoError):
        fleet_repos.canonical_repo_key("\u212ait", repos=catalog)
    with pytest.raises(fleet_repos.FleetRepoError):
        fleet_repos.canonical_repo_key("\u00a0kit", repos=catalog)

    bad_key_catalog = {
        "\u212ait": FleetRepo(key="\u212ait", github="org/kit", local_name="kit", role="public-labs", default=True),
    }
    with pytest.raises(fleet_repos.FleetRepoError, match="ASCII"):
        fleet_repos.canonical_repo_key("kit", repos=bad_key_catalog)

    bad_slug_catalog = {
        "kit": FleetRepo(key="kit", github="org/\u212ait", local_name="kit", role="public-labs", default=True),
    }
    with pytest.raises(fleet_repos.FleetRepoError, match="ASCII"):
        fleet_repos.resolve_fleet_repo_by_slug("org/kit", repos=bad_slug_catalog)

    cfg = tmp_path / "fleet_repos.yaml"
    cfg.write_text(
        "repos:\n  kit:\n    default: true\n    local_name: kit\n    github: org/\u212ait\n    role: public-labs\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid fleet repository registry"):
        git_credentials.repository_registry(cfg)

    bad_key_cfg = tmp_path / "bad_key_fleet_repos.yaml"
    bad_key_cfg.write_text(
        "repos:\n  \u212ait:\n    default: true\n    local_name: kit\n    github: org/kit\n    role: public-labs\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid fleet repository registry"):
        git_credentials.repository_registry(bad_key_cfg)

    valid_cfg = tmp_path / "valid_fleet_repos.yaml"
    valid_cfg.write_text(
        "repos:\n  kit:\n    default: true\n    local_name: kit\n    github: org/kit\n    role: public-labs\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(git_credentials, "default_config_path", lambda **_kw: valid_cfg)
    assert git_credentials.verification_credentials("https://github.com/org/\u212ait.git") == []


def test_research_observability_refuses_non_ascii_report_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Defect class: Kelvin-sign look-alike repository slug in cached membership report matches active profile via casefold."""
    from lu_machinery.research import observability

    monkeypatch.setattr(observability, "github_slug", lambda: "learn-ukrainian/machinery")
    assert (
        observability._membership_report_matches_active_profile(
            {"repository": "learn-u\u212arainian/machinery", "streams": {}},
            tmp_path / "missing.yaml",
        )
        is False
    )
