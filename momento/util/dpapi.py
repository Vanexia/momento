"""Thin ctypes wrapper around Windows DPAPI for at-rest secret encryption.

Used by ``momento/youtube/auth.py`` to encrypt the YouTube OAuth refresh
token before writing it to disk. DPAPI binds ciphertext to the current
Windows user account — even with full filesystem access on another
machine, an attacker can't decrypt it without that user's logon session.

Two-function surface so the auth layer doesn't have to think about COM
or pointer types:

- ``protect(plaintext: bytes) -> bytes``
- ``unprotect(ciphertext: bytes) -> bytes``

Both raise ``DPAPIError`` (an ``OSError`` subclass) on failure so callers
can ``except OSError`` and bin the corrupt token blob without crashing
the app.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)


class DPAPIError(OSError):
    """Raised when CryptProtectData / CryptUnprotectData fails."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


# CRYPTPROTECT_UI_FORBIDDEN — never prompt the user. We're running headless
# from the user's logon session; if DPAPI needs UI it means the call would
# block forever in a service-style context, which we'd rather fail loudly.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _load_apis() -> tuple:
    """Resolve crypt32 + kernel32 entry points. Called once per process."""
    if sys.platform != "win32":
        raise DPAPIError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),     # pDataIn
        wintypes.LPCWSTR,              # szDataDescr (description, unused)
        ctypes.POINTER(_DataBlob),     # pOptionalEntropy
        ctypes.c_void_p,               # pvReserved
        ctypes.c_void_p,               # pPromptStruct
        wintypes.DWORD,                # dwFlags
        ctypes.POINTER(_DataBlob),     # pDataOut
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),     # pDataIn
        ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr (we pass NULL)
        ctypes.POINTER(_DataBlob),     # pOptionalEntropy
        ctypes.c_void_p,               # pvReserved
        ctypes.c_void_p,               # pPromptStruct
        wintypes.DWORD,                # dwFlags
        ctypes.POINTER(_DataBlob),     # pDataOut
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    return crypt32, kernel32


_CRYPT32, _KERNEL32 = _load_apis() if sys.platform == "win32" else (None, None)


def _bytes_to_blob(data: bytes) -> _DataBlob:
    buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    blob = _DataBlob()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
    # Keep ``buf`` alive: attach it to the blob so it isn't GC'd before the
    # API call reads it. Without this the cast above can hand DPAPI a dead
    # pointer.
    blob._keep_alive = buf  # type: ignore[attr-defined]
    return blob


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    if not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(plaintext: bytes) -> bytes:
    """Encrypt ``plaintext`` with DPAPI, return opaque ciphertext.

    Output is bound to the current Windows user account. Decryption from any
    other user (or any other machine) will fail with ``DPAPIError``.
    """
    if _CRYPT32 is None or _KERNEL32 is None:
        raise DPAPIError("DPAPI not available on this platform")
    if not plaintext:
        return b""

    in_blob = _bytes_to_blob(plaintext)
    out_blob = _DataBlob()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(in_blob),
        None,                        # description string — not stored
        None,                        # entropy — not used
        None, None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.GetLastError()
        raise DPAPIError(f"CryptProtectData failed (GetLastError={err})")

    try:
        return _blob_to_bytes(out_blob)
    finally:
        if out_blob.pbData:
            _KERNEL32.LocalFree(out_blob.pbData)


def unprotect(ciphertext: bytes) -> bytes:
    """Decrypt a blob previously produced by ``protect()``.

    Raises ``DPAPIError`` on tampering, wrong user account, or wrong
    machine. Callers should treat the failure as "discard this token blob,
    user needs to re-auth" rather than retrying.
    """
    if _CRYPT32 is None or _KERNEL32 is None:
        raise DPAPIError("DPAPI not available on this platform")
    if not ciphertext:
        return b""

    in_blob = _bytes_to_blob(ciphertext)
    out_blob = _DataBlob()
    ok = _CRYPT32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,                        # discard the description string
        None,
        None, None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.GetLastError()
        raise DPAPIError(f"CryptUnprotectData failed (GetLastError={err})")

    try:
        return _blob_to_bytes(out_blob)
    finally:
        if out_blob.pbData:
            _KERNEL32.LocalFree(out_blob.pbData)
