#!/usr/bin/env bash
# Daily PostgreSQL backup for QF-gate: compressed dump, keeps 14 days. Run from cron as the qfgate user.
set -euo pipefail
set -a; . /srv/qfgate/app/.env; set +a
DIR=/srv/qfgate/backups
mkdir -p "$DIR"
FILE="$DIR/qfgate-$(date -u +%Y%m%d-%H%M).sql.gz"
PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "${POSTGRES_HOST:-localhost}" -U "${POSTGRES_USER:-qfgate}" "${POSTGRES_DB:-qfgate}" | gzip > "$FILE.tmp"
mv "$FILE.tmp" "$FILE"
find "$DIR" -name 'qfgate-*.sql.gz' -mtime +14 -delete
echo "backup written: $FILE"
