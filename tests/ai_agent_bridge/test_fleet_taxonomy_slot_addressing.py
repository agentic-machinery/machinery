"""Hermetic contract and unit tests for Taxonomy Step 4b — slot addressing and slot->holder resolution."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lu_machinery import agent_identity
from lu_machinery.common.repo_root import machinery_path
from lu_machinery.orchestration.slot_routing import resolve_slot_holder
from tests.helpers.product_tree import configured_roots  # noqa: F401

pytestmark = pytest.mark.usefixtures("configured_roots", "bridge_wake_dir")

_AREA_ASSIGNMENTS_YAML = machinery_path("config/area_assignments.yaml")


# ---------------------------------------------------------------------------
# 1. Slot Roster Acceptance & Validation Tests
# ---------------------------------------------------------------------------


def test_roster_slots_accepted_by_validation() -> None:
    """Verify every slot configured in area_assignments.yaml is accepted by validation."""
    all_valids = agent_identity.get_valid_agents(assignments_path=_AREA_ASSIGNMENTS_YAML)
    assert "grok-infra" in all_valids
    assert "claude-atlas" in all_valids
    assert "codex-devops" in all_valids
    assert "gemini-hramatka" in all_valids
    assert "gemini-infra" in all_valids
    assert "claude-folk" in all_valids
    assert "codex-corpus" in all_valids
    assert "cursor-infra" in all_valids
    assert "cursor-atlas" in all_valids
    assert "codex-hramatka" in all_valids


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "grok", "cursor"])
def test_monitor_aliases_preserve_inbox_destinations_beside_muse(provider: str) -> None:
    slot = f"{provider}-monitor"
    # grok-monitor is a registered slot (#10835); the other monitor names keep the bare-provider inbox.
    expected = slot if provider == "grok" else provider
    assert agent_identity.resolve_recipient_alias(slot, assignments_path=_AREA_ASSIGNMENTS_YAML) == expected
    for muse_slot in ("muse-monitor", "muse-devops"):
        assert muse_slot in agent_identity.get_valid_recipient_agents(assignments_path=_AREA_ASSIGNMENTS_YAML)
        assert agent_identity.resolve_recipient_alias(muse_slot, assignments_path=_AREA_ASSIGNMENTS_YAML) == muse_slot
    assert agent_identity.resolve_recipient_alias("unknown-monitor", assignments_path=_AREA_ASSIGNMENTS_YAML) == "unknown-monitor"
    assert "unknown-monitor" not in agent_identity.get_valid_recipient_agents(assignments_path=_AREA_ASSIGNMENTS_YAML)


@pytest.mark.parametrize(
    ("provider", "lane"),
    [
        *((provider, lane) for provider in ("kimi", "cursor") for lane in ("folk", "bio", "hramatka", "open-model-data")),
        *(("grok", lane) for lane in ("folk", "bio", "hramatka")),
    ],
)
def test_removed_content_slots_are_not_bridge_recipients(provider: str, lane: str) -> None:
    slot = f"{provider}-{lane}"
    assert slot not in agent_identity.get_valid_recipient_agents(assignments_path=_AREA_ASSIGNMENTS_YAML)


def test_registry_absent_or_unreadable_fallback_to_static_tuple(tmp_path: Path) -> None:
    """Verify fallback to STATIC_VALID_AGENTS when area_assignments.yaml is missing or corrupt.

    MUTATION-CHECK: If fallback is removed/broken, get_valid_agents fails or crashes.
    """
    missing_file = tmp_path / "non_existent_area_assignments.yaml"
    valids_missing = agent_identity.get_valid_agents(assignments_path=missing_file)
    assert valids_missing == agent_identity.STATIC_VALID_AGENTS

    corrupt_file = tmp_path / "corrupt_area_assignments.yaml"
    corrupt_file.write_text("invalid: yaml: ::: [[", encoding="utf-8")
    valids_corrupt = agent_identity.get_valid_agents(assignments_path=corrupt_file)
    assert valids_corrupt == agent_identity.STATIC_VALID_AGENTS


# ---------------------------------------------------------------------------
# 2. R2 Validation Sites Tests
# ---------------------------------------------------------------------------


def test_bottleneck_alerts_accepts_roster_slot_holder(tmp_path: Path) -> None:
    """Verify bottleneck_alerts._active_lease_holder accepts a registry roster slot as holder (R2 site 2)."""
    from lu_machinery.fleet_comms.bottleneck_alerts import _active_lease_holder
    from lu_machinery.session_streams import LeaseHolder, SessionStreamDatabase, SessionStreamStore

    db_path = tmp_path / "session-streams.sqlite3"
    db = SessionStreamDatabase(db_path)
    store = SessionStreamStore(db)
    now = datetime.now(UTC)
    store.open_session(
        stream_id="epic:4707",
        holder=LeaseHolder(
            agent="grok-infra",
            harness="grok",
            instance_id="inst-1",
            process_id=100,
            task_id="task-1",
        ),
        lineage_id="lineage-1",
        ttl_seconds=3600,
        now=now,
    )

    holder = _active_lease_holder("4707", session_db=db_path, now=now)
    assert holder == "grok-infra"


# ---------------------------------------------------------------------------
# 3. Slot->Holder Resolution Helper Tests (resolve_slot_holder)
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db_fixture(tmp_path: Path) -> Path:
    """Create a temporary schema-conformant session-streams.sqlite3 database."""
    db_path = tmp_path / "session-streams.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            state TEXT NOT NULL
        );

        CREATE TABLE stream_leases (
            stream_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            holder_agent TEXT NOT NULL,
            holder_harness TEXT,
            generation INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            state TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def test_resolve_slot_holder_live_holder(session_db_fixture: Path) -> None:
    """Verify resolve_slot_holder returns holder facts for an active, non-expired lease."""
    db_path = session_db_fixture
    future_time = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (session_id, stream_id, state) VALUES (?, ?, ?)",
        ("sess_atlas_123", "epic:4387", "open"),
    )
    conn.execute(
        """INSERT INTO stream_leases
           (stream_id, session_id, holder_agent, holder_harness, generation, expires_at, state)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("epic:4387", "sess_atlas_123", "claude-infra", "claude", 1, future_time, "active"),
    )
    conn.commit()
    conn.close()

    res = resolve_slot_holder("claude-atlas", session_db_path=db_path)
    assert res.has_holder is True
    assert res.slot == "claude-atlas"
    assert res.area_id == "atlas"
    assert res.stream_id == "epic:4387"  # allow-hardcoded-epic: slot addressing taxonomy resolution
    assert res.session_id == "sess_atlas_123"
    assert res.holder_agent == "claude-infra"
    assert res.holder_harness == "claude"
    assert res.generation == 1
    assert res.expires_at == future_time
    assert res.queue_location == "channels DB delivery queue for 'claude-atlas'"


