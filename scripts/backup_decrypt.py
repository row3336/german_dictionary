#!/usr/bin/env python3
"""Decrypt an off-site backup (spec #4, restore side).

    python3 -m scripts.backup_decrypt <backup.db.gz.enc> <private_key_file> [out.db.gz]

Reads the base64 private key from the given file and decrypts to a .gz (default:
the source name with the .enc suffix stripped), which you then gunzip. Run this
on YOUR machine — the private key must never touch the server.
"""
import sys

from bot.offsite_backup.crypto import decrypt_file, load_private_key


def main():
    if len(sys.argv) < 3:
        print("usage: backup_decrypt.py <backup.enc> <private_key_file> [out.gz]", file=sys.stderr)
        return 2
    src, keyfile = sys.argv[1], sys.argv[2]
    dst = sys.argv[3] if len(sys.argv) > 3 else (src[:-4] if src.endswith(".enc") else src + ".dec")
    with open(keyfile) as handle:
        key = load_private_key(handle.read())
    decrypt_file(key, src, dst)
    print("decrypted ->", dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
