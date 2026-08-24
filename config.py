import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_ADMINS = [1167433460, 1530089636, 135124772]
env_admins = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
ADMIN_IDS = list(set(DEFAULT_ADMINS + env_admins))

DEFAULT_DATABASE_URL = "postgresql://neondb_owner:npg_VEvwLW1S6FfJ@ep-cool-moon-b297m0yt.c-6.eu-central-1.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
DB_PATH = BASE_DIR / "adti.db"
FILES_DIR = BASE_DIR / "uploads"
FILES_DIR.mkdir(exist_ok=True)
DEFAULT_SHEET_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbyYVMSMfClqD-yK5XzzGzk3TG8mf3TUpkcxELuR9fCdn9skMZRgAOP4NPmeimZWoSg/exec"
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", DEFAULT_SHEET_WEBHOOK_URL)
AUDIT_CHANNEL_ID = os.getenv("AUDIT_CHANNEL_ID", "-1004434194693")
ADMIN_MASTER_PIN = os.getenv("ADMIN_MASTER_PIN", "202652")
