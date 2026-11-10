#!/bin/bash
set -euo pipefail
umask 077
backup_dir=/var/backups/sga-gateway
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
recipient=$(cat /etc/sga-gateway/backup_recipient.txt)
runuser -u sgapg -- /opt/sga-gateway/postgresql-17.11/bin/pg_dump -h /run/sga-gateway-pg -p 5548 -d gateway -Fc -Z0 | /opt/sga-gateway/age/age -r "$recipient" -o "$backup_dir/$stamp-gateway.dump.age"
tar -C /etc -cf - sga-gateway | /opt/sga-gateway/age/age -r "$recipient" -o "$backup_dir/$stamp-config.tar.age"
cd "$backup_dir"
sha256sum "$stamp-gateway.dump.age" "$stamp-config.tar.age" > "$stamp.sha256"
printf '%s\n' "Encrypted gateway backup: $backup_dir/$stamp"
