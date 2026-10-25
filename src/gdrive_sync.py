import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple
import httpx
from src.config import BASE_DIR, DATA_DIR

logger = logging.getLogger("gdrive_sync")

DB_FILE = DATA_DIR / "btc_pinning.db"
FOLDER_NAME = "BTC_Options_Pinning_Data"

# Load credentials from Environment or local token.json
def get_credentials() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")

    if not (client_id and client_secret and refresh_token):
        local_token_paths = [
            Path(r"C:\Users\Noname\.gemini\antigravity\plugin_data\gdrive\token.json"),
            BASE_DIR / "token.json",
            Path.home() / ".config" / "gdrive" / "token.json"
        ]
        for p in local_token_paths:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        client_id = data.get("client_id")
                        client_secret = data.get("client_secret")
                        refresh_token = data.get("token", {}).get("refresh_token")
                        if client_id and client_secret and refresh_token:
                            break
                except Exception as e:
                    logger.warning(f"Error reading local token from {p}: {e}")

    return client_id, client_secret, refresh_token


def get_access_token() -> Optional[str]:
    client_id, client_secret, refresh_token = get_credentials()
    if not (client_id and client_secret and refresh_token):
        logger.warning("Google Drive credentials not configured.")
        return None

    try:
        r = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15.0,
        )
        if r.status_code == 200:
            return r.json().get("access_token")
        else:
            logger.error(f"Failed to refresh Google Drive token: {r.status_code} {r.text}")
            return None
    except Exception as e:
        logger.error(f"Error refreshing Google Drive token: {e}")
        return None


def get_or_create_folder(access_token: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        # Search for existing folder
        query = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        r = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": query, "spaces": "drive", "fields": "files(id, name)"},
            headers=headers,
            timeout=15.0,
        )
        if r.status_code == 200:
            files = r.json().get("files", [])
            if files:
                return files[0]["id"]

        # Create folder if not found
        create_meta = {
            "name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
        }
        r_create = httpx.post(
            "https://www.googleapis.com/drive/v3/files",
            json=create_meta,
            headers=headers,
            timeout=15.0,
        )
        if r_create.status_code in (200, 201):
            folder_id = r_create.json().get("id")
            logger.info(f"Created Google Drive folder '{FOLDER_NAME}': {folder_id}")
            return folder_id
        else:
            logger.error(f"Failed to create Google Drive folder: {r_create.status_code} {r_create.text}")
            return None
    except Exception as e:
        logger.error(f"Error getting/creating Google Drive folder: {e}")
        return None


def upload_db_to_gdrive() -> bool:
    """Upload or update btc_pinning.db into the Google Drive folder."""
    if not DB_FILE.exists():
        logger.warning(f"Database file not found at {DB_FILE}")
        return False

    token = get_access_token()
    if not token:
        return False

    folder_id = get_or_create_folder(token)
    if not folder_id:
        return False

    headers = {"Authorization": f"Bearer {token}"}
    db_name = DB_FILE.name

    try:
        # Check if btc_pinning.db already exists in the folder
        query = f"name = '{db_name}' and '{folder_id}' in parents and trashed = false"
        r_search = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": query, "spaces": "drive", "fields": "files(id, name)"},
            headers=headers,
            timeout=15.0,
        )
        existing_id = None
        if r_search.status_code == 200:
            files = r_search.json().get("files", [])
            if files:
                existing_id = files[0]["id"]

        with open(DB_FILE, "rb") as f:
            content_bytes = f.read()

        if existing_id:
            # Update existing file
            upload_url = f"https://www.googleapis.com/upload/drive/v3/files/{existing_id}?uploadType=media"
            r_up = httpx.patch(
                upload_url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-sqlite3"},
                content=content_bytes,
                timeout=60.0,
            )
            if r_up.status_code in (200, 201):
                logger.info(f"Successfully updated {db_name} on Google Drive (ID: {existing_id}, size: {len(content_bytes):,} bytes)")
                return True
            else:
                logger.error(f"Failed to update {db_name} on Google Drive: {r_up.status_code} {r_up.text}")
                return False
        else:
            # Create new file via multipart upload
            upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
            meta = {"name": db_name, "parents": [folder_id]}
            files = {
                "metadata": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
                "file": (db_name, content_bytes, "application/x-sqlite3"),
            }
            r_up = httpx.post(upload_url, headers=headers, files=files, timeout=60.0)
            if r_up.status_code in (200, 201):
                new_id = r_up.json().get("id")
                logger.info(f"Successfully uploaded {db_name} to Google Drive (ID: {new_id}, size: {len(content_bytes):,} bytes)")
                return True
            else:
                logger.error(f"Failed to upload {db_name} to Google Drive: {r_up.status_code} {r_up.text}")
                return False
    except Exception as e:
        logger.error(f"Exception during Google Drive upload: {e}")
        return False


def download_db_from_gdrive() -> bool:
    """Download the latest btc_pinning.db from Google Drive if available."""
    token = get_access_token()
    if not token:
        return False

    folder_id = get_or_create_folder(token)
    if not folder_id:
        return False

    headers = {"Authorization": f"Bearer {token}"}
    db_name = DB_FILE.name

    try:
        query = f"name = '{db_name}' and '{folder_id}' in parents and trashed = false"
        r_search = httpx.get(
            "https://www.googleapis.com/drive/v3/files",
            params={"q": query, "spaces": "drive", "fields": "files(id, name, modifiedTime)"},
            headers=headers,
            timeout=15.0,
        )
        if r_search.status_code != 200:
            return False

        files = r_search.json().get("files", [])
        if not files:
            logger.info("No remote database found on Google Drive to download.")
            return False

        remote_file_id = files[0]["id"]
        logger.info(f"Downloading {db_name} from Google Drive (ID: {remote_file_id})...")

        r_down = httpx.get(
            f"https://www.googleapis.com/drive/v3/files/{remote_file_id}?alt=media",
            headers=headers,
            timeout=60.0,
        )
        if r_down.status_code == 200:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(DB_FILE, "wb") as f:
                f.write(r_down.content)
            logger.info(f"Successfully downloaded and restored {db_name} ({len(r_down.content):,} bytes) from Google Drive!")
            return True
        else:
            logger.error(f"Failed to download {db_name} from Google Drive: {r_down.status_code}")
            return False
    except Exception as e:
        logger.error(f"Exception during Google Drive download: {e}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing Google Drive upload...")
    ok = upload_db_to_gdrive()
    print("Upload result:", ok)
