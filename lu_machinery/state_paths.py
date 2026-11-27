"""Canonical durable store paths derived solely from the configured state root.

Every durable store under ``batch_state/`` is constructed here so no module can
derive state from a Git checkout or working directory (REBUILD-PLAN-1126 R2b).
Calling any builder with ``root=None`` resolves the state root through
:func:`lu_machinery.common.repo_root.state_root`, which fails closed when no
host context or explicit state binding is configured.
"""

from __future__ import annotations

from pathlib import Path

from lu_machinery.common.repo_root import state_root

BATCH_STATE_DIRNAME = "batch_state"

# Relative path parts / Path constants preserved for callers and tests that
# inspect or compare relative store layouts without spelling "batch_state"
# outside this module.
TASKS_PARTS: tuple[str, ...] = (BATCH_STATE_DIRNAME, "tasks")
TASKS_ARCHIVE_PARTS: tuple[str, ...] = (BATCH_STATE_DIRNAME, "tasks", "archive")
PRESERVED_PARTS: tuple[str, ...] = (BATCH_STATE_DIRNAME, "preserved")
REVIEW_RECEIPTS_PARTS: tuple[str, ...] = (BATCH_STATE_DIRNAME, "review-receipts")
REVIEW_FINDINGS_PARTS: tuple[str, ...] = (BATCH_STATE_DIRNAME, "review-findings")
REVIEW_MEASUREMENT_PARTS: tuple[str, ...] = (BATCH_STATE_DIRNAME, "review-measurement")

FLEET_COMMS_REL: Path = Path(BATCH_STATE_DIRNAME, "fleet-comms", "v1")
FLEET_COMMS_RETENTION_REL: Path = Path(BATCH_STATE_DIRNAME, "fleet-comms", "retention")
RESET_RESERVE_REL: Path = Path(BATCH_STATE_DIRNAME, "routing_budget", "operator_reset_reserve.json")
BRANCH_ARCHIVE_REL: Path = Path(BATCH_STATE_DIRNAME, "branch-archive")
BRANCH_ARCHIVE_LEDGER_REL: Path = BRANCH_ARCHIVE_REL / "evidence.jsonl"
BRANCH_ARCHIVE_RECEIPT_REL: Path = BRANCH_ARCHIVE_REL / "stale-local-receipts.jsonl"
BRANCH_ARCHIVE_LOCK_REL: Path = BRANCH_ARCHIVE_REL / "archive.lock"
BACKUP_ALERTS_REL: Path = Path(BATCH_STATE_DIRNAME, "backups", "alerts")
BACKUP_LAST_RUN_REL: Path = Path(BATCH_STATE_DIRNAME, "backups", "last-run.json")


def branch_archive_rel(filename: str) -> Path:
    """Return relative ``batch_state/branch-archive/<filename>`` with a validated filename."""
    return BRANCH_ARCHIVE_REL / _validate_component(filename)


def _anchor(root: Path | None) -> Path:
    return state_root() if root is None else Path(root)


def _validate_component(part: str) -> str:
    if not part or part in {".", ".."} or "\x00" in part or "/" in part or "\\" in part:
        raise ValueError(f"invalid state path component: {part!r}")
    return part


def batch_state_dir(root: Path | None = None) -> Path:
    """Return ``<state_root>/batch_state``."""
    return _anchor(root) / BATCH_STATE_DIRNAME


def batch_state_path(*parts: str, root: Path | None = None) -> Path:
    """Return ``<state_root>/batch_state/<parts...>`` with validated path segments."""
    return batch_state_dir(root).joinpath(*(_validate_component(part) for part in parts))


def tasks_dir(root: Path | None = None) -> Path:
    return batch_state_path("tasks", root=root)


def write_ownership_db(root: Path | None = None) -> Path:
    return tasks_dir(root) / "write-ownership.sqlite3"


def fleet_comms_dir(root: Path | None = None) -> Path:
    return _anchor(root) / FLEET_COMMS_REL


def fleet_comms_db(root: Path | None = None) -> Path:
    return fleet_comms_dir(root) / "comms.sqlite3"


def fleet_comms_retention_dir(root: Path | None = None) -> Path:
    return _anchor(root) / FLEET_COMMS_RETENTION_REL


def pr_merge_cache_path(root: Path | None = None) -> Path:
    return batch_state_path("fleet-comms", "pr-merge-facts.json", root=root)


