"""Recordings-folder picker shared by setup and Settings."""

from pathlib import Path

from PyQt6.QtCore import QSettings, QStandardPaths, QUrl
from PyQt6.QtWidgets import QFileDialog, QWidget

from momento.util import windows_api
from momento.util.paths import window_state_path


def choose_recordings_folder(
    parent: QWidget, current: str, *, title: str = "Choose recordings folder"
) -> str | None:
    """Show mounted drives explicitly and return None when cancelled."""
    current = current.strip()
    start = current or str(Path.home())
    dialog = QFileDialog(parent, title, start)
    # The native picker can crash alongside WGC/WASAPI background threads.
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)

    # Drives come first so another disk never depends on finding Qt's hidden
    # "My Computer" root in the location dropdown.
    locations = [str(drive) for drive in windows_api.logical_drives()]
    for location in (
        QStandardPaths.StandardLocation.HomeLocation,
        QStandardPaths.StandardLocation.DesktopLocation,
        QStandardPaths.StandardLocation.DocumentsLocation,
        QStandardPaths.StandardLocation.DownloadLocation,
        QStandardPaths.StandardLocation.MoviesLocation,
        QStandardPaths.StandardLocation.PicturesLocation,
    ):
        locations.extend(QStandardPaths.standardLocations(location))
    if current:
        locations.append(current)
    sidebar = []
    seen: set[str] = set()
    for location in locations:
        key = str(Path(location)).casefold()
        if location and key not in seen:
            sidebar.append(QUrl.fromLocalFile(location))
            seen.add(key)
    dialog.setSidebarUrls(sidebar)

    settings = QSettings(str(window_state_path()), QSettings.Format.IniFormat)
    geometry = settings.value("dialogs/output_folder/geometry")
    if not geometry or not dialog.restoreGeometry(geometry):
        dialog.resize(900, 600)
    try:
        accepted = dialog.exec()
        settings.setValue("dialogs/output_folder/geometry", dialog.saveGeometry())
        selected = dialog.selectedFiles() if accepted else []
        return selected[0] if selected else None
    finally:
        dialog.deleteLater()
