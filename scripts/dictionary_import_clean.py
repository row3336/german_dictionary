"""Explicit-path import of the approved clean bundle; only `apply` writes.

validate             pure source check (exact approved SHA, contracts, counts)
repair-draft         read-only gd-study-repair/v1 skeleton from live generations;
                     approvals stay null until the owner sets and reseals them
bind / preflight     read-only runtime envelope for one schema22 database
apply                --apply opt-in, exact --approved-digest and --approval
There is no default database and no migration: run scripts.migrate_db first.
Apply output is provisional until committed=true. The database receipt is
authoritative; publishing a result file is not atomic with the DB commit.
"""

import argparse
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import sys

from bot.common import data18_builder as builder, import_clean_bundle as importer


def connection(path, *, writable=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _read_only(path, action):
    with closing(connection(path)) as conn:
        conn.execute("BEGIN")
        try:
            return action(conn)
        finally:
            conn.rollback()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "repair-draft", "bind", "preflight", "apply"))
    parser.add_argument("--source", help="approved preparation bundle (validate/repair-draft/bind)")
    parser.add_argument("--bound", help="bound envelope (preflight/apply)")
    parser.add_argument("--db")
    parser.add_argument("--approval")
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true", dest="opt_in")
    parser.add_argument("--approved-digest")
    parser.add_argument("--integration-qa-evidence")
    args = parser.parse_args(argv)
    # Reserve the output before opening a write transaction (including races).
    with ExitStack() as stack:
        stream = stack.enter_context(Path(args.output).open("x", encoding="utf-8")) if args.output else None
        return _execute(args, parser, stream)


def _encode(result):
    return json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _output(result, stream):
    output = _encode(result)
    if stream is None:
        print(output, end="", flush=True)
    else:
        stream.seek(0)
        stream.write(output)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def _execute(args, parser, stream):
    if args.command in ("validate", "repair-draft", "bind"):
        if not args.source:
            parser.error(args.command + " requires --source")
        bundle, sha = importer.load_source(args.source)
    else:
        if not args.bound:
            parser.error(args.command + " requires --bound")
        bound = builder.load_json(args.bound)
    if args.command == "validate":
        result = importer.validate_source(bundle, sha)
    else:
        if not args.db:
            parser.error("--db is required; there is no implicit target")
        if args.command == "repair-draft":
            result = _read_only(args.db, lambda conn: importer.draft_repair_manifest(conn, bundle, sha))
        elif args.command == "bind":
            if not args.approval or not args.output:
                parser.error("bind requires --approval and a new --output file")
            approval = builder.load_json(args.approval)
            result = _read_only(args.db, lambda conn: importer.bind(conn, bundle, sha, approval))
        elif args.command == "preflight":
            result = _read_only(args.db, lambda conn: importer.preflight(conn, bound))
        else:
            if (not args.opt_in or not args.approved_digest or not args.approval
                    or builder.load_json(args.approval) != bound.get("approvals")):
                parser.error("apply requires --apply, --approved-digest and the exact --approval manifest")
            verified = False
            if args.integration_qa_evidence:
                qa = builder.load_json(args.integration_qa_evidence)
                if (qa.get("verified") is not True or qa.get("package_digest") != bound.get("digest")
                        or not qa.get("evidence")):
                    parser.error("integration QA evidence must verify the exact bound envelope")
                verified = True
            conn = connection(args.db, writable=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = importer.apply(conn, bound, approved_digest=args.approved_digest,
                                        integration_verified=verified)
                result = {**result, "committed": False, "operation_id": bound["operation_id"]}
                # Validate serialization before commit even for stdout. A file
                # contains only a provisional receipt if commit later fails.
                _encode(result)
                if stream is not None:
                    _output(result, stream)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            try:
                _output({**result, "committed": True}, stream)
                if stream is not None:
                    stream.close()
            except Exception as exc:
                message = ("БД зафиксирована, вывод ошибка: committed=true operation_id="
                           + bound["operation_id"] + "; database receipt is authoritative")
                print(message, file=sys.stderr, flush=True)
                raise RuntimeError(message) from exc
            return 0
    _output(result, stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