def github_client_dir(root: Path | None = None) -> Path:
    return batch_state_path("github-client", root=root)


def api_usage_dir(root: Path | None = None) -> Path:
    return batch_state_path("api_usage", root=root)


def routing_budget_dir(root: Path | None = None) -> Path:
    return batch_state_path("routing_budget", root=root)


def reset_reserve_path(root: Path | None = None) -> Path:
    return _anchor(root) / RESET_RESERVE_REL


def failover_cooldowns_db(root: Path | None = None) -> Path:
    return batch_state_path("agent_runtime_failover_cooldowns.sqlite3", root=root)


def gemini_api_cooldown_path(root: Path | None = None) -> Path:
    return batch_state_path("gemini_api_cooldown.json", root=root)


def review_receipts_dir(root: Path | None = None) -> Path:
    return _anchor(root).joinpath(*REVIEW_RECEIPTS_PARTS)


def review_findings_dir(root: Path | None = None) -> Path:
    return _anchor(root).joinpath(*REVIEW_FINDINGS_PARTS)


def review_findings_db(level: str, root: Path | None = None) -> Path:
    return review_findings_dir(root) / f"{_validate_component(level)}.sqlite"


def review_measurement_dir(root: Path | None = None) -> Path:
    return _anchor(root).joinpath(*REVIEW_MEASUREMENT_PARTS)


def contract_canary_dir(receipt_dir: str = "review-contract-canary", root: Path | None = None) -> Path:
    return batch_state_path(receipt_dir, root=root)


def issue_stream_audit_path(root: Path | None = None) -> Path:
    return batch_state_path("issue_stream_audit.json", root=root)


def issue_stream_audit_refresh_paths(root: Path | None = None) -> tuple[Path, Path]:
    base = batch_state_dir(root)
    return (
        base / "issue_stream_audit_refresh.json",
        base / "issue_stream_audit_refresh.lock",
    )


def data_tier_dir(root: Path | None = None) -> Path:
    return batch_state_path("data-tier", root=root)


def idle_settle_events_path(root: Path | None = None) -> Path:
    return batch_state_path("idle_settle", "events.jsonl", root=root)


def preserved_dir(root: Path | None = None, *parts: str) -> Path:
    return batch_state_path("preserved", *parts, root=root)


def hook_audit_measure_path(root: Path | None = None) -> Path:
    return batch_state_path("hook-audit-measure.json", root=root)


def opsec_overrides_path(root: Path | None = None) -> Path:
    return batch_state_path("opsec", "overrides.jsonl", root=root)


def agent_ledger_dir(root: Path | None = None) -> Path:
    return batch_state_path("agent-ledger", root=root)


def backup_alerts_dir(root: Path | None = None) -> Path:
    return _anchor(root) / BACKUP_ALERTS_REL


def backup_last_run_path(root: Path | None = None) -> Path:
    return _anchor(root) / BACKUP_LAST_RUN_REL


def codex_transport_health_path(root: Path | None = None) -> Path:
    return batch_state_path("runtime", "codex-transport-health.json", root=root)


def admission_shadow_dir(root: Path | None = None) -> Path:
    return batch_state_path("admission-shadow", root=root)


def reports_dir(root: Path | None = None) -> Path:
    return batch_state_path("reports", root=root)


def locks_dir(root: Path | None = None) -> Path:
    return batch_state_path("locks", root=root)


def merge_queue_keeper_paths(root: Path | None = None, *, slug: str | None = None) -> tuple[Path, Path]:
    base = batch_state_dir(root)
    if slug is None:
        return (
            base / "merge_queue_keeper.json",
            base / "locks" / "merge_queue_keeper.lock",
        )
    safe_slug = _validate_component(slug)
    return (
        base / "merge_queue_keeper" / f"{safe_slug}.json",
        base / "locks" / f"merge_queue_keeper.{safe_slug}.lock",
    )


def orchestrator_runs_dir(root: Path | None = None) -> Path:
    return batch_state_path("orchestrator-runs", root=root)


def worktree_reaper_dir(root: Path | None = None) -> Path:
    return batch_state_path("worktree-reaper", root=root)


def thread_rollovers_dir(root: Path | None = None) -> Path:
    return batch_state_path("thread-rollovers", root=root)


def batch_state_tmp_dir(root: Path | None = None) -> Path:
    return batch_state_path("tmp", root=root)


def telemetry_events_dir(root: Path | None = None) -> Path:
    return batch_state_path("telemetry", "events", root=root)
