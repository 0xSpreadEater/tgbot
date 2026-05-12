"""Pure-function entity extractors used by Phase 4 emerging detection.

All extractors are case-insensitive where applicable and respect word
boundaries to avoid spurious substring matches.
"""
from __future__ import annotations

import re

# Cashtags: $SYMBOL — symbols are 2-10 alphanumeric chars, must start with
# a letter so prices like "$100" are excluded.
_CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")

# EVM addresses
_EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

# Solana base58 addresses are 32-44 chars; exclude 0,O,I,l in base58 alphabet.
_SOL_RE = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")

# Mention handles (Twitter)
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")

CHAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "solana": ("solana", "sol ", " sol", "spl", "pump.fun", "letsbonk", "raydium"),
    "ethereum": ("ethereum", "eth ", " eth", "mainnet", "erc20", "erc-20", "erc404"),
    "base": ("base chain", "on base", "$base", "coinbase l2"),
    "arbitrum": ("arbitrum", "arb chain", "arb mainnet"),
    "optimism": ("optimism", "op chain", "op mainnet"),
    "abstract": ("abstract chain", "on abstract"),
    "hyperliquid": ("hyperliquid", "hyperevm", "hl perp"),
    "megaeth": ("megaeth", "mega eth"),
    "monad": ("monad",),
    "berachain": ("berachain", "bera chain"),
}


def extract_cashtags(text: str) -> list[str]:
    """Return uppercased cashtag symbols (excluding the $)."""
    if not text:
        return []
    return [m.upper() for m in _CASHTAG_RE.findall(text)]


def extract_evm_addresses(text: str) -> list[str]:
    if not text:
        return []
    return [m.lower() for m in _EVM_RE.findall(text)]


def extract_solana_addresses(text: str) -> list[str]:
    """Conservative Solana address extraction. Skips matches that look
    like English words or EVM-shaped strings."""
    if not text:
        return []
    out = []
    for m in _SOL_RE.findall(text):
        if m.startswith("0x"):
            continue
        # Solana addresses are typically 32-44 base58 chars and contain
        # a mix of cases. Filter out runs of digits or all-lowercase
        # words that are merely long.
        if m.isdigit():
            continue
        if m.isalpha() and m.islower():
            continue
        out.append(m)
    return out


def extract_dictionary_phrases(
    text: str, dictionary: list[dict] | list[str]
) -> list[tuple[str, float]]:
    """Return list of (term, weight) for dictionary terms that appear in
    text. Case-insensitive, word-boundary aware (where applicable).

    Accepts either a list of dict rows with 'term' / 'weight' / 'display_name'
    or a list of bare term strings.
    """
    if not text or not dictionary:
        return []
    text_l = text.lower()
    found: dict[str, float] = {}
    for entry in dictionary:
        if isinstance(entry, str):
            term = entry
            weight = 1.0
            display = term
        else:
            term = entry.get("term", "")
            weight = float(entry.get("weight", 1.0) or 1.0)
            display = entry.get("display_name") or term
        if not term:
            continue
        term_l = term.lower()
        # Use word boundaries when the phrase is alphanumeric; for terms
        # with non-word characters (e.g. "(3,3)") fall back to substring.
        if re.fullmatch(r"[\w\s\-]+", term_l):
            # Escape and use \b boundaries on each side.
            pat = r"\b" + re.escape(term_l) + r"\b"
            if re.search(pat, text_l):
                found[display] = max(found.get(display, 0.0), weight)
        elif term_l in text_l:
            found[display] = max(found.get(display, 0.0), weight)
    return sorted(found.items(), key=lambda x: (-x[1], x[0]))


def classify_chain_for_cashtag(text: str, symbol: str) -> str:
    """Best-effort chain attribution from tweet text. Returns one of the
    known chain names or 'unknown'."""
    if not text:
        return "unknown"
    text_l = text.lower()
    if extract_evm_addresses(text):
        # Look for chain-specific keywords near EVM addresses.
        for chain, kws in CHAIN_KEYWORDS.items():
            if chain in ("solana",):
                continue
            for kw in kws:
                if kw in text_l:
                    return chain
        return "ethereum"
    if extract_solana_addresses(text):
        return "solana"
    for chain, kws in CHAIN_KEYWORDS.items():
        for kw in kws:
            if kw in text_l:
                return chain
    return "unknown"


# ---------------------------------------------------------------------------
# Phase 4: composition / fair-launch / ape / deploy / backing language
# ---------------------------------------------------------------------------

