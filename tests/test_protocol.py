import json
import socket
import struct
import threading
import unittest

from src.protocol import (ConnectionClosed, MAX_HEADER_BYTES, ProtocolError,
                          peer_id, read_header, recv_exact, recv_frame, send_frame)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.left.settimeout(2)
        self.right.settimeout(2)

    def tearDown(self):
        self.left.close()
        self.right.close()

    def raw_header(self, header):
        raw = json.dumps(header).encode()
        self.left.sendall(struct.pack("!I", len(raw)) + raw)

    def test_fragmented_unicode_and_coalesced_frames(self):
        payload = "Chào Minh 👋 |\n".encode("utf-8")
        header = json.dumps({"version": 1, "type": "MSG", "payload_length": len(payload)}).encode()
        wire = struct.pack("!I", len(header)) + header + payload

        def send_fragments():
            for value in wire:
                self.left.sendall(bytes([value]))
            send_frame(self.left, {"type": "BYE"})

        worker = threading.Thread(target=send_fragments)
        worker.start()
        actual_header, actual = recv_frame(self.right)
        self.assertEqual(actual, payload)
        self.assertEqual(actual_header["payload_length"], len(payload))
        self.assertEqual(recv_frame(self.right)[0]["type"], "BYE")
        worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_eof_mid_payload(self):
        self.left.sendall(b"ab")
        self.left.shutdown(socket.SHUT_WR)
        with self.assertRaises(ConnectionClosed):
            recv_exact(self.right, 3)

    def test_oversized_header_rejected_before_body(self):
        self.left.sendall(struct.pack("!I", MAX_HEADER_BYTES + 1))
        with self.assertRaises(ProtocolError):
            read_header(self.right)

    def test_invalid_header_values(self):
        for extra in ({"version": True}, {"type": []}, {"payload_length": -1},
                      {"payload_length": True}, {"type": "UNKNOWN"}, {"version": 2}):
            with self.subTest(extra=extra):
                self.raw_header({"version": 1, "type": "MSG", "payload_length": 0, **extra})
                with self.assertRaises(ProtocolError):
                    read_header(self.right)

    def test_duplicate_json_key_rejected(self):
        raw = b'{"version":1,"type":"MSG","payload_length":0,"payload_length":1}'
        self.left.sendall(struct.pack("!I", len(raw)) + raw)
        with self.assertRaises(ProtocolError):
            read_header(self.right)

    def test_invalid_utf8_rejected(self):
        self.left.sendall(struct.pack("!I", 1) + b"\xff")
        with self.assertRaises(ProtocolError):
            read_header(self.right)

    def test_read_header_does_not_consume_file_body(self):
        raw = b'{"version":1,"type":"FILE","payload_length":4}'
        self.left.sendall(struct.pack("!I", len(raw)) + raw + b"|\x00\xffA")
        self.assertEqual(read_header(self.right)["type"], "FILE")
        self.assertEqual(recv_exact(self.right, 4), b"|\x00\xffA")

    def test_identity_uses_endpoint_not_name(self):
        self.assertNotEqual(peer_id("127.0.0.1", 5000), peer_id("127.0.0.1", 5001))
        for port in (0, 65536, True, "5000"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                peer_id("127.0.0.1", port)


if __name__ == "__main__":
    unittest.main()
