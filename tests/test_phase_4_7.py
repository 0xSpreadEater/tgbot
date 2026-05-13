"""Phase 4.7 tests: three-tier convergence + Claude-proposed patterns."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bebop_bot import db as dbm
from bebop_bot import patterns as patterns_mod
from bebop_bot.claude_client import ClaudeClient

# ---------------------------------------------------------------------------
# DB migration
# ---------------------------------------------------------------------------


async def _open_db(tmp_path):
    db_path = tmp_path / "test.sqlite"
    return await dbm.init_db(str(db_path))


async def test_phase_4_7_migration_creates_tables_and_settings(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('claude_proposed_patterns','pattern_observations')"
        ) as cur:
            rows = await cur.fetchall()
        names = {r["name"] for r in rows}
        assert "claude_proposed_patterns" in names
        assert "pattern_observations" in names

        for key, expected in (
            ("convergence_weak_threshold", "2"),
            ("convergence_medium_threshold", "3"),
            ("convergence_strong_claude_min", "4"),
            ("convergence_weak_cap_per_cycle", "15"),
            ("convergence_medium_cap_per_cycle", "10"),
            ("convergence_strong_cap_per_cycle", "5"),
            ("pattern_proposals_per_cycle", "3"),
            ("pattern_of_the_week_lookback_days", "7"),
        ):
            val = await dbm.get_setting(conn, key)
            assert val == expected, f"setting {key} expected {expected}, got {val}"
    finally:
        await conn.close()


async def test_phase_4_7_migration_renames_legacy_tiers(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        # Seed legacy tier rows directly, then re-run the migration.
        ts = datetime.now(UTC).isoformat()
        await conn.execute(
            "INSERT INTO convergence_events("
            "cycle_ts, entity_type, entity_term, tier, signal_count, summary) "
            "VALUES(?, 'token', 'X', 'convergence', 3, 'legacy m')",
            (ts,),
        )
        await conn.execute(
            "INSERT INTO convergence_events("
            "cycle_ts, entity_type, entity_term, tier, signal_count, summary) "
            "VALUES(?, 'token', 'Y', 'strong_convergence', 4, 'legacy s')",
            (ts,),
        )
        await conn.commit()
        await dbm.apply_phase_4_7_migrations(conn)
        async with conn.execute(
            "SELECT tier FROM convergence_events WHERE entity_term = 'X'"
        ) as cur:
            row = await cur.fetchone()
        assert row["tier"] == "medium"
        async with conn.execute(
            "SELECT tier FROM convergence_events WHERE entity_term = 'Y'"
        ) as cur:
            row = await cur.fetchone()
        assert row["tier"] == "strong"

        # Idempotent: running again is safe.
        await dbm.apply_phase_4_7_migrations(conn)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Pattern persistence + curation lifecycle
# ---------------------------------------------------------------------------


async def test_pattern_insert_then_bump_increments_propose_count(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)
        pid = await db.insert_pattern(
            name="VC-portfolio co-rally",
            description="Tokens pump within same VC's portfolio.",
            cycle_ts=ts,
            confidence=3,
            supporting_tweet_ids=["1", "2"],
            anchor_entities=[("token", "ABC"), ("token", "DEF")],
        )
        assert pid > 0

        # Case-insensitive lookup.
        found = await db.find_pattern_by_name("vc-portfolio co-rally")
        assert found == pid

        await db.bump_pattern(
            pid, ts + timedelta(hours=1),
            confidence=4,
            supporting_tweet_ids=["3"],
            anchor_entities=[("token", "XYZ")],
        )
        p = await db.get_pattern_by_id(pid)
        assert p["propose_count"] == 2

        obs = await db.get_pattern_observations(pid)
        assert len(obs) == 2
    finally:
        await conn.close()


async def test_pattern_label_up_and_down_hides_from_few_shot(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)
        pid_up = await db.insert_pattern(
            name="Pattern up", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        pid_down = await db.insert_pattern(
            name="Pattern down", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        await db.update_pattern_label(pid_up, "up")
        await db.set_pattern_weight(pid_up, 2.0)
        await db.update_pattern_label(pid_down, "down")

        few_shot = await db.get_patterns_for_few_shot(limit=10)
        names = {p["name"] for p in few_shot}
        assert "Pattern up" in names
        assert "Pattern down" not in names

        hidden = await db.get_patterns_hidden()
        assert any(p["name"] == "Pattern down" for p in hidden)
    finally:
        await conn.close()


async def test_pattern_housekeep_bumps_weight_after_three_proposes(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)
        pid = await db.insert_pattern(
            name="Sticky pattern", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        for _ in range(2):
            await db.bump_pattern(
                pid, ts, confidence=3,
                supporting_tweet_ids=[], anchor_entities=[],
            )
        result = await db.housekeep_patterns()
        assert result["bumped"] == 1
        p = await db.get_pattern_by_id(pid)
        assert p["weight"] == pytest.approx(1.5)
    finally:
        await conn.close()


async def test_pattern_housekeep_ages_out_stale_unlabelled(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)
        pid_stale = await db.insert_pattern(
            name="Stale pattern", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        pid_up = await db.insert_pattern(
            name="Up-voted stale", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        await db.update_pattern_label(pid_up, "up")
        # Push both timestamps back >30 days.
        old_ts = (ts - timedelta(days=45)).isoformat()
        await conn.execute(
            "UPDATE claude_proposed_patterns SET last_proposed_at = ?",
            (old_ts,),
        )
        await conn.commit()

        result = await db.housekeep_patterns()
        assert result["deleted"] == 1
        assert await db.get_pattern_by_id(pid_stale) is None
        assert await db.get_pattern_by_id(pid_up) is not None
    finally:
        await conn.close()


async def test_pattern_of_week_requires_clear_winner(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)
        pid_a = await db.insert_pattern(
            name="A", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        pid_b = await db.insert_pattern(
            name="B", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )
        # Bump A four times, B three times => margin 1 (no clear winner).
        for _ in range(3):
            await db.bump_pattern(
                pid_a, ts, confidence=3,
                supporting_tweet_ids=[], anchor_entities=[],
            )
        for _ in range(2):
            await db.bump_pattern(
                pid_b, ts, confidence=3,
                supporting_tweet_ids=[], anchor_entities=[],
            )
        assert await db.pattern_of_week_winner(7) is None

        # Push A's lead to 2.
        await db.bump_pattern(
            pid_a, ts, confidence=3,
            supporting_tweet_ids=[], anchor_entities=[],
        )
        winner = await db.pattern_of_week_winner(7)
        assert winner is not None
        assert winner["name"] == "A"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Patterns module orchestration
# ---------------------------------------------------------------------------


async def test_propose_patterns_dedups_by_name_case_insensitive(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)

        # Pre-seed an existing pattern.
        await db.insert_pattern(
            name="VC co-rally", description="exists", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )

        # Stub a Claude that proposes the same name with different case.
        async def _propose(sample, existing_patterns, cap):
            return [
                {
                    "name": "vc co-rally",
                    "description": "re-proposed",
                    "confidence": 4,
                    "tweet_ids": ["t1"],
                    "anchors": [("token", "ABC")],
                },
                {
                    "name": "Korean Twitter early adoption",
                    "description": "Korean handles precede EN waves.",
                    "confidence": 3,
                    "tweet_ids": ["t2"],
                    "anchors": [("sector", "kpop")],
                },
            ]

        claude = SimpleNamespace(propose_patterns=_propose)
        sweep = [SimpleNamespace(text="$ABC up", id="t1", author_handle="a",
                                  like_count=5)]
        proposals = await patterns_mod.propose_patterns(
            db=db, claude=claude, cycle_ts=ts, sweep_pool=sweep,
            all_entities=[("token", "ABC")],
            sector_dict=[], venue_dict=[], mechanism_dict=[],
        )
        # Both are returned; the first should bump, second should insert.
        assert len(proposals) == 2
        names = [p["name"] for p in proposals]
        assert "vc co-rally" in names[0].lower()
        # is_new flag: first should be False (bumped existing), second True.
        first = next(p for p in proposals if p["name"].lower() == "vc co-rally")
        second = next(
            p for p in proposals
            if p["name"] == "Korean Twitter early adoption"
        )
        assert first["is_new"] is False
        assert second["is_new"] is True

        # Existing pattern's propose_count should now be 2.
        existing = await db.get_pattern_by_name("VC co-rally")
        assert existing["propose_count"] == 2
    finally:
        await conn.close()


async def test_propose_patterns_handles_claude_failure_gracefully(tmp_path):
    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        claude = SimpleNamespace(
            propose_patterns=AsyncMock(side_effect=RuntimeError("boom")),
        )
        out = await patterns_mod.propose_patterns(
            db=db, claude=claude, cycle_ts=datetime.now(UTC),
            sweep_pool=[SimpleNamespace(text="x", id="1", author_handle="a")],
            all_entities=[],
        )
        assert out == []
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# ClaudeClient.propose_patterns JSON parsing
# ---------------------------------------------------------------------------


class _FakeClaudeClient(ClaudeClient):
    def __init__(self, raw_response: str):
        self.model = "test-model"
        self._raw = raw_response

        class _Resp:
            def __init__(self, raw):
                self.content = [SimpleNamespace(type="text", text=raw)]

        class _Messages:
            def __init__(self, raw):
                self._raw = raw

            async def create(self, **kwargs):
                return _Resp(self._raw)

        class _ClientStub:
            def __init__(self, raw):
                self.messages = _Messages(raw)

            async def close(self):
                pass

        self._client = _ClientStub(raw_response)


async def test_claude_propose_patterns_parses_valid_json():
    raw = json.dumps({
        "patterns": [
            {
                "name": "Test pattern",
                "description": "A clear novel pattern observation.",
                "confidence": 4,
                "tweet_indices": [1, 2],
                "anchors": [{"type": "token", "term": "ABC"}],
            }
        ]
    })
    client = _FakeClaudeClient(raw)
    sample = [
        SimpleNamespace(text="a", id="t1", author_handle="x"),
        SimpleNamespace(text="b", id="t2", author_handle="y"),
    ]
    out = await client.propose_patterns(
        sample=sample, existing_patterns=[], cap=3,
    )
    assert len(out) == 1
    assert out[0]["name"] == "Test pattern"
    assert out[0]["confidence"] == 4
    assert out[0]["tweet_ids"] == ["t1", "t2"]
    assert out[0]["anchors"] == [("token", "ABC")]


async def test_claude_propose_patterns_returns_empty_on_malformed_json():
    client = _FakeClaudeClient("not json at all")
    sample = [SimpleNamespace(text="a", id="t1", author_handle="x")]
    out = await client.propose_patterns(
        sample=sample, existing_patterns=[], cap=3,
    )
    assert out == []


async def test_claude_propose_patterns_respects_cap():
    raw = json.dumps({
        "patterns": [
            {"name": f"P{i}", "description": "desc", "confidence": 2,
             "tweet_indices": [], "anchors": []}
            for i in range(10)
        ]
    })
    client = _FakeClaudeClient(raw)
    sample = [SimpleNamespace(text="a", id="t1", author_handle="x")]
    out = await client.propose_patterns(
        sample=sample, existing_patterns=[], cap=3,
    )
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Three-tier convergence bucketing (the algorithm lives in emerging.py)
# ---------------------------------------------------------------------------


def _bucket(n_cats: int, weak_thr: int, med_thr: int) -> str | None:
    """Mirror of the logic in emerging.run_emerging's tier bucketing."""
    if n_cats < weak_thr:
        return None
    if n_cats >= med_thr:
        return "medium"
    return "weak"


