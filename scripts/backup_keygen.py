#!/usr/bin/env python3
"""Generate a backup encryption keypair (spec #4).

Writes the PRIVATE key to a 0600 file (never to stdout, so it can't land in a
terminal transcript) and prints ONLY the public key + the file path. Put the
public key in the server env as BACKUP_PUBLIC_KEY; keep the private-key file
yourself (password manager + one offline copy). Losing it makes every backup
permanently unrecoverable.

    python3 -m scripts.backup_keygen [private_key_outfile]
"""
import os
import stat
import sys

from bot.offsite_backup.crypto import generate_keypair


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "backup_private_key.txt"
    public_b64, private_b64 = generate_keypair()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(private_b64 + "\n")
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    print("BACKUP_PUBLIC_KEY (put this in the server env — it is NOT secret):")
    print(public_b64)
    print()
    print(f"private key -> {os.path.abspath(out)} (mode 0600) — keep OFFLINE, never on the server, never in git")


if __name__ == "__main__":
    sys.exit(main())
