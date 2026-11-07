#!/bin/bash
# Riva safe deploy: verify server files are unchanged, back up, apply new files, rebuild.
set -e
SRC=/root/riva_incoming
DST=/root/riva
cd "$DST"

FILES="bot/db.py bot/main.py bot/config.py app/admin_dashboard.py worker/celery_app.py worker/orchestrator.py worker/pipeline.py worker/telegram_vault.py docker-compose.yml"

# md5 (first 8 chars, line endings ignored) of the versions these edits were based on
declare -A EXPECTED=(
  [bot/db.py]=a0520121 [bot/main.py]=0000817f [bot/config.py]=bc293bcc
  [app/admin_dashboard.py]=a03d6c31 [worker/celery_app.py]=cc2c7533
  [worker/orchestrator.py]=23c55311 [worker/pipeline.py]=77bb006d
  [worker/telegram_vault.py]=51230397 [docker-compose.yml]=1dec9f8d
)

echo ">>> 1/5 Checking server files are the same base version..."
MISMATCH=0
for f in $FILES; do
  if [ -f "$f" ]; then
    h=$(tr -d '\r' < "$f" | md5sum | cut -c1-8)
  else
    h=missing
  fi
  if [ "$h" != "${EXPECTED[$f]}" ]; then
    echo "   DIFFERENT on server: $f ($h)"
    MISMATCH=1
  fi
done
if [ "$MISMATCH" = "1" ] && [ "$FORCE" != "1" ]; then
  echo "!!! Server has edits not in the local copy. Nothing was changed. Send this output to Claude."
  exit 1
fi
echo "   OK"

echo ">>> 2/5 Backing up current files + .env..."
BK=/root/riva_backup_$(date +%Y%m%d_%H%M%S).tgz
tar czf "$BK" $FILES .env $( [ -f bot/marvel.py ] && echo bot/marvel.py )
echo "   Backup: $BK"

echo ">>> 3/5 Ensuring BOT_ADMIN_PASSWORD in .env..."
if ! grep -q '^BOT_ADMIN_PASSWORD=' .env; then
  printf '\nBOT_ADMIN_PASSWORD=Riva@510\n' >> .env
  echo "   added"
else
  echo "   already set"
fi

echo ">>> 4/5 Applying new files..."
for f in $FILES bot/marvel.py; do
  cp "$SRC/$f" "$DST/$f"
done

echo ">>> 5/5 Rebuilding & restarting Riva containers..."
docker compose up -d --build
sleep 15
docker compose ps
echo "----- bot logs -----"
docker compose logs --tail=25 bot
echo
echo "DONE. Rollback if needed:  cd $DST && tar xzf $BK && docker compose up -d --build"
