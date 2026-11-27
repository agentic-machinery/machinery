"""Explicit positive-path registrations for the real AGY startup gate."""

import json
from pathlib import Path

import pytest

from lu_machinery.agent_runtime import agy_hook_config
from lu_machinery.agent_runtime.agy_hook_config import GUARD_NAME, guard_registration
from lu_machinery.host_context import invocation_checkout, load_host_context
from tests.host_context.conftest import build_host_layout


@pytest.fixture(scope="session")
def registration_host(tmp_path_factory):
    """Validate the synthetic binding once, before provider subprocess fakes are installed."""
    layout = build_host_layout(tmp_path_factory.mktemp("registration-host"))
    return load_host_context(env=layout.env)


@pytest.fixture
def bind_registration_runtime(registration_host, monkeypatch):
    """Use the validated runtime while retaining each adapter test's own product inputs."""
    monkeypatch.setattr(agy_hook_config, "hook_interpreter", lambda: registration_host.runtime.python)
    monkeypatch.setattr(agy_hook_config, "configure_native_hook_roots", invocation_checkout)


def register_agy_hooks(cwd: Path, *, home: Path | None = None) -> None:
    paths = [cwd / ".agents/hooks.json"]
    if home is not None:
        paths.append(home / ".gemini/config/hooks.json")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({GUARD_NAME: guard_registration()}), encoding="utf-8")
