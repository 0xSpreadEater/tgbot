import json
import logging

import aiosqlite

log = logging.getLogger(__name__)

SEED_TOPICS: list[tuple[str, str]] = [
    (
        "bebop_core",
        '(bebop OR "@bebop_dex" OR bopamm OR "bop amm") -anime -"cowboy bebop" -is:retweet lang:en',
    ),
    (
        "amms",
        '("prop amm" OR "proprietary amm" OR "concentrated liquidity" OR "uniswap v4" OR "uni v4" OR "v4 hook" OR "v4 hooks" OR slipstream OR "balancer v3" OR maverick OR "fluid dex") -is:retweet -is:reply lang:en',
    ),
    (
        "onchain_trading",
        '(("onchain" OR "on-chain") (trading OR volume OR flow OR liquidity) -nft) OR ("dex volume" OR "perp dex" OR "spot dex") -is:retweet -is:reply lang:en -giveaway -airdrop -presale',
    ),
    (
        "solvers_intents",
        '(solver OR solvers OR "intent based" OR "intent-based" OR intents OR cowswap OR "cow protocol" OR uniswapx OR "1inch fusion" OR "0x settler" OR "across protocol" OR anoma) -is:retweet -is:reply lang:en',
    ),
    (
        "mev",
        '(mev OR flashbots OR "private mempool" OR "block builder" OR "sandwich attack" OR "jit liquidity" OR "priority gas auction" OR "order flow auction" OR ofa) -is:retweet -is:reply lang:en',
    ),
    (
        "rwa_onchain",
        '(rwa OR "real world asset" OR "tokenized treasury" OR "tokenized equities" OR "tokenized stocks" OR ondo OR "blackrock buidl" OR superstate) -is:retweet -is:reply lang:en',
    ),
    (
        "market_liquidity",
        '(("market depth" OR "liquidity fragmentation" OR "liquidity provision" OR "lp returns" OR lvr OR "loss versus rebalancing") (crypto OR defi OR onchain OR "on-chain" OR dex OR cex)) -is:retweet -is:reply lang:en',
    ),
    (
        "microstructure",
        '(("market microstructure" OR "price discovery" OR "maker taker" OR "fee tier" OR "spread compression" OR "tick size") (crypto OR defi OR exchange OR dex OR cex OR perp OR perps)) -is:retweet -is:reply lang:en',
    ),
]

SEED_ALLOWLIST: list[str] = [
    "bebop_dex",
    "wintermute_t",
    "hayden_adams",
    "0xngmi",
    "danrobinson",
    "hasufl",
    "bantg",
    "transmissions11",
    "hosseeb",
    "robertleshner",
    "anitaramaswamy",
    "dougiedeluca",
    "lex_node",
    "smyyguy",
]

SEED_SECTORS: list[str] = [
    "RWA", "real world assets", "tokenized treasuries", "yield bearing stables",
    "yield bearing stablecoins", "restaking", "LRTs", "liquid restaking tokens",
    "prediction markets", "perp DEXs", "perp dex", "intent trading",
    "MEV protection", "account abstraction", "ERC404", "ERC6909", "hooks",
    "UniV4 hooks", "tokenized equities", "onchain gambling", "social fi",
    "M2E", "move to earn", "AI agents", "agentic finance", "BTC defi",
    "BTCFi", "ponzi game", "ponzinomics", "PT trading", "points farming",
    "intent perps",
]

SEED_VENUES: list[str] = [
    "MegaETH", "Monad", "Abstract", "Hyperliquid", "Berachain", "Plasma",
    "Sonic", "Tempo", "Hyperbeat", "Pump.fun", "LetsBonk", "Believe",
    "Virtuals", "Polymarket", "Hyperliquid HIP-3", "Friend Tech", "Pendle",
    "Ethena", "Aerodrome", "Bullpen", "Offshore", "Blackhaven", "APYX",
]

