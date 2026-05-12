"""Dictionary CRUD + housekeeping for sector / venue / mechanism dictionaries.

The dictionaries themselves live in their respective tables
(sector_dictionary, venue_dictionary, mechanism_dictionary). This module
provides a uniform interface for the orchestrator and handlers.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

KNOWN_TYPES = ("sector", "venue", "mechanism")


async def list_dictionary(db: Any, entity_type: str) -> list[dict]:
    if entity_type not in KNOWN_TYPES:
        return []
    return await db.get_dictionary(entity_type)


async def add_term(
    db: Any,
    entity_type: str,
    term: str,
    display_name: str | None = None,
    weight: float = 1.0,
    source: str = "user_added",
    is_novelty_marker: bool = False,
) -> bool:
    if entity_type not in KNOWN_TYPES:
        return False
    term = (term or "").strip()
    if not term:
        return False
    added = await db.add_dictionary_term(
        entity_type=entity_type,
        term=term,
        display_name=display_name,
        weight=weight,
        source=source,
        is_novelty_marker=is_novelty_marker,
    )
    log.info(
        "dictionary_add",
        extra={
            "entity_type": entity_type,
            "entity_term": term,
            "added": added,
            "source": source,
        },
    )
    return added


async def remove_term(db: Any, entity_type: str, term: str) -> bool:
    if entity_type not in KNOWN_TYPES:
        return False
    term = (term or "").strip()
    if not term:
        return False
    removed = await db.remove_dictionary_term(entity_type, term)
    log.info(
        "dictionary_remove",
        extra={
            "entity_type": entity_type,
            "entity_term": term,
            "removed": removed,
        },
    )
    return removed


async def propose_and_add_new_dict_terms(
    db: Any,
    claude: Any,
    sweep_pool: list[Any],
    allowlist: set[str],
    sector_dict: list[dict],
    venue_dict: list[dict],
    mechanism_dict: list[dict],
) -> list[tuple[str, str]]:
    """Ask Claude to propose new terms for each of the three dictionaries
    and persist them with source='claude_proposed' and weight=0.5 (so
    they're discoverable but lightly weighted)."""
    if not sweep_pool or claude is None:
        return []
    # Build a small sample of representative tweets.
    sample = []
    for tw in sweep_pool[:80]:
        text = getattr(tw, "text", "") or ""
        handle = getattr(tw, "author_handle", "") or ""
        if text:
            sample.append({"text": text[:280], "handle": handle})

    existing = {
        "sectors": [e["term"] for e in sector_dict],
        "venues": [e["term"] for e in venue_dict],
        "mechanisms": [e["term"] for e in mechanism_dict],
    }

    try:
        proposed = await claude.propose_dictionary_terms(
            sample=sample,
            existing_sectors=existing["sectors"],
            existing_venues=existing["venues"],
            existing_mechanisms=existing["mechanisms"],
        )
    except Exception:  # noqa: BLE001
        log.exception("propose_dictionary_terms_failed")
        return []

    added: list[tuple[str, str]] = []
    for entry in proposed or []:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            continue
        ent_type, term = entry
        if ent_type not in KNOWN_TYPES:
            continue
        ok = await add_term(
            db,
            entity_type=ent_type,
            term=term,
            weight=0.5,
            source="claude_proposed",
        )
        if ok:
            added.append((ent_type, term))
    log.info("dict_terms_proposed", extra={"added": len(added)})
    return added
