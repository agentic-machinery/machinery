"""Dispatch admission disk guard (#10776).

Disk usage comes from a monkeypatched probe and the alert publisher is a
fake: no test reads the host disk or posts to Fleet Comms. Admission never
removes anything.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from collections import namedtuple
import pytest
from lu_machinery.orchestration import dispatch_admission as adm
from tests.helpers.product_tree import configured_roots as configured_roots
import os
import runpy
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from lu_machinery.orchestration import sources_watchdog as wd
from tests.agent_runtime.product_roots import agent_runtime_roots as agent_runtime_roots

_GIB = 1024**3
_LIMITS = adm.Thresholds(max_live_write_workers=2, min_mem_available_gib=3.5, max_load_per_cpu=1.5)
_Usage = namedtuple("_Usage", "total used free")


def _disk(used_percent: float, *, size_gib: float = 400.0) -> adm.DiskProbe:
    used = int(size_gib * _GIB * used_percent / 100)
    return adm.DiskProbe(used_bytes=used, free_bytes=int(size_gib * _GIB) - used)


@pytest.fixture
def host(monkeypatch):
    """A healthy host whose disk readings come, in order, from ``readings``."""
    monkeypatch.setattr(
        adm,
        "probe_host",
        lambda: adm.HostProbe(mem_available_bytes=12 * _GIB, load1=2.0, cpu_count=8, proc_available=True),
    )
    monkeypatch.setattr(adm, "probe_pool", lambda _reserve: adm.pool_headroom.PoolCheck(reserve_bytes=0, skipped="n/a"))
    monkeypatch.setattr(adm, "probe_test_slots", lambda: adm.SlotQueueProbe(depth=0, pool_size=4))
    monkeypatch.setattr(adm, "slice_usage_clause", lambda: None)
    state: dict = {"readings": [_disk(50.0)], "probes": 0, "alerts": []}

    def probe_disk():
        state["probes"] += 1
        readings = state["readings"]
        return readings.pop(0) if len(readings) > 1 else readings[0]

    def publish(body, key):
        state["alerts"].append((body, key))

    monkeypatch.setattr(adm, "probe_disk", probe_disk)
    monkeypatch.setattr(adm, "post_disk_alert", publish)
    return state


def test_below_the_alert_threshold_is_admitted_without_alert(tmp_path, host):
    host["readings"] = [_disk(74.9)]

    decision = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)

    assert decision.admitted
    assert host["alerts"] == []
    assert "disk 74.9% used, 100.4 GiB free (refuse at 90%, alert at 75%)" in decision.summary()
    record = decision.to_record()
    assert record["disk_used_percent"] == 74.9
    assert record["disk_free_gib"] == 100.4
    assert record["disk_alert"] is None


def test_alert_band_admits_and_alerts_once_then_suppresses_within_six_hours(tmp_path, host):
    host["readings"] = [_disk(82.0)]

    first = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)
    second = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)

    assert first.admitted and second.admitted
    assert len(host["alerts"]) == 1
    body, key = host["alerts"][0]
    assert "disk 82.0% used, 72.0 GiB free" in body
    assert "new write dispatches are still admitted" in body
    assert key.startswith("dispatch-admission-disk:")
    assert "[epics alert posted]" in first.summary()
    assert "[epics alert already posted at " in second.summary()
    assert first.to_record()["disk_alert"] == "epics alert posted"


def test_alert_rate_limit_reopens_after_six_hours(tmp_path):
    check = adm.DiskCheck(probe=_disk(81.0))
    sent: list[str] = []
    publish = lambda body, _key: sent.append(body)  # noqa: E731
    start = 1_800_000_000.0

    assert (
        adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=start, publish=publish)
        == "epics alert posted"
    )
    later = start + adm.DISK_ALERT_INTERVAL_S - 1
    assert adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=later, publish=publish).startswith(
        "epics alert already posted at "
    )
    after = start + adm.DISK_ALERT_INTERVAL_S
    assert (
        adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=after, publish=publish)
        == "epics alert posted"
    )
    assert len(sent) == 2
    assert not list(tmp_path.glob("*.json")), "the alert state must not look like a task record"


def test_alert_failure_never_blocks_and_retries_after_the_backoff(tmp_path, host, caplog, monkeypatch):
    host["readings"] = [_disk(83.0)]

    def broken(_body, _key):
        raise RuntimeError("plane down")

    monkeypatch.setattr(adm, "post_disk_alert", broken)
    caplog.set_level(logging.WARNING, logger=adm.__name__)
    decision = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)

    assert decision.admitted
    assert "[epics alert failed: plane down]" in decision.summary()
    assert "disk alert to epics failed: plane down" in caplog.text
    state = json.loads((tmp_path / adm.DISK_ALERT_STATE_NAME).read_text())
    assert state["last_error"] == "plane down"

    check = adm.DiskCheck(probe=_disk(83.0))
    sent: list[str] = []
    within = state["failed_at"] + adm.DISK_ALERT_RETRY_S - 1
    assert "retry after" in adm.maybe_alert_disk(
        tmp_path, check, _LIMITS, refused=False, now=within, publish=lambda body, _key: sent.append(body)
    )
    after = state["failed_at"] + adm.DISK_ALERT_RETRY_S
    assert (
        adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=after, publish=lambda b, _k: sent.append(b))
        == "epics alert posted"
    )
    assert len(sent) == 1


def test_unreadable_alert_state_does_not_silence_the_alert(tmp_path):
    (tmp_path / adm.DISK_ALERT_STATE_NAME).write_text("{not json")
    sent: list[str] = []

    outcome = adm.maybe_alert_disk(
        tmp_path, adm.DiskCheck(probe=_disk(90.0)), _LIMITS, refused=True, publish=lambda b, _k: sent.append(b)
    )

    assert outcome == "epics alert posted"
    assert "new write dispatches are REFUSED" in sent[0]


def test_full_disk_refuses_with_percent_and_free_space(tmp_path, host):
    host["readings"] = [_disk(91.0)]

    decision = adm.evaluate("danger", tmp_path, thresholds=_LIMITS)

    assert not decision.admitted
    assert host["probes"] == 1
    assert decision.failures == (
        "disk 91.0% used, 36.0 GiB free on the .worktrees filesystem is at or above the refuse threshold of 90% "
        "(DISPATCH_DISK_REFUSE_USED_PERCENT=90)",
    )
    line = decision.refusal_line()
    assert "91.0% used" in line and "36.0 GiB free" in line
    assert len(host["alerts"]) == 1 and "REFUSED" in host["alerts"][0][0]
    record = decision.to_record()
    assert "disk_reaper" not in record and "disk_used_percent_before_reaper" not in record


def test_exactly_at_the_refuse_threshold_is_refused(tmp_path, host):
    host["readings"] = [_disk(90.0)]

    decision = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)

    assert not decision.admitted


@pytest.mark.parametrize("mode", ["read-only", "research"])
def test_read_only_dispatch_is_exempt_from_the_disk_check(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(adm, "probe_disk", lambda: pytest.fail("read-only must not read the disk"))

    decision = adm.evaluate(mode, tmp_path)

    assert decision.exempt and decision.admitted


def test_a_failing_disk_probe_admits_and_logs_the_reason(tmp_path, host, caplog):
    host["readings"] = [adm.DiskProbe(skipped="disk usage unreadable: OSError: [Errno 5] I/O error")]
    caplog.set_level(logging.WARNING, logger=adm.__name__)

    decision = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)

    assert decision.admitted
    assert host["alerts"] == []
    assert "disk check skipped (disk usage unreadable: OSError: [Errno 5] I/O error)" in decision.summary()
    assert "disk check skipped: disk usage unreadable" in caplog.text
    assert decision.to_record()["disk_check_skipped"].startswith("disk usage unreadable")


def test_read_disk_uses_df_percent_and_reports_unreadable_statvfs(tmp_path):
    probe = adm.read_disk(tmp_path, usage=lambda _path: _Usage(total=100 * _GIB, used=45 * _GIB, free=45 * _GIB))
    assert probe.used_percent == 50.0  # used / (used + available), as df prints it
    assert probe.free_gib == 45.0

    def broken(_path):
        raise PermissionError("statvfs denied")

    unreadable = adm.read_disk(tmp_path, usage=broken)
    assert unreadable.used_percent is None
    assert unreadable.skipped == "disk usage unreadable: PermissionError: statvfs denied"

    invalid = adm.read_disk(tmp_path, usage=lambda _path: _Usage(total=0, used=0, free=0))
    assert invalid.skipped == "disk usage invalid: used=0 free=0"


def test_healthy_test_host_never_reads_the_real_disk_or_posts(monkeypatch):
    monkeypatch.setattr(adm, "read_disk", lambda *_a, **_k: pytest.fail("real disk read"))

    assert adm.probe_disk().skipped == adm.TEST_HOST_POOL_SKIP
    with pytest.raises(RuntimeError, match="test host"):
        adm.post_disk_alert("body", "key")


def test_worktrees_disk_path_is_the_primary_checkouts_worktrees(monkeypatch, tmp_path):
    (tmp_path / ".worktrees").mkdir()
    monkeypatch.setattr(adm, "primary_checkout", lambda: tmp_path)
    assert adm.worktrees_disk_path() == tmp_path / ".worktrees"
    (tmp_path / ".worktrees").rmdir()
    assert adm.worktrees_disk_path() == tmp_path


def test_disk_thresholds_default_to_config_and_honour_env_overrides():
    defaults = adm.load_thresholds({})
    assert (defaults.disk_reap_used_percent, defaults.disk_refuse_used_percent) == (85.0, 90.0)
    assert defaults.disk_alert_used_percent == 75.0

    custom = adm.load_thresholds(
        {"DISPATCH_DISK_REFUSE_USED_PERCENT": "92.5", "DISPATCH_DISK_ALERT_USED_PERCENT": "78"}
    )
    assert (custom.disk_refuse_used_percent, custom.disk_alert_used_percent) == (92.5, 78.0)


@pytest.mark.parametrize("raw", ["101", "-1", "lots", "nan"])
def test_invalid_disk_threshold_env_is_rejected(raw):
    with pytest.raises(ValueError, match="DISPATCH_DISK_REFUSE_USED_PERCENT"):
        adm.load_thresholds({"DISPATCH_DISK_REFUSE_USED_PERCENT": raw})


def test_publish_runs_the_fleet_comms_channel_publish_command(monkeypatch, tmp_path):
    for entry in ("objects", "refs"):
        (tmp_path / ".git" / entry).mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("LEARN_UKRAINIAN_PRIMARY_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("LU_TEST_DISPATCH_HEALTHY_HOST", raising=False)
    monkeypatch.setattr(adm, "primary_checkout", lambda: tmp_path)
    seen: dict = {}

    def run(command, **kwargs):
        seen["command"], seen["kwargs"] = command, kwargs
        return adm.subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(adm.subprocess, "run", run)

    adm.post_disk_alert("disk full\n", "dispatch-admission-disk:t")

    assert seen["command"][0] == sys.executable
    assert seen["kwargs"]["cwd"] == tmp_path
    assert seen["command"][1:] == [
        "-I",
        "-m",
        "lu_machinery.fleet_comms",
        "channel",
        "publish",
        "epics",
        "-",
        "--sender",
        "dispatch-admission",
        "--source",
        "dispatch-admission",
        "--kind",
        "report",
        "--idempotency-key",
        "dispatch-admission-disk:t",
    ]
    assert seen["kwargs"]["input"] == "disk full\n"
    assert seen["kwargs"]["timeout"] == adm.DISK_ALERT_PUBLISH_TIMEOUT_S

    monkeypatch.setattr(
        adm.subprocess, "run", lambda command, **_k: adm.subprocess.CompletedProcess(command, 2, "", "no channel\n")
    )
    with pytest.raises(RuntimeError, match="fleet_comms publish exited 2: no channel"):
        adm.post_disk_alert("disk full\n", "k")


# --- Round 3 (#10776): hard stop, redaction, alert state ----------


def test_a_disk_refusal_is_marked_as_a_hard_stop_and_does_not_offer_the_force_flag(tmp_path, host):
    host["readings"] = [_disk(90.0)]

    decision = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS)

    assert decision.disk_refused and decision.disk is not None and decision.disk.refused
    assert decision.to_record()["disk_refused"] is True
    assert "--force-admission cannot override a full disk" in decision.refusal_line()
    assert 'pass --force-admission "<reason>"' not in decision.refusal_line()


def test_other_refusals_are_not_disk_refusals(tmp_path, host):
    host["readings"] = [_disk(50.0)]
    capped = dataclasses.replace(_LIMITS, max_live_write_workers=0)

    decision = adm.evaluate("workspace-write", tmp_path, thresholds=capped)

    assert not decision.admitted and not decision.disk_refused
    assert 'pass --force-admission "<reason>" to override' in decision.refusal_line()


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("lock /home/someone/repo/.cleanup.lock is held", "lock <path> is held"),
        ("held (~/learn-ukrainian/x.lock)", "held (<path>)"),
        ("see ~someone/x and /srv/data/a:12", "see <path> and <path>"),
        ("origin/main is 3/4 behind; refs/heads/x", "origin/main is 3/4 behind; refs/heads/x"),
        ("removed 0 worktree(s), freed 0 MB", "removed 0 worktree(s), freed 0 MB"),
        ("lock %2Fhome%2Fsomeone%2Frepo.lock is held", "lock <path> is held"),
        ("url file%3A%2F%2F%2Fvar%2Ftmp%2Fa failed", "url <path> failed"),
        ("double %252Fsrv%252Fdata%252Fx", "double <path>"),
        ("held (%7E%2Frepo%2Fx.lock)", "held (<path>)"),
        ("disk 50%25 used; origin%2Fmain; 100% full", "disk 50%25 used; origin%2Fmain; 100% full"),
    ],
)
def test_redact_paths(text, redacted):
    assert adm.redact_paths(text) == redacted


def test_a_failed_alert_publish_is_redacted(tmp_path):
    def broken(_body, _key):
        raise RuntimeError(
            "fleet_comms publish could not run: [Errno 2] No such file: '/home/someone/.venv/bin/python'"
        )

    outcome = adm.maybe_alert_disk(tmp_path, adm.DiskCheck(probe=_disk(82.0)), _LIMITS, refused=False, publish=broken)

    assert "/home" not in outcome and "<path>" in outcome
    assert "/home" not in (tmp_path / adm.DISK_ALERT_STATE_NAME).read_text()


@pytest.mark.parametrize("key", ["posted_at", "failed_at"])
@pytest.mark.parametrize("value", ["1" + "0" * 400, '"yesterday"', "true"])
def test_a_malformed_alert_time_counts_as_an_alert_failure(tmp_path, key, value):
    (tmp_path / adm.DISK_ALERT_STATE_NAME).write_text(f'{{"{key}": {value}}}')
    sent: list[str] = []
    start = 1_800_000_000.0
    publish = lambda body, _key: sent.append(body)  # noqa: E731
    check = adm.DiskCheck(probe=_disk(82.0))

    outcome = adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=start, publish=publish)

    assert outcome.startswith(f"epics alert failed: alert state malformed ({key} ")
    assert sent == []
    state = json.loads((tmp_path / adm.DISK_ALERT_STATE_NAME).read_text())
    assert state["failed_at"] == start and state["last_error"].startswith("alert state malformed")
    later = start + adm.DISK_ALERT_RETRY_S
    assert adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=later, publish=publish) == (
        "epics alert posted"
    )
    assert len(sent) == 1


def test_a_backward_clock_step_does_not_allow_a_second_alert_within_the_window(tmp_path):
    check = adm.DiskCheck(probe=_disk(81.0))
    sent: list[str] = []
    publish = lambda body, _key: sent.append(body)  # noqa: E731
    start = 1_800_000_000.0

    assert adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=start, publish=publish) == (
        "epics alert posted"
    )
    stepped_back = start - 1
    assert adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=stepped_back, publish=publish).startswith(
        "epics alert already posted at "
    )
    within = stepped_back + adm.DISK_ALERT_INTERVAL_S - 1
    assert adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=within, publish=publish).startswith(
        "epics alert already posted at "
    )
    after = stepped_back + adm.DISK_ALERT_INTERVAL_S
    assert adm.maybe_alert_disk(tmp_path, check, _LIMITS, refused=False, now=after, publish=publish) == (
        "epics alert posted"
    )
    assert len(sent) == 2


def test_a_dispatch_that_reuses_a_worktree_skips_the_disk_guard(tmp_path, host):
    host["readings"] = [_disk(99.0)]

    decision = adm.evaluate("workspace-write", tmp_path, thresholds=_LIMITS, disk_guard=False)

    assert decision.admitted and not decision.disk_refused
    assert host["probes"] == 0 and host["alerts"] == []
    assert "disk check skipped (dispatch reuses an existing worktree)" in decision.summary()


# --- Two watermarks (#10871): the cto alert at 85, refuse only at 90 ---------------------------------------------


@pytest.mark.parametrize("mode", ["workspace-write", "danger"])
@pytest.mark.parametrize(("used", "admitted"), [(84.0, True), (85.0, True), (89.0, True), (90.0, False)])
def test_write_dispatch_boundaries_refuse_only_at_90(tmp_path, host, mode, used, admitted):
    host["readings"] = [_disk(used)]

    decision = adm.evaluate(mode, tmp_path, thresholds=_LIMITS)

    assert decision.admitted is admitted
    assert host["probes"] == 1
    assert decision.disk_refused is (not admitted)


@pytest.mark.parametrize("mode", ["read-only", "research"])
@pytest.mark.parametrize("used", [84.0, 85.0, 89.0, 90.0, 99.0])
def test_read_only_dispatch_is_admitted_unchecked_at_every_level(tmp_path, host, monkeypatch, mode, used):
    host["readings"] = [_disk(used)]

    decision = adm.evaluate(mode, tmp_path, thresholds=_LIMITS)
    early = adm.precheck(mode, thresholds=_LIMITS)

    assert decision.admitted and decision.exempt and decision.disk is None
    assert early is None
    assert host["probes"] == 0 and host["alerts"] == []


@pytest.mark.parametrize(("used", "steps_aside"), [(89.0, False), (90.0, True)])
def test_precheck_steps_aside_for_the_disk_only_at_the_refuse_watermark(host, monkeypatch, used, steps_aside):
    monkeypatch.setattr(
        adm,
        "probe_host",
        lambda: adm.HostProbe(mem_available_bytes=1 * _GIB, load1=1.0, cpu_count=8, proc_available=True),
    )
    host["readings"] = [_disk(used)]

    early = adm.precheck("workspace-write", thresholds=_LIMITS)

    assert (early is None) is steps_aside


def test_default_watermarks_are_85_and_90():
    defaults = adm.config_defaults()
    assert (defaults.disk_reap_used_percent, defaults.disk_refuse_used_percent) == (85.0, 90.0)


@pytest.mark.parametrize(
    "env",
    [
        {"DISPATCH_DISK_REAP_USED_PERCENT": "90", "DISPATCH_DISK_REFUSE_USED_PERCENT": "90"},
        {"DISPATCH_DISK_REAP_USED_PERCENT": "92", "DISPATCH_DISK_REFUSE_USED_PERCENT": "88"},
        {"DISPATCH_DISK_REFUSE_USED_PERCENT": "85"},  # equal to the default reap watermark
        {"DISPATCH_DISK_REFUSE_USED_PERCENT": "70"},  # below the default reap watermark
        {"DISPATCH_DISK_REAP_USED_PERCENT": "90"},  # equal to the default refuse watermark
        {"DISPATCH_DISK_REAP_USED_PERCENT": "99"},
    ],
)
def test_reap_not_below_refuse_is_a_typed_configuration_error(env):
    with pytest.raises(adm.DiskWatermarkConfigError, match=r"DISPATCH_DISK_REAP_USED_PERCENT=.* must be below"):
        adm.load_thresholds(env)
    assert issubclass(adm.DiskWatermarkConfigError, ValueError)


def test_valid_watermark_overrides_are_read():
    custom = adm.load_thresholds({"DISPATCH_DISK_REAP_USED_PERCENT": "70", "DISPATCH_DISK_REFUSE_USED_PERCENT": "95"})
    assert (custom.disk_reap_used_percent, custom.disk_refuse_used_percent) == (70.0, 95.0)


@pytest.mark.parametrize("raw", ["101", "-1", "lots", "nan"])
def test_invalid_reap_watermark_keeps_the_existing_handling(raw):
    with pytest.raises(ValueError, match="DISPATCH_DISK_REAP_USED_PERCENT"):
        adm.load_thresholds({"DISPATCH_DISK_REAP_USED_PERCENT": raw})


def test_watermarks_come_from_the_deployment_env_file_too(tmp_path):
    env_file = tmp_path / "dispatch-admission.env"
    env_file.write_text("DISPATCH_DISK_REFUSE_USED_PERCENT=85\n")

    with pytest.raises(adm.DiskWatermarkConfigError):
        adm.load_thresholds({"DISPATCH_ADMISSION_ENV_FILE": str(env_file)})


REAL_HOST = wd.Host


PROC_NET_TCP = textwrap.dedent(
    """\
      sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
       0: 0100007F:223E 00000000:0000 0A 00000000:00000055 00:00000000 00000000  1000        0 1 1
       1: 0100007F:223E 0100007F:9C40 08 00000000:00000000 00:00000000 00000000  1000        0 2 1
       2: 0100007F:223E 0100007F:9C41 08 00000000:00000000 00:00000000 00000000  1000        0 3 1
       3: 0100007F:223E 0100007F:9C42 01 00000000:00000000 00:00000000 00000000  1000        0 4 1
       4: 0100007F:9C43 0100007F:223E 05 00000000:00000000 00:00000000 00000000  1000        0 5 1
       5: 0100007F:1F90 00000000:0000 0A 00000000:00000003 00:00000000 00000000  1000        0 6 1
    """
)


def _diagnostics_document(**changes: object) -> str:
    return json.dumps({
        "schema": "learn-ukrainian.hang-diagnostics",
        "schema_version": 1,
        "origin": "scripts/rag/hang_diagnostics.v1.json",
        "module": "scripts.rag.hang_diagnostics",
        **changes,
    })


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("raw", [
    b"\xff", b"{", b"[]", b"null", b"{}",
    _diagnostics_document(extra=True).encode(),
    _diagnostics_document(schema="other").encode(),
    _diagnostics_document(schema_version=True).encode(),
    _diagnostics_document(schema_version=1.0).encode(),
    _diagnostics_document(schema_version=2).encode(),
    _diagnostics_document(origin="elsewhere.json").encode(),
    _diagnostics_document(module=None).encode(),
    b'{"schema": "first", "schema": "second"}',
    _diagnostics_document(module=float("nan")).encode(),
    _diagnostics_document(module=float("inf")).encode(),
    _diagnostics_document(module=float("-inf")).encode(),
    b"[" * 1500 + b"]" * 1500,
])
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_invalid_json(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "hang_diagnostics.v1.json"
    path.write_bytes(raw)
    with pytest.raises(ImportError, match="hang diagnostics configuration"):
        wd._gates_module_name(path)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("field", ["schema", "schema_version", "origin", "module"])
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_each_missing_field(tmp_path: Path, field: str) -> None:
    document = json.loads(_diagnostics_document())
    del document[field]
    path = tmp_path / "hang_diagnostics.v1.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ImportError, match="fields mismatch"):
        wd._gates_module_name(path)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("name", [
    "", "scripts", "os", ".scripts.curriculum", "scripts..curriculum", "scripts.Curriculum",
    " scripts.curriculum", "scripts.curriculum\n", "scripts.curriculum:main", "scripts.сurriculum",
    "scripts.foo-bar", "scripts./curriculum", "scripts.sqlite3", "scripts._sqlite3",
])
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_malformed_or_sqlite_names(tmp_path: Path, name: str) -> None:
    path = tmp_path / "hang_diagnostics.v1.json"
    path.write_text(_diagnostics_document(module=name))
    with pytest.raises(ImportError):
        wd.load_configured_module(wd._gates_module_name(path))


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "fifo", "oversized"])
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_unsafe_data_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "hang_diagnostics.v1.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text(_diagnostics_document())
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        path.write_bytes(b" " * (wd._GATES_MAX_BYTES + 1))
    with pytest.raises((ImportError, OSError)):
        wd._gates_module_name(path)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_replaced_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "hang_diagnostics.v1.json"
    path.write_text(_diagnostics_document())
    replacement = tmp_path / "replacement.json"
    replacement.write_text(_diagnostics_document())
    real_open = os.open

    def replace_before_open(target, flags):
        replacement.replace(path)
        return real_open(target, flags)

    monkeypatch.setattr(wd.os, "open", replace_before_open)
    with pytest.raises(ImportError, match="changed while opening"):
        wd._gates_module_name(path)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_file_growth(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "hang_diagnostics.v1.json"
    path.write_text(_diagnostics_document())
    real_open = os.open

    def grow_before_open(target, flags):
        path.write_bytes(b" " * (wd._GATES_MAX_BYTES + 1))
        return real_open(target, flags)

    monkeypatch.setattr(wd.os, "open", grow_before_open)
    with pytest.raises(ImportError, match="too large"):
        wd._gates_module_name(path)


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize("origin", [None, "foreign/hang_diagnostics.py"])
@pytest.mark.usefixtures("watchdog_diagnostics_product")
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_diagnostics_refuse_foreign_cache_when_loaded(
    tmp_path: Path, monkeypatch, origin: str | None,
) -> None:
    module = ModuleType("scripts.rag.hang_diagnostics")
    module.__file__ = str(tmp_path / origin) if origin else None
    monkeypatch.setitem(sys.modules, module.__name__, module)
    runpy.run_path(wd.__file__, run_name="startup_probe")
    with pytest.raises(ImportError, match="not a file in this checkout"):
        wd.hang_diagnostics()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_parse_proc_net_tcp_counts_server_side_states_and_backlog() -> None:
    counts, backlog = wd.parse_proc_net_tcp(PROC_NET_TCP, 0x223E)
    # The client-side FIN-WAIT-2 socket (local port 0x9C43) is not the server's.
    assert counts == {"LISTEN": 1, "CLOSE-WAIT": 2, "ESTAB": 1}
    assert backlog == 0x55


class FakeHost:
    def __init__(self, tmp_path: Path, *, active: str = "active", pid: int = 4242) -> None:
        self.tmp_path = tmp_path
        self.healthy: list[bool] = []
        self.active = active
        self.pid = pid
        self.started = 7_000_000
        self.clock = 1_000_000.0
        self.probes = 0
        self.captures: list[int] = []
        self.restarts: list[str] = []
        self.published: list[tuple[str, str]] = []
        self.publish_fails = 0
        self.during_capture: Callable[[], None] | None = None

    def host(self) -> wd.Host:
        return REAL_HOST(
            probe=self.probe,
            unit_state=self.unit_state,
            restart=self.restart,
            capture=self.capture,
            wait_healthy=lambda url, timeout, deadline: (True, "healthy 3s after restart"),
            publish=self.publish,
            now=lambda: self.clock,
        )

    def unit_state(self, unit: str) -> wd.UnitState:
        sub = "running" if self.active == "active" else "dead"
        return wd.UnitState(self.active, sub, self.pid, self.started)

    def probe(self, url: str, timeout: float) -> tuple[bool, str]:
        self.probes += 1
        ok = self.healthy.pop(0) if self.healthy else False
        return ok, "ok" if ok else "TimeoutError: timed out after 5.0s"

    def capture(self, pid: int, dump_dir: Path, port: int, now: float, *, reason: str) -> Path:
        self.captures.append(pid)
        path = dump_dir / f"incident-{len(self.captures)}"
        path.mkdir(parents=True, exist_ok=True)
        if self.during_capture is not None:
            self.during_capture()
        return path

    def restart(self, unit: str) -> str:
        self.restarts.append(unit)
        return "killed and started"

    def publish(self, message: str, key: str) -> None:
        if self.publish_fails:
            self.publish_fails -= 1
            raise RuntimeError("bus down")
        self.published.append((message, key))


def _config(tmp_path: Path, **overrides: object) -> wd.Config:
    values: dict[str, object] = {
        "dump_dir": tmp_path / "dumps",
        "apply": True,
        "health_url": "http://127.0.0.1:18766/health",
    }
    values.update(overrides)
    return wd.Config(**values)  # type: ignore[arg-type]


def _tick(fake: FakeHost, config: wd.Config, healthy: bool, *, advance: float = 15.0) -> str:
    fake.healthy.append(healthy)
    result = wd.run_once(config, fake.host())
    fake.clock += advance
    return result


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_failures_below_threshold_take_no_action(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path)
    assert "failure 1/3" in _tick(fake, config, False)
    assert "failure 2/3" in _tick(fake, config, False)
    assert "healthy" in _tick(fake, config, True)
    assert "failure 1/3" in _tick(fake, config, False)
    assert fake.captures == [] and fake.restarts == [] and fake.published == []


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_sustained_failure_dumps_before_restart_and_notifies_once(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0)
    order: list[str] = []
    host = fake.host()
    host.capture = lambda *a, **k: (order.append("dump"), fake.capture(*a, **k))[1]
    host.restart = lambda unit: (order.append("restart"), fake.restart(unit))[1]
    for _ in range(3):
        fake.healthy.append(False)
        result = wd.run_once(config, host)
        fake.clock += 15
    assert order == ["dump", "restart"]
    assert "sustained failure" in result and "cto notified" in result
    assert len(fake.published) == 1
    message, key = fake.published[0]
    assert "HUNG" in message and "Dump captured: " in message
    assert key.startswith("lu-sources-watchdog:")

    # A second hang inside the same incident: dump and restart again, no new report.
    for _ in range(3):
        fake.healthy.append(False)
        wd.run_once(config, host)
        fake.clock += 15
    assert fake.captures == [4242, 4242]
    assert len(fake.restarts) == 2
    assert len(fake.published) == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_restart_budget_stops_a_restart_loop(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0, max_restarts=2)
    results = [_tick(fake, config, False) for _ in range(12)]
    assert len(fake.restarts) == 2
    assert len(fake.captures) == 2
    assert any("restart budget exhausted" in result for result in results)
    assert len(fake.published) == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_budget_frees_after_the_window(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0, max_restarts=1, restart_window=100)
    for _ in range(6):
        _tick(fake, config, False)
    assert len(fake.restarts) == 1
    fake.clock += 100
    for _ in range(3):
        _tick(fake, config, False)
    assert len(fake.restarts) == 2


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_startup_grace_suppresses_counting_after_restart(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=60)
    for _ in range(3):
        _tick(fake, config, False)
    assert len(fake.restarts) == 1
    # 45 s of failures inside the 60 s grace do not count.
    assert "startup grace" in _tick(fake, config, False)
    assert "startup grace" in _tick(fake, config, False)
    assert "startup grace" in _tick(fake, config, False)
    assert len(fake.restarts) == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_inactive_unit_is_never_touched(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path, active="inactive", pid=0), _config(tmp_path)
    for _ in range(5):
        assert "systemd owns" in _tick(fake, config, False)
    assert fake.captures == [] and fake.restarts == [] and fake.published == []


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, apply=False, failures_to_act=1)
    assert "would dump" in _tick(fake, config, False)
    assert fake.captures == [] and fake.restarts == [] and fake.published == []
    assert not (tmp_path / "dumps" / wd.STATE_FILE).exists()


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_failed_publish_is_retried_on_the_next_run(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0)
    fake.publish_fails = 1
    for _ in range(3):
        result = _tick(fake, config, False)
    assert "notification failed" in result
    assert fake.published == []
    assert "cto notified" in _tick(fake, config, True)
    assert len(fake.published) == 1
    _tick(fake, config, True)
    assert len(fake.published) == 1


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_incident_closes_after_quiet_period_and_next_hang_reports_again(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0, incident_quiet=300)
    for _ in range(3):
        _tick(fake, config, False)
    assert len(fake.published) == 1
    assert "closed" not in _tick(fake, config, True)
    fake.clock += 300
    assert "closed" in _tick(fake, config, True)
    for _ in range(3):
        _tick(fake, config, False)
    assert len(fake.published) == 2
    state = json.loads((tmp_path / "dumps" / wd.STATE_FILE).read_text(encoding="utf-8"))
    assert state["incident"]["notified"] is True


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_restart_budget_survives_incident_closure(tmp_path: Path) -> None:
    """Sol's reproducer: closing an incident must not refill the hourly budget."""
    fake, config = FakeHost(tmp_path), _config(tmp_path)
    failures: list[str] = []
    for _cycle in range(4):
        failures.extend(_tick(fake, config, False) for _ in range(3))
        closed = [_tick(fake, config, True) for _ in range(50)]
        assert any("closed" in result for result in closed)
    # Four cycles fit in about 2,600 s, inside the one-hour window.
    assert fake.clock - 1_000_000.0 < config.restart_window
    assert len(fake.restarts) == 3
    assert "restart budget exhausted (3 in 3600s)" in failures[-1]
    state = json.loads((tmp_path / "dumps" / wd.STATE_FILE).read_text(encoding="utf-8"))
    assert len(state["restart_history"]) == 3
    # The fourth incident opened with the budget spent: it is still reported, once.
    assert len(fake.published) == 4
    assert "Action: restart budget exhausted" in fake.published[-1][0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_restart_history_keeps_only_the_window(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0, restart_window=100)
    for _ in range(3):
        _tick(fake, config, False)
    fake.clock += 200
    for _ in range(3):
        _tick(fake, config, False)
    state = json.loads((tmp_path / "dumps" / wd.STATE_FILE).read_text(encoding="utf-8"))
    assert state["restart_history"] == [fake.clock - 15]
    assert len(fake.restarts) == 2


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_recovery_during_capture_aborts_the_restart(tmp_path: Path) -> None:
    """Sol's reproducer: health restored while dumping; the server must not be killed."""
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0)
    fake.during_capture = lambda: fake.healthy.append(True)
    for _ in range(2):
        _tick(fake, config, False)
    result = _tick(fake, config, False)
    assert fake.captures == [4242]
    assert fake.restarts == []
    assert fake.probes == 4, "health is probed again after the dump"
    assert "restart aborted: recovered" in result
    message, _key = fake.published[0]
    assert "restart aborted: recovered" in message
    state = json.loads((tmp_path / "dumps" / wd.STATE_FILE).read_text(encoding="utf-8"))
    assert state["failures"] == 0 and state.get("restart_history", []) == []


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.parametrize(
    ("change", "seen"),
    [
        (lambda fake: setattr(fake, "pid", 5151), "pid 5151"),
        (lambda fake: setattr(fake, "started", 7_000_999), "pid 4242"),
        (lambda fake: setattr(fake, "active", "deactivating"), "deactivating"),
    ],
    ids=["new-main-pid", "same-pid-new-process", "unit-stopping"],
)
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_changed_unit_during_capture_aborts_the_restart(
    tmp_path: Path, change: Callable[[FakeHost], None], seen: str
) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0)
    fake.during_capture = lambda: change(fake)
    for _ in range(3):
        result = _tick(fake, config, False)
    assert fake.captures == [4242]
    assert fake.restarts == []
    assert "restart aborted: changed" in result and seen in result


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_unreadable_unit_state_after_capture_aborts_the_restart(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0)
    host = fake.host()
    reads: list[str] = []

    def unit_state(unit: str) -> wd.UnitState:
        reads.append(unit)
        if fake.captures:
            raise wd.WatchdogError("systemctl show exited 1: bus unavailable")
        return fake.unit_state(unit)

    host.unit_state = unit_state
    for _ in range(3):
        fake.healthy.append(False)
        result = wd.run_once(config, host)
        fake.clock += 15
    assert fake.restarts == []
    assert "restart aborted: unverified" in result and "bus unavailable" in result
    assert len(reads) == 4


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_failures_of_a_replaced_process_do_not_carry_over(tmp_path: Path) -> None:
    """An operator restart between probes gives the new process a fresh count."""
    fake, config = FakeHost(tmp_path), _config(tmp_path, startup_grace=0)
    _tick(fake, config, False)
    _tick(fake, config, False)
    fake.pid, fake.started = 5151, 7_100_000
    assert "failure 1/3" in _tick(fake, config, False)
    assert "failure 2/3" in _tick(fake, config, False)
    assert fake.restarts == []
    assert "sustained failure" in _tick(fake, config, False)
    assert fake.captures == [5151]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_only_the_sources_unit_reaches_the_restart_callback(tmp_path: Path) -> None:
    fake, config = FakeHost(tmp_path), _config(tmp_path, unit="unrelated.service", failures_to_act=1)
    with pytest.raises(wd.WatchdogError, match=r"refusing unit 'unrelated\.service'"):
        wd.run_once(config, fake.host())
    assert fake.restarts == [] and fake.captures == [] and fake.probes == 0


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_cli_refuses_another_unit_before_any_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sol's reproducer: ``run-once --unit unrelated.service --apply --failures-to-act 1``."""
    fake = FakeHost(tmp_path)
    monkeypatch.setattr(wd, "Host", lambda **_kwargs: fake.host())
    monkeypatch.setattr(wd, "default_publisher", lambda: fake.publish)
    argv = ["--dump-dir", str(tmp_path / "dumps"), "run-once", "--unit", "unrelated.service", "--apply"]
    with pytest.raises(SystemExit) as raised:
        wd.main([*argv, "--failures-to-act", "1"])
    assert raised.value.code == 2
    assert fake.restarts == [] and fake.captures == [] and fake.probes == 0
    # The sources unit itself is still served.
    assert wd.main([*argv[:3], "--unit", wd.UNIT, "--apply", "--failures-to-act", "1"]) == 0
    assert fake.restarts == [wd.UNIT]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_systemd_actions_refuse_another_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("systemctl must not run for another unit")

    monkeypatch.setattr(wd.subprocess, "run", no_subprocess)
    for action in (wd.systemd_kill_and_start, wd.systemd_unit_state):
        with pytest.raises(wd.WatchdogError, match="refusing unit"):
            action("unrelated.service")


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_systemd_unit_state_reads_the_main_process_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def show(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = "ActiveState=active\nSubState=running\nMainPID=299873\nExecMainStartTimestampMonotonic=1341302900692\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(wd.subprocess, "run", show)
    assert wd.systemd_unit_state(wd.UNIT) == wd.UnitState("active", "running", 299873, 1341302900692)
    assert "ActiveState,SubState,MainPID,ExecMainStartTimestampMonotonic" in calls[0]


@pytest.mark.usefixtures('configured_roots')
@pytest.mark.usefixtures("watchdog_diagnostics_product")
def test_prune_keeps_newest_incidents_and_drops_dead_pid_logs(tmp_path: Path) -> None:
    dump_dir = tmp_path / "dumps"
    for stamp in ("20261015T010000Z", "20261015T020000Z", "20261015T030000Z"):
        (dump_dir / f"incident-{stamp}-pid1").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    (proc_root / "77").mkdir(parents=True)
    (dump_dir / "faulthandler-77.log").write_text("", encoding="utf-8")
    (dump_dir / "faulthandler-78.log").write_text("", encoding="utf-8")
    for stamp in ("20261015T010000Z", "20261015T020000Z", "20261015T030000Z"):
        (dump_dir / f"slow-call-{stamp}-pid77.txt").write_text("", encoding="utf-8")
    wd.prune_dump_dir(dump_dir, keep=2, proc_root=proc_root)
    assert sorted(path.name for path in dump_dir.iterdir()) == [
        "faulthandler-77.log",
        "incident-20261015T020000Z-pid1",
        "incident-20261015T030000Z-pid1",
        "slow-call-20261015T020000Z-pid77.txt",
        "slow-call-20261015T030000Z-pid77.txt",
    ]




_DIAGNOSTICS_INPUTS = {'hang_diagnostics.py': b'"""Thread dumps for a hung sources MCP server (#10674).\n\nThe standalone server registers a ``faulthandler`` dump on ``SIGUSR1`` at\nstartup. ``faulthandler`` writes every thread\'s Python stack from inside the\nsignal handler without taking the GIL, so it still works when every thread,\nincluding the event loop, is blocked on a lock. The watchdog\n(``scripts/orchestration/sources_watchdog.py``) sends the signal and copies\nthe new bytes into an incident directory before it restarts the service.\n\nBoth sides find the dump directory the same way: ``LU_SOURCES_DUMP_DIR`` when\nset, otherwise ``batch_state/sources-dumps`` under the project root. The\ndirectory is ignored by Git and owner-only. Stacks carry file names, line\nnumbers and function names, never argument values.\n"""\n\nfrom __future__ import annotations\n\nimport faulthandler\nimport os\nimport signal\nfrom collections.abc import Mapping\nfrom pathlib import Path\nfrom typing import TextIO\n\nDUMP_DIR_ENV = "LU_SOURCES_DUMP_DIR"\nDEFAULT_DUMP_SUBDIR = Path("batch_state") / "sources-dumps"\nDUMP_SIGNAL = signal.SIGUSR1\n\n# faulthandler keeps its own reference, but the file object must stay open for\n# the life of the process; keep one here too so it is never collected.\n_DUMP_FILE: TextIO | None = None\n\n\ndef resolve_dump_dir(project_root: Path, environ: Mapping[str, str] | None = None) -> Path:\n    environ = os.environ if environ is None else environ\n    override = environ.get(DUMP_DIR_ENV, "").strip()\n    return Path(override) if override else project_root / DEFAULT_DUMP_SUBDIR\n\n\ndef faulthandler_log_path(dump_dir: Path, pid: int) -> Path:\n    return dump_dir / f"faulthandler-{pid}.log"\n\n\ndef ensure_dump_dir(dump_dir: Path) -> Path:\n    dump_dir.mkdir(parents=True, exist_ok=True)\n    os.chmod(dump_dir, 0o700)\n    return dump_dir\n\n\ndef install_signal_dump(dump_dir: Path, *, signum: int = DUMP_SIGNAL) -> Path:\n    """Register an all-thread stack dump on ``signum`` and on fatal signals.\n\n    Returns the per-process log the dumps are appended to.\n    """\n    global _DUMP_FILE\n    ensure_dump_dir(dump_dir)\n    path = faulthandler_log_path(dump_dir, os.getpid())\n    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n    handle = os.fdopen(fd, "a", encoding="utf-8")\n    faulthandler.register(signum, file=handle, all_threads=True, chain=False)\n    # A watchdog SIGABRT or a crash leaves the same evidence in the same file.\n    faulthandler.enable(file=handle, all_threads=True)\n    if _DUMP_FILE is not None and _DUMP_FILE is not handle:\n        _DUMP_FILE.close()\n    _DUMP_FILE = handle\n    return path\n', 'hang_diagnostics.v1.json': b'{\n  "schema": "learn-ukrainian.hang-diagnostics",\n  "schema_version": 1,\n  "origin": "scripts/rag/hang_diagnostics.v1.json",\n  "module": "scripts.rag.hang_diagnostics"\n}\n'}


@pytest.fixture
def watchdog_diagnostics_product(agent_runtime_roots):
    for name, value in _DIAGNOSTICS_INPUTS.items():
        path = agent_runtime_roots.product / "scripts/rag" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

pytestmark = [pytest.mark.usefixtures('configured_roots')]
