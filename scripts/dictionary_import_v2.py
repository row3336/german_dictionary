"""Explicit-path dictionary v2 preparation/preflight/apply; default is read-only."""
import argparse
import json
from pathlib import Path
import sqlite3
from bot.common import data18_builder as builder, import_package_v2 as importer


def _connection(path, *, writable=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "validate", "bind", "preflight", "apply"))
    parser.add_argument("--source-root")
    parser.add_argument("--package")
    parser.add_argument("--db", help="Explicit database path; never inferred or migrated")
    parser.add_argument("--output", help="New output file; existing files are never overwritten")
    parser.add_argument("--approval", help="Explicit owner/linguistic manifest, per-ID confirmations and repair guards")
    parser.add_argument("--apply", action="store_true", dest="opt_in")
    parser.add_argument("--approved-digest")
    parser.add_argument("--integration-qa-evidence", help="Separate verified QA JSON bound to package digest")
    args = parser.parse_args(argv)
    if args.command == "build":
        if not args.source_root:
            parser.error("build requires --source-root")
        result = builder.build_package(args.source_root)
    else:
        if not args.package:
            parser.error("--package is required")
        package = builder.load_json(args.package)
        if args.command == "validate":
            result = builder.validate_content(package)
        else:
            if not args.db:
                parser.error("--db is required; there is no default target")
            approval = builder.load_json(args.approval) if args.approval else None
            if args.command == "apply":
                if not args.opt_in or not args.approved_digest or approval != package.get("approvals"):
                    parser.error("apply requires --apply, --approved-digest and exact --approval manifest")
                verified = False
                if args.integration_qa_evidence:
                    qa = builder.load_json(args.integration_qa_evidence)
                    if qa.get("verified") is not True or qa.get("package_digest") != package["digest"] or not qa.get("evidence"):
                        parser.error("integration QA evidence must verify this exact bound package")
                    verified = True
                conn = _connection(args.db,writable=True)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    result = importer.apply(conn,package,approved_digest=args.approved_digest,integration_verified=verified)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            else:
                conn = _connection(args.db)
                try:
                    conn.execute("BEGIN")
                    result = importer.bind_package(conn,package,approval) if args.command == "bind" else importer.preflight(conn,package)
                    conn.rollback()
                finally:
                    conn.close()
    raw = json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2) + "\n"
    if args.output:
        with Path(args.output).open("x",encoding="utf-8") as stream:
            stream.write(raw)
    else:
        print(raw,end="")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
