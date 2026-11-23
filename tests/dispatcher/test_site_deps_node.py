"""Pinned Node and npm toolchain selection for dispatch site dependency provisioning."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from lu_machinery.dispatcher import site_deps


def _write_fake_toolchain(
    root: Path,
    *,
    node_version: str,
    npm_version: str | None = "11.19.1",
    symlink_layout: bool = False,
    npm_uses_env_node: bool = False,
    npm_ci_marker: bool = False,
) -> Path:
    """Create a hermetic fake Node/npm bin directory under ``root``."""
    if symlink_layout:
        real_root = root.parent / f"node-{node_version}-fixture"
        bin_dir = real_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        root.symlink_to(real_root, target_is_directory=True)
        returned_dir = root / "bin"
    else:
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        returned_dir = bin_dir

    node_bin = bin_dir / "node"
    node_bin.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then printf "%s\\n" "{node_version}"; exit 0; fi\n'
        'if [ "$1" = "-e" ]; then '
        f'printf \'{{"node":"{node_version}","abi":"137","platform":"linux","arch":"x64","libc":"glibc-2.39"}}\\n\'; '
        "exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    node_bin.chmod(0o755)

    if npm_version is not None:
        if symlink_layout:
            cli = bin_dir.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True, exist_ok=True)
            npm_target = cli
            (bin_dir / "npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
        else:
            npm_target = bin_dir / "npm"

        if npm_uses_env_node:
            # Verify that probing npm --version puts the candidate's own Node first on PATH.
            npm_script = (
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then\n'
                '  resolved_node="$(command -v node)"\n'
                f'  if [ "$resolved_node" != "{bin_dir / "node"}" ]; then\n'
                '    printf "wrong node on PATH: %s\\n" "$resolved_node" >&2\n'
                "    exit 1\n"
                "  fi\n"
                f'  printf "%s\\n" "{npm_version}"\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
        elif npm_ci_marker:
            npm_script = (
                "#!/bin/sh\n"
                f'if [ "$1" = "--version" ]; then printf "%s\\n" "{npm_version}"; exit 0; fi\n'
                'if [ "$1" = "config" ]; then printf "{}\\n"; exit 0; fi\n'
                'if [ "$1" = "ci" ]; then\n'
                "  mkdir -p node_modules/pkg\n"
                f'  printf "%s|%s|%s\\n" "{node_version}" "{npm_version}" "$PATH" > node_modules/pkg/selected.txt\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
        else:
            npm_script = (
                "#!/bin/sh\n"
                f'if [ "$1" = "--version" ]; then printf "%s\\n" "{npm_version}"; exit 0; fi\n'
                "exit 0\n"
            )
        npm_target.write_text(npm_script, encoding="utf-8")
        npm_target.chmod(0o755)
    return returned_dir


def _make_worktree(
    root: Path,
    *,
    nvmrc: str | None = None,
    site_nvmrc: str | None = None,
    engines: object = None,
    include_engines_key: bool = False,
) -> tuple[Path, Path]:
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    package: dict[str, object] = {"name": "site-fixture", "version": "1.0.0"}
    if include_engines_key or engines is not None:
        package["engines"] = engines
    (site / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (site / "package-lock.json").write_text(
        json.dumps({"name": "site-fixture", "version": "1.0.0", "lockfileVersion": 3, "packages": {"": package}}),
        encoding="utf-8",
    )
    if nvmrc is not None:
        (root / ".nvmrc").write_text(nvmrc, encoding="utf-8")
    if site_nvmrc is not None:
        (site / ".nvmrc").write_text(site_nvmrc, encoding="utf-8")
    return root, site


def test_selects_pinned_node24_and_keeps_node22_worktrees_on_node22(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Defect class: wrong Node toolchain selected from ambient PATH instead of worktree pin."""
    toolchains = tmp_path / "toolchains"
    toolchains.mkdir()
    bin_22 = _write_fake_toolchain(
        toolchains / "22", node_version="v22.23.4", npm_version="10.9.9", symlink_layout=True, npm_ci_marker=True
    )
    bin_24 = _write_fake_toolchain(
        toolchains / "24", node_version="v24.22.3", npm_version="11.19.1", symlink_layout=True, npm_ci_marker=True
    )
    ambient_path = os.pathsep.join([str(bin_22), "/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", ambient_path)
    monkeypatch.delenv("AGENT_ORIGINAL_PATH", raising=False)
    monkeypatch.setenv(site_deps.NODE_TOOLCHAIN_DIRS_ENV, str(toolchains / "{major}" / "bin"))

    wt24, site24 = _make_worktree(
        tmp_path / "wt24", nvmrc="24.22.3\n", engines={"node": "24.22.3", "npm": "11.19.1"}
    )
    wt22, site22 = _make_worktree(tmp_path / "wt22", nvmrc="22\n", engines={"node": "22.x"})

    assert site_deps.resolve_toolchain_dir(wt24, site24) == bin_24.resolve()
    assert site_deps.resolve_toolchain_dir(wt22, site22) == bin_22.resolve()

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    res24 = site_deps.ensure(repo, wt24)
    res22 = site_deps.ensure(repo, wt22)
    assert res24["state"] == "installed"
    assert res22["state"] == "installed"
    assert res24["key"] != res22["key"]

    selected_24 = (site24 / "node_modules" / "pkg" / "selected.txt").read_text(encoding="utf-8").strip()
    selected_22 = (site22 / "node_modules" / "pkg" / "selected.txt").read_text(encoding="utf-8").strip()
    assert selected_24.startswith(f"v24.22.3|11.19.1|{bin_24.resolve()}{os.pathsep}")
    assert selected_22.startswith(f"v22.23.4|10.9.9|{bin_22.resolve()}{os.pathsep}")
    assert os.environ["PATH"] == ambient_path


def test_npm_version_probe_executes_under_candidate_node_not_ambient_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Defect class: npm --version shebang inheriting wrong ambient Node during probe."""
    toolchains = tmp_path / "toolchains"
    toolchains.mkdir()
    bin_22 = _write_fake_toolchain(toolchains / "22", node_version="v22.23.4", npm_version="10.9.9")
    bin_24 = _write_fake_toolchain(
        toolchains / "24",
        node_version="v24.22.3",
        npm_version="11.19.1",
        symlink_layout=True,
        npm_uses_env_node=True,
    )
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_22), "/usr/bin", "/bin"]))
    monkeypatch.delenv("AGENT_ORIGINAL_PATH", raising=False)
    monkeypatch.setenv(site_deps.NODE_TOOLCHAIN_DIRS_ENV, str(toolchains / "{major}" / "bin"))

    wt, site = _make_worktree(tmp_path / "wt", nvmrc="24.22.3\n", engines={"node": "24.22.3", "npm": "11.19.1"})
    assert site_deps.resolve_toolchain_dir(wt, site) == bin_24.resolve()


def test_clean_machine_falls_back_to_non_shim_path_and_skips_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Defect class: clean-machine failure when configured toolchain root does not exist."""
    shim_dir = tmp_path / "scripts" / "dispatcher" / "npm_shims"
    _write_fake_toolchain(shim_dir.parent, node_version="v24.22.3", npm_version="11.19.1")
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name in ("node", "npm"):
        stub = shim_dir / name
        stub.write_text("#!/bin/sh\necho shim >&2\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
    real_bin = _write_fake_toolchain(tmp_path / "hosted-node", node_version="v24.22.3", npm_version="11.19.1")
    absent_root = tmp_path / "absent-opt"

    monkeypatch.setenv("PATH", os.pathsep.join([str(shim_dir), str(real_bin), "/usr/bin", "/bin"]))
    monkeypatch.delenv("AGENT_ORIGINAL_PATH", raising=False)
    monkeypatch.setenv(site_deps.NODE_TOOLCHAIN_DIRS_ENV, str(absent_root / "{major}" / "bin"))

    wt, site = _make_worktree(tmp_path / "wt", nvmrc="24.22.3\n", engines={"node": "24.22.3", "npm": "11.19.1"})
    assert site_deps.resolve_toolchain_dir(wt, site) == real_bin.resolve()


@pytest.mark.parametrize(
    ("installed_node", "installed_npm", "expected_snippet"),
    [
        ("v22.23.4", "10.9.9", "24.22.3"),
        ("v24.20.0", "11.19.1", "24.22.3"),
        ("v24.22.3", None, "npm missing"),
        ("v24.22.3", "10.9.9", "npm 10.9.9"),
    ],
)
def test_missing_or_mismatched_toolchain_refuses_naming_pin_and_searched_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    installed_node: str,
    installed_npm: str | None,
    expected_snippet: str,
):
    """Defect class: silent fallback to wrong Node/npm when pinned toolchain is absent."""
    toolchains = tmp_path / "toolchains"
    toolchains.mkdir()
    bin_dir = _write_fake_toolchain(toolchains / "24", node_version=installed_node, npm_version=installed_npm)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    monkeypatch.delenv("AGENT_ORIGINAL_PATH", raising=False)
    monkeypatch.setenv(site_deps.NODE_TOOLCHAIN_DIRS_ENV, str(toolchains / "{major}" / "bin"))

    wt, site = _make_worktree(tmp_path / "wt", nvmrc="24.22.3\n", engines={"node": "24.22.3", "npm": "11.19.1"})
    with pytest.raises(site_deps.NodeToolchainError) as raised:
        site_deps.resolve_toolchain_dir(wt, site)
    exc = raised.value
    assert exc.code == "node_toolchain_unavailable"
    message = str(exc)
    assert "24.22.3" in message
    assert "11.19.1" in message
    assert str(toolchains / "24" / "bin") in message
    assert expected_snippet in message

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger=site_deps.LOGGER.name):
        site_deps.provision(repo, wt)
    assert any("node_toolchain_unavailable" in record.getMessage() for record in caplog.records)
    assert not (site / "node_modules").exists()


