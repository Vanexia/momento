"""Round-trip test: window geometry survives a close + reopen."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from momento.config import load_config
from momento.ui import editor as editor_module
from momento.ui.theme import apply_dark_theme

EditorWindow = editor_module.EditorWindow


def main() -> int:
    temp_workspace = tempfile.TemporaryDirectory()
    temp_root = Path(temp_workspace.name)
    p = temp_root / "window_state.ini"
    editor_module.window_state_path = lambda: p
    lines: list[str] = [f"settings file: {p}"]
    if p.exists():
        p.unlink()
        lines.append("cleared old state")

    app = QApplication([])
    apply_dark_theme(app)
    cfg = load_config()
    cfg.output_folder = temp_root / "recordings"
    cfg.output_folder.mkdir()

    # Use a size that also fits Qt's 800x800 offscreen test display, and close
    # each editor normally so its media and worker objects finish teardown.
    cfg.close_to_tray = False
    ed = EditorWindow(cfg)
    ed.resize(640, 480)
    ed.move(23, 34)
    ed._save_window_state()
    g0 = ed.geometry()
    lines.append(f"saved: {g0.width()}x{g0.height()} @ ({g0.x()},{g0.y()})")
    ed.close()
    app.processEvents()

    ed2 = EditorWindow(cfg)
    g = ed2.geometry()
    lines.append(f"restored: {g.width()}x{g.height()} @ ({g.x()},{g.y()})")

    ok = g.width() == 640 and g.height() == 480
    lines.append("PASS" if ok else "FAIL")
    ed2.close()
    app.processEvents()
    temp_workspace.cleanup()
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
