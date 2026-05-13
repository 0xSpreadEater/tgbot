# Deploying bebop-bot to a fresh Hetzner Ubuntu 24.04 VPS

Single-user systemd service, persistent SQLite, daily backups, 4-hour
cron-driven cycles. The repo lives at `/home/bebop/tgbot/` and the
systemd unit is `bebop-bot.service`. The env file is
`/etc/bebop-bot.env` (root-owned, mode 600).

> **Naming**: the GitHub repo is `github.com/0xSpreadEater/tgbot`. The
> Python package, systemd unit, and env file all use `bebop-bot` /
> `bebop_bot`. Mirror the names exactly when copy-pasting.

---

## 0. Prereqs

- A fresh Hetzner Cloud Ubuntu 24.04 box.
- Root SSH access for the initial setup.
- Your `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, `X_BEARER_TOKEN`,
  `ANTHROPIC_API_KEY` ready.

---

## 1. Base system update

SSH in as `root`:

```bash
apt update && apt upgrade -y
```

## 2. Install required packages

```bash
apt install -y git curl ca-certificates ufw sqlite3
```

## 3. Create the `bebop` service account

```bash
adduser bebop                       # set a strong password
usermod -aG sudo bebop
```

## 4. Copy your SSH key to the bebop user

From your laptop:

```bash
ssh-copy-id bebop@<server-ip>
```

(or `mkdir -p /home/bebop/.ssh && cat key.pub >> /home/bebop/.ssh/authorized_keys`
on the server, then `chown -R bebop:bebop /home/bebop/.ssh && chmod 700 /home/bebop/.ssh`.)

## 5. Re-SSH as `bebop`

From your laptop:

```bash
ssh bebop@<server-ip>
```

Everything below runs as `bebop` unless stated otherwise.

## 6. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# pick up ~/.local/bin in this shell
source ~/.local/bin/env || export PATH="$HOME/.local/bin:$PATH"
uv --version
```

## 7. Clone the repository

```bash
git clone https://github.com/0xSpreadEater/tgbot.git /home/bebop/tgbot
```

## 8. Install dependencies

```bash
cd /home/bebop/tgbot
uv sync
```

## 9. Test manually (with `.env`)

Create `/home/bebop/tgbot/.env` for the one-off test:

```bash
cat > /home/bebop/tgbot/.env <<'EOF'
TELEGRAM_BOT_TOKEN=...
TELEGRAM_USER_ID=...
DB_PATH=/home/bebop/tgbot/bebop.db
LOG_LEVEL=INFO
X_BEARER_TOKEN=...
ANTHROPIC_API_KEY=...
CLAUDE_MODEL=claude-sonnet-4-5
EOF
chmod 600 /home/bebop/tgbot/.env
```

Run the bot in the foreground:

```bash
cd /home/bebop/tgbot
uv run python -m bebop_bot
```

In Telegram, message the bot with `/help` and `/status`. Confirm it
responds. Then `Ctrl+C` to stop.

## 10. Promote the env to `/etc/bebop-bot.env`

The systemd unit reads `/etc/bebop-bot.env`. Move the file there and
lock it down (run as root):

```bash
sudo install -m 600 -o root -g root /home/bebop/tgbot/.env /etc/bebop-bot.env
rm /home/bebop/tgbot/.env
```

Verify ownership and mode:

```bash
ls -l /etc/bebop-bot.env
# -rw------- 1 root root  ... /etc/bebop-bot.env
```

## 11. Install the systemd unit

```bash
sudo cp /home/bebop/tgbot/deploy/bebop-bot.service /etc/systemd/system/bebop-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now bebop-bot
sudo systemctl status bebop-bot --no-pager
sudo journalctl -u bebop-bot -n 50 --no-pager
```

You should see `bot_ready` and `scheduler_started` in the journal.

## 12. Verify with `/status`

In Telegram, send `/status`. Confirm:

- `Paused: no`
- `Scheduler: running, next run in <hms>`
- Topics / Allowlist counts are non-zero
- DB size is small but non-zero (the seeds were applied)

## 13. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

Only SSH is exposed — the bot polls outbound to Telegram, X, and
Anthropic.

## 14. One-time backfill

Run a 14-day historical sweep to seed baselines. Wait ~7-10 minutes.
The full cycle that follows will populate all four tracks
(tokens / sectors / venues / mechanisms), build the co-occurrence
graph, and start the Claude-proposed pattern corpus.

In Telegram:

```
/backfill
```

(Use `/backfill --days N` to override the default 14-day window, or
`/backfill --force` to re-run within the 30-day cooldown.)

## 15. Daily backup cron

The SQLite database is the only persistent state — `bebop.db` in
`/home/bebop/tgbot/`. Take a `.backup` snapshot once a day and prune
backups older than 30 days.

```bash
mkdir -p /home/bebop/backups
chmod +x /home/bebop/tgbot/deploy/backup.sh
crontab -e
```

Add:

```
15 3 * * * /home/bebop/tgbot/deploy/backup.sh >> /home/bebop/backups/backup.log 2>&1
```

## 16. Confirm the next scheduled run

The scheduler fires at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC.
Send `/status` and check the `Scheduler:` line — it shows the time
until the next cycle. After the next boundary fires you should see a
`Roundup` digest arrive in Telegram.

---

## Upgrading

```bash
/home/bebop/tgbot/deploy/upgrade.sh
```

This pulls `main`, runs `uv sync`, restarts the service, and prints
recent journal output.

## Useful commands

```bash
sudo systemctl status bebop-bot
sudo systemctl restart bebop-bot
sudo journalctl -u bebop-bot -f             # live log
sudo journalctl -u bebop-bot --since "1h ago"
sqlite3 /home/bebop/tgbot/bebop.db '.tables'
```
