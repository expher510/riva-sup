#!/usr/bin/env bash
set -e
echo "Recombining database backup parts..."
cat riva_production_backup.sql.gz.part* > riva_production_backup.sql.gz
echo "Done! Restoring database to PostgreSQL..."
gunzip -c riva_production_backup.sql.gz | docker exec -i riva_postgres psql -U riva_admin -d riva_production
echo "Database successfully restored!"

