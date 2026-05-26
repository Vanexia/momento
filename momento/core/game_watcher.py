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
        self._known: set[str] = {g.lower() for g in (known_games or _load_known_games())}
        self._poll_interval = poll_interval
        self._record_any_fullscreen = bool(record_any_fullscreen)
        self.on_game_start = on_game_start
        self.on_game_stop = on_game_stop

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active: ActiveGame | None = None
        self._lock = threading.Lock()
        # Sustained-fullscreen filter: remember the candidate pid seen on the
        # PREVIOUS tick and only fire if the same pid is still foreground +
        # fullscreen on this tick. Filters out brief fullscreens (popups,
        # alt-tab overlays, transient maximisations) at the cost of one
        # poll-interval (~2s) of detection latency.
        self._pending_fullscreen_pid: int | None = None

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
        active = self._active

        # Snapshot config under the lock so update_* methods are safe.
        with self._lock:
            known = self._known
            fullscreen_mode = self._record_any_fullscreen

        # If a game is currently being tracked, first check whether it's still alive.
        if active is not None and not _pid_alive(active.pid):
            logger.info("Active game exited: %s (pid=%d)", active.exe_name, active.pid)
            self._active = None
            cb = self.on_game_stop
            if cb:
                cb(active)
            active = None

        # If still tracking something, ignore any other triggers.
        if active is not None:
            return

        # Primary trigger: scan for the first running known game. Known-games
        # match is trusted immediately — the user explicitly listed it.
        found = _find_first_known(known)

        if found is not None:
            self._pending_fullscreen_pid = None
        elif fullscreen_mode:
            # Fallback trigger (opt-in). Requires the SAME fullscreen pid to
            # be seen on two consecutive ticks before firing — kills false
            # positives from briefly-fullscreen things (popups, screenshot
            # tools, alt-tab dialogs, exclusive-mode handoffs).
            candidate = _find_foreground_fullscreen()
            if candidate is None:
                self._pending_fullscreen_pid = None
            elif self._pending_fullscreen_pid == candidate.pid:
                found = candidate
                self._pending_fullscreen_pid = None
            else:
                # First sighting — remember it; need to see it again next tick.
                self._pending_fullscreen_pid = candidate.pid
                logger.debug(
                    "Fullscreen candidate pending confirmation: %s (pid=%d)",
                    candidate.exe_name, candidate.pid,
                )
        else:
            self._pending_fullscreen_pid = None

        if found is None:
            return

        self._active = found
        logger.info("Game start detected: %s (pid=%d)", found.exe_name, found.pid)
        cb = self.on_game_start
        if cb:
            cb(found)


# ---------------------------------------------------------------- helpers
def _pid_alive(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        # Zombie/stopped should count as "not running" for our purposes.
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _find_first_known(known_lower: set[str]) -> ActiveGame | None:
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
                try:
                    exe_path = proc.exe()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    exe_path = None
                return ActiveGame(exe_name=name, pid=pid, exe_path=exe_path)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


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


def _find_foreground_fullscreen() -> ActiveGame | None:
    """Fallback trigger: any foreground window covering an entire monitor."""
    from momento.util.windows_api import foreground_fullscreen_pid

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
        exe = proc.exe() if proc.is_running() else None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    if name.lower() in _FULLSCREEN_SKIP_NAMES:
        return None
    return ActiveGame(exe_name=name, pid=pid, exe_path=exe)


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
