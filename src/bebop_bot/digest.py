import html
import json
import logging
from datetime import UTC, datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from bebop_bot.models import ScoredTweet

TIER_SYMBOLS = {"weak": "⚠", "medium": "⚠️", "strong": "⚠️⚠️"}

log = logging.getLogger(__name__)

TELEGRAM_MAX = 4000
POST_TEXT_LIMIT = 320


def relative_time(dt: datetime, now: datetime | None = None) -> str:
    if dt is None:
        return ""
    now = now or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _format_post(st: ScoredTweet, now: datetime) -> str:
    t = st.tweet
    handle = html.escape(t.author_handle)
    url = html.escape(t.url, quote=True)
    composite = st.score.composite
    likes = t.like_count
    replies = t.reply_count
    retweets = t.retweet_count
    rel = relative_time(t.created_at, now=now)
    text = t.text
    if len(text) > POST_TEXT_LIMIT:
        text = text[: POST_TEXT_LIMIT - 1] + "…"
    body = html.escape(text)
    auto_marker = " *" if st.auto_included_reason else ""
    return (
        f'<a href="{url}">@{handle}</a> score {composite:.1f}{auto_marker}\n'
        f"{likes}❤ {replies}\U0001f4ac {retweets}\U0001f501 {rel}\n\n"
        f"{body}"
    )


def _format_topic_block(
    topic_name: str, summary: str, posts: list[ScoredTweet], now: datetime
) -> str:
    """Kept for backwards compat with tests. Phase 3 sends per-post messages."""
    header = f"<b>{html.escape(topic_name)}</b> ({len(posts)})"
    parts = [header]
    if summary:
        parts.append(f"<i>{html.escape(summary)}</i>")
    for post in posts:
        parts.append(_format_post(post, now))
    return "\n".join(parts)


def _split_messages(header: str, blocks: list[str]) -> list[str]:
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = current + "\n\n" + block if current else block
        if len(candidate) > TELEGRAM_MAX and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _post_keyboard(tweet_id: str, handle: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Insight", callback_data=f"u:{tweet_id}"),
                InlineKeyboardButton("Noise", callback_data=f"d:{tweet_id}"),
                InlineKeyboardButton(f"Mute @{handle} 30d", callback_data=f"m:{tweet_id}"),
            ]
        ]
    )


async def _send_plain(bot: Any, chat_id: int, text: str) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _send_post(
    bot: Any,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


def _format_cooccurs_line(co_occurs_with: list) -> str:
    if not co_occurs_with:
        return ""
    parts = []
    for entry in co_occurs_with[:5]:
        if not entry:
            continue
        if len(entry) >= 2:
            ptype, pterm = entry[0], entry[1]
        else:
            continue
        parts.append(f"{html.escape(str(pterm))} ({html.escape(str(ptype))})")
    if not parts:
        return ""
    return "<i>Co-occurs with: " + ", ".join(parts) + "</i>"


def _convergence_keyboard(tier: str, entity_type: str, entity_term: str) -> InlineKeyboardMarkup:
    """Standard convergence-event keyboard. Compact: just an Insight/Noise pair
    keyed off the tier+entity."""
    term_safe = html.escape(entity_term, quote=False)[:30]
    cb_up = f"cv:{tier}:{entity_type}:{term_safe}:up"
    cb_down = f"cv:{tier}:{entity_type}:{term_safe}:down"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Insight", callback_data=cb_up),
        InlineKeyboardButton("Noise", callback_data=cb_down),
    ]])


async def send_convergence_strong_block(
    bot: Any, chat_id: int, strong: list[dict],
) -> None:
    if not strong:
        return
    sym = TIER_SYMBOLS["strong"]
    lines = [f"<b>{sym} Strong convergence ({len(strong)})</b>"]
    for e in strong:
        lines.append("")
        conf = e.get("claude_confidence")
        conf_str = f"Claude confidence {conf}/5, " if conf is not None else ""
        lines.append(
            f"<b>{html.escape(str(e.get('type','')))}: "
            f"{html.escape(str(e.get('term','')))}</b> — "
            f"{conf_str}{e.get('signal_count', 0)}/7 signals"
        )
        lines.append(f"<i>{html.escape(str(e.get('summary','')))}</i>")
        co = _format_cooccurs_line(e.get("co_occurs_with") or [])
        if co:
            lines.append(co)
        top_url = e.get("top_tweet_url")
        if top_url:
            lines.append(
                f'▸ <a href="{html.escape(str(top_url), quote=True)}">top tweet</a>'
            )
    await _send_plain(bot, chat_id, "\n".join(lines))


