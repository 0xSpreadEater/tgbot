import contextlib
import json
import logging
import re
from typing import Any

from anthropic import AsyncAnthropic

from bebop_bot.models import Tweet, TweetScore

log = logging.getLogger(__name__)


SCORING_SYSTEM_PROMPT = """You are a curation filter for a feed about Bebop - a DEX aggregator and PMM/RFQ execution protocol - and its surrounding domain: proprietary AMMs, AMMs broadly, onchain trading, solvers and intent-based execution, MEV, onchain RWAs, crypto market liquidity, and exchange microstructure.

For each tweet, rate it on three integer dimensions, each 1-5:

on_topic - does it relate to the domain above?
  1 = unrelated
  2 = tangentially related
  3 = clearly on-topic but generic
  4 = directly relevant
  5 = bullseye, central to the domain

substance - is it analytical/argued/data-backed, vs. shilling or surface-level?
  1 = spam, shill, price call, chart screenshot only, engagement bait
  2 = headline rehash, basic question, surface take
  3 = competent explanation of known territory
  4 = clear argument or data point with reasoning
  5 = deep analysis, original data, builder-level detail

novelty - does it add something not already obvious to a domain insider?
  1 = obvious / well-worn
  2 = repeats consensus
  3 = restates with mild new angle
  4 = non-obvious framing or fresh data
  5 = genuinely new thinking, contrarian and supported

Respond with strict JSON only:
{"scores": [{"i": 1, "on_topic": N, "substance": N, "novelty": N, "reasoning": "one line"}, ...]}"""


SUMMARY_SYSTEM_PROMPT = (
    "You synthesize a small set of tweets for one topic into a short factual paragraph. "
    "2-3 sentences. If only 1 or 2 tweets are provided, output a single sentence. "
    "Neutral tone, factual, no hype, no emoji, no hedging filler. Plain text only."
)


def _compute_composite(on_topic: int, substance: int, novelty: int) -> float:
    product = max(0, on_topic) * max(0, substance) * max(0, novelty)
    if product <= 0:
        return 0.0
    return min(5.0, product ** (1 / 3))


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


