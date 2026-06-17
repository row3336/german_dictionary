"""Dump-file creation helpers (DB / logs / suggestions copies into the dump dir).

Leaf module: depends only on app_config paths + stdlib. Shared by the dump
handler and the auto-dump job.
"""

import shutil
from datetime import datetime, timezone

from ..app_config import get_db_path, get_dump_dir, get_log_path, get_suggestions_path


def _create_dump_file():
    """Handle create dump file."""
    db_path = get_db_path()
    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump_path = dump_dir / f"words-{stamp}.db"
    shutil.copy2(db_path, dump_path)
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


def _create_suggestions_dump_file():
    """Handle create suggestions dump file."""
    src = get_suggestions_path()
    if not src.exists() or src.stat().st_size == 0:
        return None

    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = dump_dir / f"suggested-words-{stamp}.txt"
    shutil.copy2(src, out_path)
    return out_path
