"""Host admission for write-capable dispatches (#8645 part A).

Every record lives under tmp_path; memory and load come from a monkeypatched
probe, never from allocating memory or generating load.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import math
import os
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from lu_machinery.common import json_depth
from lu_machinery.dispatcher import host_admission, pool_selector
from lu_machinery.orchestration import dispatch_admission as adm
from lu_machinery.orchestration import worktree_prep
from tests.helpers.product_tree import configured_roots as configured_roots

_GIB = 1024**3
_LIMITS = adm.Thresholds(max_live_write_workers=2, min_mem_available_gib=3.5, max_load_per_cpu=1.5)


pytestmark = pytest.mark.usefixtures("configured_roots")


def _healthy_probe() -> adm.HostProbe:
    return adm.HostProbe(mem_available_bytes=12 * _GIB, load1=2.0, cpu_count=8, proc_available=True)


def _record(tasks_dir: Path, task_id: str, *, status: str = "running", mode: str = "workspace-write", **fields) -> Path:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "task_id": task_id,
        "status": status,
        "mode": mode,
        "pid": fields.pop("pid", 1000 + len(list(tasks_dir.glob("*.json")))),
        "run_nonce": f"nonce-{task_id}",
        "started_at": fields.pop("started_at", datetime.now(UTC).isoformat()),
        **fields,
    }
    path = tasks_dir / f"{task_id}.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def probe(monkeypatch):
    current = {"probe": _healthy_probe()}
    monkeypatch.setattr(adm, "probe_host", lambda *_a, **_k: current["probe"])
    return current


def test_cap_reached_is_refused(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "a", pid=11)
    _record(tasks, "b", pid=12, mode="danger")

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert not decision.admitted
    assert decision.live_task_ids == ("a", "b")
    [failure] = decision.failures
    assert "live write workers 2/2 reached the cap (DISPATCH_MAX_LIVE_WRITE_WORKERS=2)" in failure
    assert "--force-admission" in decision.refusal_line()


def test_meter_cap_defers_only_the_meter_at_its_limit(tmp_path, probe):
    """Defect class: a full meter admits another write or blocks a different account."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    _record(tasks, "codex-b", agent="codex", codex_account="b-sub")
    limits = adm.load_thresholds({
        "DISPATCH_MAX_LIVE_WRITE_WORKERS_PER_METER": "1",
        "DISPATCH_MIN_METER_LAUNCH_INTERVAL_S": "0",
    })
    blocked = adm.evaluate("workspace-write", tasks, thresholds=limits,
                           pid_alive=lambda _pid: True, meter="codex:b", now=now)
    admitted = adm.evaluate("workspace-write", tasks, thresholds=limits,
                            pid_alive=lambda _pid: True, meter="codex:a", now=now)

    assert not blocked.admitted
    assert blocked.meter is not None and blocked.meter.defer == adm.MeterDefer.CAP
    assert "meter codex:b: live write workers 1/1" in blocked.refusal_line()
    assert "subscription_meter_defer=cap" in blocked.refusal_line()
    assert admitted.admitted
    assert "meter codex:a: live write workers 0/1" in admitted.summary()