@pytest.mark.parametrize(
    ("nvmrc", "site_nvmrc", "engines"),
    [
        ("22\n", None, {"node": "24.22.3"}),
        ("22.23.4\n", None, {"node": ">=24.0.0"}),
        ("24.20.0\n", None, {"node": "24.22.3"}),
        ("24.22.3\n", "22.23.4\n", None),
        (None, None, {"node": ">=24.0.0 <23.0.0"}),
    ],
)
def test_conflicting_pins_refuse_fail_closed(
    tmp_path: Path, nvmrc: str | None, site_nvmrc: str | None, engines: dict[str, str] | None
):
    """Defect class: silent execution when .nvmrc and engines.node contradict each other."""
    wt, site = _make_worktree(tmp_path / "wt", nvmrc=nvmrc, site_nvmrc=site_nvmrc, engines=engines)
    with pytest.raises(site_deps.NodeToolchainError) as raised:
        site_deps.resolve_toolchain_dir(wt, site)
    assert raised.value.code == "node_pin_conflict"


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("nvmrc", ""),
        ("nvmrc", "lts/*\n"),
        ("nvmrc", "24.22.3\n22.23.4\n"),
        ("nvmrc", "024\n"),
        ("nvmrc", "24.22.3\x00\n"),
        ("nvmrc", "2" * 1025),
        ("nvmrc_symlink", "24.22.3\n"),
        ("nvmrc_dir", ""),
        ("engines", "24.22.3"),
        ("engines", {"node": 24}),
        ("engines", {"node": ""}),
        ("engines", {"node": "22.x.0"}),
        ("engines", {"node": "banana"}),
        ("engines", {"node": "24.22.3\u202e"}),
        ("engines", {"npm": ""}),
        ("engines", {"npm": "latest"}),
    ],
)
def test_malformed_pin_inputs_refuse_fail_closed(tmp_path: Path, kind: str, value: object):
    """Defect class: malformed or hostile .nvmrc / engines silently ignored."""
    wt, site = _make_worktree(tmp_path / "wt")
    if kind == "nvmrc":
        assert isinstance(value, str)
        (wt / ".nvmrc").write_text(value, encoding="utf-8")
    elif kind == "nvmrc_symlink":
        target = tmp_path / "outside-nvmrc"
        target.write_text(str(value), encoding="utf-8")
        (wt / ".nvmrc").symlink_to(target)
    elif kind == "nvmrc_dir":
        (wt / ".nvmrc").mkdir()
    else:
        (site / "package.json").write_text(json.dumps({"name": "fixture", "engines": value}), encoding="utf-8")
    with pytest.raises(site_deps.NodeToolchainError) as raised:
        site_deps.resolve_toolchain_dir(wt, site)
    assert raised.value.code == "node_pin_invalid"


