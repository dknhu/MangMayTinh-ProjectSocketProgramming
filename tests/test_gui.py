import gc
import hashlib
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from src.gui import ChatWindow, UIActions
from src.file_transfer import FileTransferManager
from src.member3_demo import FileDemoListener
from src.protocol import peer_id


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.events = queue.Queue()
        self.calls = []
        self.call_threads = []

        def callback(name):
            def call(*args):
                self.calls.append((name, args))
                self.call_threads.append(threading.get_ident())
            return call

        self.window = ChatWindow(self.root, "Quan", self.events, UIActions(*[
            callback(name) for name in ("select_peer", "send_message", "send_file", "set_status",
                                       "leave_chat", "cancel_transfer", "shutdown")]))
        self.minh = "127.0.0.1:5001"
        self.lan = "127.0.0.1:5002"
        for pid, name, port in ((self.minh, "Minh", 5001), (self.lan, "Lan", 5002)):
            self.window.process_event({"type": "peer_upsert", "peer": {
                "peer_id": pid, "name": name, "ip": "127.0.0.1", "tcp_port": port, "status": "online"}})

    def pump_until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            self.root.update()
            self.assertLess(time.monotonic(), deadline, "GUI không xử lý xong sự kiện")
            time.sleep(0.005)

    def tearDown(self):
        if not self.window._destroyed:
            self.window.close()
            self.pump_until(lambda: self.window._destroyed)
        self.window._worker.join(2)
        self.assertFalse(self.window._worker.is_alive())
        # Tk wrappers form cycles; collect on their owning thread before
        # a later network worker can trigger cyclic GC.
        self.window = None
        self.root = None
        gc.collect()

    def snapshot(self, pid, revision, text="hello", unread=0):
        return {"type": "conversation", "peer_id": pid, "revision": revision, "unread": unread,
                "messages": [{"text": text, "sender": "Peer"}]}

    def test_incoming_message_keeps_draft_cursor_and_focus(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("tin đang gõ")
        self.window.message_entry.icursor(4)
        old_focus = self.root.focus_get()
        self.window.process_event(self.snapshot(self.minh, 1, "tin mới"))
        self.assertEqual(self.window.draft_var.get(), "tin đang gõ")
        self.assertEqual(self.window.message_entry.index("insert"), 4)
        self.assertEqual(self.root.focus_get(), old_focus)
        self.assertIn("tin mới", self.window.chat_text.get("1.0", "end"))

    def test_styled_multiline_history_keeps_text_and_draft(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("bản nháp chưa gửi")
        messages = [{"text": f"Tin {number} — tiếng Việt ♡\nDòng thứ hai",
                     "outgoing": number % 2 == 1, "state": "sent"}
                    for number in range(500)]
        self.window.process_event({"type": "conversation", "peer_id": self.minh,
                                   "revision": 1, "unread": 0, "messages": messages})
        rendered = self.window.chat_text.get("1.0", "end")
        self.assertIn(messages[0]["text"], rendered)
        self.assertIn(messages[-1]["text"], rendered)
        self.assertEqual(self.window.draft_var.get(), "bản nháp chưa gửi")
        self.assertTrue(self.window.chat_text.tag_ranges("incoming"))
        self.assertTrue(self.window.chat_text.tag_ranges("outgoing"))
        self.assertFalse(self.window.empty_state.place_info())

    def test_switching_conversations_keeps_individual_drafts(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("draft Minh")
        self.window.select_peer(self.lan)
        self.window.draft_var.set("draft Lan")
        self.window.select_peer(self.minh)
        self.assertEqual(self.window.draft_var.get(), "draft Minh")
        self.window.select_peer(self.lan)
        self.assertEqual(self.window.draft_var.get(), "draft Lan")

    def test_unread_is_backend_snapshot_and_stale_revision_ignored(self):
        self.window.process_event(self.snapshot(self.lan, 3, unread=2))
        self.window.process_event(self.snapshot(self.lan, 2, unread=9))
        self.assertEqual(str(self.window.peer_tree.item(self.lan, "values")[-1]), "2")
        self.window.select_peer(self.lan)
        self.pump_until(lambda: ("select_peer", (self.lan,)) in self.calls)
        self.window.process_event(self.snapshot(self.lan, 4, unread=0))
        self.assertEqual(str(self.window.peer_tree.item(self.lan, "values")[-1]), "")

    def test_backend_callback_runs_off_ui_thread_and_clears_only_sent_draft(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("gửi")
        self.window._on_send()
        self.pump_until(lambda: not self.window._pending_send)
        self.assertIn(("send_message", (self.minh, "gửi")), self.calls)
        self.assertEqual(self.window.draft_var.get(), "")
        self.assertTrue(all(tid != threading.get_ident() for tid in self.call_threads))

    def test_edit_while_send_pending_does_not_erase_new_text(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("gửi")
        self.window._on_send()
        self.window.draft_var.set("tin tiếp theo")
        self.pump_until(lambda: not self.window._pending_send)
        self.assertEqual(self.window.draft_var.get(), "tin tiếp theo")

    def test_peer_goes_offline_disables_send_without_losing_draft(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("giữ lại")
        self.window.process_event({"type": "peer_removed", "peer_id": self.minh})
        self.assertFalse(self.window.peer_tree.exists(self.minh))
        self.assertIn("disabled", self.window.send_button.state())
        self.assertEqual(self.window.draft_var.get(), "giữ lại")

    def test_worker_posts_event_but_cannot_touch_widgets(self):
        errors = []

        def background():
            self.window.post_event(self.snapshot(self.lan, 1, unread=2))
            try:
                self.window.process_event(self.snapshot(self.lan, 2))
            except RuntimeError as exc:
                errors.append(str(exc))

        worker = threading.Thread(target=background)
        worker.start()
        worker.join(1)
        self.pump_until(lambda: self.lan in self.window._conversations)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.window._conversations[self.lan]["unread"], 2)

    def test_transfer_progress_and_message_both_processed(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("đang truyền vẫn gõ")
        self.events.put({"type": "transfer_progress", "transfer_id": "test", "peer_id": self.minh,
                         "filename": "file.bin", "direction": "send", "percent": 60,
                         "bytes_done": 6, "total_bytes": 10})
        self.events.put(self.snapshot(self.minh, 1, "chat trong lúc truyền"))
        self.pump_until(lambda: "chat trong lúc truyền" in self.window.chat_text.get("1.0", "end"))
        self.assertEqual(self.window.progress_var.get(), 60)
        self.assertEqual(self.window.draft_var.get(), "đang truyền vẫn gõ")
        self.window.process_event({"type": "transfer_completed", "transfer_id": "test",
                                   "percent": 100})
        self.window.process_event({"type": "transfer_progress", "transfer_id": "test", "percent": 70})
        self.assertEqual(self.window.progress_var.get(), 100)

    def test_leave_conversation_keeps_app_open(self):
        self.window.select_peer(self.minh)
        self.window.draft_var.set("bản nháp")
        self.window._on_leave()
        self.pump_until(lambda: ("leave_chat", (self.minh,)) in self.calls)
        self.assertIsNone(self.window._selected_peer)
        self.assertFalse(self.window._destroyed)
        self.window.select_peer(self.minh)
        self.assertEqual(self.window.draft_var.get(), "bản nháp")

    def test_real_tcp_file_while_gui_receives_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sender = FileTransferManager({"name": "Quan", "ip": "127.0.0.1", "tcp_port": 5009,
                                          "peer_id": "127.0.0.1:5009"}, self.events, base / "sender")
            receiver = FileTransferManager({"name": "Minh", "ip": "127.0.0.1", "tcp_port": 5010,
                                            "peer_id": "127.0.0.1:5010"}, queue.Queue(), base / "receiver")
            listener = FileDemoListener(receiver, "127.0.0.1", 0)
            peer = {"name": "Minh", "ip": "127.0.0.1", "tcp_port": listener.port,
                    "peer_id": peer_id("127.0.0.1", listener.port), "status": "online"}
            original_progress = receiver._progress

            def slower_progress(job, **kwargs):
                original_progress(job, **kwargs)
                time.sleep(0.004)  # Force an overlap with actual UI interaction.

            receiver._progress = slower_progress
            path = base / "large.bin"
            path.write_bytes(bytes(range(256)) * 32768)
            try:
                self.window.process_event({"type": "peer_upsert", "peer": peer})
                self.window.select_peer(peer["peer_id"])
                self.window.draft_var.set("vừa truyền vừa gõ")
                tid = sender.send_file(peer, path)
                self.pump_until(lambda: tid in self.window._transfers)
                self.assertNotEqual(self.window._transfers[tid]["type"], "transfer_completed")
                self.events.put(self.snapshot(peer["peer_id"], 1, "Tin trong khi truyền TCP"))
                self.pump_until(lambda: "Tin trong khi truyền TCP" in self.window.chat_text.get("1.0", "end"))
                self.assertEqual(self.window.draft_var.get(), "vừa truyền vừa gõ")
                self.window.draft_var.set("gửi lúc đang truyền")
                self.window._on_send()
                self.pump_until(lambda: not self.window._pending_send)
                self.assertIn(("send_message", (peer["peer_id"], "gửi lúc đang truyền")), self.calls)
                self.pump_until(lambda: self.window._transfers[tid]["type"] in {
                    "transfer_completed", "transfer_failed"}, timeout=8)
                self.assertEqual(self.window._transfers[tid]["type"], "transfer_completed")
                self.assertEqual((base / "receiver" / "large.bin").read_bytes(), path.read_bytes())
            finally:
                listener.stop()
                sender.stop_transfers()
                receiver.stop_transfers()

    def test_send_failure_preserves_draft(self):
        def fail(*args):
            raise ConnectionError("peer ngắt kết nối")

        self.window.actions.send_message = fail
        self.window.select_peer(self.minh)
        self.window.draft_var.set("tin chưa gửi")
        self.window._on_send()
        self.pump_until(lambda: not self.window._pending_send)
        self.assertEqual(self.window.draft_var.get(), "tin chưa gửi")
        self.assertIn("peer ngắt kết nối", self.window.notice_var.get())

    def test_cancel_file_dialog_does_not_send(self):
        self.window.select_peer(self.minh)
        with patch("src.gui.filedialog.askopenfilename", return_value=""):
            self.window._on_file()
        self.root.update()
        self.assertFalse(any(name == "send_file" for name, args in self.calls))

    def received_file(self, path, body):
        path.write_bytes(body)
        self.window.process_event({
            "type": "transfer_completed", "transfer_id": "received", "peer_id": self.minh,
            "filename": path.name, "direction": "receive", "percent": 100,
            "path": str(path), "sha256": hashlib.sha256(body).hexdigest(),
        })
        self.window.transfer_tree.selection_set("received")
        self.window._on_transfer_selected()

    def test_save_received_file_cancel_and_missing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "received.bin", Path(directory) / "saved.bin"
            self.received_file(source, b"received bytes")
            self.assertNotIn("disabled", self.window.save_button.state())
            with patch("src.gui.filedialog.asksaveasfilename", return_value=""):
                self.window._on_save_received()
            self.assertFalse(self.window._saving_file)
            self.assertFalse(target.exists())
            source.unlink()
            target.write_bytes(b"existing file")
            with patch("src.gui.filedialog.asksaveasfilename", return_value=str(target)):
                self.window._on_save_received()
            self.pump_until(lambda: not self.window._saving_file)
            self.assertIn("Lỗi save_received", self.window.notice_var.get())
            self.assertEqual(target.read_bytes(), b"existing file")

    def test_save_received_file_checks_hash_and_preserves_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "received.bin", Path(directory) / "saved.bin"
            self.received_file(source, b"original")
            source.write_bytes(b"modified after receive")
            target.write_bytes(b"keep this")
            with patch("src.gui.filedialog.asksaveasfilename", return_value=str(target)):
                self.window._on_save_received()
            self.pump_until(lambda: not self.window._saving_file)
            self.assertIn("bị thay đổi", self.window.notice_var.get())
            self.assertEqual(target.read_bytes(), b"keep this")
            self.assertEqual(list(Path(directory).glob(".lan-save-*.part")), [])

    def test_save_received_file_same_path_runs_off_tk_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "received.bin"
            self.received_file(source, b"safe same path")
            original_save = self.window._save_received
            started, release = threading.Event(), threading.Event()
            worker_threads = []

            def save(*args):
                worker_threads.append(threading.get_ident())
                started.set()
                if not release.wait(3):
                    raise TimeoutError("Test did not release copy")
                return original_save(*args)

            with patch.object(self.window, "_save_received", side_effect=save):
                try:
                    with patch("src.gui.filedialog.asksaveasfilename", return_value=str(source)):
                        self.window._on_save_received()
                    self.pump_until(started.is_set)
                    self.assertIn("disabled", self.window.save_button.state())
                    ticks = []
                    self.root.after(0, lambda: ticks.append(True))
                    self.pump_until(lambda: bool(ticks))
                    self.assertNotEqual(worker_threads, [threading.get_ident()])
                finally:
                    release.set()
                self.pump_until(lambda: not self.window._saving_file)
            self.assertEqual(source.read_bytes(), b"safe same path")
            self.assertIn("Đã lưu tệp tại:", self.window.notice_var.get())

    def test_save_only_enabled_for_completed_received_files(self):
        with patch("src.gui.filedialog.asksaveasfilename") as dialog:
            for kind, direction in (("transfer_progress", "receive"),
                                    ("transfer_failed", "receive"),
                                    ("transfer_completed", "send")):
                tid = kind + direction
                self.window.process_event({
                    "type": kind, "transfer_id": tid, "peer_id": self.minh,
                    "filename": "file.bin", "direction": direction, "percent": 100,
                    "path": "unused", "sha256": "unused",
                })
                self.window.transfer_tree.selection_set(tid)
                self.window._on_transfer_selected()
                self.assertIn("disabled", self.window.save_button.state())
                self.window._on_save_received()
            dialog.assert_not_called()

    def test_paired_windows_exchange_chat_and_send_real_file(self):
        from src.member3_pair_demo import create_demo_pair

        with tempfile.TemporaryDirectory() as directory:
            first, second = create_demo_pair(directory)
            pair_windows = []
            try:
                for backend in (first, second):
                    top = tk.Toplevel(self.root)
                    top.withdraw()
                    pair_windows.append(ChatWindow(
                        top, backend.local["name"], backend.events, backend.actions(),
                        demo="DEMO CẶP · Chat nội bộ mô phỏng; truyền tệp TCP thật."))
                left, right = pair_windows
                first_id, second_id = first.local["peer_id"], second.local["peer_id"]
                self.pump_until(lambda: second_id in left._peers and first_id in right._peers)
                left.select_peer(second_id)
                right.select_peer(first_id)
                left.draft_var.set("Chào Minh từ cửa sổ Như")
                left._on_send()
                self.pump_until(lambda: "Chào Minh từ cửa sổ Như" in
                                right.chat_text.get("1.0", "end") and not left._pending_send)
                right.draft_var.set("Như ơi, mình thấy tin rồi")
                right._on_send()
                self.pump_until(lambda: "Như ơi, mình thấy tin rồi" in
                                left.chat_text.get("1.0", "end") and not right._pending_send)
                source = Path(directory) / "test-gui-file.bin"
                source.write_bytes(bytes(range(256)) * 2048)
                with patch("src.gui.filedialog.askopenfilename", return_value=str(source)):
                    left._on_file()
                self.pump_until(lambda: bool(left._transfers) and bool(right._transfers) and
                                all(event["type"] == "transfer_completed"
                                    for window in pair_windows
                                    for event in window._transfers.values()), timeout=6)
                self.assertEqual((second.transfers.downloads_dir / source.name).read_bytes(),
                                 source.read_bytes())
            finally:
                for window in pair_windows:
                    window.close()
                self.pump_until(lambda: all(window._destroyed for window in pair_windows))
                for window in pair_windows:
                    window._worker.join(2)
                    self.assertFalse(window._worker.is_alive())
                first.shutdown()
                second.shutdown()
                pair_windows.clear()
                gc.collect()


    def test_shutdown_does_not_block_event_loop(self):
        started = threading.Event()
        release = threading.Event()
        ticks = []

        def shutdown():
            started.set()
            release.wait(2)

        self.window.actions.shutdown = shutdown
        try:
            self.window.close()
            self.assertTrue(started.wait(1))
            self.root.after(10, lambda: ticks.append("still responsive"))
            self.pump_until(lambda: bool(ticks))
            self.assertFalse(self.window._destroyed)
        finally:
            release.set()
        self.pump_until(lambda: self.window._destroyed)


if __name__ == "__main__":
    unittest.main()
