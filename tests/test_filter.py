from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bebop_bot.filter import filter_topic
from bebop_bot.models import Tweet, TweetScore


@dataclass
class FakeTopic:
    name: str
    query: str
    last_seen_id: str | None = None


class FakeDb:
    """In-memory stand-in exposing only the methods filter_topic touches."""

    def __init__(
        self,
        author_counts: dict[str, tuple[int, int]] | None = None,
        muted: dict[str, datetime] | None = None,
    ):
        self._author_counts = author_counts or {}
        self._muted = muted or {}

    async def get_muted_until(self, handle: str):
        v = self._muted.get(handle.lower())
        return v.isoformat() if v else None

    async def get_author_feedback_counts(self, handle: str, days: int = 60):
        return self._author_counts.get(handle.lower(), (0, 0))


class StubClaude:
    """Returns deterministic scores based on tweet text length."""

    def __init__(self, mapping: dict[str, int] | None = None):
        self.mapping = mapping or {}
        self.last_called_with = None

    async def score_tweets(
        self, tweets, topic_name, topic_query, taste_rubric, few_shot_ups, few_shot_downs
    ):
        self.last_called_with = list(tweets)
        out = []
        for t in tweets:
            composite = float(self.mapping.get(t.id, 2.0))
            n = max(1, min(5, round(composite)))
            out.append(
                TweetScore(
                    tweet_id=t.id,
                    on_topic=n,
                    substance=n,
                    novelty=n,
                    composite=composite,
                    reasoning="stub",
                )
            )
        return out


def make_tweet(tid: str, handle: str, text: str = "hello world") -> Tweet:
    return Tweet(
        id=tid,
        text=text,
        author_handle=handle.lower(),
        author_name=handle,
        author_created_at=None,
        author_followers=None,
        author_verified=False,
        created_at=datetime.now(UTC),
        like_count=0,
        reply_count=0,
        retweet_count=0,
        quote_count=0,
        lang="en",
        url=f"https://x.com/{handle}/status/{tid}",
    )


async def test_allowlist_passes_below_threshold():
    db = FakeDb()
    topic = FakeTopic("t", "q")
    tweets = [make_tweet("1", "alice")]
    claude = StubClaude({"1": 0.5})
    out = await filter_topic(db, topic, tweets, {"alice"}, 4.0, "", [], [], claude)
    assert len(out) == 1
    assert out[0].auto_included_reason == "allowlist"
    assert out[0].score.composite == 5.0
    # alice was auto-included, so claude should not have seen her
    assert claude.last_called_with is None


async def test_trusted_author_is_auto_included():
    db = FakeDb(author_counts={"trusty": (3, 0)})
    topic = FakeTopic("t", "q")
    tweets = [make_tweet("1", "trusty")]
    claude = StubClaude()
    out = await filter_topic(db, topic, tweets, set(), 5.0, "", [], [], claude)
    assert len(out) == 1
    assert out[0].auto_included_reason == "trusted_author"


async def test_muted_author_dropped():
    future = datetime.now(UTC) + timedelta(days=1)
    db = FakeDb(muted={"badguy": future})
    topic = FakeTopic("t", "q")
    tweets = [make_tweet("1", "badguy")]
    out = await filter_topic(db, topic, tweets, set(), 1.0, "", [], [], StubClaude({"1": 5.0}))
    assert out == []


async def test_negative_net_score_dropped():
    db = FakeDb(author_counts={"meh": (0, 3)})
    topic = FakeTopic("t", "q")
    tweets = [make_tweet("1", "meh")]
    out = await filter_topic(db, topic, tweets, set(), 1.0, "", [], [], StubClaude({"1": 5.0}))
    assert out == []


async def test_threshold_filters_candidates():
    db = FakeDb()
    topic = FakeTopic("t", "q")
    tweets = [make_tweet("1", "a"), make_tweet("2", "b"), make_tweet("3", "c")]
    claude = StubClaude({"1": 1.5, "2": 3.5, "3": 4.5})
    out = await filter_topic(db, topic, tweets, set(), 3.0, "", [], [], claude)
    ids = [st.tweet.id for st in out]
    assert ids == ["3", "2"]
    assert all(st.auto_included_reason is None for st in out)


async def test_sort_and_cap_at_15():
    db = FakeDb()
    topic = FakeTopic("t", "q")
    tweets = [make_tweet(str(i), f"u{i}") for i in range(20)]
    claude = StubClaude({str(i): 4.0 for i in range(20)})
    out = await filter_topic(db, topic, tweets, set(), 3.0, "", [], [], claude)
    assert len(out) == 15