@pytest.mark.parametrize(
    "bad_setting",
    [
        "",
        "   ",
        "/opt/lu-node/{major}/bin:",
        ":/opt/lu-node/{major}/bin",
        "/opt/lu-node/{major}/bin::/usr/local/bin",
        "opt/lu-node/{major}/bin",
        "/opt/lu-node/../etc/{major}/bin",
        "/opt/lu-node/{minor}/bin",
        "/opt/lu-node/{major/bin",
        "/opt/lu-node/{major}/bin\n/tmp/bin",
        "/opt/lu-node/{major}/bin\u202e",
    ],
)
def test_invalid_toolchain_dirs_setting_refuses_fail_closed(tmp_path: Path, bad_setting: str):
    """Defect class: invalid LU_NODE_TOOLCHAIN_DIRS setting silently falling back to defaults."""
    wt, site = _make_worktree(tmp_path / "wt", nvmrc="24.22.3\n")
    with pytest.raises(site_deps.NodeToolchainError) as raised:
        site_deps.resolve_toolchain_dir(wt, site, env={site_deps.NODE_TOOLCHAIN_DIRS_ENV: bad_setting, "PATH": "/bin"})
    assert raised.value.code == "node_toolchain_config_invalid"


def test_semver_ranges_highest_match_and_symlink_loop_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Defect class: range matching, highest-semver tie-breaking, and symlink-loop crashes."""
    loop_dir = tmp_path / "loop"
    loop_dir.mkdir()
    (loop_dir / "a").symlink_to(loop_dir / "b")
    (loop_dir / "b").symlink_to(loop_dir / "a")
    (loop_dir / "node").symlink_to(loop_dir / "a")

    older = _write_fake_toolchain(tmp_path / "older", node_version="v22.14.0", npm_version="10.8.0")
    newer = _write_fake_toolchain(tmp_path / "newer", node_version="v22.23.4", npm_version="10.9.9")
    clone = _write_fake_toolchain(tmp_path / "clone", node_version="v22.23.4", npm_version="10.9.9")

    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(loop_dir), str(older), str(newer), str(clone), "/usr/bin", "/bin"])
    )
    monkeypatch.delenv("AGENT_ORIGINAL_PATH", raising=False)
    monkeypatch.setenv(site_deps.NODE_TOOLCHAIN_DIRS_ENV, str(tmp_path / "absent" / "{major}" / "bin"))

    for engines in ({"node": "22.x"}, {"node": ">= 22.20.0 < 23"}, {"node": "^22.23.0"}, {"node": "~22.23.0"}):
        wt, site = _make_worktree(tmp_path / f"wt-{abs(hash(engines['node']))}", engines=engines)
        assert site_deps.resolve_toolchain_dir(wt, site) == newer.resolve()


def test_unpinned_worktree_returns_none_and_preserves_default_tool_lookup(tmp_path: Path):
    """Control: worktrees without .nvmrc or engines keep existing PATH lookup."""
    wt, site = _make_worktree(tmp_path / "wt")
    assert site_deps.resolve_toolchain_dir(wt, site) is None
    assert stat.S_ISDIR(wt.stat().st_mode)
