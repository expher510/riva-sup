#!/bin/bash
set -e

echo "=========================================="
echo "  Riva VOD Engine - Hostinger VPS Deploy  "
echo "=========================================="

# 1. Update and install Docker if not present
if ! command -v docker &> /dev/null; then
    echo ">>> Installing Docker..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg lsb-release
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# 2. Check docker compose command
DOCKER_COMPOSE="docker compose"
if ! docker compose version &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
fi

# 3. Create .env from template if missing
if [ ! -f .env ]; then
    echo ">>> Creating .env file..."
    cp .env.example .env
fi

# 4. Build and start services
echo ">>> Building and starting Riva Docker stack..."
$DOCKER_COMPOSE up -d --build

# 5. Wait for database health
echo ">>> Waiting for PostgreSQL and Redis to be healthy..."
sleep 10

# 6. Initialize database tables
echo ">>> Initializing database tables..."
$DOCKER_COMPOSE exec -T api python init_db.py

echo "=========================================="
echo "  DEPLOYMENT SUCCESSFUL!                  "
echo "=========================================="
echo "API Health: curl http://localhost:8000/health"
echo "Series API: curl http://localhost:8000/v1/series"
echo "Crawler Status: curl http://localhost:8000/v1/crawler/status"
echo ""
echo "To launch full background scraping for Asian / KDrama:"
echo "$DOCKER_COMPOSE exec -d worker python -m worker.scrapers.cima4u_crawler asian-series 20"
echo ""
echo "To view live logs:"
echo "$DOCKER_COMPOSE logs -f api"
