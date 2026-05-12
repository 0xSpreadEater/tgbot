import logging
from datetime import datetime
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from bebop_bot.models import Tweet

log = logging.getLogger(__name__)


class XClientError(Exception):
    """Non-retryable error response from the X API."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_tweet(t: dict[str, Any], users: dict[str, dict[str, Any]]) -> Tweet | None:
    tid = t.get("id")
    text = t.get("text") or ""
    author_id = t.get("author_id")
    if not tid or not author_id:
        return None
    user = users.get(author_id) or {}
    handle = (user.get("username") or "").lower()
    if not handle:
        return None
    metrics = t.get("public_metrics") or {}
    user_metrics = user.get("public_metrics") or {}
    return Tweet(
        id=str(tid),
        text=text,
        author_handle=handle,
        author_name=user.get("name") or handle,
        author_created_at=_parse_datetime(user.get("created_at")),
        author_followers=user_metrics.get("followers_count"),
        author_verified=bool(user.get("verified", False)),
        created_at=_parse_datetime(t.get("created_at")) or datetime.fromtimestamp(0),
        like_count=int(metrics.get("like_count", 0)),
        reply_count=int(metrics.get("reply_count", 0)),
        retweet_count=int(metrics.get("retweet_count", 0)),
        quote_count=int(metrics.get("quote_count", 0)),
        lang=t.get("lang"),
        url=f"https://x.com/{handle}/status/{tid}",
    )


class XClient:
    """Thin async wrapper around the X (Twitter) v2 recent-search endpoint."""

    BASE_URL = "https://api.x.com"

    def __init__(self, bearer_token: str):
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=httpx.Timeout(30.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def search_recent(
        self,
        query: str,
        since_id: str | None = None,
        max_results: int = 100,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[Tweet]:
        collected: list[Tweet] = []
        next_token: str | None = None
        remaining = max(1, int(max_results))

        for _page in range(5):
            page_size = min(100, remaining)
            params: dict[str, Any] = {
                "query": query,
                "max_results": max(10, page_size),
                "tweet.fields": "created_at,author_id,public_metrics,lang",
                "expansions": "author_id",
                "user.fields": "username,name,verified,created_at,public_metrics",
            }
            if since_id:
                params["since_id"] = str(since_id)
            if start_time:
                params["start_time"] = start_time
            if end_time:
                params["end_time"] = end_time
            if next_token:
                params["next_token"] = next_token

            try:
                resp = await self._request("/2/tweets/search/recent", params)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    log.warning(
                        "x_rate_limited",
                        extra={"query": query, "collected": len(collected)},
                    )
                    break
                snippet = (e.response.text or "")[:300]
                raise XClientError(
                    f"X API {e.response.status_code}: {snippet}"
                ) from e

            data = resp.get("data") or []
            includes = resp.get("includes") or {}
            users_list = includes.get("users") or []
            users_by_id = {u["id"]: u for u in users_list if u.get("id")}

            for raw in data:
                tweet = _build_tweet(raw, users_by_id)
                if tweet is not None:
                    collected.append(tweet)
                    if len(collected) >= max_results:
                        return collected

            remaining = max_results - len(collected)
            next_token = (resp.get("meta") or {}).get("next_token")
            if not next_token or remaining <= 0 or not data:
                break

        return collected

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.get(path, params=params)
        if resp.status_code >= 400:
            resp.raise_for_status()
        return resp.json()
