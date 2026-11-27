"""Separated host layout for R1 and later caller migrations."""

import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import venv
from dataclasses import dataclass
from pathlib import Path

import pytest

from lu_machinery import host_context

_VERSION = "99.1.0"


@dataclass
class HostLayout:
    file: Path
    data: dict
    python: Path
    env: dict[str, str]

    def write(self):
        self.file.write_text(json.dumps(self.data), encoding="utf-8")

    def check(self, *args, env=None):
        return subprocess.run(
            [self.python, "-I", "-B", "-m", "lu_machinery.host_context", "check", "--json", *args],
            cwd="/", env=self.env if env is None else env, capture_output=True, text=True, check=False, timeout=30,
        )


def git(checkout, *args):
    return subprocess.run(
        ["git", "-C", checkout, *args],
        cwd="/", env={"PATH": os.defpath, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        check=True, capture_output=True, timeout=10,
    )


def build_host_layout(tmp_path: Path) -> HostLayout:
    # Use the package under test, including the installed-wheel CI layout.
    # Fixture metadata lets the subprocess exercise the isolated import check.
    runtime = tmp_path / "runtime"
    venv.EnvBuilder(symlinks=True).create(runtime)
    python = runtime / "bin" / "python"
    site = runtime / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    source = Path(host_context.__file__).resolve().parent
    (site / "lu_machinery").symlink_to(source, target_is_directory=True)
    (site / "agents_extensions").symlink_to(source.parent / "agents_extensions", target_is_directory=True)
    (site / "fixture-deps.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
    metadata = site / f"lu_machinery-{_VERSION}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Name: lu-machinery\nVersion: {_VERSION}\n", encoding="utf-8")

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    repos = {}
    for key, slug in {
        "machinery": "agentic-machinery/machinery",
        "public": "learn-ukrainian/learn-ukrainian.github.io",
    }.items():
        local_name = "learn-ukrainian" if key == "public" else key
        checkout = tmp_path / "checkouts" / local_name
        checkout.mkdir(parents=True)
        git(checkout, "init", "--template=")
        git(checkout, "remote", "add", "origin", f"https://github.com/{slug}.git")
        git(checkout, "config", "extensions.worktreeConfig", "true")
        repos[key] = str(checkout)

    tool = tmp_path / "tool.py"
    tool.write_bytes(b"raise RuntimeError('private tool must not run during validation')\n")
    workspace = tmp_path / "cto"
    workspace.mkdir()
    starter = workspace / "start"
    starter.touch(mode=0o700)
    selector = tmp_path / "selector.yaml"
    selector.write_text("version: 1\nactive_slugs: {}\n", encoding="utf-8")
    file = tmp_path / "host.v1.json"
    layout = HostLayout(
        file=file,
        data={
            "schema": host_context.SCHEMA,
            "state_root": str(state),
            "runtime": {"python": str(python), "lu_machinery": _VERSION, "wheel_sha256": "a" * 64},
            "repos": repos,
            "products": {"public": {"repo": "public", "python": str(python)}},
            "default_product": "public",
            "private_tools": {"example": {"path": str(tool), "sha256": hashlib.sha256(tool.read_bytes()).hexdigest()}},
            "cto": {"workspace": str(workspace), "starter": str(starter), "config_dir": str(workspace)},
            "selector_file": str(selector),
            "monitor_url": "http://127.0.0.1:8765",
        },
        python=python,
        env={host_context.CONTEXT_ENV: str(file)},
    )
    layout.write()
    return layout


@pytest.fixture
def host_layout(tmp_path: Path) -> HostLayout:
    return build_host_layout(tmp_path)
