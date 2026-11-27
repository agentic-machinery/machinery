"""Flag new Python string literals that spell the durable store root."""

import re
from collections import Counter
from pathlib import Path

# Check the root component and quoted paths that start with it.
STORE_LITERAL = re.compile(r"""(?P<quote>["'])batch_state(?:/[^\s"']*)?(?P=quote)""")

# Existing spellings from the R2b migration. Each entry permits one occurrence
# of this exact line in this file. Remove entries as their callers move.
EXISTING_LINES = (
    ('agent_deploy/plan.py', 'records = "batch_state/agent-deploy"'),
    ('agent_runtime/attempt_boundary.py', 'if destination.startswith("batch_state/"):'),
    ('agent_runtime/attempt_boundary.py', 'for name in (*AGY_WORKSPACE_CONFIG_DIRS, ".git", ".worktrees", "batch_state"):'),
    ('agent_runtime/bounded_advisory.py', '_FORBIDDEN_ROOTS = frozenset({".git", ".worktrees", ".venv", "node_modules", "batch_state"})'),
    ('common/pytest_scratch.py', 'BACKED_UP_ROOTS = (".agent", ".claude", "batch_state", "data")'),
    ('common/release_layout.py', '"batch_state",'),
    ('common/task_store_paths.py', 'return parts[-2:] == ("batch_state", "tasks") or parts[-3:] == ("batch_state", "tasks", "archive")'),
    ('common/task_store_paths.py', 'if resolved.parts[-2:] == ("batch_state", "tasks"):'),
    ('common/task_store_paths.py', 'if next_path.parts[-2:] == ("batch_state", "tasks"):'),
    ('common/task_store_paths.py', 'if ancestor.parts[-2:] == ("batch_state", "tasks"):'),
    ('dispatcher/write_boundary.py', '_CHECKOUT_PRIVATE = ("batch_state", ".agent", ".secrets", ".runtime", ".deploy-state", ".pids", ".codex", ".gemini",'),
    ('docs/store_census.py', "CENSUS_ROOTS = ('data', '.agent', '.mcp', 'batch_state')"),
    ('docs/store_census.py', "return path.startswith('batch_state/tasks/archive/') or path.endswith(TASK_RECORD_SUFFIXES)"),
    ('docs/store_census.py', "if not row['path'].startswith('batch_state/') or row['path'].count('/') != 1:"),
    ('fleet/ignored_task_output.py', 'if relative.is_absolute() or ".." in relative.parts or not relative.is_relative_to("batch_state/preserved"):'),
    ('fleet/ignored_task_output.py', 'if tuple(relative.parts[:3]) != ("batch_state", "preserved", task_id) or len(relative.parts) < 4:'),
    ('fleet/regenerable_output.py', '"batch_state/issue_stream_audit_refresh.json",'),
    ('fleet/regenerable_output.py', '"batch_state/issue_stream_audit_refresh.lock",'),
    ('fleet/regenerable_output.py', 'or not location.is_relative_to("batch_state/preserved")):'),
    ('fleet/regenerable_output.py', 'if not candidate_relative.is_relative_to("batch_state/preserved"):'),
    ('fleet/terminal_abandon.py', '"{path: \'batch_state/...\', sha256: \'<64-hex-digest>\'}. Patches must equal git diff\\n"'),
    ('fleet_comms/artifacts.py', '"batch_state", sources.batch_state_dir, state,'),
    ('host_context.py', 'batch = root / "batch_state"'),
    ('hygiene/batch_state_retention.py', 'if len(parts) < 2 or parts[0] != "batch_state" or any(part in {"..", "."} for part in parts):'),
    ('hygiene/batch_state_retention.py', 'if origin.name in EXCLUDED or origin.parent.name != "batch_state":'),
    ('hygiene/batch_state_retention.py', 'or expiry.utcoffset().total_seconds() != 0 or path.parent.name != "batch_state"'),
    ('maintenance/batch_state_retention.py', 'if batch_state.name != "batch_state":'),
    ('maintenance/batch_state_retention.py', '"batch_state": str(batch_state),'),
    ('orchestration/lifecycle_observation.py', 'if location.parts[:3] != ("batch_state", "preserved", task_id):'),
)


def main() -> int:
    package = Path("lu_machinery")
    if not (package / "state_paths.py").is_file():
        print("Run state_path_literals from the repository root (lu_machinery/state_paths.py missing).")
        return 1

    remaining = Counter(EXISTING_LINES)
    violations = 0
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if relative in {"state_paths.py", "lint/state_path_literals.py"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not STORE_LITERAL.search(line):
                continue
            entry = relative, line.strip()
            if remaining[entry]:
                remaining[entry] -= 1
                continue
            print(f"{path}:{number}: use a path builder from lu_machinery.state_paths")
            violations += 1

    for (relative, line), count in sorted(remaining.items()):
        if count:
            print(f"{package / relative}: remove stale state path allowance for {line}")
            violations += count

    print(f"State path literal lint: {violations} violation(s).")
    return int(violations > 0)


if __name__ == "__main__":
    raise SystemExit(main())
