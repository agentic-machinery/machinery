"""The bridge loads under one package identity beside a consumer's own runtime (#100 R28)."""

import subprocess
import sys
from pathlib import Path


def test_consumer_import_order_preserves_package_identity(tmp_path):
    """A consumer's runtime must not shadow machinery or be shadowed by the bridge."""
    consumer = tmp_path / "consumer"
    runtime = consumer / "agent_runtime"
    common = consumer / "common"
    (runtime / "adapters").mkdir(parents=True)
    common.mkdir()
    for name in ("__init__.py", "adapters/__init__.py"):
        (runtime / name).write_text("")
    (runtime / "redact.py").write_text("CONSUMER = True\n")
    (common / "__init__.py").write_text("")
    (common / "repo_root.py").write_text(
        "POISON = True\n"
        "def project_interpreter():\n"
        "    raise RuntimeError('consumer common')\n"
    )
    probe = """
import importlib
import sys
sys.path[:0] = sys.argv[1:3]
before = sys.path.copy()
modules = ("agent_runtime.redact", "lu_machinery.ai_agent_bridge")
if sys.argv[3] == "bridge-first":
    modules = modules[::-1]
for name in modules:
    importlib.import_module(name)
    if name == "lu_machinery.ai_agent_bridge":
        importlib.import_module("lu_machinery.agent_runtime.acpx_discuss")
        importlib.import_module("lu_machinery.agent_runtime.lane_probe")
from lu_machinery.ai_agent_bridge import _cli
from lu_machinery.agent_runtime import runner
from lu_machinery.agent_runtime.adapters.claude import ClaudeAdapter
from lu_machinery.agent_runtime.errors import AgentTimeoutError
assert _cli.InterAgentTransportError is runner.InterAgentTransportError
assert _cli.AgentTimeoutError is AgentTimeoutError
assert importlib.import_module("agent_runtime.redact").CONSUMER is True
assert "common.repo_root" not in sys.modules
assert sys.path == before
print("PASS", sys.argv[3])
"""
    failures = []
    for order in ("consumer-first", "bridge-first"):
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(consumer), str(Path(__file__).resolve().parents[2]), order],
            cwd=tmp_path, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != f"PASS {order}":
            failures.append(f"{order}: {result.stdout}{result.stderr}")
    assert not failures, "\n".join(failures)
