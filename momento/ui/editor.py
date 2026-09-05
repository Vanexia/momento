"""Editor window: recordings/clips list (left), preview + timeline (right).

Per-row duration and the ``MOMENTO_GAME`` tag are probed asynchronously
through :mod:`momento.core.media_probe`, so a folder with many recordings
doesn't freeze the UI.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QRect, Qt, QSettings, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from momento.config import Config
from momento.core.bookmarks import load_bookmarks, sidecar_path_for as bookmark_sidecar
from momento.core.game_names import (
    friendly_recording_title,
    game_slug_from_filename,
    humanise_game_name,
)
from momento.core.game_watcher import ActiveGame
from momento.core.media_probe import (
    is_repairing,
    probe_duration_async,
    probe_metadata_async,
    repair_async,
)
from momento.core.recording_files import is_repair_temp
from momento.core.recording_safety import (
    begin_file_activity,
    has_valid_ownership_marker,
    is_file_active,
    mark_recording_owned,
    ownership_sidecar_path,
)
from momento.util.time_format import fmt_time
from momento.core.thumbnails import extract_async, thumb_is_fresh, thumb_path_for
from momento.trim.ffmpeg_trim import TrimWorker, next_clip_path
from momento.ui.preview import VideoPreview
from momento.ui.recordings_list import RecordingsList
from momento.ui import icons
from momento.ui import theme as _theme
from momento.ui.settings_dialog import SettingsPanel
from momento.ui.status_panel import StatusPanel
from momento.ui.timeline import Timeline
from momento.ui.widgets import AnchoredComboBox
from momento.util.paths import window_state_path
from momento.util.resources import app_icon_path
from momento.util import windows_api

logger = logging.getLogger(__name__)

# Clip/timeline panel height bounds (shared by construction +
# _sync_bottom_panel_height). BASE_MIN is the no-bookmarks design height; MAX
# has headroom for the bookmark chip strip and the pan scrollbar both present.
_BOTTOM_PANEL_BASE_MIN = 148
_BOTTOM_PANEL_MAX = 260


# Sort-dropdown options for the recordings/clips list.
# Tuples are (combo_label, key) — key maps to a branch in _sorted_files.
_SORT_MODES: tuple[tuple[str, str], ...] = (
    ("Newest first", "newest"),
    ("Oldest first", "oldest"),
    ("Longest", "longest"),
    ("Largest", "largest"),
    ("Game name", "game"),
)


class _YouTubeCredentialsBridge(QObject):
    """Carries a credential-refresh result back to the GUI thread."""

    completed = pyqtSignal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.config_epoch = 0


class _LinkLabel(QLabel):
    """A small text link (muted → lightens on hover) used for inline actions
    like the rail footer's 'Manage storage'."""

    clicked = pyqtSignal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply(_theme.TEXT_LOW)

    def _apply(self, colour: str) -> None:
        self.setStyleSheet(
            f"color: {colour}; font-size: 11.5px; font-weight: 600; background: transparent;"
        )

    def enterEvent(self, _event) -> None:  # noqa: N802
        self._apply(_theme.TEXT_HIGH)

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._apply(_theme.TEXT_LOW)

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self.clicked.emit()


