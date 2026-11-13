import queue
import socket
import tempfile
import time
import unittest
from pathlib import Path

from src.chat import ChatService
from src.file_transfer import FileTransferManager
from src.protocol import recv_frame, send_frame


class ChatServiceTests(unittest.TestCase):
    def setUp(self):
        self.peers = {}

    def start_peer(self, name, file_receiver=None):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        pid = f"127.0.0.1:{port}"
        local = {"peer_id": pid, "name": name, "ip": "127.0.0.1",
                 "tcp_port": port, "status": "online"}
        events = queue.Queue()
        chat = ChatService(local, events, self.peers.__getitem__, file_receiver,
                           bind_host="127.0.0.1")
        chat.start_listener()
        self.addCleanup(chat.stop_listener)
        self.peers[pid] = local
        return pid, chat, events

    def wait_event(self, events, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if predicate(event):
                return event
        self.fail("Không nhận được sự kiện mong đợi")

    def conversation(self, events, peer, predicate):
        return self.wait_event(events, lambda event: event["type"] == "conversation"
                               and event["peer_id"] == peer and predicate(event))

    def test_two_way_unicode_history_and_unread(self):
        a, alice, ae = self.start_peer("Alice")
        b, bob, be = self.start_peer("Bob")
        c, carol, _ = self.start_peer("Carol")
        alice.select_peer(b)
        initial = self.conversation(ae, b, lambda event: not event["messages"])
        large = "Chào bạn 🙂" * 4000
        alice.send_message(b, large)
        alice.send_message(b, "Tin thứ hai")
        received = self.conversation(be, a, lambda event: len(event["messages"]) == 2)
        self.assertEqual([m["text"] for m in received["messages"]], [large, "Tin thứ hai"])
        self.assertEqual(received["unread"], 2)
        bob.select_peer(a)
        opened = self.conversation(be, a, lambda event: event["unread"] == 0)
        self.assertGreater(opened["revision"], received["revision"])
        bob.send_message(a, "Đã nhận")
        reply = self.conversation(ae, b, lambda e: len(e["messages"]) == 3)
        self.assertEqual(reply["unread"], 0)
        self.assertGreater(reply["revision"], initial["revision"])
        carol.send_message(a, "Tin ngoài hội thoại")
        unread = self.conversation(ae, c, lambda e: e["unread"] == 1)
        self.assertEqual(unread["messages"][0]["sender"], "Carol")
        alice.select_peer(c)
        opened = self.conversation(ae, c, lambda e: e["unread"] == 0)
        self.assertEqual(opened["messages"], unread["messages"])

    def test_offline_send_reports_failure(self):
        _, alice, events = self.start_peer("Alice")
        pid = "127.0.0.1:1"
        alice.send_message(pid, "Chưa gửi được")
        self.wait_event(events, lambda e: e["type"] == "operation_failed")
        failure = self.conversation(events, pid, lambda e: e["messages"][-1]["state"] == "failed")
        self.assertEqual(failure["messages"][-1]["text"], "Chưa gửi được")

    def test_invalid_open_then_valid_session_and_bye(self):
        a, alice, events = self.start_peer("Alice")
        port = self.peers[a]["tcp_port"]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            send_frame(conn, {"type": "MSG"}, b"before OPEN")
            self.assertEqual(conn.recv(1), b"")
        self.wait_event(events, lambda e: e["type"] == "operation_failed")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            b = "127.0.0.1:1"
            send_frame(conn, {"type": "OPEN", "purpose": "chat", "tcp_port": 1,
                              "peer_id": b, "name": "Bob"})
            send_frame(conn, {"type": "MSG"}, b"hello")
            self.conversation(events, b, lambda e: e["unread"] == 1)
            alice.leave_chat(b)
            self.assertEqual(recv_frame(conn)[0]["type"], "BYE")
        alice.stop_listener()
        alice.stop_listener()

    def test_file_handoff_preserves_body_and_survives_chat_shutdown(self):
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "downloads"
            source = Path(root) / "test.bin"
            body = bytes(range(256)) * 256
            source.write_bytes(body)
            receiver = None

            def handoff(*args):
                receiver.receive_file(*args)

            b, bob, be = self.start_peer("Bob", handoff)
            a, alice, ae = self.start_peer("Alice")
            receiver = FileTransferManager(self.peers[b], be, downloads_dir=destination)
            sender = FileTransferManager(self.peers[a], ae)
            try:
                alice.send_message(b, "Gửi kèm file")
                self.conversation(be, a, lambda e: e["unread"] == 1)
                sender.send_file(self.peers[b], source)
                self.wait_event(be, lambda e: e["type"] == "transfer_started")
                bob.leave_chat(a)
                bob.stop_listener()
                self.wait_event(be, lambda e: e["type"] == "transfer_completed")
                self.wait_event(ae, lambda e: e["type"] == "transfer_completed")
                self.assertEqual((destination / source.name).read_bytes(), body)
            finally:
                sender.stop_transfers()
                receiver.stop_transfers()


if __name__ == "__main__":
    unittest.main()
