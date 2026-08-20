"""Regression test for the fullscreen-fallback blocklist (2026-07-03).

The opt-in "record any fullscreen" fallback must NEVER trigger a recording of a
well-known non-game that legitimately goes fullscreen (media players, browsers,
IDEs, chat/screen-share, AI assistant apps, ...). Three real incidents drove
this list: claude.exe (12h black-screen, 2026-07-01), stremio-shell-ng.exe (a
fullscreen streaming session recorded 2026-07-03), and steamwebhelper.exe (a
fullscreen Steam store trailer recorded 2026-08-20).

It must ALSO still fire for a genuinely-unknown fullscreen app (that's the whole
point of the opt-in fallback), and must honour the user's Auto-record-Off list
(disabled_games) passed as skip_names.

Pure-logic + fully mocked (no real windows/processes) -> CI-safe. Run:
    C:\\dev\\Momento\\.venv\\Scripts\\python.exe tests\\smoke_fullscreen_blocklist.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import momento.core.game_watcher as gw  # noqa: E402
from momento.config import Config  # noqa: E402
from momento.util import windows_api  # noqa: E402

_results: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> None:
    _results.append((name, bool(ok)))
    print(("PASS" if ok else "FAIL"), "-", name)


class _FakeProc:
    """Minimal psutil.Process stand-in for one foreground pid."""

    def __init__(self, pid: int, name: str) -> None:
        self._pid = pid
        self._name = name

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return f"C:\\Apps\\{self._name}"

    def is_running(self) -> bool:
        return True

    def create_time(self) -> float:
        return 1000.0


def _foreground(name: str, skip_names=None):
    """Run _find_foreground_fullscreen as if ``name`` were the foreground
    fullscreen app (pid 4242), with everything mocked."""
    orig_fg = windows_api.foreground_fullscreen_pid
    orig_proc = gw.psutil.Process
    orig_getpid = gw.os.getpid
    windows_api.foreground_fullscreen_pid = lambda: 4242
    gw.psutil.Process = lambda pid: _FakeProc(pid, name)
    gw.os.getpid = lambda: 999999  # never collide with our fake pid
    try:
        return gw._find_foreground_fullscreen(skip_names=skip_names)
    finally:
        windows_api.foreground_fullscreen_pid = orig_fg
        gw.psutil.Process = orig_proc
        gw.os.getpid = orig_getpid


def main() -> None:
    # 1) The blocklist membership itself (catches an accidental deletion).
    must_block = [
        "stremio-shell-ng.exe", "stremio.exe", "stremio-runtime.exe",
        "kodi.exe", "plex.exe", "jellyfinmediaplayer.exe", "netflix.exe",
        "claude.exe", "vlc.exe", "chrome.exe", "obs64.exe", "discord.exe",
        "code.exe",
        # Storefront video, embedded-browser, launcher, and overlay shells.
        "steam.exe", "steamwebhelper.exe", "gameoverlayui.exe",
        "epicgameslauncher.exe", "epicwebhelper.exe",
        "eadesktop.exe", "ealauncher.exe", "eabackgroundservice.exe",
        "ubisoftconnect.exe", "ubisoftconnectwebcore.exe", "uplaywebcore.exe",
        "gog galaxy.exe", "galaxyclient.exe", "galaxyclient helper.exe",
        "battle.net.exe", "battle.net helper.exe", "blizzardbrowser.exe",
        "riotclientservices.exe", "riotclientux.exe", "riotclientuxrender.exe",
        "leagueclient.exe", "leagueclientux.exe", "leagueclientuxrender.exe",
        "xboxpcapp.exe", "gamebar.exe", "gamebarftserver.exe",
        "msedgewebview2.exe",
    ]
    for exe in must_block:
        check(f"blocklist contains {exe}", exe in gw._FULLSCREEN_SKIP_NAMES)

    # 2) End-to-end through _find_foreground_fullscreen: blocked apps yield None.
    check("Stremio shell -> not recorded", _foreground("stremio-shell-ng.exe") is None)
    check("Stremio (legacy) -> not recorded", _foreground("Stremio.exe") is None)
    check("case-insensitive block (KODI.EXE)", _foreground("KODI.EXE") is None)
    check("Steam store trailer -> not recorded",
          _foreground("steamwebhelper.exe") is None)
    check("Steam helper block is case-insensitive",
          _foreground("SteamWebHelper.EXE") is None)
    check("Riot launcher shell -> not recorded",
          _foreground("RiotClientServices.exe") is None)
    check("Epic storefront video shell -> not recorded",
          _foreground("EpicWebHelper.exe") is None)

    # 3) The fallback STILL fires for a genuinely-unknown fullscreen app.
    got = _foreground("someindiegame.exe")
    check("unknown fullscreen game -> still detected", got is not None)
    check("detected game carries its exe name",
          got is not None and got.exe_name == "someindiegame.exe")
    check("detected game carries create_time (PID-identity)",
          got is not None and got.create_time == 1000.0)

    # 4) The user's Auto-record-Off list (skip_names) is honoured — a real
    #    game the user disabled must not sneak in via the fallback.
    check("disabled game (skip_names) -> not recorded",
          _foreground("someindiegame.exe", skip_names={"someindiegame.exe"}) is None)
    check("skip_names is case-folded by caller convention (already-lower)",
          _foreground("othergame.exe", skip_names={"othergame.exe"}) is None)

    # 5) Fresh and migrated configs must not treat login screens, patchers,
    #    anti-cheat bootstraps, or game launchers as gameplay. Every entry has
    #    a separate actual-game executable in the curated defaults.
    launcher_only = {
        "ffxiv_boot.exe", "ffxivlauncher.exe", "esolauncher.exe",
        "blackdesertlauncher.exe", "jagexlauncher.exe", "aionlauncher.exe",
        "dcuolauncher.exe", "fortnitelauncher.exe",
        "easyanticheat_launcher.exe", "destinylauncher.exe",
        "marvelrivals_launcher.exe", "minecraftlauncher.exe",
        "dayzlauncher.exe", "gtavlauncher.exe", "mealauncher.exe",
        "iracingui.exe", "diablo iv launcher.exe",
    }
    fresh = {name.lower() for name in Config().known_games}
    check("fresh defaults exclude launcher-only executables",
          launcher_only.isdisjoint(fresh))
    gameplay_replacements = {
        "ffxiv_dx11.exe", "eso64.exe", "blackdesert64.exe", "runelite.exe",
        "aion.exe", "dcgame.exe", "fortniteclient-win64-shipping.exe",
        "r5apex.exe", "destiny2.exe", "marvelrivals-win64-shipping.exe",
        "minecraft.windows.exe", "dayz_x64.exe", "gta5.exe",
        "masseffectandromeda.exe", "iracingsim64dx12.exe", "diablo iv.exe",
    }
    check("launcher pruning keeps each title's gameplay executable",
          gameplay_replacements.issubset(fresh))
    migrated = Config.from_dict({
        "known_games": [
            "MarvelRivals_Launcher.exe",
            "MarvelRivals-Win64-Shipping.exe",
            "MinecraftLauncher.exe",
            "Minecraft.Windows.exe",
        ]
    })
    check("saved configs prune launcher-only executables",
          {name.lower() for name in migrated.known_games} == {
              "marvelrivals-win64-shipping.exe", "minecraft.windows.exe"
          })
    check("bundled fallback list excludes the League lobby",
          "leagueclient.exe" not in {
              name.lower() for name in gw._load_known_games()
          })

    # 6) A delayed retry callback for an exited process must not release a new
    # process that inherited the same Windows PID.
    watcher = gw.GameWatcher(known_games=[])
    original = gw.ActiveGame("oldgame.exe", 4242, None, create_time=1000.0)
    replacement = gw.ActiveGame("newgame.exe", 4242, None, create_time=2000.0)
    watcher._active = replacement
    watcher.release_active_for_retry(original, retry_after_s=0)
    check("retry release preserves a replacement process with reused PID", watcher.active is replacement)

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
