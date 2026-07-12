"""Asymmetric encryption for off-site backups (spec #4).

libsodium sealed boxes (Curve25519 + XSalsa20-Poly1305, via PyNaCl): the server
holds only the PUBLIC key and can encrypt but CANNOT decrypt; the PRIVATE key
lives only with the owner, offline, and is the sole thing that can open a backup.
So a leaked channel — or even a compromised server — never exposes backup
contents (the server has no decryption secret at all).

Keys are standard base64 of the raw 32-byte values. Public key -> server env
BACKUP_PUBLIC_KEY. Private key -> the owner, never on the server, never in git.
Lose the private key and every backup is unrecoverable — keep it in a password
manager plus one offline copy.
"""

import base64

from nacl.public import PrivateKey, PublicKey, SealedBox


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def generate_keypair():
    """Return (public_b64, private_b64) for a fresh keypair."""
    secret = PrivateKey.generate()
    return _b64(bytes(secret.public_key)), _b64(bytes(secret))


def load_public_key(value):
    """PublicKey from a base64 string, or None when empty/unset."""
    value = (value or "").strip()
    if not value:
        return None
    return PublicKey(base64.b64decode(value))


def load_private_key(value):
    return PrivateKey(base64.b64decode((value or "").strip()))


def encrypt_file(public_key, src_path, dst_path):
    """Seal src_path -> dst_path with the public key."""
    with open(src_path, "rb") as fin:
        sealed = SealedBox(public_key).encrypt(fin.read())
    with open(dst_path, "wb") as fout:
        fout.write(sealed)
    return dst_path


def decrypt_file(private_key, src_path, dst_path):
    """Open a sealed src_path -> dst_path with the private key (restore side)."""
    with open(src_path, "rb") as fin:
        opened = SealedBox(private_key).decrypt(fin.read())
    with open(dst_path, "wb") as fout:
        fout.write(opened)
    return dst_path
