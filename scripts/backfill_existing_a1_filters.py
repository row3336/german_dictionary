"""Explicit prepare/check/apply CLI for the one-time existing-user A1 filter.

Standalone stdlib operation: no bot configuration, startup, DDL or services.
The operator's release wrapper is responsible for stopping the bot beforehand.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.common import existing_user_filters as filters
from bot.common.db_safety import _connection_fingerprint


def open_database(path, *, readonly=True):
    """mode=ro/rw prevents a missing database from being created."""
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("database must be an existing regular file")
    mode = "ro" if readonly else "rw"
    conn = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, isolation_level=None)
    try:
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.close()
        raise
    return conn


def verified_backup(db_path, backup_path, baseline):
    """Back up through a SECOND ro connection while the caller reserves writes.

    An unsuccessful artifact is retained for diagnosis and never auto-restored.
    The established db_safety fingerprint includes both logical rows and schema.
    """
    destination = Path(backup_path).absolute()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    source = open_database(db_path)
    try:
        source.execute("BEGIN")
        if _connection_fingerprint(source) != baseline:
            raise ValueError("backup source differs from reserved baseline")
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            integrity = [row[0] for row in target.execute("PRAGMA integrity_check")]
            if integrity != ["ok"] or target.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("backup integrity or foreign-key check failed")
            if _connection_fingerprint(target) != baseline:
                raise ValueError("backup differs from reserved baseline")
        finally:
            target.close()
    finally:
        source.close()
    return {"path": str(destination.resolve()),
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def prepare_file(db_path, manifest_path):
    conn = open_database(db_path)
    try:
        conn.execute("BEGIN")
        manifest = filters.prepare(conn)
    finally:
        conn.close()
    # Exclusive creation is intentional: a reviewed cohort cannot be overwritten.
    with Path(manifest_path).open("x", encoding="utf-8") as stream:
        stream.write(filters.canonical_json(manifest) + "\n")
    return {"state": "prepared", "targets": len(manifest["targets"]),
            "digest": manifest["digest"], "manifest": str(Path(manifest_path).absolute())}


def read_manifest(path):
    return filters.validate_manifest(filters.load_json(Path(path).read_text(encoding="utf-8")))


def check_file(db_path, manifest_path):
    manifest = read_manifest(manifest_path)
    conn = open_database(db_path)
    try:
        conn.execute("BEGIN")
        return filters.check(conn, manifest)
    finally:
        conn.close()


def apply_file(db_path, manifest_path, backup_path):
    manifest = read_manifest(manifest_path)
    conn = open_database(db_path, readonly=False)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        report = filters.check(conn, manifest)
        if report["state"] == "already_applied":
            conn.rollback()
            return report
        baseline = _connection_fingerprint(conn)
        backup = verified_backup(db_path, backup_path, baseline)
        report = filters.apply(conn, manifest)
        conn.commit()
        committed = True
        return {**report, "backup": backup}
    except Exception:
        if not committed:
            conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "check", "apply"):
        sub = commands.add_parser(command)
        sub.add_argument("--db", required=True, help="existing schema-20 SQLite database")
        sub.add_argument("--manifest", required=True, help="explicit reviewed manifest path")
        if command == "apply":
            sub.add_argument("--backup", required=True, help="new pre-apply snapshot file")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare_file(args.db, args.manifest)
        elif args.command == "check":
            report = check_file(args.db, args.manifest)
        else:
            report = apply_file(args.db, args.manifest, args.backup)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
