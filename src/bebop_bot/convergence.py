"""Two-tier convergence detection.

Tier 1 (deterministic): count how many of seven signal categories fire
for an entity in a single cycle. If count >= threshold, mark as
'convergence'.

Tier 2 (Claude-judged): when Tier 1 fires and strong_convergence is
enabled, ask Claude to score the precursor pattern against viral seed
examples. If Claude confidence >= threshold, upgrade to
'strong_convergence'.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from bebop_bot import extractors

log = logging.getLogger(__name__)

NOVELTY_WINDOW_DAYS = 14
VENUE_NEW_WINDOW_DAYS = 30


def _tweet_text(t: Any) -> str:
    return getattr(t, "text", "") or ""


def _tweet_handle(t: Any) -> str:
    return (getattr(t, "author_handle", "") or "").lower()


def _tweet_id(t: Any) -> str:
    return str(getattr(t, "id", "") or "")


def _entity_matches_tweet(
    entity_type: str, entity_term: str, tweet: Any,
    sector_dict: list[dict], venue_dict: list[dict],
    mechanism_dict: list[dict],
) -> bool:
    text = _tweet_text(tweet)
    if not text:
        return False
    if entity_type == "token":
        symbols = set(extractors.extract_cashtags(text))
        if entity_term.upper() in symbols:
            return True
        addrs = [a.lower() for a in extractors.extract_evm_addresses(text)]
        return entity_term.lower() in addrs
    dicts = {
        "sector": sector_dict,
        "venue": venue_dict,
        "mechanism": mechanism_dict,
    }
    d = dicts.get(entity_type, [])
    # Match if entity_term shows up via dictionary extraction
    for term, _w in extractors.extract_dictionary_phrases(text, d):
        if term.lower() == entity_term.lower():
            return True
    return False


def _tweets_mentioning(
    entity_type: str, entity_term: str, sweep_pool: list[Any],
    sector_dict: list[dict], venue_dict: list[dict],
    mechanism_dict: list[dict],
) -> list[Any]:
    return [
        t for t in sweep_pool
        if _entity_matches_tweet(
            entity_type, entity_term, t, sector_dict, venue_dict, mechanism_dict,
        )
    ]


# ---------------------------------------------------------------------------
# Signal functions
# ---------------------------------------------------------------------------

async def signal_novel_mechanism(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    novelty_terms: set[str] = set()
    for entry in mechanism_dict or []:
        if entry.get("is_novelty_marker"):
            novelty_terms.add(entry["term"].lower())

    matching_partners: list[str] = []
    for ptype, pterm, _wcnt in cooccurrence_partners or []:
        if ptype != "mechanism":
            continue
        if pterm.lower() in novelty_terms:
            matching_partners.append(pterm)
            continue
        # Recently first-seen mechanism
        first = await db.get_entity_first_seen("mechanism", pterm)
        if first:
            try:
                first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
                if first_dt.tzinfo is not None:
                    first_dt = first_dt.replace(tzinfo=None)
                if (cycle_ts.replace(tzinfo=None) - first_dt) <= timedelta(days=NOVELTY_WINDOW_DAYS):
                    matching_partners.append(pterm)
            except (ValueError, TypeError):
                pass
    if not matching_partners:
        return None
    return {"phrases": sorted(set(matching_partners))}


async def signal_known_builder(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    try:
        known = await db.get_viral_handles()
    except Exception:  # noqa: BLE001
        known = set(extractors.KNOWN_BUILDER_HANDLES)
    handles = [
        pterm for ptype, pterm, _ in cooccurrence_partners or []
        if ptype == "handle" and pterm.lower() in known
    ]
    if not handles:
        return None
    return {"handles": sorted(set(h.lower() for h in handles))}


async def signal_new_venue_context(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    venue_weight: dict[str, float] = {
        e["term"].lower(): float(e.get("weight", 1.0) or 1.0)
        for e in venue_dict or []
    }
    matched: list[str] = []
    for ptype, pterm, _ in cooccurrence_partners or []:
        if ptype != "venue":
            continue
        w = venue_weight.get(pterm.lower(), 1.0)
        if w < 1.0:
            matched.append(pterm)
            continue
        first = await db.get_entity_first_seen("venue", pterm)
        if first:
            try:
                first_dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
                if first_dt.tzinfo is not None:
                    first_dt = first_dt.replace(tzinfo=None)
                if (cycle_ts.replace(tzinfo=None) - first_dt) <= timedelta(days=VENUE_NEW_WINDOW_DAYS):
                    matched.append(pterm)
            except (ValueError, TypeError):
                pass
    if not matched:
        return None
    return {"phrases": sorted(set(matched))}


async def signal_recursive_lang(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    tweets = _tweets_mentioning(
        entity_type, entity_term, sweep_pool, sector_dict, venue_dict, mechanism_dict,
    )
    phrases: set[str] = set()
    tweet_ids: list[str] = []
    for t in tweets:
        hits = extractors.detect_composition_language(_tweet_text(t))
        if hits:
            phrases.update(hits)
            tweet_ids.append(_tweet_id(t))
    if not phrases:
        return None
    return {"phrases": sorted(phrases), "tweet_ids": tweet_ids[:5]}


async def signal_fair_launch_lang(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    tweets = _tweets_mentioning(
        entity_type, entity_term, sweep_pool, sector_dict, venue_dict, mechanism_dict,
    )
    phrases: set[str] = set()
    tweet_ids: list[str] = []
    for t in tweets:
        hits = extractors.detect_fair_launch_language(_tweet_text(t))
        if hits:
            phrases.update(hits)
            tweet_ids.append(_tweet_id(t))
    if not phrases:
        return None
    return {"phrases": sorted(phrases), "tweet_ids": tweet_ids[:5]}


async def signal_backing_event(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    tweets = _tweets_mentioning(
        entity_type, entity_term, sweep_pool, sector_dict, venue_dict, mechanism_dict,
    )
    matches: list[str] = []
    tweet_ids: list[str] = []
    for t in tweets:
        hits = extractors.detect_backing_event(_tweet_text(t))
        if hits:
            matches.extend(hits)
            tweet_ids.append(_tweet_id(t))
    if not matches:
        return None
    return {"phrases": list(dict.fromkeys(matches))[:5], "tweet_ids": tweet_ids[:5]}


async def signal_builder_ape_overlap(
    db: Any, entity_type: str, entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any], mechanism_dict: list[dict],
    sector_dict: list[dict], venue_dict: list[dict],
    cycle_ts: datetime,
) -> dict | None:
    tweets = _tweets_mentioning(
        entity_type, entity_term, sweep_pool, sector_dict, venue_dict, mechanism_dict,
    )
    # (a) single-tweet overlap
    single_hits: list[str] = []
    deploy_authors: set[str] = set()
    ape_authors: set[str] = set()
    deploy_phrases: set[str] = set()
    ape_phrases: set[str] = set()
    for t in tweets:
        text = _tweet_text(t)
        handle = _tweet_handle(t)
        deploys = extractors.detect_deploy_language(text)
        apes = extractors.detect_ape_language(text)
        if deploys and apes:
            single_hits.append(_tweet_id(t))
        if deploys:
            deploy_authors.add(handle)
            deploy_phrases.update(deploys)
        if apes:
            ape_authors.add(handle)
            ape_phrases.update(apes)
    cross_author = (
        deploy_authors and ape_authors
        and (deploy_authors != ape_authors or len(deploy_authors | ape_authors) > 1)
    )
    if not single_hits and not cross_author:
        return None
    return {
        "single_tweet_overlap": single_hits[:3],
        "deploy_phrases": sorted(deploy_phrases)[:5],
        "ape_phrases": sorted(ape_phrases)[:5],
        "deploy_authors": sorted(deploy_authors)[:5],
        "ape_authors": sorted(ape_authors)[:5],
    }


ALL_SIGNALS = [
    ("novel_mechanism", signal_novel_mechanism),
    ("known_builder", signal_known_builder),
    ("new_venue_context", signal_new_venue_context),
    ("recursive_lang", signal_recursive_lang),
    ("fair_launch_lang", signal_fair_launch_lang),
    ("backing_event", signal_backing_event),
    ("builder_ape_overlap", signal_builder_ape_overlap),
]


async def detect_convergence_for_entity(
    db: Any,
    entity_type: str,
    entity_term: str,
    cooccurrence_partners: list[tuple[str, str, float]],
    sweep_pool: list[Any],
    sector_dict: list[dict],
    venue_dict: list[dict],
    mechanism_dict: list[dict],
    cycle_ts: datetime,
) -> dict:
    fired: list[str] = []
    evidence: dict[str, dict] = {}
    for name, fn in ALL_SIGNALS:
        try:
            result = await fn(
                db, entity_type, entity_term, cooccurrence_partners,
                sweep_pool, mechanism_dict, sector_dict, venue_dict, cycle_ts,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "convergence_signal_error",
                extra={
                    "signal_name": name,
                    "entity_type": entity_type,
                    "entity_term": entity_term,
                },
            )
            result = None
        if result:
            fired.append(name)
            evidence[name] = result
            try:
                await db.insert_convergence_signal(
                    cycle_ts=cycle_ts,
                    entity_type=entity_type,
                    entity_term=entity_term,
                    signal_name=name,
                    evidence_json=json.dumps(result),
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "convergence_signal_insert_failed",
                    extra={
                        "signal_name": name,
                        "entity_type": entity_type,
                        "entity_term": entity_term,
                    },
                )
    return {"signals": fired, "count": len(fired), "evidence": evidence}


def _representative_tweets(
    entity_type: str, entity_term: str, sweep_pool: list[Any],
    sector_dict: list[dict], venue_dict: list[dict],
    mechanism_dict: list[dict], limit: int = 8,
) -> list[Any]:
    matched = _tweets_mentioning(
        entity_type, entity_term, sweep_pool, sector_dict, venue_dict, mechanism_dict,
    )
    return matched[:limit]


async def detect_convergence_tier(
    db: Any,
    claude: Any,
    entity_type: str,
    entity_term: str,
    signal_count: int,
    evidence: dict,
    sweep_pool: list[Any],
    sector_dict: list[dict],
    venue_dict: list[dict],
    mechanism_dict: list[dict],
    viral_seeds: list[dict],
    pattern_corpus: list[dict] | None = None,
) -> dict:
    """Tier 2: ask Claude to judge whether the pattern resembles known
    viral precursors. Returns dict with claude_confidence and rationale.

    pattern_corpus, when supplied, is the Claude-proposed pattern corpus
    (Phase 4.7); the judge sees it as additional few-shot context.
    """
    sample = _representative_tweets(
        entity_type, entity_term, sweep_pool, sector_dict, venue_dict, mechanism_dict,
    )
    try:
        judgment = await claude.judge_strong_convergence(
            entity_type=entity_type,
            entity_term=entity_term,
            signals_fired=list(evidence.keys()),
            evidence=evidence,
            viral_seeds=viral_seeds,
            sample_tweets=sample,
            pattern_corpus=pattern_corpus or [],
        )
    except TypeError:
        # Fallback for older clients without pattern_corpus kwarg.
        try:
            judgment = await claude.judge_strong_convergence(
                entity_type=entity_type,
                entity_term=entity_term,
                signals_fired=list(evidence.keys()),
                evidence=evidence,
                viral_seeds=viral_seeds,
                sample_tweets=sample,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "judge_strong_convergence_failed",
                extra={"entity_type": entity_type, "entity_term": entity_term},
            )
            judgment = {"confidence": None, "rationale": None}
    except Exception:  # noqa: BLE001
        log.exception(
            "judge_strong_convergence_failed",
            extra={"entity_type": entity_type, "entity_term": entity_term},
        )
        judgment = {"confidence": None, "rationale": None}

    return {
        "claude_confidence": judgment.get("confidence"),
        "claude_rationale": judgment.get("rationale"),
    }


def build_convergence_summary(
    entity_type: str,
    entity_term: str,
    result: dict,
    tier: str,
    claude_rationale: str | None,
) -> str:
    fired = result.get("signals", [])
    body = ", ".join(fired) or "no signals"
    label_map = {
        "strong": "STRONG",
        "strong_convergence": "STRONG",
        "medium": "convergence",
        "convergence": "convergence",
        "weak": "weak",
    }
    label = label_map.get(tier, "convergence")
    summary = f"{label}: {entity_type}:{entity_term} fired {len(fired)}/7 signals ({body})."
    if claude_rationale:
        summary += f" Claude: {claude_rationale}"
    return summary[:600]
