"""Accessible in-app guide for configuring a user-owned YouTube API project."""

from __future__ import annotations

from urllib.parse import urlsplit

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

_ALLOWED_HELP_HOSTS = {
    "console.cloud.google.com",
    "developers.google.com",
    "support.google.com",
}


class YouTubeSetupDialog(QDialog):
    """Explain Google setup and optionally hand control back to import."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up YouTube uploads")
        self.setMinimumSize(660, 600)
        self.import_requested = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        heading = QLabel("Use your own Google Cloud project")
        heading.setStyleSheet("font-size: 18px; font-weight: 700;")
        heading.setAccessibleName("Use your own Google Cloud project")
        layout.addWidget(heading)

        intro = QLabel(
            "Momento never ships another person's Google project. Set up your "
            "own once, then uploads work through the normal Momento dialog."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        guide = QTextBrowser()
        guide.setOpenExternalLinks(False)
        guide.setOpenLinks(False)
        guide.setAccessibleName("YouTube setup instructions")
        guide.setHtml(
            """
            <ol>
              <li><a href="https://console.cloud.google.com/">Open Google Cloud</a>
                  and create or select a project.</li>
              <li>Open <b>APIs &amp; Services</b>, then enable
                  <b>YouTube Data API v3</b>.</li>
              <li>Open <b>Google Auth Platform</b>. Complete Branding, choose an
                  <b>External</b> audience, and add your own Google account as a
                  test user.</li>
              <li>Under Data Access, add only
                  <code>youtube.upload</code> and <code>youtube.readonly</code>.</li>
              <li>Under Clients, create an OAuth client with application type
                  <b>Desktop app</b>, then download its JSON file.</li>
              <li>Return here and choose <b>Import OAuth JSON</b>. Momento stores
                  a DPAPI-protected copy for this Windows account.</li>
              <li>Back in YouTube Settings, choose <b>Connect YouTube account</b>
                  and finish Google's browser sign-in.</li>
            </ol>
            <p><b>Testing mode:</b> Google normally expires Testing-mode
            authorization after seven days, so you may need to reconnect.</p>
            <p><b>Upload visibility:</b> Google can force uploads from an
            unverified API project to Private, even when Public or Unlisted was
            requested.</p>
            <p>Keep the downloaded JSON private. After Momento imports it, you
            may delete that downloaded copy. Removing setup in Momento deletes
            only Momento's protected local copy; Google-side access can be
            revoked separately from your Google account.</p>
            <p><a href="https://developers.google.com/youtube/v3/guides/auth/installed-apps">
            Read Google's installed-app OAuth guide</a></p>
            """
        )
        guide.anchorClicked.connect(self._open_help_link)
        layout.addWidget(guide, 1)

        import_button = QPushButton("Import OAuth JSON…")
        import_button.setMinimumHeight(38)
        import_button.setAccessibleName("Import Google OAuth JSON")
        import_button.setAccessibleDescription(
            "Choose the Desktop app OAuth JSON downloaded from Google Cloud"
        )
        import_button.clicked.connect(self._request_import)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(import_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _request_import(self) -> None:
        self.import_requested = True
        self.accept()

    def _open_help_link(self, url: QUrl) -> None:
        value = url.toString()
        try:
            parsed = urlsplit(value)
        except ValueError:
            return
        if (
            parsed.scheme == "https"
            and parsed.hostname in _ALLOWED_HELP_HOSTS
            and parsed.username is None
            and parsed.password is None
        ):
            QDesktopServices.openUrl(url)