def test_meter_cadence_measures_from_the_last_launch_even_if_it_finished(tmp_path, probe):
    """Defect class: a meter relaunches inside its minimum interval."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    _record(tasks, "finished", status="done", agent="claude", started_at=now.isoformat())
    # A refusal does not extend the interval.
    _record(tasks, "refused", status="failed", agent="claude", never_started_reason="admission_capacity",
            started_at=(now + timedelta(seconds=4)).isoformat())
    limits = replace(_LIMITS, min_meter_launch_interval_s=5)
    blocked = adm.evaluate("workspace-write", tasks, thresholds=limits, meter="claude:a",
                           now=now + timedelta(seconds=4))
    admitted = adm.evaluate("workspace-write", tasks, thresholds=limits, meter="claude:a",
                            now=now + timedelta(seconds=5))

    assert not blocked.admitted
    assert blocked.meter is not None and blocked.meter.defer == adm.MeterDefer.CADENCE
    assert "launch interval 5s, subscription_meter_defer=cadence (retry in 1.00s)" in blocked.refusal_line()
    assert admitted.admitted


def test_meter_exhaustion_defers_and_unknown_usage_keeps_fixed_cap(tmp_path, probe, monkeypatch):
    """Defect class: throttled or depleted meters admit writes, or unknown pace raises the ceiling."""
    tasks = tmp_path / "tasks"
    now = datetime.now(UTC)
    limits = replace(adm.load_thresholds({}), max_live_write_workers=10, min_meter_launch_interval_s=0)
    assert limits.max_live_write_workers_per_meter == 8
    for index in range(8):
        _record(tasks, f"worker-{index}", agent="codex")

    def decide(row, *, stale=False):
        budget = {"diagnostics": {"stale": stale}, "generated_at": now.isoformat(),
                  "codex_accounts": {"codex:a": row}}
        return adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                            meter="codex:a", meter_budget=budget, now=now)

    for row, reason in (
        ({"status": "rate_limited"}, "rate_limited"),
        ({"status": "cool", "runtime": {"rate_limited": 1}}, "rate_limited"),
        ({"status": "near_cap"}, "near_cap"),
        ({"remaining_pct": 0}, "exhausted"),
    ):
        decision = decide(row)
        assert not decision.admitted
        assert f"subscription_meter_defer={reason}" in decision.refusal_line()
    fresh = {
        "status": "cool", "freshness": "fresh", "age_s": 0,
        "forecast": {"remaining_pct": 20, "reset_at": (now + timedelta(hours=2)).isoformat(),
                     "burn_pct_per_hour": 20},
    }
    for result in (decide({}), decide(fresh, stale=True), decide(fresh)):
        assert not result.admitted
        assert result.meter is not None and result.meter.cap == 8
        assert result.meter.defer == adm.MeterDefer.CAP
    _record(tasks, "claude-b-limited", status="rate_limited", agent="claude", claude_account="b",
            finished_at=now.isoformat())
    for account, expected in (("b", adm.MeterDefer.RATE_LIMITED), ("a", None)):
        result = adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                              meter=f"claude:{account}", now=now)
        assert result.meter is not None and result.meter.defer == expected
        assert result.admitted == (expected is None)

    def overdeep_budget():
        return json_depth.loads("[" * 600 + "]" * 600, max_depth=512)

    monkeypatch.setattr(pool_selector, "read_budget", overdeep_budget)
    monkeypatch.setattr(host_admission, "read_planning", overdeep_budget)
    monkeypatch.setattr(host_admission, "tasks_dir", lambda: tmp_path / "overdeep-tasks")
    monkeypatch.setattr(adm, "load_thresholds", lambda: limits)
    spec = SimpleNamespace(mode="workspace-write", agent="codex", model="gpt-6.2-sol",
                           claude_account=None, codex_account="b-sub")
    result = host_admission.check(spec.mode, dry_run=True, disk_guard=False, spec=spec)
    assert result.admitted
    assert "meter codex:b: live write workers 0/8 (fixed cap: planning forecast unavailable)" in result.summary()


def test_paced_meter_cap_follows_the_forecast_not_the_live_count(tmp_path, probe, monkeypatch):
    """Defect classes: a paced cap above the fixed ceiling or below 1, a cap that ignores measured burn,
    a cap that moves when a worker finishes without a meter change, and an invalid, missing, expired or
    unreadable forecast (redirected, failed, short, oversized or slow Monitor reply, or a Monitor URL that is
    not plain HTTP to an IP literal) or an unmeasured rate that changes the fixed ceiling."""
    now = datetime.now(UTC)

    def view(*, pool="claude:b", remaining=27.0, hours=40.0, rate=0.3, samples=120, **row):
        reset = (now + timedelta(hours=hours)).isoformat() if hours is not None else None
        fields = {"pool": pool, "remaining_pct": remaining, "reset_at": reset, "points_per_worker_hour": rate,
                  "worker_samples": samples}
        return {"schema_version": "model-planning.v1", "capacity": [{**fields, **row}]}

    # 27 points left, 40 hours, 0.3 points per worker-hour: 2.25 workers spend it by the reset.
    assert adm.meter_cap("claude:b", 8, view(), now) == (
        3, "paced cap: 27% left, 40.0h to reset, 0.3 points per worker-hour over 120 tasks, ceiling 8")
    for forecast, cap in (
        (view(remaining=24, hours=24, rate=0.5), 2),
        (view(remaining=10, hours=40, rate=0.25), 1),
        (view(remaining=10, hours=30, rate=1 / 3), 2),  # the float below a third: the exact quotient is above 1
        (view(remaining=0.0), 1),
        (view(remaining=0, hours=1e-9, rate=5e-324), 1),  # nothing left, even when the denominator underflows
        (view(remaining=10, hours=1 / 6, rate=10, samples=10), 6),  # exactly six: no float error lifts it to seven
        (view(remaining=5e-324, hours=0.5, rate=5e-324), 2),  # a subnormal product does not underflow to "unbounded"
        (view(remaining=1, hours=100, rate=1, samples=10), 1),
        (view(remaining=100, rate=0.0001), 8),
        (view(remaining=100, hours=1e-9, rate=5e-324), 8),  # the denominator underflows to zero
        (view(pool="grok"), 3),
    ):
        meter = "grok+cursor" if forecast["capacity"][0]["pool"] == "grok" else "claude:b"
        assert adm.meter_cap(meter, 8, forecast, now)[0] == cap
    assert adm.meter_cap("claude:b", 2, view(), now)[0] == 2
    assert adm.meter_cap("claude:b", 0, view(), now)[0] == 0

    for forecast, reason in (
        (None, "planning forecast unavailable"),
        ([], "planning forecast unavailable"),
        ({**view(), "schema_version": "model-planning.v2"}, "planning forecast unavailable"),
        ({"schema_version": "model-planning.v1", "capacity": {}}, "planning forecast unavailable"),
        (view(pool="claude:a"), "no single forecast row for this meter"),
        ({**view(), "capacity": view()["capacity"] * 2}, "no single forecast row for this meter"),
        *((view(remaining=value), "remaining allowance unknown")
          for value in (None, True, "27", math.nan, math.inf, -1, 100.5, 10**309)),
        *((view(reset_at=value), "reset time unknown")
          for value in (None, 7, "", "not a time", "2026-11-23T22:00:00", "2026-11-23T22:00:00+00:00" + " " * 20)),
        (view(hours=0), "reset time passed"),
        (view(hours=-3), "reset time passed"),
        *((view(rate=value), "no measured points per worker-hour")
          for value in (None, 0, -0.2, False, "0.3", math.nan, math.inf, 10**309)),
        *((view(samples=value), "rate measured over fewer than 10 worker tasks")
          for value in (None, 9, 0, -1, True, 10.0, "120")),
    ):
        assert adm.meter_cap("claude:b", 8, forecast, now) == (8, f"fixed cap: {reason}")

    tasks = tmp_path / "tasks"
    limits = replace(_LIMITS, max_live_write_workers=10, min_meter_launch_interval_s=0)
    started = (now - timedelta(minutes=10)).isoformat()
    for index in range(3):
        _record(tasks, f"claude-b-{index}", agent="claude", claude_account="b", started_at=started)

    def decide():
        return adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                            meter="claude:b", meter_forecast=view(), now=now)

    full = decide()
    assert not full.admitted and full.meter is not None
    assert (full.meter.live, full.meter.cap, full.meter.defer) == (3, 3, adm.MeterDefer.CAP)
    assert "meter claude:b: live write workers 3/3 (paced cap: 27% left" in full.refusal_line()
    _record(tasks, "claude-b-0", status="done", agent="claude", claude_account="b", started_at=started)
    finished = decide()
    assert finished.admitted and finished.meter is not None
    assert (finished.meter.live, finished.meter.cap) == (2, 3)

    # The dispatcher reads the view from a Monitor; every transport failure is a missing forecast.
    mode = {"name": "ok"}
    good = json.dumps(view(remaining=4, hours=50, rate=0.1)).encode()
    stalls = ("stalled_headers", "stalled_body", "stalled_closing_body")
    hung_up = {name: threading.Event() for name in stalls}  # the reader shut its socket at the deadline

    class Monitor(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            name = mode["name"]
            if name in stalls:
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                if name != "stalled_headers":
                    closing = b"Connection: close\r\n" if name == "stalled_closing_body" else b""
                    self.wfile.write(closing + f"Content-Length: {len(good)}\r\n\r\n".encode() + good[:10])
                self.wfile.flush()
                self.connection.settimeout(10)
                with contextlib.suppress(OSError):
                    if self.rfile.read(1) == b"":
                        hung_up[name].set()
                self.close_connection = True
                return
            body = {"oversize": json.dumps({**view(), "pad": "x" * host_admission.PLANNING_BODY_LIMIT}).encode(),
                    "not_json": b"{"}.get(name, good)
            if name == "chunked":
                self.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                                 + f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                self.close_connection = True
                return
            if name == "close_delimited":
                self.wfile.write(b"HTTP/1.0 200 OK\r\n\r\n" + body)
                self.close_connection = True
                return
            if name in ("hidden_length", "hidden_chunked"):  # framing hidden behind a malformed header line
                hidden = b"Content-Length: 1" if name == "hidden_length" else b"Transfer-Encoding: chunked"
                self.wfile.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n".encode()
                                 + b"no colon here\r\n" + hidden + b"\r\n\r\n" + body)
                self.close_connection = True
                return
            if name == "hidden_envelope":  # a non-field line the parser keeps as a mail envelope, not a defect
                self.wfile.write(f"HTTP/1.1 200 OK\r\nFrom x\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                self.close_connection = True
                return
            self.send_response({"redirect": 302, "error": 503}.get(name, 200))
            if name == "redirect":
                self.send_header("Location", self.path)
            lengths = {"short": [str(len(body) + 10)], "letters": ["12a"], "negative": ["-1"],
                       "superscript": ["\u00b2"], "repeated": [str(len(body))] * 2,
                       "conflicting": [str(len(body)), str(len(body) + 1)]}.get(name, [str(len(body))])
            for length in lengths:
                self.send_header("Content-Length", length)
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Monitor)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("DELEGATE_MONITOR_API", f"http://127.0.0.1:{server.server_port}/")
        monkeypatch.setattr(host_admission, "PLANNING_READ_TIMEOUT_S", 0.5)
        assert host_admission.read_planning() == json.loads(good)
        for name in ("redirect", "error", "short", "oversize", "not_json", "chunked", "close_delimited", "letters",
                     "negative", "superscript", "repeated", "conflicting", "hidden_length", "hidden_chunked",
                     "hidden_envelope", *stalls):
            mode["name"] = name
            started = time.monotonic()
            assert host_admission._monitor_json(host_admission.read_planning) is None, name
            assert time.monotonic() - started < 2, name
            if name in stalls:
                assert hung_up[name].wait(2), name
        mode["name"] = "ok"
        with monkeypatch.context() as patch:
            def refuse_start(_thread):
                raise RuntimeError("can't start new thread")

            patch.setattr(host_admission.threading.Thread, "start", refuse_start)
            assert host_admission._monitor_json(host_admission.read_planning) is None
        for base in ("", "https://127.0.0.1:1", "file:///dev/zero", "http://user@127.0.0.1:1",
                     "http://127.0.0.1:1/?q", "http://127.0.0.1:1/#f", "http://monitor.invalid:1", "http://127.0.0.1:x"):
            monkeypatch.setenv("DELEGATE_MONITOR_API", base)
            with pytest.raises(ValueError):
                host_admission._planning_origin()
        mode["name"] = "ok"
        monkeypatch.setenv("DELEGATE_MONITOR_API", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(pool_selector, "read_budget", lambda: {})
        monkeypatch.setattr(host_admission, "tasks_dir", lambda: tmp_path / "host-tasks")
        monkeypatch.setattr(adm, "load_thresholds", lambda: limits)
        spec = SimpleNamespace(mode="workspace-write", agent="claude", model="claude-opus-5-5",
                               claude_account="b", codex_account=None)
        result = host_admission.check(spec.mode, dry_run=True, disk_guard=False, spec=spec)
        assert result.admitted
        assert "meter claude:b: live write workers 0/1 (paced cap: 4% left, 50.0h to reset" in result.summary()
    finally:
        server.shutdown()
        server.server_close()


def test_active_drain_deadline_paces_to_the_earlier_reset_and_anything_else_keeps_todays_cap(
    tmp_path, probe, monkeypatch, caplog,
):
    """Defect classes (C-6214): an active operator drain deadline that pacing ignores, a deadline that
    lowers a cap or applies outside ``after <= now < deadline`` or to another meter, and an unset, empty,
    relative, missing, non-regular (directory, FIFO), oversized, undecodable, invalid, duplicate-key,
    too-deep or wrongly shaped file or entry, or a time without a UTC offset, that changes the cap, raises
    or blocks; the same bad file logged on every check."""
    now = datetime(2026, 11, 22, 9, 30, tzinfo=UTC)
    monkeypatch.setattr(adm, "_drain_warned", set())
    monkeypatch.delenv(adm.ENV_DRAIN_DEADLINES_FILE, raising=False)

    def view(pool="codex:a"):
        row = {"pool": pool, "remaining_pct": 41.0, "reset_at": (now + timedelta(hours=125)).isoformat(),
               "points_per_worker_hour": 0.583936, "worker_samples": 50}
        return {"schema_version": "model-planning.v1", "capacity": [row]}

    today = (1, "paced cap: 41% left, 125.0h to reset, 0.583936 points per worker-hour over 50 tasks, ceiling 8")
    drained = (3, "paced cap: 41% left, 28.5h to drain deadline, 0.583936 points per worker-hour over 50 tasks, "
                  "ceiling 8")
    deadline = now + timedelta(hours=28.5)

    def entry(after=now - timedelta(hours=1), until=deadline):
        return {"after": after.isoformat(), "deadline": until.isoformat(), "order": "C-6214 step 1"}

    target = tmp_path / "codex-drain-deadlines.json"

    def cap(meter="codex:a", contents=None):
        if contents is not None:
            target.write_bytes(contents if isinstance(contents, bytes) else json.dumps(contents).encode())
        return adm.meter_cap(meter, 8, view(meter), now, adm.active_drain_deadline(meter, now))

    assert cap() == today  # unset
    monkeypatch.setenv(adm.ENV_DRAIN_DEADLINES_FILE, "  ")
    assert cap() == today
    monkeypatch.setenv(adm.ENV_DRAIN_DEADLINES_FILE, str(target))
    # 41 points by the drain deadline 28.5h away at 0.583936 per worker-hour: 2.46 workers.
    assert cap(contents={"Codex a": entry(), "Codex b": entry(after=now + timedelta(days=2))}) == drained
    assert cap(contents={"Codex a": {**entry(), "deadline": "2026-11-23T14:00:00Z"}}) == drained  # the host spelling
    assert cap(contents={"Codex a": entry(after=now)}) == drained  # active from ``after`` itself
    assert cap("codex:b", {"Codex b": entry()}) == drained
    for inactive in (
        {"Codex a": entry(after=now + timedelta(seconds=1))},  # not yet
        {"Codex a": entry(after=now - timedelta(days=3), until=now)},  # the deadline itself has passed
        {"Codex a": entry(after=now - timedelta(days=3), until=now - timedelta(hours=1))},
        {"Codex a": entry(until=now + timedelta(hours=200))},  # after the meter reset: the reset still applies
        {"Codex a": entry(after=deadline, until=now)},  # an empty window
        {"Codex b": entry()},  # another meter's deadline
        {"codex a": entry(), "Codex  a": entry(), "codex:a": entry(), "Codex c": [1]},  # unknown names, ignored
        {},
    ):
        assert cap(contents=inactive) == today
    target.write_text(json.dumps({"Codex a": entry()}))
    assert adm.meter_cap("claude:b", 8, view("claude:b"), now, adm.active_drain_deadline("claude:b", now))[0] == 1
    assert not caplog.records

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    bad_files = (
        ("codex-drain-deadlines.json", "is not an absolute path"),
        (str(tmp_path / "absent.json"), "file missing"),
        (str(tmp_path), "is not a regular file"),
        (str(fifo), "is not a regular file"),
    )
    for path, reason in bad_files:
        monkeypatch.setenv(adm.ENV_DRAIN_DEADLINES_FILE, path)
        monkeypatch.setattr(adm, "_drain_warned", set())
        with caplog.at_level("WARNING", logger=adm.__name__):
            assert cap() == today
            assert cap() == today
        [warning] = caplog.records
        assert warning.getMessage() == (
            f"dispatch admission: {adm.ENV_DRAIN_DEADLINES_FILE} {reason}; pacing uses the meter reset")
        caplog.clear()
    monkeypatch.setenv(adm.ENV_DRAIN_DEADLINES_FILE, str(target))
    naive = {"Codex a": {**entry(), "after": "2026-11-20T13:00:00"}}
    for contents, reason in (
        (b" " * (64 * 1024 + 1), "file is larger than 65536 bytes"),
        (b'{"Codex a": "\xff"}', "is not valid JSON"),
        (b"\xef\xbb\xbf" + json.dumps({"Codex a": entry()}).encode(), "is not valid JSON"),
        (b'{"Codex a": ' + b"[" * 600 + b"]" * 600 + b"}", "is not valid JSON"),
        (b"{", "is not valid JSON"),
        (json.dumps({"Codex a": entry()}).encode()[:-1] + b', "Codex a": {}}', "is not valid JSON"),
        ([entry()], "is not a JSON object"),
        (None, "is not a JSON object"),
        (naive, "entry 'Codex a' needs 'after' and 'deadline' times with a UTC offset"),
        *(({"Codex a": value}, "entry 'Codex a' needs 'after' and 'deadline' times with a UTC offset") for value in (
            None, "2026-11-23T14:00:00Z", [], {"after": entry()["after"]},
            {**entry(), "deadline": 1795442400}, {**entry(), "deadline": "tomorrow"},
            {**entry(), "deadline": entry()["deadline"] + " " * 20},
        )),
    ):
        target.write_bytes(contents if isinstance(contents, bytes) else json.dumps(contents).encode())
        monkeypatch.setattr(adm, "_drain_warned", set())
        with caplog.at_level("WARNING", logger=adm.__name__):
            assert cap() == today
        assert [record.getMessage() for record in caplog.records] == [
            f"dispatch admission: {adm.ENV_DRAIN_DEADLINES_FILE} {reason}; pacing uses the meter reset"]
        caplog.clear()

    # A long-running session sees the deployment env file; evaluate paces the meter's live slots to the deadline.
    target.write_text(json.dumps({"Codex a": entry()}))
    env_file = tmp_path / "dispatch-admission.env"
    env_file.write_text(f"export {adm.ENV_DRAIN_DEADLINES_FILE}='{target}'\n")
    monkeypatch.delenv(adm.ENV_DRAIN_DEADLINES_FILE)
    monkeypatch.setenv(adm.ENV_ADMISSION_FILE, str(env_file))
    tasks = tmp_path / "tasks"
    for index in range(2):
        _record(tasks, f"codex-a-{index}", agent="codex", started_at=(now - timedelta(minutes=10)).isoformat())
    limits = replace(_LIMITS, max_live_write_workers=10, min_meter_launch_interval_s=0)

    def decide():
        return adm.evaluate("workspace-write", tasks, thresholds=limits, pid_alive=lambda _pid: True,
                            meter="codex:a", meter_forecast=view(), now=now)

    paced = decide()
    assert paced.admitted and paced.meter is not None and (paced.meter.live, paced.meter.cap) == (2, 3)
    assert "28.5h to drain deadline" in paced.summary()
    env_file.write_text("")
    refused = decide()
    assert not refused.admitted and refused.meter is not None and refused.meter.defer == adm.MeterDefer.CAP
    assert "meter codex:a: live write workers 2/1 (paced cap: 41% left, 125.0h to reset" in refused.refusal_line()


def test_grok_and_cursor_share_one_meter(tmp_path, probe):
    """Defect class: Grok and Cursor each spend a slot on a different meter."""
    tasks = tmp_path / "tasks"
    _record(tasks, "grok", agent="grok")
    _record(tasks, "cursor", agent="cursor")
    limits = replace(_LIMITS, max_live_write_workers=5, max_live_write_workers_per_meter=2)

    for agent in ("cursor", "grok"):
        decision = adm.evaluate("workspace-write", tasks, thresholds=limits,
                                pid_alive=lambda _pid: True, meter=adm.subscription_meter(agent))
        assert not decision.admitted
        assert "meter grok+cursor: live write workers 2/2" in decision.refusal_line()
        assert "subscription_meter_defer=cap" in decision.refusal_line()


def test_read_only_and_terminal_records_do_not_hold_write_slots(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "reader", pid=11, mode="read-only")
    _record(tasks, "finished", pid=12, status="done")
    _record(tasks, "writer", pid=13)

    decision = adm.evaluate("danger", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert decision.admitted
    assert decision.live_task_ids == ("writer",)


def test_dead_pid_does_not_count_and_is_handed_to_the_sweeper(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "alive", pid=11)
    _record(tasks, "dead-writer", pid=12)
    _record(tasks, "dead-reader", pid=13, mode="read-only", status="spawning")
    swept: list[str] = []

    decision = adm.evaluate(
        "workspace-write",
        tasks,
        pid_alive=lambda pid: pid == 11,
        on_dead=lambda _path, state: swept.append(state["task_id"]),
        thresholds=_LIMITS,
    )

    assert decision.admitted
    assert decision.live_task_ids == ("alive",)
    assert sorted(swept) == ["dead-reader", "dead-writer"]
    assert decision.swept
    assert "2 record(s) marked crashed" in decision.summary()


def test_report_only_evaluation_names_dead_records_without_sweeping(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "dead", pid=12)

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: False, thresholds=_LIMITS)

    assert not decision.swept
    assert decision.dead_task_ids == ("dead",)
    assert "1 record(s) dead pid, not counted: dead" in decision.summary()


def test_pidless_spawning_record_holds_a_slot_only_during_the_grace_window(tmp_path, probe):
    tasks = tmp_path / "tasks"
    stale = (datetime.now(UTC) - timedelta(seconds=adm.PIDLESS_SPAWNING_GRACE_S + 60)).isoformat()
    _record(tasks, "publishing", status="spawning", pid=None)
    _record(tasks, "orphaned", status="spawning", pid=None, started_at=stale)

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: False, thresholds=_LIMITS)

    assert decision.live_task_ids == ("publishing",)
    assert decision.dead_task_ids == ()


def test_pidless_spawning_record_from_the_future_does_not_hold_a_slot(tmp_path, probe, caplog):
    tasks = tmp_path / "tasks"
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    skewed = (datetime.now(UTC) + timedelta(seconds=adm.PIDLESS_CLOCK_SKEW_S / 2)).isoformat()
    _record(tasks, "future", status="spawning", pid=None, started_at=future)
    _record(tasks, "skewed", status="spawning", pid=None, started_at=skewed)

    with caplog.at_level("WARNING", logger=adm.__name__):
        decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: False, thresholds=_LIMITS)

    assert decision.live_task_ids == ("skewed",)
    assert "pidless spawning record future has started_at" in caplog.text
    assert "skewed" not in caplog.text


def test_low_mem_available_is_refused(tmp_path, probe):
    probe["probe"] = adm.HostProbe(mem_available_bytes=int(2.5 * _GIB), load1=1.0, cpu_count=8, proc_available=True)

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure == ("MemAvailable 2.5 GiB is below the floor of 3.5 GiB (DISPATCH_MIN_MEM_AVAILABLE_GIB=3.5)")


def test_high_load_per_cpu_is_refused(tmp_path, probe):
    probe["probe"] = adm.HostProbe(mem_available_bytes=12 * _GIB, load1=14.0, cpu_count=8, proc_available=True)

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure.startswith("load 1.75 per CPU (1-minute load 14.00 on 8 CPUs) is above the limit of 1.50")
    assert "DISPATCH_MAX_LOAD_PER_CPU=1.5" in failure


def test_single_cpu_load_failure_uses_singular(tmp_path, probe):
    probe["probe"] = adm.HostProbe(mem_available_bytes=12 * _GIB, load1=3.0, cpu_count=1, proc_available=True)

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    [failure] = decision.failures
    assert "(1-minute load 3.00 on 1 CPU) is above" in failure


def test_every_failed_check_is_named_in_one_refusal_line(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "a", pid=11)
    _record(tasks, "b", pid=12)
    probe["probe"] = adm.HostProbe(mem_available_bytes=1 * _GIB, load1=40.0, cpu_count=8, proc_available=True)

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    line = decision.refusal_line()
    assert "\n" not in line
    assert len(decision.failures) == 3
    for name in (adm.ENV_MAX_LIVE_WRITE_WORKERS, adm.ENV_MIN_MEM_AVAILABLE_GIB, adm.ENV_MAX_LOAD_PER_CPU):
        assert name in line


def test_read_only_mode_is_exempt_without_scanning(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "scan_task_records", lambda *_a, **_k: pytest.fail("read-only must not scan"))
    monkeypatch.setattr(adm, "probe_host", lambda *_a, **_k: pytest.fail("read-only must not probe"))

    decision = adm.evaluate("read-only", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.exempt and decision.admitted


def test_without_proc_memory_and_cpu_are_unknown_and_only_the_cap_applies(tmp_path, monkeypatch):
    missing_proc = tmp_path / "no-proc"
    no_proc_probe = adm.read_host(missing_proc)
    assert no_proc_probe == adm.HostProbe(
        mem_available_bytes=None, load1=None, cpu_count=os.cpu_count(), proc_available=False
    )
    monkeypatch.setattr(adm, "probe_host", lambda *_a, **_k: no_proc_probe)
    tasks = tmp_path / "tasks"
    _record(tasks, "a", pid=11)

    admitted = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)
    assert admitted.admitted
    summary = admitted.summary()
    assert "MemAvailable unknown" in summary
    assert "load unknown" in summary
    assert "only the worker cap is enforced" in summary

    _record(tasks, "b", pid=12)
    refused = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)
    assert not refused.admitted
    assert [f.split(" reached")[0] for f in refused.failures] == ["live write workers 2/2"]


def test_probe_host_reads_meminfo_and_loadavg(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal:       12582912 kB\nMemAvailable:    2621440 kB\n", encoding="ascii")
    (proc / "loadavg").write_text("3.25 2.00 1.00 2/900 12345\n", encoding="ascii")
    (proc / "stat").write_text("cpu  100 2 30 400 5 6 7 8 9 10\ncpu0 10 0 3 40 0 0 0 0 0 0\n", encoding="ascii")

    probe = adm.read_host(proc)

    assert probe.proc_available
    assert probe.mem_available_bytes == 2621440 * 1024
    assert probe.mem_available_gib == pytest.approx(2.5)
    assert probe.load1 == 3.25
    assert probe.cpu_steal_ticks == 8
    assert probe.cpu_total_ticks == 558
    snapshot = adm.AdmissionDecision(
        mode="workspace-write", exempt=False, admitted=True, thresholds=_LIMITS, probe=probe
    ).to_record()
    assert (snapshot["cpu_steal_ticks"], snapshot["cpu_total_ticks"]) == (8, 558)


@pytest.mark.parametrize("stat_text", ["", "cpu  1 2 3\n", "cpu  1 2 3 4 5 6 7 invalid\n", "cpu  1 2 3 4 5 6 7 -1\n"])
def test_probe_host_marks_invalid_cpu_steal_unknown(tmp_path, stat_text):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(stat_text, encoding="ascii")

    probe = adm.read_host(proc)

    assert probe.cpu_steal_ticks is None
    assert probe.cpu_total_ticks is None


def test_thresholds_default_to_config_and_honour_env_overrides():
    from lu_machinery import config

    defaults = adm.load_thresholds({})
    assert defaults == adm.Thresholds(
        max_live_write_workers=config.DISPATCH_MAX_LIVE_WRITE_WORKERS,
        min_mem_available_gib=config.DISPATCH_MIN_MEM_AVAILABLE_GIB,
        max_load_per_cpu=config.DISPATCH_MAX_LOAD_PER_CPU,
    )
    assert (
        defaults.max_live_write_workers,
        defaults.min_mem_available_gib,
        defaults.max_load_per_cpu,
        defaults.worker_mem_reserve_gib,
    ) == (2, 8.5, 1.0, 2.5)

    overridden = adm.load_thresholds(
        {
            adm.ENV_MAX_LIVE_WRITE_WORKERS: "0",
            adm.ENV_MIN_MEM_AVAILABLE_GIB: "6",
            adm.ENV_MAX_LOAD_PER_CPU: "0.75",
        }
    )
    assert overridden == adm.Thresholds(max_live_write_workers=0, min_mem_available_gib=6.0, max_load_per_cpu=0.75)


def test_defaults_allow_cap_slots_and_refuse_below_the_floor(tmp_path, probe):
    tasks = tmp_path / "tasks"
    defaults = adm.load_thresholds({})
    _record(tasks, "writer-00", pid=1000)

    before_cap = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=defaults)
    assert before_cap.admitted
    assert "live write workers 1/2" in before_cap.summary()

    _record(tasks, "writer-01", pid=1001)
    at_cap = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=defaults)
    assert not at_cap.admitted
    assert "live write workers 2/2 reached the cap" in at_cap.failures[0]

    probe["probe"] = adm.HostProbe(mem_available_bytes=int(8.4 * _GIB), load1=0.0, cpu_count=8, proc_available=True)
    below_floor = adm.evaluate("workspace-write", tmp_path / "empty", thresholds=defaults)
    assert not below_floor.admitted
    assert "below the floor of 8.5 GiB" in below_floor.failures[0]


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        (adm.ENV_MAX_LIVE_WRITE_WORKERS, "five"),
        (adm.ENV_MAX_LIVE_WRITE_WORKERS, "2.5"),
        (adm.ENV_MIN_MEM_AVAILABLE_GIB, "-1"),
        (adm.ENV_MAX_LOAD_PER_CPU, "nan"),
    ],
)
def test_invalid_threshold_env_is_rejected(name, raw):
    with pytest.raises(ValueError, match=name):
        adm.load_thresholds({name: raw})


def test_admission_lock_is_exclusive_across_holders(tmp_path):
    tasks = tmp_path / "tasks"
    with adm.admission_lock(tasks), pytest.raises(adm.AdmissionLockTimeout), adm.admission_lock(tasks, timeout_s=0.1):
        pass
    # Released on exit: the next holder gets it at once.
    with adm.admission_lock(tasks, timeout_s=0.1):
        pass
    assert (tasks / adm.LOCK_FILE_NAME).is_file()


# --- #8717: pid-less records owned by a live dispatcher --------------------------------------


def _own_identity() -> dict:
    return worktree_prep.process_identity(os.getpid())


def _pidless_owned_record(tasks_dir: Path, task_id: str, *, block: str, owner: dict, age_s: float) -> Path:
    """A pid-less ``spawning`` write record whose ``block`` (worktree_prep or admission_hold) names ``owner``."""
    return _record(
        tasks_dir,
        task_id,
        status="spawning",
        pid=None,
        started_at=(datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(),
        **{block: {"run_nonce": f"nonce-{task_id}", "owner_pid": owner["pid"], "owner_start": owner["start"]}},
    )


@pytest.mark.parametrize("block", ["worktree_prep", adm.ADMISSION_HOLD_KEY])
def test_pidless_record_of_a_live_dispatcher_holds_a_slot_past_the_grace_window(tmp_path, probe, block):
    """A slow ``git worktree add`` (up to 900 s) keeps its admitted slot while the dispatcher lives."""
    tasks = tmp_path / "tasks"
    _pidless_owned_record(tasks, "slow-prep", block=block, owner=_own_identity(), age_s=300)
    assert adm.PIDLESS_SPAWNING_GRACE_S < 300

    decision = adm.evaluate("workspace-write", tasks, thresholds=_LIMITS)

    assert decision.live_task_ids == ("slow-prep",)
    assert decision.dead_task_ids == ()


@pytest.mark.parametrize("block", ["worktree_prep", adm.ADMISSION_HOLD_KEY])
def test_pidless_record_of_a_dead_dispatcher_is_not_counted_and_is_handed_to_the_sweeper(tmp_path, probe, block):
    from tests.worktree_prep_helpers import exited_process_identity

    tasks = tmp_path / "tasks"
    pid, start = exited_process_identity()
    path = _pidless_owned_record(tasks, "orphan", block=block, owner={"pid": pid, "start": start}, age_s=10)
    swept: list[Path] = []

    decision = adm.evaluate("workspace-write", tasks, on_dead=lambda p, _state: swept.append(p), thresholds=_LIMITS)

    assert decision.live_task_ids == ()
    assert decision.dead_task_ids == ("orphan",)
    assert swept == [path]


def test_pidless_record_of_a_gone_dispatcher_without_start_time_is_not_counted_or_swept(tmp_path, probe):
    """Without a recorded start time a missing pid frees the slot, but is no proof for marking it crashed."""
    from tests.worktree_prep_helpers import exited_process_identity

    tasks = tmp_path / "tasks"
    pid, _start = exited_process_identity()
    _pidless_owned_record(tasks, "no-start", block="worktree_prep", owner={"pid": pid, "start": None}, age_s=10)

    decision = adm.evaluate("workspace-write", tasks, on_dead=lambda *_a: pytest.fail("no proof"), thresholds=_LIMITS)

    assert decision.live_task_ids == ()
    assert decision.dead_task_ids == ()


@pytest.fixture(autouse=True)
def _omit_live_slice(monkeypatch):
    """Admission fixtures do not read the host's lu-dispatch.slice."""
    monkeypatch.setattr(adm, "slice_usage_clause", lambda: None)


