"""Encrypt an already consistent SQLite copy with AES-GCM and a Windows DPAPI key.

The key is recoverable by the same Windows user/profile. This is a local backup,
not a portable credential backup. Keep both ciphertext and its JSON receipt.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AAD = b"tiktok-registry-backup-v1"


def protect(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def encrypt(source: Path, target: Path) -> dict:
    receipt_path = Path(str(target) + ".json")
    if target.exists() or receipt_path.exists():
        raise FileExistsError("backup target/receipt already exists")
    key, nonce = os.urandom(32), os.urandom(12)
    protected_key = protect(key)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(AAD)
    sha = hashlib.sha256()
    size = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        for chunk in iter(lambda: reader.read(8 * 1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)
            writer.write(encryptor.update(chunk))
        writer.write(encryptor.finalize())
        writer.flush()
        os.fsync(writer.fileno())
    receipt = {"schema": AAD.decode(), "algorithm": "AES-256-GCM",
               "key_protection": "Windows-DPAPI-current-user",
               "nonce": nonce.hex(), "tag": encryptor.tag.hex(),
               "protected_key": base64.b64encode(protected_key).decode(),
               "plaintext_sha256": sha.hexdigest(), "plaintext_bytes": size}
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    return receipt


def verify(source: Path, *, restore_to: Path | None = None) -> dict:
    receipt = json.loads(Path(str(source) + ".json").read_text(encoding="utf-8"))
    if receipt.get("schema") != AAD.decode() or receipt.get("algorithm") != "AES-256-GCM":
        raise ValueError("unsupported encrypted backup format")
    key = protect(base64.b64decode(receipt["protected_key"]), decrypt=True)
    decryptor = Cipher(algorithms.AES(key), modes.GCM(bytes.fromhex(receipt["nonce"]),
                                                     bytes.fromhex(receipt["tag"]))).decryptor()
    decryptor.authenticate_additional_data(AAD)
    writer = restore_to.open("xb") if restore_to is not None else None
    sha, size = hashlib.sha256(), 0
    try:
        with source.open("rb") as reader:
            for chunk in iter(lambda: reader.read(8 * 1024 * 1024), b""):
                plain = decryptor.update(chunk)
                sha.update(plain)
                size += len(plain)
                if writer:
                    writer.write(plain)
        final = decryptor.finalize()
        sha.update(final)
        size += len(final)
        if writer:
            writer.write(final)
            writer.flush()
            os.fsync(writer.fileno())
        if sha.hexdigest() != receipt["plaintext_sha256"] or size != receipt["plaintext_bytes"]:
            raise ValueError("encrypted backup plaintext checksum mismatch")
    except BaseException:
        if writer:
            writer.close()
            restore_to.unlink()  # Only the new, exclusively created failed output.
        raise
    finally:
        if writer:
            writer.close()
    return {"verified": True, "plaintext_sha256": sha.hexdigest(), "bytes": size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["encrypt", "verify", "restore"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args()
    if args.command in {"encrypt", "restore"} and args.target is None:
        parser.error("--target is required")
    if args.command == "encrypt":
        encrypt(args.source, args.target)
        result = verify(args.target)
    else:
        result = verify(args.source, restore_to=args.target if args.command == "restore" else None)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
