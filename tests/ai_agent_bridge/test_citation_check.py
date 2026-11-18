"""Tests for the citation-provenance check (#1683).

Coverage:
  1. Detection — every source in the registry is recognized.
  2. Verification — flag fires for fabricated AD citation about
     `собака`. Verification passes when the source actually covers the
     headword.
  3. Annotation placement — markers go BEFORE any trailing
     `[AGREE]`/`[DISAGREE]` token so the deliberation tail check
     (`_channels_cli.py`) keeps matching.
  4. Graceful degradation — missing sources DB → soft-skip, not flag.
  5. De-duplication — multi-pattern same-source mentions in close
     proximity collapse to one citation.
  6. Regression — the canonical Gemini fabrication body from threads
     `482884ca054e` and `7c6e401053bb` flags exactly once on АД.

The verification tests use ``monkeypatch`` to swap out the actual
``wiki.sources_db`` lookup functions, so the suite runs without
needing ``data/sources.db`` to exist on the test machine.
"""



import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lu_machinery.ai_agent_bridge import _citation_check as cc
from tests.agent_runtime.agy_hook_helpers import register_agy_hooks as register_agy_hooks
from tests.helpers.product_tree import configured_roots as configured_roots

# ─────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────


def test_detect_antonenko_basic():
    body = "Антоненко-Давидович flags it as a Russianism."
    citations = cc.detect_citations(body)
    assert len(citations) == 1
    assert citations[0].source == "antonenko_davydovych"
    assert citations[0].source_label == "Антоненко-Давидович"


def test_detect_yak_my_hovorymo_alias_is_collapsed():
    """«Як ми говоримо» (the AD publication) is the same source —
    when both names appear close together, dedup collapses to one
    citation rather than emitting two parallel flags for one claim.
    """
    body = (
        "Антоненко-Давидович у книжці «Як ми говоримо» каже, "
        "що *собака* — чоловічого роду."
    )
    citations = cc.detect_citations(body)
    sources = [c.source for c in citations]
    assert sources.count("antonenko_davydovych") == 1, (
        f"Expected dedup to collapse to 1, got {sources}"
    )


def test_detect_all_known_sources_separately():
    body = (
        "VESUM lists *голос*. СУМ-11 has the entry. ЕСУМ traces the etymology. "
        "Грінченко 1907 attests it. Шевельов notes a phonological shift. "
        "Вихованець discusses syntax. Пономарів in his stylistic guide. "
        "Antonenko-Davydovych «Як ми говоримо». Український правопис 2019."
    )
    citations = cc.detect_citations(body)
    sources = {c.source for c in citations}
    expected = {
        "vesum",
        "sum_11",
        "esum",
        "hrinchenko_1907",
        "shevelov",
        "vykhovanets",
        "ponomariv",
        "antonenko_davydovych",
        "pravopys_2019",
    }
    assert expected.issubset(sources), (
        f"Missing sources: {expected - sources}"
    )


def test_detect_extracts_italicized_headword():
    body = "Антоненко-Давидович пише про *собака* в орудному."
    citations = cc.detect_citations(body)
    assert len(citations) == 1
    assert citations[0].headword == "собака"


def test_detect_extracts_imennyk_noun_pattern():
    body = "За Антоненком-Давидовичем, іменник собака — чоловічого роду."
    citations = cc.detect_citations(body)
    assert len(citations) == 1
    assert citations[0].headword == "собака"


def test_detect_no_citations_in_plain_message():
    assert cc.detect_citations("Hello, plain message.") == []
    assert cc.detect_citations("") == []


def test_detect_strips_stress_marks_in_normalize():
    # _normalize_word handles combining stress (U+0301) + curly apostrophes.
    # "со́бака" = с + о + U+0301 + б + а + к + а — the canonical Ukrainian
    # stress representation. Latin precomposed "á" is NOT normalized
    # because it's not a valid Ukrainian character.
    assert cc._normalize_word("Со́бака") == "собака"
    assert cc._normalize_word("дідусь’ого") == "дідусь'ого"


# ─────────────────────────────────────────────────────────────────────
# Verification — mocked lookups
# ─────────────────────────────────────────────────────────────────────


class _FakeSourcesDB:
    """Stand-in for wiki.sources_db with controllable lookup results."""

    SOURCES_DB_PATH = Path("/tmp/fake-sources-db-path-that-must-exist")

    def __init__(self, *, style_guide=None, sum11=None, hrinchenko=None, esum=None):
        self._style = style_guide or {}  # word -> [hits]
        self._sum11 = sum11 or {}
        self._hrin = hrinchenko or {}
        self._esum = esum or {}

    # The verifier only calls these via _try_load_sources_db().
    def search_style_guide(self, word, limit=5):
        return self._style.get(word, [])

    def search_definitions(self, word, limit=10):
        return self._sum11.get(word, [])

    def search_grinchenko_1907(self, word, limit=10):
        return self._hrin.get(word, [])

    def search_esum(self, query, limit=5, **kw):
        return self._esum.get(query, [])


