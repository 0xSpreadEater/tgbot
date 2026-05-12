"""Tests for top_tweet_url propagation in emerging detection."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bebop_bot.emerging import (
    EmergingEntity,
    EmergingToken,
    _select_top_tweet_fallback_by_likes,
    _select_top_tweet_for_entity,
    _select_top_tweet_for_token,
    _tweet_url,
    detect_entities,
    detect_tokens,
)


def _tweet(
    tid: str = "1",
    handle: str = "alice",
    text: str = "$APYX rules",
    like_count: int = 0,
    reply_count: int = 0,
    retweet_count: int = 0,
    author_verified: bool = False,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tid,
        author_handle=handle,
        text=text,
        like_count=like_count,
        reply_count=reply_count,
        retweet_count=retweet_count,
        author_verified=author_verified,
        created_at=created_at or datetime.now(UTC),
        url=f"https://x.com/{handle}/status/{tid}",
    )


def _fake_db(viral_handles=None):
    db = SimpleNamespace()
    db.get_setting = AsyncMock(side_effect=lambda k, d=None: d)
    db.get_setting_bool = AsyncMock(return_value=True)
    db.get_allowlist = AsyncMock(return_value=set())
    db.get_viral_handles = AsyncMock(return_value=set(viral_handles or set()))
    db.insert_entity_mention = AsyncMock()
    db.insert_convergence_signal = AsyncMock()
    db.insert_convergence_event = AsyncMock(return_value=1)
    db.get_entity_first_seen = AsyncMock(return_value=None)

    class _Cur:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def fetchone(self):
            return None

    def _exec(sql, *params):
        return _Cur()

    db.conn = SimpleNamespace(execute=_exec)
    return db


# ---------------------------------------------------------------------------
# Top-tweet pickers
# ---------------------------------------------------------------------------

def test_tweet_url_falls_back_to_handle_and_id():
    t = SimpleNamespace(author_handle="alice", id="123")
    assert _tweet_url(t) == "https://x.com/alice/status/123"


def test_tweet_url_uses_existing_url():
    t = SimpleNamespace(url="https://example.com/x", author_handle="a", id="1")
    assert _tweet_url(t) == "https://example.com/x"


def test_select_top_tweet_for_entity_uses_score():
    t1 = _tweet(tid="1", like_count=10, reply_count=0, retweet_count=0)
    t2 = _tweet(tid="2", like_count=500, reply_count=10, retweet_count=10)
    t3 = _tweet(tid="3", like_count=20)
    pick = _select_top_tweet_for_entity([t1, t2, t3], set())
    assert pick.id == "2"


def test_select_top_tweet_fallback_by_likes():
    earlier = datetime.now(UTC) - timedelta(days=1)
    later = datetime.now(UTC)
    t_low = _tweet(tid="1", like_count=5, created_at=later)
    t_high_old = _tweet(tid="2", like_count=100, created_at=earlier)
    t_high_new = _tweet(tid="3", like_count=100, created_at=later)
    pick = _select_top_tweet_fallback_by_likes([t_low, t_high_old, t_high_new])
    assert pick.id == "3"


async def test_select_top_tweet_for_token_uses_claude_first():
    claude = SimpleNamespace()
    chosen = _tweet(tid="ch", like_count=0)
    claude.pick_representative_tweets = AsyncMock(return_value=[chosen])
    others = [_tweet(tid="x", like_count=999)]
    pick = await _select_top_tweet_for_token(
        claude, [chosen] + others, "APYX", set(),
    )
    assert pick.id == "ch"


async def test_select_top_tweet_for_token_falls_back_to_likes_when_empty():
    """When pick_representative_tweets returns empty, use like_count fallback."""
    claude = SimpleNamespace()
    claude.pick_representative_tweets = AsyncMock(return_value=[])
    obs = [
        _tweet(tid="low", like_count=5),
        _tweet(tid="high", like_count=500),
        _tweet(tid="mid", like_count=50),
    ]
    pick = await _select_top_tweet_for_token(claude, obs, "APYX", set())
    assert pick.id == "high"


async def test_select_top_tweet_for_token_falls_back_when_claude_raises():
    claude = SimpleNamespace()
    claude.pick_representative_tweets = AsyncMock(side_effect=RuntimeError("boom"))
    obs = [_tweet(tid="solo", like_count=42)]
    pick = await _select_top_tweet_for_token(claude, obs, "X", set())
    assert pick.id == "solo"


def test_select_top_tweet_for_entity_single_tweet():
    only = _tweet(tid="only")
    assert _select_top_tweet_for_entity([only], set()).id == "only"


# ---------------------------------------------------------------------------
# detect_tokens / detect_entities populate top_tweet_url
# ---------------------------------------------------------------------------

async def test_detect_tokens_populates_top_tweet_url_via_claude():
    db = _fake_db()
    claude = SimpleNamespace()
    chosen = _tweet(tid="picked", handle="claudie", text="$APYX deep dive", like_count=2)
    claude.pick_representative_tweets = AsyncMock(return_value=[chosen])
    pool = [
        _tweet(tid="1", handle="a1", text="$APYX moon", like_count=1),
        chosen,
        _tweet(tid="2", handle="a2", text="$APYX again", like_count=3),
        _tweet(tid="3", handle="a3", text="$APYX more", like_count=4),
    ]
    cycle = datetime.now(UTC)
    tokens = await detect_tokens(
        db=db, per_chain={"evm": pool, "solana": []},
        allowlist=set(), cycle_ts=cycle, threshold=0.0, cooc_graph={},
        claude=claude,
    )
    assert tokens, "expected at least one emerging token"
    apyx = next(t for t in tokens if t.token == "APYX")
    assert apyx.top_tweet_url == "https://x.com/claudie/status/picked"


async def test_detect_tokens_fallback_when_claude_returns_empty():
    db = _fake_db()
    claude = SimpleNamespace()
    claude.pick_representative_tweets = AsyncMock(return_value=[])
    high = _tweet(tid="winner", handle="big", text="$APYX details", like_count=999)
    pool = [
        _tweet(tid="1", handle="x", text="$APYX a", like_count=1),
        _tweet(tid="2", handle="y", text="$APYX b", like_count=10),
        high,
    ]
    cycle = datetime.now(UTC)
    tokens = await detect_tokens(
        db=db, per_chain={"evm": pool, "solana": []},
        allowlist=set(), cycle_ts=cycle, threshold=0.0, cooc_graph={},
        claude=claude,
    )
    apyx = next(t for t in tokens if t.token == "APYX")
    assert apyx.top_tweet_url == "https://x.com/big/status/winner"


async def test_detect_entities_populates_top_tweet_url():
    db = _fake_db()
    sector_dict = [{"term": "restaking", "weight": 1.0}]
    pool = [
        _tweet(tid="lo", handle="a", text="restaking is back", like_count=5),
        _tweet(tid="hi", handle="b", text="restaking analysis", like_count=500,
               reply_count=10, retweet_count=20),
        _tweet(tid="mid", handle="c", text="restaking meta", like_count=50),
    ]
    cycle = datetime.now(UTC)
    results = await detect_entities(
        db=db, sweep_pool=pool, allowlist=set(),
        entity_type="sector", dictionary_rows=sector_dict,
        cycle_ts=cycle, threshold=0.0, cooc_graph={},
    )
    assert results
    r = next(e for e in results if e.term == "restaking")
    assert r.top_tweet_url == "https://x.com/b/status/hi"


async def test_detect_entities_single_tweet_top_is_that_tweet():
    db = _fake_db()
    sector_dict = [{"term": "perps", "weight": 1.0}]
    only = _tweet(tid="only", handle="solo", text="perps perps")
    cycle = datetime.now(UTC)
    results = await detect_entities(
        db=db, sweep_pool=[only], allowlist=set(),
        entity_type="sector", dictionary_rows=sector_dict,
        cycle_ts=cycle, threshold=0.0, cooc_graph={},
    )
    assert results
    assert results[0].top_tweet_url == "https://x.com/solo/status/only"


# ---------------------------------------------------------------------------
# Dataclass field exists
# ---------------------------------------------------------------------------

def test_emerging_token_has_top_tweet_url_field():
    from bebop_bot.scoring import EntityScore
    es = EntityScore(narrowness=3.0, quality=3.0, momentum=3.0, coherence=1.0, composite=3.0)
    t = EmergingToken(
        token="X", chain="evm", score=es, unique_authors_24h=1,
        weighted_24h=1.0, raw_24h=1, top_tweet_url="https://x.com/a/status/1",
    )
    assert t.top_tweet_url == "https://x.com/a/status/1"


def test_emerging_entity_has_top_tweet_url_field():
    from bebop_bot.scoring import EntityScore
    es = EntityScore(narrowness=3.0, quality=3.0, momentum=3.0, coherence=1.0, composite=3.0)
    e = EmergingEntity(
        entity_type="sector", term="restaking", score=es,
        unique_authors_24h=1, weighted_24h=1.0, raw_24h=1,
        top_tweet_url="https://x.com/a/status/1",
    )
    assert e.top_tweet_url == "https://x.com/a/status/1"
