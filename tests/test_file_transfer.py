import hashlib
from pathlib import Path
import queue
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from src.file_transfer import FileTransferManager, safe_filename
from src.member3_demo import FileDemoListener
from src.protocol import (ProtocolError, peer_id, recv_frame,
                          send_frame, send_header)


TERMINAL = {"transfer_completed", "transfer_failed", "transfer_cancelled"}


def wait_event(events, kind=None, tid=None, timeout=6):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=max(0.001, deadline - time.monotonic()))
        except queue.Empty:
            break
        seen.append(event)
        if (kind is None or event["type"] in kind) and (tid is None or event.get("transfer_id") == tid):
            return event, seen
    raise AssertionError(f"Không nhận được event {kind}, tid={tid}; đã thấy {seen}")


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.resources = []
        self.sender, self.sender_events, self.sender_peer = self.make_manager("Quan")
        self.receiver, self.receiver_events, self.receiver_peer = self.make_manager("Minh")

    def make_manager(self, name, **config):
        events = queue.Queue()
        local = {"name": name, "ip": "127.0.0.1", "tcp_port": 5000,
                 "peer_id": "127.0.0.1:5000"}
        config.setdefault("timeout", 1.0)
        manager = FileTransferManager(local, events, self.base / (name + "-downloads"), **config)
        listener = FileDemoListener(manager, "127.0.0.1", 0)
        local["tcp_port"] = listener.port
        local["peer_id"] = peer_id(local["ip"], listener.port)
        manager.local_peer = dict(local)
        self.resources.append((listener, manager))
        return manager, events, local

    def tearDown(self):
        try:
            for listener, manager in self.resources:
                listener.stop()
                manager.stop_transfers()
        finally:
            self.temp.cleanup()

    def source(self, name, data):
        path = self.base / name
        path.write_bytes(data)
        return path

    def transfer(self, path):
        tid = self.sender.send_file(self.receiver_peer, path)
        sent, sender_seen = wait_event(self.sender_events, TERMINAL, tid)
        received, receiver_seen = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(sent["type"], "transfer_completed", sent)
        self.assertEqual(received["type"], "transfer_completed", received)
        self.assertEqual(Path(received["path"]).read_bytes(), path.read_bytes())
        self.assertEqual(sent["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(sent["percent"], 100)
        self.assertEqual(received["percent"], 100)
        self.assert_no_partial()
        return sent, received, sender_seen + receiver_seen

    def assert_no_partial(self):
        if self.receiver.downloads_dir.exists():
            self.assertEqual(list(self.receiver.downloads_dir.glob("*.part")), [])

    def raw_connection(self, filename="raw.bin", data=b"abc", size=None, checksum=None, **extra):
        tid = str(uuid.uuid4())
        sock = socket.create_connection(("127.0.0.1", self.receiver_peer["tcp_port"]), 2)
        self.addCleanup(sock.close)
        send_frame(sock, {"type": "OPEN", "purpose": "file", "tcp_port": 5019,
                          "peer_id": "127.0.0.1:5019", "transfer_id": tid})
        send_header(sock, {"type": "FILE", "transfer_id": tid, "filename": filename,
                           "payload_length": len(data) if size is None else size,
                           "sha256": checksum or hashlib.sha256(data).hexdigest(), **extra})
        return sock, tid

    def test_binary_unicode_and_progress(self):
        data = bytes(range(256)) * 2000 + b"|\0\n\xff"
        _, _, seen = self.transfer(self.source("Tài liệu 🐈.bin", data))
        for direction in ("send", "receive"):
            counts = [e["bytes_done"] for e in seen if e.get("direction") == direction]
            self.assertEqual(counts, sorted(counts))
        self.assertTrue(any(e.get("stage") == "awaiting_confirmation" for e in seen))

    def test_zero_byte(self):
        self.transfer(self.source("empty.txt", b""))

    def test_both_directions(self):
        path = self.source("reverse.txt", "Xin chào Quan".encode())
        tid = self.receiver.send_file(self.sender_peer, path)
        sent, _ = wait_event(self.receiver_events, TERMINAL, tid)
        received, _ = wait_event(self.sender_events, TERMINAL, tid)
        self.assertEqual(sent["type"], "transfer_completed", sent)
        self.assertEqual(Path(received["path"]).read_bytes(), path.read_bytes())

    def test_duplicate_names_preserve_existing_file(self):
        path = self.source("report.txt", b"first")
        _, first, _ = self.transfer(path)
        path.write_bytes(b"second")
        _, second, _ = self.transfer(path)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(Path(first["path"]).read_bytes(), b"first")
        self.assertEqual(Path(second["path"]).name, "report (1).txt")

    def test_simultaneous_transfers_same_name(self):
        path = self.source("parallel.bin", bytes(range(256)) * 1500)
        tids = [self.sender.send_file(self.receiver_peer, path) for _ in range(3)]
        sent, received = {}, {}
        deadline = time.monotonic() + 8
        while len(sent) < 3 or len(received) < 3:
            for events, target in ((self.sender_events, sent), (self.receiver_events, received)):
                try:
                    e = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if e["type"] in TERMINAL:
                    target[e["transfer_id"]] = e
            self.assertLess(time.monotonic(), deadline)
        self.assertEqual(set(sent), set(tids))
        self.assertTrue(all(e["type"] == "transfer_completed" for e in [*sent.values(), *received.values()]))
        self.assertEqual(len({e["path"] for e in received.values()}), 3)
        self.assert_no_partial()

    def test_missing_source_is_async_error(self):
        tid = self.sender.send_file(self.receiver_peer, self.base / "missing.txt")
        event, _ = wait_event(self.sender_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_failed")

    def test_disconnected_peer(self):
        reservation = socket.socket()
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        # Bound but not listening: guaranteed to refuse without picking a
        # supposedly free port that another program could take.
        self.addCleanup(reservation.close)
        peer = {"ip": "127.0.0.1", "tcp_port": port, "peer_id": peer_id("127.0.0.1", port)}
        tid = self.sender.send_file(peer, self.source("offline.txt", b"hello"))
        event, _ = wait_event(self.sender_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_failed")

    def test_short_body_is_deleted(self):
        sock, tid = self.raw_connection(size=100)
        sock.sendall(b"abc")
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_frame(sock)[0]["type"], "ERROR")
        event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_failed")
        self.assert_no_partial()
        self.assertFalse((self.receiver.downloads_dir / "raw.bin").exists())

    def test_excess_body_is_deleted(self):
        sock, tid = self.raw_connection(data=b"abc", size=2)
        sock.sendall(b"abc")
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(recv_frame(sock)[0]["type"], "ERROR")
        wait_event(self.receiver_events, TERMINAL, tid)
        self.assert_no_partial()
        self.assertFalse((self.receiver.downloads_dir / "raw.bin").exists())

    def test_checksum_mismatch_is_deleted(self):
        sock, tid = self.raw_connection(checksum="0" * 64)
        sock.sendall(b"abc")
        sock.shutdown(socket.SHUT_WR)
        reply, _ = recv_frame(sock)
        self.assertEqual(reply["type"], "ERROR")
        event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertIn("SHA-256", event["error"])
        self.assert_no_partial()

    def test_unsafe_name_never_writes_outside_downloads(self):
        sock, tid = self.raw_connection(filename="../escape.bin")
        self.assertEqual(recv_frame(sock)[0]["type"], "ERROR")
        event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_failed")
        self.assertFalse((self.base / "escape.bin").exists())

    def test_invalid_names(self):
        for name in ("", "..", "a/b", "a\\b", "C:evil", "CON.txt", "LPT1", "x.", "x ", "\0", "🐈" * 80):
            with self.subTest(name=name), self.assertRaises(ProtocolError):
                safe_filename(name)

    def test_cancel_active_receiver_cleans_partial(self):
        sock, tid = self.raw_connection(size=1024)
        sock.sendall(b"abc")
        wait_event(self.receiver_events, {"transfer_progress"}, tid)
        self.assertTrue(self.receiver.cancel_transfer(tid))
        event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_cancelled")
        self.assert_no_partial()

    def test_timeout_cleans_partial(self):
        self.receiver.timeout = 0.15
        sock, tid = self.raw_connection(size=1024)
        sock.sendall(b"abc")
        event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_failed")
        self.assert_no_partial()

    def test_stop_unblocks_receive_and_rejects_new_transfers(self):
        sock, tid = self.raw_connection(size=1024)
        sock.sendall(b"abc")
        wait_event(self.receiver_events, {"transfer_progress"}, tid)
        self.receiver.stop_transfers()
        self.assertEqual(self.receiver.active_transfer_ids, ())
        self.assert_no_partial()
        with self.assertRaises(RuntimeError):
            self.receiver.send_file(self.sender_peer, self.base / "anything")

    def test_disk_publish_failure_cleans_partial(self):
        path = self.source("disk.bin", b"content")
        with patch.object(self.receiver, "_publish", side_effect=OSError("disk denied")):
            tid = self.sender.send_file(self.receiver_peer, path)
            received, _ = wait_event(self.receiver_events, TERMINAL, tid)
            sent, _ = wait_event(self.sender_events, TERMINAL, tid)
        self.assertEqual(received["type"], "transfer_failed")
        self.assertEqual(sent["type"], "transfer_failed")
        self.assert_no_partial()

    def test_cleanup_failure_is_reported(self):
        actual_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.suffix == ".part":
                raise PermissionError("cleanup denied")
            return actual_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            sock, tid = self.raw_connection(size=100)
            sock.shutdown(socket.SHUT_WR)
            event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertIn("không xóa được file nhận dở", event["error"])
        for path in self.receiver.downloads_dir.glob("*.part"):
            path.unlink()

    def test_receiver_limit_enforced_before_creating_file(self):
        self.receiver.max_file_bytes = 2
        sock, tid = self.raw_connection(size=3)
        self.assertEqual(recv_frame(sock)[0]["type"], "ERROR")
        event, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(event["type"], "transfer_failed")
        self.assert_no_partial()

    def test_reject_peer_identity_mismatch_and_close_socket(self):
        left, right = socket.socketpair()
        self.addCleanup(right.close)
        with self.assertRaises(ProtocolError):
            self.receiver.receive_file(left, {"version": 1, "type": "OPEN", "purpose": "file",
                "payload_length": 0, "peer_id": "127.0.0.2:5000", "tcp_port": 5000,
                "transfer_id": str(uuid.uuid4())}, "127.0.0.1")
        self.assertEqual(left.fileno(), -1)

    def test_source_changed_after_hash_does_not_report_success(self):
        path = self.source("changing.bin", b"A" * (256 * 1024))
        original = self.sender._progress
        changed = False

        def mutate(job, **kwargs):
            nonlocal changed
            if kwargs.get("stage") == "transferring" and not changed:
                changed = True
                path.write_bytes(b"B" * (256 * 1024))
            original(job, **kwargs)

        self.sender._progress = mutate
        tid = self.sender.send_file(self.receiver_peer, path)
        sent, _ = wait_event(self.sender_events, TERMINAL, tid)
        received, _ = wait_event(self.receiver_events, TERMINAL, tid)
        self.assertEqual(sent["type"], "transfer_failed")
        self.assertEqual(received["type"], "transfer_failed")
        self.assert_no_partial()

    def test_sender_requires_receiver_confirmation(self):
        actual_send_frame = send_frame

        def drop_ack(sock, header, payload=b""):
            if header.get("type") == "FILE_OK":
                raise ConnectionResetError("ACK lost")
            return actual_send_frame(sock, header, payload)

        with patch("src.file_transfer.send_frame", side_effect=drop_ack):
            path = self.source("ack.txt", b"saved on receiver")
            tid = self.sender.send_file(self.receiver_peer, path)
            received, _ = wait_event(self.receiver_events, TERMINAL, tid)
            sent, _ = wait_event(self.sender_events, TERMINAL, tid)
        self.assertEqual(received["type"], "transfer_completed")
        self.assertIn("không gửi được xác nhận", received["warning"])
        self.assertEqual(Path(received["path"]).read_bytes(), path.read_bytes())
        self.assertEqual(sent["type"], "transfer_failed")
        self.assert_no_partial()


if __name__ == "__main__":
    unittest.main()
