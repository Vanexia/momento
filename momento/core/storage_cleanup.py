"""Disk-budget enforcement for the recordings folder.

When ``Config.max_storage_gb > 0`` Momento deletes the oldest top-level
``.mkv`` recordings (and their sidecars) until the folder's total size sits
under the limit. Clips and their sidecars in ``<output>/clips/`` are never
considered — they're user-curated and persistent on purpose.

Runs at startup and at the end of every recording. The work is cheap (stat
+ unlink) and synchronous; the caller just calls :func:`enforce_storage_limit`.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from momento.core.recording_files import is_repair_temp
from momento.core.recording_safety import (
    OWNERSHIP_SIDECAR_SUFFIX,
    has_valid_ownership_marker,
    is_file_active,
    is_momento_owned,
    mark_recording_owned,
    ownership_sidecar_path,
)

logger = logging.getLogger(__name__)

_SIDECAR_SUFFIXES = (
    ".thumb.jpg",
    ".bookmarks.json",
    OWNERSHIP_SIDECAR_SUFFIX,
)
_MEDIA_SUFFIXES = (".mkv", ".mp4")


def enforce_storage_limit(folder: Path, max_gb: int) -> int:
    """Delete oldest recordings until the folder's recording total is under
    ``max_gb``. Returns the number of recordings deleted.

    ``max_gb`` <= 0 disables enforcement.
    """
    if max_gb <= 0:
        return 0
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    limit_bytes = max_gb * (1024 ** 3)

    recordings: list[tuple[float, Path, int]] = []
    try:
        for p in folder.iterdir():
            # Count + evict every suffix the editor treats as a recording
            # (.mkv AND legacy root-folder .mp4) — not just .mkv — or the budget
            # silently undercounts and over-deletes .mkv chasing a total it can
            # never satisfy. clips/ is a subdir, never enumerated here.
            if not p.is_file() or p.suffix.lower() not in _MEDIA_SUFFIXES:
                continue
            if is_repair_temp(p):
                continue  # mid-repair work file — not a recording to count/delete
            if not is_momento_owned(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            recordings.append((st.st_mtime, p, st.st_size))
    except OSError:
        return 0

    total = sum(size for _, _, size in recordings)
    if total <= limit_bytes:
        return 0

    recordings.sort(key=lambda t: t[0])  # oldest first
    deleted = 0
    for _, path, size in recordings:
        if total <= limit_bytes:
            break
        if is_file_active(path):
            logger.info(
                "Storage cleanup: skipping %s — file activity in flight", path.name
            )
            continue
        try:
            path.unlink()
        except OSError as e:
            logger.warning("Storage cleanup: couldn't delete %s: %s", path.name, e)
            continue
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = path.with_name(path.name + suffix)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
        total -= size
        deleted += 1
        logger.info(
            "Storage cleanup: removed %s (%.1f MiB) — over %d GiB limit",
            path.name, size / (1024 ** 2), max_gb,
        )
    return deleted


class MigrationWorker:
    """Move every recording + clip + sidecar from ``old`` to ``new``.

    Cross-drive moves go through copy-then-delete, so per-multi-GB
    recording the worker must run off the UI thread — the caller wires a
    ``progress_callback`` to update a progress dialog. Files already
    present at the destination are skipped (counted as failed). Sidecars
    follow their parent and don't tick the counter.
    """

    def __init__(self, old: Path, new: Path) -> None:
        self._old = Path(old)
        self._new = Path(new)

    def run(
        self,
        pairs: list[tuple[Path, Path]] | None = None,
        progress_callback=None,
    ) -> tuple[int, int]:
        """Execute the migration. ``pairs`` lets the caller skip a
        second :meth:`collect_media_pairs` walk when it already enumerated
        the source for a pre-flight count.
        """
        if pairs is None:
            pairs = self.collect_media_pairs()
        total = len(pairs)
        moved = failed = 0
        for i, (src, dst) in enumerate(pairs):
            if progress_callback is not None:
                progress_callback(i, total, src.name)
            if is_file_active(src) or is_file_active(dst):
                logger.info("Migrate: skipping %s — file activity in flight", src.name)
                failed += 1
                continue
            was_owned = has_valid_ownership_marker(src) or is_momento_owned(src)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    logger.info("Migrate: %s already at destination, skipping", src.name)
                    failed += 1
                    continue
                shutil.move(str(src), str(dst))
            except OSError as e:
                logger.warning("Migrate: couldn't move %s: %s", src.name, e)
                failed += 1
                continue
            moved += 1
            for suffix in _SIDECAR_SUFFIXES:
                sidecar = src.parent / (src.name + suffix)
                if not sidecar.exists():
                    continue
                sidecar_dst = dst.parent / sidecar.name
                if sidecar_dst.exists():
                    logger.warning(
                        "Migrate: sidecar %s already at destination, skipping",
                        sidecar.name,
                    )
                    failed += 1
                    continue
                try:
                    shutil.move(str(sidecar), str(sidecar_dst))
                except OSError as e:
                    logger.warning("Migrate: sidecar %s: %s", sidecar.name, e)
                    # The media was moved successfully, but leaving an original
                    # sidecar behind is a partial migration and must be visible
                    # to the caller. The source sidecar remains recoverable.
                    failed += 1
            if was_owned and mark_recording_owned(dst):
                # A cross-volume move may alter timestamp precision. Refreshing
                # the destination marker keeps it bound to the moved file.
                source_marker = ownership_sidecar_path(src)
                try:
                    source_marker.unlink(missing_ok=True)
                except OSError:
                    pass
        if progress_callback is not None:
            progress_callback(total, total, "")
        return moved, failed

    def collect_media_pairs(self) -> list[tuple[Path, Path]]:
        pairs: list[tuple[Path, Path]] = []
        if not self._old.is_dir():
            return pairs
        try:
            old_resolved = self._old.resolve()
            new_resolved = self._new.resolve()
        except OSError:
            return pairs
        if old_resolved == new_resolved:
            return pairs
        try:
            top = list(self._old.iterdir())
        except OSError:
            return pairs
        for src in top:
            if (
                src.is_file()
                and src.suffix.lower() in _MEDIA_SUFFIXES
                and not is_repair_temp(src)
            ):
                pairs.append((src, self._new / src.name))
        old_clips = self._old / "clips"
        if old_clips.is_dir():
            new_clips = self._new / "clips"
            try:
                for src in old_clips.iterdir():
                    if (
                        src.is_file()
                        and src.suffix.lower() in _MEDIA_SUFFIXES
                        and not is_repair_temp(src)
                    ):
                        pairs.append((src, new_clips / src.name))
            except OSError:
                pass
        return pairs

def count_movable(folder: Path) -> tuple[int, int]:
    """Return ``(recordings, clips)`` — counts of media files in ``folder``
    (recordings) and ``folder/clips/`` (clips). Either count is 0 if the
    folder doesn't exist or contains nothing movable."""
    folder = Path(folder)
    if not folder.is_dir():
        return 0, 0
    return _count_media(folder), _count_media(folder / "clips")


def _count_media(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    try:
        return sum(
            1 for p in folder.iterdir()
            if p.is_file()
            and p.suffix.lower() in _MEDIA_SUFFIXES
            and not is_repair_temp(p)
        )
    except OSError:
        return 0