def _make_fake_db_present(monkeypatch, **lookups):
    """Install a fake sources_db whose path is reported as existing."""
    fake = _FakeSourcesDB(**lookups)
    # Replace the lazy-load helper with one that returns our fake.
    monkeypatch.setattr(cc, "_try_load_sources_db", lambda: fake)

    # _verify_antonenko also calls _body_text_has(SOURCES_DB_PATH, ...).
    # Default it to return False so the body-text fallback doesn't
    # accidentally find hits. Tests can override.
    monkeypatch.setattr(cc, "_body_text_has", lambda *a, **kw: False)


def test_verify_antonenko_fabrication_about_sobaka_flags(monkeypatch):
    _make_fake_db_present(monkeypatch, style_guide={})
    body = (
        "Антоненко-Давидович пише, що feminine agreement with *собака* "
        "is a calque/Russianism."
    )
    result = cc.check_and_annotate(body)
    assert len(result.unverified) == 1
    assert result.unverified[0].citation.source == "antonenko_davydovych"
    assert "собака" in result.unverified[0].detail


def test_verify_antonenko_real_entry_does_not_flag(monkeypatch):
    """If AD's word column or body contains the headword, no flag."""
    _make_fake_db_present(monkeypatch, style_guide={"учень": [{"id": 1}]})
    body = "Антоненко-Давидович обговорює *учень* в орудному."
    result = cc.check_and_annotate(body)
    assert result.unverified == []


def test_verify_antonenko_body_fallback_passes(monkeypatch):
    """word-column miss but body-text hit → verified."""
    _make_fake_db_present(monkeypatch, style_guide={})
    monkeypatch.setattr(cc, "_body_text_has", lambda *a, **kw: True)
    body = "Антоненко-Давидович обговорює *учень*."
    result = cc.check_and_annotate(body)
    assert result.unverified == []


def test_sum11_entry_is_not_verification_evidence(monkeypatch):
    _make_fake_db_present(monkeypatch, sum11={"собака": [{"word": "собака"}]})
    body = "СУМ-11 has *собака* as masculine."
    result = cc.check_and_annotate(body)
    flagged_sources = [v.citation.source for v in result.unverified]
    assert "sum_11" in flagged_sources
    assert next(v for v in result.unverified if v.citation.source == "sum_11").detail == "sovietized_source_not_authority"
    assert "sovietized_source_not_authority" in result.annotated_body


def test_vesum_and_shevelov_soft_skip(monkeypatch):
    _make_fake_db_present(monkeypatch)
    body = "VESUM lists *голос*. Шевельов notes a shift in *хата*."
    result = cc.check_and_annotate(body)
    # VESUM and Шевельов both have _verify_unknown — never flag.
    assert result.unverified == []
    # Both citations were detected, just not verified.
    sources = {c.source for c in result.citations}
    assert {"vesum", "shevelov"} <= sources


# ─────────────────────────────────────────────────────────────────────
# Annotation
# ─────────────────────────────────────────────────────────────────────


def test_annotation_preserves_agree_tail(monkeypatch):
    _make_fake_db_present(monkeypatch, style_guide={})
    body = "Антоненко-Давидович каже про *собака* (Russianism).\n\n[AGREE]"
    result = cc.check_and_annotate(body)
    assert result.annotated_body.rstrip().endswith("[AGREE]")
    assert "<!-- CITATION-UNVERIFIED" in result.annotated_body


def test_no_annotation_when_all_verified(monkeypatch):
    _make_fake_db_present(monkeypatch, style_guide={"учень": [{"id": 1}]})
    body = "Антоненко-Давидович обговорює *учень*.\n\n[AGREE]"
    result = cc.check_and_annotate(body)
    assert "<!-- CITATION-UNVERIFIED" not in result.annotated_body
    assert result.annotated_body == body  # untouched


# ─────────────────────────────────────────────────────────────────────
# Graceful degradation
# ─────────────────────────────────────────────────────────────────────


def test_missing_sources_db_soft_skips(monkeypatch):
    """When sources DB is unavailable, never flag — only soft-skip."""
    monkeypatch.setattr(cc, "_try_load_sources_db", lambda: None)
    body = "Антоненко-Давидович пише про *собака*."
    result = cc.check_and_annotate(body)
    assert result.unverified == []  # no flags
    # But citations are still detected.
    assert len(result.citations) == 1
    assert result.citations[0].source == "antonenko_davydovych"


