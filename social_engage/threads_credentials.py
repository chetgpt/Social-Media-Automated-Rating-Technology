"""Exact-account Threads user tokens in the current user's Windows vault.

This module never verifies a token, accesses Meta, enumerates credentials, or
prints secrets. Onboarding must verify the intended account before save_token.
Only the user token is stored; app secrets and app access tokens are excluded.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

from .adapters import AdapterError, numeric, username


class CredentialError(RuntimeError):
    """A fixed diagnostic code, never a native exception or credential value."""


_GENERIC = 1
_LOCAL_MACHINE = 2  # Current Windows user, this computer, across logins.
_NOT_FOUND = 1168
_MAX_BLOB = 2560


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialBackend:
    """Small CredRead/Write/Delete binding; no generic command or enumeration."""

    def __init__(self):
        if os.name != "nt":
            raise CredentialError("windows_credential_manager_required")
        try:
            self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            pointer = ctypes.POINTER(_Credential)
            self.api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
            self.api.CredWriteW.restype = wintypes.BOOL
            self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                          wintypes.DWORD, ctypes.POINTER(pointer)]
            self.api.CredReadW.restype = wintypes.BOOL
            self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
            self.api.CredDeleteW.restype = wintypes.BOOL
            self.api.CredFree.argtypes = [ctypes.c_void_p]
            self.api.CredFree.restype = None
        except Exception:
            raise CredentialError("credential_manager_unavailable") from None

    def write(self, target, account, blob):
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = _Credential(Type=_GENERIC, TargetName=target,
            Comment="Threads official API user token", CredentialBlobSize=len(blob),
            CredentialBlob=buffer, Persist=_LOCAL_MACHINE, UserName=account)
        try:
            if not self.api.CredWriteW(ctypes.byref(credential), 0):
                raise CredentialError("credential_write_failed")
        finally:
            ctypes.memset(buffer, 0, len(buffer))

    def read(self, target):
        pointer = ctypes.POINTER(_Credential)()
        if not self.api.CredReadW(target, _GENERIC, 0, ctypes.byref(pointer)):
            code = "credential_not_found" if ctypes.get_last_error() == _NOT_FOUND else "credential_read_failed"
            raise CredentialError(code)
        try:
            record = pointer.contents
            size = record.CredentialBlobSize
            if (record.Type != _GENERIC or record.TargetName != target or
                    not record.CredentialBlob or not 0 < size <= _MAX_BLOB):
                raise CredentialError("invalid_stored_credential")
            return record.UserName, ctypes.string_at(record.CredentialBlob, size)
        finally:
            # Clear the API's temporary copy before releasing its allocated block.
            if pointer and pointer.contents.CredentialBlob and 0 < pointer.contents.CredentialBlobSize <= _MAX_BLOB:
                ctypes.memset(pointer.contents.CredentialBlob, 0, pointer.contents.CredentialBlobSize)
            self.api.CredFree(pointer)

    def delete(self, target):
        if self.api.CredDeleteW(target, _GENERIC, 0):
            return True
        if ctypes.get_last_error() == _NOT_FOUND:
            return False
        raise CredentialError("credential_delete_failed")


def _binding(app_id, account):
    try:
        return numeric(app_id), username(account)
    except AdapterError:
        raise CredentialError("invalid_credential_binding") from None


def credential_target(app_id, account):
    app_id, account = _binding(app_id, account)
    return "KC-Lab:Threads:" + app_id + ":" + account


def _token_blob(token):
    if (not isinstance(token, str) or not token or token != token.strip() or
            any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)):
        raise CredentialError("invalid_credential_token")
    try:
        blob = token.encode("utf-16-le")
    except UnicodeError:
        raise CredentialError("invalid_credential_token") from None
    if not 0 < len(blob) <= _MAX_BLOB:
        raise CredentialError("invalid_credential_token")
    return blob


def save_token(app_id, account, token, *, backend=None):
    """Store/replace one verified user token. Caller verifies identity first."""
    app_id, account = _binding(app_id, account)
    blob = _token_blob(token)
    (backend or WindowsCredentialBackend()).write(credential_target(app_id, account), account, blob)


def load_token(app_id, account, *, backend=None):
    """Load only this app/account target; reject inconsistent vault metadata."""
    app_id, account = _binding(app_id, account)
    stored_account, blob = (backend or WindowsCredentialBackend()).read(credential_target(app_id, account))
    if stored_account != account or not isinstance(blob, bytes) or not 0 < len(blob) <= _MAX_BLOB:
        raise CredentialError("invalid_stored_credential")
    try:
        token = blob.decode("utf-16-le")
        _token_blob(token)
    except (UnicodeError, CredentialError):
        raise CredentialError("invalid_stored_credential") from None
    return token


def delete_token(app_id, account, *, backend=None):
    """Remove only this exact entry; return False when it was already absent."""
    target = credential_target(app_id, account)
    return (backend or WindowsCredentialBackend()).delete(target)
