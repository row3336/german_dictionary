"""Explicit-path PONS/data18 import; validation and preflight are read-only."""

import argparse
import json
from pathlib import Path
import sqlite3

from bot.common import data18_builder as builder, import_package_v3 as importer


def connection(path, *, writable=False):
    uri = Path(path).resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "bind", "preflight", "apply"))
    parser.add_argument("--package", required=True)
    parser.add_argument("--db")
    parser.add_argument("--approval")
    parser.add_argument("--output")
    parser.add_argument("--apply", action="store_true", dest="opt_in")
    parser.add_argument("--approved-digest")
    parser.add_argument("--integration-qa-evidence")
    args = parser.parse_args(argv)
    package = builder.load_json(args.package)
    if args.command == "validate":
        result = importer.validate_source(package)
    else:
        if not args.db:
            parser.error("--db is required; there is no implicit target")
        if args.command == "bind":
            if not args.approval or not args.output:
                parser.error("bind requires --approval and a new --output file")
            approval = builder.load_json(args.approval)
            with connection(args.db) as conn:
                conn.execute("BEGIN")
                try:
                    result = importer.bind_package(conn, package, approval)
                finally:
                    conn.rollback()
        elif args.command == "preflight":
            with connection(args.db) as conn:
                conn.execute("BEGIN")
                try:
                    result = importer.preflight(conn, package)
                finally:
                    conn.rollback()
        else:
            if (not args.opt_in or not args.approved_digest or not args.approval
                    or builder.load_json(args.approval) != package.get("approvals")):
                parser.error("apply requires --apply, --approved-digest and exact --approval manifest")
            verified = False
            if args.integration_qa_evidence:
                qa = builder.load_json(args.integration_qa_evidence)
                if (qa.get("verified") is not True or qa.get("package_digest") != package.get("digest")
                        or not qa.get("evidence")):
                    parser.error("integration QA evidence must verify the exact bound package")
                verified = True
            conn = connection(args.db, writable=True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = importer.apply(conn, package, approved_digest=args.approved_digest,
                                        integration_verified=verified)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as stream:
            stream.write(output)
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