def test_summary_adds_active_slice_use_without_changing_the_decision(tmp_path, probe, monkeypatch):
    tasks = tmp_path / "tasks"
    _record(tasks, "writer", pid=11)
    monkeypatch.setattr(adm, "slice_usage_clause", lambda: "lu-dispatch.slice 0.4/11.0 GiB")

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert decision.admitted
    assert decision.failures == ()
    assert "lu-dispatch.slice 0.4/11.0 GiB" in decision.summary()


def test_summary_omits_the_slice_when_it_is_not_reported(tmp_path, probe):
    tasks = tmp_path / "tasks"
    _record(tasks, "writer", pid=11)

    summary = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS).summary()

    assert "lu-dispatch.slice" not in summary


# --- Shared lu.slice pool headroom (#9975) --------------------------------------------------


def _fake_pool(directory: Path, *, current: int, file_cache: int, high: int = 21 * _GIB) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "memory.current").write_text(f"{current}\n", encoding="ascii")
    (directory / "memory.high").write_text(f"{high}\n", encoding="ascii")
    (directory / "memory.max").write_text(f"{23 * _GIB}\n", encoding="ascii")
    (directory / "memory.stat").write_text(f"active_file {file_cache}\ninactive_file 0\n", encoding="ascii")
    return directory