COMPOSITION_PHRASES: list[str] = [
    "recursive", "looped leverage", "looped lending", "PT loop", "YT loop",
    "recursive PT", "PT collateral", "depositing PT", "borrow against",
    "collateral on Morpho", "collateral on Aave", "leverage loop",
    "recursive loop", "5x leverage", "10x looped",
]

FAIR_LAUNCH_PHRASES: list[str] = [
    "fair launch", "no presale", "no team allocation", "no VC",
    "no investors", "bonding curve", "exponential bonding curve",
    "stealth launch", "rugproof", "community-launched",
    "no migration", "no graduate",
]

APE_PHRASES: list[str] = [
    "aping", "aped", "ape in", "ape into", "loaded up", "size up",
    "sized up", "sent it", "all in", "fomo in", "100x", "1000x",
    "filled bags", "bags packed", "lottery ticket",
]

DEPLOY_PHRASES: list[str] = [
    "contract verified", "just deployed", "just shipped", "audit ready",
    "audit complete", "hooks shipped", "mainnet live", "TGE",
    "launching now", "new pool", "pool created", "mint open",
]

BACKING_PATTERNS: list[str] = [
    r"\b(paradigm|a16z|sequoia|polychain|multicoin|dragonfly)\b.*\b(seed|series|funding|backed)\b",
    r"\b(seed|series|funding|backed)\b.*\b(paradigm|a16z|sequoia|polychain|multicoin|dragonfly)\b",
    r"\blisted on (binance|coinbase|okx|kucoin|bybit|gate|poloniex|bitmart|kraken)\b",
    r"\b(binance|coinbase|okx|kucoin|bybit|gate|poloniex|bitmart|kraken)\b.*\blisting\b",
    r"\b(pendle|aave|morpho|compound)\b.*\b(integration|pool|added|live)\b",
]

# Fallback set of known builder handles. Runtime callers should prefer the
# DB-backed `viral_handles` table; this is consulted when the DB is empty
# (e.g. unit tests that don't seed).
KNOWN_BUILDER_HANDLES: set[str] = {
    "0xracer", "0xraceralt", "ctrl", "acme", "bigtoshi",
    "satoshi_bigmoto", "chameleon_jeff", "jeff", "alon_cohen",
    "shrimp", "racer",
}


def _phrase_in(text_lower: str, phrase: str) -> bool:
    phrase_l = phrase.lower()
    # Word-boundary check when phrase is alphanumeric-ish.
    if re.fullmatch(r"[\w\s\-]+", phrase_l):
        pat = r"\b" + re.escape(phrase_l) + r"\b"
        return re.search(pat, text_lower) is not None
    return phrase_l in text_lower


def extract_handles(text: str) -> list[str]:
    """Return lowercased @-handles in text (without the @)."""
    if not text:
        return []
    return sorted({m.lower() for m in _HANDLE_RE.findall(text)})


def extract_builder_handles(
    text: str, known_handles: set[str] | None = None
) -> list[str]:
    """Return lowercased @-handles in text that match a known builder set.

    If `known_handles` is provided, use it; else fall back to the static
    KNOWN_BUILDER_HANDLES module constant.
    """
    if not text:
        return []
    pool = known_handles if known_handles is not None else KNOWN_BUILDER_HANDLES
    found = {m.lower() for m in _HANDLE_RE.findall(text)}
    return sorted(found & pool)


def detect_composition_language(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    return [p for p in COMPOSITION_PHRASES if _phrase_in(text_l, p)]


def detect_fair_launch_language(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    return [p for p in FAIR_LAUNCH_PHRASES if _phrase_in(text_l, p)]


def detect_ape_language(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    return [p for p in APE_PHRASES if _phrase_in(text_l, p)]


def detect_deploy_language(text: str) -> list[str]:
    if not text:
        return []
    text_l = text.lower()
    return [p for p in DEPLOY_PHRASES if _phrase_in(text_l, p)]


def detect_builder_ape_overlap(text: str) -> bool:
    if not text:
        return False
    text_l = text.lower()
    has_deploy = any(_phrase_in(text_l, p) for p in DEPLOY_PHRASES)
    has_ape = any(_phrase_in(text_l, p) for p in APE_PHRASES)
    return has_deploy and has_ape


def detect_backing_event(text: str) -> list[str]:
    if not text:
        return []
    matches: list[str] = []
    for pat in BACKING_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            matches.append(m.group(0)[:80])
    return matches