async def send_convergence_medium_block(
    bot: Any, chat_id: int, medium: list[dict],
) -> None:
    if not medium:
        return
    sym = TIER_SYMBOLS["medium"]
    lines = [f"<b>{sym} Convergence ({len(medium)})</b>"]
    for e in medium:
        lines.append("")
        lines.append(
            f"<b>{html.escape(str(e.get('type','')))}: "
            f"{html.escape(str(e.get('term','')))}</b> — "
            f"{e.get('signal_count', 0)}/7 signals"
        )
        lines.append(f"<i>{html.escape(str(e.get('summary','')))}</i>")
        co = _format_cooccurs_line(e.get("co_occurs_with") or [])
        if co:
            lines.append(co)
        top_url = e.get("top_tweet_url")
        if top_url:
            lines.append(
                f'▸ <a href="{html.escape(str(top_url), quote=True)}">top tweet</a>'
            )
    await _send_plain(bot, chat_id, "\n".join(lines))


async def send_convergence_weak_block(
    bot: Any, chat_id: int, weak: list[dict],
) -> None:
    """Compact rendering: no co-occurs line (saves vertical space)."""
    if not weak:
        return
    sym = TIER_SYMBOLS["weak"]
    lines = [
        f"<b>{sym} Weak convergence ({len(weak)})</b>",
        "<i>2+ precursor categories aligned. Worth a glance.</i>",
    ]
    for e in weak[:15]:
        lines.append("")
        lines.append(
            f"<b>{html.escape(str(e.get('type','')))}: "
            f"{html.escape(str(e.get('term','')))}</b> — "
            f"{e.get('signal_count', 0)}/7 signals"
        )
        signals = e.get("signals") or []
        if signals:
            joined = ", ".join(html.escape(str(s)) for s in signals[:5])
            lines.append(f"<i>signals: {joined}</i>")
        top_url = e.get("top_tweet_url")
        if top_url:
            lines.append(
                f'▸ <a href="{html.escape(str(top_url), quote=True)}">top tweet</a>'
            )
    await _send_plain(bot, chat_id, "\n".join(lines))


async def send_convergence_block(
    bot: Any, chat_id: int, convergence_events: list[dict],
) -> None:
    """Backwards-compatible single-call rendering. Splits the events into
    three tier buckets and routes them through the new per-tier renderers.

    Accepts both Phase 4.7 tiers ('strong'/'medium'/'weak') and legacy
    Phase 4 tiers ('strong_convergence'/'convergence') as synonyms.
    """
    if not convergence_events:
        return

    def _bucket(tier: str) -> str:
        if tier in ("strong", "strong_convergence"):
            return "strong"
        if tier in ("medium", "convergence", "structural"):
            return "medium"
        return tier

    strong = [e for e in convergence_events if _bucket(e.get("tier") or "") == "strong"]
    medium = [e for e in convergence_events if _bucket(e.get("tier") or "") == "medium"]
    weak = [e for e in convergence_events if _bucket(e.get("tier") or "") == "weak"]

    await send_convergence_strong_block(bot, chat_id, strong)
    await send_convergence_medium_block(bot, chat_id, medium)
    await send_convergence_weak_block(bot, chat_id, weak)


def _cooc_partner_line(
    cooc_graph: dict, entity_type: str, entity_term: str, limit: int = 4,
) -> str:
    """Compact display: top co-occurrence partners under each entity."""
    partners = cooc_graph.get((entity_type, entity_term), []) if cooc_graph else []
    ranked = sorted(partners, key=lambda x: x[2], reverse=True)[:limit]
    if not ranked:
        return ""
    parts = []
    for ptype, pterm, _w in ranked:
        parts.append(f"{html.escape(pterm)} ({html.escape(ptype)})")
    return "<i>Co-occurs with: " + ", ".join(parts) + "</i>"