def test_pravopys_never_flags_in_v1(monkeypatch):
    """Regression: Codex review of #1683 caught that the original
    pravopys verifier (a) used a topic→section helper not a headword
    lookup, false-flagging unmapped words like ``кав'ярня``, and (b)
    did synchronous live HTTP via `pravopys_section`. v1 deliberately
    soft-skips every Правопис citation.

    This regression locks in: even with a callable lookup that returns
    ``None`` (the topic-not-mapped case), no flag fires. Replace this
    test with proper Правопис verification logic when a deterministic
    local verifier ships.
    """
    # Even an installable lookup that returns None must not produce a flag.
    monkeypatch.setattr(cc, "_try_load_pravopys", lambda: lambda topic: None)
    body = (
        "Український правопис 2019 §11 рекомендує писати *кав'ярня* "
        "разом, без апострофа в основі."
    )
    result = cc.check_and_annotate(body)
    flagged_sources = [v.citation.source for v in result.unverified]
    assert "pravopys_2019" not in flagged_sources
    # And the citation should still be detected.
    detected_sources = {c.source for c in result.citations}
    assert "pravopys_2019" in detected_sources


# ─────────────────────────────────────────────────────────────────────
# Regression — canonical fabrication
# ─────────────────────────────────────────────────────────────────────


# Verbatim from thread `482884ca054e467a8fc7f596fb66ae96`, Gemini's r1
# reply to the собака-gender deliberation question (2026-05-05 ~00:30Z).
# Trimmed to the part that triggers the fabrication; the rest of the
# reply is grammar paradigms + adjectival-agreement which don't cite
# any source. The canonical fabrication is the explicit АД attribution.
_GEMINI_FABRICATION_482884 = """In modern literary Ukrainian, **собака** is strictly a **masculine** noun (чоловічий рід).

**Source Authority:**
This assignment is codified in VESUM and the academic dictionaries (СУМ-11, СУМ-20). It is also explicitly emphasized by Антоненко-Давидович у посібнику «Як ми говоримо», who categorizes the use of feminine agreement with *собака* as a morphological calque/Russianism.

**Declension Paradigm:**
Despite its masculine gender, *собака* belongs to the **First Declension, hard group**.

[AGREE]"""


def test_canonical_fabrication_regression_482884(monkeypatch):
    """Regression: the verbatim Gemini fake reply must flag exactly once on АД.

    The body mentions VESUM (soft-skip — no automated verifier), СУМ-11
    (never verification evidence), and АД (fabricated — must flag).
    Tail is `[AGREE]` and must remain at the end of the output
    so the deliberation tail-check still recognizes the message.
    """
    # Real-world DB state for собака:
    #   - sum11.word=собака exists       (1 hit)
    #   - grinchenko.word=собака exists  (1 hit)
    #   - style_guide: NO entry, NO body mention
    _make_fake_db_present(
        monkeypatch,
        style_guide={},  # AD has no собака
        sum11={"собака": [{"word": "собака"}]},
        hrinchenko={"собака": [{"word": "собака"}]},
    )

    result = cc.check_and_annotate(_GEMINI_FABRICATION_482884)

    # Both the fabricated AD claim and the disallowed СУМ-11 authority flag.
    flagged_sources = [v.citation.source for v in result.unverified]
    assert flagged_sources == ["sum_11", "antonenko_davydovych"], flagged_sources

    # AGREE tail is preserved at end.
    assert result.annotated_body.rstrip().endswith("[AGREE]")

    # Annotation appears before the [AGREE] tail.
    body_no_tail = result.annotated_body.rsplit("[AGREE]", 1)[0]
    assert "CITATION-UNVERIFIED" in body_no_tail
    assert "antonenko_davydovych" in body_no_tail
    assert 'headword="собака"' in body_no_tail


@pytest.mark.parametrize(
    "tail",
    ["", "\n[AGREE]", "\n\n[AGREE]", "\n[DISAGREE]", "\n\n  [AGREE]  "],
)
def test_tail_preservation_variants(monkeypatch, tail):
    _make_fake_db_present(monkeypatch, style_guide={})
    body = f"Антоненко-Давидович пише про *собака*.{tail}"
    result = cc.check_and_annotate(body)
    if tail.strip():
        assert result.annotated_body.rstrip().endswith(tail.strip())
    if "собака" in body:
        assert any(
            v.citation.source == "antonenko_davydovych"
            for v in result.unverified
        )


# ─────────────────────────────────────────────────────────────────────
# Headword extraction edge cases
# ─────────────────────────────────────────────────────────────────────


def test_headword_extraction_picks_closest_to_citation():
    """If multiple Cyrillic-headword candidates exist, prefer the one
    nearest the source-name span. Avoids picking unrelated words from
    the same paragraph.
    """
    body = (
        "Earlier we mentioned *вертеп* in passing. "
        "Now: Антоненко-Давидович пише про *собака*. "
        "And later we'll discuss *кав'ярня*."
    )
    citations = cc.detect_citations(body)
    assert len(citations) == 1
    assert citations[0].headword == "собака"


def test_headword_extraction_handles_missing():
    body = "Антоненко-Давидович is a good source."
    citations = cc.detect_citations(body)
    assert len(citations) == 1
    # No Cyrillic headword in the sentence — gracefully None.
    assert citations[0].headword is None


