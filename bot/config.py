import os

# Telegram Bot Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID") or 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# Secondary Delivery / Worker Bot (Shields Primary Bot from DMCA & Bans)
DELIVERY_BOT_TOKEN = os.getenv("DELIVERY_BOT_TOKEN", "").strip()
_raw_deliv_user = os.getenv("DELIVERY_BOT_USERNAME", "").strip()
if "t.me/" in _raw_deliv_user:
    DELIVERY_BOT_USERNAME = _raw_deliv_user.split("t.me/")[-1].split("/")[0].split("?")[0].lstrip("@")
else:
    DELIVERY_BOT_USERNAME = _raw_deliv_user.lstrip("@")

# Master Vault Channel
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "-1004466886745"))

# Bot Admin User IDs & Secret Password for Force-Subscribe Management
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("BOT_ADMIN_IDS", "8511689363").split(",")
    if x.strip().isdigit()
}
BOT_ADMIN_PASSWORD = os.getenv("BOT_ADMIN_PASSWORD", "")

# Database Credentials
POSTGRES_USER = os.getenv("POSTGRES_USER", "riva_admin")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "super_secret_db_password_change_in_production")
POSTGRES_DB = os.getenv("POSTGRES_DB", "riva_production")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))

SYNC_DATABASE_URL = os.getenv(
    "SYNC_DATABASE_URL",
    f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)
