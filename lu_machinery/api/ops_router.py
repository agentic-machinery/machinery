"""Read-only ops surfaces: retention plans, test slots, high-load samples and firing alerts."""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from lu_machinery.common import json_depth, test_slots

from . import load_samples, panel_values
from .monitor_context import MonitorContext, get_ctx
from .state_helpers import cache_get_or_compute_async, ctx_scoped_ttl_key
from .subscriptions import POOL_IDS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


def _plan_dir(ctx: MonitorContext) -> Path:
    return ctx.roots.batch_state_dir / "fleet-comms" / "retention"


@router.get("/v1/retention/latest")
def retention_latest(ctx: MonitorContext = Depends(get_ctx)) -> dict:
    """Return the latest retention plan JSON if present (no mutations)."""
    plan_dir = _plan_dir(ctx)
    latest = plan_dir / "latest.json"
    if not latest.is_file():
        return {
            "schema": "fleet-comms.retention.plan.v1",
            "missing": True,
            "plan_dir": str(plan_dir),
            "hint": (
                ".venv/bin/python "
                f"{Path(__file__).parents[1].name}/hygiene/retention_engine.py plan"
            ),
        }
    try:
        payload = json_depth.loads(latest.read_text(encoding="utf-8"), max_depth=512)
    except (OSError, json.JSONDecodeError):
        # Do not return exception text to HTTP clients (CodeQL py/stack-trace-exposure).
        logger.exception("retention plan unreadable: %s", latest)
        return {
            "schema": "fleet-comms.retention.plan.v1",
            "error": "unreadable_plan",
            "plan_path": str(latest),
        }
    if isinstance(payload, dict):
        payload.setdefault("plan_path", str(latest))
        payload["missing"] = False
        return payload
    return {
        "schema": "fleet-comms.retention.plan.v1",
        "error": "invalid_plan",
        "plan_path": str(latest),
    }


@router.get("/v1/retention/plan-dir")
def retention_plan_dir(ctx: MonitorContext = Depends(get_ctx)) -> dict:
    """Expose the on-disk plan directory path (for operators)."""
    plan_dir = _plan_dir(ctx)
    return {
        "plan_dir": str(plan_dir),
        "exists": plan_dir.is_dir(),
        "archive_root_default": str(
            Path.home()
            / "Library"
            / "Application Support"
            / "learn-ukrainian"
            / "retention-archives"
        ),
        "scheduled_apply_default": False,
    }


@router.get("/v1/test-slots")
def test_slots_status(_ctx: MonitorContext = Depends(get_ctx)) -> dict:
    """Host test-slot pool (#10662): holders by task, the FIFO queue and wait times; never a path.

    A plain ``def``: the status read touches files, so FastAPI runs it off the event loop.
    ``observed_at`` and ``age_s`` describe the source observation. A failed read
    leaves both unknown instead of presenting the request time as freshness;
    ``requested_at`` records the request separately.
    """
    requested_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = test_slots.status()
    payload["requested_at"] = requested_at
    if payload.get("error") is None:
        payload["observed_at"] = requested_at
        payload["age_s"] = 0.0
    else:
        payload["observed_at"] = None
        payload["age_s"] = None
    return payload