def test_three_tier_bucketing_assignments():
    weak, med = 2, 3
    assert _bucket(1, weak, med) is None
    assert _bucket(2, weak, med) == "weak"
    assert _bucket(3, weak, med) == "medium"
    # N=5 entity falls into medium (and is a strong-tier *candidate*),
    # never also into weak.
    assert _bucket(5, weak, med) == "medium"


# ---------------------------------------------------------------------------
# Convergence threshold command validation
# ---------------------------------------------------------------------------


async def test_cmd_convergence_threshold_rejects_weak_at_or_above_medium(tmp_path):
    from bebop_bot.handlers import cmd_convergence_threshold

    conn = await _open_db(tmp_path)
    try:
        # Fake context wrapping the connection.
        class _App:
            def __init__(self, conn):
                self.bot_data = {"db": conn}

        class _Ctx:
            def __init__(self, conn, args):
                self.application = _App(conn)
                self.args = args

        replies: list[str] = []

        class _Msg:
            async def reply_text(self, text, parse_mode=None):
                replies.append(text)

        class _Update:
            effective_message = _Msg()
            effective_user = SimpleNamespace(id=0)

        # Pre-set authorized user via env? auth.restricted may block.
        # Bypass by calling .__wrapped__ if present, else call directly.
        fn = getattr(cmd_convergence_threshold, "__wrapped__", cmd_convergence_threshold)

        # weak 5 should be rejected because default medium=3.
        await fn(_Update(), _Ctx(conn, ["weak", "5"]))
        assert any("Rejected" in r for r in replies)

        # weak 1 should succeed.
        replies.clear()
        await fn(_Update(), _Ctx(conn, ["weak", "1"]))
        val = await dbm.get_setting(conn, "convergence_weak_threshold")
        assert val == "1"

        # medium 4 should succeed.
        await fn(_Update(), _Ctx(conn, ["medium", "4"]))
        val = await dbm.get_setting(conn, "convergence_medium_threshold")
        assert val == "4"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Convergence callback routing (legacy 'cv:structural:*' -> medium)
