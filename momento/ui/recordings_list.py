"""Card-style recordings list with thumbnails.

Replaces the plain QTableWidget editor list with a QListView + custom
QStyledItemDelegate that paints a thumbnail on the left and stacked
metadata on the right.

Public API:
    add_item(path, mtime, size_bytes, duration_secs|None, thumb_path|None)
    clear()
    update_duration(path, seconds)
    update_thumbnail(path, thumb_path)
    selected_path -> Path | None
    selected_path_changed signal -> Path | None
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import QListView, QMenu, QStyledItemDelegate, QStyle, QStyleOptionViewItem

from momento.core.game_names import friendly_recording_title
from momento.ui import theme as _theme
from momento.util.format import format_bytes
from momento.util.time_format import fmt_time

# Data roles
_ROLE_PATH = Qt.ItemDataRole.UserRole + 1
_ROLE_MTIME = Qt.ItemDataRole.UserRole + 2  # float (timestamp)
_ROLE_SIZE = Qt.ItemDataRole.UserRole + 3   # int (bytes)
_ROLE_DURATION = Qt.ItemDataRole.UserRole + 4  # float | None
_ROLE_THUMB = Qt.ItemDataRole.UserRole + 5  # str (path) | ""

THUMB_W = 160
THUMB_H = 90
CARD_PAD = 10
CARD_GAP = 12
CARD_HEIGHT = THUMB_H + 2 * CARD_PAD


class RecordingItemDelegate(QStyledItemDelegate):
    """Paints one card: thumbnail (left) + name/meta/duration (right)."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), CARD_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        try:
            rect = option.rect
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

            # ---- Card background ----
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            card = QRect(
                rect.left() + 4, rect.top() + 4,
                rect.width() - 8, rect.height() - 8,
            )
            if selected:
                # Tinted background derived from the accent so theme
                # changes carry through to the most prominent surface
                # in the list.
                accent = QColor(_theme.ACCENT)
                bg = QColor.fromHslF(
                    accent.hueF(),
                    max(0.0, accent.saturationF() * 0.55),
                    0.22,
                )
                border = accent
            elif hovered:
                bg = QColor("#262a33")
                border = QColor("#3a4150")
            else:
                bg = QColor("#1d2027")
                border = QColor("#262a33")
            painter.setPen(QPen(border, 1))
            painter.setBrush(QBrush(bg))
            painter.drawRoundedRect(card, 8, 8)

            # ---- Thumbnail ----
            thumb_rect = QRect(
                card.left() + CARD_PAD,
                card.top() + (card.height() - THUMB_H) // 2,
                THUMB_W,
                THUMB_H,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#0e1117"))
            painter.drawRoundedRect(thumb_rect, 5, 5)

            thumb_path = index.data(_ROLE_THUMB) or ""
            duration = index.data(_ROLE_DURATION)
            if thumb_path:
                pm = QPixmap(thumb_path)
                if not pm.isNull():
                    scaled = pm.scaled(
                        thumb_rect.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    x = thumb_rect.left() + (thumb_rect.width() - scaled.width()) // 2
                    y = thumb_rect.top() + (thumb_rect.height() - scaled.height()) // 2
                    painter.drawPixmap(x, y, scaled)
            else:
                # Placeholder glyph (small film square) while thumbnail loads.
                painter.setPen(QColor("#3a4150"))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                inner = thumb_rect.adjusted(20, 14, -20, -14)
                painter.drawRoundedRect(inner, 4, 4)
                painter.drawText(
                    inner,
                    Qt.AlignmentFlag.AlignCenter,
                    "…",
                )

            # Duration badge bottom-right of thumb
            if isinstance(duration, float) and duration > 0:
                dur_text = fmt_time(duration)
                badge_font = QFont(option.font)
                badge_font.setPointSize(max(7, option.font.pointSize() - 1))
                badge_font.setBold(True)
                painter.setFont(badge_font)
                fm = painter.fontMetrics()
                pad_x, pad_y = 6, 2
                tw = fm.horizontalAdvance(dur_text) + pad_x * 2
                th = fm.height() + pad_y * 2
                badge = QRect(
                    thumb_rect.right() - tw - 6,
                    thumb_rect.bottom() - th - 6,
                    tw, th,
                )
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(0, 0, 0, 180))
                painter.drawRoundedRect(badge, 4, 4)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, dur_text)

            # ---- Text column ----
            text_left = thumb_rect.right() + CARD_GAP
            text_rect = QRect(
                text_left, card.top() + CARD_PAD,
                card.right() - text_left - CARD_PAD,
                card.height() - 2 * CARD_PAD,
            )

            raw_path = Path(index.data(_ROLE_PATH) or "")
            name = friendly_recording_title(raw_path.name) if raw_path.name else "(unnamed)"
            mtime = index.data(_ROLE_MTIME) or 0.0
            size_bytes = index.data(_ROLE_SIZE) or 0

            # Name (bold, larger). Use ElideRight — the prefix is the game
            # name (the bit the user cares about), so cropping the tail is
            # the right move on narrow lists.
            name_font = QFont(option.font)
            name_font.setPointSize(option.font.pointSize() + 1)
            name_font.setBold(True)
            painter.setFont(name_font)
            painter.setPen(QColor("#e6e8ee"))
            fm_name = painter.fontMetrics()
            name_elided = fm_name.elidedText(
                name, Qt.TextElideMode.ElideRight, text_rect.width()
            )
            painter.drawText(
                QRect(text_rect.left(), text_rect.top(),
                      text_rect.width(), fm_name.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                name_elided,
            )

            # Meta line: date · size. Duration sits on the thumbnail.
            meta_font = QFont(option.font)
            meta_font.setPointSize(max(8, option.font.pointSize() - 1))
            painter.setFont(meta_font)
            painter.setPen(QColor("#9aa1b1"))
            try:
                date_str = datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")
            except (ValueError, OSError):
                date_str = "—"
            meta = "  ·  ".join((date_str, format_bytes(int(size_bytes))))
            fm_meta = painter.fontMetrics()
            painter.drawText(
                QRect(
                    text_rect.left(),
                    text_rect.top() + fm_name.height() + 4,
                    text_rect.width(),
                    fm_meta.height(),
                ),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                meta,
            )
        finally:
            painter.restore()


class RecordingsList(QListView):
    selected_path_changed = pyqtSignal(object)  # Path | None — the *current* item
    # Fires when the user asks to delete one or more rows (right-click / Delete
    # key / button). The editor is responsible for confirming and performing
    # the actual on-disk deletes.
    delete_requested = pyqtSignal(list)  # list[Path] (always >= 1)
    # Right-click "Open file location" — single path. (Was "Show in Explorer".)
    reveal_in_explorer_requested = pyqtSignal(object)  # Path
    # Right-click "Rename" — single path. Editor prompts + performs the rename
    # (including bookmark + thumb sidecars).
    rename_requested = pyqtSignal(object)  # Path
    # Right-click "Repair recording" — single path. Editor confirms + runs
    # ffmpeg stream-copy in background to rewrite Matroska segment header.
    repair_requested = pyqtSignal(object)  # Path
    # Right-click "Play" — select + start playback.
    play_requested = pyqtSignal(object)  # Path
    # Right-click "Export clip" — open the file and trigger the export prompt.
    export_requested = pyqtSignal(object)  # Path
    # Right-click "Upload to YouTube…" — single file. Editor checks the YouTube
    # connection state, prompts to connect if needed, then opens the upload
    # dialog + progress dialog. Works on either .mkv recordings or .mp4 clips
    # (YouTube accepts both).
    upload_to_youtube_requested = pyqtSignal(object)  # Path

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setItemDelegate(RecordingItemDelegate(self))
        # Extended selection: click = pick one, ctrl+click = toggle, shift+click
        # = range, ctrl+A = select all (handled by QAbstractItemView).
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # currentChanged drives the preview (most-recently-clicked card).
        self.selectionModel().currentChanged.connect(self._emit_selection)

        # Delete key shortcut — only fires when this list has focus.
        del_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self)
        del_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        del_shortcut.activated.connect(self._on_delete_shortcut)

    # -------------------------------------------------------- public API
    def clear(self) -> None:
        self._model.removeRows(0, self._model.rowCount())

    def row_count(self) -> int:
        return self._model.rowCount()

    def select_first(self) -> None:
        """Select row 0 if any rows exist — used to populate the preview
        when the tab is first opened so the user isn't staring at a black
        rectangle until they click."""
        if self._model.rowCount() == 0:
            return
        self.setCurrentIndex(self._model.index(0, 0))

    def select_by_path(self, path: Path | str) -> bool:
        """Move the current selection to the row matching ``path``.

        Returns True if a row was selected. Used by right-click actions
        that need to operate on the targeted row even if it isn't currently
        the preview's source.
        """
        target = str(path)
        for row in range(self._model.rowCount()):
            if self._model.item(row).data(_ROLE_PATH) == target:
                self.setCurrentIndex(self._model.index(row, 0))
                return True
        return False

    def add_item(
        self,
        path: Path,
        mtime: float,
        size_bytes: int,
        duration_secs: float | None = None,
        thumb_path: str | None = None,
    ) -> None:
        item = QStandardItem()
        item.setData(str(path), _ROLE_PATH)
        item.setData(float(mtime), _ROLE_MTIME)
        item.setData(int(size_bytes), _ROLE_SIZE)
        item.setData(float(duration_secs) if duration_secs else None, _ROLE_DURATION)
        item.setData(thumb_path or "", _ROLE_THUMB)
        self._model.appendRow(item)

    def update_duration(self, path: Path | str, seconds: float | None) -> None:
        row = self._row_for(path)
        if row < 0:
            return
        self._model.item(row).setData(
            float(seconds) if seconds is not None else None, _ROLE_DURATION
        )
        self._refresh_row(row)

    def update_thumbnail(self, path: Path | str, thumb_path: str | None) -> None:
        row = self._row_for(path)
        if row < 0:
            return
        self._model.item(row).setData(thumb_path or "", _ROLE_THUMB)
        self._refresh_row(row)

    def selected_path(self) -> Path | None:
        """Path of the *current* (most-recently-clicked) row, or None."""
        idx = self.currentIndex()
        if not idx.isValid():
            return None
        p = idx.data(_ROLE_PATH)
        return Path(p) if p else None

    def selected_paths(self) -> list[Path]:
        """All currently-selected paths, in row order."""
        sel = self.selectionModel().selectedIndexes()
        # selectedIndexes returns one per column; we only have column 0, but
        # belt-and-braces: dedupe by row.
        seen_rows: set[int] = set()
        out: list[Path] = []
        for idx in sorted(sel, key=lambda i: i.row()):
            if idx.row() in seen_rows:
                continue
            seen_rows.add(idx.row())
            p = idx.data(_ROLE_PATH)
            if p:
                out.append(Path(p))
        return out

    def remove_path(self, path: Path | str) -> bool:
        """Remove the row for ``path`` from the model. Returns True if removed."""
        row = self._row_for(path)
        if row < 0:
            return False
        self._model.removeRow(row)
        return True

    # -------------------------------------------------------- internals
    def _row_for(self, path: Path | str) -> int:
        target = str(path)
        for r in range(self._model.rowCount()):
            if self._model.item(r).data(_ROLE_PATH) == target:
                return r
        return -1

    def _refresh_row(self, row: int) -> None:
        idx = self._model.index(row, 0)
        self.update(idx)

    def _emit_selection(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            self.selected_path_changed.emit(None)
            return
        p = current.data(_ROLE_PATH)
        self.selected_path_changed.emit(Path(p) if p else None)

    def _on_context_menu(self, pos: QPoint) -> None:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        # If the user right-clicked an unselected row, treat that one item as
        # the action target (matches Explorer behaviour). If they right-clicked
        # one of an existing multi-selection, operate on the whole selection.
        selected = self.selected_paths()
        right_clicked = idx.data(_ROLE_PATH)
        if right_clicked and Path(right_clicked) not in selected:
            self.setCurrentIndex(idx)
            selected = [Path(right_clicked)]
        if not selected:
            return
        single = len(selected) == 1

        menu = QMenu(self)
        play_action = None
        rename_action = None
        reveal_action = None
        export_action = None
        upload_action = None
        repair_action = None
        if single:
            play_action = menu.addAction("Play")
            menu.addSeparator()
            rename_action = menu.addAction("Rename…")
            reveal_action = menu.addAction("Open file location")
            export_action = menu.addAction("Export clip…")
            upload_action = menu.addAction("Upload to YouTube…")
            repair_action = menu.addAction("Repair recording…")
            menu.addSeparator()
        delete_label = (
            "Delete" if single else f"Delete {len(selected)} files"
        )
        delete_action = menu.addAction(delete_label)
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == delete_action:
            self.delete_requested.emit(selected)
        elif single and chosen == play_action:
            self.play_requested.emit(selected[0])
        elif single and chosen == reveal_action:
            self.reveal_in_explorer_requested.emit(selected[0])
        elif single and chosen == rename_action:
            self.rename_requested.emit(selected[0])
        elif single and chosen == export_action:
            self.export_requested.emit(selected[0])
        elif single and chosen == upload_action:
            self.upload_to_youtube_requested.emit(selected[0])
        elif single and chosen == repair_action:
            self.repair_requested.emit(selected[0])

    def _on_delete_shortcut(self) -> None:
        selected = self.selected_paths()
        if selected:
            self.delete_requested.emit(selected)