def test_full_shared_pool_refuses_a_write_worker(tmp_path, probe, monkeypatch):
    pool = _fake_pool(tmp_path / "lu.slice", current=21 * _GIB, file_cache=_GIB // 2)
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))
    tasks = tmp_path / "tasks"

    decision = adm.evaluate("workspace-write", tasks, pid_alive=lambda _pid: True, thresholds=_LIMITS)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure == (
        "lu.slice non-cache use 20.5 GiB plus a 2.5 GiB worker reserve exceeds MemoryHigh 21.0 GiB "
        "(DISPATCH_WORKER_MEM_RESERVE_GIB=2.5)"
    )
    record = decision.to_record()
    assert record["pool_nonreclaimable_gib"] == 20.5
    assert record["pool_file_cache_gib"] == 0.5
    assert record["pool_limit_gib"] == 21.0
    assert record["worker_mem_reserve_gib"] == 2.5
    assert record["pool_check_skipped"] is None


def test_file_cache_does_not_count_against_the_pool(tmp_path, probe, monkeypatch):
    pool = _fake_pool(tmp_path / "lu.slice", current=28 * _GIB, file_cache=14 * _GIB)
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert "lu.slice 14.0/21.0 GiB non-cache (+14.0 GiB file cache; worker reserve 2.5 GiB)" in decision.summary()