# ---------------------------------------------------------------------------


async def test_on_convergence_callback_legacy_structural_routes_to_medium():
    from bebop_bot.handlers import on_convergence_callback

    captured: dict = {}

    class _Query:
        def __init__(self, data):
            self.data = data
            self.message = None

        async def answer(self, text=None):
            captured["answered"] = text

    class _Update:
        def __init__(self, data):
            self.callback_query = _Query(data)

    ctx = SimpleNamespace(application=SimpleNamespace(bot_data={}))
    await on_convergence_callback(_Update("cv:structural:token:ABC:up"), ctx)
    assert captured["answered"] == "Marked as insight."

    await on_convergence_callback(_Update("cv:weak:token:ABC:down"), ctx)
    assert captured["answered"] == "Marked as noise."


# ---------------------------------------------------------------------------
# Patterns callback routing
# ---------------------------------------------------------------------------


async def test_on_pattern_callback_up_sets_weight_and_label(tmp_path):
    from bebop_bot.handlers import on_pattern_callback

    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)
        ts = datetime.now(UTC)
        await db.insert_pattern(
            name="ZZZ", description="d", cycle_ts=ts,
            confidence=3, supporting_tweet_ids=[], anchor_entities=[],
        )

        class _Msg:
            async def reply_text(self, text, parse_mode=None,
                                  disable_web_page_preview=None):
                pass

        class _Query:
            def __init__(self, data):
                self.data = data
                self.message = _Msg()

            async def answer(self, text=None):
                self.answered = text

        class _Update:
            def __init__(self, data):
                self.callback_query = _Query(data)

        ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data={"db": conn, "db_wrapper": db}),
        )

        await on_pattern_callback(_Update("pat:up:ZZZ"), ctx)
        p = await db.get_pattern_by_name("ZZZ")
        assert p["user_label"] == "up"
        assert p["weight"] == pytest.approx(2.0)

        await on_pattern_callback(_Update("pat:down:ZZZ"), ctx)
        p = await db.get_pattern_by_name("ZZZ")
        assert p["user_label"] == "down"
    finally:
        await conn.close()


async def test_on_pattern_callback_unknown_name_answers_gracefully(tmp_path):
    from bebop_bot.handlers import on_pattern_callback

    conn = await _open_db(tmp_path)
    try:
        db = dbm.Db(conn)

        captured: dict = {}

        class _Query:
            def __init__(self, data):
                self.data = data
                self.message = None

            async def answer(self, text=None):
                captured["answered"] = text

        class _Update:
            def __init__(self, data):
                self.callback_query = _Query(data)

        ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data={"db": conn, "db_wrapper": db}),
        )
        await on_pattern_callback(_Update("pat:up:nonexistent"), ctx)
        assert "not found" in (captured.get("answered") or "").lower()
    finally:
        await conn.close()
