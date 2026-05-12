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
        "settings": await seed_settings(conn),
    }
    log.info("seed_all_done", extra=counts)
    return counts
