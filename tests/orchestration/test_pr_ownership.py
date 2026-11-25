"""Ownerless-PR alert, accepted handoffs and queue custody (scripts/orchestration/pr_ownership.py)."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from lu_machinery.fleet_comms.authority import AuthorityService
from lu_machinery.orchestration import merge_queue_keeper as keeper
from lu_machinery.orchestration import pr_ownership as own
from lu_machinery.orchestration.fleet_repos import FleetRepo
from lu_machinery.publish import recovery

REPO = "learn-ukrainian/learn-ukrainian.github.io"
KEY = own.pr_key(REPO, 42)
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
HEAD = "a" * 40


def ownerless(number=42, unknown=False):
    return own.Finding(REPO, number, "codex/x", "last owner ended timeout, PR not MERGED", "old", unknown=unknown)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def task(task_id, branch="codex/x", status="running"):
    return {"task_id": task_id, "worktree_branch": branch, "status": status, "repository": REPO}


def is_live(record):
    return record is not None and record.get("status") in {"running", "spawning"}


def accepted(data, minutes, successor="succ", **extra):
    data["handoffs"][KEY] = {"successor": successor, "predecessor": None, "accepted_at": at(minutes).isoformat(),
                             "head": HEAD, "custody": [], "rerun_allowance": "unspent", **extra}


# --- alert episodes ---------------------------------------------------------------


def test_alert_after_fifteen_minutes_once_then_hourly_reminder():
    data = own._empty()
    assert own.step(data, {KEY}, {KEY: ownerless()}, at(0))["alert"] == []
    assert own.step(data, {KEY}, {KEY: ownerless()}, at(14))["alert"] == []
    notices = own.step(data, {KEY}, {KEY: ownerless()}, at(15))
    assert [row["pr"] for row in notices["alert"]] == [KEY]
    assert own.step(data, {KEY}, {KEY: ownerless()}, at(30)) == {"alert": [], "reminder": [], "cleared": []}
    assert [row["pr"] for row in own.step(data, {KEY}, {KEY: ownerless()}, at(75))["reminder"]] == [KEY]


def test_owner_returning_inside_grace_cancels_silently():
    data = own._empty()
    own.step(data, {KEY}, {KEY: ownerless()}, at(0))
    assert own.step(data, {KEY}, {}, at(10)) == {"alert": [], "reminder": [], "cleared": []}
    assert data["episodes"] == {}


def test_unknown_telemetry_never_starts_advances_or_clears():
    data = own._empty()
    own.step(data, {KEY}, {KEY: ownerless(unknown=True)}, at(0))
    assert data["episodes"] == {}
    own.step(data, {KEY}, {KEY: ownerless()}, at(0))
    own.step(data, {KEY}, {KEY: ownerless()}, at(15))
    before = json.dumps(data["episodes"], sort_keys=True)
    accepted(data, 20)
    assert own.step(data, {KEY}, {KEY: ownerless(unknown=True)}, at(90)) == {"alert": [], "reminder": [], "cleared": []}
    assert json.dumps(data["episodes"], sort_keys=True) == before


def test_alert_stays_outstanding_until_a_successor_accepts():
    data = own._empty()
    own.step(data, {KEY}, {KEY: ownerless()}, at(0))
    own.step(data, {KEY}, {KEY: ownerless()}, at(15))
    # A task now owns the branch, but nobody recorded acceptance: still outstanding.
    assert own.step(data, {KEY}, {}, at(20))["cleared"] == []
    assert data["episodes"][KEY]["alerted_at"]
    # A handoff accepted before this episode began does not count.
    accepted(data, -5)
    assert own.step(data, {KEY}, {}, at(21))["cleared"] == []
    accepted(data, 22)
    notices = own.step(data, {KEY}, {}, at(23))
    assert notices["cleared"][0]["how"] == "handoff accepted by succ"
    assert data["episodes"] == {} and data["history"][-1]["event"] == "cleared"


def test_closed_pr_clears_alert_and_drops_handoff():
    data = own._empty()
    own.step(data, {KEY}, {KEY: ownerless()}, at(0))
    own.step(data, {KEY}, {KEY: ownerless()}, at(15))
    accepted(data, 16)
    notices = own.step(data, set(), {}, at(20))
    assert notices["cleared"][0]["how"] == "PR no longer open"
    assert data["episodes"] == {} and data["handoffs"] == {}


# --- the owner check with handoffs and unknown liveness ----------------------------

MACHINERY = "learn-ukrainian/machinery"
FLEET = {
    "public": FleetRepo("public", REPO, "learn-ukrainian", "public-monorepo", True),
    "machinery": FleetRepo("machinery", MACHINERY, "machinery", "public-machinery"),
}


def find(prs, records, handoffs=None, live=is_live):
    return own.findings({REPO: prs}, records, FLEET, handoffs or {}, live)


def test_accepted_successor_owns_from_another_branch_while_live():
    prs = [{"number": 42, "headRefName": "codex/x", "isCrossRepository": False}]
    successor = {**task("succ", "claude/queue"), "repository": MACHINERY, "repository_id": MACHINERY, "repo": "machinery"}
    records = [task("old", status="timeout"), successor]
    (row,) = find(prs, records)
    assert not row.unknown and row.last_task == "old"
    assert find(prs, records, {(REPO.casefold(), 42): "succ"}) == []
    finished = [task("old", status="timeout"), task("succ", "claude/queue", "done")]
    (row,) = find(prs, finished, {(REPO.casefold(), 42): "succ"})
    assert not row.unknown
    fork = [{"number": 42, "headRefName": "codex/x", "isCrossRepository": True}]
    assert find(fork, records, {(REPO.casefold(), 42): "succ"}) == []


def test_running_task_on_another_branch_is_not_ownerless_when_the_head_names_it():
    """Defect class: a live owner reported as ownerless.

    A running task whose PR is on a different branch, with that task's trailer on the PR head,
    is not reported ownerless.
    """
    prs = [{
        "number": 42,
        "headRefName": "codex/x-rebuild",
        "isCrossRepository": False,
        "headRefOid": HEAD,
        "commits": [{
            "oid": HEAD,
            "messageHeadline": "rebuild the branch",
            "messageBody": "X-Agent: codex/worker-1",
        }],
    }]
    worker = task("worker-1", "codex/worker-1")
    assert find(prs, [worker]) == []
    (row,) = find(prs, [{**worker, "repo": "machinery"}])
    assert row.unknown and row.reason == "task_repository_conflict"


def fake_proc(monkeypatch, files):
    """Serve /proc reads from ``files``: a string is content, an exception is raised."""
    real = own.Path.read_text

    def read(self, *args, **kwargs):
        if str(self).startswith("/proc/"):
            value = files.get(str(self), FileNotFoundError(str(self)))
            if isinstance(value, Exception):
                raise value
            return value
        return real(self, *args, **kwargs)

    monkeypatch.setattr(own.Path, "read_text", read)


STAT = "7 (python) S" + " 0" * 18 + " 555 0"  # field 22 of proc(5), start ticks, is 555


def test_unobservable_owner_is_unknown_and_gone_owner_is_ownerless(monkeypatch):
    prs = [{"number": 42, "headRefName": "codex/x", "isCrossRepository": False}]
    owner = {**task("t"), "pid": 7, "process_start": "boot:555"}
    fake_proc(monkeypatch, {})  # the recorded pid no longer exists
    for extra in ({}, {"pid": 7}, {"pid": 7, "process_start": None}):
        (row,) = find(prs, [{**task("t"), **extra}], live=own.liveness)
        assert row.unknown and "cannot be observed" in row.reason
    (row,) = find(prs, [{**task("t"), "repository": None}], live=own.liveness)
    assert row.unknown and row.reason == "task_repository_missing"
    (row,) = find(prs, [owner], live=own.liveness)
    assert not row.unknown and "not verifiably alive" in row.reason
    boot = {"/proc/sys/kernel/random/boot_id": "boot\n"}
    fake_proc(monkeypatch, {"/proc/7/stat": STAT, **boot})
    assert find(prs, [owner], live=own.liveness) == []
    fake_proc(monkeypatch, {"/proc/7/stat": STAT.replace(" 555 ", " 556 "), **boot})  # pid reused
    assert own.liveness(owner) is False
    (row,) = find(prs, [task("old", status="done"), task("succ", "claude/q")], {(REPO.casefold(), 42): "succ"},
                  live=lambda r: None if r and r["task_id"] == "succ" else False)
    assert row.unknown


@pytest.mark.parametrize("files", [
    {"/proc/7/stat": PermissionError("stat")},
    {"/proc/7/stat": STAT, "/proc/sys/kernel/random/boot_id": PermissionError("boot")},
    {"/proc/7/stat": "garbage"},
])
def test_any_unreadable_identity_source_is_unknown(monkeypatch, files):
    fake_proc(monkeypatch, files)
    assert own.liveness({**task("t"), "pid": 7, "process_start": "boot:555"}) is None

def fleet(tmp_path):
    with AuthorityService(root=tmp_path / "fleet") as service:
        service.create_channel(own.CTO_CHANNEL)  # exists in the live authority store
    return lambda: AuthorityService(root=tmp_path / "fleet")


def inbox(tmp_path):
    with AuthorityService(root=tmp_path / "fleet") as service:
        rows = service._conn.execute("SELECT message_id, recipient, kind FROM comms_messages").fetchall()
        return [(row[1], row[2], service.read_message_body(row[0])) for row in rows]


def test_ownerless_open_pr_past_grace_records_a_decision_owner(tmp_path):
    """Defect class: ownerless PR stayed an alert the CTO had to accept by hand.

    Past the grace, a known ownerless open PR with a readable head gets a recorded
    decision owner, an attempt row, and a checked postcondition. An unknown owner
    on another PR is not seated.
    """
    path = tmp_path / "state.json"
    other = own.pr_key(REPO, 43)
    evidenced = own.Finding(
        REPO, 42, "codex/x", "last owner ended timeout, PR not MERGED", "old",
        head=HEAD, pr_state="OPEN",
    )
    unknown = own.Finding(
        REPO, 43, "codex/y", "an owner is active but its supervisor cannot be observed (unknown)", "old",
        unknown=True, head=HEAD, pr_state="OPEN",
    )
    observe = lambda handoffs: ({KEY, other}, {KEY: evidenced, other: unknown})  # noqa: E731
    tick = dict(path=path, observe=observe, service_factory=fleet(tmp_path))
    own.keeper_tick(now=at(0), **tick)
    lines, failed = own.keeper_tick(now=at(15), **tick)
    assert failed is False
    state = own.load(path)
    handoff = state["handoffs"][KEY]
    assert own.decision_custody(handoff)
    assert handoff["decision_owner"] == own.CTO_INBOX and handoff["head"] == HEAD
    assert set(handoff) <= own._OWNERSHIP_FIELDS and handoff["custody"] == []
    view = own.custody_view(state)
    assert view["evidence"] == "known" and "why" not in view
    assert view["recorded_custody"] == [{
        "pr": KEY, "decision_owner": own.CTO_INBOX, "head": HEAD, "by": "keeper",
    }]
    assert view["waiting_for_grace"] == [] and view["unknown_evidence"] == []
    dirty = {**state, "handoffs": {KEY: {**handoff, "merge": True}}}
    assert own.custody_view(dirty)["recorded_custody"] == []
    assert own.custody_view(dirty)["unknown_evidence"][0]["why"] == "handoff has fields outside ownership"
    refused = own._empty()
    refused["last_tick"] = {"at": at(15).isoformat(), "ok": True}
    refused["episodes"][KEY] = {
        "since": at(0).isoformat(), "alerted_at": at(15).isoformat(), "notified_at": at(15).isoformat(),
        "reminders": 0, "branch": "codex/x", "reason": "r", "last_task": "old",
    }
    refused["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "refused", "postcondition": "false", "at": at(15).isoformat(),
    }
    assert own.custody_view(refused)["alert_outstanding"] == [{
        "pr": KEY, "since": at(0).isoformat(), "keeper": "refused",
    }]
    refused["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "pending", "postcondition": "unknown", "at": at(15).isoformat(),
    }
    unverified = own.custody_view(refused)
    assert unverified["unknown_evidence"][0]["why"] == "keeper attempt unverified"
    assert unverified["recorded_custody"] == [] and unverified["waiting_for_grace"] == []
    # A failed observation does not hide a decision owner, and does not leave an alert looking current.
    stale = own._empty()
    stale["last_tick"] = {"at": at(20).isoformat(), "ok": False, "error": "RuntimeError"}
    stale["handoffs"][KEY] = dict(handoff)
    stale["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "recorded", "postcondition": "true", "at": handoff["accepted_at"],
    }
    stale["episodes"][own.pr_key(REPO, 43)] = {
        "since": at(0).isoformat(), "alerted_at": at(15).isoformat(), "notified_at": at(15).isoformat(),
        "reminders": 0, "branch": "codex/y", "reason": "r", "last_task": "old",
    }
    stale_view = own.custody_view(stale)
    assert stale_view["evidence"] == "unknown" and stale_view["why"] == "RuntimeError"
    assert stale_view["recorded_custody"] == [{
        "pr": KEY, "decision_owner": own.CTO_INBOX, "head": HEAD, "by": "keeper",
    }]
    assert stale_view["alert_outstanding"] == [] and stale_view["waiting_for_grace"] == []
    assert stale_view["unknown_evidence"] == [{"pr": other, "why": "last keeper tick is not evidence", "alerted": True}]
    # An earlier keeper attempt does not name a later decision owner, hide a new grace
    # period, or label a later alert. A closed PR's leftover attempt is not a row.
    replaced = own._empty()
    replaced["last_tick"] = {"at": at(40).isoformat(), "ok": True}
    replaced["handoffs"][KEY] = {
        **handoff, "decision_owner": "other-coordinator", "successor": "other-coordinator",
        "accepted_at": at(40).isoformat(),
    }
    replaced["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "recorded", "postcondition": "true", "at": handoff["accepted_at"],
    }
    assert own.custody_view(replaced)["recorded_custody"] == [{
        "pr": KEY, "decision_owner": "other-coordinator", "head": HEAD, "by": "coordinator",
    }]
    replaced["heal_attempts"][KEY] = {"result": "pending", "postcondition": "unknown"}
    replaced_view = own.custody_view(replaced)
    assert replaced_view["recorded_custody"][0]["by"] == "coordinator"
    assert replaced_view["unknown_evidence"] == [] and replaced_view["waiting_for_grace"] == []
    fresh = own._empty()
    fresh["last_tick"] = {"at": at(25).isoformat(), "ok": True}
    fresh["episodes"][KEY] = {
        "since": at(20).isoformat(), "alerted_at": None, "notified_at": None,
        "reminders": 0, "branch": "codex/x", "reason": "r", "last_task": "old",
    }
    fresh["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "pending", "postcondition": "unknown", "at": at(0).isoformat(),
    }
    fresh_view = own.custody_view(fresh)
    assert [row["pr"] for row in fresh_view["waiting_for_grace"]] == [KEY]
    assert fresh_view["unknown_evidence"] == [] and fresh_view["alert_outstanding"] == []
    # A missing or unreadable clock does not prove the attempt is older than this episode.
    fresh["heal_attempts"][f"{KEY}:{HEAD}"] = {"result": "pending", "postcondition": "unknown"}
    clockless = own.custody_view(fresh)
    assert clockless["waiting_for_grace"] == [] and clockless["alert_outstanding"] == []
    assert clockless["unknown_evidence"] == [{
        "pr": KEY, "why": "keeper attempt unverified", "alerted": False,
    }]
    fresh["heal_attempts"][f"{KEY}:{HEAD}"]["at"] = "not-a-time"
    assert own.custody_view(fresh)["unknown_evidence"][0]["why"] == "keeper attempt unverified"
    fresh["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "pending", "postcondition": "unknown", "at": at(20).isoformat(),
    }
    assert own.custody_view(fresh)["unknown_evidence"][0]["why"] == "keeper attempt unverified"
    fresh["episodes"][KEY]["alerted_at"] = at(25).isoformat()
    fresh["heal_attempts"][f"{KEY}:{HEAD}"] = {"result": "refused", "postcondition": "false"}
    alerted_clockless = own.custody_view(fresh)
    assert alerted_clockless["alert_outstanding"] == []
    assert alerted_clockless["unknown_evidence"][0]["alerted"] is True
    fresh["heal_attempts"][f"{KEY}:{HEAD}"] = "nope"
    assert own.custody_view(fresh)["unknown_evidence"][0]["why"] == "keeper attempt unverified"
    later = own._empty()
    later["last_tick"] = {"at": at(25).isoformat(), "ok": True}
    later["episodes"][KEY] = {
        "since": at(20).isoformat(), "alerted_at": at(25).isoformat(), "notified_at": at(25).isoformat(),
        "reminders": 0, "branch": "codex/x", "reason": "r", "last_task": "old",
    }
    later["heal_attempts"][f"{KEY}:{'c' * 40}"] = {
        "result": "pending", "postcondition": "unknown", "at": at(0).isoformat(),
    }
    later["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "refused", "postcondition": "false", "at": at(25).isoformat(),
    }
    neighbour = own.pr_key(REPO, 420)
    later["heal_attempts"][f"{neighbour}:{HEAD}"] = {
        "result": "pending", "postcondition": "unknown", "at": at(25).isoformat(),
    }
    assert own.custody_view(later)["alert_outstanding"] == [{
        "pr": KEY, "since": at(20).isoformat(), "keeper": "refused",
    }]
    assert own.custody_view(later)["unknown_evidence"] == []
    closed = own._empty()
    closed["last_tick"] = {"at": at(30).isoformat(), "ok": True}
    closed["heal_attempts"][f"{KEY}:{HEAD}"] = {
        "result": "recorded", "postcondition": "true", "at": at(15).isoformat(),
    }
    closed_view = own.custody_view(closed)
    assert closed_view["unknown_evidence"] == [] and closed_view["recorded_custody"] == []
    assert closed_view["waiting_for_grace"] == [] and closed_view["alert_outstanding"] == []
    # A heal-attempt key with no head shares the episode's name and must not also be waiting.
    both = own._empty()
    both["last_tick"] = {"at": at(5).isoformat(), "ok": True}
    both["episodes"][KEY] = {
        "since": at(0).isoformat(), "alerted_at": None, "notified_at": None,
        "reminders": 0, "branch": "codex/x", "reason": "r", "last_task": "old",
    }
    both["heal_attempts"][KEY] = {"result": "pending", "postcondition": "unknown"}
    both["heal_attempts"][f":{HEAD}"] = {"result": "pending", "postcondition": "unknown"}
    both_view = own.custody_view(both)
    assert both_view["waiting_for_grace"] == [] and both_view["recorded_custody"] == []
    assert both_view["unknown_evidence"] == [
        {"pr": "?", "why": "heal attempt key is unreadable", "alerted": False},
        {"pr": KEY, "why": "heal attempt key is unreadable", "alerted": False},
    ]
    bad_clock = own._empty()
    bad_clock["last_tick"] = {"at": at(5).isoformat(), "ok": True}
    bad_clock["episodes"][KEY] = {"since": "not-a-time", "alerted_at": at(5).isoformat()}
    assert own.custody_view(bad_clock)["unknown_evidence"] == [{
        "pr": KEY, "why": "episode clock is unreadable", "alerted": True,
    }]
    bad_clock["episodes"][KEY] = None
    assert own.custody_view(bad_clock)["unknown_evidence"] == [{
        "pr": KEY, "why": "episode is unreadable", "alerted": False,
    }]
    attempt = state["heal_attempts"][f"{KEY}:{HEAD}"]
    assert attempt["result"] == "recorded" and attempt["postcondition"] == "true"
    assert KEY not in state["episodes"] and other not in state["handoffs"]
    assert not any(f"alert outstanding: {KEY}" in line for line in lines)
    assert any("custody recorded" in body for _, _, body in inbox(tmp_path))
    # The same head is not recorded again.
    own.keeper_tick(now=at(30), **tick)
    assert own.load(path)["heal_attempts"][f"{KEY}:{HEAD}"]["result"] == "recorded"
    # A seat request left in the outbox by a failed delivery is not sent after custody is recorded.
    queued = tmp_path / "queued.json"
    blind = own.Finding(REPO, 42, "codex/x", "last owner ended timeout, PR not MERGED", "old")
    own.keeper_tick(
        now=at(0), path=queued, observe=lambda handoffs: ({KEY}, {KEY: blind}), service_factory=fleet(tmp_path),
    )

    def down():
        raise OSError("inbox down")

    lines, failed = own.keeper_tick(
        now=at(15), path=queued, observe=lambda handoffs: ({KEY}, {KEY: blind}), service_factory=down,
    )
    assert failed and own.load(queued)["outbox"] and own.load(queued)["heal_attempts"] == {}
    held = own.custody_view(own.load(queued))
    assert held["evidence"] == "known" and held["recorded_custody"] == [] and held["waiting_for_grace"] == []
    assert held["alert_outstanding"] == [{
        "pr": KEY, "since": at(0).isoformat(), "keeper": "not_recorded",
    }]
    lines, failed = own.keeper_tick(
        now=at(20), path=queued, observe=lambda handoffs: ({KEY}, {KEY: evidenced}), service_factory=fleet(tmp_path),
    )
    assert failed is False and own.load(queued)["outbox"] == []
    bodies = [body for _, _, body in inbox(tmp_path)]
    assert any("custody recorded" in body for body in bodies)
    assert not any("Seat one owner per PR" in body for body in bodies)


def test_tick_alerts_cto_inbox_and_clears_on_accepted_handoff(tmp_path):
    path = tmp_path / "state.json"
    observation = ({KEY}, {KEY: ownerless()})
    tick = dict(path=path, observe=lambda handoffs: observation, service_factory=fleet(tmp_path))
    assert own.keeper_tick(now=at(0), **tick)[1] is False
    assert inbox(tmp_path) == []
    assert own.keeper_tick(now=at(3), **tick) == ([], False)  # throttled
    lines, failed = own.keeper_tick(now=at(15), **tick)
    assert not failed and f"pr ownership alert outstanding: {KEY}" in lines
    ((recipient, kind, body),) = inbox(tmp_path)
    assert (recipient, kind) == ("claude-cto", "request") and KEY in body
    with own.locked(path) as data:
        accepted(data, 16)
    seen = {}

    def owned(handoffs):
        seen.update(handoffs)
        return {KEY}, {}

    lines, _ = own.keeper_tick(now=at(20), path=path, observe=owned, service_factory=fleet(tmp_path))
    assert seen == {(REPO.casefold(), 42): "succ"}
    assert "cleared=1" in lines[0]
    assert "Ownerless-PR alerts cleared" in inbox(tmp_path)[-1][2]


def test_unreadable_source_is_unknown_and_never_counts_toward_grace(tmp_path):
    path = tmp_path / "state.json"
    good = dict(path=path, service_factory=fleet(tmp_path))
    ownerless_now = lambda h: ({KEY}, {KEY: ownerless()})  # noqa: E731
    own.keeper_tick(now=at(0), observe=ownerless_now, **good)
    waiting = own.custody_view(own.load(path))
    assert waiting["evidence"] == "known"
    assert [row["pr"] for row in waiting["waiting_for_grace"]] == [KEY]

    def broken(handoffs):
        raise RuntimeError("gh down")

    lines, failed = own.keeper_tick(now=at(5), observe=broken, **good)
    assert failed and "UNKNOWN" in lines[0]
    state = own.load(path)
    assert state["episodes"] == {} and state["last_tick"]["ok"] is False
    unknown = own.custody_view(state)
    assert unknown["evidence"] == "unknown" and unknown["why"] == "RuntimeError"
    assert unknown["waiting_for_grace"] == [] and unknown["recorded_custody"] == []
    assert own.keeper_tick(now=at(9), observe=lambda h: pytest.fail("scan before cadence"), **good) == ([], False)
    assert own.custody_view(own.load(path))["evidence"] == "unknown"
    # The clock restarts once the source is readable again.
    own.keeper_tick(now=at(10), observe=ownerless_now, **good)
    assert own.keeper_tick(now=at(20), observe=ownerless_now, **good)[0][0].endswith("undelivered=0")
    assert inbox(tmp_path) == []
    own.keeper_tick(now=at(25), observe=ownerless_now, **good)
    assert len(inbox(tmp_path)) == 1
    # A raised alert survives an unknown tick.
    own.keeper_tick(now=at(80), observe=broken, **good)
    assert own.load(path)["episodes"][KEY]["alerted_at"]

def test_undelivered_alert_stays_in_the_outbox_and_is_resent_unchanged(tmp_path):
    path = tmp_path / "state.json"
    observe = lambda handoffs: ({KEY}, {KEY: ownerless()})  # noqa: E731
    own.keeper_tick(now=at(0), path=path, observe=observe, service_factory=fleet(tmp_path))

    def down():
        raise OSError("inbox down")

    lines, failed = own.keeper_tick(now=at(15), path=path, observe=observe, service_factory=down)
    assert failed and "undelivered=1" in lines[0]
    (entry,) = own.load(path)["outbox"]
    tick = dict(path=path, observe=lambda h: pytest.fail("delivery retry read GitHub"))
    assert own.keeper_tick(now=at(16), service_factory=down, **tick)[1] is True
    lines, failed = own.keeper_tick(now=at(17), service_factory=fleet(tmp_path), **tick)
    assert not failed and "undelivered=0" in lines[0]
    assert [body for _, _, body in inbox(tmp_path)] == [entry["body"]]
    assert own.load(path)["last_tick"] == {"at": at(15).isoformat(), "ok": True}
    assert own.keeper_tick(now=at(19), service_factory=fleet(tmp_path), **tick) == ([], False)
    assert "open=1" in own.keeper_tick(now=at(20), path=path, observe=observe, service_factory=fleet(tmp_path))[0][0]


def test_delivered_but_unsaved_entry_is_a_noop_on_retry(tmp_path):
    entry = own.outbox_entries({"alert": [{"pr": KEY, "since": at(0).isoformat(), "branch": "b", "reason": "r"}]})
    fleet(tmp_path)
    with AuthorityService(root=tmp_path / "fleet") as service:
        assert own.deliver(list(entry), service) == []
        outbox = list(entry)  # the save after delivery was lost
        assert own.deliver(outbox, service) == [] and outbox == []
    assert len(inbox(tmp_path)) == 1

def test_keeper_apply_runs_the_ownership_tick(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(keeper, "GitHub", lambda *args: object())
    monkeypatch.setattr(keeper, "run", lambda *a, **k: (_ for _ in ()).throw(keeper.KeeperError("queue down")))
    monkeypatch.setattr(own, "keeper_tick", lambda: calls.append(1) or (["pr ownership: open=0"], False))
    assert keeper.main(["--apply", "--repo-root", str(tmp_path)]) == 1  # queue failure still reported
    assert calls == [1]


# --- accept -----------------------------------------------------------------------


def do_accept(data, records, *, successor="succ", predecessor=None, custody=False, pr=None, rerun="unspent"):
    pr = pr or {"state": "OPEN", "headRefOid": HEAD, "headRefName": "codex/x", "isCrossRepository": False}
    return own.accept(data, repo=REPO, number=42, pr=pr, successor=successor, records=records,
                      predecessor=predecessor, custody=custody, now=at(30), rerun=rerun, live=is_live,
                      repos=FLEET)


def test_accept_records_one_owner_and_refuses_a_second():
    data = own._empty()
    records = [task("old", status="timeout"), task("succ", "claude/queue"), task("other", "claude/other")]
    receipt = do_accept(data, records, rerun="RECOVERY_ALLOWANCE_SPENT: first=run-rerun")
    assert receipt["successor"] == "succ" and receipt["rerun_allowance"].startswith("RECOVERY_ALLOWANCE_SPENT")
    with pytest.raises(own.OwnershipError, match="one owner at a time: succ"):
        do_accept(data, records, successor="other")
    assert do_accept(data, records)["successor"] == "succ"  # re-accepting is idempotent


def _write_task(branch="cursor/dispatch-branch", publish_branch=None):
    return {**task("succ", branch), "mode": "workspace-write", "publish_branch": publish_branch}


def test_accept_succeeds_when_publish_branch_is_the_pr_head(monkeypatch):
    monkeypatch.setenv(own.TASK_ENV, "succ")
    receipt = do_accept(own._empty(), [_write_task(publish_branch="codex/x")])
    assert receipt["successor"] == "succ" and receipt["head"] == HEAD


def test_accept_refuses_when_publish_branch_is_null(monkeypatch):
    monkeypatch.setenv(own.TASK_ENV, "succ")
    data = own._empty()
    with pytest.raises(own.PublishBranchRequired, match="re-dispatch with --branch codex/x") as caught:
        do_accept(data, [_write_task(publish_branch=None)])
    assert caught.value.code == "publish_branch_required" and data["handoffs"] == {}


def test_decision_custody_outside_a_task_is_unchanged(monkeypatch):
    monkeypatch.delenv(own.TASK_ENV, raising=False)
    data = own._empty()
    pr = {"state": "OPEN", "headRefOid": HEAD, "headRefName": "codex/x", "isCrossRepository": False}
    receipt = own.record_decision_custody(
        data, repo=REPO, number=42, pr=pr, reason="driver holding an ownerless PR",
        decision_owner="claude-cto", records=[_write_task()], now=at(30), rerun="unspent",
        repos=FLEET, live=is_live,
    )
    assert receipt["decision_owner"] == "claude-cto" and receipt["pr_state"] == "OPEN"
    assert data["handoffs"][KEY]["successor"] == "claude-cto"


def test_accept_refuses_while_a_live_task_owns_the_branch_or_successor_is_not_live():
    records = [task("live-owner"), task("succ", "claude/queue"), task("dead", "claude/d", "done")]
    with pytest.raises(own.OwnershipError, match="live-owner"):
        do_accept(own._empty(), records)
    without_binding = [{**records[0], "repository": None}, *records[1:]]
    data = own._empty()
    with pytest.raises(own.OwnershipError, match="live-owner"):
        do_accept(data, without_binding)
    assert data["handoffs"] == {}
    with pytest.raises(own.OwnershipError, match="not live"):
        do_accept(own._empty(), records, successor="dead")
    with pytest.raises(own.OwnershipError, match="unknown"):
        own.accept(own._empty(), repo=REPO, number=42, successor="succ", records=records, predecessor=None,
                   custody=False, now=at(30), rerun="unspent", live=lambda record: None, repos=FLEET,
                   pr={"state": "OPEN", "headRefOid": HEAD, "headRefName": "codex/x"})
    with pytest.raises(own.OwnershipError, match="MERGED"):
        do_accept(own._empty(), records, pr={"state": "MERGED", "headRefOid": HEAD, "headRefName": "codex/x"})


def test_custody_needs_the_live_waiting_owner_as_predecessor():
    records = [task("waiter"), task("integration", "claude/queue"), task("stranger", "claude/s")]
    with pytest.raises(own.OwnershipError, match="needs --custody"):
        do_accept(own._empty(), records, successor="integration", predecessor="waiter")
    with pytest.raises(own.OwnershipError, match="live owner"):
        do_accept(own._empty(), records, successor="integration", predecessor="stranger", custody=True)
    without_binding = [{**records[0], "repository": None}, *records[1:]]
    with pytest.raises(own.OwnershipError, match="live owner"):
        do_accept(own._empty(), without_binding, successor="integration", predecessor="waiter", custody=True)
    data = own._empty()
    receipt = do_accept(data, records, successor="integration", predecessor="waiter", custody=True)
    assert receipt["custody"] == list(own.CUSTODY_COVERS) and receipt["predecessor"] == "waiter"


def test_rerun_allowance_is_carried_across_owner_changes(tmp_path, monkeypatch):
    ledger = tmp_path / "ci-recovery.sqlite3"
    monkeypatch.setattr(recovery, "ledger_path", lambda cwd: ledger)
    assert own._rerun_allowance(REPO, 42, HEAD) == "unspent"
    # The first owner spends the head's one evidenced rerun ...
    recovery.consume(ledger, REPO, 42, HEAD, "run-rerun", {"run_id": 7})
    # ... and the successor's acceptance carries it: no fresh allowance on the same head.
    assert own._rerun_allowance(REPO, 42, HEAD).startswith("RECOVERY_ALLOWANCE_SPENT")
    assert own._rerun_allowance(REPO, 42, "b" * 40) == "unspent"


# --- queue exit -------------------------------------------------------------------


def custody_state(tick_minutes=25, ok=True):
    data = own._empty()
    data["last_tick"] = {"at": at(tick_minutes).isoformat(), "ok": ok}
    accepted(data, 26, successor="integration", predecessor="waiter", custody=list(own.CUSTODY_COVERS))
    return data


def exit_reasons(data, *, pr_head=HEAD, queued=True, state="OPEN", records=None, me="waiter"):
    records = records if records is not None else [task("waiter"), task("integration", "claude/queue")]
    return own.queue_exit(data, repo=REPO, number=42, pr={"state": state, "headRefOid": pr_head},
                          queued=queued, me=me, records=records, now=at(30), live=is_live)


def test_queue_exit_allowed_only_under_live_custody_with_the_alert_running():
    assert exit_reasons(custody_state()) == []
    assert "ownership alert" in exit_reasons(custody_state(tick_minutes=0))[0]
    assert "ownership alert" in exit_reasons(custody_state(ok=False))[0]
    assert "head moved" in exit_reasons(custody_state(), pr_head="b" * 40)[0]
    assert "merge queue" in exit_reasons(custody_state(), queued=None)[0]
    assert "closeout yourself" in exit_reasons(custody_state(), state="MERGED", queued=False)[0]
    dead = [task("waiter"), task("integration", "claude/queue", "timeout")]
    assert "not verifiably live" in exit_reasons(custody_state(), records=dead)[0]
    assert "no accepted queue custody" in exit_reasons(custody_state(), me="someone-else")[0]
    plain = custody_state()
    plain["handoffs"][KEY]["custody"] = []
    assert "no accepted queue custody" in exit_reasons(plain)[0]


# --- CLI --------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    from lu_machinery.dispatcher import store
    from lu_machinery.orchestration import fleet_repos

    path = tmp_path / "pr_ownership" / "state.json"
    monkeypatch.setattr(own, "state_path", lambda: path)
    records = [task("waiter"), task("integration", "claude/queue")]
    monkeypatch.setattr(store, "list_tasks", lambda *a, **k: records)
    monkeypatch.setattr(own, "liveness", is_live)
    monkeypatch.setattr(own, "_gh_pr", lambda repo, n: {"state": "OPEN", "headRefOid": HEAD, "headRefName": "codex/x"})
    monkeypatch.setattr(own, "_rerun_allowance", lambda *a: "unspent")
    repo = fleet_repos.FleetRepo("public", REPO, "learn-ukrainian", "public-monorepo", True)
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda: {"public": repo})
    return path


def test_cli_accept_then_queue_exit(world, monkeypatch, capsys):
    from lu_machinery.dispatcher import store

    monkeypatch.delenv(own.TASK_ENV, raising=False)
    assert own.main(["status"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["custody"]["evidence"] == "unknown"
    assert empty["custody"]["why"] == "no successful keeper tick"
    assert empty["custody"]["waiting_for_grace"] == []
    assert empty["custody"]["recorded_custody"] == []
    # A mixed call names both forms and writes nothing.
    assert own.main([
        "accept", "--pr", "42", "--repo", REPO, "--custody", "--from", "waiter",
        "--reason", "worker crashed after approval", "--decision-owner", "claude-cto",
    ]) == 1
    mixed = json.loads(capsys.readouterr().out)
    assert mixed["accepted"] is False
    assert mixed["usage"] == [own.QUEUE_CUSTODY_USAGE, own.RECORDED_CUSTODY_USAGE]
    monkeypatch.setenv(own.TASK_ENV, "integration")
    assert own.main([
        "accept", "--pr", "42", "--repo", REPO, "--custody", "--decision-owner", "claude-cto",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["usage"] == [own.QUEUE_CUSTODY_USAGE, own.RECORDED_CUSTODY_USAGE]
    monkeypatch.delenv(own.TASK_ENV, raising=False)
    assert own.main(["accept", "--pr", "42", "--repo", REPO, "--release", "--reason", "no"]) == 1
    assert json.loads(capsys.readouterr().out)["usage"] == [own.RELEASE_CUSTODY_USAGE]
    assert own.main(["accept", "--pr", "42", "--repo", REPO, "--reason", "held"]) == 1
    assert json.loads(capsys.readouterr().out)["usage"] == [own.RECORDED_CUSTODY_USAGE]
    # A supplied empty or blank --from is the queue form, including next to release or recorded custody.
    for extra in (["--from", ""], ["--from", " "], ["--decision-owner", ""]):
        assert own.main(["accept", "--pr", "42", "--repo", REPO, "--release", *extra]) == 1
        assert json.loads(capsys.readouterr().out)["usage"] == [own.RELEASE_CUSTODY_USAGE]
    assert own.main([
        "accept", "--pr", "42", "--repo", REPO, "--from", "",
        "--reason", "worker crashed after approval", "--decision-owner", "claude-cto",
    ]) == 1
    assert json.loads(capsys.readouterr().out)["usage"] == [own.QUEUE_CUSTODY_USAGE, own.RECORDED_CUSTODY_USAGE]
    assert not world.exists()
    monkeypatch.setattr(store, "list_tasks", lambda *a, **k: [task("old", status="timeout")])
    assert own.main([
        "accept", "--pr", "42", "--repo", REPO,
        "--reason", "worker crashed after approval", "--decision-owner", "claude-cto",
    ]) == 0
    recorded = own.load(world)["handoffs"][KEY]
    assert own.decision_custody(recorded)
    assert set(recorded) <= own._OWNERSHIP_FIELDS and recorded["custody"] == []
    capsys.readouterr()
    assert own.main(["status"]) == 0
    seated = json.loads(capsys.readouterr().out)["custody"]
    assert seated["recorded_custody"] == [{
        "pr": KEY, "decision_owner": "claude-cto", "head": HEAD, "by": "coordinator",
    }]
    assert seated["waiting_for_grace"] == [] and seated["evidence"] == "unknown"
    assert own.main(["accept", "--pr", "42", "--repo", REPO, "--release", "--from", ""]) == 1
    assert json.loads(capsys.readouterr().out)["usage"] == [own.RELEASE_CUSTODY_USAGE]
    assert KEY in own.load(world)["handoffs"]
    assert own.main(["accept", "--pr", "42", "--repo", REPO, "--release"]) == 0
    assert own.load(world)["handoffs"] == {}
    monkeypatch.setattr(store, "list_tasks", lambda *a, **k: [task("waiter"), task("integration", "claude/queue")])
    capsys.readouterr()
    assert own.main(["accept", "--pr", "42", "--repo", REPO]) == 2
    monkeypatch.setenv(own.TASK_ENV, "integration")
    assert own.main(["accept", "--pr", "42", "--repo", REPO, "--custody", "--from", "waiter"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["accepted"] is True
    queued = own.load(world)["handoffs"][KEY]
    assert set(queued) <= own._OWNERSHIP_FIELDS and "decision_owner" not in queued
    assert queued["custody"] == list(own.CUSTODY_COVERS)
    assert own.accepted_handoffs(world) == {(REPO.casefold(), 42): "integration"}
    monkeypatch.setattr(own, "_queued_at", lambda repo, n, head, base: head == HEAD)
    monkeypatch.setenv(own.TASK_ENV, "waiter")
    with own.locked(world) as data:
        data["last_tick"] = {"at": datetime.now(UTC).isoformat(), "ok": True}
    capsys.readouterr()
    assert own.main(["queue-exit", "--pr", "42", "--repo", REPO]) == 0
    assert json.loads(capsys.readouterr().out) == {"may_exit": True, "reasons": []}
    # Class: a verb acting on a repository the caller never named. Omitted --repo is refused before any lookup.
    for verb in ("accept", "queue-exit"):
        with pytest.raises(SystemExit) as refused:
            own.main([verb, "--pr", "42"])
        assert refused.value.code == 2
        assert "--repo" in capsys.readouterr().err
        # An empty repository lets native gh pick GH_REPO or its checkout; host, URL and .git spellings key dead handoffs.
        for spelled in ("", "github.com/o/r", "https://github.com/o/r", "o/r.git", "machinery"):
            with pytest.raises(SystemExit):
                own.main([verb, "--pr", "42", "--repo", spelled])
            assert "owner/name" in capsys.readouterr().err
    assert own.main(["status"]) == 0
    final = json.loads(capsys.readouterr().out)["custody"]
    assert final["evidence"] == "known" and final["recorded_custody"] == []
    assert final["waiting_for_grace"] == [] and final["unknown_evidence"] == []


def test_documented_module_invocation_shows_help():
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-m", "lu_machinery.orchestration.pr_ownership", "--help"],
                          capture_output=True, text=True, timeout=60, check=False, cwd=root)
    assert proc.returncode == 0 and "Exit codes:" in proc.stdout
    assert own.QUEUE_CUSTODY_USAGE in proc.stdout and own.RECORDED_CUSTODY_USAGE in proc.stdout
    accept_help = subprocess.run(
        [sys.executable, "-m", "lu_machinery.orchestration.pr_ownership", "accept", "--help"],
        capture_output=True, text=True, timeout=60, check=False, cwd=root,
    )
    assert accept_help.returncode == 0
    assert own.QUEUE_CUSTODY_USAGE in accept_help.stdout
    assert own.RECORDED_CUSTODY_USAGE in accept_help.stdout
    assert "never a merge gate" in accept_help.stdout


# --- round-1 review regressions -----------------------------------------------------


def test_unreadable_process_is_unknown_and_blocks_takeover(monkeypatch):
    fake_proc(monkeypatch, {"/proc/4242/stat": PermissionError("/proc/4242/stat")})
    owner = {**task("owner"), "pid": 4242, "process_start": "boot:1"}
    assert own.liveness(owner) is None
    live = lambda r: own.liveness(r) if r and r["task_id"] == "owner" else is_live(r)  # noqa: E731
    with pytest.raises(own.OwnershipError, match="one owner at a time: owner"):
        own.accept(own._empty(), repo=REPO, number=42, successor="succ", predecessor=None, custody=False,
                   records=[owner, task("succ", "claude/q")], now=at(30), rerun="unspent", repos=FLEET, live=live,
                   pr={"state": "OPEN", "headRefOid": HEAD, "headRefName": "codex/x", "isCrossRepository": False})


def test_unknown_interval_restarts_the_grace_clock():
    data = own._empty()
    own.step(data, {KEY}, {KEY: ownerless()}, at(0))
    own.step(data, {KEY}, {KEY: ownerless(unknown=True)}, at(5))
    assert own.step(data, {KEY}, {KEY: ownerless()}, at(60))["alert"] == []
    assert [row["pr"] for row in own.step(data, {KEY}, {KEY: ownerless()}, at(75))["alert"]] == [KEY]


def test_custody_predecessor_must_own_this_pr_by_the_owner_check():
    other_repo = {**task("waiter"), "repository": "someone/else"}
    with pytest.raises(own.OwnershipError, match="live owner"):
        do_accept(own._empty(), [other_repo, task("integration", "claude/q")],
                  successor="integration", predecessor="waiter", custody=True)
    fork = {"state": "OPEN", "headRefOid": HEAD, "headRefName": "codex/x", "isCrossRepository": True}
    with pytest.raises(own.OwnershipError, match="live owner"):
        do_accept(own._empty(), [task("waiter"), task("integration", "claude/q")],
                  successor="integration", predecessor="waiter", custody=True, pr=fork)
    # An accepted successor on another branch is the PR's owner and may hand over custody.
    data = own._empty()
    accepted(data, 0, successor="waiter")
    receipt = do_accept(data, [task("waiter", "claude/w"), task("integration", "claude/q")],
                        successor="integration", predecessor="waiter", custody=True)
    assert receipt["predecessor"] == "waiter"


def test_queue_membership_is_bound_to_the_custody_head(monkeypatch):
    from lu_machinery.orchestration import integration_sweep

    def membership(self, repo, pr):
        if pr["headRefOid"] != "b" * 40:  # the same observation shows the head moved to B
            raise integration_sweep.SweepError("merge queue head moved or membership unavailable")
        return True

    monkeypatch.setattr(integration_sweep.GitHubAdapter, "queue_membership", membership)
    assert own._queued_at(REPO, 42, HEAD, "main") is None
    assert own._queued_at(REPO, 42, "b" * 40, "main") is True
    assert "custody head" in exit_reasons(custody_state(), queued=None)[0]


def test_notice_identity_ignores_the_clock(tmp_path):
    notices = {"alert": [{"pr": KEY, "since": at(0).isoformat(), "branch": "codex/x", "reason": "r"}]}
    assert own.outbox_entries(notices) == own.outbox_entries(notices)
    later = {"alert": [{"pr": KEY, "since": at(0).isoformat(), "branch": "codex/x", "reason": "r2"}]}
    assert own.outbox_entries(later)[0]["key"] == own.outbox_entries(notices)[0]["key"]

def test_malformed_nested_state_fails_the_tick_without_raising(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({**own._empty(), "episodes": {KEY: None}}))
    lines, failed = own.keeper_tick(now=at(0), path=path, observe=lambda h: pytest.fail("observed"),
                                    service_factory=fleet(tmp_path))
    assert failed and "state unusable" in lines[0]


def test_keeper_keeps_its_queue_result_when_the_ownership_tick_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(keeper, "GitHub", lambda *args: object())
    monkeypatch.setattr(keeper, "run", lambda *a, **k: (["#42 enqueued"], False))
    monkeypatch.setattr(own, "keeper_tick", lambda: (_ for _ in ()).throw(AttributeError("boom")))
    assert keeper.main(["--apply", "--repo-root", str(tmp_path)]) == 1
    assert "#42 enqueued" in capsys.readouterr().out


@pytest.mark.parametrize(("reason", "reset", "retry"), [
    ("pool_exhausted", 60, 60),
    ("retry_after", 20, 20),
    ("retry_after", 7, 10),
    ("secondary_hold", None, 10),
])
def test_head_rate_limit_delays_next_scan_without_ownerless_evidence(tmp_path, monkeypatch, reason, reset, retry):
    from lu_machinery.common.github_client import Result

    reset_at = int(at(reset).timestamp()) if reset is not None else None
    refusal = Result(status=429, error="github_rate_limited", reset_at=reset_at, reason=reason)
    monkeypatch.setattr(own.github_client, "run", lambda *a, **k: SimpleNamespace(returncode=75, github_result=refusal))

    def limited(handoffs):
        own._with_head_commit(REPO, {"number": 42, "headRefOid": HEAD})
        pytest.fail("rate limit was not propagated")

    path = tmp_path / "s.json"
    own.keeper_tick(now=at(0), path=path, observe=lambda h: ({KEY}, {KEY: ownerless()}),
                    service_factory=fleet(tmp_path))
    lines, failed = own.keeper_tick(now=at(5), path=path, observe=limited, service_factory=fleet(tmp_path))
    assert not failed and "skipped" in lines[0]
    state = own.load(path)
    assert state["last_tick"]["ok"] is False  # queue-exit stays closed
    assert state["episodes"] == {}  # rate-limited time never counts toward grace
    lines, failed = own.keeper_tick(now=at(retry) - timedelta(seconds=1), path=path,
                                    observe=lambda h: pytest.fail("scan before retry deadline"),
                                    service_factory=fleet(tmp_path))
    assert (lines, failed) == ([], False)
    assert own.custody_view(own.load(path))["evidence"] == "unknown"
    lines, failed = own.keeper_tick(now=at(retry), path=path, observe=lambda h: ({KEY}, {KEY: ownerless()}),
                                    service_factory=fleet(tmp_path))
    assert not failed and "open=1" in lines[0]
    assert own.load(path)["episodes"][KEY]["since"] == at(retry).isoformat()


def test_retry_after_a_channel_context_change_reconciles_instead_of_stalling(tmp_path):
    entry = own.outbox_entries({"alert": [{"pr": KEY, "since": at(0).isoformat(), "branch": "b", "reason": "r"}]})
    fleet(tmp_path)
    with AuthorityService(root=tmp_path / "fleet") as service:
        assert own.deliver(list(entry), service) == []
        # The save after delivery is lost, then the channel's context revision moves on.
        service.set_channel_context(own.CTO_CHANNEL, "new channel context")
        outbox = list(entry)
        assert own.deliver(outbox, service) == [] and outbox == []
        broken = [{**entry[0], "key": "pr-ownership:alert:other"}]
        service.close()
        assert own.deliver(broken, service) and broken  # a real failure stays queued
    assert len(inbox(tmp_path)) == 1


# --- pre-enqueue keeper custody (DETACH-CI-1122 PR 1) ------------------------------


def _verdict_receipt(pr_number: int = 42, head: str = HEAD, **extra):
    base = {
        "pr": pr_number,
        "head": head,
        "task": "review-42-r1",
        "verdict": "APPROVED",
        "comment": "posted",
        "status": "posted",
    }
    base.update(extra)
    return base


def _worker_task(task_id: str = "worker-1", branch: str = "codex/x", *, status: str = "running", nonce: str = "nonce-1", initiator: str | None = "claude-cto"):
    rec = {**task(task_id, branch, status=status), "run_nonce": nonce}
    if initiator is not None:
        rec["initiator"] = initiator
    return rec


def test_keeper_transfer_binds_exact_pr_and_allows_worker_exit_while_ci_pending(tmp_path, monkeypatch, capsys):
    """Defect class: worker cannot exit with durable keeper custody while CI is pending before enqueue."""
    from lu_machinery.dispatcher import store
    from lu_machinery.orchestration import fleet_repos

    path = tmp_path / "pr_ownership" / "state.json"
    pr_open = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "codex/x",
        "baseRefName": "main",
        "isCrossRepository": False,
    }
    worker = _worker_task("worker-1", nonce="nonce-abc")
    ack = own.acknowledge_keeper_transfer(
        path,
        repo=REPO,
        number=42,
        pr=pr_open,
        worker_task="worker-1",
        worker_nonce="nonce-abc",
        records=[worker],
        now=at(0),
        rerun="unspent",
        repos=FLEET,
        verdict_receipt=_verdict_receipt(),
        live=is_live,
    )
    assert ack["accepted"] is True and ack["acknowledged"] is True
    assert ack["custody_mode"] == "keeper" and ack["keeper_state"] == "active"
    assert ack["repository"] == REPO and ack["pr"] == 42 and ack["base"] == "main" and ack["head"] == HEAD
    assert ack["worker_task"] == "worker-1" and ack["worker_nonce"] == "nonce-abc"
    assert ack["review_classification"] == "verdict" and ack["verdict_receipt"]["verdict"] == "APPROVED"

    # Exact retry is idempotent.
    ack2 = own.acknowledge_keeper_transfer(
        path,
        repo=REPO,
        number=42,
        pr=pr_open,
        worker_task="worker-1",
        worker_nonce="nonce-abc",
        records=[worker],
        now=at(1),
        rerun="unspent",
        repos=FLEET,
        verdict_receipt=_verdict_receipt(),
        live=is_live,
    )
    assert ack2["custody_sha256"] == ack["custody_sha256"]

    # After worker exits cleanly while CI is still pending, PR is NOT alerted ownerless.
    tick_args = dict(path=path, observe=lambda h: ({KEY}, {KEY: ownerless()}), service_factory=fleet(tmp_path))
    assert own.keeper_tick(now=at(5), **tick_args)[1] is False
    assert own.keeper_tick(now=at(25), **tick_args)[1] is False
    assert inbox(tmp_path) == []
    view = own.custody_view(own.load(path))
    assert view["keeper_custody"] == [{
        "pr": KEY,
        "keeper_state": "active",
        "base": "main",
        "head": HEAD,
        "worker_task": "worker-1",
        "worker_nonce": "nonce-abc",
        "review_classification": "verdict",
    }]
    assert view["alert_outstanding"] == [] and view["waiting_for_grace"] == []

    # CLI keeper-transfer also supports --trivial and --verdict-receipt-file.
    cli_path = tmp_path / "cli_state.json"
    monkeypatch.setattr(own, "state_path", lambda: cli_path)
    monkeypatch.setattr(store, "list_tasks", lambda *a, **k: [worker])
    monkeypatch.setattr(own, "liveness", is_live)
    monkeypatch.setattr(own, "_gh_pr", lambda repo, n: pr_open)
    monkeypatch.setattr(own, "_rerun_allowance", lambda *a: "unspent")
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda: FLEET)
    monkeypatch.setenv(own.TASK_ENV, "worker-1")
    assert own.main(["keeper-transfer", "--pr", "42", "--repo", REPO, "--nonce", "nonce-abc", "--trivial"]) == 0
    cli_ack = json.loads(capsys.readouterr().out)
    assert cli_ack["acknowledged"] is True and cli_ack["review_classification"] == "trivial"
    assert cli_ack["verdict_receipt"] is None

    # Verdict receipt file on a second PR.
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(_verdict_receipt(pr_number=43)))
    worker_43 = _worker_task("worker-43", branch="codex/y", nonce="nonce-43")
    monkeypatch.setattr(store, "list_tasks", lambda *a, **k: [worker_43])
    monkeypatch.setattr(own, "_gh_pr", lambda repo, n: {**pr_open, "headRefName": "codex/y"})
    monkeypatch.setenv(own.TASK_ENV, "worker-43")
    assert own.main([
        "keeper-transfer", "--pr", "43", "--repo", REPO, "--nonce", "nonce-43",
        "--verdict-receipt-file", str(receipt_file),
    ]) == 0
    file_ack = json.loads(capsys.readouterr().out)
    assert file_ack["acknowledged"] is True and file_ack["review_classification"] == "verdict"


def test_keeper_transfer_refuses_invalid_bindings_draft_and_non_owner():
    """Defect class: forged receipt, wrong nonce, draft PR, or non-owner obtains keeper custody."""
    pr_open = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "codex/x",
        "baseRefName": "main",
        "isCrossRepository": False,
    }
    worker = _worker_task("worker-1", nonce="nonce-1")
    stranger = _worker_task("stranger", branch="codex/other", nonce="nonce-s")
    dead = _worker_task("worker-1", status="done", nonce="nonce-1")

    def call(**overrides):
        kwargs = dict(
            repo=REPO, number=42, pr=pr_open, worker_task="worker-1", worker_nonce="nonce-1",
            records=[worker], now=at(0), rerun="unspent", repos=FLEET,
            verdict_receipt=_verdict_receipt(), trivial=False, live=is_live,
        )
        kwargs.update(overrides)
        return own.transfer_to_keeper(own._empty(), **kwargs)

    # Draft or closed PR refused.
    with pytest.raises(own.OwnershipError, match="draft"):
        call(pr={**pr_open, "isDraft": True})
    with pytest.raises(own.OwnershipError, match="not open"):
        call(pr={**pr_open, "state": "CLOSED"})
    # Mismatched nonce or dead worker or non-owner refused.
    with pytest.raises(own.OwnershipError, match="worker_nonce"):
        call(worker_nonce="wrong-nonce")
    with pytest.raises(own.OwnershipError, match="not live"):
        call(records=[dead])
    with pytest.raises(own.OwnershipError, match="does not currently own"):
        call(worker_task="stranger", worker_nonce="nonce-s", records=[worker, stranger])
    # Both or neither of verdict_receipt and trivial refused.
    with pytest.raises(own.OwnershipError, match="not both"):
        call(verdict_receipt=_verdict_receipt(), trivial=True)
    with pytest.raises(own.OwnershipError, match="requires a verdict_receipt or trivial"):
        call(verdict_receipt=None, trivial=False)
    # Forged / mismatched verdict receipts refused.
    for bad in (
        _verdict_receipt(verdict="CHANGES_REQUESTED"),
        _verdict_receipt(head="b" * 40),
        _verdict_receipt(pr_number=99),
        _verdict_receipt(status="failed"),
        _verdict_receipt(repository="other/repo"),
    ):
        with pytest.raises(own.OwnershipError):
            call(verdict_receipt=bad)
    # Different binding overwrite refused.
    data = own._empty()
    own.transfer_to_keeper(
        data, repo=REPO, number=42, pr=pr_open, worker_task="worker-1", worker_nonce="nonce-1",
        records=[worker], now=at(0), rerun="unspent", repos=FLEET, verdict_receipt=_verdict_receipt(), live=is_live,
    )
    with pytest.raises(own.OwnershipError, match="different binding"):
        own.transfer_to_keeper(
            data, repo=REPO, number=42, pr=pr_open, worker_task="worker-1", worker_nonce="nonce-1",
            records=[worker], now=at(1), rerun="unspent", repos=FLEET, trivial=True, live=is_live,
        )


def test_keeper_repair_custody_released_only_when_cto_successor_accepts():
    """Defect class: repair custody cleared without a live claude-cto successor accepting ownership."""
    pr_open = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": HEAD,
        "headRefName": "codex/x",
        "baseRefName": "main",
        "isCrossRepository": False,
    }
    data = own._empty()
    worker_live = _worker_task("worker-1", nonce="nonce-1", status="running")
    own.transfer_to_keeper(
        data, repo=REPO, number=42, pr=pr_open, worker_task="worker-1", worker_nonce="nonce-1",
        records=[worker_live], now=at(0), rerun="unspent", repos=FLEET, verdict_receipt=_verdict_receipt(), live=is_live,
    )
    worker_done = _worker_task("worker-1", nonce="nonce-1", status="done")
    cto_succ = _worker_task("repair-1", branch="claude/repair", status="running", nonce="nonce-r1", initiator="claude-cto")
    rogue_succ = _worker_task("rogue-1", branch="claude/rogue", status="running", nonce="nonce-g1", initiator="other-lane")

    # Active keeper custody refuses accept, decision custody, and release_decision_custody.
    with pytest.raises(own.OwnershipError, match="active keeper custody"):
        do_accept(data, [worker_done, cto_succ], successor="repair-1", pr=pr_open)
    with pytest.raises(own.OwnershipError, match="keeper custody"):
        own.record_decision_custody(
            data, repo=REPO, number=42, pr=pr_open, reason="hold", decision_owner="claude-cto",
            records=[worker_done], now=at(5), rerun="unspent", repos=FLEET, live=is_live,
        )
    with pytest.raises(own.OwnershipError, match="keeper custody"):
        own.release_decision_custody(data, repo=REPO, number=42, pr=pr_open, now=at(5))

    # Transition to repair custody.
    updated = own.mark_keeper_repair(data, repo=REPO, number=42, reason="CI-red-CI Gate", now=at(10), head="b" * 40, run_ids=[555])
    assert updated is not None and updated["keeper_state"] == "repair" and updated["observed_head"] == "b" * 40

    # Non-CTO successor and release_decision_custody are still refused.
    with pytest.raises(own.OwnershipError, match="claude-cto successor"):
        do_accept(data, [worker_done, rogue_succ], successor="rogue-1", pr=pr_open)
    with pytest.raises(own.OwnershipError, match="keeper custody"):
        own.release_decision_custody(data, repo=REPO, number=42, pr=pr_open, now=at(11))

    # Only the CTO's live successor releases repair custody.
    receipt = do_accept(data, [worker_done, cto_succ], successor="repair-1", pr=pr_open)
    assert receipt["successor"] == "repair-1"
    assert not own.keeper_custody(data["handoffs"][KEY])
    assert any(item["event"] == "repair_custody_released" and item["successor"] == "repair-1" for item in data["history"])


# --- run PR inventory ------------------------------------------------------------


@pytest.fixture
def run_pr_store(tmp_path, monkeypatch, capsys):
    from lu_machinery.orchestration import fleet_repos

    tasks = tmp_path / "tasks"
    tasks.mkdir()
    archive = tasks / "archive"
    archive.mkdir()
    ownership = tmp_path / "pr_ownership" / "state.json"
    monkeypatch.setenv("LU_TASKS_DIR", str(tasks))
    monkeypatch.delenv(own.TASK_ENV, raising=False)
    monkeypatch.setattr(fleet_repos, "load_fleet_repos", lambda: FLEET)
    branches = {}
    prs = {}

    def write(record, *, archived=False):
        directory = archive if archived else tasks
        (directory / f"{record['task_id']}.json").write_text(json.dumps({"schema_version": 2, **record}))

    write({"task_id": "run", "run_nonce": "run-1"}, archived=True)

    def child(task_id, branch=None, *, archived=False, **fields):
        record = {
            "task_id": task_id, "run_nonce": f"{task_id}-1", "parent_task_id": "run", "parent_run_nonce": "run-1",
            "repository": REPO, "worktree_branch": branch, "status": "done" if archived else "running",
            **fields,
        }
        write(record, archived=archived)
        return record

    def github(args, **kwargs):
        if args[1] == "api":
            assert args[2:4] == ["--paginate", "--slurp"]
            endpoint = urlsplit(args[4])
            repo = endpoint.path.removeprefix("repos/").removesuffix("/pulls")
            body = [branches.get((repo, parse_qs(endpoint.query)["head"][0]), [])]
            assert parse_qs(endpoint.query)["state"] == ["all"]
        else:
            assert args[1:3] == ["pr", "view"]
            assert args[4] == "--repo" and args[6] == "--json"
            assert "commits" not in args[7].split(",")
            number, repo = int(args[3]), args[5]
            state, head = prs[repo, number]
            body = {"number": number, "state": state, "headRefOid": head}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    monkeypatch.setattr(own.github_client, "run", github)

    def read():
        status = own.main(["run-prs", "--task-id", "run", "--run-nonce", "run-1"])
        result = capsys.readouterr()
        assert status == 0, result.err
        payload = json.loads(result.out)
        assert payload["task_id"] == "run" and payload["run_nonce"] == "run-1"
        assert "unrecorded branches" in payload["residuals"][0]
        return payload["pull_requests"]

    return SimpleNamespace(
        tasks=tasks, archive=archive, ownership=ownership, write=write, child=child, read=read,
        branches=branches, prs=prs,
    )


def _branch_pull(repo, branch, number):
    return {
        "number": number, "state": "open", "head": {"ref": branch, "repo": {"full_name": repo}},
        "base": {"repo": {"full_name": repo}},
    }


def test_run_prs_does_not_omit_live_children_open_prs_on_recorded_branches(run_pr_store):
    store = run_pr_store
    store.child("live", "codex/live", publish_branch="claude/published")
    store.child("other-repo", "codex/live", repository=MACHINERY)
    store.child("another-run", "codex/ignored", parent_run_nonce="run-2")
    store.branches[REPO, "learn-ukrainian:codex/live"] = [_branch_pull(REPO, "codex/live", 42)]
    store.branches[REPO, "learn-ukrainian:claude/published"] = [_branch_pull(REPO, "claude/published", 43)]
    store.branches[MACHINERY, "learn-ukrainian:codex/live"] = [_branch_pull(MACHINERY, "codex/live", 42)]
    store.branches[REPO, "learn-ukrainian:codex/ignored"] = [_branch_pull(REPO, "codex/ignored", 99)]
    store.prs[REPO, 42] = store.prs[REPO, 43] = store.prs[MACHINERY, 42] = ("OPEN", HEAD)

    assert store.read() == [
        {"repo": REPO, "number": 42, "state": "OPEN", "head": HEAD, "found_via": ["worktree_branch"]},
        {"repo": REPO, "number": 43, "state": "OPEN", "head": HEAD, "found_via": ["publish_branch"]},
        {"repo": MACHINERY, "number": 42, "state": "OPEN", "head": HEAD, "found_via": ["worktree_branch"]},
    ]


def test_run_prs_does_not_omit_an_accepted_pr_on_another_branch(run_pr_store):
    store = run_pr_store
    successor = store.child("succ", "claude/repair", repository=MACHINERY)
    with own.locked(store.ownership) as data:
        do_accept(data, [successor])
    live_head = "b" * 40
    store.prs[REPO, 42] = ("OPEN", live_head)

    assert store.read() == [
        {"repo": REPO, "number": 42, "state": "OPEN", "head": live_head, "found_via": ["accepted"]},
    ]


def test_run_prs_does_not_omit_keeper_custody_before_or_after_release(run_pr_store):
    store = run_pr_store
    worker = store.child("worker", "codex/x")
    own.acknowledge_keeper_transfer(
        store.ownership, repo=REPO, number=42, worker_task="worker", worker_nonce="worker-1",
        pr={"state": "OPEN", "isDraft": False, "headRefOid": HEAD, "headRefName": "codex/x",
            "baseRefName": "main", "isCrossRepository": False},
        records=[worker], now=at(0), rerun="unspent", repos=FLEET, trivial=True, live=is_live,
    )
    store.prs[REPO, 42] = ("OPEN", HEAD)
    assert store.read() == [
        {"repo": REPO, "number": 42, "state": "OPEN", "head": HEAD, "found_via": ["keeper_transfer"]},
    ]

    assert own.release_closed_keeper_custody(store.ownership, repo=REPO, number=42, how="merged", now=at(1))
    store.prs[REPO, 42] = ("MERGED", HEAD)
    assert store.read() == [
        {"repo": REPO, "number": 42, "state": "MERGED", "head": HEAD,
         "found_via": ["keeper_custody_released", "keeper_transfer"]},
    ]


def test_run_prs_reports_live_merge_state_after_branch_discovery(run_pr_store):
    store = run_pr_store
    store.child("merged", "codex/merged", archived=True, delivery_pr=f"{REPO}#42",
                delivery_prs=[f"{REPO}#42", f"{MACHINERY}#43"])
    store.branches[REPO, "learn-ukrainian:codex/merged"] = [_branch_pull(REPO, "codex/merged", 42)]
    store.prs[REPO, 42] = store.prs[MACHINERY, 43] = ("MERGED", HEAD)

    assert store.read() == [
        {"repo": REPO, "number": 42, "state": "MERGED", "head": HEAD,
         "found_via": ["delivery_pr", "delivery_prs", "worktree_branch"]},
        {"repo": MACHINERY, "number": 43, "state": "MERGED", "head": HEAD, "found_via": ["delivery_prs"]},
    ]


def test_run_prs_custody_history_does_not_lose_accept_after_250_events(run_pr_store):
    store = run_pr_store
    store.child("succ", "claude/old", archived=True)
    with own.locked(store.ownership) as data:
        do_accept(data, [task("succ", "claude/old")])
    with own.locked(store.ownership) as data:
        own.step(data, set(), {}, at(31))
        data["history"].extend(
            {"pr": own.pr_key(REPO, n), "at": at(32).isoformat(), "event": "alerted"}
            for n in range(100, 350)
        )
    data = own.load(store.ownership)
    assert data["handoffs"] == {}
    assert len(data["history"]) == 201
    assert data["history"][0]["event"] == "accepted"
    store.prs[REPO, 42] = ("CLOSED", HEAD)
    assert store.read() == [
        {"repo": REPO, "number": 42, "state": "CLOSED", "head": HEAD, "found_via": ["accepted"]},
    ]


def test_run_prs_refuses_complete_output_when_an_archive_record_is_unreadable(run_pr_store, capsys):
    store = run_pr_store
    store.child("succ", "codex/x")
    (store.archive / "broken.json").write_text("{")
    assert own.main(["run-prs", "--task-id", "run", "--run-nonce", "run-1"]) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert "unreadable task record broken.json" in result.err


def test_pr_ownership_rejects_non_ascii_handoff_keys_and_verdict_repos(tmp_path):
    """Defect class: Unicode-digit handoff keys via int() or unguarded casefold alias real PR ownership records."""
    bad_digits_path = tmp_path / "unicode_digits.json"
    bad_digits_path.write_text(json.dumps({
        "schema": 1,
        "last_tick": None,
        "episodes": {},
        "handoffs": {
            f"{REPO}#1277": {
                "successor": "codex/valid-worker",
                "accepted_at": at(0).isoformat(),
                "head": HEAD,
            },
            f"{REPO}#\u0664\u0662": {
                "successor": "codex/worker",
                "accepted_at": at(0).isoformat(),
                "head": HEAD,
            },
        },
        "history": [],
        "outbox": [],
        "heal_attempts": {},
    }))
    loaded = own.load(bad_digits_path)
    assert list(loaded["handoffs"]) == [f"{REPO}#1277"]
    with pytest.raises(own.OwnershipError), own.locked(bad_digits_path):
        pass

    lookalike_repo = REPO.replace("k", "\u212a")
    with pytest.raises(own.OwnershipError):
        own.pr_key(lookalike_repo, 42)
    with pytest.raises(own.OwnershipError):
        own.keeper_custodies_for_repo(tmp_path / "missing.json", lookalike_repo)
    with pytest.raises(own.OwnershipError, match="repository"):
        own._validate_verdict_receipt(
            {
                "repository": lookalike_repo,
                "pr": 42,
                "head": HEAD,
                "task": "review-1",
                "verdict": "APPROVED",
                "status": "posted",
            },
            pr=42,
            head=HEAD,
            repo=REPO,
        )
