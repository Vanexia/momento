"""Open a regular file while denying concurrent write and delete access."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from momento.updater.metadata import UpdateMetadataError


@contextmanager
def open_locked_read(path: Path) -> Iterator[BinaryIO]:
    """Hold a read handle whose Windows sharing mode blocks replacement."""
    if path.is_symlink() or not path.is_file():
        raise UpdateMetadataError("Locked update file is not a regular file")
    if os.name != "nt":
        with path.open("rb") as handle:
            yield handle
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ; deny write and delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid_handle):
        raise OSError(ctypes.get_last_error(), "Could not lock update file")
    try:
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
    except Exception:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb", closefd=True) as file_handle:
        yield file_handle
