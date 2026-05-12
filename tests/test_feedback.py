"""Tests for Phase 3 callback parsing, idempotency, label flips,
suggestion threshold, and pending_feedback expiry."""
import pytest

from bebop_bot.db import Db, init_db
from bebop_bot.feedback import _parse_callback
from bebop_bot.suggestions import maybe_suggest_allowlist


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def test_parse_callback_valid():
    assert _parse_callback("u:12345") == ("u", "12345")
    assert _parse_callback("d:abc") == ("d", "abc")
    assert _parse_callback("m:xyz") == ("m", "xyz")
    assert _parse_callback("s:y:somebody") == ("s", "y:somebody")


def test_parse_callback_invalid():
    assert _parse_callback(None) is None
    assert _parse_callback("") is None
    assert _parse_callback("nocolon") is None
    assert _parse_callback("u:") is None


async def test_pending_feedback_roundtrip(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        await db.upsert_pending_feedback(
            tweet_id="t1",
            author_handle="alice",
            tweet_text="hello",
            topic_name="amms",
            metrics_json='{"likes":1}',
        )
        got = await db.get_pending_feedback("t1")
        assert got is not None
        assert got["author_handle"] == "alice"
        assert got["tweet_text"] == "hello"
        assert got["topic_name"] == "amms"
        assert got["metrics_json"] == '{"likes":1}'

        missing = await db.get_pending_feedback("nope")
        assert missing is None
    finally:
        await conn.close()


async def test_label_flip_arithmetic(db_path):
    """up -> down should decrement ups and increment downs."""
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        # First up.
        await db.upsert_feedback(
            tweet_id="t1",
            topic_name="t",
            author_handle="alice",
            label="up",
            tweet_text="x",
        )
        await db.adjust_author_score("alice", None, "up")
        ups, downs = await db.get_recent_author_score("alice", 60)
        assert ups == 1 and downs == 0

        # Flip same tweet to down.
        await db.upsert_feedback(
            tweet_id="t1",
            topic_name="t",
            author_handle="alice",
            label="down",
            tweet_text="x",
        )
        await db.adjust_author_score("alice", "up", "down")
        ups, downs = await db.get_recent_author_score("alice", 60)
        # feedback table aggregation: single row label='down'
        assert ups == 0 and downs == 1
    finally:
        await conn.close()


async def test_idempotent_label_does_not_change_counts(db_path):
    """Re-applying same label leaves counts unchanged (caller skips DB write)."""
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        await db.upsert_feedback(
            tweet_id="t1",
            topic_name="t",
            author_handle="bob",
            label="up",
            tweet_text="x",
        )
        await db.adjust_author_score("bob", None, "up")
        ups1, downs1 = await db.get_recent_author_score("bob", 60)

        # Real handler returns early when prior_label == new_label, so we don't
        # call upsert/adjust again. Simulate by reading counts.
        ups2, downs2 = await db.get_recent_author_score("bob", 60)
        assert (ups1, downs1) == (ups2, downs2) == (1, 0)
    finally:
        await conn.close()


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        })


async def test_suggestion_threshold_fires_at_3_ups(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        bot = FakeBot()
        for i in range(3):
            await db.upsert_feedback(
                tweet_id=f"t{i}",
                topic_name="t",
                author_handle="carol",
                label="up",
                tweet_text="x",
            )
        ups, downs = await db.get_recent_author_score("carol", 60)
        assert (ups, downs) == (3, 0)

        triggered = await maybe_suggest_allowlist(
            bot=bot, chat_id=1, db=db, handle="carol",
        )
        assert triggered is True
        assert len(bot.sent) == 1
        assert "@carol" in bot.sent[0]["text"]
    finally:
        await conn.close()


async def test_suggestion_does_not_fire_below_threshold(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        bot = FakeBot()
        for i in range(2):
            await db.upsert_feedback(
                tweet_id=f"t{i}",
                topic_name="t",
                author_handle="dave",
                label="up",
                tweet_text="x",
            )
        triggered = await maybe_suggest_allowlist(
            bot=bot, chat_id=1, db=db, handle="dave",
        )
        assert triggered is False
        assert bot.sent == []
    finally:
        await conn.close()


async def test_suggestion_does_not_fire_with_any_downvote(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        bot = FakeBot()
        for i in range(3):
            await db.upsert_feedback(
                tweet_id=f"t{i}",
                topic_name="t",
                author_handle="eve",
                label="up",
                tweet_text="x",
            )
        await db.upsert_feedback(
            tweet_id="td",
            topic_name="t",
            author_handle="eve",
            label="down",
            tweet_text="x",
        )
        triggered = await maybe_suggest_allowlist(
            bot=bot, chat_id=1, db=db, handle="eve",
        )
        assert triggered is False
    finally:
        await conn.close()


async def test_suggestion_skipped_when_blocked(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        bot = FakeBot()
        await db.add_suggestion_block("frank")
        for i in range(5):
            await db.upsert_feedback(
                tweet_id=f"t{i}",
                topic_name="t",
                author_handle="frank",
                label="up",
                tweet_text="x",
            )
        triggered = await maybe_suggest_allowlist(
            bot=bot, chat_id=1, db=db, handle="frank",
        )
        assert triggered is False
    finally:
        await conn.close()


async def test_suggestion_skipped_when_already_allowlisted(db_path):
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        bot = FakeBot()
        await db.add_to_allowlist("grace")
        for i in range(3):
            await db.upsert_feedback(
                tweet_id=f"t{i}",
                topic_name="t",
                author_handle="grace",
                label="up",
                tweet_text="x",
            )
        triggered = await maybe_suggest_allowlist(
            bot=bot, chat_id=1, db=db, handle="grace",
        )
        assert triggered is False
    finally:
        await conn.close()


async def test_pending_feedback_expiry_returns_none(db_path):
    """If pending_feedback row is missing, get returns None (simulates expiry)."""
    conn = await init_db(db_path)
    try:
        db = Db(conn)
        assert await db.get_pending_feedback("never_inserted") is None
    finally:
        await conn.close()


async def test_set_and_clear_author_muted(db_path):
    from datetime import UTC, datetime, timedelta

    conn = await init_db(db_path)
    try:
        db = Db(conn)
        until = datetime.now(UTC) + timedelta(days=30)
        await db.set_author_muted("harry", until)
        assert await db.get_muted_until("harry") is not None
        changed = await db.clear_author_muted("harry")
        assert changed is True
        assert await db.get_muted_until("harry") is None
    finally:
        await conn.close()
