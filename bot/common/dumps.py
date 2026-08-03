"""Dump-file creation helpers (DB / logs / suggestions copies into the dump dir).

Depends on app_config paths + common.db_safety (WAL-safe DB backup) + stdlib.
Shared by the dump handler and the auto-dump job.
"""

import shutil
from datetime import datetime, timezone

from ..app_config import get_db_path, get_dump_dir, get_log_path
from .db_safety import backup_database


def _create_dump_file():
    """Handle create dump file."""
    db_path = get_db_path()
    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump_path = dump_dir / f"words-{stamp}.db"
    # WAL-safe: consistent snapshot via backup API, not a raw file copy (which
    # could miss transactions still in the -wal sidecar). See db_safety.backup_database.
    backup_database(db_path, dump_path)
    return dump_path


def _create_logs_dump_file():
    """Handle create logs dump file."""
    log_path = get_log_path()
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None

    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = dump_dir / f"bot-logs-{stamp}.txt"
    shutil.copy2(log_path, out_path)
    return out_path
