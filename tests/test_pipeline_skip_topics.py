"""Tests for the skip_topics flag on run_roundup."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bebop_bot import pipeline


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(
        self, chat_id, text, parse_mode=None,
        disable_web_page_preview=None, reply_markup=None,
    ):
        self.sent.append({"chat_id": chat_id, "text": text})


def _fake_db(topics_called_marker: list):
    db = SimpleNamespace()
    db.is_paused = AsyncMock(return_value=False)
    db.get_allowlist = AsyncMock(return_value=set())
    db.get_setting = AsyncMock(side_effect=lambda k, d=None: d)
    db.get_recent_feedback = AsyncMock(return_value=[])
    db.set_setting = AsyncMock()
    db.update_topic_since_id = AsyncMock()

    async def _get_topics():
        topics_called_marker.append(True)
        return []

    db.get_topics = _get_topics
    return db


async def test_skip_topics_true_skips_get_topics_and_does_not_advance_since_id():
    topics_called: list = []
    db = _fake_db(topics_called)
    x = SimpleNamespace(search_recent=AsyncMock(return_value=[]))
    claude = SimpleNamespace()
    bot = FakeBot()

    with patch.object(pipeline.emerging_mod, "run_emerging", new=AsyncMock(return_value={
        "cycle_ts": None, "tokens": [], "sectors": [], "venues": [],
        "mechanisms": [], "convergence_events": [], "new_dict_terms": [],
        "venue_suggestions": [], "cooc_graph": {},
    })) as mock_emerging:
        result = await pipeline.run_roundup(
            db=db, x=x, claude=claude, bot=bot, chat_id=42,
            advance_since_id=False, force=True,
            manual_scan=True, skip_topics=True,
        )

    # Emerging was called once.
    assert mock_emerging.await_count == 1
    # No topics were iterated.
    assert topics_called == []
    # Result has no topic entries.
    assert result == {}
    # Since-id was not touched.
    db.update_topic_since_id.assert_not_awaited()
    # X.search_recent was not called for topic fetching.
    x.search_recent.assert_not_awaited()


async def test_skip_topics_false_runs_get_topics():
    topics_called: list = []
    db = _fake_db(topics_called)
    x = SimpleNamespace(search_recent=AsyncMock(return_value=[]))
    claude = SimpleNamespace()
    bot = FakeBot()

    with patch.object(pipeline.emerging_mod, "run_emerging", new=AsyncMock(return_value={
        "cycle_ts": None, "tokens": [], "sectors": [], "venues": [],
        "mechanisms": [], "convergence_events": [], "new_dict_terms": [],
        "venue_suggestions": [], "cooc_graph": {},
    })):
        await pipeline.run_roundup(
            db=db, x=x, claude=claude, bot=bot, chat_id=42,
            advance_since_id=True, force=False,
            manual_scan=False, skip_topics=False,
        )

    assert topics_called == [True]


async def test_skip_topics_true_still_sends_header():
    topics_called: list = []
    db = _fake_db(topics_called)
    x = SimpleNamespace(search_recent=AsyncMock(return_value=[]))
    claude = SimpleNamespace()
    bot = FakeBot()

    with patch.object(pipeline.emerging_mod, "run_emerging", new=AsyncMock(return_value={
        "cycle_ts": None, "tokens": [], "sectors": [], "venues": [],
        "mechanisms": [], "convergence_events": [], "new_dict_terms": [],
        "venue_suggestions": [], "cooc_graph": {},
    })):
        await pipeline.run_roundup(
            db=db, x=x, claude=claude, bot=bot, chat_id=42,
            advance_since_id=False, force=True,
            manual_scan=True, skip_topics=True,
        )

    # Empty emerging + empty results = only the "No posts cleared" message.
    assert bot.sent, "expected at least one outgoing message"
    text = bot.sent[0]["text"]
    assert "Manual scan" in text


async def test_skip_topics_true_with_emerging_results_renders_only_header_no_topic_line():
    """If emerging has results but topics are skipped, header should not say
    '0 topics, 0 posts surfaced'."""
    topics_called: list = []
    db = _fake_db(topics_called)
    x = SimpleNamespace(search_recent=AsyncMock(return_value=[]))
    claude = SimpleNamespace()
    bot = FakeBot()

    with patch.object(pipeline.emerging_mod, "run_emerging", new=AsyncMock(return_value={
        "cycle_ts": None, "tokens": [], "sectors": [], "venues": [],
        "mechanisms": [],
        "convergence_events": [{
            "type": "token", "term": "APYX", "tier": "convergence",
            "signal_count": 3, "claude_confidence": None,
            "claude_rationale": None, "summary": "Test summary",
            "top_tweet_url": None,
        }],
        "new_dict_terms": [], "venue_suggestions": [], "cooc_graph": {},
    })):
        await pipeline.run_roundup(
            db=db, x=x, claude=claude, bot=bot, chat_id=42,
            advance_since_id=False, force=True,
            manual_scan=True, skip_topics=True,
        )

    assert bot.sent
    header = bot.sent[0]["text"]
    assert "Manual scan" in header
    assert "0 topics" not in header
    assert "0 posts surfaced" not in header
