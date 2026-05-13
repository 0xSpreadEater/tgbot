#!/usr/bin/env bash
set -euo pipefail
DB=/home/bebop/tgbot/bebop.db
DEST=/home/bebop/backups
STAMP=$(date +%F-%H%M)
mkdir -p "$DEST"
sqlite3 "$DB" ".backup '$DEST/bebop-$STAMP.db'"
find "$DEST" -name 'bebop-*.db' -mtime +30 -delete
