"""GUI with production services over loopback, not the paired demo.

Presence travels through real UDP sockets using unicast and separate test ports.
This does not verify LAN broadcast, visible-window focus, or two physical PCs.
Only ChatWindow consumes each Application's event queue.
"""

from functools import partial
import gc
import hashlib
from pathlib import Path
import socket
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from src.chat import ChatService
from src.file_transfer import FileTransferManager
from src.gui import ChatWindow
from src.main import Application, off_ui_thread


def available_port(ip, kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as reservation:
        reservation.bind((ip, 0))
        return reservation.getsockname()[1]


class GuiIntegrationTests(unittest.TestCase):
    TERMINAL_TRANSFERS = {"transfer_completed", "transfer_failed", "transfer_cancelled"}

    def setUp(self):
        self.apps = []
        self.windows = []
        self.releases = []
        self.tk_root = None
        self.initial_threads = set(threading.enumerate())
        self.temporary = tempfile.TemporaryDirectory()
        self.downloads = Path(self.temporary.name)
        # unittest runs cleanups even if setup or a network assertion fails.
        self.addCleanup(self.cleanup_services)
        self.tk_root = tk.Tk()
        self.tk_root.withdraw()
        self.alice, self.left = self.start_peer("Alice", "127.0.0.2")
        self.bob, self.right = self.start_peer("Bob", "127.0.0.1")
        self.aid = self.alice.local_peer["peer_id"]
        self.bid = self.bob.local_peer["peer_id"]
        self.announce(self.alice, self.bob)
        self.announce(self.bob, self.alice)
        self.pump_until(lambda: self.left.peer_tree.exists(self.bid)
                        and self.right.peer_tree.exists(self.aid))
        self.left.select_peer(self.bid)
        self.right.select_peer(self.aid)
        self.pump_until(lambda: self.bid in self.left._conversations
                        and self.aid in self.right._conversations)

    def start_peer(self, name, ip):
        port = available_port(ip)
        local = {"peer_id": f"{ip}:{port}", "name": name, "ip": ip,
                 "tcp_port": port, "status": "online"}
        app = Application(local, ChatService, partial(
            FileTransferManager, downloads_dir=self.downloads / name))
        self.apps.append(app)
        # Discovery binds all interfaces, so reserve a distinct UDP port globally.
        app.discovery.udp_port = available_port("", socket.SOCK_DGRAM)
        off_ui_thread(app.start)
        top = tk.Toplevel(self.tk_root)
        top.withdraw()
        window = ChatWindow(top, name, app.events, app.actions())
        self.windows.append(window)
        self.assertIs(window.events, app.events)
        return app, window

    def announce(self, source, target):
        # Local test routing only; no fake peer_upsert/conversation events.
        with source.discovery._lock:
            peer = dict(source.discovery.local_peer)
        message = f"HELLO|{peer['name']}|{peer['tcp_port']}|{peer['status']}"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.bind((peer["ip"], 0))
            sender.sendto(message.encode("utf-8"),
                          (target.local_peer["ip"], target.discovery.udp_port))

    def pump_until(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.tk_root.update()
            if time.monotonic() >= deadline:
                notices = [window.notice_var.get() for window in self.windows
                           if not window._destroyed]
                self.fail(f"GUI integration timed out; notices: {notices}")
            time.sleep(0.005)

    def send_from_gui(self, sender, receiver, text):
        sender.draft_var.set(text)
        sender._on_send()
        self.pump_until(lambda: text in receiver.chat_text.get("1.0", "end")
                        and not sender._pending_send)
        self.assertEqual(sender.draft_var.get(), "")

    def cleanup_services(self):
        errors = []
        sockets = []
        workers = set(threading.enumerate()) - self.initial_threads
        for release in self.releases:
            release.set()
        for app in self.apps:
            sockets.extend(sock for sock in (
                app.discovery._receiver, app.discovery._sender, app.chat._listener)
                if sock is not None)
            with app.chat._lock:
                sockets.extend(app.chat._sockets)
            with app.files._lock:
                sockets.extend(job.sock for job in app.files._jobs.values()
                               if job.sock is not None)
        try:
            for window in self.windows:
                window.close()
            if self.windows:
                try:
                    self.pump_until(lambda: all(window._destroyed for window in self.windows))
                except Exception as error:
                    errors.append(str(error))
            # Also cover partial setup where no window owns the service yet.
            for app in reversed(self.apps):
                try:
                    off_ui_thread(app.shutdown)
                except Exception as error:
                    errors.append(str(error))
            for worker in workers:
                worker.join(1)
            alive = [worker.name for worker in threading.enumerate()
                     if worker not in self.initial_threads]
            if alive:
                errors.append(f"Workers survived shutdown: {alive}")
            if any(sock.fileno() != -1 for sock in sockets):
                errors.append("Sockets survived shutdown")
        finally:
            if self.tk_root is not None:
                self.tk_root.destroy()
            self.windows.clear()
            self.left = self.right = None
            window = None
            self.tk_root = None
            # Finalize Tk wrappers on their owning thread before another test's
            # network workers can trigger cyclic GC.
            gc.collect()
            self.temporary.cleanup()
        self.assertEqual(errors, [], "; ".join(errors))

    def test_udp_status_two_way_chat_draft_and_third_peer_unread(self):
        peer = self.left._peers[self.bid]
        self.assertEqual((peer["name"], peer["ip"], peer["tcp_port"], peer["status"]),
                         ("Bob", "127.0.0.1", self.bob.local_peer["tcp_port"], "online"))
        self.send_from_gui(self.left, self.right, "Chào Bob qua TCP thật 🙂")
        self.left.draft_var.set("Bản nháp đang gõ")
        self.left.message_entry.icursor(4)
        focus = self.tk_root.focus_get()
        self.send_from_gui(self.right, self.left, "Alice ơi, đã nhận!")
        self.assertEqual(self.left.draft_var.get(), "Bản nháp đang gõ")
        self.assertEqual(self.left.message_entry.index("insert"), 4)
        self.assertEqual(self.tk_root.focus_get(), focus)

        carol, third = self.start_peer("Carol", "127.0.0.1")
        cid = carol.local_peer["peer_id"]
        self.announce(carol, self.alice)
        self.announce(self.alice, carol)
        self.pump_until(lambda: self.left.peer_tree.exists(cid)
                        and third.peer_tree.exists(self.aid))
        third.select_peer(self.aid)
        third.draft_var.set("Tin TCP từ peer thứ ba")
        third._on_send()
        self.pump_until(lambda: self.left._conversations.get(cid, {}).get("unread") == 1
                        and not third._pending_send)
        self.assertEqual(str(self.left.peer_tree.item(cid, "values")[-1]), "1")
        self.assertEqual(self.left._selected_peer, self.bid)
        self.assertEqual(self.left.draft_var.get(), "Bản nháp đang gõ")

        self.right.status_var.set("away")
        self.right._on_status()
        self.pump_until(lambda: self.right._local_status == "away"
                        and self.bob.discovery.local_peer["status"] == "away")
        self.announce(self.bob, self.alice)
        self.pump_until(lambda: self.left._peers[self.bid]["status"] == "away")
        self.assertIn("Tạm vắng", self.left.conversation_detail.get())
        self.assertEqual(self.left.peer_tree.item(self.bid, "values")[2], "away")

        self.left.select_peer(cid)
        self.pump_until(lambda: self.left._conversations[cid]["unread"] == 0)
        self.assertEqual(str(self.left.peer_tree.item(cid, "values")[-1]), "")
        self.assertIn("Tin TCP từ peer thứ ba", self.left.chat_text.get("1.0", "end"))
        self.left.select_peer(self.bid)
        self.assertEqual(self.left.draft_var.get(), "Bản nháp đang gõ")
        self.assertIn("Alice ơi, đã nhận!", self.left.chat_text.get("1.0", "end"))

    def test_file_from_gui_overlaps_real_tcp_chat_and_keeps_gui_responsive(self):
        self.send_from_gui(self.left, self.right, "Chuẩn bị gửi file")
        source = self.downloads / "tệp-gui.bin"
        body = bytes(range(256)) * 32768  # 8 MiB, only in TemporaryDirectory.
        source.write_bytes(body)
        expected_hash = hashlib.sha256(body).hexdigest()
        receiving = threading.Event()
        release = threading.Event()
        self.releases.append(release)
        original_progress = self.bob.files._progress

        def pause_mid_body(job, **kwargs):
            original_progress(job, **kwargs)
            if 0 < job.bytes_done < job.total_bytes and not receiving.is_set():
                receiving.set()
                if not release.wait(10):
                    raise TimeoutError("Test did not release the in-progress transfer")

        # Coordinate real I/O; do not replace send/receive or synthesize events.
        with patch.object(self.bob.files, "_progress", side_effect=pause_mid_body):
            try:
                with patch("src.gui.filedialog.askopenfilename", return_value=str(source)):
                    self.left._on_file()
                self.pump_until(lambda: receiving.is_set() and bool(self.right._transfers)
                                and bool(self.left._transfers))
                tid, progress = next(iter(self.right._transfers.items()))
                self.assertGreater(progress["percent"], 0)
                self.assertLess(progress["percent"], 100)
                self.assertIn(tid, self.bob.files.active_transfer_ids)
                self.assertIn(tid, self.alice.files.active_transfer_ids)

                self.left.draft_var.set("Vừa truyền vừa gõ")
                self.send_from_gui(self.right, self.left, "TCP chat khi file đang truyền")
                self.assertEqual(self.left.draft_var.get(), "Vừa truyền vừa gõ")
                self.send_from_gui(self.left, self.right, "Trả lời khi file chưa xong")
                ticks = []
                self.tk_root.after(0, lambda: ticks.append(True))
                self.pump_until(lambda: bool(ticks))
                self.assertFalse(release.is_set())
                self.assertIn(tid, self.bob.files.active_transfer_ids)
                for window in (self.left, self.right):
                    self.assertNotIn(window._transfers[tid]["type"], self.TERMINAL_TRANSFERS)
            finally:
                release.set()
            self.pump_until(lambda: all(window._transfers[tid]["type"]
                            in self.TERMINAL_TRANSFERS for window in (self.left, self.right)))

        for window in (self.left, self.right):
            result = window._transfers[tid]
            self.assertEqual(result["type"], "transfer_completed", result)
            self.assertEqual(result["sha256"], expected_hash)
            self.assertEqual(result["percent"], 100)
            self.assertEqual(result["bytes_done"], len(body))
            self.assertEqual(window.progress_var.get(), 100)
            self.assertEqual(window.transfer_tree.item(tid, "values")[-1], "Hoàn tất")
            self.assertFalse(window._destroyed)
        destination = self.bob.files.downloads_dir / source.name
        self.assertEqual(destination.read_bytes(), body)
        self.assertEqual(list(self.bob.files.downloads_dir.glob(".lan-*.part")), [])
        self.assertIn("disabled", self.left.save_button.state())
        self.assertNotIn("disabled", self.right.save_button.state())
        saved_copy = self.downloads / "bản-lưu-do-người-nhận-chọn.bin"
        with patch("src.gui.filedialog.asksaveasfilename", return_value=str(saved_copy)):
            self.right.save_button.invoke()
        self.pump_until(lambda: not self.right._saving_file)
        self.assertEqual(saved_copy.read_bytes(), body)
        self.assertEqual(hashlib.sha256(saved_copy.read_bytes()).hexdigest(), expected_hash)
        self.assertEqual(destination.read_bytes(), body)
        self.assertIn(str(saved_copy), self.right.notice_var.get())


if __name__ == "__main__":
    unittest.main()