async def send_emerging_track(
    bot: Any,
    chat_id: int,
    track_label: str,
    entries: list[Any],
    cooc_graph: dict | None,
) -> None:
    """Render one emerging track (tokens / sectors / venues / mechanisms)."""
    if not entries:
        return
    lines = [f"<b>{html.escape(track_label)} ({len(entries)})</b>"]
    for e in entries[:15]:
        if hasattr(e, "token"):
            term = e.token
            entity_type = "token"
            header = (
                f"<b>${html.escape(term)}</b> {html.escape(e.chain.upper())} "
                f"score {e.score.composite:.1f}"
            )
        else:
            term = e.term
            entity_type = e.entity_type
            header = (
                f"<b>{html.escape(term)}</b> score {e.score.composite:.1f}"
            )
        lines.append("")
        lines.append(header)
        lines.append(
            f"<i>n={e.score.narrowness:.0f} q={e.score.quality:.0f} "
            f"m={e.score.momentum:.0f} c={e.score.coherence:.0f}, "
            f"{e.unique_authors_24h} authors, {e.raw_24h} mentions</i>"
        )
        partner_line = _cooc_partner_line(cooc_graph or {}, entity_type, term)
        if partner_line:
            lines.append(partner_line)
        top_url = getattr(e, "top_tweet_url", None)
        if top_url:
            lines.append(
                f'▸ <a href="{html.escape(str(top_url), quote=True)}">top tweet</a>'
            )
    await _send_plain(bot, chat_id, "\n".join(lines))


async def send_new_dict_terms(
    bot: Any, chat_id: int, new_dict_terms: list[tuple[str, str]],
) -> None:
    if not new_dict_terms:
        return
    by_type: dict[str, list[str]] = {}
    for t, term in new_dict_terms:
        by_type.setdefault(t, []).append(term)
    lines = [f"<b>New dictionary terms ({len(new_dict_terms)})</b>"]
    for t in ("sector", "venue", "mechanism"):
        terms = by_type.get(t, [])
        if terms:
            joined = ", ".join(html.escape(x) for x in terms[:15])
            lines.append(f"<b>{t}s:</b> {joined}")
    await _send_plain(bot, chat_id, "\n".join(lines))


def _venue_keyboard(term: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Accept", callback_data=f"s_v:accept:{term}"),
            InlineKeyboardButton("Not now", callback_data=f"s_v:later:{term}"),
            InlineKeyboardButton("Never", callback_data=f"s_v:never:{term}"),
        ]
    ])


async def send_venue_suggestions(
    bot: Any, chat_id: int, suggestions: list[dict],
) -> None:
    for s in suggestions:
        term = s.get("venue_term", "")
        n = s.get("n_cycles", 0)
        m = s.get("unique_authors", 0)
        text = (
            "<b>📍 New venue suggestion</b>\n"
            f"<b>{html.escape(term)}</b> has appeared in {n} cycles "
            f"with {m} unique authors. Want to add this chain to the "
            "active sweep set?"
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_venue_keyboard(term),
            )
        except Exception:  # noqa: BLE001
            log.exception("venue_suggestion_send_failed", extra={"entity_term": term})


def _pattern_keyboard(name: str) -> InlineKeyboardMarkup:
    safe = name[:30]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 keep", callback_data=f"pat:up:{safe}"),
        InlineKeyboardButton("👎 hide", callback_data=f"pat:down:{safe}"),
        InlineKeyboardButton("Expand", callback_data=f"pat:show:{safe}"),
    ]])


async def send_patterns_block(
    bot: Any, chat_id: int, proposals: list[dict],
) -> None:
    """Phase 4.7 'Claude noticed:' block. One Telegram message per
    pattern so each can get its own inline keyboard."""
    if not proposals:
        return
    for p in proposals:
        name = str(p.get("name") or "")
        if not name:
            continue
        desc = str(p.get("description") or "")
        confidence = max(1, min(5, int(p.get("confidence", 1) or 1)))
        marker = "•" * confidence
        new_tag = " <i>[NEW]</i>" if p.get("is_new") else ""
        body_lines = [
            "<b>Claude noticed:</b>",
            "",
            f"<b>{html.escape(name)}</b>{new_tag} <i>{marker}</i>",
            f"<i>{html.escape(desc)}</i>",
        ]
        anchors = p.get("anchors") or []
        if anchors:
            anchor_str = ", ".join(
                f"{html.escape(str(a[0]))}:{html.escape(str(a[1]))}"
                for a in anchors[:3]
                if isinstance(a, (list, tuple)) and len(a) >= 2
            )
            if anchor_str:
                body_lines.append(f"<i>seen on: {anchor_str}</i>")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="\n".join(body_lines),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=_pattern_keyboard(name),
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "patterns_block_send_failed",
                extra={"pattern_name": name},
            )


