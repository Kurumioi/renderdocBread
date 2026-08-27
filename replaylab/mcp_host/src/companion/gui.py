"""Minimal tkinter shell around the asynchronous companion engine."""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Awaitable, Callable


Backend = Callable[
    [Callable[[], Awaitable[str | None]], Callable[[str, str], None]],
    Awaitable[None],
]


def run_gui(backend: Backend) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as error:  # pragma: no cover - platform packaging path
        raise RuntimeError("tkinter is not available in this Python installation") from error

    root = tk.Tk()
    root.title("RenderDoc Ultra AI Companion")
    root.geometry("920x680")

    frame = ttk.Frame(root, padding=8)
    frame.pack(fill="both", expand=True)
    transcript = tk.Text(frame, wrap="word", state="disabled")
    transcript.pack(fill="both", expand=True)
    transcript.tag_configure("user", foreground="#3b67c7")
    transcript.tag_configure("assistant", foreground="#176b39")
    transcript.tag_configure("system", foreground="#7a4b00")

    bottom = ttk.Frame(frame)
    bottom.pack(fill="x", pady=(8, 0))
    entry = ttk.Entry(bottom)
    entry.pack(side="left", fill="x", expand=True)
    send_button = ttk.Button(bottom, text="Send")
    send_button.pack(side="left", padx=(8, 0))
    status = tk.StringVar(value="Starting external companion...")
    ttk.Label(frame, textvariable=status).pack(fill="x", pady=(6, 0))

    requests: queue.Queue[str | None] = queue.Queue()
    updates: queue.Queue[tuple[str, str]] = queue.Queue()
    closed = threading.Event()

    def append(role: str, message: str) -> None:
        updates.put((str(role), str(message)))

    def drain_updates() -> None:
        if closed.is_set():
            return
        while True:
            try:
                role, message = updates.get_nowait()
            except queue.Empty:
                break
            transcript.configure(state="normal")
            label = {"user": "You", "assistant": "AI", "system": "System"}.get(
                role, role
            )
            transcript.insert("end", "%s: %s\n\n" % (label, message), role)
            transcript.configure(state="disabled")
            transcript.see("end")
            status.set(message.splitlines()[0][:180] if message else "Ready")
        root.after(50, drain_updates)

    async def receive() -> str | None:
        return await asyncio.to_thread(requests.get)

    def submit(_event=None) -> None:
        value = entry.get().strip()
        if not value:
            return
        entry.delete(0, "end")
        append("user", value)
        requests.put(value)

    send_button.configure(command=submit)
    entry.bind("<Return>", submit)

    def worker() -> None:
        try:
            asyncio.run(backend(receive, append))
        except Exception as error:  # final startup/runtime boundary
            append("system", "Companion stopped: %s" % error)

    thread = threading.Thread(target=worker, name="renderdoc-ai-companion", daemon=True)
    thread.start()

    def close() -> None:
        closed.set()
        requests.put(None)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.after(0, drain_updates)
    entry.focus_set()
    root.mainloop()
    return 0
