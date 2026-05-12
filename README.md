# bebop-bot

Single-user Telegram bot that (eventually) DMs you a curated roundup of X (Twitter)
posts every 4 hours, surfaces emerging tokens / sectors / venues, and (V2) tracks
airdrop farming opportunities.

This phase contains only the skeleton: project layout, database schema, seeded
first-boot data, query-syntax validation, single-user auth, and Telegram command
handlers for topic and allowlist management. No X API, no Claude API, no
scheduler — those come in later phases.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A Telegram bot token and your Telegram numeric user ID

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

- `/pause` / `/resume` — toggle the `paused` setting
- `/status` — show paused, topic count, allowlist count, threshold, chains, DB path/size
- `/threshold <n>` — set roundup threshold (1..5)

Meta:

- `/start`, `/help` — full help text. Commands not yet implemented are marked
  "Coming in a later phase."

All other commands in the future surface (e.g. `/run`, `/preview`, `/sectors`,
`/venues`, `/emerging`, `/backfill`, etc.) reply with "Coming in a later phase."