# The host Alertmanager listens on loopback only.
ALERTMANAGER_HOST = "127.0.0.1"
ALERTMANAGER_PORT = 9093
ALERTMANAGER_ALERTS_PATH = "/api/v2/alerts?active=true&silenced=false&inhibited=false"
ALERTS_TTL_S = 15.0
ALERTS_TIMEOUT_S = 3.0
# A second reader may keep a cache hit 15 s old. Include the 3 s collector, 30 s page cycle,
# 8 s HTTP budget and 4 s margin.
ALERTS_STALE_AFTER_S = 60.0
# The dead-man heartbeat fires all the time by design; it proves Alertmanager
# is evaluating, so it is reported as a flag and not listed as a firing alert.
HEARTBEAT_ALERTNAMES = frozenset({"Watchdog"})
# Alertmanager v2 alert states; anything else is a malformed reading.
ALERT_STATES = frozenset({"active", "suppressed", "unprocessed"})
# One plain-English entry per alert our own rules define: (title, what it means,
# what happens next, subject kind). It is the allowlist too: a name not listed here is
# reported as UnrecognizedAlert, and neither labels nor annotation text are passed
# through (the rule text lives in a private repository). A rule added without its entry
# here reads as an unknown alert, so a new rule adds its entry in the same change.
# Subject kind "pool" or "cli" names the one fixed-set label that identifies what the
# alert is about; any other label stays server-side.
_ALERT_TEXT: dict[str, tuple[str, str, str, str | None]] = {
    "ApprovedNotLanded30m": (
        "Approved change not merged", "A change has had its approval for over 30 minutes but has not merged.",
        "The owning worker or the CTO lands it, or says why it is held.", None),
    "BackupSuccessOverdue": (
        "Scheduled data copy overdue", "The regular data copy has not completed successfully in time.",
        "The infrastructure lane checks the last run and repeats it.", None),
    "CappedScopeOomKill": (
        "Limited task hit its memory cap", "A task running under a memory limit was stopped for using too much memory.",
        "Informational: the limit did its job. The task owner may rerun it or raise the limit.", None),
    "CertExpirySoon": (
        "Certificate expires soon", "A security certificate is close to its end date.",
        "The infrastructure lane renews it before it expires.", None),
    "CfRunnerStuck": (
        "Check runner is stuck", "A continuous-integration job has stopped making progress.",
        "The infrastructure lane cancels and reruns it.", None),
    "CgroupCapOomKill": (
        "Task hit its memory cap", "A task was stopped for using more memory than its limit allows.",
        "The task owner reduces its memory use or raises the limit.", None),
    "CiRecoveriesOver3PerDay": (
        "Too many CI recoveries today", "More than three infrastructure recoveries of checks happened in one day.",
        "The CTO looks for the common cause before more reruns.", None),
    "CliHealthBehind": (
        "Command-line tool is out of date", "An installed agent tool is older than the latest release.",
        "The tool is updated at the next maintenance pass.", "cli"),
    "CliHealthCheckFailed": (
        "Command-line tool health check failed", "The routine health check of an agent tool did not pass.",
        "The infrastructure lane reads the check result and repairs the tool.", "cli"),
    "CliHealthSignedOut": (
        "Command-line tool is signed out", "An agent tool is not signed in, so it cannot run work.",
        "The operator signs the account in again.", "cli"),
    "CliHealthTextfileStale": (
        "Tool health report is stale", "The tool health report has not been refreshed recently.",
        "The infrastructure lane restarts the report job.", None),
    "CtoNoLiveCli": (
        "No live coordinator session", "The coordinating agent has no running session.",
        "The launcher restarts it; the infrastructure lane checks if it does not return.", None),
    "DiskAlmostFull": (
        "Storage is almost full", "Free storage is running low.",
        "The infrastructure lane clears old data before it fills.", None),
    "DiskCritical": (
        "Storage is critically full", "Free storage is nearly gone and work may start failing.",
        "The infrastructure lane clears space now.", None),
    "DriverKeeperCollectErrors": (
        "Driver keeper cannot collect state", "The service that watches drivers failed to collect their state.",
        "The infrastructure lane reads its log and repairs it.", None),
    "DriverKeeperCtoInboxWait": (
        "Coordinator inbox is waiting", "Messages to the coordinator have waited longer than usual.",
        "The coordinator drains its inbox on its next turn.", None),
    "DriverKeeperCtoInboxWaitLong": (
        "Coordinator inbox waiting too long", "Messages to the coordinator have waited a long time unread.",
        "The infrastructure lane checks that the coordinator is alive and reading.", None),
    "DriverKeeperHealthStale": (
        "Driver health reading is stale", "The driver health reading has not been refreshed recently.",
        "The infrastructure lane checks the keeper service.", None),
    "DriverKeeperLaneAlert": (
        "A work lane raised an alert", "A lane driver reported a problem with its own work.",
        "The lane's driver handles it; the CTO steps in if it persists.", None),
    "DriverKeeperStale": (
        "Driver keeper is not reporting", "The service that watches drivers has stopped reporting.",
        "The infrastructure lane restarts it.", None),
    "DriverPaneNoLiveCli": (
        "A driver has no live session", "A lane driver is running without a live agent session.",
        "The launcher restarts the session.", None),
    "DriverTextfileStale": (
        "Driver report is stale", "The driver status report has not been refreshed recently.",
        "The infrastructure lane restarts the report job.", None),
    "FleetCommsCollectionFailed": (
        "Messaging status unavailable", "The agent messaging status could not be collected.",
        "The infrastructure lane checks the messaging service.", None),
    "FleetCommsNewDeadLetters": (
        "Messages could not be delivered", "Some agent messages failed delivery for good.",
        "The CTO reads the failed messages and resends what matters.", None),
    "FleetCommsNewFailedDeliveries": (
        "Message deliveries are failing", "Some agent message deliveries failed.",
        "The infrastructure lane checks the recipients and retries.", None),
    "FleetCommsNewFailedJobs": (
        "Messaging jobs are failing", "Some agent messaging jobs failed.",
        "The infrastructure lane reads the job errors and repairs them.", None),
    "GitHubActionsRunQueuedTooLong": (
        "A CI run has waited too long", "A GitHub Actions run has been queued far longer than usual.",
        "It starts when a runner frees up; the infrastructure lane checks runner capacity.", None),
    "HostMemoryPressure": (
        "Memory is running low", "The machine is short of free memory.",
        "Heavy jobs finish or are stopped; the infrastructure lane finds the cause.", None),
    "HostOomKill": (
        "Out of memory: process killed", "The machine ran out of memory and stopped a process.",
        "The infrastructure lane finds which job caused it and limits it.", None),
    "ListenerEvidenceUnavailable": (
        "Cannot check open network ports", "The check for unexpected open network ports could not run.",
        "The infrastructure lane restores the check.", None),
    "LuApiProbeDown": (
        "Monitor API is not answering", "The Monitor API failed its health probe.",
        "The infrastructure lane restarts it.", None),
    "LuApiProbeSlow": (
        "Monitor API is slow", "The Monitor API answers its health probe slowly.",
        "The infrastructure lane finds the slow route.", None),
    "LuPaceStale": (
        "Usage pace reading is stale", "The subscription pace reading has not been refreshed recently.",
        "The infrastructure lane restarts the usage reader.", None),
    "MergeQueueStalled": (
        "Merge queue is stuck", "Changes sit in the merge queue without progress.",
        "The CTO finds the blocking change and clears or ejects it.", None),
    "MonitorApiErrors": (
        "Monitor API is returning errors", "Some Monitor API routes are answering with errors.",
        "The infrastructure lane reads the logs and fixes the failing route.", None),
    "MonitorApiSlowRoute": (
        "A Monitor API route is slow", "One Monitor API route takes much longer than it should.",
        "The infrastructure lane profiles and speeds up that route.", None),
    "NonLoopbackListener": (
        "Unexpected open network port", "A service is listening on a network address it should not.",
        "The infrastructure lane stops it and finds out why.", None),
    "OffsiteBackupFreshnessUnknown": (
        "Off-site copy age unknown", "The age of the latest off-site copy cannot be determined.",
        "The infrastructure lane restores the check.", None),
    "OffsiteBackupOverdue": (
        "Off-site copy overdue", "The latest off-site copy of the data is older than allowed.",
        "The infrastructure lane runs the copy and finds the cause.", None),
    "OffsiteBackupRunFailed": (
        "Off-site copy failed", "The last off-site copy run did not finish.",
        "The infrastructure lane reads its log and repeats it.", None),
    "OffsitePrimaryCapacityCritical": (
        "Off-site storage is critically full", "The main off-site storage is nearly full.",
        "The operator frees or adds space.", None),
    "OffsitePrimaryCapacityWarning": (
        "Off-site storage is filling up", "The main off-site storage is getting full.",
        "The infrastructure lane plans cleanup before it fills.", None),
    "OffsiteSecondaryCoverageStale": (
        "Second off-site copy is stale", "The second off-site copy is out of date.",
        "The infrastructure lane refreshes it.", None),
    "OomEvidenceUnavailable": (
        "Cannot check for memory kills", "The check for out-of-memory kills could not run.",
        "The infrastructure lane restores the check.", None),
    "OomKillUnclassified": (
        "Unexplained out-of-memory kill", "A process was stopped for lack of memory and the cause is not yet known.",
        "The infrastructure lane classifies it.", None),
    "PRBacklogRising3Days": (
        "Open changes keep piling up", "The number of open changes in a repository has risen for three days.",
        "The CTO reviews what is blocking merges.", None),
    "PROpenOver60m": (
        "Change open over an hour", "A change has stayed open for over an hour without landing.",
        "Its owner pushes it through review or reports the blocker.", None),
    "ReviewRoundsOver2": (
        "Change needs more than two review rounds", "A change has needed more than two review rounds.",
        "The CTO checks whether the brief or the change should be reshaped.", None),
    "ScorecardSourceDown": (
        "Scorecard data unavailable", "The data behind the scorecard cannot be read.",
        "The infrastructure lane restores the source.", None),
    "SecurityAlertsOpen": (
        "Security alerts are open", "A repository has open security alerts.",
        "The owning lane fixes or dismisses each with a reason.", None),
    "SecurityAlertsSourceDown": (
        "Security alert data unavailable", "The security alert source could not be read.",
        "The infrastructure lane restores the source.", None),
    "SecurityAlertsTextfileStale": (
        "Security alert report is stale", "The security alert report has not been refreshed recently.",
        "The infrastructure lane restarts the report job.", None),
    "ShutDownUnitBecameActive": (
        "A shut-down service restarted", "A service that was turned off on purpose is running again.",
        "The infrastructure lane stops it again and finds what restarted it.", None),
    "SreWatchTimerMissed": (
        "Reliability check did not run", "The scheduled reliability check missed its run.",
        "The infrastructure lane runs it and fixes the schedule.", None),
    "StalePRsIdleOver24h": (
        "Changes idle for over a day", "Open changes in a repository have seen no activity for over 24 hours.",
        "Their owners continue or close them.", None),
    "SubscriptionPoolMetricsMissing": (
        "Subscription readings missing", "No subscription capacity readings are being published.",
        "The infrastructure lane restores the reader.", None),
    "SubscriptionPoolRunsOutBeforeReset": (
        "Subscription pool will run out early", "At the current pace this pool's allowance ends before it resets.",
        "New work is routed to other pools until the pool resets.", "pool"),
    "SubscriptionPoolStatusUnknown": (
        "Subscription pool state unknown", "The capacity state of a pool cannot be read.",
        "The infrastructure lane restores the reading.", "pool"),
    "SystemdUnitCrashLooping": (
        "A service keeps crashing", "A background service restarts again and again.",
        "The infrastructure lane reads its log and fixes the cause.", None),
    "SystemdUnitFailed": (
        "A service has failed", "A background service stopped with an error.",
        "The infrastructure lane restarts it and finds the cause.", None),
    "TeacherApiProbeDown": (
        "Teacher API is not answering", "The teacher API failed its health probe.",
        "The infrastructure lane restarts it.", None),
    "TeacherApiScrapeDown": (
        "Teacher API metrics missing", "The teacher API metrics cannot be read.",
        "The infrastructure lane restores the reading.", None),
    "ThroughputReportTimerMissed": (
        "Throughput report did not run", "The scheduled throughput report missed its run.",
        "The infrastructure lane runs it and fixes the schedule.", None),
    "UsageCacheStale": (
        "Usage data is stale", "The cached subscription usage data has not been refreshed recently.",
        "The infrastructure lane restarts the usage reader.", None),
    "UsagePoolExpiringUnspent": (
        "Pool allowance may go unused", "This pool is on course to reset with allowance left over.",
        "Informational: more work can be routed to it.", "pool"),
    "UsagePoolLowBeforeReset": (
        "Pool allowance is low", "This pool has little allowance left and is not due to reset soon.",
        "New work is routed to other pools until it recovers.", "pool"),
    "UsagePoolReadingStale": (
        "Pool reading is stale", "A pool's usage reading has not been refreshed recently.",
        "The infrastructure lane restarts the usage reader.", "pool"),
    "UsagePoolRestored": (
        "Pool allowance is back", "This pool had run low and has recovered.",
        "Informational: no action needed; it is available for work again.", "pool"),
}
_ALERT_NAMES = frozenset(_ALERT_TEXT)

