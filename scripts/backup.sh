#!/usr/bin/env bash
set -euo pipefail
: "${AGE_RECIPIENT:?Set the public age recipient (not the private key)}"
: "${BACKUP_DIR:?Set an external backup directory}"
command -v age >/dev/null
umask 077
mkdir -p "$BACKUP_DIR"
backup_file="$BACKUP_DIR/gateway-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
test ! -e "$backup_file"
trap 'rm -f "$backup_file.partial"' EXIT
docker compose exec -T db sh -c 'exec pg_dump --format=custom --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  | age -r "$AGE_RECIPIENT" -o "$backup_file.partial"
mv "$backup_file.partial" "$backup_file"
if command -v sha256sum >/dev/null; then
  sha256sum "$backup_file" > "$backup_file.sha256"
else
  shasum -a 256 "$backup_file" > "$backup_file.sha256"
fi
printf 'Encrypted backup: %s\n' "$backup_file"