SEED_MECHANISMS: list[tuple[str, str | None, int, str]] = [
    ("ERC404", None, 1, "seed"),
    ("ERC721C", None, 0, "seed"),
    ("ERC6909", None, 0, "seed"),
    ("ERC4626", None, 0, "seed"),
    ("DN404", None, 1, "seed"),
    ("v4 hook", "UniV4 hook", 1, "seed"),
    ("uniswap v4 hook", "UniV4 hook", 1, "seed"),
    ("hooks", None, 0, "seed"),
    ("concentrated liquidity", None, 0, "seed"),
    ("CLOB", "central limit order book", 0, "seed"),
    ("bonding curve", None, 1, "seed"),
    ("exponential bonding curve", None, 1, "seed"),
    ("fair launch", None, 1, "seed"),
    ("no presale", None, 1, "seed"),
    ("no team allocation", None, 1, "seed"),
    ("stealth launch", None, 1, "seed"),
    ("PT loop", None, 1, "seed"),
    ("YT loop", None, 1, "seed"),
    ("recursive PT", None, 1, "seed"),
    ("recursive loop", None, 1, "seed"),
    ("PT collateral", None, 1, "seed"),
    ("looped leverage", None, 1, "seed"),
    ("move-to-earn", "M2E", 1, "seed"),
    ("M2E", "move-to-earn", 1, "seed"),
    ("mine-to-earn", None, 1, "seed"),
    ("play-to-earn", "P2E", 0, "seed"),
    ("merge mining", None, 1, "seed"),
    ("reserve-backed", None, 1, "seed"),
    ("protocol-owned liquidity", "POL", 0, "seed"),
    ("(3,3)", None, 1, "seed"),
    ("ponzi", None, 1, "seed"),
    ("ponzi game", None, 1, "seed"),
    ("ponzinomics", None, 1, "seed"),
    ("rebase", None, 0, "seed"),
    ("liquid restaking", "LRT", 0, "seed"),
    ("LRT", "liquid restaking", 0, "seed"),
    ("AVS", None, 0, "seed"),
    ("intent-based", None, 0, "seed"),
    ("solver-based", None, 0, "seed"),
    ("RFQ settlement", None, 0, "seed"),
    ("BTCFi", None, 0, "seed"),
    ("BTC defi", None, 0, "seed"),
    ("dividend-backed", None, 1, "seed"),
    ("dividend-backed stablecoin", None, 1, "seed"),
    ("DBS", "dividend-backed stablecoin", 1, "seed"),
]


SEED_VIRAL_HANDLES: list[str] = [
    "0xracer", "0xraceralt", "ctrl", "acme", "bigtoshi",
    "satoshi_bigmoto", "chameleon_jeff", "jeff", "alon_cohen",
    "shrimp", "racer", "pumpdotfun", "hyperliquidx", "stepnofficial",
    "pendle_fi", "megaeth_labs",
]


