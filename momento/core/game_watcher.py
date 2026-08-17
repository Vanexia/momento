"""Poll for known-game processes and fire start/stop callbacks.

Only one active game at a time: once a known game is detected the watcher
ignores any others until the active game's process exits. This matches the
recorder's single-session-at-a-time model.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import psutil

from momento.util.resources import known_games_path

logger = logging.getLogger(__name__)

GameStartCallback = Callable[["ActiveGame"], None]
GameStopCallback = Callable[["ActiveGame"], None]

DEFAULT_POLL_INTERVAL = 2.0


@dataclass(frozen=True)
class ActiveGame:
    exe_name: str  # e.g. "eldenring.exe" — case as reported by psutil
    pid: int
    exe_path: str | None  # absolute path if psutil could resolve it
    # Process creation timestamp — (pid, create_time) is the real process
    # identity. Windows reuses PIDs aggressively, so a bare PID check can
    # match an unrelated process that inherited the number; every liveness /
    # same-process comparison must include this. None if unreadable.
    create_time: float | None = None


class GameWatcher:
    """Polls psutil for known game processes; emits start/stop events.

    Callbacks are invoked from the watcher thread. Keep them quick or hand work
    off to another thread / queue.
    """

    def __init__(
        self,
        known_games: Iterable[str] | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_game_start: GameStartCallback | None = None,
        on_game_stop: GameStopCallback | None = None,
        record_any_fullscreen: bool = False,
    ) -> None:
        initial_games = _load_known_games() if known_games is None else known_games
        self._known: set[str] = {g.lower() for g in initial_games}
        self._poll_interval = poll_interval
        self._record_any_fullscreen = bool(record_any_fullscreen)
        self.on_game_start = on_game_start
        self.on_game_stop = on_game_stop

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active: ActiveGame | None = None
        self._lock = threading.Lock()
        # pid -> (cooldown_until_monotonic, create_time). create_time is carried
        # so a reused PID belonging to a DIFFERENT process (Windows recycles PIDs
        # fast) isn't suppressed — only the same failed process is cooled down.
        self._retry_not_before: dict[int, tuple[float, float | None]] = {}
        # Sustained-fullscreen filter: remember the candidate seen on the
        # PREVIOUS tick and only fire if the same process is still foreground +
        # fullscreen on this tick. Filters out brief fullscreens (popups,
        # alt-tab overlays, transient maximisations) at the cost of one
        # poll-interval (~2s) of detection latency. Identity is
        # (pid, create_time), not bare pid — PID reuse between ticks must not
        # count as "sustained".
        self._pending_fullscreen_pid: tuple[int, float | None] | None = None
        # Exes the user toggled Auto-record OFF in the Games tab. The
        # fullscreen fallback must honour that choice too — without this,
        # disabling a game only removed it from the known set, and the
        # fallback would happily record it anyway.
        self._fullscreen_skip: set[str] = set()

    # ------------------------------------------------------------------ API
    @property
    def active(self) -> ActiveGame | None:
        with self._lock:
            return self._active

    def update_known_games(self, exes: Iterable[str]) -> None:
        """Replace the watch list (called after Settings save)."""
        with self._lock:
            self._known = {g.lower() for g in exes}

    def set_record_any_fullscreen(self, enabled: bool) -> None:
        with self._lock:
            self._record_any_fullscreen = bool(enabled)

    def update_fullscreen_skip(self, exes: Iterable[str]) -> None:
        """Exes the fullscreen fallback must never trigger on (the user's
        Auto-record-Off list). Called at construction time and after Settings
        save."""
        with self._lock:
            self._fullscreen_skip = {g.lower() for g in exes}

    def release_active_for_retry(
        self, game: ActiveGame, retry_after_s: float = 15.0
    ) -> None:
        """Let a still-running active game be detected again after a cooldown.

        Used when the session saw a retryable start failure after process
        detection. Without this, the watcher pins the active PID until process
        exit and a transient WGC/window failure loses the whole session.
        """
        with self._lock:
            active = self._active
            if active is None or active.pid != game.pid:
                return
            # A retry request can arrive after the original process exited. Do
            # not release a new process that inherited the same Windows PID.
            if (
                active.create_time is not None
                and game.create_time is not None
                and abs(active.create_time - game.create_time) > 0.01
            ):
                logger.info(
                    "Ignoring stale retry release for reused pid=%d (%s -> %s)",
                    game.pid,
                    game.create_time,
                    active.create_time,
                )
                return
            self._active = None
            self._pending_fullscreen_pid = None
            self._retry_not_before[game.pid] = (
                time.monotonic() + max(0.0, float(retry_after_s)),
                game.create_time,
            )
        logger.info(
            "Released active game for retry after %.0fs: %s (pid=%d)",
            retry_after_s, game.exe_name, game.pid,
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="GameWatcher", daemon=True
        )
        self._thread.start()
        logger.info("GameWatcher started (poll %.1fs, %d known)", self._poll_interval, len(self._known))

    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive() and not self._stop_event.is_set()

    def stop(self) -> None:
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self._poll_interval + 1.0)
            if t.is_alive():
                logger.warning(
                    "GameWatcher stop timed out; retaining the existing thread "
                    "until its current callback returns"
                )
                return
        self._thread = None
        logger.info("GameWatcher stopped")

    # ----------------------------------------------------------------- impl
    def _run(self) -> None:
        # Run an initial poll immediately, then on the interval, so users don't
        # wait a full cycle for the first detection.
        while True:
            try:
                self._tick()
            except Exception:
                logger.exception("GameWatcher tick raised")
            if self._stop_event.wait(self._poll_interval):
                return

    def _tick(self) -> None:
        # Snapshot config under the lock so update_* methods are safe.
        with self._lock:
            active = self._active
            known = self._known
            fullscreen_mode = self._record_any_fullscreen
            fullscreen_skip = self._fullscreen_skip
            now = time.monotonic()
            expired = [
                pid for pid, (until, _ct) in self._retry_not_before.items()
                if until <= now
            ]
            for pid in expired:
                self._retry_not_before.pop(pid, None)
            # pid -> create_time of the process that's cooling down, so the
            # finders can tell a reused PID (new process) from the same one.
            retry_blocked = {
                pid: ct for pid, (_until, ct) in self._retry_not_before.items()
            }

        # If a game is currently being tracked, first check whether it's still alive.
        if active is not None and not _game_still_running(active):
            logger.info("Active game exited: %s (pid=%d)", active.exe_name, active.pid)
            with self._lock:
                if self._active is not None and self._active.pid == active.pid:
                    self._active = None
                self._retry_not_before.pop(active.pid, None)
            cb = self.on_game_stop
            if cb:
                cb(active)
            active = None

        # If still tracking something, ignore any other triggers.
        if active is not None:
            return

        # Primary trigger: scan for the first running known game. Known-games
        # match is trusted immediately — the user explicitly listed it.
        found = _find_first_known(known, skip=retry_blocked)

        if found is not None:
            self._pending_fullscreen_pid = None
        elif fullscreen_mode:
            # Fallback trigger (opt-in). Requires the SAME fullscreen pid to
            # be seen on two consecutive ticks before firing — kills false
            # positives from briefly-fullscreen things (popups, screenshot
            # tools, alt-tab dialogs, exclusive-mode handoffs).
            candidate = _find_foreground_fullscreen(
                skip=retry_blocked, skip_names=fullscreen_skip
            )
            candidate_key = (
                (candidate.pid, candidate.create_time) if candidate is not None else None
            )
            if candidate is None:
                self._pending_fullscreen_pid = None
            elif self._pending_fullscreen_pid == candidate_key:
                found = candidate
                self._pending_fullscreen_pid = None
            else:
                # First sighting — remember it; need to see the SAME process
                # ((pid, create_time), not a bare reused pid) next tick.
                self._pending_fullscreen_pid = candidate_key
                logger.debug(
                    "Fullscreen candidate pending confirmation: %s (pid=%d)",
                    candidate.exe_name, candidate.pid,
                )
        else:
            self._pending_fullscreen_pid = None

        if found is None:
            return

        with self._lock:
            self._active = found
        logger.info("Game start detected: %s (pid=%d)", found.exe_name, found.pid)
        cb = self.on_game_start
        if cb:
            cb(found)


# ---------------------------------------------------------------- helpers
def _game_still_running(game: ActiveGame) -> bool:
    """True while the SAME process that was detected is still alive.

    A bare pid-exists check is wrong: constructing a fresh psutil.Process
    each tick defeats psutil's own (pid, create_time) reuse protection, so a
    long-lived process that inherited the game's recycled PID would pin the
    watcher's active slot forever (no stop event, no further detections).
    Verify identity via create_time when we have it.
    """
    try:
        p = psutil.Process(game.pid)
        if game.create_time is not None:
            # Same pid + same start timestamp == same process. Tolerance for
            # float round-tripping through psutil.
            if abs(p.create_time() - game.create_time) > 0.01:
                return False
        # Zombie/stopped should count as "not running" for our purposes.
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        # Elevated / anti-cheat-protected game (e.g. FFXIV) that Momento can't
        # query via psutil while running non-elevated. Do NOT treat
        # "can't determine" as "exited" — that made the watcher fire game-stop
        # then re-detect the still-running game by name every ~2s: a
        # stop/start/toast/re-record storm (a tracked known-rough-edge). Fall
        # back to a native OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) +
        # GetExitCodeProcess check, which works across integrity levels; only
        # report dead when the OS confirms the PID is gone.
        alive = _native_pid_running(game.pid)
        if alive is None:
            return True  # inconclusive — assume alive rather than re-fire
        return alive


def _native_pid_running(pid: int) -> bool | None:
    """OS-level liveness check for a process psutil can't query.

    Returns True (confirmed running), False (confirmed gone/exited), or None
    (couldn't determine). Uses PROCESS_QUERY_LIMITED_INFORMATION, which a
    non-elevated caller is granted even against an elevated/protected target.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No such pid → the process is genuinely gone. Access-denied / other
            # → can't tell (shouldn't happen with LIMITED_INFORMATION).
            return False if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — never let a liveness probe crash the tick
        logger.debug("Native liveness probe failed for pid=%d", pid, exc_info=True)
        return None


def _safe_create_time(proc: psutil.Process) -> float | None:
    try:
        return float(proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None


def _find_first_known(
    known_lower: set[str], skip: dict[int, float | None] | None = None
) -> ActiveGame | None:
    skip = skip or {}
    # ``exe`` is intentionally omitted from the broad iteration: psutil
    # opens each process with PROCESS_QUERY_LIMITED_INFORMATION to read
    # the image path, which dominates the per-poll cost. Resolve the exe
    # lazily for the single matched process instead.
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = proc.info.get("name")
            if not name:
                continue
            if name.lower() in known_lower:
                pid = int(proc.info["pid"])
                created = _safe_create_time(proc)
                if _is_cooldown_blocked(pid, created, skip):
                    continue
                try:
                    exe_path = proc.exe()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    exe_path = None
                return ActiveGame(
                    exe_name=name, pid=pid, exe_path=exe_path,
                    create_time=created,
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _is_cooldown_blocked(
    pid: int, created: float | None, skip: dict[int, float | None]
) -> bool:
    """True if ``pid`` is in the retry-cooldown set AND is the SAME process that
    failed (matched by create_time). A reused PID whose create_time differs is a
    new process and must not be suppressed. When either create_time is unknown we
    can't prove it's a different process, so we keep cooling down (safe default).
    """
    if pid not in skip:
        return False
    blocked_ct = skip[pid]
    if blocked_ct is None or created is None:
        return True
    return abs(created - blocked_ct) <= 0.01


# Names that should never trigger the fullscreen fallback. Comprehensive
# block-list of well-known non-games that legitimately go fullscreen but
# absolutely should not start a gameplay recording. Lowercase, exe basename.
#
# The fallback path itself is opt-in (Settings → "Record any fullscreen"),
# but even when enabled the user almost certainly doesn't want recordings
# of their Parsec session / OBS preview / browser video / VS Code window.
# Coverage here is generous because false-positives produce silent
# behind-the-scenes recordings the user only finds when their disk fills.
_FULLSCREEN_SKIP_NAMES = frozenset({
    # Shell + ourselves
    "explorer.exe", "shellexperiencehost.exe", "searchapp.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "lockapp.exe",
    "applicationframehost.exe",
    "python.exe", "pythonw.exe", "momento.exe",
    # Remote desktop / game streaming hosts (not the games they stream)
    "parsec.exe", "parsecd.exe",
    "teamviewer.exe", "teamviewer_service.exe", "tv_w32.exe", "tv_x64.exe",
    "anydesk.exe",
    "moonlight.exe",
    "sunshine.exe",
    "rustdesk.exe",
    "vncviewer.exe", "tvnviewer.exe", "tightvnc.exe", "winvnc.exe",
    "mstsc.exe",  # Windows Remote Desktop client
    "chrome_remote_desktop_host.exe",
    # Streamers / recorders / overlays (often running alongside games)
    "obs64.exe", "obs32.exe", "obs.exe",
    "streamlabs obs.exe", "slobs.exe", "streamlabs.exe",
    "xsplit.exe", "xsplit.broadcaster.exe", "xsplit.gamecaster.exe",
    "bandicam.exe", "bdcam.exe",
    "action.exe",  # Mirillis Action!
    "fraps.exe",
    "nvidia share.exe", "nvidia overlay.exe", "nvcontainer.exe",
    "shadowplay.exe",
    "outplayed.exe", "outplayed.tray.exe",
    "medal.exe",
    # Media players (windowed → fullscreen on play)
    "vlc.exe",
    "mpv.exe", "mpv-uosc.exe",
    "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe",
    "potplayermini.exe", "potplayermini64.exe",
    "wmplayer.exe",
    "video.uwp.exe", "movies & tv.exe",
    "iina.exe",
    # Streaming / media-centre apps (fullscreen playback — recorded a
    # stremio-shell-ng.exe fullscreen session on 2026-07-03)
    "stremio.exe", "stremio-shell-ng.exe", "stremio-shell.exe",
    "stremio-runtime.exe",
    "kodi.exe",
    "plex.exe", "plexmediaplayer.exe", "plex media player.exe", "plexhtpc.exe",
    "jellyfinmediaplayer.exe", "jellyfin media player.exe",
    "embytheater.exe", "emby theater.exe",
    "popcorntime.exe", "popcorn-time.exe",
    "netflix.exe",
    # Browsers — Netflix/YouTube/Twitch full-screen is a popular false-positive
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "librewolf.exe", "waterfox.exe", "arc.exe",
    "iexplore.exe", "tor.exe", "torbrowser.exe",
    "thorium.exe", "ungoogled-chromium.exe",
    # IDEs / editors (often used at fullscreen on big monitors)
    "code.exe", "code - insiders.exe",  # VS Code
    "devenv.exe",  # Visual Studio
    "rider64.exe", "rider.exe",
    "idea64.exe", "idea.exe",
    "pycharm64.exe", "pycharm.exe",
    "webstorm64.exe", "webstorm.exe",
    "clion64.exe", "clion.exe",
    "phpstorm64.exe", "phpstorm.exe",
    "goland64.exe", "goland.exe",
    "rubymine64.exe", "rubymine.exe",
    "android studio64.exe",
    "sublime_text.exe",
    "notepad++.exe", "notepad.exe",
    "atom.exe",
    "neovide.exe", "nvim-qt.exe",
    "cursor.exe",
    # Office / productivity
    "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
    "onenote.exe", "msaccess.exe", "mspub.exe", "visio.exe", "lync.exe",
    "soffice.exe", "soffice.bin",  # LibreOffice
    "acrobat.exe", "acrord32.exe",  # Adobe Acrobat / Reader
    # AI assistants / agent desktop apps (agentic screen control can leave
    # them foreground-fullscreen for hours — a 12h black-screen recording of
    # claude.exe is how this category earned its place)
    "claude.exe", "chatgpt.exe", "copilot.exe", "perplexity.exe",
    "lmstudio.exe", "ollama app.exe",
    # Chat / video calls (full-screen during screen-share is the killer)
    "discord.exe", "discordcanary.exe", "discordptb.exe",
    "slack.exe",
    "teams.exe", "ms-teams.exe",
    "zoom.exe", "zoomit.exe",
    "skype.exe",
    "telegram.exe", "telegramdesktop.exe",
    "signal.exe",
    "whatsapp.exe",
    # Creative tools
    "photoshop.exe", "illustrator.exe", "premiere pro.exe", "afterfx.exe",
    "blender.exe",
    "gimp-2.10.exe", "gimp.exe",
    "krita.exe",
    "obs studio.exe",  # alternate name
    "davinci resolve.exe", "resolve.exe",
    # System utilities
    "mmc.exe", "taskmgr.exe", "regedit.exe", "perfmon.exe",
    "cmd.exe", "powershell.exe", "wt.exe", "pwsh.exe", "conhost.exe",
    # Misc launchers that can go foreground-fullscreen
    "epicgameslauncher.exe", "steam.exe", "eadesktop.exe", "ealauncher.exe",
    "gog galaxy.exe", "ubisoftconnect.exe",
    "battle.net.exe", "battle.net launcher.exe",
    "rockstargameslauncher.exe", "playgameslauncher.exe",
})


def _find_foreground_fullscreen(
    skip: dict[int, float | None] | None = None,
    skip_names: set[str] | None = None,
) -> ActiveGame | None:
    """Fallback trigger: any foreground window covering an entire monitor.

    ``skip_names`` carries the user's Auto-record-Off list (lowercase) — a
    game the user explicitly disabled must not sneak back in through the
    fullscreen fallback.
    """
    from momento.util.windows_api import foreground_fullscreen_pid

    skip = skip or {}
    pid = foreground_fullscreen_pid()
    if pid is None:
        return None
    # Skip ourselves up front — covers both dev (python.exe) and the frozen
    # build, even if some external process spoofs the exe name.
    if pid == os.getpid():
        return None
    try:
        proc = psutil.Process(pid)
        name = proc.name() or "fullscreen.exe"
        created = _safe_create_time(proc)
        # The exe path is optional (ActiveGame.exe_path is str | None) and its
        # own AccessDenied must NOT abort detection — an elevated unknown
        # fullscreen game is exactly what the fallback exists to catch. Mirror
        # the known-games path, which isolates this same call.
        try:
            exe = proc.exe() if proc.is_running() else None
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            exe = None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    # Retry cooldown: skip only if this is the SAME process that just failed
    # (create_time match); a reused PID for a new fullscreen app must record.
    if _is_cooldown_blocked(pid, created, skip):
        return None
    lowered = name.lower()
    if lowered in _FULLSCREEN_SKIP_NAMES or (skip_names and lowered in skip_names):
        return None
    return ActiveGame(exe_name=name, pid=pid, exe_path=exe, create_time=created)


def _load_known_games(path: Path | None = None) -> list[str]:
    p = path or known_games_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("known_games.json not found at %s; watch list is empty", p)
        return []
    except json.JSONDecodeError:
        logger.exception("known_games.json is malformed at %s; watch list is empty", p)
        return []
    exes = data.get("executables")
    if not isinstance(exes, list):
        logger.warning("known_games.json has no 'executables' list")
        return []
    return [str(e) for e in exes if isinstance(e, str)]
