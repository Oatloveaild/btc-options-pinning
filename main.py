import os
import uvicorn
from src.config import BASE_DIR
from src.db import init_db
from src.importer import import_historical_data

if __name__ == "__main__":
    # Try restoring latest database from Google Drive on startup
    try:
        from src.gdrive_sync import download_db_from_gdrive
        download_db_from_gdrive()
    except Exception as e:
        print(f"Google Drive restore check skipped: {e}")

    init_db()
    # If DB is empty, run importer
    from src.db import get_db
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM snapshots").fetchone()["c"]
        if count == 0:
            print("Database empty. Importing historical data...")
            import_historical_data()

    port = int(os.environ.get("PORT", 7860 if os.environ.get("SPACE_ID") else 8000))
    print(f"Starting BTC Options Pinning Dashboard on port {port} ...")
    uvicorn.run("web.app:app", host="0.0.0.0", port=port, reload=False)