class EditorWindow(QMainWindow):
    """The recordings browser + preview + timeline + settings host."""

    # Emitted when the user picks a different recording.
    selected_changed = pyqtSignal(object)  # Path | None
    # Emitted when the embedded settings panel saves — tray listens to apply
    # to SessionManager + hotkey + autostart.
    settings_saved = pyqtSignal(object)  # Config
    check_updates_requested = pyqtSignal()

    def __init__(
        self,
        config: Config,
        session=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Momento")
        self.resize(1200, 700)
        icon_p = app_icon_path()
        if icon_p is not None:
            self.setWindowIcon(QIcon(str(icon_p)))

        self._config = config
        self._session = session
        self._thumb_submitted: set[str] = set()
        self._all_files: list[Path] = []
        # "All games" sentinel — combo userData when no filter is applied.
        self._game_filter: str | None = None
        # path → game slug derived from the file's MOMENTO_GAME container
        # tag. Survives rename because the tag lives inside the file. The
        # sentinel ``None`` means "probe in flight" so we never double-submit;
        # an empty string means "probed, no tag" → fall back to the filename
        # regex; any other string is the slug.
        self._game_slug_cache: dict[str, str | None] = {}
        # Batch many probe-completions in the same event-loop turn into a
        # single combo rebuild. Set when a rebuild is queued, cleared by it.
        self._filter_rebuild_pending = False
        # path → duration in seconds (None = unknown). Populated from the
        # combined metadata probe alongside _game_slug_cache. Used by the
        # "Longest" sort option; sort is re-applied when a probe lands while
        # that mode is active.
        self._duration_cache: dict[str, float | None] = {}
        # ffprobe-derived duration hints (seconds) for broken-metadata files
        # whose QMediaPlayer.duration() stays 0. Cached so a tray-park reopen can
        # re-apply the hint without re-probing (and without racing teardown).
        self._duration_hint_cache: dict[str, float] = {}
        # User-driven list controls — pure UI state, not persisted.
        self._search_text: str = ""
        # See _SORT_MODES below for the option list and tuple shape.
        self._sort_mode: str = "newest"
        # Currently-selected recording. The preview's heavy QMediaPlayer/WMF
        # load is deferred to when the editor is actually on screen (see
        # _sync_preview_to_visibility) so a tray-resident, never-opened window
        # doesn't keep a multi-hour video decoder resident in RAM.
        self._current_selection: Path | None = None
        self._parked_preview_path: Path | None = None
        self._parked_preview_position: float = 0.0
        self._youtube_auth_bridge: _YouTubeCredentialsBridge | None = None
        self._youtube_upload_path: Path | None = None
        self._youtube_config_epoch = 0

        self._build_menu()

        # The window hosts a QStackedWidget with two pages:
        #   0: the editor (recordings list + preview + timeline) — main UX
        #   1: the settings panel (Audio / Capture / Output / ...)
        # The cogwheel on the editor's toolbar switches to settings; the
        # settings panel's "Back" / "Save" emits done() which switches back.
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_editor_view())

        # The settings panel is heavy to build (~640-row games table, ~0.8s).
        # Build it lazily on first open so showing the editor lands on the
        # recordings page fast; an idle timer below pre-warms it so the first
        # Settings open is also instant.
        self._settings_panel: SettingsPanel | None = None

        self.setCentralWidget(self._stack)
        # Start transparent; showEvent reveals once painted (see showEvent).
        self.setWindowOpacity(0.0)
        self._install_shortcuts()
        self._restore_window_state()
        # Tray Quit calls QApplication.quit() which skips closeEvent — hook
        # aboutToQuit too so geometry is always written before exit.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._save_window_state)
            app.aboutToQuit.connect(self._cancel_active_trim_for_quit)
        self.refresh()
        # NB: settings is built lazily on first open (see _ensure_settings_panel)
        # rather than idle-prebuilt — a background build would land as a visible
        # ~0.75s hitch shortly after the window opens. The one-time build when
        # the user actually navigates to Settings is the expected place for it.

    # ----------------------------------------------------------- shortcuts
    def _install_shortcuts(self) -> None:
        """Standard NLE / video player muscle memory.

        Bindings are window-scoped (not focus-widget-scoped) so they work
        regardless of which child has focus — except inside text-entry
        widgets where Qt suppresses them via the editing focus chain.
        """
        def add(seq: str, slot) -> None:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

        add("Space", self._toggle_play_pause)
        add("M", self._toggle_mute)
        add("F", self._toggle_fullscreen)
        add("Escape", self._exit_fullscreen_if_active)
        add("Left", lambda: self._nudge(-5.0))
        add("Right", lambda: self._nudge(+5.0))
        add("Shift+Left", lambda: self._nudge(-1.0))
        add("Shift+Right", lambda: self._nudge(+1.0))
        add("Home", self._seek_to_start)
        add("End", self._seek_to_end)

    def _is_text_focus(self) -> bool:
        """Suppress player shortcuts while the user types into a text field
        (clip-name dialog, hotkey field, known-games box, ...)."""
        fw = QApplication.focusWidget() if QApplication.instance() else None
        if fw is None:
            return False
        # QLineEdit / QTextEdit / QPlainTextEdit and subclasses.
        return isinstance(fw, (QLineEdit,)) or fw.metaObject().className() in {
            "QTextEdit", "QPlainTextEdit"
        }

    def _toggle_play_pause(self) -> None:
        if self._is_text_focus():
            return
        self.preview.toggle_play()

    def _toggle_mute(self) -> None:
        if self._is_text_focus():
            return
        self.preview.toggle_mute()

    def _toggle_fullscreen(self) -> None:
        if self._is_text_focus():
            return
        self.preview.toggle_fullscreen()

    def _exit_fullscreen_if_active(self) -> None:
        """Esc handler — only acts when the video is fullscreen, so it doesn't
        swallow Escape elsewhere."""
        if self.preview.is_fullscreen():
            self.preview.toggle_fullscreen()

    def _apply_video_fullscreen(self, fullscreen: bool) -> None:
        """Make THIS editor window fullscreen showing only the video, then
        restore — driven by VideoPreview's fullscreen toggle.

        The video widget itself is never moved; we just hide the surrounding
        chrome (status strip, recordings list, clip/timeline panel, menu bar)
        and fullscreen the window, so the video fills the same window handle
        that screen-share / window-capture is following.
        """
        def _show(widget, visible: bool) -> None:
            if widget is not None:
                widget.setVisible(visible)

        menu = self.menuBar()
        # _status_panel is None when there's no live session (tests/screenshots).
        chrome = (self._status_panel, self._left_pane, self._bottom_panel, menu)
        WS = Qt.WindowState
        hwnd = int(self.winId())
        if fullscreen:
            # Capture the EXACT pre-fullscreen placement via Win32 (showCmd +
            # normal restore rect) — the reliable record of "maximized vs
            # normal" for the exit restore.
            self._fs_was_maximized = self.isMaximized()
            self._fs_placement = windows_api.save_window_placement(hwnd)
            self._fs_main_sizes = self._main_splitter.sizes()
            self._fs_right_sizes = self._right_splitter.sizes()
            # Animations off for the whole fullscreen session (re-enabled on the
            # exit settle); keeps both the enter and exit resizes instant.
            windows_api.set_window_transitions_enabled(hwnd, False)
            for w in chrome:
                _show(w, False)
            # Borderless fullscreen via Win32: the SAME HWND covers the monitor
            # (so Discord/OBS window-share follows), and Qt's window STATE is
            # never touched — which is the whole point. Qt's fullscreen<->max
            # stepping drops the window to its NORMAL size for a frame on the
            # way back, and DWM composites that frame (the exit "flash");
            # driving the HWND straight max<->monitor-bounds never goes there.
            self._fs_style = windows_api.enter_borderless_fullscreen(hwnd)
            if self._fs_style is None:
                # Non-Windows / failure: fall back to Qt fullscreen.
                self.setWindowState(
                    (WS.WindowMaximized | WS.WindowFullScreen)
                    if self._fs_was_maximized
                    else WS.WindowFullScreen
                )
        else:
            windows_api.set_window_transitions_enabled(hwnd, False)
            self.setUpdatesEnabled(False)
            try:
                for w in chrome:
                    _show(w, True)
                if getattr(self, "_fs_style", None) is not None:
                    # Direct maximized<->monitor-bounds restore — no normal-size
                    # intermediate frame, so no flash.
                    restored = windows_api.exit_borderless_fullscreen(
                        hwnd, self._fs_style, getattr(self, "_fs_placement", None)
                    )
                    if not restored and getattr(self, "_fs_was_maximized", False):
                        # Placement restore failed (capture failed at enter) —
                        # same best-effort fallback the Qt branch uses.
                        self.setWindowState(WS.WindowMaximized)
                else:
                    # Qt fallback (we entered via Qt fullscreen): restore the
                    # frame, then snap geometry + maximized state back via the
                    # placement Win32 can't drop.
                    self.setWindowState(WS.WindowNoState)
                    restored = windows_api.restore_window_placement(
                        hwnd, getattr(self, "_fs_placement", None)
                    )
                    if not restored and getattr(self, "_fs_was_maximized", False):
                        self.setWindowState(WS.WindowMaximized)
                # Splitter proportions can reset while a child is hidden — put
                # them back so the layout returns exactly as it was.
                if getattr(self, "_fs_main_sizes", None):
                    self._main_splitter.setSizes(self._fs_main_sizes)
                if getattr(self, "_fs_right_sizes", None):
                    self._right_splitter.setSizes(self._fs_right_sizes)
            finally:
                self.setUpdatesEnabled(True)
            self._fs_style = None
            self._fs_placement = None
            # Re-enable window animations + log the TRUE end state once the WM
            # has settled (a synchronous read here reports the pre-WM state and
            # misleads). The deferred log keeps fullscreen
            # round-trips diagnosable from momento.log.
            def _settle() -> None:
                windows_api.set_window_transitions_enabled(hwnd, True)
                try:
                    r = self.geometry()
                    logger.info(
                        "Fullscreen exit settled: was_maximized=%s isMaximized=%s "
                        "geom=%dx%d+%d+%d",
                        getattr(self, "_fs_was_maximized", None), self.isMaximized(),
                        r.width(), r.height(), r.x(), r.y(),
                    )
                except RuntimeError:
                    pass  # window torn down within the settle window — fine
            QTimer.singleShot(80, _settle)

    def _nudge(self, seconds: float) -> None:
        if self._is_text_focus():
            return
        dur = self.preview.duration()
        if dur <= 0:
            return
        target = max(0.0, min(dur, self.preview.position() + seconds))
        self.preview.seek(target)

    def _jump_to_bookmark(self, seconds: float) -> None:
        """Seek to a bookmark and start playback — clicking a bookmark means
        'take me there and play', so the user doesn't have to hit play after."""
        self.preview.seek(seconds)
        self.preview.play()

    def _seek_to_start(self) -> None:
        if self._is_text_focus():
            return
        self.preview.seek(0.0)

    def _seek_to_end(self) -> None:
        if self._is_text_focus():
            return
        dur = self.preview.duration()
        if dur > 0:
            self.preview.seek(max(0.0, dur - 0.05))

    # ----------------------------------------------------------- API
    def refresh(self, preserve_selection: bool = False) -> None:
        """Rescan the output folder and repopulate the list.

        ``preserve_selection=True`` keeps the user's current selection (and
        thus the preview / timeline) intact across the rebuild — used by the
        live auto-refresh when a new recording finishes, so a recording landing
        while the window is open doesn't yank whatever the user is viewing.
        """
        # Probe + thumb jobs run on the global pool; they self-clean. Just
        # clear our "already submitted" tracking so a re-scanned folder can
        # re-submit any rows whose files came back.
        self._thumb_submitted.clear()
        folder = Path(self._config.output_folder)
        excluded: set[Path] = set()
        if self._session is not None:
            active = getattr(self._session, "current_output", None)
            if active is not None:
                excluded.add(Path(active))
        # The live MKV already exists and is incrementally readable, but it is
        # not library media yet. Listing it would expose delete/rename/repair,
        # thumbnail and upload actions while the encoder still owns the file.
        self._all_files = _list_recordings(folder, exclude_paths=excluded)
        # Update tab labels with current counts so the user sees "(N)" without
        # having to switch.
        self._update_tab_counts()
        self._rebuild_game_filter()
        self._reapply_filter(preserve_selection=preserve_selection)
        section_label = "clip" if self._section_tabs.currentIndex() == 1 else "recording"
        visible_count = self._visible_count()
        self._status.setText(f"{visible_count} {section_label}(s) in {folder}")

    def add_finished_recording(
        self, path: Path | str, preserve_selection: bool = True
    ) -> bool:
        """Add or update one finished recording without rescanning the folder."""
        p = Path(path).resolve()
        if (
            p.suffix.lower() not in _RECORDING_SUFFIXES
            or is_repair_temp(p)
            or not p.is_file()
        ):
            return False

        key = str(p)
        replaced = False
        for i, existing in enumerate(self._all_files):
            if str(existing) == key:
                self._all_files[i] = p
                replaced = True
                break
        if not replaced:
            self._all_files.append(p)
        self._all_files.sort(key=_safe_mtime, reverse=True)

        # Force a fresh metadata pass now that the container is closed or
        # recoverable; refresh deliberately excludes it while it is live.
        self._game_slug_cache.pop(key, None)
        self._duration_cache.pop(key, None)
        self._duration_hint_cache.pop(key, None)
        self._thumb_submitted.discard(key)

        self._update_tab_counts()
        self._rebuild_game_filter()
        self._reapply_filter(preserve_selection=preserve_selection)
        section_label = "clip" if self._section_tabs.currentIndex() == 1 else "recording"
        self._status.setText(f"{self._visible_count()} {section_label}(s) shown")
        return True

    def _update_tab_counts(self) -> None:
        n_rec = sum(1 for f in self._all_files if not _is_clip(f))
        n_clip = sum(1 for f in self._all_files if _is_clip(f))
        self._section_tabs.setTabText(0, f"Recordings ({n_rec})")
        self._section_tabs.setTabText(1, f"Clips ({n_clip})")

    # ------------------------------------------------------- filtering
    def _files_in_current_section(self) -> list[Path]:
        """Return the subset of ``_all_files`` matching the active tab."""
        want_clips = self._section_tabs.currentIndex() == 1
        return [f for f in self._all_files if _is_clip(f) == want_clips]

    def _visible_count(self) -> int:
        return len(self._visible_files())

    def _game_slug_for(self, path: Path) -> str | None:
        """Embedded ``MOMENTO_GAME`` tag if probed, else the filename fallback.

        Cache values: missing or ``None`` (in-flight) → filename fallback;
        ``""`` (probed, no tag) → filename fallback; non-empty string → use it.
        """
        cached = self._game_slug_cache.get(str(path))
        if cached:
            return cached
        return game_slug_from_filename(path.name)

    def _rebuild_game_filter(self) -> None:
        """Populate the combo with the unique games in the active tab."""
        from collections import Counter
        files = self._files_in_current_section()
        counts = Counter(
            s for s in (self._game_slug_for(p) for p in files) if s
        )
        current = self._game_filter
        self._game_combo.blockSignals(True)
        try:
            self._game_combo.clear()
            self._game_combo.addItem(f"All games ({len(files)})", None)
            for slug in sorted(counts):
                friendly = humanise_game_name(slug + ".exe")
                self._game_combo.addItem(f"{friendly} ({counts[slug]})", slug)
            target = 0
            if current is not None:
                for i in range(self._game_combo.count()):
                    if self._game_combo.itemData(i) == current:
                        target = i
                        break
                else:
                    self._game_filter = None
            self._game_combo.setCurrentIndex(target)
        finally:
            self._game_combo.blockSignals(False)

    def _reapply_filter(self, preserve_selection: bool = False) -> None:
        """Re-render the list according to tab + game filter + search + sort.

        ``preserve_selection=True`` re-selects the previously-selected
        recording (if it's still visible) WITHOUT re-emitting the selection —
        so a live refresh leaves the preview / timeline / playback untouched.
        """
        prev = self._current_selection if preserve_selection else None
        if prev is not None:
            # Quiet rebuild: BOTH clear() (selection cleared) and the re-select
            # emit selection signals; block them around the whole rebuild so the
            # preview/timeline the user is viewing stay put. The selected
            # recording is already loaded, so nothing needs to reload.
            sm = self._list.selectionModel()
            was_blocked = sm.blockSignals(True)
            try:
                self._list.clear()
                for f in self._visible_files():
                    self._add_item(f)
                restored = self._list.select_by_path(prev, emit=False)
            finally:
                sm.blockSignals(was_blocked)
            self._update_list_empty_state()
            if restored:
                return  # selection + preview untouched
            # prev was deleted / filtered out — fall through to pick a fresh row
            # and let it drive the preview.
        else:
            self._list.clear()
            for f in self._visible_files():
                self._add_item(f)
            self._update_list_empty_state()

        # Auto-select the first row so the preview opens on real content, not a
        # black QVideoWidget. Empty → emit None so the preview clears any stale
        # source/timeline state.
        if self._list.row_count() > 0:
            self._list.select_first()
        else:
            self.selected_changed.emit(None)

    def _visible_files(self) -> list[Path]:
        """Apply tab + game filter + search to ``_all_files``, then sort."""
        files = self._files_in_current_section()
        if self._game_filter is not None:
            files = [f for f in files if self._game_slug_for(f) == self._game_filter]
        if self._search_text:
            needle = self._search_text.lower()
            files = [f for f in files if self._matches_search(f, needle)]
        return self._sorted_files(files)

    def _matches_search(self, path: Path, needle: str) -> bool:
        """True if ``path`` matches ``needle`` (lower-case substring search).

        Checks the file stem and the humanised game title — that's how the
        card itself is rendered, so the search box stays predictable.
        """
        from momento.core.game_names import friendly_recording_title
        stem = path.stem.lower()
        title = friendly_recording_title(path.name).lower()
        return needle in stem or needle in title

    def _sorted_files(self, files: list[Path]) -> list[Path]:
        mode = self._sort_mode
        if mode == "oldest":
            return sorted(files, key=_safe_mtime)
        if mode == "largest":
            return sorted(files, key=_safe_size, reverse=True)
        if mode == "game":
            return sorted(
                files,
                key=lambda p: (
                    (self._game_slug_for(p) or "").lower(),
                    -_safe_mtime(p),  # ties broken newest-first
                ),
            )
        if mode == "longest":
            return sorted(
                files,
                key=lambda p: self._duration_cache.get(str(p)) or 0.0,
                reverse=True,
            )
        # "newest" (default) and any unknown mode fall through here.
        return sorted(files, key=_safe_mtime, reverse=True)

    def _update_list_empty_state(self) -> None:
        """Show the list itself or a contextual placeholder, depending on
        whether the current tab + filter has any matching files."""
        if self._list.row_count() > 0:
            self._list_stack.setCurrentIndex(0)
            return
        on_clips_tab = self._section_tabs.currentIndex() == 1
        filter_active = (
            self._game_filter is not None or bool(self._search_text)
        )
        if filter_active:
            msg = "No matches — try clearing the search or game filter."
        elif on_clips_tab:
            msg = (
                "No clips yet.\n\n"
                "Open a recording, drag the trim handles, then choose "
                "Export clip to create one."
            )
        else:
            msg = (
                "No recordings yet.\n\n"
                "Launch a game from your known-games list — Momento records "
                "automatically while it's running."
            )
        self._list_empty_label.setText(msg)
        self._list_stack.setCurrentIndex(1)

    def _on_section_changed(self, _index: int) -> None:
        self._rebuild_game_filter()
        self._reapply_filter()
        section_label = "clip" if self._section_tabs.currentIndex() == 1 else "recording"
        self._status.setText(f"{self._visible_count()} {section_label}(s) shown")

    def _on_game_filter_changed(self, index: int) -> None:
        self._game_filter = self._game_combo.itemData(index)
        self._reapply_filter()

    def _on_search_text_changed(self, text: str) -> None:
        self._search_text = text.strip()
        self._reapply_filter()

    def _on_sort_changed(self, index: int) -> None:
        self._sort_mode = self._sort_combo.itemData(index) or "newest"
        self._reapply_filter()

    # ----------------------------------------------------------- views
    def _build_editor_view(self) -> QWidget:
        """The top-level page shown when not in settings — everything you
        actually edit clips with. The settings cog lives in the left pane's
        Recordings header so it doesn't waste a whole toolbar row."""
        wrapper = QWidget()
        col = QVBoxLayout(wrapper)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # Status panel needs a SessionManager to read live recording state.
        # The smoke tests construct EditorWindow with session=None, so the
        # strip is only shown when we have a real session attached.
        self._status_panel: StatusPanel | None = None
        if self._session is not None:
            self._status_panel = StatusPanel(self._session, self._config)
            self._status_panel.settings_clicked.connect(self._show_settings_view)
            col.addWidget(self._status_panel)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        left_pane = self._build_left_pane()
        self._left_pane = left_pane
        # Keep the recordings list usably wide and stop the splitter from
        # collapsing either pane to zero when dragged past its minimum (the
        # default QSplitter behaviour, which made the left pane vanish).
        left_pane.setMinimumWidth(300)
        self._main_splitter.addWidget(left_pane)
        self._main_splitter.addWidget(self._build_right_pane())
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([460, 740])
        col.addWidget(self._main_splitter, stretch=1)
        return wrapper

    def prewarm_settings(self) -> None:
        """Build the settings panel ahead of time (e.g. from the tray's idle
        prebuild, while this window is still hidden) so the first Settings
        open is instant rather than paying the ~0.7s build on click."""
        self._ensure_settings_panel()

    def has_update_blocking_activity(self) -> bool:
        """Return whether editor-owned background work must finish first."""
        trim_thread = getattr(self, "_trim_thread", None)
        settings_auth_active = bool(
            self._settings_panel is not None
            and self._settings_panel.has_active_youtube_auth()
        )
        return bool(
            (trim_thread is not None and trim_thread.isRunning())
            or self._youtube_auth_bridge is not None
            or settings_auth_active
        )

    def _ensure_settings_panel(self) -> SettingsPanel:
        """Lazy-construct the (heavy) settings panel and wire it once."""
        if self._settings_panel is None:
            self._settings_panel = SettingsPanel(self._config, session=self._session)
            self._settings_panel.settings_saved.connect(self._on_settings_saved)
            self._settings_panel.youtube_configuration_changed.connect(
                self._on_youtube_configuration_changed
            )
            self._settings_panel.done.connect(self._show_editor_view)
            self._stack.addWidget(self._settings_panel)
        return self._settings_panel

    def _show_settings_view(self, open_tab: str | None = None) -> None:
        """Swap the stack to the settings page. ``open_tab`` can be e.g. "Audio"."""
        # Refresh from disk in case config changed via the warning-toast path,
        # then optionally jump to a specific tab (e.g. when summoned from the
        # tray's "Settings" menu or the welcome dialog).
        panel = self._ensure_settings_panel()
        panel.reload_from_config(self._config)
        if open_tab:
            panel.open_tab(open_tab)
        self._stack.setCurrentWidget(panel)

    def _show_editor_view(self) -> None:
        self._stack.setCurrentIndex(0)

    def _on_settings_saved(self, new_cfg: Config) -> None:
        # Update our own pointer so subsequent filter / output operations see
        # the new folder; the outer app (tray + session) is wired in __main__.
        self._config = new_cfg
        if self._status_panel is not None:
            self._status_panel.set_config(new_cfg)
        # If the output folder changed, re-scan immediately on returning.
        self.refresh()
        # Forward via the existing tray hook (set in tray.py).
        self.settings_saved.emit(new_cfg)

    def set_session_status(self, status: str, game: ActiveGame | None) -> None:
        """Pushed from the tray when SessionManager status changes."""
        if self._status_panel is not None:
            self._status_panel.set_status(status, game)

    # ----------------------------------------------------------- build
    def _build_menu(self) -> None:
        menubar = self.menuBar()
        file_menu: QMenu = menubar.addMenu("&File")
        refresh_action = QAction("&Refresh", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.refresh)
        file_menu.addAction(refresh_action)
        open_folder_action = QAction("Open output &folder", self)
        open_folder_action.triggered.connect(self._open_output_folder)
        file_menu.addAction(open_folder_action)
        file_menu.addSeparator()
        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(lambda: self._show_settings_view())
        file_menu.addAction(settings_action)
        tutorial_action = QAction("Run setup &tutorial…", self)
        tutorial_action.triggered.connect(self._run_setup_tutorial)
        file_menu.addAction(tutorial_action)
        file_menu.addSeparator()
        close_action = QAction("&Close", self)
        close_action.setShortcut("Ctrl+W")
        close_action.triggered.connect(self.close)
        file_menu.addAction(close_action)

        help_menu: QMenu = menubar.addMenu("&Help")
        update_action = QAction("Check for &updates...", self)
        update_action.triggered.connect(self.check_updates_requested.emit)
        help_menu.addAction(update_action)

    def _run_setup_tutorial(self) -> None:
        """Re-open the first-time setup wizard with the current config.

        The wizard's ``settings_saved`` signal flows through the same
        slot the embedded settings panel uses, so the tray reloads the
        session and persists the new config exactly like any other save.
        """
        from momento.ui.welcome import WelcomeDialog
        dlg = WelcomeDialog(self._config, self)
        dlg.settings_saved.connect(self._on_settings_saved)
        dlg.exec()

    # ----------------------------------------------------------- public nav
    def show_settings(self, open_tab: str | None = None) -> None:
        """Public entry point — used by the tray's Settings menu item and the
        first-run welcome dialog to surface the settings page."""
        self._show_settings_view(open_tab=open_tab)

    def _build_left_pane(self) -> QWidget:
        wrap = QWidget()
        wrap.setObjectName("LibraryRail")
        wrap.setStyleSheet(
            f"QWidget#LibraryRail {{ background-color: {_theme.BG_RAIL}; "
            f"border-right: 1px solid {_theme.BORDER_HAIRLINE}; }}"
        )
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 14, 12, 0)
        layout.setSpacing(10)

        # Segmented tab control — splits the user's files into "Recordings"
        # (raw game captures) vs "Clips" (exported trims). Letting users
        # mass-delete one without taking the other is a real safety win.
        self._section_tabs = QTabBar()
        self._section_tabs.setDocumentMode(True)
        self._section_tabs.setExpanding(True)
        self._section_tabs.setDrawBase(False)
        self._section_tabs.addTab("Recordings")
        self._section_tabs.addTab("Clips")
        self._section_tabs.setStyleSheet(
            f"QTabBar {{ background: {_theme.BG_INPUT}; border: 1px solid {_theme.BORDER}; "
            f"border-radius: 10px; padding: 4px; }}"
            "QTabBar::tab { background: transparent; color: #6b6d78; padding: 9px 14px; "
            "margin: 0 2px; border-radius: 7px; font-size: 12.5px; font-weight: 700; }"
            "QTabBar::tab:hover:!selected { color: #b6b8c2; }"
            "QTabBar::tab:selected { color: #e9d5ff; background: qlineargradient("
            "x1:0, y1:0, x2:0, y2:1, stop:0 #2a2233, stop:1 #1e1a28); "
            "border: 1px solid rgba(168,85,247,0.30); }"
        )
        self._section_tabs.currentChanged.connect(self._on_section_changed)
        layout.addWidget(self._section_tabs)

        # Search box — case-insensitive substring match against the friendly
        # game name + filename stem. Hidden value, not persisted.
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search recordings, games, highlights…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.addAction(
            icons.qicon("search", 15, _theme.TEXT_FAINT),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self._search_edit)

        # Two pill dropdowns side by side: game filter + sort order. Their
        # current value labels them (e.g. "All games" / "Newest"), so no
        # separate captions — matches the design rail.
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self._game_combo = AnchoredComboBox()
        self._game_combo.setToolTip(
            "Show only files from a specific game. The dropdown only lists "
            "games that have at least one matching file in the folder."
        )
        self._game_combo.addItem("All games", None)
        self._game_combo.currentIndexChanged.connect(self._on_game_filter_changed)
        filter_row.addWidget(self._game_combo, stretch=1)
        self._sort_combo = AnchoredComboBox()
        self._sort_combo.setToolTip("Sort order")
        for label, key in _SORT_MODES:
            self._sort_combo.addItem(label, key)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        filter_row.addWidget(self._sort_combo, stretch=1)
        layout.addLayout(filter_row)

        self._list = RecordingsList()
        self._list.selected_path_changed.connect(self._on_list_selection_changed)
        self._list.delete_requested.connect(self._on_delete_requested)
        self._list.reveal_in_explorer_requested.connect(self._on_reveal_in_explorer)
        self._list.rename_requested.connect(self._on_rename_requested)
        self._list.repair_requested.connect(self._on_repair_requested)
        self._list.play_requested.connect(self._on_play_requested)
        self._list.export_requested.connect(self._on_export_requested_from_list)
        self._list.upload_to_youtube_requested.connect(self._on_upload_to_youtube_requested)

        # The cards paint their own surfaces, so the list rides transparently
        # on the rail background.
        self._list.setStyleSheet("QListView { background: transparent; border: none; }")

        # Stack the list with an empty-state placeholder so the left pane
        # doesn't show a blank QListView when the folder is empty / a filter
        # matches nothing. The empty message is rewritten in _reapply_filter.
        self._list_empty_label = QLabel("")
        self._list_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_empty_label.setWordWrap(True)
        self._list_empty_label.setStyleSheet(
            f"QLabel {{ color: {_theme.TEXT_MID}; font-size: 10pt; padding: 32px 24px; }}"
        )
        self._list_stack = QStackedWidget()
        self._list_stack.addWidget(self._list)
        self._list_stack.addWidget(self._list_empty_label)
        layout.addWidget(self._list_stack, stretch=1)

        # Rail footer: recordings count + folder (left) · Manage storage (right).
        # Delete / Refresh remain on the right-click menu, the Delete key, and
        # File → Refresh — matching the design's clean rail.
        footer = QFrame()
        footer.setObjectName("RailFooter")
        footer.setStyleSheet(
            f"QFrame#RailFooter {{ border-top: 1px solid {_theme.BORDER_HAIRLINE}; }}"
            "QFrame#RailFooter QLabel { background: transparent; }"
        )
        footer_lay = QHBoxLayout(footer)
        footer_lay.setContentsMargins(2, 12, 4, 12)
        footer_lay.setSpacing(8)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {_theme.TEXT_FAINT}; font-size: 11.5px;")
        footer_lay.addWidget(self._status)
        footer_lay.addStretch(1)
        self._manage_storage_link = _LinkLabel("Manage storage")
        self._manage_storage_link.clicked.connect(
            lambda: self._show_settings_view(open_tab="Output")
        )
        footer_lay.addWidget(self._manage_storage_link)
        layout.addWidget(footer)

        return wrap

    def _make_action_button(
        self, text: str, icon_name: str | None, primary: bool = False
    ) -> QPushButton:
        """A footer action button (gradient primary / dark secondary) with an
        optional leading glyph."""
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if primary:
            btn.setObjectName("primary")
        if icon_name:
            colour = "#ffffff" if primary else _theme.ACCENT_VIOLET
            btn.setIcon(icons.qicon(icon_name, 15, colour))
            btn.setIconSize(QSize(15, 15))
        return btn

    def _on_open_location_clicked(self) -> None:
        if self._current_selection is not None:
            self._on_reveal_in_explorer(self._current_selection)

    def _on_upload_clicked(self) -> None:
        if self._current_selection is not None:
            self._on_upload_to_youtube_requested(self._current_selection)

    def _build_right_pane(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter = splitter

        self.preview = VideoPreview()
        self.preview.error_occurred.connect(self._on_preview_error)
        # Fullscreen expands THIS window in place (no separate top-level), so
        # window-capture / screen-share follows the video into fullscreen.
        self.preview.set_fullscreen_window_callback(self._apply_video_fullscreen)
        splitter.addWidget(self.preview)

        bottom = QFrame()
        self._bottom_panel = bottom
        bottom.setObjectName("EditorBottom")
        bottom.setStyleSheet(
            f"QFrame#EditorBottom {{ background-color: {_theme.BG_APP}; }}"
        )
        # Panel sizes to content (clip controls + optional scroll + optional
        # bookmark strip). Splitter gives the rest to the preview. Cap so a
        # very tall window doesn't stretch the panel into empty space. The cap
        # has headroom for the bookmark chip strip AND the pan scrollbar both
        # being present at once (a zoomed-in clip with bookmarks);
        # _sync_bottom_panel_height re-apportions the splitter to fit.
        bottom.setMinimumHeight(_BOTTOM_PANEL_BASE_MIN)
        bottom.setMaximumHeight(_BOTTOM_PANEL_MAX)
        bottom_lay = QVBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.setSpacing(0)

        # --- Trim & highlights block ---
        trim_block = QWidget()
        trim_lay = QVBoxLayout(trim_block)
        trim_lay.setContentsMargins(18, 10, 18, 0)
        trim_lay.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        trim_title = QLabel("Trim & highlights")
        trim_title.setStyleSheet(
            f"color: {_theme.TEXT}; font-weight: 700; font-size: 13px;"
        )
        header_row.addWidget(trim_title)
        # Bookmark-count pill: a violet pip (matching the timeline markers) +
        # count. Hidden when the clip has none.
        self._bookmarks_badge = QWidget()
        self._bookmarks_badge.setObjectName("BookmarksBadge")
        self._bookmarks_badge.setStyleSheet(
            "QWidget#BookmarksBadge { background: rgba(167,139,250,0.13); "
            "border: 1px solid rgba(167,139,250,0.30); border-radius: 9px; }"
        )
        _badge_lay = QHBoxLayout(self._bookmarks_badge)
        _badge_lay.setContentsMargins(9, 3, 11, 3)
        _badge_lay.setSpacing(6)
        _badge_pip = QLabel()
        _badge_pip.setFixedSize(8, 8)
        _badge_pip.setStyleSheet(
            f"background: {_theme.ACCENT_VIOLET}; border-radius: 2px;"
        )
        _badge_lay.addWidget(_badge_pip)
        self._bookmarks_badge_label = QLabel("0 bookmarks")
        self._bookmarks_badge_label.setStyleSheet(
            f"color: {_theme.ACCENT_VIOLET}; font-size: 11px; font-weight: 600; "
            "background: transparent;"
        )
        _badge_lay.addWidget(self._bookmarks_badge_label)
        self._bookmarks_badge.setVisible(False)
        header_row.addWidget(self._bookmarks_badge)
        header_row.addStretch(1)
        self._reset_zoom_btn = QToolButton()
        self._reset_zoom_btn.setText("Reset zoom")
        self._reset_zoom_btn.setToolTip("Restore the full clip view")
        self._reset_zoom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_zoom_btn.setStyleSheet(
            f"QToolButton {{ color: {_theme.TEXT_MID}; padding: 5px 10px; "
            f"border-radius: 8px; border: 1px solid {_theme.BORDER}; }}"
            f"QToolButton:hover {{ background: {_theme.BG_HOVER}; color: {_theme.TEXT}; }}"
        )
        self._reset_zoom_btn.setVisible(False)
        header_row.addWidget(self._reset_zoom_btn)
        trim_lay.addLayout(header_row)

        self.timeline = Timeline()
        trim_lay.addWidget(self.timeline)

        # Pan scrollbar — only useful (and visible) when the timeline is
        # zoomed in to a subset of the clip.
        self._timeline_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self._timeline_scroll.setVisible(False)
        self._timeline_scroll.valueChanged.connect(self._on_pan_scroll)
        trim_lay.addWidget(self._timeline_scroll)

        # Bookmark chip strip — hidden when the current clip has no
        # bookmarks. Populated from the bookmark sidecar in
        # ``_on_recording_selected``; each chip seeks the preview when
        # clicked. Same source of truth as the violet pips on the timeline.
        self._bookmarks_panel = QFrame()
        self._bookmarks_panel.setFrameShape(QFrame.Shape.NoFrame)
        bm_lay = QHBoxLayout(self._bookmarks_panel)
        bm_lay.setContentsMargins(0, 2, 0, 2)
        bm_lay.setSpacing(6)
        bm_label = QLabel("Bookmarks:")
        bm_label.setStyleSheet(f"color: {_theme.TEXT_MID}; font-size: 9pt;")
        bm_lay.addWidget(bm_label)
        self._bookmarks_chip_host = QWidget()
        self._bookmarks_chips_layout = QHBoxLayout(self._bookmarks_chip_host)
        self._bookmarks_chips_layout.setContentsMargins(0, 0, 0, 0)
        self._bookmarks_chips_layout.setSpacing(4)
        self._bookmarks_chips_layout.addStretch(1)
        bm_scroll = QScrollArea()
        bm_scroll.setWidgetResizable(True)
        bm_scroll.setFrameShape(QFrame.Shape.NoFrame)
        bm_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        bm_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        bm_scroll.setWidget(self._bookmarks_chip_host)
        bm_scroll.setFixedHeight(30)
        bm_lay.addWidget(bm_scroll, stretch=1)
        self._bookmarks_panel.setVisible(False)
        trim_lay.addWidget(self._bookmarks_panel)

        bottom_lay.addWidget(trim_block)
        bottom_lay.addStretch(1)

        # Thin export-progress bar above the footer — visible only mid-export.
        self._export_progress = QProgressBar()
        self._export_progress.setRange(0, 100)
        self._export_progress.setValue(0)
        self._export_progress.setTextVisible(True)
        self._export_progress.setFormat("")
        self._export_progress.setFixedHeight(6)
        self._export_progress.setVisible(False)
        bottom_lay.addWidget(self._export_progress)

        # --- Export / Upload footer bar ---
        export_bar = QFrame()
        export_bar.setObjectName("ExportBar")
        export_bar.setStyleSheet(
            f"QFrame#ExportBar {{ background-color: {_theme.BG_FOOTER}; "
            f"border-top: 1px solid {_theme.BORDER_HAIRLINE}; }}"
            "QFrame#ExportBar QLabel { background: transparent; }"
        )
        export_row = QHBoxLayout(export_bar)
        export_row.setContentsMargins(18, 9, 18, 9)
        export_row.setSpacing(14)

        # Clip length readout (eyebrow + big tabular value).
        len_col = QVBoxLayout()
        len_col.setSpacing(0)
        len_eyebrow = QLabel("CLIP LENGTH")
        len_eyebrow.setStyleSheet(
            f"color: {_theme.TEXT_FAINT}; font-size: 10px; font-weight: 700; "
            "letter-spacing: 1px;"
        )
        len_col.addWidget(len_eyebrow)
        self._clip_length_label = QLabel("0:00")
        self._clip_length_label.setStyleSheet(
            "color: #ffffff; font-size: 18px; font-weight: 800;"
        )
        len_col.addWidget(self._clip_length_label)
        export_row.addLayout(len_col)

        divider = QFrame()
        divider.setFixedSize(1, 32)
        divider.setStyleSheet(f"background: {_theme.BORDER};")
        export_row.addWidget(divider)

        self._export_btn = self._make_action_button("Export Clip", "download")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export_clicked)
        export_row.addWidget(self._export_btn)

        self._set_start_btn = self._make_action_button("Set start here", None)
        self._set_start_btn.setEnabled(False)
        self._set_start_btn.setToolTip(
            "Move the clip's start handle to the playhead — the moment you're "
            "watching right now — without replaying or re-dragging."
        )
        self._set_start_btn.clicked.connect(self._on_set_start_here_clicked)
        export_row.addWidget(self._set_start_btn)

        self._play_clip_btn = self._make_action_button("Play clip portion", None)
        self._play_clip_btn.setEnabled(False)
        self._play_clip_btn.setToolTip(
            "Play just the section between the trim handles so you can preview "
            "the exported clip before saving."
        )
        self._play_clip_btn.clicked.connect(self._on_play_clip_clicked)
        export_row.addWidget(self._play_clip_btn)

        export_row.addStretch(1)

        self._open_location_btn = self._make_action_button("Open file location", "folder")
        self._open_location_btn.setEnabled(False)
        self._open_location_btn.clicked.connect(self._on_open_location_clicked)
        export_row.addWidget(self._open_location_btn)

        self._upload_btn = self._make_action_button("Upload to YouTube", "youtube", primary=True)
        self._upload_btn.setEnabled(False)
        self._upload_btn.setAccessibleDescription(
            "Upload the selected recording or open YouTube setup"
        )
        self._upload_btn.clicked.connect(self._on_upload_clicked)
        export_row.addWidget(self._upload_btn)

        bottom_lay.addWidget(export_bar)

        splitter.addWidget(bottom)

        # Wire selection -> preview + timeline.
        self.selected_changed.connect(self._on_recording_selected)
        # Preview events update timeline.
        self.preview.duration_changed.connect(self._on_duration_changed)
        self.preview.position_changed.connect(self.timeline.set_playhead)
        # Dragging a handle seeks the preview and updates the clip-length label.
        self.timeline.seek_requested.connect(self.preview.seek)
        # Clicking a bookmark marker jumps there AND plays.
        self.timeline.bookmark_clicked.connect(self._jump_to_bookmark)
        self.timeline.start_changed.connect(lambda _v: self._update_clip_length_label())
        self.timeline.end_changed.connect(lambda _v: self._update_clip_length_label())
        self.timeline.view_changed.connect(self._on_timeline_view_changed)
        self._reset_zoom_btn.clicked.connect(self.timeline.reset_zoom)

        # Export-task state.
        self._trim_thread: QThread | None = None
        self._trim_worker: TrimWorker | None = None
        self._trimming_paths: set[str] = set()
        self._trim_input_key: str | None = None
        self._trim_activity = None
        self._app_quitting = False

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        return splitter

    def _on_preview_error(self, message: str) -> None:
        # Non-blocking — the placeholder/preview itself is empty on error.
        self._status.setText(f"Preview error: {message}")

    # ----------------------------------------------------- trim/export
    def _on_recording_selected(self, path) -> None:
        if path and Path(path) != self._parked_preview_path:
            self._parked_preview_path = None
            self._parked_preview_position = 0.0
        self._current_selection = path if path else None
        # Any deferred right-click export belongs to the previous selection.
        # (The export-from-list path re-arms this AFTER select_by_path runs.)
        self._pending_export_path = None
        # Load into the (heavy) video preview only when the editor is on
        # screen. The tray idle-prebuild selects a row while the window is
        # still hidden; loading then would keep a multi-hour video decoder in
        # RAM for a window nobody opened. showEvent loads it on first open.
        self._sync_preview_to_visibility()
        # Reset the timeline; duration arrives via the preview's signal.
        self.timeline.set_duration(0.0)
        # Pull any bookmarks recorded during this session — the timeline
        # renders them as ticks; no need to spam the status bar with a count.
        if path:
            try:
                marks = load_bookmarks(Path(path))
            except Exception:
                logger.exception("Could not read bookmarks for %s", path)
                marks = []
            # Make sure the thumbnail is present (re-extract if missing).
            p_obj = Path(path)
            if not thumb_is_fresh(p_obj):
                self._kick_thumbnail(p_obj)
            # Kick off an ffprobe duration scan in parallel with QMediaPlayer's
            # own metadata read. QMediaPlayer reports duration=0 forever for
            # MKVs whose segment header was never finalised (recording killed
            # mid-write); ffprobe handles that case correctly. Whichever
            # arrives with a real duration first wins.
            self._probing_path = str(p_obj)
            probe_duration_async(p_obj, self._on_duration_probe_done)
            # Player overlay: clip title + game subline (only when it adds
            # info, i.e. the file was renamed away from its game name) +
            # highlight markers (sourced from this clip's bookmarks).
            title = friendly_recording_title(p_obj.name)
            slug = self._game_slug_for(p_obj)
            game = humanise_game_name(slug + ".exe") if slug else None
            subline = game if (game and game.lower() != title.lower()) else ""
            self.preview.set_clip_meta(title, subline)
            self.preview.set_highlights(marks)
        else:
            marks = []
            self._probing_path = None
            self.preview.set_clip_meta("", "")
            self.preview.set_highlights([])
        self.timeline.set_bookmarks(marks)
        self._populate_bookmark_chips(marks)
        self._export_btn.setEnabled(False)
        # The duration-gated controls belong to the PREVIOUS clip until the
        # new one's duration arrives — disable them and zero the readout so a
        # clip whose duration never lands (broken metadata) can't be driven
        # with stale state.
        self._play_clip_btn.setEnabled(False)
        self._set_start_btn.setEnabled(False)
        self._update_clip_length_label()
        has_selection = path is not None
        self._open_location_btn.setEnabled(has_selection)
        self._upload_btn.setEnabled(has_selection)
        self._export_progress.setValue(0)
        self._export_progress.setFormat("")
        self._export_progress.setVisible(False)

    def _sync_preview_to_visibility(self) -> None:
        """Keep the video preview loaded only while the editor is on screen.

        Visible + a recording selected → load it (if not already loaded).
        Hidden (closed to tray, or prebuilt-but-never-shown) → unload, which
        releases the QMediaPlayer/WMF decoder + frame buffers. The editor
        window itself stays prebuilt either way, so opening it is still
        instant; only the clip's first frame fills in a moment later, exactly
        like selecting any recording.
        """
        loaded = self.preview.current_path()
        if self.isVisible():
            if self._current_selection and loaded != self._current_selection:
                if is_repairing(self._current_selection):
                    # A QMediaPlayer/WMF load holds the file open indefinitely,
                    # which would exhaust the repair swap's bounded retry
                    # budget and fail the repair. Repairs take seconds — check
                    # back shortly instead of loading now.
                    QTimer.singleShot(1500, self._sync_preview_to_visibility)
                    return
                restore_position = (
                    self._parked_preview_position
                    if self._parked_preview_path == self._current_selection
                    else 0.0
                )
                self.preview.load(self._current_selection)
                # preview.load() zeroed the duration hint, and this reload path
                # does NOT go through _on_recording_selected (the selection didn't
                # change) — so a broken-metadata clip (QMediaPlayer duration stuck
                # at 0) would otherwise reopen with its trim UI locked at 0:00
                # until the user re-selects it. Re-apply the cached ffprobe hint
                # synchronously (no fresh async probe → no teardown race). Healthy
                # files have no hint and get their real duration from QMediaPlayer.
                hint = self._duration_hint_cache.get(str(self._current_selection))
                if hint and hint > 0:
                    self.preview.set_duration_hint_seconds(hint)
                if restore_position > 0:
                    self._restore_parked_preview_position(
                        self._current_selection, restore_position
                    )
            elif not self._current_selection and loaded is not None:
                self.preview.load(None)
        elif loaded is not None:
            self._remember_preview_position()
            self.preview.load(None)

    def _remember_preview_position(self) -> None:
        path = self.preview.current_path()
        if path is None:
            return
        self._parked_preview_path = path
        self._parked_preview_position = max(0.0, self.preview.position())

    def _restore_parked_preview_position(
        self, path: Path, seconds: float, attempts: int = 0
    ) -> None:
        def _seek() -> None:
            if self.preview.current_path() != path:
                return
            duration = self.preview.duration()
            target = max(0.0, seconds)
            if duration > 0:
                target = min(target, max(0.0, duration - 0.05))
            if attempts > 0:
                current = max(0.0, self.preview.position())
                if abs(current - target) < 0.5:
                    return
                if current > 1.0 and abs(current - seconds) > 1.0:
                    return
            self.preview.seek(target)
            if attempts < 2 and self.preview.current_path() == path:
                QTimer.singleShot(
                    250,
                    lambda: self._restore_parked_preview_position(
                        path, seconds, attempts + 1
                    ),
                )

        QTimer.singleShot(0, _seek)

    def _populate_bookmark_chips(self, marks: list[float]) -> None:
        """Rebuild the bookmark chip strip from ``marks``. Hide the panel
        when there are none."""
        layout = self._bookmarks_chips_layout
        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        n = len(marks)
        self._bookmarks_badge_label.setText(f"{n} bookmark{'' if n == 1 else 's'}")
        self._bookmarks_badge.setVisible(n > 0)
        if not marks:
            self._bookmarks_panel.setVisible(False)
            self._sync_bottom_panel_height()
            return
        # Bookmark chips read accent at build time so a future theme
        # swap carries through without touching this code.
        from PyQt6.QtGui import QColor as _QColor
        accent = _QColor(_theme.ACCENT)
        chip_bg = _QColor.fromHslF(
            accent.hueF(),
            max(0.0, accent.saturationF() * 0.55),
            0.22,
        ).name()
        chip_hover = _QColor.fromHslF(
            accent.hueF(),
            max(0.0, accent.saturationF() * 0.55),
            0.30,
        ).name()
        chip_css = (
            f"QPushButton {{ background: {chip_bg}; color: #e6e8ee; "
            f"border: 1px solid {_theme.ACCENT}; border-radius: 10px; "
            f"padding: 1px 10px; font-size: 9pt; }}"
            f"QPushButton:hover {{ background: {chip_hover}; }}"
        )
        for t in sorted(marks):
            chip = QPushButton(fmt_time(t))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(chip_css)
            chip.setToolTip(f"Jump to {fmt_time(t)}")
            seconds = float(t)
            chip.clicked.connect(lambda _checked=False, s=seconds: self._jump_to_bookmark(s))
            layout.addWidget(chip)
        layout.addStretch(1)
        self._bookmarks_panel.setVisible(True)
        self._sync_bottom_panel_height()

    def _sync_bottom_panel_height(self) -> None:
        """Reserve enough height for the clip/timeline panel's current contents.

        The panel shares a vertical splitter with the video preview. Without
        this, the splitter freezes the panel at the height it had when first
        laid out — and the tray prebuilds the editor HIDDEN and selects the
        first clip while hidden, so any size set then is wrong/ignored — so the
        conditionally-shown bookmark chip strip (and the pan scrollbar when
        zoomed) overflows UPWARD into the timeline, dropping the chips on top of
        the under-handle time labels.

        Driving the panel's *minimum height* (rather than a one-shot
        ``setSizes``) is robust to that timing: a splitter honours a child's
        minimum on every layout pass, including after the window is finally
        shown. The minimum tracks the panel's own content sizeHint (which is
        valid even while hidden), so the strip always gets a row below the
        timeline and the space is given back when a clip has no bookmarks.
        """
        def _apply() -> None:
            splitter = getattr(self, "_right_splitter", None)
            bottom = getattr(self, "_bottom_panel", None)
            if bottom is None:
                return
            lay = bottom.layout()
            if lay is not None:
                lay.activate()
                content = lay.sizeHint().height()
            else:
                content = bottom.sizeHint().height()
            # Clamp to the panel's hard cap; floor at the no-bookmarks baseline
            # so we never reserve LESS than the original design height.
            content = max(_BOTTOM_PANEL_BASE_MIN, min(bottom.maximumHeight(), content))
            if bottom.minimumHeight() != content:
                bottom.setMinimumHeight(content)
            # Nudge the splitter so the new minimum takes effect immediately
            # when the window is already visible (otherwise it only applies on
            # the next layout pass).
            if splitter is not None:
                total = splitter.height()
                if total > content:
                    splitter.setSizes([total - content, content])

        QTimer.singleShot(0, _apply)

    def _on_duration_probe_done(self, path_str: str, seconds: float) -> None:
        """Apply (or note the absence of) ffprobe-derived duration."""
        if path_str != self._probing_path:
            # User selected a different clip while the probe was in flight.
            return
        if seconds > 0:
            # Feed the hint to the preview; it'll override its internal
            # scrubber range if QMediaPlayer is still stuck at 0. Cache it so a
            # tray-park reopen can re-apply it without re-probing.
            self._duration_hint_cache[path_str] = seconds
            self.preview.set_duration_hint_seconds(seconds)
            return
        # Broken metadata: the recording was killed before encoder.stop().
        # The preview stays gated until the user runs Repair from the
        # right-click menu (or deletes the recording).
        self._status.setText(
            "This recording's duration metadata is missing — likely killed "
            "mid-record. Right-click the clip and choose “Repair recording” "
            "to fix."
        )

    def _on_duration_changed(self, seconds: float) -> None:
        self.timeline.set_duration(seconds)
        self._update_clip_length_label()
        # Only allow export once we know the duration and a trim task isn't running.
        export_ready = seconds > 0 and self._trim_thread is None
        self._export_btn.setEnabled(export_ready)
        self._play_clip_btn.setEnabled(seconds > 0)
        self._set_start_btn.setEnabled(seconds > 0)
        # A right-click Export on a not-yet-selected row waits here for the
        # freshly-loaded clip's duration before opening the export prompt.
        pending = getattr(self, "_pending_export_path", None)
        if pending is not None and seconds > 0:
            self._pending_export_path = None
            current = self._current_selection
            if current is not None and Path(current) == pending:
                self._on_export_clicked()

    def _on_set_start_here_clicked(self) -> None:
        """Jump the START trim handle to the current playhead position.

        Pure handle move: no seek, no playback change — the user keeps
        watching from exactly where they are. If the playhead sits at or past
        the end handle, the start is clamped to just before it (same 0.05 s
        invariant as dragging).
        """
        if self.timeline.duration <= 0:
            return
        pos = self.preview.position()
        end = self.timeline.end_seconds
        start = max(0.0, min(pos, end - 0.05))
        self.timeline.set_clip_range(start, end)

    def _on_play_clip_clicked(self) -> None:
        start = self.timeline.start_seconds
        end = self.timeline.end_seconds
        if end - start < 0.05:
            self._status.setText(
                "Drag the trim handles first — the selected portion is empty."
            )
            return
        self.preview.play_range(start, end)

    def _on_timeline_view_changed(self, view_start: float, view_end: float) -> None:
        """Sync the pan scrollbar to the timeline's zoom state."""
        duration = self.timeline.duration
        bar = self._timeline_scroll
        if duration <= 0:
            bar.setVisible(False)
            self._reset_zoom_btn.setVisible(False)
            return
        view_range = view_end - view_start
        zoomed_in = view_range < duration - 1e-3
        if bar.isVisible() != zoomed_in:
            # Scrollbar appearing/disappearing changes the panel's content
            # height — refit so it never overlaps the timeline labels.
            bar.setVisible(zoomed_in)
            self._sync_bottom_panel_height()
        self._reset_zoom_btn.setVisible(zoomed_in)
        if not zoomed_in:
            return
        # Encode (start, range) in millisecond units — QScrollBar wants ints
        # and ms gives plenty of precision for hours-long clips.
        max_start_ms = int(round((duration - view_range) * 1000))
        bar.blockSignals(True)
        try:
            bar.setRange(0, max_start_ms)
            bar.setPageStep(int(round(view_range * 1000)))
            bar.setSingleStep(max(1, int(round(view_range * 100))))  # ~10% step
            bar.setValue(int(round(view_start * 1000)))
        finally:
            bar.blockSignals(False)

    def _on_pan_scroll(self, value_ms: int) -> None:
        view_range = self.timeline.view_end - self.timeline.view_start
        if view_range <= 0:
            return
        new_start = value_ms / 1000.0
        self.timeline.set_view(new_start, new_start + view_range)

    def _update_clip_length_label(self) -> None:
        """Refresh the CLIP LENGTH readout from the timeline's current handles."""
        if self.timeline.duration <= 0:
            self._clip_length_label.setText("0:00")
            return
        length = max(0.0, self.timeline.end_seconds - self.timeline.start_seconds)
        self._clip_length_label.setText(fmt_time(length))

    def _on_export_clicked(self) -> None:
        path = getattr(self, "_current_selection", None)
        if path is None:
            return
        start = self.timeline.start_seconds
        end = self.timeline.end_seconds
        if end - start < 0.05:
            QMessageBox.warning(
                self, "Momento",
                "Selected clip is too short. Drag the handles to define a range first.",
            )
            return
        try:
            suggested = next_clip_path(Path(path)).stem
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(self, "Export clip", f"Could not prepare a clip filename:\n{exc}")
            return
        chosen, ok = QInputDialog.getText(
            self, "Export clip", "Save clip as (without .mp4):",
            QLineEdit.EchoMode.Normal, suggested,
        )
        if not ok:
            return
        chosen = chosen.strip()
        if not chosen:
            QMessageBox.warning(self, "Momento", "Clip name cannot be empty.")
            return
        try:
            output = _resolve_output_path(Path(path).parent, chosen)
        except (OSError, RuntimeError, ValueError) as exc:
            QMessageBox.warning(
                self, "Export clip",
                "Could not prepare the clip's output file. Check that the output "
                f"folder is available and writable.\n\n{exc}",
            )
            return
        self._launch_trim(Path(path), start, end, output)

    def _launch_trim(
        self, input_path: Path, start: float, end: float, output: Path
    ) -> None:
        """Spin up the TrimWorker / QThread pair for a trim export."""
        if self._trim_thread is not None:
            self._status.setText("An export is already in progress — wait for it to finish.")
            return
        if is_repairing(input_path):
            self._status.setText(
                f"Can't export {input_path.name} while it is being repaired."
            )
            return
        worker = TrimWorker(input_path, start, end, output)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_trim_progress)
        worker.done.connect(self._on_trim_done)
        worker.failed.connect(self._on_trim_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_trim_thread_finished)

        self._trim_thread = thread
        self._trim_worker = worker
        self._trim_input_key = self._file_key(input_path)
        self._trimming_paths.add(self._trim_input_key)
        self._trim_activity = begin_file_activity(input_path, output)
        self._export_btn.setEnabled(False)
        self._export_progress.setVisible(True)
        self._export_progress.setValue(0)
        self._export_progress.setFormat("Exporting… 0%")
        self._status.setText(f"Exporting {output.name}…")
        try:
            thread.start()
        except Exception:
            self._trim_activity.release()
            self._trim_activity = None
            self._trimming_paths.discard(self._trim_input_key)
            self._trim_input_key = None
            self._trim_thread = None
            self._trim_worker = None
            raise

    def _on_play_requested(self, path: Path) -> None:
        """Right-click → Play. Select the row, seek to 0, start playback."""
        if path is None:
            return
        if self._current_selection != path:
            self._list.select_by_path(path)
        self.preview.seek(0.0)
        self.preview.play()

    def _on_export_requested_from_list(self, path: Path) -> None:
        """Right-click → Export clip. Make the row the current selection
        (which loads the preview), then trigger the standard export prompt."""
        if path is None:
            return
        if self._current_selection != path:
            self._list.select_by_path(path)
        if self.timeline.duration <= 0:
            # The clip's duration arrives asynchronously (QMediaPlayer
            # metadata / ffprobe) — this covers both a just-selected row AND
            # an already-selected row still loading. Opening the export
            # prompt now would see start == end == 0 and always bounce with
            # "too short" — defer until the duration lands
            # (_on_duration_changed consumes this).
            self._pending_export_path = Path(path)
            return
        self._on_export_clicked()

    def _on_upload_to_youtube_requested(self, path: Path) -> None:
        """Right-click → Upload to YouTube. Gate on connection state, then
        open the upload dialog → progress dialog flow."""
        if path is None or not Path(path).is_file():
            return

        # Local imports so the YouTube package isn't pulled in just for app
        # startup — and so a missing google-api-python-client install never
        # breaks the editor at launch.
        from momento.youtube import auth as yt_auth
        from momento.youtube import client_config

        try:
            active_client = client_config.load_active_client_config()
        except client_config.OAuthClientConfigError:
            QMessageBox.warning(
                self,
                "YouTube setup needs attention",
                "Momento can't read the saved Google OAuth setup. Open Settings "
                "to replace or remove it.",
            )
            self.show_settings("YouTube")
            return
        if active_client is None:
            QMessageBox.information(
                self,
                "Set up YouTube uploads",
                "Import a Desktop OAuth JSON from your own Google Cloud project. "
                "Momento will guide you through the steps.",
            )
            self.show_settings("YouTube")
            return

        # 1. Are we connected?
        if not yt_auth.is_connected():
            reply = QMessageBox.question(
                self,
                "Connect YouTube account",
                "Momento isn't connected to a YouTube account yet.\n\n"
                "Open Settings → YouTube to sign in?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.show_settings("YouTube")
            return

        # Keep the gate closed until Qt has consumed the queued result. The
        # Python thread can finish just before its signal is dispatched.
        if self._youtube_auth_bridge is not None:
            self._status.setText("Already checking the YouTube connection")
            return

        # Loading credentials may refresh an expired token over the network.
        # Keep that request off Qt's GUI thread so the editor remains responsive.
        bridge = _YouTubeCredentialsBridge()
        bridge.config_epoch = self._youtube_config_epoch
        bridge.completed.connect(self._on_youtube_credentials_ready)
        self._youtube_auth_bridge = bridge
        self._youtube_upload_path = Path(path)
        self._status.setText("Checking YouTube connection…")

        def load_credentials() -> None:
            try:
                result = yt_auth.get_authorized_credentials()
                bridge.completed.emit(result, None)
            except Exception as exc:  # defensive: auth normally returns None
                logger.exception("Could not load YouTube credentials")
                bridge.completed.emit(None, exc)

        thread = threading.Thread(
            target=load_credentials,
            name="YouTubeCredentials",
            daemon=True,
        )
        thread.start()

    def _on_youtube_credentials_ready(self, creds, error) -> None:
        path = self._youtube_upload_path
        bridge = self._youtube_auth_bridge
        self._youtube_auth_bridge = None
        self._youtube_upload_path = None
        if bridge is not None:
            bridge.deleteLater()

        if (
            bridge is None
            or getattr(bridge, "config_epoch", -1) != self._youtube_config_epoch
        ):
            self._status.setText("YouTube setup changed; start the upload again")
            return

        if error is not None:
            self._status.setText("Could not check the YouTube connection")
        else:
            self._status.setText("YouTube connection checked")
        if creds is None:
            QMessageBox.warning(
                self,
                "YouTube re-auth needed",
                "Your saved YouTube sign-in is no longer valid (it may have "
                "been revoked, or the refresh failed).\n\n"
                "Open Settings → YouTube and click Connect again.",
            )
            return

        from momento.youtube import auth as yt_auth

        if not yt_auth.credentials_match_active_client(creds):
            self._status.setText("YouTube setup changed; start the upload again")
            QMessageBox.information(
                self,
                "YouTube setup changed",
                "The Google OAuth setup changed while Momento checked your sign-in. "
                "Start the upload again with the current setup.",
            )
            return

        if path is None or not path.is_file():
            QMessageBox.warning(
                self,
                "Recording unavailable",
                "That recording is no longer available to upload.",
            )
            return

        self._show_youtube_upload_dialog(path, creds)

    def _on_youtube_configuration_changed(self) -> None:
        """Invalidate credentials being refreshed under an older client."""
        self._youtube_config_epoch += 1
        if self._youtube_auth_bridge is not None:
            self._status.setText("YouTube setup changed; waiting for the old check to finish")

    def _show_youtube_upload_dialog(self, path: Path, creds) -> None:
        from momento.config import save_config
        from momento.ui.youtube_upload_dialog import YouTubeUploadDialog
        from momento.ui.youtube_upload_progress import YouTubeUploadProgressDialog

        # 3. Collect upload metadata.
        dlg = YouTubeUploadDialog(
            clip_path=Path(path),
            config=self._config,
            channel_name=self._config.youtube_channel_name,
            parent=self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # Persist updated defaults (privacy / category / tags) so the next
        # upload starts from the user's last preferences.
        try:
            self._config = dlg.updated_config_defaults()
            save_config(self._config)
        except OSError:
            logger.exception("Could not save updated YouTube defaults")

        # 4. Run the upload with a progress dialog.
        options = dlg.get_options()
        progress = YouTubeUploadProgressDialog(creds, options, parent=self)
        progress.exec()

    def _on_trim_progress(self, current: float, total: float) -> None:
        pct = 0 if total <= 0 else int(min(100, max(0, round(current / total * 100))))
        self._export_progress.setValue(pct)
        self._export_progress.setFormat(f"Exporting… {pct}%")

    def _on_trim_done(self, output_path: str) -> None:
        self._export_progress.setValue(100)
        self._export_progress.setFormat("Done")
        name = Path(output_path).name
        if not mark_recording_owned(output_path):
            logger.warning("Could not mark exported clip as Momento-owned: %s", output_path)
        self._status.setText(f"Exported {name}")
        # Pull the new clip into the list immediately — preserving the
        # selection so the recording the user is trimming (and their handle
        # positions / playback) isn't yanked to row 0 by the rebuild.
        self.refresh(preserve_selection=True)

    def _on_trim_failed(self, message: str) -> None:
        self._export_progress.setValue(0)
        self._export_progress.setFormat("Failed")
        self._status.setText(f"Export failed: {message}")
        if self._app_quitting and message == "Cancelled":
            return
        QMessageBox.warning(self, "Momento", f"Export failed:\n{message}")

    def _on_trim_thread_finished(self) -> None:
        activity = getattr(self, "_trim_activity", None)
        if activity is not None:
            activity.release()
        self._trim_activity = None
        if self._trim_input_key is not None:
            self._trimming_paths.discard(self._trim_input_key)
        self._trim_input_key = None
        self._trim_thread = None
        self._trim_worker = None
        ready = self.preview.duration() > 0
        self._export_btn.setEnabled(ready)
        # Tuck the thin progress bar away again shortly after the export ends.
        QTimer.singleShot(1600, lambda: self._export_progress.setVisible(False))

    # ---------------------------------------------------------- rows
    def _add_item(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        existing_thumb = thumb_path_for(path)
        thumb = str(existing_thumb) if thumb_is_fresh(path) else None
        # Seed the duration from the cache so a list REBUILD keeps the badge.
        # The metadata probe is skipped for already-probed paths (see
        # _kick_metadata_probe), so without this seed a rebuilt row — e.g. the
        # tray re-lists the prebuilt editor when it's opened — would lose its
        # duration badge until something forced a re-probe. Thumbnails already
        # survive a rebuild because they're re-read from the on-disk cache;
        # this gives the duration the same treatment via _duration_cache.
        cached_duration = self._duration_cache.get(str(path))
        slug = self._game_slug_for(path)
        game_name = humanise_game_name(slug + ".exe") if slug else None
        self._list.add_item(
            path=path,
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            duration_secs=cached_duration,
            thumb_path=thumb,
            game_name=game_name,
        )
        self._kick_metadata_probe(path)
        if thumb is None:
            self._kick_thumbnail(path)

    def _kick_metadata_probe(self, path: Path) -> None:
        key = str(path)
        if key in self._game_slug_cache:
            return  # already probed or in-flight (None sentinel)
        # Don't open a file whose repair is mid-flight — the ffprobe read
        # handle would race the repair's atomic swap. Skip WITHOUT caching so
        # the next refresh (the tray re-lists on repair completion) re-probes
        # the now-finalised file.
        if is_repairing(path):
            return
        self._game_slug_cache[key] = None
        probe_metadata_async(path, self._on_metadata_probed)

    def _on_metadata_probed(self, path_str: str, duration: float, slug: str) -> None:
        if duration > 0:
            self._list.update_duration(Path(path_str), duration)
            self._duration_cache[path_str] = duration
        prior = self._game_slug_cache.get(path_str)
        self._game_slug_cache[path_str] = slug
        if slug and not mark_recording_owned(path_str):
            logger.warning("Could not mark recording as Momento-owned: %s", path_str)
        # Push the resolved game name onto the card so the secondary line lights
        # up once the embedded MOMENTO_GAME tag is read (renamed files survive).
        if slug:
            self._list.update_game(Path(path_str), humanise_game_name(slug + ".exe"))
        # Sort by "Longest" depends on duration data that arrived async —
        # schedule a re-sort once probes have populated the cache.
        need_resort = self._sort_mode == "longest" and duration > 0
        # Only schedule a rebuild when the embedded slug differs from what
        # we'd already be showing (filename-derived). Coalesce many probes
        # in the same event-loop turn into one rebuild via singleShot(0).
        slug_changed = (
            slug
            and slug != prior
            and slug != game_slug_from_filename(Path(path_str).name)
        )
        if (need_resort or slug_changed) and not self._filter_rebuild_pending:
            self._filter_rebuild_pending = True
            QTimer.singleShot(0, self._flush_pending_filter_rebuild)

    def _flush_pending_filter_rebuild(self) -> None:
        """Apply queued probe-driven updates: combo rebuild + list re-sort."""
        self._filter_rebuild_pending = False
        self._rebuild_game_filter()
        # If sort depends on async-arriving data (currently just Longest),
        # re-render the list so the new durations affect row order.
        if self._sort_mode == "longest":
            self._reapply_filter()

    # ------------------------------------------------------- thumbnails
    def _kick_thumbnail(self, path: Path) -> None:
        key = str(path)
        if key in self._thumb_submitted:
            return
        # As with the metadata probe: a thumbnail extraction opens the file,
        # so skip (without marking submitted) while a repair is in flight.
        if is_repairing(path):
            return
        self._thumb_submitted.add(key)
        extract_async(path, self._on_thumb_done)

    def _on_thumb_done(self, path: str, thumb_path: str) -> None:
        if thumb_path:
            self._list.update_thumbnail(Path(path), thumb_path)
        else:
            # Allow a future re-attempt (e.g. on a manual refresh).
            self._thumb_submitted.discard(path)

    # --------------------------------------------------------- events
    def _on_list_selection_changed(self, path) -> None:
        self.selected_changed.emit(path)

    def _on_delete_requested(self, paths: list[Path]) -> None:
        # The signal may arrive with a single Path (older code path) or a list
        # — normalise.
        if isinstance(paths, Path):
            paths = [paths]
        paths = [p for p in paths if p is not None]
        if not paths:
            return

        # Filter out any that vanished already (and quietly drop their rows).
        existing: list[Path] = []
        for p in paths:
            if p.exists():
                existing.append(p)
            else:
                self._list.remove_path(p)
        if not existing:
            return

        busy = [p for p in existing if self._is_file_busy(p)]
        if busy:
            names = ", ".join(p.name for p in busy[:3])
            if len(busy) > 3:
                names += f" (+{len(busy) - 3} more)"
            self._status.setText(
                f"Can't delete {names} while an export or repair is in progress."
            )
            return

        # Build a confirmation message that doesn't grow unbounded for huge
        # selections — show up to 6 names then "(+N more)".
        SHOW = 6
        names = "\n".join(f"    {p.name}" for p in existing[:SHOW])
        if len(existing) > SHOW:
            names += f"\n    (+{len(existing) - SHOW} more)"
        title = (
            f"Delete {len(existing)} recordings?" if len(existing) > 1
            else "Delete recording?"
        )
        body = (
            f"Permanently delete the following from\n\n    {existing[0].parent}\n\n"
            f"{names}\n\n"
            "This also removes their thumbnails and bookmark sidecars. Files "
            "are deleted from disk and cannot be recovered from inside Momento."
        )
        reply = QMessageBox.question(
            self, title, body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Free preview handle if it points at any of the doomed files —
        # Windows holds an exclusive lock while QMediaPlayer has it open.
        if self._current_selection in existing:
            self.preview.load(None)
            self._current_selection = None

        deleted: list[Path] = []
        errors: list[str] = []
        for p in existing:
            # The confirmation dialog runs a nested event loop. An export or
            # repair may have started since the pre-dialog activity check.
            if self._is_file_busy(p):
                errors.append(f"{p.name}: the file is now in use")
                continue
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                # Keep bookmarks and ownership bound to any video Windows
                # refused to delete (for example, an external player's lock).
                errors.append(f"{p.name}: {e}")
                continue
            deleted.append(p)
            self._game_slug_cache.pop(str(p), None)
            self._duration_cache.pop(str(p), None)
            self._duration_hint_cache.pop(str(p), None)
            for target in (
                thumb_path_for(p),
                bookmark_sidecar(p),
                ownership_sidecar_path(p),
            ):
                try:
                    Path(target).unlink(missing_ok=True)
                except OSError as e:
                    errors.append(f"{target.name}: {e}")

        if errors:
            QMessageBox.warning(
                self, "Momento",
                "Some files could not be deleted:\n\n" + "\n".join(errors[:20])
                + ("\n\n…and more" if len(errors) > 20 else ""),
            )

        # Re-scan; this updates the filter combo counts naturally.
        self.refresh()
        if len(deleted) == 1:
            self._status.setText(f"Deleted {deleted[0].name}")
        else:
            self._status.setText(f"Deleted {len(deleted)} recording(s)")

    def _on_reveal_in_explorer(self, path: Path) -> None:
        """Open Windows Explorer with the recording highlighted.

        Uses ``explorer /select,<path>`` which opens the containing folder
        AND selects the file so the user sees exactly which one. Falls
        back to opening the parent folder if /select fails.
        """
        if not isinstance(path, Path) or not path.exists():
            self._status.setText(f"File missing: {Path(path).name if path else '?'}")
            return
        try:
            subprocess.Popen(
                ["explorer", f"/select,{str(path)}"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            logger.exception("Could not show %s in Explorer", path)
            try:
                os.startfile(path.parent)
            except OSError as e:
                self._status.setText(f"Couldn't open folder: {e}")

    def _on_rename_requested(self, path: Path) -> None:
        """Rename a recording (+ its bookmark / thumb sidecars).

        Preserves the file extension. Refuses if the target name already
        exists. If the file is the currently-loaded preview, unload it
        first because Windows holds an exclusive lock on the playing file.
        """
        if not isinstance(path, Path) or not path.exists():
            self._status.setText("File missing — refresh.")
            return
        if self._is_file_busy(path):
            self._status.setText(
                f"Can't rename {path.name} while an export or repair is in progress."
            )
            return
        current_stem = path.stem
        new_stem, ok = QInputDialog.getText(
            self, "Rename recording",
            f"New name (extension stays {path.suffix}):",
            QLineEdit.EchoMode.Normal,
            current_stem,
        )
        if not ok:
            return
        new_stem = new_stem.strip()
        if not new_stem or new_stem == current_stem:
            return
        # Sanitize using the same rules as clip export.
        cleaned = _INVALID_FS_CHARS.sub("_", new_stem).strip().rstrip(".")
        if not cleaned:
            QMessageBox.warning(self, "Momento", "Name is empty after removing invalid characters.")
            return
        new_path = path.with_name(cleaned + path.suffix)
        if self._is_file_busy(path) or self._is_file_busy(new_path):
            self._status.setText("Can't rename this recording while its files are in use.")
            return
        # Reject orphan destination sidecars too: otherwise the renamed video
        # could silently inherit another recording's bookmarks or thumbnail.
        targets = (
            new_path, thumb_path_for(new_path), bookmark_sidecar(new_path),
            ownership_sidecar_path(new_path),
        )
        collision = next((target for target in targets if target.exists()), None)
        if collision is not None:
            QMessageBox.warning(
                self, "Momento",
                f"A file named {collision.name!r} already exists in this folder.",
            )
            return

        # If we're previewing this clip, release the file handle first.
        was_loaded = self._current_selection == path
        if was_loaded:
            self.preview.load(None)
            self._current_selection = None

        # Move main file + sidecars. The bookmark sidecar's name is built
        # from the FULL filename (including extension), so it follows the
        # new name + same extension.
        moves: list[tuple[Path, Path]] = [(path, new_path)]
        old_thumb = thumb_path_for(path)
        if old_thumb.exists():
            moves.append((old_thumb, thumb_path_for(new_path)))
        old_bm = bookmark_sidecar(path)
        if old_bm.exists():
            moves.append((old_bm, bookmark_sidecar(new_path)))
        old_owner = ownership_sidecar_path(path)
        if old_owner.exists():
            moves.append((old_owner, ownership_sidecar_path(new_path)))

        completed_moves: list[tuple[Path, Path]] = []
        try:
            for src, dst in moves:
                src.rename(dst)
                completed_moves.append((src, dst))
        except OSError as e:
            rollback_errors: list[str] = []
            for src, dst in reversed(completed_moves):
                try:
                    dst.rename(src)
                except OSError as rollback_error:
                    rollback_errors.append(f"{dst.name}: {rollback_error}")
            detail = "The original filenames were restored."
            if rollback_errors:
                detail = (
                    "Some files could not be restored to their original names. "
                    "Keep both sets of files together in this folder:\n"
                    + "\n".join(rollback_errors)
                )
            QMessageBox.critical(
                self, "Momento",
                f"Rename failed:\n{e}\n\n{detail}",
            )
            self.refresh()
            return

        cached_slug = self._game_slug_cache.pop(str(path), None)
        if cached_slug is not None:
            self._game_slug_cache[str(new_path)] = cached_slug
        cached_duration = self._duration_cache.pop(str(path), None)
        if cached_duration is not None:
            self._duration_cache[str(new_path)] = cached_duration
        cached_hint = self._duration_hint_cache.pop(str(path), None)
        if cached_hint is not None:
            self._duration_hint_cache[str(new_path)] = cached_hint
        if was_loaded:
            self._refresh_and_reselect(new_path)
        else:
            self.refresh()
        self._status.setText(f"Renamed to {new_path.name}")

    def _on_repair_requested(self, path: Path) -> None:
        """Rewrite a recording's container metadata via ffmpeg stream-copy.

        Useful for recordings that were killed mid-write (segment header
        never finalised → no duration → the trim UI is locked). ffmpeg writes
        the re-mux to ``<name>.repairing.mkv`` and the result is swapped over
        the original with an atomic ``os.replace``; on any failure the
        original is left untouched.
        """
        if not isinstance(path, Path) or not path.exists():
            self._status.setText("File missing — refresh.")
            return
        if path.suffix.lower() != ".mkv":
            self._status.setText("Only MKV recordings can be repaired.")
            return
        if self._file_key(path) in self._trimming_paths:
            self._status.setText(
                f"Can't repair {path.name} while it is being exported."
            )
            return
        # A startup auto-repair may already be re-muxing this file. Queueing a
        # second one would have both ffmpeg processes fight over the same temp
        # — refuse and tell the user it's already running.
        if is_repairing(path):
            self._status.setText(f"{path.name} is already being repaired…")
            return
        size_mb = path.stat().st_size / 1024 / 1024
        reply = QMessageBox.question(
            self,
            "Repair recording?",
            (
                f"Repair {path.name}?\n\n"
                f"This rewrites the file's container in place — no quality "
                f"loss (stream-copy), but it has to read every byte. "
                f"Roughly 30 s to a few minutes for a {size_mb:.0f} MB file. "
                f"The original is left untouched unless the rewrite "
                f"completes successfully.\n\n"
                f"Most recordings don't need this — only run it when the "
                f"timeline is stuck at 0:00."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Release the preview's file handle — ffmpeg can't replace a file
        # that QMediaPlayer still has open.
        if self._current_selection == path:
            self.preview.load(None)
            self._current_selection = None

        # Snapshot the splitter sizes so we can restore them after the
        # post-repair dialogs land — wide error messages occasionally end
        # up pushing the splitter handle around on Windows.
        self._repair_splitter_sizes = self._main_splitter.sizes()

        # Modal indeterminate progress dialog with a live elapsed-seconds
        # readout — ffmpeg doesn't pipe progress through ``-loglevel
        # error`` so we can't paint a real percentage, but the busy bar +
        # elapsed counter is enough to communicate "still working".
        from PyQt6.QtCore import QElapsedTimer
        progress = QProgressDialog("Repairing…", "", 0, 0, self)
        progress.setWindowTitle("Momento — Repairing recording")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)
        progress.setValue(0)

        elapsed = QElapsedTimer()
        elapsed.start()
        tick = QTimer(progress)
        tick.setInterval(500)

        def _on_tick() -> None:
            secs = max(1, elapsed.elapsed() // 1000)
            mins, s = divmod(secs, 60)
            unit = f"{mins}m {s:02d}s" if mins else f"{s}s"
            progress.setLabelText(f"Repairing {path.name}\nElapsed: {unit}")

        tick.timeout.connect(_on_tick)
        tick.start()
        _on_tick()

        self._repair_target = path
        self._repair_was_owned = has_valid_ownership_marker(path) or bool(
            self._game_slug_cache.get(str(path))
        )
        self._repair_progress = progress
        self._repair_tick = tick
        self._status.setText(f"Repairing {path.name}…")
        if repair_async(path, self._on_repair_done) is None:
            # Lost a race to a just-started repair — _on_repair_done will never
            # fire, so don't show a modal that can't close itself.
            tick.stop()
            progress.close()
            progress.deleteLater()
            self._repair_target = None
            self._repair_was_owned = False
            self._repair_progress = None
            self._repair_tick = None
            self._status.setText(f"{path.name} is already being repaired…")
            return
        progress.exec()  # modal — blocks until _on_repair_done closes it

    def _on_repair_done(self, path_str: str, ok: bool, err: str) -> None:
        target = getattr(self, "_repair_target", None)
        self._repair_target = None
        was_owned = bool(getattr(self, "_repair_was_owned", False))
        self._repair_was_owned = False
        # Tear down the progress dialog before anything else — leaves the
        # event loop in a clean state for the warning dialog below.
        tick = getattr(self, "_repair_tick", None)
        if tick is not None:
            tick.stop()
        self._repair_tick = None
        dialog = getattr(self, "_repair_progress", None)
        self._repair_progress = None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

        p = Path(path_str)
        if not ok:
            self._status.setText(f"Repair failed: {err[:120]}")
            logger.error("Repair failed for %s: %s", path_str, err)
            # Detailed text keeps the ffmpeg stderr collapsible — without
            # that the dialog ends up obnoxiously wide and (on Windows)
            # nudges the editor's splitter handle around.
            box = QMessageBox(self)
            box.setWindowTitle("Momento")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                f"Couldn't repair {p.name}.\n\nThe original file is unchanged."
            )
            box.setDetailedText(err or "No details.")
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
            if target is not None:
                self._list.select_by_path(target, emit=False)
                self.selected_changed.emit(target)
            self._restore_splitter_after_repair()
            return
        self._status.setText(f"Repaired {p.name}.")
        if was_owned and not mark_recording_owned(p):
            logger.warning("Could not refresh ownership marker after repair: %s", p)
        # Invalidate the repaired file's cached metadata BEFORE refresh(). It was
        # probed in its broken state (slug="", no duration written), and
        # _kick_metadata_probe short-circuits on any cached key — so without this
        # the now-finalised file keeps showing "—" for duration (and no game
        # line) until an app restart. Mirror add_finished_recording's eviction.
        for cache_key in {path_str, str(p), str(target) if target is not None else None}:
            if cache_key is None:
                continue
            self._game_slug_cache.pop(cache_key, None)
            self._duration_cache.pop(cache_key, None)
            self._duration_hint_cache.pop(cache_key, None)
            self._thumb_submitted.discard(cache_key)
        # Refresh so the new size + readable duration show up.
        if target is not None:
            self._refresh_and_reselect(target)
        else:
            self.refresh()
        self._restore_splitter_after_repair()

    def _refresh_and_reselect(self, path: Path) -> None:
        """Rebuild after mutation while keeping row, preview, and timeline aligned."""
        self._current_selection = path
        self.refresh(preserve_selection=True)
        # A filter may hide the target and make refresh select another row.
        # Reload only when the target is still the visibly selected item.
        if self._current_selection == path:
            self.selected_changed.emit(path)

    def _restore_splitter_after_repair(self) -> None:
        sizes = getattr(self, "_repair_splitter_sizes", None)
        self._repair_splitter_sizes = None
        if sizes:
            self._main_splitter.setSizes(sizes)

    @staticmethod
    def _file_key(path: Path | str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def _is_file_busy(self, path: Path | str) -> bool:
        return (
            EditorWindow._file_key(path) in getattr(self, "_trimming_paths", set())
            or is_repairing(path)
            or is_file_active(path)
        )

    def _cancel_active_trim_for_quit(self) -> None:
        """Cancel and drain an export only when the application really quits."""
        self._app_quitting = True
        worker = getattr(self, "_trim_worker", None)
        thread = getattr(self, "_trim_thread", None)
        if worker is not None:
            worker.cancel()
        if thread is None or not thread.isRunning():
            return
        thread.quit()
        if not thread.wait(10_000):
            logger.warning("Trim thread did not stop promptly; waiting for safe shutdown")
            if worker is not None:
                worker.cancel()
            thread.wait()

    def _open_output_folder(self) -> None:
        folder = str(self._config.output_folder)
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except OSError:
            try:
                subprocess.Popen(["explorer.exe", folder])
            except OSError as e:
                QMessageBox.warning(self, "Momento", f"Could not open folder: {e}")

    # ----------------------------------------------------- window geometry
    _WINDOW_STATE_GROUP = "editor"

    def _window_state_settings(self) -> QSettings:
        return QSettings(str(window_state_path()), QSettings.Format.IniFormat)

    def _restore_window_state(self) -> None:
        s = self._window_state_settings()
        s.beginGroup(self._WINDOW_STATE_GROUP)
        try:
            geom = s.value("geometry")
            restored = bool(geom) and self.restoreGeometry(geom)
            state = s.value("state")
            if state:
                self.restoreState(state)
            # restoreGeometry()/showMaximized() don't reliably re-apply the
            # maximized state OR the correct monitor for a window that's restored
            # while hidden (the editor is prebuilt hidden, then shown from the
            # tray) — they drift to the primary monitor. So track the maximized
            # flag + the absolute normal rect, and re-place explicitly on first
            # show. With no valid saved geometry, default to maximized.
            self._want_maximized = (
                s.value("maximized", False, type=bool) if restored else True
            )
            self._saved_normal_geom = s.value("normal_geometry")  # QRect | None
        finally:
            s.endGroup()

    def _save_window_state(self) -> None:
        # Never persist the borderless-fullscreen state (frameless window at
        # monitor bounds). closeEvent exits fullscreen before saving, but the
        # tray-Quit path saves via aboutToQuit without running closeEvent —
        # keeping the previous (normal/maximized) save is strictly better.
        try:
            if self.preview.is_fullscreen():
                return
        except Exception:
            pass  # teardown ordering — preview may already be gone at quit
        s = self._window_state_settings()
        s.beginGroup(self._WINDOW_STATE_GROUP)
        try:
            s.setValue("geometry", self.saveGeometry())
            s.setValue("state", self.saveState())
            s.setValue("maximized", self.isMaximized())
            # Save the absolute normal (un-maximized) rect too. Its x/y encode
            # which monitor the window was on, so a multi-monitor restore can
            # re-place it there explicitly — restoreGeometry()/showMaximized()
            # both drift back to the primary monitor for a prebuilt-hidden window.
            ng = self.normalGeometry() if self.isMaximized() else self.geometry()
            s.setValue("normal_geometry", ng)
        finally:
            s.endGroup()

    def _apply_first_show_geometry(self) -> None:
        """Place the window on the monitor + size it was last closed at.

        Done on first show (not in __init__) because the editor is prebuilt
        hidden, and a hidden window's screen can't be set reliably. Uses the
        saved absolute normal rect to land on the right monitor, then maximizes
        there if it was maximized."""
        ng = getattr(self, "_saved_normal_geom", None)
        if isinstance(ng, QRect) and ng.isValid():
            # Only honour the saved rect if it still lands on a connected screen
            # (a monitor may have been unplugged) — else leave the window where
            # restoreGeometry put it so it can't strand offscreen.
            screens = QApplication.screens() if QApplication.instance() else []
            if any(scr.geometry().intersects(ng) for scr in screens):
                if self.isMaximized():
                    # A maximized window ignores setGeometry; drop to normal so
                    # the reposition takes, then re-maximize on the right monitor.
                    self.showNormal()
                self.setGeometry(ng)
        if getattr(self, "_want_maximized", False) and not self.isMaximized():
            self.showMaximized()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        # Restore the saved monitor + maximized state the first time the window
        # is shown. Only on first show, so a later tray re-open respects whatever
        # the user left it at this session. All of this happens while the window
        # is still at opacity 0 (revealed below), so any reposition is invisible.
        if not getattr(self, "_geometry_applied", False):
            self._geometry_applied = True
            self._apply_first_show_geometry()
        # Anti-flash: Windows paints a top-level window's default (white)
        # background for a frame before Qt's first dark paint — a blinding
        # flash on a dark, maximised window, worse on a re-map from hidden.
        # The window is kept at opacity 0 until shown (see __init__ and the
        # close-to-tray path); reveal it only after the event loop has had a
        # turn to paint the real content, so the white frame happens while
        # the window is invisible.
        if self.windowOpacity() < 1.0:
            QTimer.singleShot(0, self._reveal_window)
        # Now that the window is on screen, load the selected recording into
        # the preview (it's kept unloaded while hidden — see
        # _sync_preview_to_visibility). setSource is non-blocking, so this
        # doesn't hitch the open; the first frame decodes asynchronously.
        self._sync_preview_to_visibility()
        # The editor is prebuilt hidden, so any bookmark-strip sizing done at
        # selection time ran with no real geometry — refit now the window has
        # actual size so the strip can't overlap the timeline on first open.
        self._sync_bottom_panel_height()

    def _reveal_window(self) -> None:
        # setWindowOpacity(1.0) also drops the transient layered-window style,
        # so the native video surface composites normally afterwards.
        self.setWindowOpacity(1.0)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Leave video fullscreen before saving/hiding. Closing while in
        # borderless fullscreen (Ctrl+W / File->Close work even without a
        # title bar) would otherwise park a FRAMELESS monitor-bounds window
        # with all its chrome hidden in the tray — and reopening would bring
        # exactly that back.
        try:
            if self.preview.is_fullscreen():
                self.preview.toggle_fullscreen()
        except Exception:
            logger.exception("Could not exit fullscreen during close")
        # Save geometry on every close path — whether the user hides the
        # window or quits the app entirely, the next launch should land in
        # the same place.
        self._save_window_state()
        if self._settings_panel is not None:
            try:
                self._settings_panel._stop_mic_test()
            except Exception:
                pass
        # Every close path must silence playback, including the optional mode
        # that closes this window object while the tray application stays up.
        self._park_preview_for_tray()
        QTimer.singleShot(10_000, self._release_preview_if_parked)
        # ``close_to_tray`` (default on) hides the editor instead of closing
        # it; the tray icon stays the user's entry point. Quit from the tray
        # menu when they really want to exit.
        if getattr(self._config, "close_to_tray", True):
            event.ignore()
            self.hide()
            # Release the preview's video decoder a short while after parking in
            # the tray. Delayed (not immediate) so a quick close-then-reopen
            # stays instant — the editor stays built either way; this only frees
            # a multi-hour clip's WMF buffers once the window is genuinely left
            # parked. A reopen cancels it implicitly via the isVisible() check.
            # Re-arm the anti-flash guard so the next show starts transparent
            # and only reveals once it has repainted.
            self.setWindowOpacity(0.0)
            return
        super().closeEvent(event)

    def _park_preview_for_tray(self) -> None:
        """Silence playback immediately when the editor is hidden to tray."""
        try:
            self._remember_preview_position()
            self.preview.pause()
        except Exception:
            logger.exception("Could not pause preview while parking editor")

    def _release_preview_if_parked(self) -> None:
        """Unload the preview iff the editor is still hidden (parked in tray)."""
        if not self.isVisible():
            self._sync_preview_to_visibility()


# ------------------------------------------------------------- helpers
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _resolve_output_path(folder: Path, user_name: str) -> Path:
    """Sanitize the user-typed clip name into a non-colliding clips/ path.

    Clips always go into a ``clips/`` subfolder relative to ``folder``
    (or stay in ``folder`` if it already IS clips/, e.g. when the user
    re-trims an existing clip). The subfolder is created on demand.
    """
    name = user_name.strip()
    if name.lower().endswith(".mp4"):
        name = name[:-4]
    name = _INVALID_FS_CHARS.sub("_", name).strip().rstrip(".")
    if not name:
        name = "clip"

    clips_dir = folder if folder.name.lower() == CLIPS_SUBDIR_NAME else folder / CLIPS_SUBDIR_NAME
    clips_dir.mkdir(parents=True, exist_ok=True)

    candidate = clips_dir / f"{name}.mp4"
    if not candidate.exists():
        return candidate
    # Existing — auto-suffix so we never overwrite without asking.
    n = 2
    while True:
        candidate = clips_dir / f"{name}_{n}.mp4"
        if not candidate.exists():
            return candidate
        n += 1
        if n > 9999:
            raise RuntimeError("Ran out of suffix slots")


_RECORDING_SUFFIXES = (".mkv", ".mp4")
CLIPS_SUBDIR_NAME = "clips"


def _is_clip(path: Path) -> bool:
    """A file is a clip iff it lives in the ``clips/`` subfolder.

    Classification is purely by location — the trim worker writes there,
    the migration moves legacy clips there. No filename heuristic.
    """
    return path.parent.name.lower() == CLIPS_SUBDIR_NAME


def _list_recordings(
    folder: Path, *, exclude_paths: set[Path] | None = None
) -> list[Path]:
    """Recordings (root folder) + clips (clips/ subfolder), newest first.

    Both are returned in one list — the editor's tab filter separates them
    by ``_is_clip(path)``.
    """
    if not folder.is_dir():
        return []
    excluded: set[str] = set()
    for path in exclude_paths or set():
        try:
            excluded.add(str(Path(path).resolve()))
        except OSError:
            excluded.add(str(path))
    out: list[tuple[float, Path]] = []
    for parent in (folder, folder / CLIPS_SUBDIR_NAME):
        if not parent.is_dir():
            continue
        try:
            entries = list(parent.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if (
                    p.is_file()
                    and p.suffix.lower() in _RECORDING_SUFFIXES
                    and not is_repair_temp(p)
                    and str(p.resolve()) not in excluded
                ):
                    out.append((p.stat().st_mtime, p))
            except OSError:
                continue
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out]


def _safe_mtime(path: Path) -> float:
    """Modification time, or 0.0 if the file vanished between scan and sort."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


