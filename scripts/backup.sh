#!/usr/bin/env bash
# Nightly pg_dump of memory-core to $BACKUP_DIR (default ./backups); keeps 14 days. The access log is not worth backing up.
set -euo pipefail
dir=${BACKUP_DIR:-./backups}
out=$dir/memory-$(date +%F).dump
cd "$(dirname "$0")/.."
mkdir -p "$dir"
docker compose exec -T postgres pg_dump -U memory -Fc --exclude-table-data=access_log memory </dev/null > "$out.part" && mv "$out.part" "$out"
find "$dir" -name 'memory-*.dump' -mtime +14 -delete
