"""Paired local preview for Member 3; chat is relayed in memory, files use TCP.

This module exercises GUI/file integration without implementing Member 2's
ChatService or UDP discovery. It is not evidence of production TCP chat.
"""

from pathlib import Path
import queue
import threading
import time
import tkinter as tk

from .gui import ChatWindow
from .member3_demo import DemoBackend
from .protocol import peer_id


class PairedDemoBackend(DemoBackend):
    def __init__(self, name, downloads):
        self.partner = None
        self._closed = threading.Event()
        local = {"name": name, "ip": "127.0.0.1", "tcp_port": 5000,
                 "peer_id": peer_id("127.0.0.1", 5000), "status": "online"}
        super().__init__(local, queue.Queue(), downloads=downloads,
                         host="127.0.0.1", listen_port=0, sample_peers=False)

    def send_message(self, pid, text):
        partner = self.partner
        if (self._closed.is_set() or partner is None or partner._closed.is_set()
                or pid != partner.local["peer_id"]):
            raise ConnectionError("Cửa sổ bên nhận đã đóng")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Tin nhắn không được rỗng")
        with self._lock:
            self._state(pid)["messages"].append(
                {"text": text, "outgoing": True, "state": "sent"})
            self._publish(pid)
        # Local demo relay only: never pretend that this is TCP chat.
        partner.inject_message(self.local["peer_id"], text)

    def set_status(self, status):
        super().set_status(status)
        with self._lock:
            self.local["status"] = status
        if self.partner is not None and not self.partner._closed.is_set():
            self.partner.add_peer(self.local)

    def shutdown(self):
        super().shutdown()
        if not self._closed.is_set():
            self._closed.set()
            if self.partner is not None and not self.partner._closed.is_set():
                self.partner.events.put({"type": "peer_removed",
                                         "peer_id": self.local["peer_id"]})


def create_demo_pair(downloads=None):
    """Return two real file endpoints linked by an explicit mock chat relay."""
    base = (Path(downloads) if downloads is not None else
            Path(__file__).resolve().parents[1] / "downloads" / "pair-demo")
    first = PairedDemoBackend("Như", base / "nhu")
    try:
        second = PairedDemoBackend("Minh", base / "minh")
    except Exception:
        first.shutdown()
        raise
    first.partner, second.partner = second, first
    first.add_peer(second.local)
    second.add_peer(first.local)
    return first, second


def run_pair_demo(downloads=None):
    """One command opens Như and Minh with the same illustrative conversation."""
    backends = create_demo_pair(downloads)
    root = None
    windows = []
    try:
        first, second = backends
        first.send_message(second.local["peer_id"],
                           "Chào Minh, mình là Như. Bạn thử gửi tệp cho mình nhé!")
        second.send_message(first.local["peer_id"],
                            "Oke, mình thấy tin nhắn rồi. Thử gửi tệp nha ♡")
        root = tk.Tk()
        root.withdraw()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        for number, backend in enumerate(backends):
            top = tk.Toplevel(root)
            window = ChatWindow(
                top, backend.local["name"], backend.events, backend.actions(),
                demo="DEMO CẶP · Chat nội bộ mô phỏng; truyền tệp TCP thật.")
            windows.append(window)
            x = 20 if number == 0 else max(40, screen_w - 950)
            y = 20 if number == 0 else max(60, screen_h - 770)
            top.geometry(f"920x700+{x}+{y}")
            top.title(f"chat.exe — {backend.local['name']} (demo hai cửa sổ)")
            while True:
                try:
                    window.process_event(backend.events.get_nowait())
                except queue.Empty:
                    break
            window.select_peer(backend.partner.local["peer_id"])
            window.notice_var.set(
                f"Hội thoại mẫu Như ↔ Minh. Chọn Tệp để gửi thật; nhận tại {backend.transfers.downloads_dir}")

        def check_closed():
            if all(window._destroyed for window in windows):
                root.destroy()
            else:
                root.after(80, check_closed)

        root.after(80, check_closed)
        root.mainloop()
    finally:
        # Normal shutdown happens through each GUI's command worker. Also
        # clean up startup failures / interrupted previews without orphaning TCP.
        for window in windows:
            if not window._destroyed:
                try:
                    window.close()
                except tk.TclError:
                    pass
        for backend in reversed(backends):
            backend.shutdown()
        if root is not None:
            deadline = time.monotonic() + 6
            while any(window._worker.is_alive() for window in windows):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Demo hai cửa sổ chưa dừng command worker")
                try:
                    root.update()
                except tk.TclError:
                    pass
                time.sleep(0.01)
            try:
                root.destroy()
            except tk.TclError:
                pass
