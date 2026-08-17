"""Animated desktop window used as a deterministic game-capture target."""

from __future__ import annotations

import tkinter as tk


root = tk.Tk()
root.title("Momento Mock Game")
root.geometry("960x540")
root.minsize(640, 360)

canvas = tk.Canvas(root, background="#14121a", highlightthickness=0)
canvas.pack(fill="both", expand=True)

title = canvas.create_text(
    480,
    96,
    text="Momento capture qualification",
    fill="#ffffff",
    font=("Segoe UI", 26, "bold"),
)
block = canvas.create_rectangle(80, 200, 240, 360, fill="#7c3aed", outline="#c4b5fd", width=4)
counter = canvas.create_text(
    480,
    430,
    text="Frame 0",
    fill="#d8b4fe",
    font=("Segoe UI", 18),
)

frame = 0


def animate() -> None:
    global frame
    frame += 1
    width = max(640, canvas.winfo_width())
    height = max(360, canvas.winfo_height())
    x = 80 + (frame * 7) % max(1, width - 320)
    y = height // 2
    canvas.coords(block, x, y - 80, x + 160, y + 80)
    canvas.coords(title, width // 2, max(60, height // 6))
    canvas.coords(counter, width // 2, max(300, height - 70))
    canvas.itemconfigure(counter, text=f"Frame {frame}")
    root.after(16, animate)


root.after(16, animate)
root.mainloop()