class ClaudeClient:
    """Thin async wrapper around the Anthropic SDK for the scoring/summarizing calls
    this phase needs. Phase-4 methods are placeholders."""

    BATCH_SIZE = 30

    def __init__(self, api_key: str, model: str):
        self.model = model
        self._client = AsyncAnthropic(api_key=api_key)

    async def close(self) -> None:
        # AsyncAnthropic uses httpx under the hood; close its session.
        await self._client.close()

    async def score_tweets(
        self,
        tweets: list[Tweet],
        topic_name: str,
        topic_query: str,
        taste_rubric: str,
        few_shot_ups: list[Any],
        few_shot_downs: list[Any],
    ) -> list[TweetScore]:
        if not tweets:
            return []

        system_prompt = SCORING_SYSTEM_PROMPT
        if taste_rubric and taste_rubric.strip():
            system_prompt += "\n\nAdditional user-specified taste guidance:\n" + taste_rubric.strip()

        log.debug(
            "claude_score_few_shot",
            extra={
                "topic": topic_name,
                "few_shot_ups": len(few_shot_ups or []),
                "few_shot_downs": len(few_shot_downs or []),
            },
        )
        calibration = ""
        if few_shot_ups or few_shot_downs:
            calibration = "Calibration examples from the user's past feedback:\n"
            for ex in (few_shot_ups or [])[:15]:
                calibration += (
                    f'<example user_label="insight">{_ex_text(ex)} - by @{_ex_handle(ex)}</example>\n'
                )
            for ex in (few_shot_downs or [])[:15]:
                calibration += (
                    f'<example user_label="noise">{_ex_text(ex)} - by @{_ex_handle(ex)}</example>\n'
                )

        all_scores: list[TweetScore] = []
        for batch_start in range(0, len(tweets), self.BATCH_SIZE):
            batch = tweets[batch_start : batch_start + self.BATCH_SIZE]
            numbered = "\n".join(
                f"{i + 1}. {t.text} - by @{t.author_handle}" for i, t in enumerate(batch)
            )
            user_message = (calibration + "\n" if calibration else "") + numbered

            try:
                resp = await self._client.messages.create(
                    model=self.model,
                    max_tokens=2000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                raw = "".join(
                    block.text for block in resp.content if getattr(block, "type", None) == "text"
                )
                parsed = _extract_json(raw)
                if not parsed or not isinstance(parsed.get("scores"), list):
                    log.warning(
                        "claude_score_parse_fail",
                        extra={"topic": topic_name, "raw_snippet": raw[:300]},
                    )
                    all_scores.extend(_neutral_scores(batch))
                    continue
                by_index: dict[int, dict[str, Any]] = {}
                for item in parsed["scores"]:
                    if isinstance(item, dict) and "i" in item:
                        with contextlib.suppress(TypeError, ValueError):
                            by_index[int(item["i"])] = item
                for i, t in enumerate(batch, start=1):
                    item = by_index.get(i)
                    if not item:
                        all_scores.append(_neutral_score(t))
                        continue
                    try:
                        on_topic = max(1, min(5, int(item.get("on_topic", 3))))
                        substance = max(1, min(5, int(item.get("substance", 3))))
                        novelty = max(1, min(5, int(item.get("novelty", 3))))
                    except (TypeError, ValueError):
                        all_scores.append(_neutral_score(t))
                        continue
                    reasoning = str(item.get("reasoning", ""))[:300]
                    all_scores.append(
                        TweetScore(
                            tweet_id=t.id,
                            on_topic=on_topic,
                            substance=substance,
                            novelty=novelty,
                            composite=_compute_composite(on_topic, substance, novelty),
                            reasoning=reasoning,
                        )
                    )
            except Exception as e:  # noqa: BLE001
                log.exception(
                    "claude_score_error",
                    extra={"topic": topic_name, "error": str(e)},
                )
                all_scores.extend(_neutral_scores(batch))

        return all_scores

    async def summarize_topic(self, topic_name: str, tweets: list[Tweet]) -> str:
        if not tweets:
            return ""
        constraint = (
            "Output a single sentence." if len(tweets) <= 2 else "Output 2-3 sentences."
        )
        body = "\n".join(f"- {t.text} (@{t.author_handle})" for t in tweets[:20])
        user_msg = (
            f"Topic: {topic_name}\n\n{constraint}\n\nTweets:\n{body}"
        )
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=400,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            return "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            ).strip()
        except Exception as e:  # noqa: BLE001
            log.exception(
                "claude_summary_error",
                extra={"topic": topic_name, "error": str(e)},
            )
            return ""

    async def pick_representative_tweets(self, *args, **kwargs):
        raise NotImplementedError("pick_representative_tweets is added in Phase 4")

    async def summarize_entity_emergence(self, *args, **kwargs):
        raise NotImplementedError("summarize_entity_emergence is added in Phase 4")

    async def propose_dictionary_terms(self, *args, **kwargs):
        raise NotImplementedError("propose_dictionary_terms is added in Phase 4")

    async def summarize_learnings(
        self,
        ups: list[Any],
        downs: list[Any],
        current_rubric: str,
    ) -> str:
        system = (
            "Summarize patterns in the user's curation choices. 4-6 sentences. "
            "Focus on what they reward and reject. If the current rubric is non-empty, "
            "note where new evidence aligns or conflicts."
        )

        def _fmt(examples: list[Any]) -> str:
            lines = []
            for i, ex in enumerate(examples, start=1):
                lines.append(f"{i}. {_ex_text(ex)} - by @{_ex_handle(ex)}")
            return "\n".join(lines) if lines else "(none)"

        rubric_block = current_rubric.strip() or "(empty)"
        user_msg = (
            f"Current taste rubric:\n{rubric_block}\n\n"
            f"Recent ups ({len(ups)}):\n{_fmt(ups)}\n\n"
            f"Recent downs ({len(downs)}):\n{_fmt(downs)}"
        )
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            return "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            ).strip()
        except Exception as e:  # noqa: BLE001
            log.exception("claude_learnings_error", extra={"error": str(e)})
            return ""


def _neutral_score(t: Tweet) -> TweetScore:
    return TweetScore(
        tweet_id=t.id,
        on_topic=3,
        substance=3,
        novelty=3,
        composite=3.0,
        reasoning="(neutral default; claude parse failed)",
    )


def _neutral_scores(batch: list[Tweet]) -> list[TweetScore]:
    return [_neutral_score(t) for t in batch]


def _ex_text(ex: Any) -> str:
    if hasattr(ex, "tweet_text"):
        return ex.tweet_text
    if isinstance(ex, dict):
        return ex.get("tweet_text") or ex.get("text") or ""
    return str(ex)


def _ex_handle(ex: Any) -> str:
    if hasattr(ex, "author_handle"):
        return ex.author_handle
    if isinstance(ex, dict):
        return ex.get("author_handle") or ex.get("handle") or ""
    return ""