def test_worker_reserve_env_override_changes_the_pool_decision(tmp_path, probe, monkeypatch):
    pool = _fake_pool(tmp_path / "lu.slice", current=17 * _GIB, file_cache=0)
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))
    tasks = tmp_path / "tasks"

    roomy = adm.evaluate("workspace-write", tasks, thresholds=_LIMITS)
    strict = adm.evaluate(
        "workspace-write", tasks, thresholds=adm.load_thresholds({adm.ENV_WORKER_MEM_RESERVE_GIB: "5.5"})
    )

    assert roomy.admitted
    assert not strict.admitted
    assert any("DISPATCH_WORKER_MEM_RESERVE_GIB=5.5" in failure for failure in strict.failures)


def test_missing_pool_cgroup_skips_the_check_and_logs_why(tmp_path, probe, monkeypatch, caplog):
    monkeypatch.setenv("LU_SLICE_CGROUP", str(tmp_path / "absent" / "lu.slice"))

    with caplog.at_level("WARNING", logger=adm.__name__):
        decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert "lu.slice pool check skipped (lu.slice memory.current unavailable: " in decision.summary()
    assert str(tmp_path) not in decision.summary()
    assert "memory.current unavailable" in decision.to_record()["pool_check_skipped"]
    [warning] = [r for r in caplog.records if "shared pool check skipped" in r.getMessage()]
    assert warning.levelname == "WARNING"


