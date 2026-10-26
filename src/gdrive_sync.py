"""
Google Drive Sync Bridge Module.
Delegates to src.gdrive_backup for robust backup/restore operations.
"""
from typing import Optional
from src.gdrive_backup import (
    backup_database,
    restore_database_if_missing,
    get_access_token,
    ensure_backup_folder,
)


def upload_db_to_gdrive() -> bool:
    """Uploads/updates master database and snapshot archive to Google Drive."""
    res = backup_database()
    return bool(res.get("success", False))


def download_db_from_gdrive(force: bool = False) -> bool:
    """Downloads master database from Google Drive."""
    res = restore_database_if_missing(force=force)
    return bool(res.get("restored", False))
