"""Phase 4 scoring: narrowness × quality × momentum × coherence.

All axes are on a 1-5 scale. Composite is the geometric mean (4th root
of the product). Coherence is new in Phase 4 and floors at 1.0 so old
behavior (no co-occurrence partners) reduces to a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass

from bebop_bot.extractors import KNOWN_BUILDER_HANDLES


@dataclass(frozen=True, slots=True)
class EntityScore:
    narrowness: float
    quality: float
    momentum: float
    coherence: float
    composite: float


def compute_narrowness(unique_authors_7d: int) -> float:
    """Penalize entities mentioned by very few or very many authors.

    Sweet spot: 3-15 unique authors → 5.0
        1 author → 2.0
        2 authors → 3.0
        3-15 authors → 5.0
        16-40 → 4.0
        41-100 → 3.0
        100+ → 2.0
        0 → 1.0
    """
    n = max(0, int(unique_authors_7d or 0))
    if n == 0:
        return 1.0
    if n == 1:
        return 2.0
    if n == 2:
        return 3.0
    if n <= 15:
        return 5.0
    if n <= 40:
        return 4.0
    if n <= 100:
        return 3.0
    return 2.0


def compute_quality(weighted_24h: float, raw_24h: int) -> float:
    """Author-weighted mentions vs raw mentions: high ratio = more trusted authors."""
    raw = max(1, int(raw_24h or 0))
    ratio = float(weighted_24h or 0.0) / raw
    if ratio >= 1.5:
        return 5.0
    if ratio >= 1.0:
        return 4.0
    if ratio >= 0.7:
        return 3.0
    if ratio >= 0.4:
        return 2.0
    return 1.0


def compute_momentum(weighted_24h: float, mean_weighted_7d: float) -> float:
    """Ratio of current 24h weighted mentions to 7d mean. Sharp uptick → high."""
    w = float(weighted_24h or 0.0)
    base = float(mean_weighted_7d or 0.0)
    if base <= 0.0:
        # No history — if there's any activity now, momentum is high.
        return 5.0 if w > 0 else 1.0
    ratio = w / base
    if ratio >= 3.0:
        return 5.0
    if ratio >= 2.0:
        return 4.0
    if ratio >= 1.3:
        return 3.0
    if ratio >= 0.8:
        return 2.0
    return 1.0


def compute_coherence(
    cooccurrence_partners: list[tuple[str, str, float]],
    known_builder_handles: set[str] | None = None,
) -> float:
    """Number of distinct legitimacy axes with at least one strong partner.

    cooccurrence_partners: [(partner_type, partner_term, weighted_count), ...]
    Floors at 1.0 for entities with no co-occurrence partners.
    """
    pool = (
        known_builder_handles
        if known_builder_handles is not None
        else KNOWN_BUILDER_HANDLES
    )
    axes_hit: set[str] = set()
    for ptype, pterm, wcnt in cooccurrence_partners or []:
        try:
            if float(wcnt) < 0.5:
                continue
        except (TypeError, ValueError):
            continue
        if ptype == "mechanism":
            axes_hit.add("mechanism")
        elif ptype == "venue":
            axes_hit.add("venue")
        elif ptype == "sector":
            axes_hit.add("sector")
        elif ptype == "handle" and (pterm or "").lower() in pool:
            axes_hit.add("known_builder")
        elif ptype == "token":
            axes_hit.add("other_token")
    n = len(axes_hit)
    if n == 0:
        return 1.0
    if n == 1:
        return 2.0
    if n == 2:
        return 3.0
    if n == 3:
        return 4.0
    return 5.0


def compute_entity_score(
    unique_authors_7d: int,
    weighted_24h: float,
    raw_24h: int,
    mean_weighted_7d: float,
    cooccurrence_partners: list[tuple[str, str, float]],
    known_builder_handles: set[str] | None = None,
) -> EntityScore:
    n = compute_narrowness(unique_authors_7d)
    q = compute_quality(weighted_24h, raw_24h)
    m = compute_momentum(weighted_24h, mean_weighted_7d)
    c = compute_coherence(cooccurrence_partners, known_builder_handles)
    product = n * q * m * c
    composite = product ** (1.0 / 4.0) if product > 0 else 0.0
    composite = min(5.0, composite)
    return EntityScore(n, q, m, c, composite)