def test_malformed_memory_high_skips_with_reason_in_the_admitted_line(tmp_path, probe, monkeypatch, caplog):
    pool = _fake_pool(tmp_path / "lu.slice", current=25 * _GIB, file_cache=0)
    (pool / "memory.high").write_text("28G\n", encoding="ascii")
    monkeypatch.setenv("LU_SLICE_CGROUP", str(pool))

    with caplog.at_level("WARNING", logger=adm.__name__):
        decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert "lu.slice pool check skipped (lu.slice memory.high is neither a number nor 'max')" in decision.summary()
    assert "memory.high is neither a number nor 'max'" in caplog.text


def test_healthy_test_host_never_reads_the_real_pool(tmp_path, probe, monkeypatch):
    monkeypatch.delenv("LU_SLICE_CGROUP", raising=False)
    monkeypatch.setattr(adm.pool_headroom, "check_pool", lambda *_a, **_k: pytest.fail("real pool read"))

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert decision.pool is not None
    assert decision.pool.skipped == "test host"
    assert "lu.slice pool check skipped (test host)" in decision.summary()


def test_read_only_dispatch_does_not_probe_the_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "probe_pool", lambda *_a, **_k: pytest.fail("read-only must not probe the pool"))

    assert adm.evaluate("read-only", tmp_path / "tasks").admitted


