"""Round-trip test: window geometry survives a close + reopen."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from momento.config import load_config
from momento.ui.editor import EditorWindow
from momento.ui.theme import apply_dark_theme
from momento.util.paths import window_state_path


def main() -> int:
    p = window_state_path()
    out_file = Path(__file__).resolve().parent / "smoke_window_state.out"
    lines: list[str] = [f"settings file: {p}"]
    if p.exists():
        p.unlink()
        lines.append("cleared old state")

    app = QApplication([])
    apply_dark_theme(app)
    cfg = load_config()

    ed = EditorWindow(cfg)
    ed.resize(900, 500)
    ed.move(123, 234)
    ed._save_window_state()
    g0 = ed.geometry()
    lines.append(f"saved: {g0.width()}x{g0.height()} @ ({g0.x()},{g0.y()})")

    ed2 = EditorWindow(cfg)
    g = ed2.geometry()
    lines.append(f"restored: {g.width()}x{g.height()} @ ({g.x()},{g.y()})")

    ok = g.width() == 900 and g.height() == 500
    lines.append("PASS" if ok else "FAIL")
    out_file.write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
