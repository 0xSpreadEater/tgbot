"""Claude-proposed patterns (Phase 4.7).

Free-form, observational track: each cycle Claude scans a dense
co-occurrence sample of recent tweets and proposes named patterns that
are NOT covered by the seven structural signal categories. Patterns are
persisted with a propose_count, accumulate observations across cycles,
and feed back into the strong-tier convergence judge as few-shot
context. User curates them with up/down labels.

This module owns:
  - propose_patterns(): the per-cycle proposal flow
  - housekeep_patterns(): weight auto-bumps + age-out
  - maybe_send_pattern_of_the_week(): weekly highlight
  - build_dense_cooccurrence_sample(): sample selection helper
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any

from bebop_bot import extractors

log = logging.getLogger(__name__)


def build_dense_cooccurrence_sample(
    sweep_pool: list[Any],
    all_entities: list[tuple[str, str]],
    sector_dict: list[dict] | None = None,
    venue_dict: list[dict] | None = None,
    mechanism_dict: list[dict] | None = None,
    n: int = 40,
) -> list[Any]:
    """Pick the n posts with the highest co-occurrence density.

    Score each post as:
      sum(1 for category fired in this post) +
      count of emerging entity terms in this post
    Sort desc; take top n. Tiebreak by like_count desc.
    """
    if not sweep_pool:
        return []
    emerging_terms: set[str] = {
        f"{t.lower()}:{term.lower()}" for (t, term) in (all_entities or [])
    }
    sector_dict = sector_dict or []
    venue_dict = venue_dict or []
    mechanism_dict = mechanism_dict or []

    def _density(t: Any) -> tuple[int, int, int]:
        text = getattr(t, "text", "") or ""
        if not text:
            return (0, 0, 0)
        categories_fired = 0
        if extractors.detect_composition_language(text):
            categories_fired += 1
        if extractors.detect_fair_launch_language(text):
            categories_fired += 1
        if extractors.detect_backing_event(text):
            categories_fired += 1
        if extractors.detect_deploy_language(text) or extractors.detect_ape_language(text):
            categories_fired += 1

        entity_hits = 0
        for sym in extractors.extract_cashtags(text):
            if f"token:{sym.lower()}" in emerging_terms:
                entity_hits += 1
        for dct, etype in (
            (sector_dict, "sector"),
            (venue_dict, "venue"),
            (mechanism_dict, "mechanism"),
        ):
            for term, _w in extractors.extract_dictionary_phrases(text, dct):
                if f"{etype}:{term.lower()}" in emerging_terms:
                    entity_hits += 1
        likes = int(getattr(t, "like_count", 0) or 0)
        return (categories_fired + entity_hits, categories_fired, likes)

    scored = [(t, _density(t)) for t in sweep_pool]
    scored.sort(key=lambda kv: kv[1], reverse=True)
    out: list[Any] = []
    for t, (score, _cats, _likes) in scored:
        if score <= 0:
            break
        out.append(t)
        if len(out) >= int(n):
            break
    return out


async def propose_patterns(
    db: Any,
    claude: Any,
    cycle_ts: datetime,
    sweep_pool: list[Any],
    all_entities: list[tuple[str, str]],
    sector_dict: list[dict] | None = None,
    venue_dict: list[dict] | None = None,
    mechanism_dict: list[dict] | None = None,
) -> list[dict]:
    """Drive Claude's pattern-proposal pass once per cycle.

    Returns the list of proposals (with `is_new` set for rows that were
    inserted vs bumped) for digest rendering. Persistence side-effects
    happen as a side effect of this call.
    """
    if claude is None or not sweep_pool:
        return []
    try:
        cap = int(await db.get_setting("pattern_proposals_per_cycle", "3") or 3)
    except Exception:  # noqa: BLE001
        cap = 3
    if cap <= 0:
        return []

    try:
        existing = await db.get_patterns_active(exclude_down=True, limit=50)
    except Exception:  # noqa: BLE001
        log.exception("propose_patterns_existing_fetch_failed")
        existing = []

    sample = build_dense_cooccurrence_sample(
        sweep_pool=sweep_pool,
        all_entities=all_entities,
        sector_dict=sector_dict,
        venue_dict=venue_dict,
        mechanism_dict=mechanism_dict,
        n=40,
    )
    if not sample:
        return []

    try:
        raw_proposals = await claude.propose_patterns(
            sample=sample, existing_patterns=existing, cap=cap,
        )
    except Exception:  # noqa: BLE001
        log.exception("propose_patterns_claude_call_failed")
        return []

    out: list[dict] = []
    for p in raw_proposals or []:
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        try:
            existing_id = await db.find_pattern_by_name(name)
        except Exception:  # noqa: BLE001
            log.exception(
                "find_pattern_by_name_failed",
                extra={"pattern_name": name},
            )
            existing_id = None
        is_new = False
        if existing_id:
            try:
                await db.bump_pattern(
                    existing_id, cycle_ts,
                    confidence=int(p.get("confidence", 1)),
                    supporting_tweet_ids=list(p.get("tweet_ids", [])),
                    anchor_entities=list(p.get("anchors", [])),
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "bump_pattern_failed",
                    extra={"pattern_name": name, "pattern_id": existing_id},
                )
        else:
            try:
                await db.insert_pattern(
                    name=name,
                    description=str(p.get("description") or ""),
                    cycle_ts=cycle_ts,
                    confidence=int(p.get("confidence", 1)),
                    supporting_tweet_ids=list(p.get("tweet_ids", [])),
                    anchor_entities=list(p.get("anchors", [])),
                )
                is_new = True
            except Exception:  # noqa: BLE001
                log.exception(
                    "insert_pattern_failed",
                    extra={"pattern_name": name},
                )
                continue
        out.append({
            "name": name,
            "description": str(p.get("description") or ""),
            "confidence": int(p.get("confidence", 1)),
            "tweet_ids": list(p.get("tweet_ids", [])),
            "anchors": list(p.get("anchors", [])),
            "is_new": is_new,
        })
    log.info(
        "patterns_proposed",
        extra={
            "proposal_count": len(out),
            "sample_size": len(sample),
            "existing_pattern_count": len(existing),
        },
    )
    return out


async def housekeep_patterns(db: Any) -> dict[str, int]:
    """Auto-bump organically-persistent patterns; age out stale ones.

    Safe to call once per cycle, after propose_patterns.
    """
    try:
        result = await db.housekeep_patterns()
    except Exception:  # noqa: BLE001
        log.exception("housekeep_patterns_failed")
        return {"bumped": 0, "deleted": 0}
    log.info("patterns_housekept", extra={
        "bumped_count": int(result.get("bumped", 0)),
        "deleted_count": int(result.get("deleted", 0)),
    })
    return result


def format_pattern_detail(pattern: dict, observations: list[dict]) -> str:
    """HTML detail for /patterns show NAME and the pat:show button."""
    name = html.escape(str(pattern.get("name") or ""))
    desc = html.escape(str(pattern.get("description") or ""))
    weight = float(pattern.get("weight", 1.0) or 1.0)
    propose_count = int(pattern.get("propose_count", 0) or 0)
    label = pattern.get("user_label") or "—"
    lines = [
        f"<b>{name}</b>",
        f"<i>{desc}</i>",
        "",
        f"weight: {weight:.1f}, proposed {propose_count}x, "
        f"label: {html.escape(str(label))}",
    ]
    if observations:
        lines.append("")
        lines.append("<b>Recent observations</b>")
        for o in observations[:8]:
            cycle = html.escape(str(o.get("cycle_ts") or ""))
            conf = int(o.get("confidence", 0) or 0)
            anchors = o.get("anchor_entities") or []
            anchor_str = ", ".join(
                f"{html.escape(str(a[0]))}:{html.escape(str(a[1]))}"
                for a in anchors[:5]
            )
            lines.append(
                f"• {cycle} — conf {conf}/5 — anchors: {anchor_str or '(none)'}"
            )
    return "\n".join(lines)


async def maybe_send_pattern_of_the_week(
    bot: Any, chat_id: int, db: Any,
) -> dict | None:
    """Highlight the pattern with the most recent observations in the
    lookback window, IF there's a clear weekly winner. Silent otherwise.
    """
    try:
        lookback = int(
            await db.get_setting("pattern_of_the_week_lookback_days", "7") or 7
        )
    except Exception:  # noqa: BLE001
        lookback = 7
    try:
        winner = await db.pattern_of_week_winner(lookback)
    except Exception:  # noqa: BLE001
        log.exception("pattern_of_week_failed")
        return None
    if not winner:
        return None
    name = html.escape(str(winner.get("name") or ""))
    desc = html.escape(str(winner.get("description") or ""))
    count = int(winner.get("recent_propose_count", 0) or 0)
    msg = (
        "<b>📌 Pattern of the week</b>\n\n"
        f"<b>{name}</b> "
        f"<i>(seen {count}x in last {lookback} days)</i>\n"
        f"<i>{desc}</i>\n\n"
        "<i>This pattern keeps showing up. Consider whether it's worth "
        "promoting to a full structural detector in a future phase.</i>"
    )
    try:
        await bot.send_message(
            chat_id=chat_id, text=msg, parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001
        log.exception("pattern_of_week_send_failed", extra={"pattern_name": name})
        return None
    return dict(winner)
