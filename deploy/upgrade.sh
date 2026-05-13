#!/usr/bin/env bash
set -euo pipefail
cd /home/bebop/tgbot
git pull
uv sync
sudo systemctl restart bebop-bot
sleep 2
sudo systemctl status bebop-bot --no-pager
sudo journalctl -u bebop-bot -n 50 --no-pager
