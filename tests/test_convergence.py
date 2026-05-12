from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bebop_bot.convergence import (
    build_convergence_summary,
    detect_convergence_for_entity,
    detect_convergence_tier,
    signal_backing_event,
    signal_builder_ape_overlap,
    signal_fair_launch_lang,
    signal_known_builder,
    signal_novel_mechanism,
    signal_recursive_lang,
)


def _make_tweet(text: str, author: str = "alice", tweet_id: str = "1") -> SimpleNamespace:
    return SimpleNamespace(
        text=text, author_handle=author, id=tweet_id, author_verified=False,
    )


def _fake_db(viral_handles=None, first_seen_map=None):
    db = SimpleNamespace()
    db.get_viral_handles = AsyncMock(
        return_value=set(viral_handles or {"ctrl", "acme"})
    )
    fs = first_seen_map or {}

    async def _first_seen(et, term):
        return fs.get((et, term))

    db.get_entity_first_seen = _first_seen
    db.insert_convergence_signal = AsyncMock()
    return db


@pytest.fixture
def now_ts():
    return datetime.now(UTC)


async def test_signal_novel_mechanism_via_novelty_marker(now_ts):
    db = _fake_db()
    mech_dict = [
        {"term": "ERC404", "is_novelty_marker": True, "weight": 1.0},
        {"term": "hooks", "is_novelty_marker": False, "weight": 1.0},
    ]
    partners = [("mechanism", "ERC404", 2.0)]
    out = await signal_novel_mechanism(
        db, "token", "TESTCOIN", partners, [], mech_dict, [], [], now_ts,
    )
    assert out is not None
    assert "ERC404" in out["phrases"]


async def test_signal_known_builder_matches(now_ts):
    db = _fake_db(viral_handles={"ctrl"})
    partners = [("handle", "ctrl", 1.0)]
    out = await signal_known_builder(
        db, "token", "TESTCOIN", partners, [], [], [], [], now_ts,
    )
    assert out is not None
    assert "ctrl" in out["handles"]


async def test_signal_known_builder_no_match(now_ts):
    db = _fake_db(viral_handles=set())
    partners = [("handle", "randomperson", 1.0)]
    out = await signal_known_builder(
        db, "token", "TESTCOIN", partners, [], [], [], [], now_ts,
    )
    assert out is None


async def test_signal_recursive_lang_fires(now_ts):
    db = _fake_db()
    tweets = [_make_tweet("recursive PT loop on Morpho with $TESTCOIN")]
    out = await signal_recursive_lang(
        db, "token", "TESTCOIN", [], tweets, [], [], [], now_ts,
    )
    assert out is not None
    assert out["phrases"]


async def test_signal_fair_launch_lang_fires(now_ts):
    db = _fake_db()
    tweets = [_make_tweet("$TESTCOIN fair launch, no presale")]
    out = await signal_fair_launch_lang(
        db, "token", "TESTCOIN", [], tweets, [], [], [], now_ts,
    )
    assert out is not None
    assert "fair launch" in out["phrases"]


async def test_signal_backing_event(now_ts):
    db = _fake_db()
    tweets = [_make_tweet("just listed on Binance: $TESTCOIN")]
    out = await signal_backing_event(
        db, "token", "TESTCOIN", [], tweets, [], [], [], now_ts,
    )
    assert out is not None


async def test_signal_builder_ape_overlap_single_tweet(now_ts):
    db = _fake_db()
    tweets = [_make_tweet("just deployed $TESTCOIN, aping in")]
    out = await signal_builder_ape_overlap(
        db, "token", "TESTCOIN", [], tweets, [], [], [], now_ts,
    )
    assert out is not None


async def test_signal_builder_ape_overlap_cross_author(now_ts):
    db = _fake_db()
    tweets = [
        _make_tweet("just deployed $TESTCOIN", author="dev", tweet_id="1"),
        _make_tweet("aping into $TESTCOIN", author="trader", tweet_id="2"),
    ]
    out = await signal_builder_ape_overlap(
        db, "token", "TESTCOIN", [], tweets, [], [], [], now_ts,
    )
    assert out is not None


async def test_signal_builder_ape_overlap_negative(now_ts):
    db = _fake_db()
    tweets = [_make_tweet("just deployed $TESTCOIN", author="dev")]
    out = await signal_builder_ape_overlap(
        db, "token", "TESTCOIN", [], tweets, [], [], [], now_ts,
    )
    assert out is None


async def test_detect_convergence_for_entity_aggregates_signals(now_ts):
    db = _fake_db(viral_handles={"ctrl"})
    mech_dict = [{"term": "ERC404", "is_novelty_marker": True, "weight": 1.0}]
    partners = [
        ("mechanism", "ERC404", 2.0),
        ("handle", "ctrl", 1.0),
    ]
    tweets = [
        _make_tweet(
            "$TESTCOIN fair launch, no presale, just deployed, aping in",
            author="dev", tweet_id="1",
        ),
    ]
    out = await detect_convergence_for_entity(
        db=db, entity_type="token", entity_term="TESTCOIN",
        cooccurrence_partners=partners,
        sweep_pool=tweets,
        sector_dict=[], venue_dict=[], mechanism_dict=mech_dict,
        cycle_ts=now_ts,
    )
    # Expect: novel_mechanism + known_builder + fair_launch_lang +
    # builder_ape_overlap → at least 4
    assert out["count"] >= 4
    assert "novel_mechanism" in out["signals"]
    assert "known_builder" in out["signals"]
    assert "fair_launch_lang" in out["signals"]
    assert "builder_ape_overlap" in out["signals"]


async def test_detect_convergence_tier_calls_claude(now_ts):
    db = _fake_db()
    claude = SimpleNamespace()
    claude.judge_strong_convergence = AsyncMock(
        return_value={"confidence": 4, "rationale": "strong pattern match"}
    )
    out = await detect_convergence_tier(
        db=db, claude=claude,
        entity_type="token", entity_term="TESTCOIN",
        signal_count=4, evidence={"novel_mechanism": {"phrases": ["ERC404"]}},
        sweep_pool=[], sector_dict=[], venue_dict=[], mechanism_dict=[],
        viral_seeds=[{"name": "X", "chain": "eth"}],
    )
    assert out["claude_confidence"] == 4
    assert out["claude_rationale"] == "strong pattern match"


async def test_detect_convergence_tier_handles_low_confidence(now_ts):
    db = _fake_db()
    claude = SimpleNamespace()
    claude.judge_strong_convergence = AsyncMock(
        return_value={"confidence": 2, "rationale": "weak"}
    )
    out = await detect_convergence_tier(
        db=db, claude=claude,
        entity_type="token", entity_term="TESTCOIN",
        signal_count=3, evidence={},
        sweep_pool=[], sector_dict=[], venue_dict=[], mechanism_dict=[],
        viral_seeds=[],
    )
    assert out["claude_confidence"] == 2


def test_build_convergence_summary():
    s = build_convergence_summary(
        "token", "TESTCOIN",
        {"signals": ["novel_mechanism", "known_builder"], "count": 2},
        "convergence", None,
    )
    assert "TESTCOIN" in s
    assert "2/7" in s


def test_build_convergence_summary_strong_with_rationale():
    s = build_convergence_summary(
        "token", "TESTCOIN",
        {"signals": ["novel_mechanism"] * 5, "count": 5},
        "strong_convergence", "looks like a textbook pattern",
    )
    assert "STRONG" in s
    assert "textbook pattern" in s