def test_deployment_env_file_fills_thresholds_the_environment_does_not_set(tmp_path):
    env_file = tmp_path / "admission.env"
    env_file.write_text(
        "# deployment overrides\n"
        f"export {adm.ENV_MAX_LIVE_WRITE_WORKERS}=7\n"
        f"{adm.ENV_MIN_MEM_AVAILABLE_GIB}='5.5'\n"
        "UNRELATED=1\n"
        "not a line\n",
        encoding="utf-8",
    )
    limits = adm.load_thresholds(
        {adm.ENV_ADMISSION_FILE: str(env_file), adm.ENV_MIN_MEM_AVAILABLE_GIB: "9.5"}
    )
    assert limits.max_live_write_workers == 7  # from the file
    assert limits.min_mem_available_gib == 9.5  # the environment wins over the file
    assert limits.max_load_per_cpu == adm.config_defaults().max_load_per_cpu


def test_env_file_is_optional_and_can_be_disabled(tmp_path, monkeypatch):
    assert adm.load_thresholds({adm.ENV_ADMISSION_FILE: str(tmp_path / "missing.env")}) == adm.config_defaults()
    assert adm.admission_env_file({adm.ENV_ADMISSION_FILE: ""}, live=True) is None
    # An explicit mapping without the variable reads no file; the live default is XDG-based.
    assert adm.admission_env_file({}, live=False) is None
    assert adm.admission_env_file({"XDG_CONFIG_HOME": str(tmp_path)}, live=True) == (
        tmp_path / "learn-ukrainian" / adm.ADMISSION_ENV_FILE_NAME
    )