# Fixed sets for the one subject label an alert may pass through. A value outside its set
# is dropped, never echoed. Pools carry two id schemes (routing ids and usage-series ids).
_POOL_LABELS = {
    **{pool_id: name for name, pool_id in POOL_IDS.items()},
    "claude-a": "Claude a", "claude-b": "Claude b", "codex-a": "Codex a", "codex-b": "Codex b",
    "grok-cursor": "Grok+Cursor", "agy": "AGY", "codex": "Codex",
}
_CLI_LABELS = {
    "agy": "AGY", "claude": "Claude", "codex": "Codex", "codexbar": "CodexBar",
    "cursor-agent": "Cursor agent", "grok": "Grok", "opencode": "OpenCode",
}


def _start_key(starts_at: str | None) -> datetime:
    """Chronological sort key for a normalised start time; a missing one sorts last.

    The serialised form drops the fraction for whole seconds, so comparing the strings
    would put ``…:00.5Z`` before ``…:00Z``. A date-only value parses as a naive midnight,
    so it is read as UTC: aware and naive keys never meet in one comparison.
    """
    if starts_at is None:
        return datetime.max.replace(tzinfo=UTC)
    moment = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _alerts_reading() -> dict:
    """Read firing alerts from the loopback Alertmanager and keep only allow-listed fields.

    Labels such as ``instance`` and ``job`` and the generator URL name hosts and
    scrape targets, so they never leave this function.
    """
    observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        connection = http.client.HTTPConnection(ALERTMANAGER_HOST, ALERTMANAGER_PORT, timeout=ALERTS_TIMEOUT_S)
        try:
            connection.request("GET", ALERTMANAGER_ALERTS_PATH, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"HTTP {response.status}")
            raw = json_depth.loads(response.read(2_000_000), max_depth=512)
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        logger.warning("alertmanager read failed: %s", type(exc).__name__)
        return {"status": "unknown", "reason_code": "alertmanager_unreachable", "observed_at": observed_at,
                "heartbeat": None, "alerts": []}
    if not isinstance(raw, list):
        return {"status": "unknown", "reason_code": "alertmanager_invalid_response", "observed_at": observed_at,
                "heartbeat": None, "alerts": []}
    alerts: list[dict] = []
    heartbeat = False
    for item in raw:
        labels = item.get("labels") if isinstance(item, dict) else None
        status = item.get("status") if isinstance(item, dict) else None
        state = status.get("state") if isinstance(status, dict) else None
        annotations = item.get("annotations", {}) if isinstance(item, dict) else None
        # A malformed item means the reading cannot be trusted as a whole: an
        # "ok" reading with a dropped alert would show "Nothing firing".
        if (
            not isinstance(labels, dict)
            or not isinstance(labels.get("alertname"), str)
            or not isinstance(state, str)
            or state not in ALERT_STATES
            or not isinstance(annotations, dict)
        ):
            return {"status": "unknown", "reason_code": "alertmanager_invalid_response", "observed_at": observed_at,
                    "heartbeat": None, "alerts": []}
        if state != "active":
            continue
        if labels["alertname"] in HEARTBEAT_ALERTNAMES:
            heartbeat = True
            continue
        alertname = panel_values.choice(labels.get("alertname"), _ALERT_NAMES, "UnrecognizedAlert")
        title, meaning, next_step, subject_kind = _ALERT_TEXT.get(alertname, (None, None, None, None))
        # Only the label the table names for this alert is read, and only as a fixed-set value.
        subject = None
        if subject_kind is not None and isinstance(labels.get(subject_kind), str):
            subject = {"pool": _POOL_LABELS, "cli": _CLI_LABELS}[subject_kind].get(labels[subject_kind])
        row = {
            "alertname": alertname,
            "title": title,
            "meaning": meaning,
            "next_step": next_step,
            "subject": subject,
            "severity": panel_values.choice(labels.get("severity"), {"critical", "warning", "info", "none"}, "unknown"),
            "starts_at": panel_values.timestamp(item.get("startsAt")),
        }
        alerts.append(row)
    # Problems first (critical, then the rest), then information; oldest first within each.
    alerts.sort(key=lambda row: (row["severity"] == "info", row["severity"] != "critical", _start_key(row["starts_at"])))
    return {"status": "ok", "reason_code": None, "observed_at": observed_at, "heartbeat": heartbeat, "alerts": alerts}


