@echo off
echo Recombining database backup parts...
copy /b riva_production_backup.sql.gz.part* riva_production_backup.sql.gz
echo Done! Restoring database to PostgreSQL...
docker exec -i riva_postgres psql -U riva_admin -d riva_production < riva_production_backup.sql.gz
echo Database restored!
pause