def test_headword_extraction_falls_back_to_pre_citation():
    """Codex review of #1683 follow-up: when the only Cyrillic
    headword candidate is BEFORE the citation, extraction should
    still find it. Post-citation bias is a tiebreaker, not a hard
    filter.

    Construction: "про *X* пише Антоненко-Давидович" — the headword
    `X` precedes the citation, no headword follows it. Without
    pre-citation fallback we would emit ``headword=None`` and lose
    the verification opportunity.
    """
    body = "Про *собака* пише Антоненко-Давидович."
    citations = cc.detect_citations(body)
    assert len(citations) == 1
    assert citations[0].headword == "собака"


# ─────────────────────────────────────────────────────────────────────
# Integration with _channels.post()
# ─────────────────────────────────────────────────────────────────────


def _isolated_db(tmp_path, monkeypatch):
    """Spin up a fresh broker DB rooted in ``tmp_path`` and seed the
    test channel + agents. Returns the connection.

    Repoints ``_config.DB_PATH`` to a tmp file so each test gets its
    own DB without touching the real broker. Cleared automatically
    when ``tmp_path`` is removed at end of the test.
    """
    from lu_machinery.ai_agent_bridge import _db
    db_path = tmp_path / "messages.db"
    monkeypatch.setenv("AB_DB_PATH", str(db_path))
    conn = _db.init_db()
    conn.execute(
        "INSERT INTO channels(name, description, subscribers, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("reviews", "test", "claude,gemini,codex", "2026-05-05T00:00:00Z"),
    )
    conn.commit()
    monkeypatch.setattr(_db, "get_db", lambda: conn)
    return conn


def test_channels_post_annotates_unverified_citation(monkeypatch, tmp_path):
    """End-to-end: posting a message with a fabricated citation results
    in the body (as persisted) carrying the CITATION-UNVERIFIED marker.
    """
    from lu_machinery.ai_agent_bridge import _channels, _citation_check

    fake = _FakeSourcesDB(style_guide={})  # no AD entries
    monkeypatch.setattr(_citation_check, "_try_load_sources_db", lambda: fake)
    monkeypatch.setattr(_citation_check, "_body_text_has", lambda *a, **kw: False)

    conn = _isolated_db(tmp_path, monkeypatch)

    body = (
        "Антоненко-Давидович categorizes feminine *собака* as a Russianism."
    )
    result = _channels.post(
        "reviews",
        "gemini",
        body,
        to_agents=["claude"],
        kind="post",
        auto_snapshot=False,
    )

    row = conn.execute(
        "SELECT body FROM channel_messages WHERE message_id = ?",
        (result["message_id"],),
    ).fetchone()
    assert row is not None
    persisted = row["body"]
    assert "CITATION-UNVERIFIED" in persisted
    assert "antonenko_davydovych" in persisted
    assert 'headword="собака"' in persisted
    assert "feminine *собака*" in persisted


@pytest.fixture()
def _synthetic_publishing_rules(synthetic_opsec, publisher_transport, monkeypatch):
    """Public writes use synthetic policy, independently of host tooling."""
    monkeypatch.setenv("GH_REPO", "unit/public")


class TestConfig:
    """Tests for scripts/ai_agent_bridge/_config.py constants."""

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_cli_paths_are_correct_types(self):
        from lu_machinery.ai_agent_bridge._config import AGY_CLI, CLAUDE_CMD, GEMINI_CLI
        assert isinstance(CLAUDE_CMD, list)
        # #4875: prefers a native `claude` binary on PATH (single-element
        # command) and only falls back to the npx shim when none is found —
        # both are correct depending on the machine running the test.
        if CLAUDE_CMD[0] == "npx":
            assert "@anthropic-ai/claude-code" in CLAUDE_CMD[1]
        else:
            assert len(CLAUDE_CMD) == 1
            assert CLAUDE_CMD[0].endswith("claude")
        assert isinstance(AGY_CLI, str)
        assert isinstance(GEMINI_CLI, str)


