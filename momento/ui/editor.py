"""Editor window: recordings/clips list (left), preview + timeline (right).

Per-row duration and the ``MOMENTO_GAME`` tag are probed asynchronously
through :mod:`momento.core.media_probe`, so a folder with many recordings
doesn't freeze the UI.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QSettings, QThread, QTimer, pyqtSignal
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
from momento.core.game_names import game_slug_from_filename, humanise_game_name
from momento.core.game_watcher import ActiveGame
from momento.core.media_probe import (
    probe_duration_async,
    probe_metadata_async,
    repair_async,
)
from momento.util.time_format import fmt_time, parse_time
from momento.core.thumbnails import extract_async, thumb_is_fresh, thumb_path_for
from momento.trim.ffmpeg_trim import TrimWorker, next_clip_path
from momento.ui.preview import VideoPreview
from momento.ui.recordings_list import RecordingsList
from momento.ui import theme as _theme
from momento.ui.settings_dialog import SettingsPanel
from momento.ui.status_panel import StatusPanel
from momento.ui.timeline import Timeline
from momento.ui.widgets import AnchoredComboBox
from momento.util.paths import window_state_path
from momento.util.resources import app_icon_path

logger = logging.getLogger(__name__)


# Sort-dropdown options for the recordings/clips list.
# Tuples are (combo_label, key) — key maps to a branch in _sorted_files.
_SORT_MODES: tuple[tuple[str, str], ...] = (
    ("Newest first", "newest"),
    ("Oldest first", "oldest"),
    ("Longest", "longest"),
    ("Largest", "largest"),
    ("Game name", "game"),
)


class EditorWindow(QMainWindow):
    """The recordings browser + preview + timeline + settings host."""

    # Emitted when the user picks a different recording.
    selected_changed = pyqtSignal(object)  # Path | None
    # Emitted when the embedded settings panel saves — tray listens to apply
    # to SessionManager + hotkey + autostart.
    settings_saved = pyqtSignal(object)  # Config

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
        # User-driven list controls — pure UI state, not persisted.
        self._search_text: str = ""
        # See _SORT_MODES below for the option list and tuple shape.
        self._sort_mode: str = "newest"

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
        self._install_shortcuts()
        self._restore_window_state()
        # Tray Quit calls QApplication.quit() which skips closeEvent — hook
        # aboutToQuit too so geometry is always written before exit.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._save_window_state)
        self.refresh()
        # Pre-warm settings shortly after the editor exists, off the critical
        # open path, so navigating to it later doesn't stall.
        QTimer.singleShot(800, self._ensure_settings_panel)

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

    def _nudge(self, seconds: float) -> None:
        if self._is_text_focus():
            return
        dur = self.preview.duration()
        if dur <= 0:
            return
        target = max(0.0, min(dur, self.preview.position() + seconds))
        self.preview.seek(target)

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
    def refresh(self) -> None:
        """Rescan the output folder and repopulate the list."""
        # Probe + thumb jobs run on the global pool; they self-clean. Just
        # clear our "already submitted" tracking so a re-scanned folder can
        # re-submit any rows whose files came back.
        self._thumb_submitted.clear()
        folder = Path(self._config.output_folder)
        self._all_files = _list_recordings(folder)
        # Update tab labels with current counts so the user sees "(N)" without
        # having to switch.
        n_rec = sum(1 for f in self._all_files if not _is_clip(f))
        n_clip = sum(1 for f in self._all_files if _is_clip(f))
        self._section_tabs.setTabText(0, f"Recordings ({n_rec})")
        self._section_tabs.setTabText(1, f"Clips ({n_clip})")
        self._rebuild_game_filter()
        self._reapply_filter()  # handles selection + empty-state placeholder
        section_label = "clip" if self._section_tabs.currentIndex() == 1 else "recording"
        visible_count = self._visible_count()
        self._status.setText(f"{visible_count} {section_label}(s) in {folder}")

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

    def _reapply_filter(self) -> None:
        """Re-render the list according to tab + game filter + search + sort."""
        self._list.clear()
        for f in self._visible_files():
            self._add_item(f)
        self._update_list_empty_state()
        # Auto-select the first row so the preview opens on real content,
        # not a black QVideoWidget. Empty → emit None so the preview clears
        # any stale source/timeline state.
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
            col.addWidget(self._status_panel)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.addWidget(self._build_left_pane())
        self._main_splitter.addWidget(self._build_right_pane())
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setSizes([460, 740])
        col.addWidget(self._main_splitter, stretch=1)
        return wrapper

    def _ensure_settings_panel(self) -> SettingsPanel:
        """Lazy-construct the (heavy) settings panel and wire it once."""
        if self._settings_panel is None:
            self._settings_panel = SettingsPanel(self._config)
            self._settings_panel.settings_saved.connect(self._on_settings_saved)
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
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(12, 12, 8, 12)
        layout.setSpacing(8)

        # Tab bar at the top — splits the user's files into "Recordings"
        # (raw game captures) vs "Clips" (exported trims). Letting users
        # mass-delete one without taking the other is a real safety win.
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self._section_tabs = QTabBar()
        self._section_tabs.setDocumentMode(True)
        self._section_tabs.addTab("Recordings")
        self._section_tabs.addTab("Clips")
        self._section_tabs.setStyleSheet(
            "QTabBar::tab { font-size: 12pt; font-weight: 600; padding: 6px 14px; }"
            "QTabBar::tab:selected { color: #e6e8ee; }"
            "QTabBar::tab:!selected { color: #6e7588; }"
        )
        self._section_tabs.currentChanged.connect(self._on_section_changed)
        header_row.addWidget(self._section_tabs)
        header_row.addStretch(1)
        # Cog sits in the section header — always visible, doesn't compete
        # with the action buttons (Refresh / Delete) at the bottom.
        self._settings_btn = QToolButton()
        self._settings_btn.setText("⚙")
        self._settings_btn.setToolTip("Settings  (Ctrl+,)")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._settings_btn.setStyleSheet(
            "QToolButton { font-size: 16pt; padding: 2px 8px; border-radius: 6px; }"
            "QToolButton:hover { background: #2e333e; }"
        )
        self._settings_btn.clicked.connect(self._show_settings_view)
        header_row.addWidget(self._settings_btn)
        layout.addLayout(header_row)

        # Search box — case-insensitive substring match against the friendly
        # game name + filename stem. Hidden value, not persisted.
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search recordings…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self._search_edit)

        filter_row = QHBoxLayout()
        filter_lbl = QLabel("Filter by game:")
        filter_lbl.setStyleSheet("color: #9aa1b1;")
        self._game_combo = AnchoredComboBox()
        self._game_combo.setToolTip(
            "Show only files from a specific game. The dropdown only lists "
            "games that have at least one matching file in the folder."
        )
        self._game_combo.addItem("All games", None)
        self._game_combo.currentIndexChanged.connect(self._on_game_filter_changed)
        filter_row.addWidget(filter_lbl)
        filter_row.addWidget(self._game_combo, stretch=1)
        sort_lbl = QLabel("Sort:")
        sort_lbl.setStyleSheet("color: #9aa1b1;")
        self._sort_combo = AnchoredComboBox()
        for label, key in _SORT_MODES:
            self._sort_combo.addItem(label, key)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        filter_row.addWidget(sort_lbl)
        filter_row.addWidget(self._sort_combo)
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

        # Stack the list with an empty-state placeholder so the left pane
        # doesn't show a blank QListView when the folder is empty / a filter
        # matches nothing. The empty message is rewritten in _reapply_filter.
        self._list_empty_label = QLabel("")
        self._list_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_empty_label.setWordWrap(True)
        self._list_empty_label.setStyleSheet(
            "QLabel { color: #8a92a3; font-size: 10pt; padding: 32px 24px; }"
        )
        self._list_stack = QStackedWidget()
        self._list_stack.addWidget(self._list)
        self._list_stack.addWidget(self._list_empty_label)
        layout.addWidget(self._list_stack, stretch=1)

        button_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh list")
        refresh_btn.clicked.connect(self.refresh)
        button_row.addWidget(refresh_btn)
        delete_btn = QPushButton("Delete selected")
        delete_btn.setToolTip("Permanently delete the selected files (and their bookmarks/thumbnails).")
        delete_btn.clicked.connect(self._on_delete_selected)
        button_row.addWidget(delete_btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._status = QLabel("")
        self._status.setStyleSheet("color: #6e7588; font-size: 9pt;")
        layout.addWidget(self._status)

        return wrap

    def _make_clip_label(self, text: str) -> QLabel:
        """Small grey caption label used to head each time-input field."""
        label = QLabel(text)
        label.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
        return label

    def _make_time_edit(self, placeholder: str) -> QLineEdit:
        """Compact time-input field (``M:SS`` or ``H:MM:SS``)."""
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMaximumWidth(96)
        edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        edit.setToolTip("Enter a time as M:SS or H:MM:SS — press Enter to apply.")
        return edit

    def _build_right_pane(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.preview = VideoPreview()
        self.preview.error_occurred.connect(self._on_preview_error)
        splitter.addWidget(self.preview)

        bottom = QFrame()
        bottom.setFrameShape(QFrame.Shape.StyledPanel)
        # Panel sizes to content (clip controls + optional scroll + optional
        # bookmark strip). Splitter gives the rest to the preview. Cap so a
        # very tall window doesn't stretch the panel into empty space.
        bottom.setMinimumHeight(170)
        bottom.setMaximumHeight(260)
        bottom_lay = QVBoxLayout(bottom)
        bottom_lay.setContentsMargins(14, 12, 14, 12)
        bottom_lay.setSpacing(10)

        info_row = QHBoxLayout()
        info_row.setSpacing(10)
        info_row.addWidget(self._make_clip_label("Start"))
        self._start_time_edit = self._make_time_edit("0:00")
        self._start_time_edit.editingFinished.connect(self._on_start_time_edited)
        info_row.addWidget(self._start_time_edit)
        info_row.addWidget(self._make_clip_label("End"))
        self._end_time_edit = self._make_time_edit("0:00")
        self._end_time_edit.editingFinished.connect(self._on_end_time_edited)
        info_row.addWidget(self._end_time_edit)
        info_row.addWidget(self._make_clip_label("Length"))
        self._clip_length_label = QLabel("0:00")
        self._clip_length_label.setStyleSheet("color: #e6e8ee; font-size: 10pt; font-weight: 600;")
        self._clip_length_label.setMinimumWidth(64)
        info_row.addWidget(self._clip_length_label)
        info_row.addStretch(1)
        # Reset-zoom button — visible only when actually zoomed in.
        self._reset_zoom_btn = QToolButton()
        self._reset_zoom_btn.setText("Reset zoom")
        self._reset_zoom_btn.setToolTip("Restore the full clip view")
        self._reset_zoom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_zoom_btn.setStyleSheet(
            "QToolButton { color: #ddd; padding: 2px 8px; border-radius: 4px; "
            "border: 1px solid #3a4150; }"
            "QToolButton:hover { background: #2e333e; }"
        )
        self._reset_zoom_btn.setVisible(False)
        info_row.addWidget(self._reset_zoom_btn)
        bottom_lay.addLayout(info_row)

        self.timeline = Timeline()
        bottom_lay.addWidget(self.timeline)

        # Pan scrollbar — only useful (and visible) when the timeline is
        # zoomed in to a subset of the clip.
        self._timeline_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self._timeline_scroll.setVisible(False)
        self._timeline_scroll.valueChanged.connect(self._on_pan_scroll)
        bottom_lay.addWidget(self._timeline_scroll)

        # Bookmark chip strip — hidden when the current clip has no
        # bookmarks. Populated from the bookmark sidecar in
        # ``_on_recording_selected``; each chip seeks the preview when
        # clicked. Same source of truth as the orange ticks on the timeline.
        self._bookmarks_panel = QFrame()
        self._bookmarks_panel.setFrameShape(QFrame.Shape.NoFrame)
        bm_lay = QHBoxLayout(self._bookmarks_panel)
        bm_lay.setContentsMargins(0, 4, 0, 4)
        bm_lay.setSpacing(6)
        bm_label = QLabel("Bookmarks:")
        bm_label.setStyleSheet("color: #9aa1b1; font-size: 9pt;")
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
        bottom_lay.addWidget(self._bookmarks_panel)

        export_row = QHBoxLayout()
        self._play_clip_btn = QPushButton("Play clip portion")
        self._play_clip_btn.setEnabled(False)
        self._play_clip_btn.setToolTip(
            "Play just the section between the trim handles so you can preview "
            "the exported clip before saving."
        )
        self._play_clip_btn.clicked.connect(self._on_play_clip_clicked)
        export_row.addWidget(self._play_clip_btn)

        self._export_btn = QPushButton("Export selected clip")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export_clicked)
        export_row.addWidget(self._export_btn)

        self._export_progress = QProgressBar()
        self._export_progress.setRange(0, 100)
        self._export_progress.setValue(0)
        self._export_progress.setTextVisible(True)
        self._export_progress.setFormat("")
        export_row.addWidget(self._export_progress, stretch=1)
        bottom_lay.addLayout(export_row)

        splitter.addWidget(bottom)

        # Wire selection -> preview + timeline.
        self.selected_changed.connect(self._on_recording_selected)
        # Preview events update timeline.
        self.preview.duration_changed.connect(self._on_duration_changed)
        self.preview.position_changed.connect(self.timeline.set_playhead)
        # Dragging a handle seeks the preview and updates the clip-length label.
        self.timeline.seek_requested.connect(self.preview.seek)
        self.timeline.start_changed.connect(lambda _v: self._update_clip_length_label())
        self.timeline.end_changed.connect(lambda _v: self._update_clip_length_label())
        self.timeline.view_changed.connect(self._on_timeline_view_changed)
        self._reset_zoom_btn.clicked.connect(self.timeline.reset_zoom)

        # Export-task state.
        self._trim_thread: QThread | None = None
        self._trim_worker: TrimWorker | None = None

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        return splitter

    def _on_preview_error(self, message: str) -> None:
        # Non-blocking — the placeholder/preview itself is empty on error.
        self._status.setText(f"Preview error: {message}")

    # ----------------------------------------------------- trim/export
    def _on_recording_selected(self, path) -> None:
        self.preview.load(path)
        self._current_selection: Path | None = path if path else None
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
        else:
            marks = []
            self._probing_path = None
        self.timeline.set_bookmarks(marks)
        self._populate_bookmark_chips(marks)
        self._export_btn.setEnabled(False)
        self._export_progress.setValue(0)
        self._export_progress.setFormat("")

    def _populate_bookmark_chips(self, marks: list[float]) -> None:
        """Rebuild the bookmark chip strip from ``marks``. Hide the panel
        when there are none."""
        layout = self._bookmarks_chips_layout
        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not marks:
            self._bookmarks_panel.setVisible(False)
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
            chip.clicked.connect(lambda _checked=False, s=seconds: self.preview.seek(s))
            layout.addWidget(chip)
        layout.addStretch(1)
        self._bookmarks_panel.setVisible(True)

    def _on_duration_probe_done(self, path_str: str, seconds: float) -> None:
        """Apply (or note the absence of) ffprobe-derived duration."""
        if path_str != self._probing_path:
            # User selected a different clip while the probe was in flight.
            return
        if seconds > 0:
            # Feed the hint to the preview; it'll override its internal
            # scrubber range if QMediaPlayer is still stuck at 0.
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
        bar.setVisible(zoomed_in)
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
        """Refresh all three time displays from the timeline's current handles."""
        if self.timeline.duration <= 0:
            self._clip_length_label.setText("0:00")
            self._start_time_edit.blockSignals(True)
            self._end_time_edit.blockSignals(True)
            self._start_time_edit.setText("")
            self._end_time_edit.setText("")
            self._start_time_edit.blockSignals(False)
            self._end_time_edit.blockSignals(False)
            return
        length = max(0.0, self.timeline.end_seconds - self.timeline.start_seconds)
        self._clip_length_label.setText(fmt_time(length))
        # Mirror the handles into the input fields (but not while the user
        # is actively editing one — focus-having field stays as-typed).
        focus = QApplication.focusWidget()
        if focus is not self._start_time_edit:
            self._start_time_edit.setText(fmt_time(self.timeline.start_seconds))
        if focus is not self._end_time_edit:
            self._end_time_edit.setText(fmt_time(self.timeline.end_seconds))

    def _on_start_time_edited(self) -> None:
        if self.timeline.duration <= 0:
            return
        seconds = parse_time(self._start_time_edit.text())
        if seconds is None:
            self._start_time_edit.setText(fmt_time(self.timeline.start_seconds))
            self._status.setText("Couldn't parse the start time — try M:SS or H:MM:SS.")
            return
        # Keep ``end`` fixed unless that would invert the range — then push
        # end forward by the minimum gap so set_clip_range doesn't have to
        # silently clamp it.
        new_end = max(self.timeline.end_seconds, seconds + 0.05)
        self.timeline.set_clip_range(seconds, new_end)
        self.preview.seek(self.timeline.start_seconds)

    def _on_end_time_edited(self) -> None:
        if self.timeline.duration <= 0:
            return
        seconds = parse_time(self._end_time_edit.text())
        if seconds is None:
            self._end_time_edit.setText(fmt_time(self.timeline.end_seconds))
            self._status.setText("Couldn't parse the end time — try M:SS or H:MM:SS.")
            return
        new_start = min(self.timeline.start_seconds, max(0.0, seconds - 0.05))
        self.timeline.set_clip_range(new_start, seconds)
        self.preview.seek(self.timeline.end_seconds)

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
        suggested = next_clip_path(Path(path)).stem
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
        output = _resolve_output_path(Path(path).parent, chosen)
        self._launch_trim(Path(path), start, end, output)

    def _launch_trim(
        self, input_path: Path, start: float, end: float, output: Path
    ) -> None:
        """Spin up the TrimWorker / QThread pair for a trim export."""
        if self._trim_thread is not None:
            self._status.setText("An export is already in progress — wait for it to finish.")
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
        self._export_btn.setEnabled(False)
        self._export_progress.setValue(0)
        self._export_progress.setFormat("Exporting… 0%")
        self._status.setText(f"Exporting {output.name}…")
        thread.start()

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
        from momento.ui.youtube_upload_dialog import YouTubeUploadDialog
        from momento.ui.youtube_upload_progress import YouTubeUploadProgressDialog
        from momento.config import save_config

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

        # 2. Try to load credentials (handles refresh / revoked-token cases).
        creds = yt_auth.get_authorized_credentials()
        if creds is None:
            QMessageBox.warning(
                self,
                "YouTube re-auth needed",
                "Your saved YouTube sign-in is no longer valid (it may have "
                "been revoked, or the refresh failed).\n\n"
                "Open Settings → YouTube and click Connect again.",
            )
            return

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
        self._status.setText(f"Exported {name}")
        # Pull the new clip into the list immediately.
        self.refresh()

    def _on_trim_failed(self, message: str) -> None:
        self._export_progress.setValue(0)
        self._export_progress.setFormat("Failed")
        self._status.setText(f"Export failed: {message}")
        QMessageBox.warning(self, "Momento", f"Export failed:\n{message}")

    def _on_trim_thread_finished(self) -> None:
        self._trim_thread = None
        self._trim_worker = None
        ready = self.preview.duration() > 0
        self._export_btn.setEnabled(ready)

    # ---------------------------------------------------------- rows
    def _add_item(self, path: Path) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        existing_thumb = thumb_path_for(path)
        thumb = str(existing_thumb) if thumb_is_fresh(path) else None
        self._list.add_item(
            path=path,
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            duration_secs=None,
            thumb_path=thumb,
        )
        self._kick_metadata_probe(path)
        if thumb is None:
            self._kick_thumbnail(path)

    def _kick_metadata_probe(self, path: Path) -> None:
        key = str(path)
        if key in self._game_slug_cache:
            return  # already probed or in-flight (None sentinel)
        self._game_slug_cache[key] = None
        probe_metadata_async(path, self._on_metadata_probed)

    def _on_metadata_probed(self, path_str: str, duration: float, slug: str) -> None:
        if duration > 0:
            self._list.update_duration(Path(path_str), duration)
            self._duration_cache[path_str] = duration
        prior = self._game_slug_cache.get(path_str)
        self._game_slug_cache[path_str] = slug
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

    def _on_delete_selected(self) -> None:
        paths = self._list.selected_paths()
        if not paths:
            self._status.setText("Nothing selected to delete.")
            return
        self._on_delete_requested(paths)

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

        deleted = 0
        errors: list[str] = []
        for p in existing:
            ok = True
            self._game_slug_cache.pop(str(p), None)
            self._duration_cache.pop(str(p), None)
            for target in (p, thumb_path_for(p), bookmark_sidecar(p)):
                try:
                    Path(target).unlink(missing_ok=True)
                except OSError as e:
                    errors.append(f"{target.name}: {e}")
                    ok = False
            if ok:
                deleted += 1

        if errors:
            QMessageBox.warning(
                self, "Momento",
                "Some files could not be deleted:\n\n" + "\n".join(errors[:20])
                + ("\n\n…and more" if len(errors) > 20 else ""),
            )

        # Re-scan; this updates the filter combo counts naturally.
        self.refresh()
        if deleted == 1:
            self._status.setText(f"Deleted {existing[0].name}")
        else:
            self._status.setText(f"Deleted {deleted} recording(s)")

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
        if new_path.exists():
            QMessageBox.warning(
                self, "Momento",
                f"A file named {new_path.name!r} already exists in this folder.",
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

        try:
            for src, dst in moves:
                src.rename(dst)
        except OSError as e:
            QMessageBox.critical(
                self, "Momento",
                f"Rename failed:\n{e}\n\nSome files may have moved partially.",
            )
            self.refresh()
            return

        cached_slug = self._game_slug_cache.pop(str(path), None)
        if cached_slug is not None:
            self._game_slug_cache[str(new_path)] = cached_slug
        cached_duration = self._duration_cache.pop(str(path), None)
        if cached_duration is not None:
            self._duration_cache[str(new_path)] = cached_duration
        self.refresh()
        if was_loaded:
            # Re-select the renamed file so the user doesn't lose context.
            self._current_selection = new_path
            self.preview.load(new_path)
        self._status.setText(f"Renamed to {new_path.name}")

    def _on_repair_requested(self, path: Path) -> None:
        """Rewrite a recording's container metadata via ffmpeg stream-copy.

        Useful for recordings that were killed mid-write (segment header
        never finalised → no duration → the trim UI is locked). Original
        is kept as ``<name>.broken-bak.mkv`` until ffmpeg finishes, then
        deleted; on any failure the original is left untouched.
        """
        if not isinstance(path, Path) or not path.exists():
            self._status.setText("File missing — refresh.")
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
                f"The original is kept as a backup until the rewrite "
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
        self._repair_progress = progress
        self._repair_tick = tick
        self._status.setText(f"Repairing {path.name}…")
        repair_async(path, self._on_repair_done)
        progress.exec()  # modal — blocks until _on_repair_done closes it

    def _on_repair_done(self, path_str: str, ok: bool, err: str) -> None:
        target = getattr(self, "_repair_target", None)
        self._repair_target = None
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
            self._restore_splitter_after_repair()
            return
        self._status.setText(f"Repaired {p.name}.")
        # Refresh so the new size + readable duration show up.
        self.refresh()
        # Re-select the repaired clip so the user picks up where they were.
        if target is not None:
            self._current_selection = target
            self.preview.load(target)
        self._restore_splitter_after_repair()

    def _restore_splitter_after_repair(self) -> None:
        sizes = getattr(self, "_repair_splitter_sizes", None)
        self._repair_splitter_sizes = None
        if sizes:
            self._main_splitter.setSizes(sizes)

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
            if geom:
                self.restoreGeometry(geom)
            state = s.value("state")
            if state:
                self.restoreState(state)
        finally:
            s.endGroup()

    def _save_window_state(self) -> None:
        s = self._window_state_settings()
        s.beginGroup(self._WINDOW_STATE_GROUP)
        try:
            s.setValue("geometry", self.saveGeometry())
            s.setValue("state", self.saveState())
        finally:
            s.endGroup()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Save geometry on every close path — whether the user hides the
        # window or quits the app entirely, the next launch should land in
        # the same place.
        self._save_window_state()
        if self._settings_panel is not None:
            try:
                self._settings_panel._stop_mic_test()
            except Exception:
                pass
        # ``close_to_tray`` (default on) hides the editor instead of closing
        # it; the tray icon stays the user's entry point. Quit from the tray
        # menu when they really want to exit.
        if getattr(self._config, "close_to_tray", True):
            event.ignore()
            self.hide()
            return
        super().closeEvent(event)


# ------------------------------------------------------------- helpers
import re as _re

_INVALID_FS_CHARS = _re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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


def _list_recordings(folder: Path) -> list[Path]:
    """Recordings (root folder) + clips (clips/ subfolder), newest first.

    Both are returned in one list — the editor's tab filter separates them
    by ``_is_clip(path)``.
    """
    if not folder.is_dir():
        return []
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
                if p.is_file() and p.suffix.lower() in _RECORDING_SUFFIXES:
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


