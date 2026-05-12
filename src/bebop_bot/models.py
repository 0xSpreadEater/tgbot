from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Tweet:
    id: str
    text: str
    author_handle: str
    author_name: str
    author_created_at: datetime | None
    author_followers: int | None
    author_verified: bool
    created_at: datetime
    like_count: int
    reply_count: int
    retweet_count: int
    quote_count: int
    lang: str | None
    url: str


@dataclass(frozen=True, slots=True)
class TweetScore:
    tweet_id: str
    on_topic: int
    substance: int
    novelty: int
    composite: float
    reasoning: str


@dataclass(frozen=True, slots=True)
class ScoredTweet:
    tweet: Tweet
    score: TweetScore
    auto_included_reason: str | None