class TestDb:
    """Tests for scripts/ai_agent_bridge/_db.py."""

    @pytest.fixture
    def tmp_db(self, tmp_path):
        """Patch DB_PATH to use a temp directory."""
        db_path = tmp_path / "test.db"
        with patch("lu_machinery.ai_agent_bridge._config.DB_PATH", db_path):
            yield db_path

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_init_db_creates_tables(self, tmp_db):
        from lu_machinery.ai_agent_bridge._db import init_db
        conn = init_db()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "messages" in tables
        assert "sessions" in tables
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_get_db_creates_if_not_exists(self, tmp_db):
        from lu_machinery.ai_agent_bridge._db import get_db
        assert not tmp_db.exists()
        conn = get_db()
        assert tmp_db.exists()
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_get_db_migration_adds_status(self, tmp_db):
        """If DB exists without status column, migration adds it."""
        from lu_machinery.ai_agent_bridge._db import get_db
        # Create a DB without status column
        tmp_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, from_llm TEXT NOT NULL, to_llm TEXT NOT NULL,
            message_type TEXT DEFAULT 'message', content TEXT NOT NULL,
            data TEXT, timestamp TEXT NOT NULL, acknowledged INTEGER DEFAULT 0
        )""")
        conn.commit()
        conn.close()

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(messages)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "status" in cols
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_set_session_and_get_session_claude(self, tmp_db):
        from lu_machinery.ai_agent_bridge._db import get_session, init_db, set_session
        init_db()
        set_session("task-1", "claude", "sess-abc123")
        result = get_session("task-1")
        assert result["claude"] == "sess-abc123"
        assert result["gemini"] is None

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_set_session_update_existing(self, tmp_db):
        from lu_machinery.ai_agent_bridge._db import get_session, init_db, set_session
        init_db()
        set_session("task-1", "claude", "old-session")
        set_session("task-1", "claude", "new-session")
        result = get_session("task-1")
        assert result["claude"] == "new-session"


class TestBroker:
    """Tests for scripts/ai_agent_bridge/_broker.py."""

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_validate_file_writes_with_unauthorized(self):
        from lu_machinery.ai_agent_bridge._broker import _validate_file_writes
        pre = {"M file.txt"}
        post = {"M file.txt", " M sneaky.py"}
        with patch("lu_machinery.ai_agent_bridge._broker._git_status_snapshot", return_value=post):
            violations = _validate_file_writes(pre, "allowed/file.txt")
        assert len(violations) == 1
        assert "unauthorized" in violations[0]

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_validate_file_writes_allowed_path(self):
        from lu_machinery.ai_agent_bridge._broker import _validate_file_writes
        from lu_machinery.ai_agent_bridge._config import REPO_ROOT
        pre = set()
        allowed = "scripts/test.py"
        post = {f" M {allowed}"}
        with patch("lu_machinery.ai_agent_bridge._broker._git_status_snapshot", return_value=post):
            violations = _validate_file_writes(pre, str(REPO_ROOT / allowed))
        assert violations == []

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_write_pid_file(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _write_pid_file
        with patch("lu_machinery.ai_agent_bridge._config.PID_DIR", tmp_path):
            _write_pid_file("gemini", "task-1", {"model": "flash"}, pid=12345)
        pid_file = tmp_path / "gemini-task-1.json"
        assert pid_file.exists()
        data = json.loads(pid_file.read_text())
        assert data["pid"] == 12345
        assert data["agent"] == "gemini"
        assert data["model"] == "flash"

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_remove_pid_file(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _remove_pid_file
        pid_file = tmp_path / "claude-task-1.json"
        pid_file.write_text("{}")
        with patch("lu_machinery.ai_agent_bridge._config.PID_DIR", tmp_path):
            _remove_pid_file("claude", "task-1")
        assert not pid_file.exists()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_is_task_locked_own_pid(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _is_task_locked
        pid_file = tmp_path / "gemini-task-1.json"
        pid_file.write_text(json.dumps({"pid": os.getpid(), "started": datetime.now(UTC).isoformat()}))
        with patch("lu_machinery.ai_agent_bridge._config.PID_DIR", tmp_path):
            assert _is_task_locked("gemini", "task-1") is False

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_is_task_locked_dead_process(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _is_task_locked
        pid_file = tmp_path / "gemini-task-1.json"
        pid_file.write_text(json.dumps({"pid": 99999999, "started": datetime.now(UTC).isoformat()}))
        with patch("lu_machinery.ai_agent_bridge._config.PID_DIR", tmp_path):
            result = _is_task_locked("gemini", "task-1")
        assert result is False
        assert not pid_file.exists()  # Cleaned up

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_stale_lock_no_started(self):
        from lu_machinery.ai_agent_bridge._broker import _check_stale_lock
        result = _check_stale_lock({}, 12345, Path("/tmp/fake.json"))
        assert result is True  # Assumes locked

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_stale_lock_young(self):
        from lu_machinery.ai_agent_bridge._broker import _check_stale_lock
        data = {"started": datetime.now(UTC).isoformat()}
        result = _check_stale_lock(data, 12345, Path("/tmp/fake.json"))
        assert result is True  # Young lock

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_stale_lock_old_python_process(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _check_stale_lock
        old_time = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
        pid_file = tmp_path / "test.json"
        pid_file.write_text("{}")
        mock_result = MagicMock()
        mock_result.stdout = "python3"
        with patch("subprocess.run", return_value=mock_result):
            result = _check_stale_lock({"started": old_time}, 12345, pid_file)
        assert result is True  # Our process, still locked

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_stale_lock_old_unknown_process(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _check_stale_lock
        old_time = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
        pid_file = tmp_path / "test.json"
        pid_file.write_text("{}")
        mock_result = MagicMock()
        mock_result.stdout = "chrome"
        with patch("subprocess.run", return_value=mock_result):
            result = _check_stale_lock({"started": old_time}, 12345, pid_file)
        assert result is False  # Stale, cleaned up

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_cleanup_ancient_messages_old_messages(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _cleanup_ancient_messages
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, task_id TEXT, from_llm TEXT, to_llm TEXT,
            data TEXT, timestamp TEXT, acknowledged INTEGER DEFAULT 0
        )""")
        old_ts = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        conn.execute("INSERT INTO messages (task_id, from_llm, to_llm, timestamp, acknowledged) VALUES (?, ?, ?, ?, 0)",
                     ("task-1", "claude", "gemini", old_ts))
        conn.commit()
        conn.close()
        with patch("lu_machinery.ai_agent_bridge._config.DB_PATH", db_path):
            result = _cleanup_ancient_messages("Cleaning", 24, False)
        assert result == 1

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_cleanup_ancient_messages_dry_run(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _cleanup_ancient_messages
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, task_id TEXT, from_llm TEXT, to_llm TEXT,
            data TEXT, timestamp TEXT, acknowledged INTEGER DEFAULT 0
        )""")
        old_ts = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        conn.execute("INSERT INTO messages (task_id, from_llm, to_llm, timestamp, acknowledged) VALUES (?, ?, ?, ?, 0)",
                     ("task-1", "claude", "gemini", old_ts))
        conn.commit()
        conn.close()
        with patch("lu_machinery.ai_agent_bridge._config.DB_PATH", db_path):
            result = _cleanup_ancient_messages("Would clean", 24, True)
        assert result == 1
        # Verify not actually updated
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT acknowledged FROM messages WHERE id=1").fetchone()
        assert row[0] == 0
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_broker_retention_cleanup_deletes_acknowledged_rows(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import broker_retention_cleanup
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, task_id TEXT, from_llm TEXT, to_llm TEXT,
                data TEXT, timestamp TEXT, acknowledged INTEGER DEFAULT 0
            );
            CREATE TABLE channel_messages (
                message_id TEXT PRIMARY KEY, created_at TEXT, parent_id TEXT
            );
            CREATE TABLE deliveries (
                delivery_id TEXT PRIMARY KEY, message_id TEXT, to_agent TEXT, to_model TEXT, status TEXT
            );
        """)
        old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
        conn.execute(
            "INSERT INTO messages (id, task_id, from_llm, to_llm, timestamp, acknowledged) VALUES (?, ?, ?, ?, ?, 1)",
            (1, "task-1", "claude", "gemini", old_ts),
        )
        conn.execute(
            "INSERT INTO messages (id, task_id, from_llm, to_llm, timestamp, acknowledged) VALUES (?, ?, ?, ?, ?, 0)",
            (2, "task-2", "claude", "gemini", old_ts),
        )
        conn.execute(
            "INSERT INTO channel_messages (message_id, created_at, parent_id) VALUES (?, ?, NULL)",
            ("m1", old_ts),
        )
        conn.execute(
            "INSERT INTO deliveries (delivery_id, message_id, status) VALUES (?, ?, ?)",
            ("d1", "m1", "delivered"),
        )
        conn.commit()
        conn.close()
        with patch("lu_machinery.ai_agent_bridge._config.DB_PATH", db_path):
            result = broker_retention_cleanup("30d", False)
        assert result == 3
        conn = sqlite3.connect(str(db_path))
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM channel_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_broker_retention_cleanup_dry_run(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import broker_retention_cleanup
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY, to_llm TEXT, data TEXT, timestamp TEXT, acknowledged INTEGER DEFAULT 0
        )""")
        old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
        conn.execute(
            "INSERT INTO messages (id, timestamp, acknowledged) VALUES (?, ?, 1)",
            (1, old_ts),
        )
        conn.commit()
        conn.close()
        with patch("lu_machinery.ai_agent_bridge._config.DB_PATH", db_path):
            result = broker_retention_cleanup("30d", True)
        assert result == 1
        conn = sqlite3.connect(str(db_path))
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_categorize_pid_files_dead(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _categorize_pid_files
        pf = tmp_path / "gemini-task-1.json"
        pf.write_text(json.dumps({"pid": 99999999}))
        _alive, stale = _categorize_pid_files([pf])
        assert len(_alive) == 0
        assert len(stale) == 1

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_categorize_pid_files_corrupt(self, tmp_path):
        from lu_machinery.ai_agent_bridge._broker import _categorize_pid_files
        pf = tmp_path / "bad.json"
        pf.write_text("not json")
        _alive, stale = _categorize_pid_files([pf])
        assert len(stale) == 1

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_git_status_snapshot_error(self):
        from lu_machinery.ai_agent_bridge._broker import _git_status_snapshot
        with patch("subprocess.run", side_effect=Exception("fail")):
            result = _git_status_snapshot()
        assert result == set()


class TestMessaging:
    """Tests for scripts/ai_agent_bridge/_messaging.py."""

    @pytest.fixture
    def msg_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLEET_COMMS_MESSAGE_PLANE", "shadow")
        db_path = tmp_path / "msg.db"
        # Create the DB file and schema
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT, from_llm TEXT NOT NULL, to_llm TEXT NOT NULL,
            message_type TEXT DEFAULT 'message', content TEXT NOT NULL,
            data TEXT, timestamp TEXT NOT NULL, acknowledged INTEGER DEFAULT 0,
            consumed_by_live_driver INTEGER DEFAULT 0, consumed_at TEXT,
            status TEXT DEFAULT 'pending'
        )""")
        conn.commit()
        conn.close()

        def _fresh_conn():
            from lu_machinery.ai_agent_bridge._db import register_kimi_row_functions

            c = sqlite3.connect(str(db_path))
            register_kimi_row_functions(c)  # as get_db does
            return c

        with patch("lu_machinery.ai_agent_bridge._messaging.get_db", side_effect=_fresh_conn):
            yield db_path

    def _query(self, db_path, sql, params=()):
        """Helper to query the test DB."""
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(sql, params).fetchone()
        conn.close()
        return row

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_send_message_basic(self, msg_db):
        from lu_machinery.ai_agent_bridge._messaging import send_message
        with patch("subprocess.run"):
            msg_id = send_message("hello", task_id="t1", quiet=True)
        assert msg_id is not None
        row = self._query(msg_db, "SELECT content, from_llm, to_llm FROM messages WHERE id=?", (msg_id,))
        assert row[0] == "hello"

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_send_message_self_addressed_auto_ack(self, msg_db):
        from lu_machinery.ai_agent_bridge._messaging import send_message
        with patch("subprocess.run"):
            msg_id = send_message("self", from_llm="claude", to_llm="claude", quiet=True)
        row = self._query(msg_db, "SELECT acknowledged FROM messages WHERE id=?", (msg_id,))
        assert row[0] == 1

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_send_message_with_json_data(self, msg_db):
        from lu_machinery.ai_agent_bridge._messaging import send_message
        with patch("subprocess.run"):
            msg_id = send_message("hi", data='{"key": "val"}', quiet=True)
        row = self._query(msg_db, "SELECT data FROM messages WHERE id=?", (msg_id,))
        parsed = json.loads(row[0])
        assert parsed["key"] == "val"


    def _insert(self, db_path, task_id, from_llm, to_llm, content, ts="2026-01-01T00:00:00"):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO messages (task_id, from_llm, to_llm, content, timestamp) VALUES (?,?,?,?,?)",
            (task_id, from_llm, to_llm, content, ts)
        )
        conn.commit()
        conn.close()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_acknowledge_single(self, msg_db):
        from lu_machinery.ai_agent_bridge._messaging import acknowledge
        self._insert(msg_db, "t1", "claude", "gemini", "hi")
        acknowledge(1, quiet=True)
        row = self._query(msg_db, "SELECT acknowledged FROM messages WHERE id=1")
        assert row[0] == 1