def test_live_thresholds_read_the_default_env_file(tmp_path, monkeypatch):
    target = tmp_path / "learn-ukrainian" / adm.ADMISSION_ENV_FILE_NAME
    target.parent.mkdir()
    target.write_text(f"{adm.ENV_MAX_LOAD_PER_CPU}=0.5\n", encoding="utf-8")
    monkeypatch.delenv(adm.ENV_ADMISSION_FILE, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert adm.load_thresholds().max_load_per_cpu == 0.5


def test_invalid_env_file_value_is_refused(tmp_path):
    env_file = tmp_path / "admission.env"
    env_file.write_text(f"{adm.ENV_MAX_LIVE_WRITE_WORKERS}=many\n", encoding="utf-8")
    with pytest.raises(ValueError, match="is not a whole number"):
        adm.load_thresholds({adm.ENV_ADMISSION_FILE: str(env_file)})


# --- Host test-slot queue (#10662) -----------------------------------------------------------


@pytest.fixture
def slot_pool(tmp_path, monkeypatch):
    """A private one-slot pool; an outer governed run's holder marker must not leak in."""
    from lu_machinery.common import test_slots

    monkeypatch.setenv(test_slots.ENABLE_ENV, "on")
    monkeypatch.setenv(test_slots.DIR_ENV, str(tmp_path / "test-slots"))
    monkeypatch.setenv(test_slots.COUNT_ENV, "1")
    monkeypatch.delenv(test_slots.HOLDER_ENV, raising=False)
    return test_slots


def test_a_long_test_slot_queue_refuses_a_write_worker(tmp_path, probe, monkeypatch):
    monkeypatch.setattr(adm, "probe_test_slots", lambda: adm.SlotQueueProbe(depth=5, pool_size=14))
    limits = adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "4"})

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=limits)

    assert not decision.admitted
    [failure] = decision.failures
    assert failure == "test-slot queue 5 is longer than 4 waiting runs (DISPATCH_MAX_TEST_SLOT_QUEUE=4)"
    record = decision.to_record()
    assert (record["test_slot_queue_depth"], record["max_test_slot_queue"]) == (5, 4)
    assert record["test_slot_queue_skipped"] is None


def test_a_queue_at_the_limit_is_admitted_and_reported(tmp_path, probe, monkeypatch):
    monkeypatch.setattr(adm, "probe_test_slots", lambda: adm.SlotQueueProbe(depth=4, pool_size=14))

    decision = adm.evaluate(
        "workspace-write", tmp_path / "tasks", thresholds=adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "4"})
    )

    assert decision.admitted
    assert "test-slot queue 4 (limit 4, pool 14)" in decision.summary()


def test_the_real_test_slot_queue_is_counted(tmp_path, probe, slot_pool):
    import subprocess
    import sys

    from tests.wait_helpers import wait_for_line

    repo = Path(__file__).resolve().parents[2]
    log = tmp_path / "waiter.log"
    with slot_pool.acquire(1, kind="pytest"):
        env = {k: v for k, v in os.environ.items() if k != slot_pool.HOLDER_ENV}
        env["PYTHONPATH"] = str(repo)
        with log.open("w", encoding="utf-8") as stderr:
            waiter = subprocess.Popen(
                [sys.executable, "-m", "lu_machinery.common.test_slots", "run", "--", sys.executable, "-c", "pass"],
                cwd=repo,
                env=env,
                stderr=stderr,
            )
        try:
            wait_for_line(log, stop_if=lambda: waiter.poll() is not None)
            strict = adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "0"})
            decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=strict)
        finally:
            waiter.terminate()
            waiter.wait(timeout=30)

    assert not decision.admitted
    assert decision.failures == (
        "test-slot queue 1 is longer than 0 waiting runs (DISPATCH_MAX_TEST_SLOT_QUEUE=0)",
    )


@pytest.mark.parametrize(("setting", "clause"), [("off", "pool disabled"), ("maybe", "invalid_configuration")])
def test_a_disabled_or_unreadable_pool_skips_the_queue_check_and_says_why(tmp_path, probe, slot_pool, monkeypatch, setting, clause):
    monkeypatch.setenv(slot_pool.ENABLE_ENV, setting)

    decision = adm.evaluate(
        "workspace-write", tmp_path / "tasks", thresholds=adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "0"})
    )

    assert decision.admitted
    assert f"test-slot queue check skipped ({clause}" in decision.summary()
    assert clause in decision.to_record()["test_slot_queue_skipped"]


def test_test_slot_queue_threshold_defaults_to_config_and_rejects_invalid_values():
    from lu_machinery import config

    assert adm.load_thresholds({}).max_test_slot_queue == config.DISPATCH_MAX_TEST_SLOT_QUEUE == 4
    assert adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "2"}).max_test_slot_queue == 2
    with pytest.raises(ValueError, match=adm.ENV_MAX_TEST_SLOT_QUEUE):
        adm.load_thresholds({adm.ENV_MAX_TEST_SLOT_QUEUE: "many"})


def test_read_only_dispatch_does_not_read_the_test_slot_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(adm, "probe_test_slots", lambda: pytest.fail("read-only must not read the slot pool"))

    assert adm.evaluate("read-only", tmp_path / "tasks").admitted


def test_healthy_test_host_never_reads_the_real_test_slot_pool(tmp_path, probe, monkeypatch):
    monkeypatch.delenv(adm.test_slots.DIR_ENV, raising=False)
    monkeypatch.setattr(adm.test_slots, "status", lambda *_a, **_k: pytest.fail("real slot pool read"))

    decision = adm.evaluate("workspace-write", tmp_path / "tasks", thresholds=_LIMITS)

    assert decision.admitted
    assert decision.slots is not None and decision.slots.skipped == "test host"
    assert "test-slot queue check skipped (test host)" in decision.summary()
