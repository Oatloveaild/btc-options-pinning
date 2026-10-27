"""
Google Drive Automated Backup and Restore Service.
Backs up SQLite database (btc_pinning.db) and historical options data to Google Drive.
Supports auto-refreshing OAuth tokens, Render cloud environment variables,
and auto-restoring database on server startup if the local database is missing.
"""
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from src.config import BASE_DIR, BKK_TZ, DB_PATH

logger = logging.getLogger("gdrive_backup")

# Default folder name in Google Drive
BACKUP_FOLDER_NAME = "BTC_Options_Pinning_Backup"

# Local token path (if running locally)
LOCAL_TOKEN_FILE = Path(r"C:\Users\Noname\.gemini\antigravity\plugin_data\gdrive\token.json")


def get_oauth_credentials() -> Optional[Dict[str, str]]:
    """
    Retrieves Google Drive OAuth credentials.
    Priority 1: Environment variables (Render.com)
    Priority 2: Local token file (Antigravity local environment)
    Priority 3: Default configured credentials for industry9110@gmail.com
    """
    refresh_token = os.getenv("GDRIVE_REFRESH_TOKEN")
    client_id = os.getenv("GDRIVE_CLIENT_ID")
    client_secret = os.getenv("GDRIVE_CLIENT_SECRET")

    if refresh_token and client_id and client_secret:
        return {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }

    # Fallback to local token files if present
    local_token_paths = [
        LOCAL_TOKEN_FILE,
        BASE_DIR / "token.json",
        Path.home() / ".config" / "gdrive" / "token.json"
    ]
    for p in local_token_paths:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                r_token = data.get("token", {}).get("refresh_token") or data.get("refresh_token")
                c_id = data.get("client_id")
                c_sec = data.get("client_secret")
                if r_token and c_id and c_sec:
                    return {
                        "refresh_token": r_token,
                        "client_id": c_id,
                        "client_secret": c_sec,
                    }
            except Exception as e:
                logger.warning(f"Failed to read local token file {p}: {e}")

    return None


def get_access_token() -> Optional[str]:
    """Requests a fresh OAuth access token using the refresh token."""
    creds = get_oauth_credentials()
    if not creds or not creds.get("refresh_token"):
        logger.warning("No Google Drive OAuth credentials available.")
        return None

    try:
        r = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=15.0,
        )
        if r.status_code == 200:
            return r.json().get("access_token")
        else:
            logger.error(f"Failed to refresh Google token: {r.status_code} {r.text}")
            return None
    except Exception as e:
        logger.error(f"Error refreshing Google token: {e}")
        return None


def ensure_backup_folder(access_token: str) -> Optional[str]:
    """Finds or creates the backup folder in Google Drive."""
    headers = {"Authorization": f"Bearer {access_token}"}
    
    # Check if folder ID is passed via environment
    env_folder_id = os.getenv("GDRIVE_FOLDER_ID")
    if env_folder_id:
        return env_folder_id

    try:
        # Search for existing folder
        q = f"name = '{BACKUP_FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        r = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": q, "fields": "files(id, name)"},
            timeout=15.0,
        )
        if r.status_code == 200:
            files = r.json().get("files", [])
            if files:
                return files[0]["id"]

        # Create folder if not found
        r_create = httpx.post(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            json={
                "name": BACKUP_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
            },
            timeout=15.0,
        )
        if r_create.status_code in (200, 201):
            folder_id = r_create.json().get("id")
            logger.info(f"Created Google Drive folder: {BACKUP_FOLDER_NAME} (ID: {folder_id})")
            return folder_id
        else:
            logger.error(f"Failed to create Google Drive folder: {r_create.text}")
            return None
    except Exception as e:
        logger.error(f"Error in ensure_backup_folder: {e}")
        return None