SEED_VIRAL_EXAMPLES: list[dict] = [
    {
        "name": "ERC404 / PANDORA",
        "chain": "ethereum",
        "window_start": "2024-01-30T00:00:00Z",
        "window_end": "2024-02-02T00:00:00Z",
        "signals": ["novel_mechanism", "fair_launch_lang",
                    "builder_ape_overlap", "known_builder"],
        "phrases": ["ERC404", "fractional NFT", "bonding curve", "ctrl", "Acme"],
        "handles": [],
        "rationale": (
            "Novel token standard ('ERC404') discussed by devs and quants as a "
            "noun for ~7 days before PANDORA launch. Pseudonymous builders 'ctrl'"
            " + 'Acme' had prior presence. Fair-launch mechanics. Builder language"
            " (token standard discussion) overlapped with ape language. Hit $10M"
            " volume in 5 hours on Uniswap."
        ),
    },
    {
        "name": "Friend Tech",
        "chain": "base",
        "window_start": "2023-08-08T00:00:00Z",
        "window_end": "2023-08-11T00:00:00Z",
        "signals": ["known_builder", "new_venue_context",
                    "fair_launch_lang", "backing_event"],
        "phrases": ["bonding curve", "tokenize your friends", "keys",
                    "shares", "PWA"],
        "handles": ["0xRacerAlt"],
        "rationale": (
            "Racer had two prior projects (TweetDAO Apr 2022, Stealcam Mar 2023)"
            " — known pseudonymous builder. Base was 2 days old. Bonding-curve"
            " mechanism. Paradigm seed funding (Aug 19) was a second viral spike"
            " larger than launch. PWA-native UX. By Sep 15, daily fees > Ethereum's."
        ),
    },
    {
        "name": "STEPN",
        "chain": "solana",
        "window_start": "2022-03-01T00:00:00Z",
        "window_end": "2022-04-01T00:00:00Z",
        "signals": ["novel_mechanism", "new_venue_context"],
        "phrases": ["move-to-earn", "M2E", "NFT sneakers",
                    "dual-token", "GMT", "GST"],
        "handles": ["Stepnofficial"],
        "rationale": (
            "Mechanism phrase 'move-to-earn' entered circulation Q4 2021 after "
            "Solana hackathon (4th place). No paid marketing — word-of-mouth in "
            "builder community. Dual-token model gave both speculators (GMT) and "
            "users (GST) something to hold. GMT 34,000% in one month. Adidas "
            "partnership later was a backing event."
        ),
    },
    {
        "name": "Pendle Effect / APYX",
        "chain": "ethereum",
        "window_start": "2026-03-01T00:00:00Z",
        "window_end": "2026-03-15T00:00:00Z",
        "signals": ["recursive_lang", "backing_event", "novel_mechanism"],
        "phrases": ["apxUSD", "apyUSD", "PT-apyUSD", "recursive", "PT loop",
                    "Morpho collateral", "dividend-backed stablecoin",
                    "STRC", "SATA"],
        "handles": ["pendle_fi"],
        "rationale": (
            "Pendle integration (Mar 2026) was the legitimization. Recursive "
            "composition language ('deposit → PT → Morpho collateral → borrow "
            "USDC → buy more') visible in trader discussion. Novel mechanism: DAT"
            " preferred equity dividends → onchain stablecoin yield. Pools went 0"
            " → $237M TVL across 3 markets, ~$2.5M/day TVL accumulation. apyUSD"
            " alone $62M TVL."
        ),
    },
    {
        "name": "BIGCOIN on Abstract",
        "chain": "abstract",
        "window_start": "2025-04-01T00:00:00Z",
        "window_end": "2025-04-18T00:00:00Z",
        "signals": ["novel_mechanism", "new_venue_context",
                    "fair_launch_lang", "builder_ape_overlap"],
        "phrases": ["mine-to-earn", "Bigtoshi", "21 million supply",
                    "halving", "no presale", "no team allocation"],
        "handles": [],
        "rationale": (
            "Abstract mainnet launched Jan 2025. Bigcoin launched on Abstract "
            "within ~3 months. Novel mechanism ('mine-to-earn' gamified mining "
            "simulation). Fair launch (no presale, fixed supply mirroring Bitcoin)."
            " Pseudonymous founder 'Bigtoshi'. Abstract active addresses 2x in 2"
            " weeks (peak 27.2k on Apr 15)."
        ),
    },
    {
        "name": "sato on Ethereum",
        "chain": "ethereum",
        "window_start": "2026-04-15T00:00:00Z",
        "window_end": "2026-05-05T00:00:00Z",
        "signals": ["novel_mechanism", "fair_launch_lang"],
        "phrases": ["v4 hook", "exponential bonding curve", "no presale",
                    "no migration", "minted and repurchased"],
        "handles": [],
        "rationale": (
            "UniV4 Hook + exponential bonding curve. Minimalist 'it is an asset' "
            "thesis (lowercase name signals philosophy). No presale, no migration."
            " Poloniex listing May 5, 2026 was the legitimization moment. Contract"
            " 0x829f4B62EEBE12Af653b4dD4fFc480966F7d7f09."
        ),
    },
    {
        "name": "Pump.fun",
        "chain": "solana",
        "window_start": "2024-02-15T00:00:00Z",
        "window_end": "2024-03-15T00:00:00Z",
        "signals": ["novel_mechanism", "fair_launch_lang", "builder_ape_overlap"],
        "phrases": ["bonding curve", "fair launch", "graduate",
                    "pump.fun", "no presale", "rugproof"],
        "handles": ["alon_cohen", "pumpdotfun"],
        "rationale": (
            "Bonding-curve launch model for memecoins (graduate to Raydium at "
            "~$69k market cap). 'Fair launch' language explicitly invoked. Builder"
            " Alon Cohen active in trader communities. Solana memecoin season "
            "provided the substrate."
        ),
    },
    {
        "name": "Hyperliquid pre-airdrop",
        "chain": "hyperliquid",
        "window_start": "2024-10-01T00:00:00Z",
        "window_end": "2024-11-29T00:00:00Z",
        "signals": ["new_venue_context", "novel_mechanism", "known_builder"],
        "phrases": ["onchain orderbook", "perp DEX", "Jeff",
                    "HyperEVM", "no VC", "points farming"],
        "handles": ["HyperliquidX", "chameleon_jeff"],
        "rationale": (
            "Hyperliquid had been live since 2023 but pre-airdrop saw mechanism "
            "discussion intensify: fully onchain orderbook (vs hybrid models "
            "elsewhere), perp DEX mechanism. 'No VC' / 'no team allocation' fair-"
            "launch language. Known pseudonymous builder Jeff. Airdrop Nov 29 2024"
            " was the legitimization moment."
        ),
    },
    {
        "name": "POD on Base",
        "chain": "base",
        "window_start": "2026-04-28T00:00:00Z",
        "window_end": "2026-05-09T00:00:00Z",
        "signals": ["new_venue_context", "novel_mechanism"],
        "phrases": ["Dolphin", "Mistral 24B", "Venice", "AI agent",
                    "DePIN", "uncensored AI"],
        "handles": [],
        "rationale": (
            "POD (Dolphin) is an AI model / DePIN project on Base. Mechanism is "
            "'uncensored AI model marketplace'. Bundle with $VVV (Venice Protocol)"
            " — the two appeared together pre-spike. Hit $82M market cap, +130% "
            "24h. Contract 0xeD664536023d8E4b1640C394777D34aBAFF1dF8F."
        ),
    },
    {
        "name": "Offshore Protocol on MegaETH",
        "chain": "megaeth",
        "window_start": "2026-04-15T00:00:00Z",
        "window_end": "2026-05-12T00:00:00Z",
        "signals": ["new_venue_context", "novel_mechanism"],
        "phrases": ["MegaETH", "Offshore Protocol", "real-time",
                    "App Wave 3", "Terminal"],
        "handles": ["MegaETH_Labs"],
        "rationale": (
            "MegaETH mainnet launched ahead of April 30 2026 TGE. Offshore "
            "Protocol was in App Wave 3 alongside Blackhaven and Stomp. Real-time"
            " execution mechanism (sub-millisecond latency) is the novel substrate."
            " Pre-TGE points-farming attention created builder + trader overlap."
        ),
    },
    {
        "name": "Blackhaven on MegaETH",
        "chain": "megaeth",
        "window_start": "2026-04-15T00:00:00Z",
        "window_end": "2026-05-12T00:00:00Z",
        "signals": ["new_venue_context", "novel_mechanism", "known_builder"],
        "phrases": ["reserve-backed", "(3,3)", "RBT", "Reserve Backed Token",
                    "USDm", "principal-protected note", "fixed-term bond",
                    "OHM founder"],
        "handles": [],
        "rationale": (
            "Reserve-backed treasury mechanism — explicitly a (3,3) ponzi-style "
            "design with on-chain transparency. Built by the founder of OHM "
            "(known builder). Launched on MegaETH (new venue). Liquidity backbone"
            " narrative for the chain. Convergence of: reserve-backed + known "
            "builder + new venue."
        ),
    },
]


