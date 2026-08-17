"""Upload-to-YouTube modal — collects title/description/tags/privacy/thumbnail.

Pre-fills sensible defaults from the clip filename + the user's saved
defaults in Config. The actual upload runs from
``ui/youtube_upload_progress.py`` on a worker thread — this dialog only
gathers inputs and validates them, then accepts/rejects.

Validation matches YouTube's hard limits so users don't get a 400 from
the API:
- Title: 1..100 chars
- Description: 0..5000 chars
- Tags total length: 0..500 chars (joined, comma-separated; YouTube counts
  the bytes after serialisation including the quote characters)
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from momento.config import Config
from momento.core.game_names import friendly_recording_title
from momento.ui.widgets import AnchoredComboBox
from momento.util.format import format_bytes
from momento.youtube.uploader import UploadOptions

# Subset of YouTube's video categories most relevant to a game recorder.
# Full list: https://developers.google.com/youtube/v3/docs/videoCategories/list
# Order roughly by frequency-of-use for gaming clips.
_CATEGORIES: list[tuple[int, str]] = [
    (20, "Gaming"),
    (24, "Entertainment"),
    (23, "Comedy"),
    (22, "People & Blogs"),
    (17, "Sports"),
    (10, "Music"),
    (1, "Film & Animation"),
    (27, "Education"),
    (28, "Science & Technology"),
]

_PRIVACY_OPTIONS: list[tuple[str, str]] = [
    ("public", "Public — anyone can find it"),
    ("unlisted", "Unlisted — only people with the link"),
    ("private", "Private — only you can see it"),
]

_MAX_TITLE = 100
_MAX_DESC = 5000
_MAX_TAGS_TOTAL = 500


class YouTubeUploadDialog(QDialog):
    """Modal that returns an ``UploadOptions`` via :py:meth:`get_options`."""

    def __init__(
        self,
        clip_path: Path,
        config: Config,
        channel_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._clip_path = clip_path
        self._config = config

        self.setWindowTitle("Upload to YouTube")
        self.setMinimumWidth(560)
        self.setModal(True)

        # ------ Form ------
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self._title_edit = QLineEdit(self)
        self._title_edit.setMaxLength(_MAX_TITLE)
        self._title_edit.setText(friendly_recording_title(clip_path.name))
        self._title_edit.setPlaceholderText("Title (required)")
        form.addRow("Title", self._title_edit)

        self._desc_edit = QPlainTextEdit(self)
        self._desc_edit.setPlaceholderText(
            "Description — what's in the clip, any context, links, credits…"
        )
        self._desc_edit.setFixedHeight(140)
        form.addRow("Description", self._desc_edit)

        self._tags_edit = QLineEdit(self)
        self._tags_edit.setText(config.youtube_default_tags)
        self._tags_edit.setPlaceholderText("comma, separated, tags")
        form.addRow("Tags", self._tags_edit)

        # AnchoredComboBox keeps the popup pinned directly below the field
        # instead of Windows-default jumping so the selected row lands under
        # the cursor — matches every other combo in the app per CLAUDE.md.
        self._category_combo = AnchoredComboBox(self)
        for cat_id, label in _CATEGORIES:
            self._category_combo.addItem(label, userData=cat_id)
        self._select_category(config.youtube_default_category)
        form.addRow("Category", self._category_combo)

        self._privacy_combo = AnchoredComboBox(self)
        for value, label in _PRIVACY_OPTIONS:
            self._privacy_combo.addItem(label, userData=value)
        self._select_privacy(config.youtube_default_privacy)
        form.addRow("Privacy", self._privacy_combo)

        # ------ Thumbnail picker (optional) ------
        thumb_row = QHBoxLayout()
        thumb_row.setContentsMargins(0, 0, 0, 0)
        self._thumb_edit = QLineEdit(self)
        self._thumb_edit.setReadOnly(True)
        self._thumb_edit.setPlaceholderText("Optional — defaults to YouTube auto-pick")
        thumb_browse = QPushButton("Browse…", self)
        thumb_browse.setAutoDefault(False)
        thumb_clear = QPushButton("Clear", self)
        thumb_clear.setAutoDefault(False)
        thumb_browse.clicked.connect(self._on_browse_thumbnail)
        thumb_clear.clicked.connect(lambda: self._thumb_edit.clear())
        thumb_row.addWidget(self._thumb_edit, 1)
        thumb_row.addWidget(thumb_browse)
        thumb_row.addWidget(thumb_clear)
        thumb_container = QWidget(self)
        thumb_container.setLayout(thumb_row)
        form.addRow("Thumbnail", thumb_container)

        # ------ Header strip ------
        header = QLabel(self)
        header.setWordWrap(True)
        header.setTextFormat(Qt.TextFormat.RichText)
        size = clip_path.stat().st_size if clip_path.is_file() else 0
        header.setText(
            f"<b>Uploading to:</b> {channel_name or 'YouTube'}<br>"
            f"<b>File:</b> {clip_path.name} "
            f"<span style='color:#888'>· {format_bytes(size)}</span>"
        )

        # ------ Char counters under title + description ------
        self._title_count = QLabel(self)
        self._desc_count = QLabel(self)
        self._tags_count = QLabel(self)
        for lbl in (self._title_count, self._desc_count, self._tags_count):
            lbl.setStyleSheet("color: #888; font-size: 11px;")

        self._title_edit.textChanged.connect(self._update_title_count)
        self._desc_edit.textChanged.connect(self._update_desc_count)
        self._tags_edit.textChanged.connect(self._update_tags_count)
        self._update_title_count()
        self._update_desc_count()
        self._update_tags_count()

        counter_row = QFormLayout()
        counter_row.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        counter_row.addRow("", self._title_count)
        counter_row.addRow("", self._desc_count)
        counter_row.addRow("", self._tags_count)

        # ------ Buttons ------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel, self
        )
        self._upload_btn = QPushButton("Upload", self)
        self._upload_btn.setDefault(True)
        buttons.addButton(self._upload_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        self._upload_btn.clicked.connect(self._on_upload_clicked)
        buttons.rejected.connect(self.reject)

        # ------ Compose ------
        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addLayout(form)
        layout.addLayout(counter_row)
        layout.addStretch(1)
        layout.addWidget(buttons)

    # ---- Public API ------------------------------------------------------

    def get_options(self) -> UploadOptions:
        """Return the user's choices as an UploadOptions. Call after accept()."""
        tags_raw = self._tags_edit.text().strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        thumb_text = self._thumb_edit.text().strip()
        thumb_path = Path(thumb_text) if thumb_text else None

        return UploadOptions(
            file_path=self._clip_path,
            title=self._title_edit.text().strip(),
            description=self._desc_edit.toPlainText(),
            tags=tags,
            category_id=int(self._category_combo.currentData()),
            privacy=str(self._privacy_combo.currentData()),
            thumbnail_path=thumb_path,
        )

    def updated_config_defaults(self) -> Config:
        """Return a Config with the dialog's privacy/category/tags persisted
        as the new defaults. Caller decides whether to actually save."""
        return replace(
            self._config,
            youtube_default_privacy=str(self._privacy_combo.currentData()),
            youtube_default_category=int(self._category_combo.currentData()),
            youtube_default_tags=self._tags_edit.text().strip(),
        )

    # ---- Helpers ---------------------------------------------------------

    def _select_category(self, cat_id: int) -> None:
        for i in range(self._category_combo.count()):
            if self._category_combo.itemData(i) == cat_id:
                self._category_combo.setCurrentIndex(i)
                return
        self._category_combo.setCurrentIndex(0)  # default Gaming

    def _select_privacy(self, value: str) -> None:
        for i in range(self._privacy_combo.count()):
            if self._privacy_combo.itemData(i) == value:
                self._privacy_combo.setCurrentIndex(i)
                return
        self._privacy_combo.setCurrentIndex(1)  # default Unlisted

    def _on_browse_thumbnail(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a thumbnail",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg)",
        )
        if path:
            self._thumb_edit.setText(path)

    def _update_title_count(self) -> None:
        n = len(self._title_edit.text())
        self._title_count.setText(f"Title: {n} / {_MAX_TITLE}")
        if n == 0:
            self._title_count.setStyleSheet("color: #d27; font-size: 11px;")
        else:
            self._title_count.setStyleSheet("color: #888; font-size: 11px;")

    def _update_desc_count(self) -> None:
        n = len(self._desc_edit.toPlainText())
        col = "#d27" if n > _MAX_DESC else "#888"
        self._desc_count.setText(f"Description: {n} / {_MAX_DESC}")
        self._desc_count.setStyleSheet(f"color: {col}; font-size: 11px;")

    def _update_tags_count(self) -> None:
        # YouTube counts the comma-serialised bytes including the quoting
        # they apply server-side, but the conservative measure of the raw
        # joined string length matches their effective limit in practice.
        raw = self._tags_edit.text()
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        joined_len = sum(len(t) for t in tags) + max(0, len(tags) - 1) * 2  # ", "
        col = "#d27" if joined_len > _MAX_TAGS_TOTAL else "#888"
        self._tags_count.setText(f"Tags: {joined_len} / {_MAX_TAGS_TOTAL}")
        self._tags_count.setStyleSheet(f"color: {col}; font-size: 11px;")

    def _on_upload_clicked(self) -> None:
        title = self._title_edit.text().strip()
        desc = self._desc_edit.toPlainText()
        tags = [t.strip() for t in self._tags_edit.text().split(",") if t.strip()]
        tags_len = sum(len(t) for t in tags) + max(0, len(tags) - 1) * 2

        if not title:
            QMessageBox.warning(self, "Title required", "YouTube requires a title.")
            self._title_edit.setFocus()
            return
        if len(title) > _MAX_TITLE:
            QMessageBox.warning(self, "Title too long",
                                f"Maximum {_MAX_TITLE} characters.")
            return
        if len(desc) > _MAX_DESC:
            QMessageBox.warning(self, "Description too long",
                                f"Maximum {_MAX_DESC} characters.")
            return
        if tags_len > _MAX_TAGS_TOTAL:
            QMessageBox.warning(self, "Tags too long",
                                f"Combined tag length is {tags_len} characters; "
                                f"YouTube limit is {_MAX_TAGS_TOTAL}.")
            return

        thumb_text = self._thumb_edit.text().strip()
        if thumb_text and not Path(thumb_text).is_file():
            QMessageBox.warning(self, "Thumbnail not found",
                                "Chosen thumbnail file no longer exists.")
            return

        self.accept()