def backup_database(db_file_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Backs up the SQLite database to Google Drive:
    1. Keeps a master 'btc_pinning.db' updated in place
    2. Keeps a timestamped archive copy 'btc_pinning_YYYYMMDD_HHMM.db'
    """
    target_path = Path(db_file_path) if db_file_path else DB_PATH
    if not target_path.exists():
        return {"success": False, "error": f"Database file not found: {target_path}"}

    access_token = get_access_token()
    if not access_token:
        return {"success": False, "error": "Google Drive authentication failed (no access token)"}

    folder_id = ensure_backup_folder(access_token)
    if not folder_id:
        return {"success": False, "error": "Could not locate or create Google Drive backup folder"}

    headers = {"Authorization": f"Bearer {access_token}"}
    now_bkk = datetime.now(BKK_TZ).strftime("%Y%m%d_%H%M%S")
    db_bytes = target_path.read_bytes()
    size_kb = round(len(db_bytes) / 1024, 2)

    try:
        # 1. Update or create the main 'btc_pinning.db'
        q = f"name = 'btc_pinning.db' and '{folder_id}' in parents and trashed = false"
        r_find = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": q, "fields": "files(id, name, size)"},
            timeout=15.0,
        )
        existing = r_find.json().get("files", []) if r_find.status_code == 200 else []

        if existing:
            remote_size = int(existing[0].get("size", 0))
            if remote_size > 2_000_000 and len(db_bytes) < (remote_size * 0.5):
                logger.warning(f"Aborting master update: local size ({len(db_bytes)}) is much smaller than remote master ({remote_size}). Only saving archive.")
                action = "skipped_master_update"
            else:
                file_id = existing[0]["id"]
                upload_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media"
                r_up = httpx.patch(
                    upload_url,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/x-sqlite3"},
                    content=db_bytes,
                    timeout=60.0,
                )
                action = "updated" if r_up.status_code == 200 else "failed"
        else:
            meta = {
                "name": "btc_pinning.db",
                "parents": [folder_id],
                "description": f"Master BTC Options Pinning Database (Updated {now_bkk})",
            }
            # Multipart upload
            files = {
                "metadata": (None, json.dumps(meta), "application/json; charset=UTF-8"),
                "file": ("btc_pinning.db", db_bytes, "application/x-sqlite3"),
            }
            r_up = httpx.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                headers=headers,
                files=files,
                timeout=30.0,
            )
            action = "created"

        if r_up.status_code not in (200, 201):
            return {"success": False, "error": f"Upload failed: {r_up.text}"}

        # 2. Upload timestamped archive snapshot (e.g. btc_pinning_20261026_222500.db)
        archive_name = f"btc_pinning_{now_bkk}.db"
        archive_meta = {
            "name": archive_name,
            "parents": [folder_id],
            "description": f"Historical Archive Snapshot {now_bkk}",
        }
        r_arch = httpx.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            headers=headers,
            files={
                "metadata": (None, json.dumps(archive_meta), "application/json; charset=UTF-8"),
                "file": (archive_name, db_bytes, "application/x-sqlite3"),
            },
            timeout=30.0,
        )

        msg = f"Database successfully backed up to Google Drive ({action} master + archived {archive_name}, {size_kb} KB)"
        logger.info(msg)
        return {
            "success": True,
            "action": action,
            "size_kb": size_kb,
            "folder_id": folder_id,
            "timestamp": now_bkk,
            "message": msg,
        }

    except Exception as e:
        logger.error(f"Error during Google Drive database backup: {e}")
        return {"success": False, "error": str(e)}


def restore_database_if_missing(db_file_path: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    """
    On cold startup (especially on Render cloud where disk might be wiped or contain stale git clone),
    downloads the richest database from Google Drive if local database is missing,
    has fewer than 10 snapshots, is significantly smaller than remote, or if running in Render cloud.
    """
    target_path = Path(db_file_path) if db_file_path else DB_PATH

    access_token = get_access_token()
    if not access_token:
        return {"restored": False, "error": "No Google Drive access token available"}

    folder_id = ensure_backup_folder(access_token)
    if not folder_id:
        return {"restored": False, "error": "No backup folder found on Google Drive"}

    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        # Find all db files in the backup folder
        q = f"'{folder_id}' in parents and trashed = false and name contains '.db'"
        r_find = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": q, "fields": "files(id, name, size, modifiedTime)", "orderBy": "modifiedTime desc"},
            timeout=15.0,
        )
        files = r_find.json().get("files", []) if r_find.status_code == 200 else []
        if not files:
            return {"restored": False, "error": "No database backups found in Google Drive folder"}

        # Find the richest database file (largest file size)
        best_file = max(files, key=lambda x: int(x.get("size", 0)))
        remote_size = int(best_file.get("size", 0))
        local_size = target_path.stat().st_size if target_path.exists() else 0

        # Check local snapshot count
        local_snapshots = 0
        if target_path.exists() and local_size > 0:
            try:
                import sqlite3
                with sqlite3.connect(target_path) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM snapshots")
                    local_snapshots = cur.fetchone()[0]
            except Exception:
                local_snapshots = 0

        # Needs restore if forced, missing, local has fewer than 10 snapshots, or local is smaller than 80% of remote
        needs_restore = force or (not target_path.exists()) or (local_snapshots < 10) or (local_size < remote_size * 0.8) or bool(os.getenv("RENDER"))

        if not needs_restore:
            return {"restored": False, "reason": f"Local database is already healthy ({local_snapshots} snapshots, {round(local_size/1024, 1)} KB)."}

        file_id = best_file["id"]
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        r_down = httpx.get(download_url, headers=headers, timeout=60.0)
        if r_down.status_code == 200:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(r_down.content)
            size_kb = round(len(r_down.content) / 1024, 2)
            msg = f"Restored {size_kb} KB database ({best_file['name']}) from Google Drive successfully!"
            logger.info(msg)
            try:
                from src.cache import memory_cache
                memory_cache.clear()
            except Exception:
                pass
            return {"restored": True, "size_kb": size_kb, "message": msg, "source_file": best_file["name"]}
        else:
            return {"restored": False, "error": f"Failed downloading database: {r_down.status_code}"}

    except Exception as e:
        logger.error(f"Error restoring database from Google Drive: {e}")
        return {"restored": False, "error": str(e)}
        return {"restored": False, "error": str(e)}
