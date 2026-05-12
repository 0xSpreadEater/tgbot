from datetime import UTC, datetime, timedelta

from bebop_bot.digest import (
    _format_post,
    _format_topic_block,
    _split_messages,
    relative_time,
    send_digest,
)
from bebop_bot.models import ScoredTweet, Tweet, TweetScore


def make_scored(tid="1", handle="user", text="hi", composite=3.0, auto=None) -> ScoredTweet:
    t = Tweet(
        id=tid,
        text=text,
        author_handle=handle,
        author_name=handle,
        author_created_at=None,
        author_followers=None,
        author_verified=False,
        created_at=datetime.now(UTC) - timedelta(hours=2),
        like_count=12,
        reply_count=3,
        retweet_count=0,
        quote_count=0,
        lang="en",
        url=f"https://x.com/{handle}/status/{tid}",
    )
    s = TweetScore(tweet_id=tid, on_topic=3, substance=3, novelty=3, composite=composite, reasoning="r")
    return ScoredTweet(tweet=t, score=s, auto_included_reason=auto)


def test_relative_time():
    now = datetime.now(UTC)
    assert relative_time(now - timedelta(seconds=10), now=now).endswith("s ago")
    assert relative_time(now - timedelta(minutes=34), now=now) == "34m ago"
    assert relative_time(now - timedelta(hours=2), now=now) == "2h ago"
    assert relative_time(now - timedelta(days=3), now=now) == "3d ago"


def test_html_escape_in_post():
    st = make_scored(text="<script>alert(1)</script> & friends", handle="ev<il>")
    out = _format_post(st, datetime.now(UTC))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "ev&lt;il&gt;" in out
    assert "&amp;" in out


def test_post_truncation():
    long = "x" * 1000
    st = make_scored(text=long)
    out = _format_post(st, datetime.now(UTC))
    # body trimmed to <=320 chars including ellipsis
    assert "x" * 1000 not in out
    assert "x" * 320 not in out
    assert "…" in out


def test_format_topic_block_contains_summary_and_count():
    posts = [make_scored("1"), make_scored("2")]
    block = _format_topic_block("amms", "Summary text", posts, datetime.now(UTC))
    assert "<b>amms</b> (2)" in block
    assert "<i>Summary text</i>" in block


def test_split_messages_breaks_on_size():
    header = "HEADER"
    big = "x" * 3500
    blocks = [big, big]
    messages = _split_messages(header, blocks)
    assert len(messages) >= 2
    assert messages[0].startswith("HEADER")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(
        self,
        chat_id,
        text,
        parse_mode=None,
        disable_web_page_preview=None,
        reply_markup=None,
    ):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        })


async def test_send_digest_empty_results():
    bot = FakeBot()
    await send_digest(bot, 123, {})
    assert len(bot.sent) == 1
    assert "No posts cleared" in bot.sent[0]["text"]


async def test_send_digest_with_results():
    bot = FakeBot()
    results = {"amms": ("good summary", [make_scored("1"), make_scored("2")])}
    await send_digest(bot, 123, results)
    assert len(bot.sent) >= 1
    joined = "\n".join(m["text"] for m in bot.sent)
    assert "Roundup -" in joined
    assert "amms" in joined
    assert "good summary" in joined
