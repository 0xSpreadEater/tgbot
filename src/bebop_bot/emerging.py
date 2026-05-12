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

from bebop_bot import convergence, cooccurrence, dictionary, extractors
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


@dataclass(frozen=True)
class EmergingEntity:
    entity_type: str
    term: str
    score: EntityScore
    unique_authors_24h: int
    weighted_24h: float
    raw_24h: int
    sample_tweet_ids: list[str] = field(default_factory=list)


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
    """Aggregate per-term: weighted_count, raw_count, unique_authors, sample_tweet_ids."""
    by_term: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "weighted": 0.0,
            "raw": 0,
            "authors": set(),
            "tweet_ids": [],
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
            if len(entry["tweet_ids"]) < 5:
                entry["tweet_ids"].append(tid)
    return by_term


async def detect_tokens(
    db: Any,
    per_chain: dict[str, list[Any]],
    allowlist: set[str],
    cycle_ts: datetime,
    threshold: float,
    cooc_graph: dict,
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
            out.append(EmergingToken(
                token=token, chain=chain, score=score,
                unique_authors_24h=unique,
                weighted_24h=data["weighted"], raw_24h=data["raw"],
                sample_tweet_ids=list(data["tweet_ids"]),
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
        out.append(EmergingEntity(
            entity_type=entity_type, term=term, score=score,
            unique_authors_24h=unique,
            weighted_24h=data["weighted"], raw_24h=data["raw"],
            sample_tweet_ids=list(data["tweet_ids"]),
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

    # Step 7: convergence (deterministic + Claude tier)
    conv_threshold = int(await db.get_setting("convergence_signal_threshold", "3") or 3)
    strong_enabled = await db.get_setting_bool("strong_convergence_enabled", True)
    strong_threshold = int(
        await db.get_setting("strong_convergence_claude_threshold", "4") or 4
    )
    viral_seeds = await db.get_viral_seed_examples()

    convergence_events: list[dict] = []
    candidate_entities: list[tuple[str, str]] = (
        [("token", t.token) for t in tokens_results[:10]]
        + [("sector", e.term) for e in sectors_results[:10]]
        + [("venue", e.term) for e in venues_results[:10]]
        + [("mechanism", e.term) for e in mechanisms_results[:10]]
    )

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
        if result["count"] < conv_threshold:
            continue

        tier = "convergence"
        claude_confidence = None
        claude_rationale = None
        if strong_enabled and claude is not None:
            strong = await convergence.detect_convergence_tier(
                db=db, claude=claude,
                entity_type=ent_type, entity_term=ent_term,
                signal_count=result["count"], evidence=result["evidence"],
                sweep_pool=sweep_pool,
                sector_dict=sector_dict, venue_dict=venue_dict,
                mechanism_dict=mechanism_dict,
                viral_seeds=viral_seeds,
            )
            claude_confidence = strong.get("claude_confidence")
            claude_rationale = strong.get("claude_rationale")
            if (
                claude_confidence is not None
                and isinstance(claude_confidence, int)
                and claude_confidence >= strong_threshold
            ):
                tier = "strong_convergence"

        summary = convergence.build_convergence_summary(
            ent_type, ent_term, result, tier, claude_rationale,
        )
        try:
            await db.insert_convergence_event(
                cycle_ts=cycle_ts,
                entity_type=ent_type, entity_term=ent_term,
                tier=tier, signal_count=result["count"],
                claude_confidence=claude_confidence,
                claude_rationale=claude_rationale,
                summary=summary,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "convergence_event_insert_failed",
                extra={"entity_type": ent_type, "entity_term": ent_term},
            )
        convergence_events.append({
            "type": ent_type, "term": ent_term, "tier": tier,
            "signal_count": result["count"],
            "claude_confidence": claude_confidence,
            "claude_rationale": claude_rationale,
            "summary": summary,
        })

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

    log.info(
        "emerging_done",
        extra={
            "n_tokens": len(tokens_results),
            "n_sectors": len(sectors_results),
            "n_venues": len(venues_results),
            "n_mechanisms": len(mechanisms_results),
            "n_convergence": len(convergence_events),
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
        "new_dict_terms": new_dict_terms,
        "venue_suggestions": venue_suggestions,
        "cooc_graph": cooc_graph,
    }
