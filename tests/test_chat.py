import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.chat import ChatService, MAX_HANDSHAKES, MAX_HISTORY, OPEN_TIMEOUT
from src.file_transfer import FileTransferManager
from src.protocol import recv_frame, send_frame, send_header


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

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Trạng thái chat không hội tụ đúng hạn")

    def connect_raw(self, pid):
        conn = socket.create_connection(("127.0.0.1", self.peers[pid]["tcp_port"]), 5)
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def frame(header, payload=b""):
        data = bytearray()
        send_frame(Mock(sendall=data.extend), header, payload)
        return bytes(data)

    def opening(self, pid):
        return {"type": "OPEN", "purpose": "chat", "peer_id": pid,
                "tcp_port": self.peers[pid]["tcp_port"], "name": self.peers[pid]["name"]}

    def test_history_keeps_500_per_peer_and_unread_counts_evicted_messages(self):
        a, alice, ae = self.start_peer("Alice")
        b, bob, be = self.start_peer("Bob")
        c, carol, ce = self.start_peer("Carol")
        carol.send_message(b, "Riêng Carol")
        self.conversation(be, c, lambda e: e["unread"] == 1)
        alice.send_message(b, "0")
        first = self.conversation(be, a, lambda e: e["unread"] == 1)
        for number in range(1, MAX_HISTORY + 5):
            alice.send_message(b, str(number))
        received = self.conversation(be, a, lambda e: e["unread"] == MAX_HISTORY + 5)
        sent = self.conversation(ae, b, lambda e: len(e["messages"]) == MAX_HISTORY
                                 and all(m["state"] == "sent" for m in e["messages"]))
        expected = [str(number) for number in range(5, MAX_HISTORY + 5)]
        self.assertEqual([m["text"] for m in received["messages"]], expected)
        self.assertEqual([m["text"] for m in sent["messages"]], expected)
        self.assertEqual(first["messages"][0]["text"], "0")
        self.assertEqual(first["unread"], 1)
        with bob._lock:
            self.assertEqual(len(bob._conversations[a].messages), MAX_HISTORY)
            self.assertEqual(len(bob._conversations[c].messages), 1)
        bob.select_peer(a)
        opened = self.conversation(be, a, lambda e: e["unread"] == 0)
        self.assertEqual(opened["messages"], received["messages"])
        self.assertGreater(opened["revision"], received["revision"])
        alice.send_message(b, "Đang xem")
        active = self.conversation(be, a, lambda e: e["messages"][-1]["text"] == "Đang xem")
        self.assertEqual(active["unread"], 0)
        self.assertEqual(len(active["messages"]), MAX_HISTORY)

    def test_late_send_failure_does_not_restore_evicted_message(self):
        a, alice, ae = self.start_peer("Alice")
        b, _, _ = self.start_peer("Bob")
        resolving, release = threading.Event(), threading.Event()

        def delayed_failure(pid):
            resolving.set()
            if not release.wait(5):
                raise RuntimeError("Test không giải phóng resolver")
            raise KeyError(pid)

        self.addCleanup(release.set)
        with patch.object(alice, "_resolve_peer", delayed_failure):
            alice.send_message(b, "Tin gửi bị loại khỏi history")
            self.assertTrue(resolving.wait(2))
            conn = self.connect_raw(a)
            conn.sendall(self.frame(self.opening(b)) + b"".join(
                self.frame({"type": "MSG"}, str(i).encode()) for i in range(MAX_HISTORY)))
            before = self.conversation(ae, b, lambda e: e["unread"] == MAX_HISTORY)
            release.set()
            self.wait_event(ae, lambda e: e["type"] == "operation_failed")
            after = self.conversation(ae, b, lambda e: e["revision"] > before["revision"])
            self.assertEqual(after["messages"], before["messages"])
            self.assertTrue(all(not m["outgoing"] for m in after["messages"]))

    def test_fragmented_open_and_coalesced_messages(self):
        a, _, events = self.start_peer("Alice")
        b, _, _ = self.start_peer("Bob")
        conn = self.connect_raw(a)
        opening = self.frame(self.opening(b))
        for byte in opening:
            conn.sendall(bytes([byte]))
        conn.sendall(self.frame({"type": "MSG"}, "Chào 🙂".encode())
                     + self.frame({"type": "MSG"}, b"second"))
        received = self.conversation(events, b, lambda e: e["unread"] == 2)
        self.assertEqual([m["text"] for m in received["messages"]], ["Chào 🙂", "second"])

    def test_open_deadline_covers_length_and_header_despite_trickle(self):
        a, alice, events = self.start_peer("Alice")
        b, _, _ = self.start_peer("Bob")
        conn = self.connect_raw(a)
        stopped = threading.Event()
        sent = []

        def trickle():
            for byte in self.frame(self.opening(b)):
                try:
                    conn.sendall(bytes([byte]))
                    sent.append(byte)
                except OSError:
                    return
                if stopped.wait(0.2):
                    return

        worker = threading.Thread(target=trickle)
        started = time.monotonic()
        worker.start()
        try:
            self.wait_event(events, lambda e: e["type"] == "operation_failed")
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, OPEN_TIMEOUT - 0.3)
            self.assertLess(elapsed, OPEN_TIMEOUT + 1)
            self.assertGreater(len(sent), 4)
            self.wait_until(lambda: not alice._sockets)
        finally:
            stopped.set()
            worker.join(2)
        # The timed-out connection must return its slot and leave the listener usable.
        healthy = self.connect_raw(a)
        healthy.sendall(self.frame(self.opening(b)) + self.frame({"type": "MSG"}, b"alive"))
        self.conversation(events, b, lambda e: e["messages"][-1]["text"] == "alive")

    def test_handshake_limit_rejects_excess_and_releases_slots_on_shutdown(self):
        a, alice, events = self.start_peer("Alice")
        conns = [self.connect_raw(a) for _ in range(MAX_HANDSHAKES)]
        self.wait_until(lambda: len(alice._sockets) == MAX_HANDSHAKES)
        excess = self.connect_raw(a)
        self.assertEqual(excess.recv(1), b"")
        for conn in conns:
            conn.shutdown(socket.SHUT_WR)
        self.wait_until(lambda: not alice._sockets)
        for _ in range(MAX_HANDSHAKES):
            self.connect_raw(a)
        self.wait_until(lambda: len(alice._sockets) == MAX_HANDSHAKES)
        alice.stop_listener()
        self.assertFalse(alice._sockets)
        self.assertFalse(alice._workers)
        alice.stop_listener()

    def test_simultaneous_connect_selects_same_socket_and_preserves_messages(self):
        a, alice, ae = self.start_peer("Alice")
        b, bob, be = self.start_peer("Bob")
        connecting = threading.Barrier(2)

        def resolve(pid):
            connecting.wait(3)
            return self.peers[pid]

        with patch.object(alice, "_resolve_peer", resolve), patch.object(bob, "_resolve_peer", resolve):
            alice.select_peer(b)
            bob.select_peer(a)
            for i in range(20):
                alice.send_message(b, f"Alice {i}")
                bob.send_message(a, f"Bob {i}")
            ar = self.conversation(ae, b, lambda e: len(e["messages"]) == 40
                                   and all(m.get("state", "sent") == "sent" for m in e["messages"]))
            br = self.conversation(be, a, lambda e: len(e["messages"]) == 40
                                   and all(m.get("state", "sent") == "sent" for m in e["messages"]))
        self.assertEqual(sorted(m["text"] for m in ar["messages"] if not m["outgoing"]),
                         sorted(f"Bob {i}" for i in range(20)))
        self.assertEqual(sorted(m["text"] for m in br["messages"] if not m["outgoing"]),
                         sorted(f"Alice {i}" for i in range(20)))
        self.wait_until(lambda: len(alice._sessions.get(b, ())) == len(bob._sessions.get(a, ())) == 1)
        left, right = alice._sessions[b][0], bob._sessions[a][0]
        self.assertEqual(left.rank, right.rank)
        self.assertEqual(left.rank[0], min(a, b))
        self.assertEqual(left.sock.getsockname(), right.sock.getpeername())
        alice.send_message(b, "Sau hội tụ")
        self.conversation(be, a, lambda e: e["messages"][-1]["text"] == "Sau hội tụ")
        alice.stop_listener()
        bob.stop_listener()
        self.assertFalse(alice._workers)
        self.assertFalse(bob._workers)

    def test_duplicate_retirement_drains_inflight_frame_and_old_worker_cannot_drop_reconnect(self):
        peers = [self.start_peer("Alice"), self.start_peer("Bob")]
        (low_id, low, low_events), (high_id, high, high_events) = sorted(peers)
        resolving, allow_connect = threading.Event(), threading.Event()
        partial_sent, finish_send = threading.Event(), threading.Event()
        self.addCleanup(allow_connect.set)
        self.addCleanup(finish_send.set)
        payload = "Tin đang gửi 🙂" * 2000

        def delayed_resolve(pid):
            resolving.set()
            if not allow_connect.wait(5):
                raise RuntimeError("Test không giải phóng connect")
            return self.peers[pid]

        def fragmented_send(sock, header, body=b""):
            if header["type"] == "MSG" and body == payload.encode():
                send_header(sock, {**header, "payload_length": len(body)})
                sock.sendall(body[:10])
                partial_sent.set()
                if not finish_send.wait(5):
                    raise RuntimeError("Test không giải phóng frame")
                sock.sendall(body[10:])
            else:
                send_frame(sock, header, body)

        with patch.object(low, "_resolve_peer", delayed_resolve), \
                patch("src.chat.send_frame", fragmented_send):
            low.select_peer(high_id)
            self.assertTrue(resolving.wait(2))
            high.send_message(low_id, payload)
            self.assertTrue(partial_sent.wait(2))
            with low._lock:
                old_session = low._current_session(high_id)
            self.assertEqual(old_session.rank[0], high_id)
            allow_connect.set()
            self.wait_until(lambda: old_session.retiring)
            finish_send.set()
            received = self.conversation(low_events, high_id, lambda e: len(e["messages"]) == 1)
            self.assertEqual(received["messages"][0]["text"], payload)
            self.wait_until(lambda: len(low._sessions.get(high_id, ()))
                            == len(high._sessions.get(low_id, ())) == 1)
        high.send_message(low_id, "Sau đổi socket")
        self.conversation(low_events, high_id, lambda e: len(e["messages"]) == 2)
        self.assertEqual(low._sessions[high_id][0].rank, high._sessions[low_id][0].rank)
        low.leave_chat(high_id)
        self.wait_until(lambda: not high._sessions.get(low_id))
        high.send_message(low_id, "Kết nối lại")
        self.conversation(low_events, high_id, lambda e: len(e["messages"]) == 3)
        replacement = low._sessions[high_id][0]
        # Reproduce a delayed cleanup from the earlier socket after reconnect.
        low._drop(high_id, old_session)
        self.assertIs(low._sessions[high_id][0], replacement)
        low.send_message(high_id, "Phiên mới còn hoạt động")
        self.conversation(high_events, low_id, lambda e: e["messages"][-1]["text"]
                          == "Phiên mới còn hoạt động")

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
