#!/usr/bin/env bash
set -euo pipefail
: "${AGE_IDENTITY:?Set the recovery identity outside the repositories}"
if [ "$#" -ne 1 ]; then echo 'Usage: scripts/restore.sh backup.dump.age' >&2; exit 2; fi
backup_file="$1"
test -f "$backup_file"
# Start only the fresh database before restoring. Never clear an existing volume.
tables=''
for attempt in {1..30}; do
  if tables=$(docker compose exec -T db sh -c 'exec psql -X -A -t -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = '\''public'\''"') 2>/dev/null; then break; fi
  sleep 1
done
if [ -z "$tables" ]; then
  echo 'Target database is not ready. Start the fresh db service and wait for health.' >&2; exit 1
fi

if [ "$(printf '%s' "$tables" | tr -d '[:space:]')" != 0 ]; then
  echo 'Refusing to restore into a non-empty database. Use a new Compose project/volume.' >&2; exit 1
fi
age -d -i "$AGE_IDENTITY" "$backup_file" \
  | docker compose exec -T db sh -c 'exec pg_restore --single-transaction --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
echo 'Restore completed in the fresh database. Start migrations/API/worker after checking configuration.'