def test_resolve_slot_holder_no_holder(session_db_fixture: Path) -> None:
    """Verify resolve_slot_holder returns has_holder=False when no active lease exists."""
    res = resolve_slot_holder("grok-infra", session_db_path=session_db_fixture)
    assert res.has_holder is False
    assert res.slot == "grok-infra"
    assert res.area_id == "infra"
    assert res.queue_location == "channels DB delivery queue for 'grok-infra'"
    assert res.reason == "no-live-holder"


def test_resolve_slot_holder_expired_lease_treated_as_no_live_holder(session_db_fixture: Path) -> None:
    """Verify expired lease (expires_at in past) is treated as no-live-holder."""
    db_path = session_db_fixture
    past_time = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (session_id, stream_id, state) VALUES (?, ?, ?)",
        ("sess_devops_456", "epic:5703", "open"),
    )
    conn.execute(
        """INSERT INTO stream_leases
           (stream_id, session_id, holder_agent, holder_harness, generation, expires_at, state)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("epic:5703", "sess_devops_456", "codex-infra", "codex", 1, past_time, "active"),
    )
    conn.commit()
    conn.close()

    res = resolve_slot_holder("codex-devops", session_db_path=db_path)
    assert res.has_holder is False
    assert res.slot == "codex-devops"
    assert res.area_id == "devops"
    assert res.reason == "no-live-holder"


def test_resolve_slot_holder_unknown_area_slot() -> None:
    """Verify resolve_slot_holder returns unknown-area reason for unmapped area slot."""
    res = resolve_slot_holder("claude-nosucharea")
    assert res.has_holder is False
    assert res.slot == "claude-nosucharea"
    assert res.reason == "unknown-area"


def test_resolve_slot_holder_alias_slot(session_db_fixture: Path) -> None:
    """Verify alias slot resolution (e.g. claude-folk -> area seminars -> epic:2836)."""
    db_path = session_db_fixture
    future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sessions (session_id, stream_id, state) VALUES (?, ?, ?)",
        ("sess_folk_789", "epic:2836", "rolling"),
    )
    conn.execute(
        """INSERT INTO stream_leases
           (stream_id, session_id, holder_agent, holder_harness, generation, expires_at, state)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("epic:2836", "sess_folk_789", "grok-infra", "grok", 2, future_time, "active"),
    )
    conn.commit()
    conn.close()

    res = resolve_slot_holder("claude-folk", session_db_path=db_path)
    assert res.has_holder is True
    assert res.slot == "claude-folk"
    assert res.area_id == "seminars"
    assert res.stream_id == "epic:2836"  # allow-hardcoded-epic: slot addressing alias resolution
    assert res.holder_agent == "grok-infra"