async def send_emerging_blocks(
    bot: Any,
    chat_id: int,
    emerging: dict,
) -> None:
    """Render the Phase 4 emerging blocks in the correct order."""
    cooc_graph = emerging.get("cooc_graph") or {}

    # Three-tier convergence (Phase 4.7). If the new keys aren't present,
    # fall back to the legacy combined list rendered through
    # send_convergence_block (which splits into tiers internally).
    strong = emerging.get("convergence_strong")
    medium = emerging.get("convergence_medium")
    weak = emerging.get("convergence_weak")
    if strong is not None or medium is not None or weak is not None:
        await send_convergence_strong_block(bot, chat_id, strong or [])
        await send_convergence_medium_block(bot, chat_id, medium or [])
        await send_convergence_weak_block(bot, chat_id, weak or [])
    else:
        await send_convergence_block(
            bot, chat_id, emerging.get("convergence_events", []),
        )

    # Claude-proposed patterns ('Claude noticed:') sit between
    # convergence and the emerging tracks.
    await send_patterns_block(bot, chat_id, emerging.get("pattern_proposals", []))

    await send_emerging_track(
        bot, chat_id, "Emerging tokens", emerging.get("tokens", []), cooc_graph,
    )
    await send_emerging_track(
        bot, chat_id, "Emerging sectors", emerging.get("sectors", []), cooc_graph,
    )
    await send_emerging_track(
        bot, chat_id, "Emerging venues", emerging.get("venues", []), cooc_graph,
    )
    await send_emerging_track(
        bot, chat_id, "Emerging mechanisms", emerging.get("mechanisms", []), cooc_graph,
    )
    await send_new_dict_terms(bot, chat_id, emerging.get("new_dict_terms", []))
    await send_venue_suggestions(bot, chat_id, emerging.get("venue_suggestions", []))


async def send_digest(
    bot: Any,
    chat_id: int,
    results: dict[str, tuple[str, list[ScoredTweet]]],
    db: Any = None,
    manual_scan: bool = False,
    emerging: dict | None = None,
) -> None:
    now = datetime.now(UTC)
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    header_title = "Manual scan" if manual_scan else "Roundup"

    has_results = bool(results)
    has_emerging = bool(emerging) and any(
        emerging.get(k) for k in (
            "tokens", "sectors", "venues", "mechanisms",
            "convergence_events", "convergence_strong",
            "convergence_medium", "convergence_weak",
            "pattern_proposals",
            "new_dict_terms", "venue_suggestions",
        )
    )

    if not has_results and not has_emerging:
        await _send_plain(
            bot, chat_id,
            f"<b>{header_title} - {ts}</b>\nNo posts cleared the threshold.",
        )
        return

    total = sum(len(posts) for _, posts in results.values())
    n_topics = len(results)
    if has_results:
        header = (
            f"<b>{header_title} - {ts}</b>\n"
            f"{n_topics} topics, {total} posts surfaced"
        )
    else:
        header = f"<b>{header_title} - {ts}</b>"
    await _send_plain(bot, chat_id, header)

    if emerging:
        await send_emerging_blocks(bot, chat_id, emerging)

    for topic_name, (summary, posts) in results.items():
        if not posts:
            continue
        topic_header = f"<b>=== {html.escape(topic_name)} ===</b>"
        if summary:
            topic_header += f"\n<i>{html.escape(summary)}</i>"
        await _send_plain(bot, chat_id, topic_header)

        for st in posts:
            t = st.tweet
            body = _format_post(st, now)
            metrics = {
                "likes": t.like_count,
                "replies": t.reply_count,
                "retweets": t.retweet_count,
                "quotes": t.quote_count,
            }
            if db is not None:
                try:
                    await db.upsert_pending_feedback(
                        tweet_id=t.id,
                        author_handle=t.author_handle,
                        tweet_text=t.text,
                        topic_name=topic_name,
                        metrics_json=json.dumps(metrics),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("pending_feedback_insert_failed", extra={"tweet_id": t.id})
            kb = _post_keyboard(t.id, t.author_handle)
            await _send_post(bot, chat_id, body, kb)
