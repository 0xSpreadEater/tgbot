# bebop-bot

Single-user Telegram bot that (eventually) DMs you a curated roundup of X (Twitter)
posts every 4 hours, surfaces emerging tokens / sectors / venues, and (V2) tracks
airdrop farming opportunities.

Phases 1 and 2 are in: project layout, schema, seeded first-boot data,
query-syntax validation, single-user auth, topic/allowlist commands, plus
the full roundup pipeline (X search → allowlist/feedback filter → Claude
scoring on 1-5 across `on_topic` / `substance` / `novelty` → topic summary
→ HTML digest via Telegram), triggerable manually via `/run` and
`/preview`. The scheduler, inline reaction buttons, emerging-tokens
pipeline, and backfill are still future phases.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A Telegram bot token and your Telegram numeric user ID
- An X (Twitter) API bearer token with v2 recent-search access
- An Anthropic API key

## Getting your Telegram bot token and user ID

1. Open Telegram and message [@BotFather](https://t.me/BotFather). Send `/newbot`
   and follow the prompts. BotFather will return an HTTP API token of the form
   `123456789:AA...`. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
2. To get your numeric user ID, message [@userinfobot](https://t.me/userinfobot)
   on Telegram. It replies with your numeric ID. Put it in `.env` as
   `TELEGRAM_USER_ID`. Any Telegram account that is not this ID will be silently
   ignored by the bot.

## Setup

```bash
cp .env.example .env
# edit .env and fill in TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID

uv sync
```

## Run

```bash
uv run python -m bebop_bot
```

First boot creates `bebop.db`, applies the schema, and seeds topics, allowlist,
sector/venue dictionaries, and default settings. Subsequent boots are
idempotent (`INSERT OR IGNORE` everywhere).

## Tests and lint

```bash
uv run pytest
uv run ruff check .
```

## Commands implemented this phase

Topic management:

- `/add <name> | <query>` — add a topic; query is normalized and validated
- `/remove <name>` — remove a topic
- `/list` — list topics (numbered, query truncated to 80 chars)
- `/show <name>` — show full query for a topic
- `/edit <name> | <query>` — replace a topic's query

Allowlist (curated X authors):

- `/allow <handle>` — add to allowlist (lowercased, `@` stripped)
- `/disallow <handle>` — remove from allowlist
- `/allowlist` — list allowed handles alphabetically

Roundup controls:

- `/run` — fetch, score, summarize, DM the digest now. Rate-limited to once
  per hour. Advances `last_seen_id` per topic so subsequent runs only see
  new tweets.
- `/preview` — same as `/run` but does NOT advance `last_seen_id`. Shares the
  same rate-limit bucket.
- `/test <query>` — fetch the 5 most recent matching tweets, no scoring, no
  save. Use to sanity-check a topic query before adding it.
- `/pause` / `/resume` — toggle the `paused` setting
- `/status` — show paused, topic count, allowlist count, threshold, chains, DB path/size
- `/threshold <n>` — set roundup threshold (1.0 .. 5.0, floats allowed)

Filtering:

- `/rubric` — show the current taste rubric appended to the Claude system prompt
- `/rubric set <text>` — replace it
- `/rubric clear` — empty it
- `/calibrate <score> | <tweet text>` — record a manual feedback example.
  Score `>=4` is labelled `up`, `<=2` is labelled `down`, `3` is rejected
  as ambiguous. Calibration examples feed into Claude's few-shot context.

Meta:

- `/start`, `/help` — full help text. Commands not yet implemented are marked
  "Coming in a later phase."

Other commands (`/learnings`, `/sectors`, `/venues`, `/emerging`, `/backfill`,
etc.) reply with "Coming in a later phase."

## API costs

X API (Basic tier, `tweets/search/recent`):

- Worst case: ~100 posts × 8 topics = ~800 posts per cycle. At 6 cycles/day
  that's ~4,800 posts/day before `since_id` deduplication kicks in.
- Steady state once `since_id` is advancing per topic: ~2-3k posts/day.
- A `/test` call is 5 posts.

Anthropic API (Sonnet 4.5):

- One scoring call per topic per cycle (batched up to 30 tweets per call,
  paginated above), plus one short summary call per topic that produced
  results.
- Roughly ~$0.05-0.15 per full cycle in practice, so a 6-cycle/day cadence
  costs ~$10-25/month. Heavily activity-dependent.
