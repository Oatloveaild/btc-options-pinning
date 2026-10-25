import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
LOGS_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "btc_pinning.db"

RAW_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

BKK_TZ = ZoneInfo("Asia/Bangkok")
DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"
FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Schedules in Asia/Bangkok
SCHEDULE_HOURS = {
    "0600": (6, 0),
    "1530": (15, 30),
    "2100": (21, 0),
}
