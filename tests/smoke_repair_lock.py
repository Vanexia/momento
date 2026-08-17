"""Tests for the startup auto-repair file-lock fix (2026-06-18).

The bug: a crash-left-unfinalised MKV is ffmpeg-repaired to a
``*.repairing.mkv`` temp, but the swap onto the original fails with
``[WinError 32]`` because the temp (and the original) end in ``.mkv`` and so
get opened by the editor's metadata-probe / thumbnail jobs, which race the
repair's renames.

What is exercised here:

  1. EXCLUSION — every place that enumerates the recordings folder skips repair
     work files (``*.repairing.mkv`` / ``*.broken-bak.mkv``) via the single
     ``recording_files.is_repair_temp`` predicate: the editor listing, the
     crash-recovery scan, storage cleanup, and output-folder migration.
  2. ROBUST SWAP — the repair swaps with an atomic ``os.replace`` retried with
     backoff (``_replace_with_retry``) that rides out a transient read handle
     on the original but fails fast on permanent (non-lock) errors.
  3. IN-FLIGHT REGISTRY — ``repair_async`` refuses to queue a second repair for
     a file already being repaired, and ``is_repairing`` lets the editor skip
     probing/thumbnailing a file mid-repair.
  4. REFUSAL — ``RepairJob`` refuses to re-mux a temp (no temp-chaining).
  5. STALE SWEEP — ``cleanup_stale_repair_temps`` reaps orphaned temps, age-gated
     so it can never touch a live repair.

Run:
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_repair_lock.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Offscreen so importing the editor module (heavy Qt) needs no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv[:1])

import momento.core.media_probe as media_probe  # noqa: E402
import momento.ui.recordings_list as recordings_list_module  # noqa: E402
from momento.core.media_probe import (  # noqa: E402
    RepairJob,
    _replace_with_retry,
    cleanup_stale_repair_temps,
    find_broken_recordings,
    is_repairing,
    repair_async,
)
from momento.core.recording_files import (  # noqa: E402
    REPAIR_BACKUP_SUFFIX,
    REPAIR_TMP_SUFFIX,
    is_recording_file,
    is_repair_temp,
)
from momento.core.storage_cleanup import (  # noqa: E402
    MigrationWorker,
    _count_media,
    enforce_storage_limit,
)
from momento.core.recording_safety import mark_recording_owned  # noqa: E402
from momento.ui.editor import _list_recordings  # noqa: E402
from momento.ui.recordings_list import RecordingsList  # noqa: E402
from media_fixture import make_momento_mkv  # noqa: E402

_results: list[tuple[str, bool]] = []
_CREATION = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}")


def _touch(path: Path, size: int = 2048, *, age_seconds: float = 0.0) -> Path:
    """Create a file of ``size`` bytes; optionally backdate its mtime."""
    path.write_bytes(b"\0" * size)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _make_broken_mkv(path: Path) -> bool:
    """Write a genuinely truncated MKV with no readable Segment duration."""
    try:
        make_momento_mkv(path, live=True)
    except OSError:
        return False
    return path.stat().st_size >= 4096


# ---------------------------------------------------------------- classifier
def test_is_repair_temp_classifies_correctly() -> None:
    rec = "Wow_2026-06-18_190515.mkv"
    tmp = "Wow_2026-06-18_190515.repairing.mkv"
    bak = "Wow_2026-06-18_190515.broken-bak.mkv"

    check("classifier: real recording is NOT a temp", not is_repair_temp(rec))
    check("classifier: .repairing.mkv IS a temp", is_repair_temp(tmp))
    check("classifier: .broken-bak.mkv IS a temp", is_repair_temp(bak))
    check("classifier: case-insensitive", is_repair_temp(tmp.upper()))
    check("classifier: clip .mp4 is NOT a temp", not is_repair_temp("clip.mp4"))
    check("classifier: REPAIR_TMP_SUFFIX is a temp", is_repair_temp(f"x{REPAIR_TMP_SUFFIX}"))
    check("classifier: REPAIR_BACKUP_SUFFIX is a temp", is_repair_temp(f"x{REPAIR_BACKUP_SUFFIX}"))
    check("classifier: recording is a listable file", is_recording_file(rec))
    check("classifier: temp is NOT a listable file", not is_recording_file(tmp))


# --------------------------------------------------- editor listing (primary)
def test_list_recordings_excludes_temps(tmp: Path) -> None:
    folder = tmp / "lib"
    folder.mkdir()
    rec = _touch(folder / "Wow_2026-06-18_190515.mkv")
    mp4 = _touch(folder / "Wow_2026-06-18_190515.mp4")
    repairing = _touch(folder / "Wow_2026-06-18_190515.repairing.mkv")
    bak = _touch(folder / "Wow_2026-06-18_190515.broken-bak.mkv")

    listed = set(_list_recordings(folder))

    check("editor-list: real .mkv recording is listed", rec in listed)
    check("editor-list: real .mp4 recording is listed", mp4 in listed)
    check("editor-list: .repairing.mkv temp is EXCLUDED", repairing not in listed)
    check("editor-list: .broken-bak.mkv temp is EXCLUDED", bak not in listed)
    check("editor-list: exactly the two real files listed", len(listed) == 2)


# ----------------------------------------------------------- robust swap
def test_replace_with_retry_rides_out_a_lock(tmp: Path) -> None:
    """The swap must survive a transient read handle on the destination.

    On Windows, Python's ``open(dst, 'rb')`` denies delete-sharing, so an
    immediate ``os.replace`` over it fails — exactly the lock that broke
    auto-repair. ``_replace_with_retry`` must block until the handle releases
    and then complete.
    """
    # Happy path: no lock -> swaps immediately on every platform.
    dst = tmp / "swap_a.mkv"
    dst.write_bytes(b"OLD")
    src = tmp / "swap_a.repairing.mkv"
    src.write_bytes(b"NEW")
    err = _replace_with_retry(src, dst)
    check("swap: unobstructed replace succeeds", err is None)
    check("swap: destination now holds the new bytes", dst.read_bytes() == b"NEW")
    check("swap: temp consumed by the replace", not src.exists())

    if sys.platform != "win32":
        return  # the lock semantics below are Windows-specific

    # Precondition: an open read handle really does block an immediate replace.
    dst2 = tmp / "swap_b.mkv"
    dst2.write_bytes(b"OLD")
    tmp2 = tmp / "swap_b.repairing.mkv"
    tmp2.write_bytes(b"NEW")
    blocker = open(dst2, "rb")  # noqa: SIM115 — held deliberately
    blocked = False
    try:
        os.replace(tmp2, dst2)
    except OSError:
        blocked = True
    finally:
        blocker.close()
    check("swap: open handle blocks an immediate replace (precondition)", blocked)

    # The real thing: hold the handle, release it after 0.4 s, and confirm the
    # retried swap waits it out and completes well inside the retry budget.
    dst3 = tmp / "swap_c.mkv"
    dst3.write_bytes(b"OLD")
    tmp3 = tmp / "swap_c.repairing.mkv"
    tmp3.write_bytes(b"NEW")
    held = open(dst3, "rb")  # noqa: SIM115

    def _release_soon() -> None:
        time.sleep(0.4)
        held.close()

    threading.Thread(target=_release_soon, daemon=True).start()
    t0 = time.monotonic()
    err3 = _replace_with_retry(tmp3, dst3)
    elapsed = time.monotonic() - t0
    if not held.closed:
        held.close()

    check("swap: retried replace eventually succeeds despite lock", err3 is None)
    check("swap: swapped to new bytes after lock released", dst3.read_bytes() == b"NEW")
    check("swap: it actually waited for the release (>=0.3 s)", elapsed >= 0.3)
    check("swap: finished within the retry budget (<11 s)", elapsed < 11.0)


def test_replace_with_retry_fails_fast_on_permanent_error(tmp: Path) -> None:
    """A missing source (or any non-lock error) can never clear by waiting, so
    the helper must return at once instead of sleeping out the whole budget."""
    dst = tmp / "keep.mkv"
    dst.write_bytes(b"OLD")
    missing = tmp / "never_created.repairing.mkv"

    t0 = time.monotonic()
    err = _replace_with_retry(missing, dst)
    elapsed = time.monotonic() - t0

    check("failfast: returns an OSError for a missing temp", isinstance(err, OSError))
    check("failfast: did NOT burn the retry budget (<0.5 s)", elapsed < 0.5)
    check("failfast: destination left untouched", dst.read_bytes() == b"OLD")


# ------------------------------------------------ stale orphan temp sweep
def test_cleanup_stale_repair_temps_age_gates(tmp: Path) -> None:
    folder = tmp / "sweep"
    folder.mkdir()
    rec = _touch(folder / "game.mkv", age_seconds=600)            # real, old
    old_rec = _touch(folder / "old.mkv", age_seconds=600)
    check("sweep fixture: game recording is owned", mark_recording_owned(rec))
    check("sweep fixture: old recording is owned", mark_recording_owned(old_rec))
    fresh_temp = _touch(folder / "game.repairing.mkv")            # temp, brand new
    stale_temp = _touch(folder / "old.repairing.mkv", age_seconds=600)  # temp, old
    stale_bak = _touch(folder / "old.broken-bak.mkv", age_seconds=600)  # legacy, old

    unrelated = _touch(folder / "unrelated.repairing.mkv", age_seconds=600)
    removed = cleanup_stale_repair_temps(folder, min_age_seconds=120.0)

    check("sweep: removed both stale owned temps", removed == 2)
    check("sweep: real recording untouched", rec.exists())
    check("sweep: a fresh temp (live repair) is kept", fresh_temp.exists())
    check("sweep: stale .repairing.mkv removed", not stale_temp.exists())
    check("sweep: stale .broken-bak.mkv removed", not stale_bak.exists())
    check("sweep: unrelated stale temp is preserved", unrelated.exists())


# -------------------------------------------------- crash-recovery scan
def test_find_broken_excludes_temps(tmp: Path) -> None:
    folder = tmp / "recover"
    folder.mkdir()
    # Garbage temps are always excluded (before the probe even runs).
    _touch(folder / "x.repairing.mkv", size=2048, age_seconds=600)
    _touch(folder / "x.broken-bak.mkv", size=2048, age_seconds=600)
    broken = find_broken_recordings(folder, min_age_seconds=0.0, min_size_bytes=1024)
    check("recover: garbage temps are never returned", broken == [])

    # Strong, ffmpeg-gated proof: a genuinely broken MKV under a normal name IS
    # flagged, but the SAME broken bytes under a temp name are EXCLUDED — so the
    # test fails if the is_repair_temp guard is removed from the scan.
    real = folder / "broken_2026-01-01_000000.mkv"
    generated = _make_broken_mkv(real)
    check("recover fixture: broken tagged MKV was generated", generated)
    if not generated:
        return
    os.utime(real, (0, 0))
    owned = mark_recording_owned(real)
    check("recover fixture: broken recording has a valid marker", owned)
    if not owned:
        return
    flagged = find_broken_recordings(folder, min_age_seconds=0.0, min_size_bytes=1024)
    check("recover: a real owned broken recording IS flagged", real in flagged)
    if real not in flagged:
        return
    temp = folder / "broken_2026-01-01_000000.repairing.mkv"
    temp.write_bytes(real.read_bytes())
    os.utime(temp, (0, 0))
    flagged2 = find_broken_recordings(folder, min_age_seconds=0.0, min_size_bytes=1024)
    check("recover: a real broken recording remains flagged (control)", real in flagged2)
    check("recover: same broken bytes under a temp name are EXCLUDED", temp not in flagged2)

    unrelated = folder / "family-holiday.mkv"
    unrelated.write_bytes(real.read_bytes())
    os.utime(unrelated, (0, 0))
    flagged3 = find_broken_recordings(folder, min_age_seconds=0.0, min_size_bytes=1024)
    check("recover: an unowned broken MKV is never auto-repaired", unrelated not in flagged3)


# ------------------------------------------------------- storage cleanup
def test_storage_cleanup_ignores_temps(tmp: Path) -> None:
    folder = tmp / "store"
    folder.mkdir()
    _touch(folder / "real.mkv", size=4 * 1024 * 1024, age_seconds=600)
    _touch(folder / "real.repairing.mkv", size=4 * 1024 * 1024, age_seconds=300)

    # _count_media feeds the same is_repair_temp-filtered candidate list that
    # the eviction loop uses, so this assertion is load-bearing: it returns 2
    # with the exclusion and 3 without it.
    check("storage: _count_media counts only the real recording", _count_media(folder) == 1)
    # Under-cap: nothing is evicted; importantly the temp size is not counted
    # toward the budget and the temp is never a deletion candidate.
    deleted = enforce_storage_limit(folder, max_gb=1)
    check("storage: nothing deleted under the cap", deleted == 0)


# ----------------------------------------------- RepairJob temp refusal
def test_repairjob_refuses_temp(tmp: Path) -> None:
    """RepairJob must refuse to re-mux a temp (would chain X.repairing.repairing
    .mkv and never converge) and must do so BEFORE invoking ffmpeg."""
    temp = _touch(tmp / "x.repairing.mkv", size=8192)
    done: list[tuple[bool, str]] = []
    job = RepairJob(temp)
    # Same-thread run() -> direct connection -> callback fires synchronously.
    job.signals.done.connect(lambda p, ok, err: done.append((ok, err)))
    job.run()

    check("refuse-temp: emitted exactly one result", len(done) == 1)
    ok, err = done[0] if done else (None, "")
    check("refuse-temp: refused (ok is False)", ok is False)
    check("refuse-temp: message explains the refusal", "temp" in err.lower())
    check("refuse-temp: temp left on disk untouched", temp.exists())


def test_repair_rejects_mp4_before_ffmpeg(tmp: Path) -> None:
    """MP4 clips must never be re-muxed to Matroska under an .mp4 name."""
    clip = _touch(tmp / "already-exported.mp4", size=8192)
    original = clip.read_bytes()
    done: list[tuple[bool, str]] = []
    run_calls: list[list[str]] = []
    real_run = media_probe.subprocess.run

    def fake_run(args, **_kwargs):
        run_calls.append(list(args))
        raise AssertionError("ffmpeg must not run for MP4 repair")

    media_probe.subprocess.run = fake_run
    try:
        job = RepairJob(clip)
        job.signals.done.connect(lambda _p, ok, err: done.append((ok, err)))
        job.run()
    finally:
        media_probe.subprocess.run = real_run

    check("refuse-mp4: ffmpeg was not invoked", not run_calls)
    check("refuse-mp4: emitted exactly one result", len(done) == 1)
    check(
        "refuse-mp4: result explains MKV-only repair",
        bool(done) and done[0][0] is False and "mkv" in done[0][1].lower(),
    )
    check("refuse-mp4: original clip is untouched", clip.read_bytes() == original)


def test_repair_async_rejects_mp4_before_queue(tmp: Path) -> None:
    clip = _touch(tmp / "queued-clip.mp4", size=8192)
    callbacks: list[tuple[str, bool, str]] = []
    started: list[RepairJob] = []
    real_pool = media_probe._POOL

    class FakePool:
        def start(self, job: RepairJob) -> None:
            started.append(job)

    media_probe._POOL = FakePool()
    try:
        job = repair_async(clip, lambda *args: callbacks.append(args))
    finally:
        media_probe._POOL = real_pool
        with media_probe._repair_lock:
            media_probe._in_flight_repairs.discard(str(clip.resolve()))

    check("async-refuse-mp4: returns None", job is None)
    check("async-refuse-mp4: no worker was queued", not started)
    check("async-refuse-mp4: path was never registered", not is_repairing(clip))
    check("async-refuse-mp4: no asynchronous callback was promised", not callbacks)


def test_recordings_menu_hides_repair_for_mp4(tmp: Path) -> None:
    """The context menu offers Repair for MKV recordings, never MP4 clips."""
    shown_actions: list[str] = []
    real_menu = recordings_list_module.QMenu

    class FakeMenu:
        def __init__(self, _parent=None) -> None:
            pass

        def addAction(self, text: str):
            shown_actions.append(text)
            return object()

        def addSeparator(self) -> None:
            pass

        def exec(self, _pos):
            return None

    def menu_actions(path: Path) -> list[str]:
        shown_actions.clear()
        view = RecordingsList()
        view.resize(600, 300)
        view.add_item(path, path.stat().st_mtime, path.stat().st_size)
        view.show()
        _app.processEvents()
        view.select_first()
        _app.processEvents()
        pos = view.visualRect(view.currentIndex()).center()
        view._on_context_menu(pos)
        view.close()
        return list(shown_actions)

    mkv = _touch(tmp / "recording.mkv", size=8192)
    mp4 = _touch(tmp / "clip.mp4", size=8192)
    recordings_list_module.QMenu = FakeMenu
    try:
        mkv_actions = menu_actions(mkv)
        mp4_actions = menu_actions(mp4)
    finally:
        recordings_list_module.QMenu = real_menu

    check("menu: MKV offers Repair recording", "Repair recording…" in mkv_actions)
    check("menu: MP4 does not offer Repair recording", "Repair recording…" not in mp4_actions)


def test_repairjob_unexpected_error_still_completes(tmp: Path) -> None:
    """Unexpected worker errors must emit one terminal result so the in-flight
    registry and any progress UI cannot remain stuck forever."""
    src = _touch(tmp / "unexpected.mkv", size=8192)
    done: list[tuple[bool, str]] = []
    real_run = media_probe.subprocess.run
    real_replace = media_probe._replace_with_retry
    real_validate = media_probe.validate_repair_candidate

    def fake_run(args, **_kwargs):
        Path(args[-1]).write_bytes(b"x" * 8192)
        return SimpleNamespace(returncode=0, stderr="")

    media_probe.subprocess.run = fake_run
    media_probe.validate_repair_candidate = lambda *_args, **_kwargs: None
    media_probe._replace_with_retry = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("simulated unexpected swap crash")
    )
    try:
        job = RepairJob(src)
        job.signals.done.connect(lambda _p, ok, err: done.append((ok, err)))
        raised = False
        try:
            job.run()
        except Exception:
            raised = True
        check("unexpected repair error does not escape the worker", not raised)
        check("unexpected repair error emits exactly one result", len(done) == 1)
        check("unexpected repair result is a useful failure", bool(done) and done[0][0] is False and "unexpected" in done[0][1].lower())
    finally:
        media_probe.subprocess.run = real_run
        media_probe._replace_with_retry = real_replace
        media_probe.validate_repair_candidate = real_validate


def test_repair_rejects_invalid_exit_zero_output(tmp: Path) -> None:
    src = _touch(tmp / "invalid-success.mkv", size=8192)
    original = src.read_bytes()
    done: list[tuple[bool, str]] = []
    replaced: list[bool] = []
    real_run = media_probe.subprocess.run
    real_replace = media_probe._replace_with_retry
    real_validate = media_probe.validate_repair_candidate

    def fake_run(args, **_kwargs):
        Path(args[-1]).write_bytes(b"not-media" * 1024)
        return SimpleNamespace(returncode=0, stderr="")

    media_probe.subprocess.run = fake_run
    media_probe.validate_repair_candidate = (
        lambda *_args, **_kwargs: "candidate is not readable media"
    )
    media_probe._replace_with_retry = lambda *_args: (replaced.append(True) or None)
    try:
        job = RepairJob(src)
        job.signals.done.connect(lambda _p, ok, err: done.append((ok, err)))
        job.run()
    finally:
        media_probe.subprocess.run = real_run
        media_probe._replace_with_retry = real_replace
        media_probe.validate_repair_candidate = real_validate

    check("repair validation: corrupt exit-zero output is rejected", bool(done) and not done[0][0])
    check("repair validation: original bytes remain unchanged", src.read_bytes() == original)
    check("repair validation: invalid candidate never reaches atomic replace", not replaced)
    check(
        "repair validation: rejected temp is cleaned",
        not src.with_name(src.stem + REPAIR_TMP_SUFFIX).exists(),
    )


def test_folder_migration_skips_inflight_repair(tmp: Path) -> None:
    old = tmp / "old"
    new = tmp / "new"
    old.mkdir()
    src = _touch(old / "repairing-now.mkv", size=8192)
    real_is_repairing = media_probe.is_repairing
    media_probe.is_repairing = lambda path: Path(path).resolve() == src.resolve()
    try:
        moved, failed = MigrationWorker(old, new).run()
    finally:
        media_probe.is_repairing = real_is_repairing
    check("migration skips a recording with repair in flight", moved == 0 and failed == 1)
    check("migration leaves repairing source and destination untouched", src.exists() and not (new / src.name).exists())


# ----------------------------------------------- in-flight dedup registry
def test_repair_dedup_blocks_double_queue(tmp: Path) -> None:
    """Two repairs of the same file would clobber the one shared temp, so the
    second queue must be refused while the first is in flight."""
    ghost = tmp / "ghost_2026-01-01_000000.mkv"  # does not exist -> run() fails fast
    done: list[tuple[bool, str]] = []
    job1 = repair_async(ghost, lambda p, ok, err: done.append((ok, err)))
    check("dedup: first repair is queued", job1 is not None)
    check("dedup: path registered as in-flight", is_repairing(ghost))

    job2 = repair_async(ghost, lambda p, ok, err: done.append((ok, err)))
    check("dedup: duplicate queue is refused (returns None)", job2 is None)

    # Let the worker finish and the queued 'done' deliver -> deregister.
    # Repairs run on media_probe's own bounded pool (split from the thumbnail
    # pool + globalInstance so a repair can't queue behind thumbnail jobs).
    from momento.core.media_probe import _POOL as _probe_pool

    _probe_pool.waitForDone(3000)
    _app.processEvents()
    check("dedup: deregistered after completion", not is_repairing(ghost))
    check("dedup: exactly one callback fired (the duplicate did not)", len(done) == 1)
    # Keep job1 referenced until here so its signals object outlives delivery.
    del job1


def main() -> int:
    test_is_repair_temp_classifies_correctly()
    with tempfile.TemporaryDirectory(prefix="momento_repairlock_") as d:
        tmp = Path(d)
        for fn in (
            test_list_recordings_excludes_temps,
            test_replace_with_retry_rides_out_a_lock,
            test_replace_with_retry_fails_fast_on_permanent_error,
            test_cleanup_stale_repair_temps_age_gates,
            test_find_broken_excludes_temps,
            test_storage_cleanup_ignores_temps,
            test_repairjob_refuses_temp,
            test_repair_rejects_mp4_before_ffmpeg,
            test_repair_async_rejects_mp4_before_queue,
            test_recordings_menu_hides_repair_for_mp4,
            test_repairjob_unexpected_error_still_completes,
            test_repair_rejects_invalid_exit_zero_output,
            test_folder_migration_skips_inflight_repair,
            test_repair_dedup_blocks_double_queue,
        ):
            sub = tmp / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except Exception as e:  # a test that errors is itself a failure
                check(f"{fn.__name__} raised unexpectedly: {e!r}", False)

    passed = sum(1 for _, ok in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
