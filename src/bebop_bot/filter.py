import logging
from datetime import UTC, datetime
from typing import Any

from bebop_bot.models import ScoredTweet, Tweet, TweetScore

log = logging.getLogger(__name__)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def filter_topic(
    db: Any,
    topic: Any,
    raw_tweets: list[Tweet],
    allowlist_set: set[str],
    threshold: float,
    taste_rubric: str,
    few_shot_ups: list[Any],
    few_shot_downs: list[Any],
    claude: Any,
) -> list[ScoredTweet]:
    """Bucket tweets, score the unknowns via Claude, and return what's worth shipping.

    Buckets:
      auto_include = author in allowlist OR (ups >= 3 AND downs == 0 in last 60d)
      auto_hide    = muted_until > now() OR net 60d score <= -3
      candidates   = everything else
    """
    now = datetime.now(UTC)

    auto_include: list[tuple[Tweet, str]] = []
    candidates: list[Tweet] = []
    dropped = 0
    seen_ids: set[str] = set()

    for tweet in raw_tweets:
        if tweet.id in seen_ids:
            continue
        seen_ids.add(tweet.id)

        handle = tweet.author_handle.lower()

        muted_until = await db.get_muted_until(handle)
        muted_dt = _parse_ts(muted_until)
        if muted_dt is not None:
            if muted_dt.tzinfo is None:
                muted_dt = muted_dt.replace(tzinfo=UTC)
            if muted_dt > now:
                dropped += 1
                continue

        ups, downs = await db.get_author_feedback_counts(handle, days=60)
        net = ups - downs
        if net <= -3:
            dropped += 1
            continue

        if handle in allowlist_set:
            auto_include.append((tweet, "allowlist"))
            continue
        if ups >= 3 and downs == 0:
            auto_include.append((tweet, "trusted_author"))
            continue

        candidates.append(tweet)

    scores_by_id: dict[str, TweetScore] = {}
    if candidates:
        scored = await claude.score_tweets(
            candidates,
            topic.name,
            topic.query,
            taste_rubric,
            few_shot_ups,
            few_shot_downs,
        )
        scores_by_id = {s.tweet_id: s for s in scored}

    result: list[ScoredTweet] = []

    for tweet, reason in auto_include:
        synthetic = TweetScore(
            tweet_id=tweet.id,
            on_topic=5,
            substance=5,
            novelty=5,
            composite=5.0,
            reasoning=reason,
        )
        result.append(ScoredTweet(tweet=tweet, score=synthetic, auto_included_reason=reason))

    for tweet in candidates:
        score = scores_by_id.get(tweet.id)
        if score is None:
            continue
        if score.composite < threshold:
            continue
        result.append(ScoredTweet(tweet=tweet, score=score, auto_included_reason=None))

    result.sort(key=lambda st: (st.score.composite, st.tweet.created_at), reverse=True)

    log.info(
        "filter_topic_done",
        extra={
            "topic": topic.name,
            "raw": len(raw_tweets),
            "auto_included": len(auto_include),
            "candidates": len(candidates),
            "dropped_auto_hide": dropped,
            "kept": len(result),
        },
    )

    return result[:15]
