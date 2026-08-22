import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
DB_PATH = BASE_DIR / "adti.db"
FILES_DIR = BASE_DIR / "uploads"
FILES_DIR.mkdir(exist_ok=True)
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "")  # Google Apps Script URL
