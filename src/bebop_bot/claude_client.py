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

    async def pick_representative_tweets(
        self, tweets: list[Tweet], entity_term: str, limit: int = 5,
    ) -> list[Tweet]:
        """Best-effort representative pick. Returns up to `limit` tweets;
        if Claude is unavailable, falls back to ranking by engagement."""
        if not tweets:
            return []
        if len(tweets) <= limit:
            return list(tweets)

        ranked = sorted(
            tweets,
            key=lambda t: (
                getattr(t, "like_count", 0)
                + getattr(t, "retweet_count", 0)
                + getattr(t, "reply_count", 0)
            ),
            reverse=True,
        )
        return ranked[:limit]

    async def summarize_entity_emergence(
        self, entity_type: str, entity_term: str, tweets: list[Tweet],
    ) -> str:
        if not tweets:
            return ""
        body = "\n".join(
            f"- {t.text[:200]} (@{t.author_handle})" for t in tweets[:10]
        )
        system = (
            "You are summarizing why a crypto entity is showing emerging "
            "discussion. 1-2 sentences, neutral, factual, no hype. "
            "Plain text only, no emoji."
        )
        user = (
            f"Entity type: {entity_type}\nEntity term: {entity_term}\n\n"
            f"Recent tweets:\n{body}"
        )
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=300,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            ).strip()
        except Exception as e:  # noqa: BLE001
            log.exception(
                "claude_entity_emergence_error",
                extra={
                    "entity_type": entity_type,
                    "entity_term": entity_term,
                    "error": str(e),
                },
            )
            return ""

    async def propose_dictionary_terms(
        self,
        sample: list[dict],
        existing_sectors: list[str],
        existing_venues: list[str],
        existing_mechanisms: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Ask Claude to propose new sector / venue / mechanism terms.
        Returns a list of (type, term) pairs."""
        if not sample:
            return []
        existing_mechanisms = existing_mechanisms or []
        system = (
            "You scan recent crypto tweets to propose additions to three "
            "dictionaries: SECTORS (thematic narratives, e.g. 'restaking', "
            "'prediction markets'), VENUES (chains, exchanges, launchpads, "
            "e.g. 'MegaETH', 'pump.fun'), and MECHANISMS (token standards, "
            "AMM patterns, launch curves, yield-composition patterns, e.g. "
            "'ERC404', 'bonding curve', 'recursive PT', 'mine-to-earn').\n\n"
            "Propose only NEW terms not already in the lists provided. Each "
            "bucket gets up to 10 suggestions. Strict JSON output:\n"
            '{"sectors": ["term1", ...], "venues": [...], "mechanisms": [...]}'
        )
        sample_block = "\n".join(
            f"- {s.get('text', '')[:200]} (@{s.get('handle','')})" for s in sample[:60]
        )
        user = (
            f"Existing sectors ({len(existing_sectors)}): "
            f"{', '.join(existing_sectors[:80])}\n\n"
            f"Existing venues ({len(existing_venues)}): "
            f"{', '.join(existing_venues[:80])}\n\n"
            f"Existing mechanisms ({len(existing_mechanisms)}): "
            f"{', '.join(existing_mechanisms[:80])}\n\n"
            f"Recent tweet sample ({len(sample)}):\n{sample_block}"
        )
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=800,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = "".join(
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            )
        except Exception as e:  # noqa: BLE001
            log.exception("claude_propose_terms_error", extra={"error": str(e)})
            return []
        parsed = _extract_json(raw)
        if not parsed:
            log.warning(
                "claude_propose_terms_parse_fail",
                extra={"raw_snippet": raw[:300]},
            )
            return []
        out: list[tuple[str, str]] = []
        for ent_type, key in (
            ("sector", "sectors"),
            ("venue", "venues"),
            ("mechanism", "mechanisms"),
        ):
            items = parsed.get(key) or []
            if not isinstance(items, list):
                continue
            for item in items[:10]:
                if isinstance(item, str) and item.strip():
                    out.append((ent_type, item.strip()))
        return out

    async def judge_strong_convergence(
        self,
        entity_type: str,
        entity_term: str,
        signals_fired: list[str],
        evidence: dict,
        viral_seeds: list[dict],
        sample_tweets: list[Tweet],
        pattern_corpus: list[dict] | None = None,
    ) -> dict:
        """Ask Claude to score how strongly this entity resembles known viral
        precursors. Returns {'confidence': 1-5, 'rationale': str}.

        pattern_corpus (Phase 4.7) is an optional list of named observational
        patterns Claude previously proposed and the user has curated. When
        supplied, it's added as an additional few-shot block and Claude is
        asked to call out any pattern from the corpus that applies to the
        candidate.
        """
        pattern_corpus = pattern_corpus or []
        system = (
            "You are evaluating whether a crypto entity is showing precursor "
            "signals to a viral run. You have:\n"
            "  - Calibration examples (historical viral-token precursors)\n"
            "  - Pattern corpus (named observational patterns the bot has "
            "accumulated; user-curated)\n"
            "  - A fresh candidate with which signals fired and what evidence "
            "supports each.\n\n"
            "Output strict JSON:\n"
            '{"confidence": <1-5>, "rationale": "<1-2 sentences>"}\n\n'
            "Scoring rubric:\n"
            "  1 = unrelated coincidence; signals are weak or background-rate\n"
            "  2 = some signal but pattern is incomplete or commonly misfires\n"
            "  3 = signals match but I can't tell if it's converging or just noisy\n"
            "  4 = pattern strongly resembles historical positives in this dataset\n"
            "  5 = textbook precursor pattern; high confidence this is an early "
            "viral candidate\n\n"
            "If a pattern from the pattern corpus seems to apply to the "
            "candidate, mention it by name in your rationale.\n\n"
            "Be CONSERVATIVE. False positives are cheaper than false negatives, "
            "but the user reads every 'strong convergence' message. Reserve 5 "
            "for patterns that line up with at least 2 historical positives "
            "across multiple signal categories."
        )

        sig_lines = []
        for name in signals_fired:
            ev = evidence.get(name, {})
            ev_str = json.dumps(ev)[:200]
            sig_lines.append(f"  - {name}: {ev_str}")
        sig_block = "\n".join(sig_lines) if sig_lines else "  (none)"

        seed_lines = []
        for s in viral_seeds[:15]:
            seed_lines.append(
                f"  - {s.get('name','?')} ({s.get('chain','?')}): "
                f"signals={s.get('signals', [])}, "
                f"phrases={s.get('phrases', [])[:8]}, "
                f"rationale: {s.get('rationale','')[:300]}"
            )
        seed_block = "\n".join(seed_lines) if seed_lines else "  (none)"

        tweet_lines = []
        for i, t in enumerate(sample_tweets[:10], start=1):
            text = (getattr(t, "text", "") or "")[:200]
            handle = getattr(t, "author_handle", "")
            tweet_lines.append(f"  {i}. {text} - @{handle}")
        tweet_block = "\n".join(tweet_lines) if tweet_lines else "  (none)"

        if pattern_corpus:
            pattern_lines = []
            for p in pattern_corpus[:15]:
                weight = float(p.get("weight", 1.0) or 1.0)
                pattern_lines.append(
                    f"  - {p.get('name','?')} (weight {weight:.1f}): "
                    f"{(p.get('description') or '')[:300]}"
                )
            pattern_block = (
                "Pattern corpus (Claude-proposed, user-curated):\n"
                + "\n".join(pattern_lines)
            )
        else:
            pattern_block = "Pattern corpus: (none yet)"

        user = (
            f"Candidate entity: {entity_type}: {entity_term}\n\n"
            f"Signals fired ({len(signals_fired)}/7):\n{sig_block}\n\n"
            f"Historical positive examples for comparison:\n{seed_block}\n\n"
            f"{pattern_block}\n\n"
            f"Recent tweets mentioning this entity:\n{tweet_block}\n"
        )
        log.debug(
            "claude_judge_strong_convergence_prompt",
            extra={
                "entity_type": entity_type,
                "entity_term": entity_term,
                "signals_fired_count": len(signals_fired),
                "viral_seeds_count": len(viral_seeds),
                "pattern_corpus_count": len(pattern_corpus),
                "sample_tweets_count": len(sample_tweets),
                "prompt_body": user[:4000],
            },
        )

        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=400,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = "".join(
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "claude_judge_strong_convergence_error",
                extra={
                    "entity_type": entity_type,
                    "entity_term": entity_term,
                    "error": str(e),
                },
            )
            return {"confidence": None, "rationale": None}

        parsed = _extract_json(raw)
        if not parsed:
            return {"confidence": None, "rationale": None}
        confidence_raw = parsed.get("confidence")
        try:
            confidence = max(1, min(5, int(confidence_raw)))
        except (TypeError, ValueError):
            confidence = None
        rationale = str(parsed.get("rationale", ""))[:600]
        return {"confidence": confidence, "rationale": rationale}

    async def propose_patterns(
        self,
        sample: list[Tweet],
        existing_patterns: list[dict],
        cap: int,
    ) -> list[dict]:
        """Free-form pattern proposal (Phase 4.7).

        Returns a list of dicts:
          {'name': str, 'description': str, 'confidence': int 1-5,
           'tweet_ids': list[str], 'anchors': list[(etype, eterm)]}

        Each pattern must be a noun-phrase name (<=6 words), observed
        across >=3 posts in the sample, distinct from the seven
        structural categories the bot already detects, and distinct
        from existing names. Empty list if nothing qualifies; the
        method never raises.
        """
        if not sample or cap <= 0:
            return []

        existing_block = "\n".join(
            f"  - {p.get('name','?')} (weight {float(p.get('weight',1.0) or 1.0):.1f}, "
            f"proposed {int(p.get('propose_count',1) or 1)}x): "
            f"{(p.get('description') or '')[:300]}"
            for p in (existing_patterns or [])[:30]
        ) or "(none yet)"

        system = (
            "You are scanning crypto Twitter for patterns that may signal "
            "an emerging viral token or narrative — patterns BEYOND the "
            "structural categories the bot already detects.\n\n"
            "The bot already detects these seven structural signal "
            "categories per post:\n"
            "  1. novel mechanism vocabulary\n"
            "  2. new venue + flagship-dApp coupling\n"
            "  3. known pseudonymous builder\n"
            "  4. recursive yield / composition language\n"
            "  5. backing or legitimization moment\n"
            "  6. builder + trader language same window\n"
            "  7. fair-launch / bonding-curve mechanic\n\n"
            "Your job is to identify patterns that DON'T fit cleanly into "
            "any of those seven, but that look meaningful — for example: "
            "VC-portfolio co-rallies, ecosystem token rotation patterns, "
            "specific airdrop-eligibility hint language, geographic early-"
            "adoption patterns, cross-token speculation correlations, "
            "narrative-handoff patterns, etc.\n\n"
            "Each pattern you propose must:\n"
            "  - Have a SHORT noun-phrase name (max 6 words)\n"
            "  - Be observed across multiple posts in the sample (>=3)\n"
            "  - Be distinct from the seven structural categories\n"
            "  - Be distinct from existing proposed patterns in the corpus "
            "(case-insensitive name match)\n"
            "  - Cite specific supporting tweet indices and anchor entities\n\n"
            f"Be conservative. Propose at most {int(cap)} patterns this "
            "cycle. Return zero if you don't see anything genuinely novel. "
            "Don't manufacture filler to hit the cap.\n\n"
            "Existing pattern corpus (do NOT re-propose under the same "
            "name unless the sample strongly reinforces it):\n"
            f"{existing_block}\n\n"
            'Return strict JSON: {"patterns": [{"name": str, '
            '"description": str (1 sentence), "confidence": int 1-5, '
            '"tweet_indices": [int], "anchors": [{"type": str, '
            '"term": str}]}, ...]} — empty list if nothing qualifies.'
        )

        sample_block = "\n".join(
            f"{i + 1}. {(t.text or '')[:280]} - @{getattr(t,'author_handle','')}"
            for i, t in enumerate(sample[:60])
        )
        user_msg = "Tweet sample:\n" + sample_block

        log.debug(
            "claude_propose_patterns_prompt",
            extra={
                "sample_size": len(sample),
                "existing_pattern_count": len(existing_patterns or []),
                "proposal_cap": int(cap),
                "prompt_body": (system + "\n\n" + user_msg)[:4000],
            },
        )

        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=800,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = "".join(
                block.text for block in resp.content
                if getattr(block, "type", None) == "text"
            )
        except Exception as e:  # noqa: BLE001
            log.exception(
                "claude_propose_patterns_error",
                extra={"error": str(e)},
            )
            return []

        parsed = _extract_json(raw)
        if not parsed:
            log.warning(
                "claude_propose_patterns_parse_fail",
                extra={"raw_snippet": raw[:300]},
            )
            return []

        items = parsed.get("patterns") or []
        if not isinstance(items, list):
            return []

        out: list[dict] = []
        for item in items[: int(cap)]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            desc = str(item.get("description") or "").strip()
            if not name or not desc:
                continue
            try:
                conf = max(1, min(5, int(item.get("confidence", 1))))
            except (TypeError, ValueError):
                conf = 1
            try:
                indices = [int(i) for i in (item.get("tweet_indices") or [])]
            except (TypeError, ValueError):
                indices = []
            tweet_ids: list[str] = []
            for i in indices:
                if 1 <= i <= len(sample):
                    tid = str(getattr(sample[i - 1], "id", "") or "")
                    if tid:
                        tweet_ids.append(tid)
            anchors_raw = item.get("anchors") or []
            anchors: list[tuple[str, str]] = []
            if isinstance(anchors_raw, list):
                for a in anchors_raw[:5]:
                    if isinstance(a, dict):
                        etype = str(a.get("type") or "").strip()
                        eterm = str(a.get("term") or "").strip()
                        if etype and eterm:
                            anchors.append((etype, eterm))
            out.append({
                "name": name[:80],
                "description": desc[:500],
                "confidence": conf,
                "tweet_ids": tweet_ids[:5],
                "anchors": anchors,
            })
        return out

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
