"""Tests for the top tweet link rendering in digest cards."""
from bebop_bot.digest import send_convergence_block, send_emerging_track
from bebop_bot.emerging import EmergingEntity, EmergingToken
from bebop_bot.scoring import EntityScore


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(
        self, chat_id, text, parse_mode=None,
        disable_web_page_preview=None, reply_markup=None,
    ):
        self.sent.append({"text": text})


def _score(c: float = 3.0) -> EntityScore:
    return EntityScore(narrowness=3.0, quality=3.0, momentum=3.0, coherence=1.0, composite=c)


async def test_emerging_track_renders_top_tweet_link():
    bot = FakeBot()
    t = EmergingToken(
        token="APYX", chain="evm", score=_score(),
        unique_authors_24h=4, weighted_24h=4.0, raw_24h=8,
        top_tweet_url="https://x.com/u/status/123",
    )
    await send_emerging_track(bot, 1, "Emerging tokens", [t], cooc_graph={})
    assert bot.sent
    body = bot.sent[0]["text"]
    assert "top tweet" in body
    assert "https://x.com/u/status/123" in body


async def test_emerging_track_omits_link_when_none():
    bot = FakeBot()
    e = EmergingEntity(
        entity_type="sector", term="restaking", score=_score(),
        unique_authors_24h=4, weighted_24h=4.0, raw_24h=8,
        top_tweet_url=None,
    )
    await send_emerging_track(bot, 1, "Emerging sectors", [e], cooc_graph={})
    body = bot.sent[0]["text"]
    assert "top tweet" not in body


async def test_convergence_block_renders_top_tweet_link_for_strong():
    bot = FakeBot()
    events = [{
        "type": "token", "term": "APYX", "tier": "strong_convergence",
        "signal_count": 5, "claude_confidence": 4,
        "claude_rationale": "looks viral",
        "summary": "STRONG: token:APYX",
        "top_tweet_url": "https://x.com/big/status/999",
    }]
    await send_convergence_block(bot, 1, events)
    assert bot.sent
    body = bot.sent[0]["text"]
    assert "top tweet" in body
    assert "https://x.com/big/status/999" in body


async def test_convergence_block_renders_top_tweet_link_for_normal():
    bot = FakeBot()
    events = [{
        "type": "sector", "term": "restaking", "tier": "convergence",
        "signal_count": 3, "claude_confidence": None,
        "claude_rationale": None,
        "summary": "convergence: sector:restaking",
        "top_tweet_url": "https://x.com/u/status/111",
    }]
    await send_convergence_block(bot, 1, events)
    body = bot.sent[0]["text"]
    assert "top tweet" in body
    assert "https://x.com/u/status/111" in body


async def test_convergence_block_omits_link_when_none():
    bot = FakeBot()
    events = [{
        "type": "sector", "term": "amm", "tier": "convergence",
        "signal_count": 3, "claude_confidence": None,
        "claude_rationale": None,
        "summary": "...",
        "top_tweet_url": None,
    }]
    await send_convergence_block(bot, 1, events)
    body = bot.sent[0]["text"]
    assert "top tweet" not in body
