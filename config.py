import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_ADMINS = [1167433460, 1530089636, 1555693508]
env_admins = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
ADMIN_IDS = list(set(DEFAULT_ADMINS + env_admins))
DB_PATH = BASE_DIR / "adti.db"
FILES_DIR = BASE_DIR / "uploads"
FILES_DIR.mkdir(exist_ok=True)
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "")  # Google Apps Script URL
AUDIT_CHANNEL_ID = os.getenv("AUDIT_CHANNEL_ID", "-1004434194693")

