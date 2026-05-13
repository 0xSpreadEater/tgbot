"""Phase 4 orchestrator: TOKENS / SECTORS / VENUES / MECHANISMS, plus
co-occurrence-aware scoring, convergence detection, dictionary proposal,
and venue suggestion.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from bebop_bot import convergence, cooccurrence, dictionary, extractors, patterns
from bebop_bot.scoring import EntityScore, compute_entity_score

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmergingToken:
    token: str
    chain: str
    score: EntityScore
    unique_authors_24h: int
    weighted_24h: float
    raw_24h: int
    sample_tweet_ids: list[str] = field(default_factory=list)
    top_tweet_url: str | None = None


@dataclass(frozen=True)
class EmergingEntity:
    entity_type: str
    term: str
    score: EntityScore
    unique_authors_24h: int
    weighted_24h: float
    raw_24h: int
    sample_tweet_ids: list[str] = field(default_factory=list)
    top_tweet_url: str | None = None


def _author_weight(tweet: Any, allowlist: set[str]) -> float:
    handle = (getattr(tweet, "author_handle", "") or "").lower()
    if handle in allowlist:
        return 1.5
    if getattr(tweet, "author_verified", False):
        return 1.2
    return 1.0


async def collect_sweep_pool(db: Any, x: Any) -> tuple[list[Any], dict[str, list[Any]]]:
    """Run the EVM and Solana sweep queries and return combined + per-chain
    pools. Honors chain enabled flags from settings."""
    pool: list[Any] = []
    per_chain: dict[str, list[Any]] = {"evm": [], "solana": []}
    seen_ids: set[str] = set()

    evm_on = await db.get_setting_bool("chain_evm_enabled", True)
    sol_on = await db.get_setting_bool("chain_solana_enabled", True)

    if evm_on:
        q = await db.get_setting("evm_sweep_query", "") or ""
        if q.strip() and x is not None:
            try:
                results = await x.search_recent(q, max_results=100)
            except Exception:  # noqa: BLE001
                log.exception("evm_sweep_failed")
                results = []
            for t in results:
                if t.id in seen_ids:
                    continue
                seen_ids.add(t.id)
                pool.append(t)
                per_chain["evm"].append(t)

    if sol_on:
        q = await db.get_setting("solana_sweep_query", "") or ""
        if q.strip() and x is not None:
            try:
                results = await x.search_recent(q, max_results=100)
            except Exception:  # noqa: BLE001
                log.exception("solana_sweep_failed")
                results = []
            for t in results:
                if t.id in seen_ids:
                    continue
                seen_ids.add(t.id)
                pool.append(t)
                per_chain["solana"].append(t)

    log.info(
        "sweep_pool_collected",
        extra={
            "total_tweets": len(pool),
            "evm_tweets": len(per_chain["evm"]),
            "solana_tweets": len(per_chain["solana"]),
        },
    )
    return pool, per_chain


def _aggregate_mentions(
    pool: list[Any],
    extract_fn,
    allowlist: set[str],
) -> dict[str, dict[str, Any]]:
    """Aggregate per-term: weighted_count, raw_count, unique_authors, sample_tweet_ids, tweets."""
    by_term: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "weighted": 0.0,
            "raw": 0,
            "authors": set(),
            "tweet_ids": [],
            "tweets": [],
        }
    )
    for t in pool:
        text = getattr(t, "text", "") or ""
        terms = extract_fn(text)
        if not terms:
            continue
        weight = _author_weight(t, allowlist)
        handle = (getattr(t, "author_handle", "") or "").lower()
        tid = str(getattr(t, "id", "") or "")
        for term in terms:
            entry = by_term[term]
            entry["weighted"] += weight
            entry["raw"] += 1
            entry["authors"].add(handle)
            entry["tweets"].append(t)
            if len(entry["tweet_ids"]) < 5:
                entry["tweet_ids"].append(tid)
    return by_term


def _tweet_url(t: Any) -> str | None:
    url = getattr(t, "url", None)
    if url:
        return str(url)
    handle = getattr(t, "author_handle", None)
    tid = getattr(t, "id", None)
    if handle and tid:
        return f"https://x.com/{handle}/status/{tid}"
    return None


def _score_tweet_for_top_pick(t: Any, allowlist: set[str]) -> float:
    return (
        _author_weight(t, allowlist) * 2.0
        + float(getattr(t, "like_count", 0) or 0) / 100.0
        + (
            float(getattr(t, "reply_count", 0) or 0)
            + float(getattr(t, "retweet_count", 0) or 0)
        ) / 50.0
    )


def _select_top_tweet_for_entity(
    observations: list[Any], allowlist: set[str],
) -> Any | None:
    if not observations:
        return None
    return max(observations, key=lambda t: _score_tweet_for_top_pick(t, allowlist))


def _select_top_tweet_fallback_by_likes(observations: list[Any]) -> Any | None:
    """Fallback ranking for tokens: highest like_count, tiebreak created_at desc."""
    if not observations:
        return None
    def _key(t: Any) -> tuple[int, str]:
        likes = int(getattr(t, "like_count", 0) or 0)
        created = getattr(t, "created_at", None)
        created_str = created.isoformat() if hasattr(created, "isoformat") else str(created or "")
        return (likes, created_str)
    return max(observations, key=_key)


async def _select_top_tweet_for_token(
    claude: Any, observations: list[Any], token: str, allowlist: set[str],
) -> Any | None:
    if not observations:
        return None
    if claude is not None:
        try:
            reps = await claude.pick_representative_tweets(
                observations, token, limit=2,
            )
            if reps:
                return reps[0]
        except Exception:  # noqa: BLE001
            log.exception(
                "pick_representative_tweets_failed",
                extra={"token_symbol": token},
            )
    return _select_top_tweet_fallback_by_likes(observations)


async def detect_tokens(
    db: Any,
    per_chain: dict[str, list[Any]],
    allowlist: set[str],
    cycle_ts: datetime,
    threshold: float,
    cooc_graph: dict,
    claude: Any = None,
) -> list[EmergingToken]:
    """Detect emerging tokens across EVM + Solana sweep pools."""
    out: list[EmergingToken] = []

    chain_pools = {"ethereum": per_chain.get("evm", []), "solana": per_chain.get("solana", [])}
    for chain, pool in chain_pools.items():
        if not pool:
            continue
        if chain == "solana":
            def _extract(text: str) -> list[str]:
                # Treat cashtags + Solana addresses as tokens
                syms = extractors.extract_cashtags(text)
                addrs = extractors.extract_solana_addresses(text)
                return syms + addrs
        else:
            def _extract(text: str) -> list[str]:
                syms = extractors.extract_cashtags(text)
                addrs = extractors.extract_evm_addresses(text)
                return syms + addrs

        agg = _aggregate_mentions(pool, _extract, allowlist)
        for token, data in agg.items():
            unique = len(data["authors"])
            partners = cooc_graph.get(("token", token), [])
            mean_7d = await _mean_weighted_history(db, "token", token, cycle_ts)
            score = compute_entity_score(
                unique_authors_7d=unique,
                weighted_24h=data["weighted"],
                raw_24h=data["raw"],
                mean_weighted_7d=mean_7d,
                cooccurrence_partners=partners,
            )
            if score.composite < threshold:
                continue
            top_tweet = await _select_top_tweet_for_token(
                claude, data["tweets"], token, allowlist,
            )
            top_tweet_url = _tweet_url(top_tweet) if top_tweet is not None else None
            out.append(EmergingToken(
                token=token, chain=chain, score=score,
                unique_authors_24h=unique,
                weighted_24h=data["weighted"], raw_24h=data["raw"],
                sample_tweet_ids=list(data["tweet_ids"]),
                top_tweet_url=top_tweet_url,
            ))
            try:
                await db.insert_entity_mention(
                    entity_type="token", entity_term=f"{chain}:{token}",
                    cycle_ts=cycle_ts, weighted_count=data["weighted"],
                    raw_count=data["raw"], unique_authors=unique,
                )
            except Exception:  # noqa: BLE001
                log.exception("token_mention_insert_failed",
                              extra={"token_symbol": token, "chain_name": chain})

    out.sort(key=lambda e: e.score.composite, reverse=True)
    return out


async def _mean_weighted_history(
    db: Any, entity_type: str, entity_term: str, cycle_ts: datetime,
) -> float:
    """Mean weighted_count over the past 7 days from entity_mentions, excluding
    the current cycle. Returns 0.0 if no history."""
    cutoff = (cycle_ts - timedelta(days=7)).isoformat()
    current = cycle_ts.isoformat()
    sql = (
        "SELECT AVG(weighted_count) AS m FROM entity_mentions "
        "WHERE entity_type = ? AND entity_term = ? "
        "AND cycle_ts >= ? AND cycle_ts < ?"
    )
    async with db.conn.execute(sql, (entity_type, entity_term, cutoff, current)) as cur:
        row = await cur.fetchone()
    if not row or row["m"] is None:
        return 0.0
    return float(row["m"])


async def detect_entities(
    db: Any,
    sweep_pool: list[Any],
    allowlist: set[str],
    entity_type: str,
    dictionary_rows: list[dict],
    cycle_ts: datetime,
    threshold: float,
    cooc_graph: dict,
) -> list[EmergingEntity]:
    """Detect emerging sectors / venues / mechanisms."""
    if not dictionary_rows or not sweep_pool:
        return []

    def _extract(text: str) -> list[str]:
        return [term for term, _w in extractors.extract_dictionary_phrases(
            text, dictionary_rows,
        )]

    agg = _aggregate_mentions(sweep_pool, _extract, allowlist)
    out: list[EmergingEntity] = []
    for term, data in agg.items():
        unique = len(data["authors"])
        partners = cooc_graph.get((entity_type, term), [])
        mean_7d = await _mean_weighted_history(db, entity_type, term, cycle_ts)
        score = compute_entity_score(
            unique_authors_7d=unique,
            weighted_24h=data["weighted"],
            raw_24h=data["raw"],
            mean_weighted_7d=mean_7d,
            cooccurrence_partners=partners,
        )
        if score.composite < threshold:
            continue
        top_tweet = _select_top_tweet_for_entity(data["tweets"], allowlist)
        top_tweet_url = _tweet_url(top_tweet) if top_tweet is not None else None
        out.append(EmergingEntity(
            entity_type=entity_type, term=term, score=score,
            unique_authors_24h=unique,
            weighted_24h=data["weighted"], raw_24h=data["raw"],
            sample_tweet_ids=list(data["tweet_ids"]),
            top_tweet_url=top_tweet_url,
        ))
        try:
            await db.insert_entity_mention(
                entity_type=entity_type, entity_term=term,
                cycle_ts=cycle_ts, weighted_count=data["weighted"],
                raw_count=data["raw"], unique_authors=unique,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "entity_mention_insert_failed",
                extra={"entity_type": entity_type, "entity_term": term},
            )
    out.sort(key=lambda e: e.score.composite, reverse=True)
    return out


async def collect_venue_suggestions(
    db: Any,
    venues_results: list[EmergingEntity],
    cycle_ts: datetime,
) -> list[dict]:
    """For each venue in venues_results, check whether it has appeared in
    >= venue_suggest_min_cycles cycles with >= venue_suggest_min_unique_authors.
    Skip ones already accepted/blocked."""
    min_cycles = int(await db.get_setting("venue_suggest_min_cycles", "3") or 3)
    min_authors = int(await db.get_setting("venue_suggest_min_unique_authors", "5") or 5)

    suggestions: list[dict] = []
    for v in venues_results:
        if v.unique_authors_24h < min_authors:
            continue
        existing = await db.get_venue_suggestion_state(v.term)
        if existing and existing["status"] in ("accepted", "blocked"):
            continue
        n_cycles = await db.count_venue_recent_cycles(v.term, min_authors)
        if n_cycles < min_cycles:
            continue
        await db.set_venue_suggestion(v.term, "pending", cycle_ts.isoformat())
        suggestions.append({
            "venue_term": v.term,
            "n_cycles": n_cycles,
            "unique_authors": v.unique_authors_24h,
        })
    return suggestions


async def run_emerging(
    db: Any, x: Any, claude: Any, bot: Any, chat_id: int,
) -> dict:
    cycle_ts = datetime.now(UTC)
    threshold = float(await db.get_setting("emerging_entity_threshold", "1.5") or 1.5)
    allowlist = await db.get_allowlist()

    # Step 1: sweep
    sweep_pool, per_chain = await collect_sweep_pool(db, x)

    # Step 2: dictionaries
    sector_dict = await db.get_dictionary("sector")
    venue_dict = await db.get_dictionary("venue")
    mechanism_enabled = await db.get_setting_bool("mechanism_track_enabled", True)
    mechanism_dict = await db.get_dictionary("mechanism") if mechanism_enabled else []

    # Step 3: co-occurrence graph
    cooc_graph = await cooccurrence.build_cooccurrence_graph(
        db=db,
        sweep_pool=sweep_pool,
        allowlist=allowlist,
        sector_dict=sector_dict,
        venue_dict=venue_dict,
        mechanism_dict=mechanism_dict,
        cycle_ts=cycle_ts,
    )

    # Step 4-6: four tracks
    tokens_results = await detect_tokens(
        db, per_chain, allowlist, cycle_ts, threshold, cooc_graph,
        claude=claude,
    )
    sectors_results = await detect_entities(
        db, sweep_pool, allowlist, "sector", sector_dict,
        cycle_ts, threshold, cooc_graph,
    )
    venues_results = await detect_entities(
        db, sweep_pool, allowlist, "venue", venue_dict,
        cycle_ts, threshold, cooc_graph,
    )
    mechanisms_results: list[EmergingEntity] = []
    if mechanism_dict:
        mechanisms_results = await detect_entities(
            db, sweep_pool, allowlist, "mechanism", mechanism_dict,
            cycle_ts, threshold, cooc_graph,
        )

    # Step 6.5: Pattern proposal. Runs after structural emerging detection
    # (so we have the co-occurrence map) but BEFORE the strong-tier
    # judge — proposals feed into the judge's pattern_corpus few-shot.
    all_entity_keys: list[tuple[str, str]] = (
        [("token", t.token) for t in tokens_results]
        + [(e.entity_type, e.term) for e in sectors_results + venues_results + mechanisms_results]
    )
    pattern_proposals: list[dict] = []
    try:
        pattern_proposals = await patterns.propose_patterns(
            db=db,
            claude=claude,
            cycle_ts=cycle_ts,
            sweep_pool=sweep_pool,
            all_entities=all_entity_keys,
            sector_dict=sector_dict,
            venue_dict=venue_dict,
            mechanism_dict=mechanism_dict,
        )
    except Exception:  # noqa: BLE001
        log.exception("pattern_proposal_failed")

    # Step 7: three-tier convergence (weak / medium / strong).
    weak_thr = int(await db.get_setting("convergence_weak_threshold", "2") or 2)
    med_thr = int(await db.get_setting("convergence_medium_threshold", "3") or 3)
    strong_min_conf = int(
        await db.get_setting("convergence_strong_claude_min", "4") or 4
    )
    weak_cap = int(await db.get_setting("convergence_weak_cap_per_cycle", "15") or 15)
    med_cap = int(await db.get_setting("convergence_medium_cap_per_cycle", "10") or 10)
    strong_cap = int(
        await db.get_setting("convergence_strong_cap_per_cycle", "5") or 5
    )
    strong_enabled = await db.get_setting_bool("strong_convergence_enabled", True)
    viral_seeds = await db.get_viral_seed_examples()
    try:
        pattern_corpus = await db.get_patterns_for_few_shot(
            limit=int(await db.get_setting("pattern_few_shot_limit", "15") or 15),
        )
    except Exception:  # noqa: BLE001
        log.exception("pattern_corpus_fetch_failed")
        pattern_corpus = []

    top_tweet_by_entity: dict[tuple[str, str], str | None] = {}
    composite_by_entity: dict[tuple[str, str], float] = {}
    for t in tokens_results:
        top_tweet_by_entity[("token", t.token)] = t.top_tweet_url
        composite_by_entity[("token", t.token)] = t.score.composite
    for e in sectors_results + venues_results + mechanisms_results:
        top_tweet_by_entity[(e.entity_type, e.term)] = e.top_tweet_url
        composite_by_entity[(e.entity_type, e.term)] = e.score.composite

    candidate_entities: list[tuple[str, str]] = (
        [("token", t.token) for t in tokens_results[:10]]
        + [("sector", e.term) for e in sectors_results[:10]]
        + [("venue", e.term) for e in venues_results[:10]]
        + [("mechanism", e.term) for e in mechanisms_results[:10]]
    )

    weak_candidates: list[dict] = []
    medium_candidates: list[dict] = []
    for ent_type, ent_term in candidate_entities:
        partners = cooc_graph.get((ent_type, ent_term), [])
        result = await convergence.detect_convergence_for_entity(
            db=db,
            entity_type=ent_type, entity_term=ent_term,
            cooccurrence_partners=partners,
            sweep_pool=sweep_pool,
            sector_dict=sector_dict, venue_dict=venue_dict,
            mechanism_dict=mechanism_dict,
            cycle_ts=cycle_ts,
        )
        n_cats = int(result.get("count", 0))
        if n_cats < weak_thr:
            continue
        bucket = "medium" if n_cats >= med_thr else "weak"
        candidate = {
            "type": ent_type,
            "term": ent_term,
            "signal_count": n_cats,
            "signals": list(result.get("signals", [])),
            "evidence": dict(result.get("evidence", {})),
            "composite": float(composite_by_entity.get((ent_type, ent_term), 0.0)),
            "top_tweet_url": top_tweet_by_entity.get((ent_type, ent_term)),
            "co_occurs_with": [
                (pt, pterm) for (pt, pterm, _w) in partners[:6]
            ],
        }
        if bucket == "medium":
            medium_candidates.append(candidate)
        else:
            weak_candidates.append(candidate)

    weak_candidates.sort(key=lambda c: c["composite"], reverse=True)
    medium_candidates.sort(key=lambda c: c["composite"], reverse=True)
    weak_candidates = weak_candidates[:weak_cap]
    medium_candidates = medium_candidates[:med_cap]

    # Persist weak + medium tier rows up-front so the digest and any later
    # tier upgrade share the same convergence_events row count.
    weak_events: list[dict] = []
    medium_events: list[dict] = []
    medium_event_ids_by_key: dict[tuple[str, str], int] = {}

    async def _persist_event(
        candidate: dict, tier: str, conf: int | None, rationale: str | None,
    ) -> int | None:
        summary = convergence.build_convergence_summary(
            candidate["type"], candidate["term"],
            {"signals": candidate["signals"], "count": candidate["signal_count"]},
            tier, rationale,
        )
        candidate["summary"] = summary
        try:
            return await db.insert_convergence_event(
                cycle_ts=cycle_ts,
                entity_type=candidate["type"], entity_term=candidate["term"],
                tier=tier, signal_count=candidate["signal_count"],
                claude_confidence=conf,
                claude_rationale=rationale,
                summary=summary,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "convergence_event_insert_failed",
                extra={
                    "entity_type": candidate["type"],
                    "entity_term": candidate["term"],
                    "tier_name": tier,
                },
            )
            return None

    for cand in weak_candidates:
        await _persist_event(cand, "weak", None, None)
        weak_events.append({
            "type": cand["type"], "term": cand["term"], "tier": "weak",
            "signal_count": cand["signal_count"],
            "claude_confidence": None,
            "claude_rationale": None,
            "summary": cand.get("summary", ""),
            "top_tweet_url": cand.get("top_tweet_url"),
            "co_occurs_with": cand.get("co_occurs_with", []),
            "signals": cand.get("signals", []),
        })

    for cand in medium_candidates:
        event_id = await _persist_event(cand, "medium", None, None)
        if event_id:
            medium_event_ids_by_key[(cand["type"], cand["term"])] = event_id
        medium_events.append({
            "type": cand["type"], "term": cand["term"], "tier": "medium",
            "signal_count": cand["signal_count"],
            "claude_confidence": None,
            "claude_rationale": None,
            "summary": cand.get("summary", ""),
            "top_tweet_url": cand.get("top_tweet_url"),
            "co_occurs_with": cand.get("co_occurs_with", []),
            "signals": cand.get("signals", []),
        })

    # Strong tier: candidates are the top-K mediums; Claude judges them.
    strong_events: list[dict] = []
    if strong_enabled and claude is not None and medium_candidates:
        for cand in medium_candidates[:strong_cap]:
            try:
                strong = await convergence.detect_convergence_tier(
                    db=db, claude=claude,
                    entity_type=cand["type"], entity_term=cand["term"],
                    signal_count=cand["signal_count"],
                    evidence=cand["evidence"],
                    sweep_pool=sweep_pool,
                    sector_dict=sector_dict, venue_dict=venue_dict,
                    mechanism_dict=mechanism_dict,
                    viral_seeds=viral_seeds,
                    pattern_corpus=pattern_corpus,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "strong_judge_failed",
                    extra={
                        "entity_type": cand["type"],
                        "entity_term": cand["term"],
                    },
                )
                continue
            claude_confidence = strong.get("claude_confidence")
            claude_rationale = strong.get("claude_rationale")
            if (
                isinstance(claude_confidence, int)
                and claude_confidence >= strong_min_conf
            ):
                key = (cand["type"], cand["term"])
                event_id = medium_event_ids_by_key.get(key)
                summary = convergence.build_convergence_summary(
                    cand["type"], cand["term"],
                    {"signals": cand["signals"], "count": cand["signal_count"]},
                    "strong", claude_rationale,
                )
                if event_id is not None:
                    try:
                        await db.conn.execute(
                            "UPDATE convergence_events SET tier = 'strong', "
                            "claude_confidence = ?, claude_rationale = ?, "
                            "summary = ? WHERE id = ?",
                            (
                                int(claude_confidence), claude_rationale,
                                summary, int(event_id),
                            ),
                        )
                        await db.conn.commit()
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "convergence_event_strong_update_failed",
                            extra={
                                "entity_type": cand["type"],
                                "entity_term": cand["term"],
                            },
                        )
                strong_events.append({
                    "type": cand["type"], "term": cand["term"],
                    "tier": "strong",
                    "signal_count": cand["signal_count"],
                    "claude_confidence": claude_confidence,
                    "claude_rationale": claude_rationale,
                    "summary": summary,
                    "top_tweet_url": cand.get("top_tweet_url"),
                    "co_occurs_with": cand.get("co_occurs_with", []),
                    "signals": cand.get("signals", []),
                })
                # Remove the row from medium_events list (tier upgraded).
                medium_events = [
                    e for e in medium_events
                    if (e["type"], e["term"]) != key
                ]

    convergence_events: list[dict] = strong_events + medium_events + weak_events

    # Step 8: propose new dict terms via Claude
    new_dict_terms: list[tuple[str, str]] = []
    if claude is not None and sweep_pool:
        try:
            new_dict_terms = await dictionary.propose_and_add_new_dict_terms(
                db, claude, sweep_pool, allowlist,
                sector_dict, venue_dict, mechanism_dict,
            )
        except Exception:  # noqa: BLE001
            log.exception("propose_terms_failed")

    # Step 9: venue suggestions
    venue_suggestions = await collect_venue_suggestions(
        db, venues_results, cycle_ts,
    )

    # Step 10: pattern housekeeping (auto-bump organically-persistent;
    # age out stale unlabelled patterns).
    try:
        await patterns.housekeep_patterns(db)
    except Exception:  # noqa: BLE001
        log.exception("pattern_housekeep_failed")

    log.info(
        "emerging_done",
        extra={
            "n_tokens": len(tokens_results),
            "n_sectors": len(sectors_results),
            "n_venues": len(venues_results),
            "n_mechanisms": len(mechanisms_results),
            "n_convergence": len(convergence_events),
            "n_strong": len(strong_events),
            "n_medium": len(medium_events),
            "n_weak": len(weak_events),
            "n_new_dict_terms": len(new_dict_terms),
            "n_venue_suggestions": len(venue_suggestions),
        },
    )

    return {
        "cycle_ts": cycle_ts,
        "tokens": tokens_results,
        "sectors": sectors_results,
        "venues": venues_results,
        "mechanisms": mechanisms_results,
        "convergence_events": convergence_events,
        "convergence_strong": strong_events,
        "convergence_medium": medium_events,
        "convergence_weak": weak_events,
        "new_dict_terms": new_dict_terms,
        "venue_suggestions": venue_suggestions,
        "cooc_graph": cooc_graph,
        "sweep_pool": sweep_pool,
        "pattern_proposals": pattern_proposals,
    }