DEFAULT_SETTINGS: list[tuple[str, str]] = [
    ("paused", "0"),
    ("threshold", "2"),
    ("emerging_entity_threshold", "1.5"),
    ("chain_evm_enabled", "1"),
    ("chain_solana_enabled", "1"),
    ("sol_min_author_score", "0.5"),
    ("sol_min_unique_authors", "3"),
    ("sol_exclude_keywords", "1000x,10000x,guaranteed,moonshot guarantee,100% gem"),
    ("evm_min_author_score", "0.3"),
    ("evm_min_unique_authors", "2"),
    ("evm_exclude_keywords", "1000x,guaranteed gem"),
    ("taste_rubric", ""),
    (
        "evm_sweep_query",
        '("just deployed" OR "new hook" OR "v4 hook" OR "new pool" OR "fresh deploy" OR "stealth launch" OR aping OR "loaded up" OR "fresh mint") -is:retweet -is:reply lang:en',
    ),
    (
        "solana_sweep_query",
        '(("pump.fun" OR letsbonk OR "fresh mint" OR "just aped" OR "new launch" OR moonshot) (sol OR solana OR spl)) -is:retweet -is:reply lang:en',
    ),
    ("backfilled_at", ""),
]


async def seed_topics(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for name, query in SEED_TOPICS:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO topics(name, query) VALUES(?, ?)",
            (name, query),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info("seed_topics", extra={"inserted": inserted, "total_seed": len(SEED_TOPICS)})
    return inserted


async def seed_allowlist(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for handle in SEED_ALLOWLIST:
        h = handle.lstrip("@").lower()
        cur = await conn.execute(
            "INSERT OR IGNORE INTO allowlist(handle) VALUES(?)",
            (h,),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info("seed_allowlist", extra={"inserted": inserted, "total_seed": len(SEED_ALLOWLIST)})
    return inserted


async def seed_sectors(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for term in SEED_SECTORS:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO sector_dictionary(term, display_name, weight, source) "
            "VALUES(?, ?, 1.0, 'seed')",
            (term, term),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info("seed_sectors", extra={"inserted": inserted, "total_seed": len(SEED_SECTORS)})
    return inserted


async def seed_venues(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for term in SEED_VENUES:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO venue_dictionary(term, display_name, weight, source) "
            "VALUES(?, ?, 1.0, 'seed')",
            (term, term),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info("seed_venues", extra={"inserted": inserted, "total_seed": len(SEED_VENUES)})
    return inserted


async def seed_mechanisms(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for term, display_name, is_novelty, source in SEED_MECHANISMS:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO mechanism_dictionary("
            "term, display_name, weight, source, is_novelty_marker) "
            "VALUES(?, ?, 1.0, ?, ?)",
            (term, display_name, source, int(is_novelty)),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info(
        "seed_mechanisms",
        extra={"inserted": inserted, "total_seed": len(SEED_MECHANISMS)},
    )
    return inserted


async def seed_viral_examples(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for entry in SEED_VIRAL_EXAMPLES:
        signals_json = json.dumps({
            "signals": entry.get("signals", []),
            "phrases": entry.get("phrases", []),
            "handles": entry.get("handles", []),
        })
        cur = await conn.execute(
            "INSERT OR IGNORE INTO viral_seed_examples("
            "name, chain, window_start, window_end, signals_json, rationale, source) "
            "VALUES(?, ?, ?, ?, ?, ?, 'seed')",
            (
                entry["name"], entry["chain"],
                entry.get("window_start"), entry.get("window_end"),
                signals_json, entry.get("rationale", ""),
            ),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info(
        "seed_viral_examples",
        extra={"inserted": inserted, "total_seed": len(SEED_VIRAL_EXAMPLES)},
    )
    return inserted


async def seed_viral_handles(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for handle in SEED_VIRAL_HANDLES:
        h = handle.lstrip("@").lower().strip()
        cur = await conn.execute(
            "INSERT OR IGNORE INTO viral_handles(handle, source) VALUES(?, 'seed')",
            (h,),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info(
        "seed_viral_handles",
        extra={"inserted": inserted, "total_seed": len(SEED_VIRAL_HANDLES)},
    )
    return inserted


async def seed_settings(conn: aiosqlite.Connection) -> int:
    inserted = 0
    for key, value in DEFAULT_SETTINGS:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            (key, value),
        )
        if cur.rowcount:
            inserted += 1
    await conn.commit()
    log.info(
        "seed_settings",
        extra={"inserted": inserted, "total_seed": len(DEFAULT_SETTINGS)},
    )
    return inserted


async def seed_all(conn: aiosqlite.Connection) -> dict[str, int]:
    counts = {
        "topics": await seed_topics(conn),
        "allowlist": await seed_allowlist(conn),
        "sectors": await seed_sectors(conn),
        "venues": await seed_venues(conn),
        "mechanisms": await seed_mechanisms(conn),
        "viral_examples": await seed_viral_examples(conn),
        "viral_handles": await seed_viral_handles(conn),
        "settings": await seed_settings(conn),
    }
    log.info("seed_all_done", extra=counts)
    return counts
