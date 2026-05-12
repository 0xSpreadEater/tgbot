"""Per-cycle entity co-occurrence graph.

For each tweet, extract every entity present (tokens, sectors, venues,
mechanisms, known builder handles) and emit edges for every unordered
pair within that tweet. Edges accumulate weighted_count (sum of author
weights) and unique_authors across the cycle.
"""
from __future__ import annotations

import contextlib
import itertools
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

from bebop_bot import extractors

log = logging.getLogger(__name__)


def _author_weight(tweet: Any, allowlist: set[str]) -> float:
    handle = (getattr(tweet, "author_handle", "") or "").lower()
    if handle in allowlist:
        return 1.5
    if getattr(tweet, "author_verified", False):
        return 1.2
    return 1.0


def _extract_entities_in_tweet(
    tweet: Any,
    sector_dict: list[dict],
    venue_dict: list[dict],
    mechanism_dict: list[dict],
    known_handles: set[str],
) -> list[tuple[str, str]]:
    text = getattr(tweet, "text", "") or ""
    out: list[tuple[str, str]] = []

    for sym in extractors.extract_cashtags(text):
        out.append(("token", sym))
    for s, _w in extractors.extract_dictionary_phrases(text, sector_dict):
        out.append(("sector", s))
    for v, _w in extractors.extract_dictionary_phrases(text, venue_dict):
        out.append(("venue", v))
    for m, _w in extractors.extract_dictionary_phrases(text, mechanism_dict):
        out.append(("mechanism", m))
    for h in extractors.extract_builder_handles(text, known_handles):
        out.append(("handle", h))

    # Dedupe within a tweet
    seen = set()
    deduped = []
    for et, term in out:
        key = (et, term.lower() if et != "token" else term)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((et, term))
    return deduped


def _canonical_pair(
    a: tuple[str, str], b: tuple[str, str]
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Order pairs lexicographically by 'type:term' so edges are unique."""
    sa = f"{a[0]}:{a[1]}"
    sb = f"{b[0]}:{b[1]}"
    return (a, b) if sa < sb else (b, a)


async def build_cooccurrence_graph(
    db: Any,
    sweep_pool: list[Any],
    allowlist: set[str],
    sector_dict: list[dict],
    venue_dict: list[dict],
    mechanism_dict: list[dict],
    cycle_ts: datetime,
    known_handles: set[str] | None = None,
) -> dict[tuple[str, str], list[tuple[str, str, float]]]:
    """Return adjacency dict {(type, term): [(partner_type, partner_term,
    weighted_count), ...]}. Also persists edges to entity_cooccurrences.
    """
    if known_handles is None:
        try:
            known_handles = await db.get_viral_handles()
        except Exception:  # noqa: BLE001
            known_handles = set(extractors.KNOWN_BUILDER_HANDLES)

    # Per-edge accumulators
    edge_raw: dict[tuple, int] = defaultdict(int)
    edge_weighted: dict[tuple, float] = defaultdict(float)
    edge_authors: dict[tuple, set[str]] = defaultdict(set)

    for tweet in sweep_pool:
        entities = _extract_entities_in_tweet(
            tweet, sector_dict, venue_dict, mechanism_dict, known_handles,
        )
        if len(entities) < 2:
            continue
        weight = _author_weight(tweet, allowlist)
        handle = (getattr(tweet, "author_handle", "") or "").lower()
        for a, b in itertools.combinations(entities, 2):
            if a == b:
                continue
            (ea, eb) = _canonical_pair(a, b)
            key = (ea[0], ea[1], eb[0], eb[1])
            edge_raw[key] += 1
            edge_weighted[key] += weight
            edge_authors[key].add(handle)

    # Persist
    for key, raw_count in edge_raw.items():
        ea_t, ea_term, eb_t, eb_term = key
        try:
            await db.insert_cooccurrence(
                cycle_ts=cycle_ts,
                entity_a_type=ea_t,
                entity_a_term=ea_term,
                entity_b_type=eb_t,
                entity_b_term=eb_term,
                raw_count=raw_count,
                weighted_count=edge_weighted[key],
                unique_authors=len(edge_authors[key]),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "cooccurrence_insert_failed",
                extra={
                    "entity_a_type": ea_t, "entity_a_term": ea_term,
                    "entity_b_type": eb_t, "entity_b_term": eb_term,
                },
            )
    with contextlib.suppress(Exception):
        await db.commit()

    # Build adjacency from edges
    adj: dict[tuple[str, str], list[tuple[str, str, float]]] = defaultdict(list)
    for key, weighted in edge_weighted.items():
        ea_t, ea_term, eb_t, eb_term = key
        adj[(ea_t, ea_term)].append((eb_t, eb_term, weighted))
        adj[(eb_t, eb_term)].append((ea_t, ea_term, weighted))

    log.info(
        "cooccurrence_built",
        extra={
            "edges": len(edge_raw),
            "nodes": len(adj),
            "tweets_scanned": len(sweep_pool),
        },
    )
    return dict(adj)


def top_partners(
    adj: dict[tuple[str, str], list[tuple[str, str, float]]],
    entity_type: str,
    entity_term: str,
    limit: int = 4,
) -> list[tuple[str, str, float]]:
    """Top-N co-occurrence partners for a given entity, by weighted_count."""
    partners = adj.get((entity_type, entity_term), [])
    return sorted(partners, key=lambda x: x[2], reverse=True)[:limit]
