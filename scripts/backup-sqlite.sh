#!/usr/bin/env bash
set -euo pipefail

data_dir="${DATA_DIR:-/opt/video-series-bot/data}"
backup_dir="${BACKUP_DIR:-/opt/video-series-backups}"
stamp="$(date +%F)"

mkdir -p "$backup_dir"
sqlite3 "$data_dir/library.db" ".backup '$backup_dir/library-$stamp.db'"
find "$backup_dir" -type f -name 'library-*.db' -mtime +30 -delete
