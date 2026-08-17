"""Single source of truth for classifying files in the recordings folder.

The repair flow (:class:`momento.core.media_probe.RepairJob`) writes work
files next to a recording whose names still end in ``.mkv``:

  * ``<name>.repairing.mkv`` — the in-progress ``ffmpeg -c copy`` re-mux.
  * ``<name>.broken-bak.mkv`` — a legacy pre-swap backup written by older
    builds. The current repair no longer creates these, but a user upgrading
    from an older exe may still have one on disk, so we keep recognising it.

A naive ``suffix == ".mkv"`` check treats both as real recordings, which is
exactly the bug this module exists to prevent: every place that lists the
output folder (the editor's metadata-probe + thumbnail jobs, the crash-
recovery scan, storage cleanup, the clip migration) would otherwise open the
temp. On Windows those read handles race the repair's atomic swap and make it
fail with a sharing violation (``WinError 32``); the crash-recovery scan would
even try to "repair" the temp recursively. Keep this the *only* place that
knows which ``.mkv`` files are not recordings.
"""

from __future__ import annotations

from pathlib import Path

# Recording / clip media containers the editor lists.
RECORDING_SUFFIXES = (".mkv", ".mp4")

# Work files the repair flow writes alongside a recording. Both end in
# ``.mkv`` but must never be treated as recordings.
REPAIR_TMP_SUFFIX = ".repairing.mkv"
REPAIR_BACKUP_SUFFIX = ".broken-bak.mkv"
_REPAIR_TEMP_SUFFIXES = (REPAIR_TMP_SUFFIX, REPAIR_BACKUP_SUFFIX)


def is_repair_temp(path: Path | str) -> bool:
    """True if ``path`` is a transient repair work file, not a recording.

    Matches on the full filename ending (not :attr:`Path.suffix`, which only
    sees the final ``.mkv``) so ``Wow_..._190515.repairing.mkv`` is caught.
    """
    name = Path(path).name.lower()
    return any(name.endswith(suffix) for suffix in _REPAIR_TEMP_SUFFIXES)


def is_recording_file(path: Path | str) -> bool:
    """True if ``path`` is a listable recording/clip media file.

    Combines the suffix check with the repair-temp exclusion so callers can
    enumerate a folder with one predicate.
    """
    p = Path(path)
    return p.suffix.lower() in RECORDING_SUFFIXES and not is_repair_temp(p)
