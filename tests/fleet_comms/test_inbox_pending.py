"""Synthetic store proofs for the body-free pending probe (#10558)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lu_machinery import inbox_pending
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.fleet_comms.authority import AuthorityService, pending_delivery_snapshot
from tests.helpers.agy_inbox_fixtures import (
    BODY_MARKER,
    CLI_BOOTSTRAP,
    MESSAGE_ID,
    run_cli,
    snapshot,
)
from tests.helpers.agy_inbox_fixtures import (
    add_authority as add_legacy_authority,
)
from tests.helpers.product_tree import configured_roots as configured_roots
from tests.host_context.conftest import host_layout as host_layout
from tests.host_context.conftest import installed_hook_env

pytestmark = pytest.mark.usefixtures("configured_roots")

pytest_plugins = ("tests.helpers.agy_inbox_fixtures",)
ROOT = Path(__file__).resolve().parents[2]
EXPECTED_UNKNOWN_NOTICE = (
    "PENDING ORDERS: unknown; "
    "authority: .venv/bin/python -m lu_machinery.fleet_comms deliveries --help"
)


@pytest.fixture
def inbox_hook_binding(host_layout, configured_roots, stores, monkeypatch):
    """Bind the native hook to fixture checkouts with the installed runtime."""
    state = Path(host_layout.data["state_root"])
    fleet = state / "batch_state/fleet-comms/v1"
    fleet.mkdir(parents=True)
    authority = stores[0].rename(fleet / stores[0].name)
    for name, value in installed_hook_env(host_layout).items():
        monkeypatch.setenv(name, value)
    return (authority,)


def add_authority(path, **kwargs):
    add_legacy_authority(path, **kwargs)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE comms_messages SET kind = 'request' WHERE message_id = ?", (kwargs.get("identifier", MESSAGE_ID),))


def test_legacy_supervisory_only_in_ordinary_slot_is_not_pending(stores):
    """Defect class: unclaimable legacy supervisory mail inflates ordinary inbox counts."""
    add_legacy_authority(stores[0])
    assert inbox_pending.probe("gemini-infra") == {
        "count": 0, "ids": [], "truncated": False, "stores": {"authority": 0}, "probe": "ok",
    }
    assert inbox_pending.hook("{}", "gemini-infra") == {}


@pytest.mark.parametrize(
    ("state", "consumed", "wake", "expected"),
    [
        ("queued", False, False, 1),
        ("running", False, False, 1),
        ("running", True, False, 0),
        ("running", False, True, 1),
        ("acknowledged", False, False, 0),
        ("expired", False, False, 0),
        ("failed", False, False, 0),
        ("dead_lettered", False, False, 0),
    ],
)
def test_authority_delivery_states(stores, state, consumed, wake, expected):
    add_authority(stores[0], state=state, consumed=consumed, wake_consumed=wake)
    assert inbox_pending.probe("gemini-infra") == {
        "count": expected,
        "ids": [MESSAGE_ID] if expected else [],
        "truncated": False,
        "stores": {"authority": expected},
        "probe": "ok",
    }


def test_h26_wake_observation_is_not_supervisory_consumption(inbox_hook_binding, roster):
    stores = inbox_hook_binding
    recipient = "supervisor:epic:9999"
    with AuthorityService(root=stores[0].parent) as service:
        message = service.enqueue_supervisory_request(
            sender="supervisor",
            recipient=recipient,
            body=BODY_MARKER,
            correlation_id="h26",
            idempotency_key="h26",
        )
        delivery_id = message.delivery_ids[0]
        lease = service.claim_next_delivery(recipient, "h26-worker")
        assert lease is not None
        service.record_wake_receipt(delivery_id, fence_token=lease.fence_token, state="consumed")
        expected_metadata = {
            "count": 1,
            "ids": [message.message_id],
            "truncated": False,
            "probe": "ok",
            "content_included": False,
        }
        expected_notice = {}
        print(
            "H26 frozen expectations:",
            json.dumps({"status": "delivered", "metadata": expected_metadata, "hook": expected_notice}),
        )
        before = snapshot(stores)
        metadata = pending_delivery_snapshot(recipient)
        hook_proc = subprocess.run(
            ["bash", str(machinery_path("agy_hooks/pending_orders_inject.sh"))],
            input="{}",
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert hook_proc.returncode == 0
        assert hook_proc.stderr == ""
        assert BODY_MARKER not in hook_proc.stdout
        notice = json.loads(hook_proc.stdout)
        status = service.supervisory_delivery_status(delivery_id)
        print(
            "H26 actual before authority consumption:",
            json.dumps({"status": status, "metadata": metadata, "hook": notice}),
        )
        assert snapshot(stores) == before
        assert status == "delivered"
        assert metadata == expected_metadata
        assert notice == expected_notice

        service.record_supervisory_consumption(
            delivery_id,
            worker_id="h26-worker",
            fence_token=lease.fence_token,
            driver_generation="h26-generation",
        )
        status = service.supervisory_delivery_status(delivery_id)
        assert status == "live_driver_consumed"
        before = snapshot(stores)
        metadata = pending_delivery_snapshot(recipient)
        assert metadata == {
            "count": 0,
            "ids": [],
            "truncated": False,
            "probe": "ok",
            "content_included": False,
        }
        hook_proc = subprocess.run(
            ["bash", str(machinery_path("agy_hooks/pending_orders_inject.sh"))],
            input="{}",
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert hook_proc.returncode == 0
        assert hook_proc.stderr == ""
        notice = json.loads(hook_proc.stdout)
        assert notice == {}
        assert snapshot(stores) == before
        print(
            "H26 actual after authority consumption:",
            json.dumps({"status": status, "metadata": metadata, "hook": notice, "stores_unchanged": True}),
        )


@pytest.mark.parametrize("state", ["queued", "running"])
def test_expired_deadline_is_silent(stores, state):
    add_authority(stores[0], state=state, deadline="2000-01-01T00:00:00Z")
    assert inbox_pending.probe("gemini-infra")["count"] == 0


@pytest.mark.parametrize(
    "recipient",
    ["gemini", "unknown", "gemini-not-a-lane", "", "gemini-infra\n", "gemini-infra' OR 1=1--"],
)
def test_recipient_shapes(stores, recipient):
    add_authority(stores[0], recipient="claude")
    result = inbox_pending.probe(recipient)
    assert result["probe"] == "unknown"
    assert result["ids"] == []


def test_registry_unavailable_is_unknown(stores, roster):
    roster.unlink()
    assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"


def test_counts_and_five_full_ids(stores):
    for identifier in range(1, 8):
        add_authority(stores[0], identifier="message_" + f"{identifier:032x}", delivery_id=f"delivery-{identifier}")
    assert inbox_pending.probe("gemini-infra") == {
        "count": 7,
        "ids": ["message_" + f"{identifier:032x}" for identifier in range(1, 6)],
        "truncated": True,
        "stores": {"authority": 7},
        "probe": "ok",
    }


def test_readonly_hashes_and_no_body_or_artifacts(stores, tmp_path):
    add_authority(stores[0], state="running")
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in stores}
    files = set(tmp_path.iterdir())
    for _ in range(2):
        result = inbox_pending.probe("gemini-infra")
        assert result["probe"] == "ok"
        assert result["stores"] == {"authority": 1}
        assert BODY_MARKER not in json.dumps(result)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in stores} == before
    assert set(tmp_path.iterdir()) == files


def test_missing_store_returns_unknown_without_creation(stores):
    stores[0].unlink()
    assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"
    assert not stores[0].exists()


def test_locked_store_is_unknown(stores):
    with sqlite3.connect(stores[0]) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"


def test_unreadable_store_is_unknown(stores, monkeypatch):
    from lu_machinery.fleet_comms import authority
    original = authority.open_readonly

    def denied(path, **kwargs):
        if path == stores[0]:
            raise PermissionError(BODY_MARKER)
        return original(path, **kwargs)

    monkeypatch.setattr(authority, "open_readonly", denied)
    result = inbox_pending.probe("gemini-infra")
    assert result["probe"] == "unknown"
    assert BODY_MARKER not in json.dumps(result)


def test_schema_drift_and_corrupt_store_unknown(stores):
    stores[0].write_bytes(b"not sqlite " + BODY_MARKER.encode())
    assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"


@pytest.mark.parametrize("schema_fault", ["table", "delivery_id", "state"])
@pytest.mark.parametrize("pending", [False, True])
def test_wake_schema_drift_is_unknown(stores, roster, schema_fault, pending):
    if pending:
        add_authority(stores[0])
    with sqlite3.connect(stores[0]) as conn:
        if schema_fault == "table":
            conn.execute("DROP TABLE authority_wake_receipts")
        else:
            conn.execute(f"ALTER TABLE authority_wake_receipts RENAME COLUMN {schema_fault} TO unavailable")
    before = snapshot(stores)
    assert run_cli("gemini-infra") == {
        "count": 0,
        "ids": [],
        "truncated": False,
        "stores": {"authority": None},
        "probe": "unknown",
    }
    assert inbox_pending.hook("{}", "gemini-infra") == {"injectSteps": [{"ephemeralMessage": EXPECTED_UNKNOWN_NOTICE}]}
    assert snapshot(stores) == before


def test_invalid_stored_id_never_injects_data(stores):
    add_authority(stores[0], identifier=BODY_MARKER)
    result = inbox_pending.probe("gemini-infra")
    assert result["probe"] == "unknown"
    assert BODY_MARKER not in json.dumps(result)


def test_imported_authority_identifier(stores):
    identifier = "legacy-message-" + "a" * 32
    add_authority(stores[0], identifier=identifier)
    assert inbox_pending.probe("gemini-infra")["ids"] == [identifier]


def test_cli_returns_only_metadata(stores):
    result = subprocess.run(
        [sys.executable, "-m", "lu_machinery.inbox_pending", "--recipient", "gemini-infra"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "count": 0,
        "ids": [],
        "truncated": False,
        "stores": {"authority": 0},
        "probe": "ok",
    }
    assert BODY_MARKER not in result.stdout


def test_cli_help(capsys):
    assert inbox_pending.main(["--help"]) == 0
    assert "Outputs:" in capsys.readouterr().out


@pytest.mark.parametrize("hook_mode", [False, True])
def test_cli_bad_arguments_hide_error_data(capsys, hook_mode):
    args = ["--" + BODY_MARKER]
    if hook_mode:
        args.append("--agy-hook")
    assert inbox_pending.main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert BODY_MARKER not in captured.out
    result = json.loads(captured.out)
    if hook_mode:
        assert result == {"injectSteps": [{"ephemeralMessage": EXPECTED_UNKNOWN_NOTICE}]}
    else:
        assert result["probe"] == "unknown"


def test_hook_cli_does_not_echo_stdin(inbox_hook_binding, monkeypatch, capsys):
    from io import StringIO

    monkeypatch.setattr(sys, "stdin", StringIO(BODY_MARKER))
    assert inbox_pending.main(["--agy-hook"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"injectSteps": [{"ephemeralMessage": EXPECTED_UNKNOWN_NOTICE}]}


@pytest.mark.parametrize(
    ("failure", "store_index", "pending"),
    [
        ("missing-store", 1, True),
        ("malformed-schema", 1, False),
        ("malformed-schema", 1, True),
        ("garbage-stdin", None, True),
        ("missing-slot", None, True),
    ],
)
def test_unknown_cli_notice_guides_both_drains(inbox_hook_binding, roster, tmp_path, monkeypatch, failure, store_index, pending):
    stores = inbox_hook_binding
    if pending:
        add_authority(stores[0], state="running")
    if failure == "missing-store":
        stores[0].unlink()
    elif failure == "malformed-schema":
        with sqlite3.connect(stores[0]) as conn:
            conn.execute("DROP TABLE authority_wake_receipts")
    elif failure == "missing-slot":
        monkeypatch.delenv("SESSION_HANDOFF_AGENT")
    existing_stores = [path for path in stores if path.exists()]
    before = snapshot(existing_stores)
    files = set(tmp_path.rglob("*"))
    proc = subprocess.run(
        [sys.executable, "-c", CLI_BOOTSTRAP, "--agy-hook"],
        input=BODY_MARKER if failure == "garbage-stdin" else "{}",
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert BODY_MARKER not in proc.stdout
    result = json.loads(proc.stdout)
    expected = {"injectSteps": [{"ephemeralMessage": EXPECTED_UNKNOWN_NOTICE}]}
    assert result == expected
    if result:
        line = result["injectSteps"][0]["ephemeralMessage"]
        assert len(line) <= 200
        assert line.isascii() and all(32 <= ord(char) < 127 for char in line)
    assert snapshot(existing_stores) == before
    assert set(tmp_path.rglob("*")) == files


def test_wal_reader_sees_latest_committed_order(stores):
    with sqlite3.connect(stores[0]) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        add_authority(stores[0])
        assert inbox_pending.probe("gemini-infra")["ids"] == [MESSAGE_ID]


def test_retired_plane_and_relative_override_are_unknown(stores, monkeypatch, tmp_path, configured_roots):
    monkeypatch.delenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW")
    (tmp_path / "READ_ME_CANONICAL_ON_JOB_HOST.txt").write_text("retired")
    assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"
    assert pending_delivery_snapshot("gemini-infra", root=stores[0].parent)["probe"] == "unknown"
    monkeypatch.setenv("FLEET_COMMS_ALLOW_LOCAL_SHADOW", "1")
    (configured_roots.primary / "comms.sqlite3").write_bytes(stores[0].read_bytes())
    assert pending_delivery_snapshot("gemini-infra", root=configured_roots.primary)["probe"] == "unknown"
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("FLEET_COMMS_ROOT", tmp_path.name)
    assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"
    assert pending_delivery_snapshot("gemini-infra", root=Path(tmp_path.name))["probe"] == "unknown"
    monkeypatch.setenv("FLEET_COMMS_ROOT", "relative-plane")
    assert inbox_pending.probe("gemini-infra")["probe"] == "unknown"


@pytest.mark.parametrize("count", [0, 1, 8])
def test_direct_renderer_behavior(stores, count):
    for identifier in range(1, count + 1):
        add_authority(stores[0], identifier="message_" + f"{identifier:032x}", delivery_id=f"delivery-{identifier}")
    result = inbox_pending.hook("{}", "gemini-infra")
    if not count:
        assert result == {}
    else:
        ids = "message_00000000000000000000000000000001"
        if count > 1:
            ids += ",message_00000000000000000000000000000002; truncated"
        assert result == {
            "injectSteps": [
                {
                    "ephemeralMessage": f"PENDING ORDERS: {count} (ids {ids}); authority: .venv/bin/python -m lu_machinery.fleet_comms deliveries --help"
                }
            ]
        }


@pytest.mark.parametrize("payload", ["[]", '{"value": NaN}', BODY_MARKER])
def test_direct_invalid_payload_is_unknown(stores, payload):
    assert inbox_pending.hook(payload, "gemini-infra") == {
        "injectSteps": [{"ephemeralMessage": EXPECTED_UNKNOWN_NOTICE}]
    }