class TestModel:
    """Tests for scripts/ai_agent_bridge/_model.py."""

    @pytest.fixture(autouse=True)
    def supported_agy_version(self, monkeypatch):
        # Probe-result mocks must not also replace the adapter's version check.
        monkeypatch.setattr("lu_machinery.agent_runtime.adapters.agy._agy_version", lambda _binary: (1, 2, 9))
        import subprocess

        from lu_machinery.common.repo_root import _HOOK_ROOTS_SCRIPT, machinery_path

        self._real_run = subprocess.run
        self._root_command = ["/bin/bash", "-c", _HOOK_ROOTS_SCRIPT, "lu-hook-roots",
                              str(machinery_path("lib/project_interpreter.sh"))]

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_detect_model_error_not_found(self):
        from lu_machinery.ai_agent_bridge._model import _detect_model_error
        result = _detect_model_error("Error: model not found", "test-model")
        assert result is not None
        assert "not available" in result

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_model_cached(self, capsys):
        import time

        from lu_machinery.ai_agent_bridge._config import _MODEL_CACHE
        from lu_machinery.ai_agent_bridge._model import check_model
        _MODEL_CACHE["cached-model"] = (True, time.time())
        try:
            result = check_model("cached-model")
            assert result is True
            assert "cached" in capsys.readouterr().out
        finally:
            _MODEL_CACHE.pop("cached-model", None)

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_model_timeout(self, capsys, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        register_agy_hooks(tmp_path, home=home)
        monkeypatch.setattr("lu_machinery.ai_agent_bridge._config.REPO_ROOT", tmp_path)
        import subprocess

        from lu_machinery.ai_agent_bridge._config import _MODEL_CACHE
        from lu_machinery.ai_agent_bridge._model import check_model
        _MODEL_CACHE.pop("gemini-3.8-flash-high", None)
        def provider_probe(argv, **kwargs):
            if argv == self._root_command:
                return self._real_run(argv, **kwargs)
            raise subprocess.TimeoutExpired("cmd", 90)

        with patch("subprocess.run", side_effect=provider_probe):
            result = check_model("gemini-3.8-flash-high")
        assert result is False
        assert "timed out" in capsys.readouterr().out
        _MODEL_CACHE.pop("gemini-3.8-flash-high", None)

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_model_file_not_found(self, capsys, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        register_agy_hooks(tmp_path, home=home)
        monkeypatch.setattr("lu_machinery.ai_agent_bridge._config.REPO_ROOT", tmp_path)
        from lu_machinery.ai_agent_bridge._config import _MODEL_CACHE
        from lu_machinery.ai_agent_bridge._model import check_model
        _MODEL_CACHE.pop("gemini-3.8-flash-high", None)
        def provider_probe(argv, **kwargs):
            if argv == self._root_command:
                return self._real_run(argv, **kwargs)
            raise FileNotFoundError

        with patch("subprocess.run", side_effect=provider_probe):
            result = check_model("gemini-3.8-flash-high")
        assert result is False
        assert "AGY CLI not found" in capsys.readouterr().out
        _MODEL_CACHE.pop("gemini-3.8-flash-high", None)

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_model_unknown_slug(self, capsys):
        from lu_machinery.ai_agent_bridge._config import _MODEL_CACHE
        from lu_machinery.ai_agent_bridge._model import check_model
        _MODEL_CACHE.pop("not-a-real-model", None)
        result = check_model("not-a-real-model")
        assert result is False
        assert "not a recognized AGY model" in capsys.readouterr().out
        _MODEL_CACHE.pop("not-a-real-model", None)

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_check_model_success(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        register_agy_hooks(tmp_path, home=home)
        monkeypatch.setattr("lu_machinery.ai_agent_bridge._config.REPO_ROOT", tmp_path)
        from lu_machinery.ai_agent_bridge._config import _MODEL_CACHE
        from lu_machinery.ai_agent_bridge._model import check_model
        _MODEL_CACHE.pop("gemini-3.8-flash-high", None)
        mock_result = MagicMock(returncode=0, stdout="MODEL_OK", stderr="")
        def provider_probe(argv, **kwargs):
            if argv == self._root_command:
                return self._real_run(argv, **kwargs)
            return mock_result

        with patch("subprocess.run", side_effect=provider_probe) as run:
            assert check_model("gemini-3.8-flash-high") is True
        cmd = run.call_args.args[0]
        assert "agy" in cmd[0] or cmd[0].endswith("/agy")
        assert "--model" in cmd
        # Active 3.8 slug is preserved in the probe invocation.
        assert "gemini-3.8-flash-high" in cmd or "Gemini 3.8 Flash (High)" in cmd
        _MODEL_CACHE.pop("gemini-3.8-flash-high", None)


class TestGitHub:
    """Tests for scripts/ai_agent_bridge/_github.py."""

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_gh_comment_refuses_missing_matcher_before_transport(self, tmp_path, monkeypatch, capsys):
        from lu_machinery.ai_agent_bridge._github import _gh_comment
        from lu_machinery.opsec import prepublish

        monkeypatch.setattr(prepublish, "private_tooling", lambda: tmp_path / "missing-tooling")
        with patch("subprocess.run") as transport:
            assert _gh_comment(123, "body") is False
        transport.assert_not_called()
        assert "private matcher or rules unavailable/incompatible; write refused" in capsys.readouterr().out

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_split_content_no_newline(self):
        from lu_machinery.ai_agent_bridge._github import _split_content
        content = "a" * 20
        result = _split_content(content, limit=10)
        assert len(result) == 2

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_gh_comment_success(self):
        from lu_machinery.ai_agent_bridge._github import _gh_comment
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            assert _gh_comment(123, "body") is True

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_gh_comment_failure(self, capsys):
        from lu_machinery.ai_agent_bridge._github import _gh_comment
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "auth error"
        with patch("subprocess.run", return_value=mock_result):
            assert _gh_comment(123, "body") is False

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_post_review_to_github_existing_issue(self):
        from lu_machinery.ai_agent_bridge._github import _post_review_to_github
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = _post_review_to_github("issue-42", "review text", "flash")
        assert result == 42

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_post_review_to_github_no_issue_skips(self):
        """When task_id doesn't map to an issue, don't create a new one (#970)."""
        from lu_machinery.ai_agent_bridge._github import _post_review_to_github
        # No subprocess should be called — we skip GH posting entirely
        with patch("subprocess.run") as mock_run:
            result = _post_review_to_github("random-task", "review text", "opus")
        assert result is None
        mock_run.assert_not_called()

    @pytest.mark.usefixtures('configured_roots', '_synthetic_publishing_rules')
    def test_post_review_to_github_gh_not_found(self):
        from lu_machinery.ai_agent_bridge._github import _post_review_to_github
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _post_review_to_github("issue-1", "text", "model")
        assert result is None

pytestmark = [pytest.mark.usefixtures('configured_roots', 'bridge_wake_dir')]