@router.get("/v1/alerts")
async def firing_alerts(ctx: MonitorContext = Depends(get_ctx)) -> JSONResponse:
    """Alerts firing now in the loopback Alertmanager, allow-listed fields only.

    ``status`` is ``unknown`` with a ``reason`` when Alertmanager cannot be read,
    so an outage never reads as "nothing firing".
    """
    key = await asyncio.to_thread(ctx_scoped_ttl_key, ctx, "ops", "alerts")
    # This loopback read is cheap. Waiting for it on expiry avoids serving the
    # previous page refresh's reading on every 30-second UI cycle.
    reading = await cache_get_or_compute_async(key, ALERTS_TTL_S, _alerts_reading)
    # The bound travels in the body too, so a reader that loses the headers still judges the age.
    payload = {**reading, "stale_after_s": ALERTS_STALE_AFTER_S}
    return JSONResponse(content=payload, headers=panel_values.snapshot_headers(
        panel_values.observation_age_s(payload.get("observed_at")), ALERTS_STALE_AFTER_S,
        unavailable=payload.get("status") != "ok",
    ))


@router.get("/v1/load-samples")
def load_samples_tail(
    limit: int = Query(50, ge=1, le=load_samples.MAX_SAMPLES), ctx: MonitorContext = Depends(get_ctx)
) -> dict:
    """Read the bounded attribution ring without triggering process collection."""
    return load_samples.tail(ctx.roots.batch_state_dir, limit)
