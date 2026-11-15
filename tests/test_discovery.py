import queue
import subprocess
import sys
from pathlib import Path
import socket
import time
import unittest
from unittest.mock import Mock, patch

from src.discovery import DiscoveryService, validate_ip, validate_name


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.local = {"peer_id": "127.0.0.1:5000", "name": "Local",
                      "ip": "127.0.0.1", "tcp_port": 5000, "status": "online"}
        self.discovery = DiscoveryService(self.local, self.events)
        self.addCleanup(self.discovery.stop_discovery)

    def test_validation_and_malformed_datagrams(self):
        for name in ("", "a|b", "a\nb", "🙂" * 17, None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_name(name)
        self.assertEqual(validate_name("🙂" * 16), "🙂" * 16)
        for ip in ("0.0.0.0", "255.255.255.255", "224.0.0.1", "invalid"):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                validate_ip(ip)
        for packet in (b"", b"\xff", b"HELLO|Bob|5001|busy",
                       b"HELLO|Bob|0|online", b"HELLO|Bob|65536|online",
                       b"HELLO|Bob|+5001|online", b"HELLO||5001|online",
                       b"HELLO|Bob|5001|online|extra", b"x" * 513):
            with self.subTest(packet=packet):
                self.assertIsNone(self.discovery._parse_presence(packet, "127.0.0.1"))

    def test_presence_identity_status_snapshot_and_timeout(self):
        self.discovery._accept_presence(b"HELLO|Local|5000|online", "127.0.0.1")
        self.assertTrue(self.events.empty())
        with patch("src.discovery.time.monotonic", return_value=100):
            self.discovery._accept_presence(b"HELLO|Local|5001|online", "127.0.0.1")
        pid = "127.0.0.1:5001"
        self.assertEqual(self.events.get_nowait()["type"], "peer_upsert")
        snapshot = self.discovery.get_peers()
        snapshot[pid]["name"] = "Changed outside service"
        self.assertEqual(self.discovery.get_peers()[pid]["name"], "Local")
        with patch("src.discovery.time.monotonic", return_value=105):
            self.discovery._accept_presence(b"HELLO|Local|5001|away", "127.0.0.1")
        self.assertEqual(self.events.get_nowait()["peer"]["status"], "away")
        with patch("src.discovery.time.monotonic", return_value=114.9):
            self.discovery._expire_peers()
        self.assertIn(pid, self.discovery.get_peers())
        with patch("src.discovery.time.monotonic", return_value=115.1):
            self.discovery._expire_peers()
        self.assertEqual(self.events.get_nowait(), {"type": "peer_removed", "peer_id": pid})
        self.assertEqual(self.discovery.get_peers(), {})

    def test_broadcast_heartbeat_and_immediate_status(self):
        sender = Mock()
        self.discovery._sender = sender
        with patch("src.discovery.time.monotonic", return_value=100):
            self.discovery._send_presence()
        sender.sendto.assert_called_with(b"HELLO|Local|5000|online", ("255.255.255.255", 9999))
        self.assertEqual(self.discovery._next_send, 103)
        self.discovery.set_status("away")
        sender.sendto.assert_called_with(b"HELLO|Local|5000|away", ("255.255.255.255", 9999))
        self.assertEqual(self.events.get_nowait(), {"type": "status_changed", "status": "away"})
        with self.assertRaises(ValueError):
            self.discovery.set_status("offline")

    def test_real_udp_ignores_bad_packets_and_stops_cleanly(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.discovery.udp_port = reservation.getsockname()[1]
        self.discovery.start_discovery()
        worker = self.discovery._thread
        self.discovery.start_discovery()
        self.assertIs(worker, self.discovery._thread)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            target = ("127.0.0.1", self.discovery.udp_port)
            sender.sendto(b"x" * 1024, target)
            sender.sendto(b"HELLO|Remote|5001|away", target)
        deadline = time.monotonic() + 3
        while True:
            event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event["type"] == "operation_failed":
                self.fail(event["error"])
            if event["type"] == "peer_upsert":
                self.assertEqual(event["peer"]["peer_id"], "127.0.0.1:5001")
                break
        self.discovery.stop_discovery()
        self.discovery.stop_discovery()
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.discovery.get_peers(), {})

    def test_start_failure_closes_sockets_and_reports_error(self):
        with patch("src.discovery.socket.socket") as factory:
            receiver = factory.return_value
            receiver.bind.side_effect = OSError("UDP unavailable")
            with self.assertRaisesRegex(OSError, "UDP unavailable"):
                self.discovery.start_discovery()
        receiver.close.assert_called_once()
        self.assertIsNone(self.discovery._receiver)
        self.assertIsNone(self.discovery._sender)
        self.assertEqual(self.events.get_nowait()["type"], "operation_failed")


    def test_whitespace_only_names_rejected_before_advertising(self):
        names = (" ", "   ", "\u00a0", "\u2003", "\u3000", " \u00a0\u2003\u3000 ")
        for name in names:
            with self.subTest(name=repr(name)):
                with self.assertRaisesRegex(ValueError, "non-whitespace"):
                    validate_name(name)
                with patch("src.discovery.socket.socket") as factory:
                    with self.assertRaises(ValueError):
                        DiscoveryService({**self.local, "name": name}, self.events)
                factory.assert_not_called()

    def test_cli_rejects_ascii_and_unicode_whitespace_names(self):
        root = Path(__file__).resolve().parents[1]
        for name in (" ", "\u00a0", "\u2003\u3000", " \u00a0\u2003 "):
            with self.subTest(name=repr(name)):
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", "-m", "src.main",
                     "--name", name, "--ip", "127.0.0.1"],
                    cwd=root, capture_output=True, text=True,
                    encoding="utf-8", timeout=5,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Name must contain a non-whitespace character", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_udp_ignores_whitespace_only_presence(self):
        for name in (" ", "   ", "\u00a0", "\u2003", "\u3000", " \u00a0\u2003 "):
            with self.subTest(name=repr(name)):
                packet = f"HELLO|{name}|5001|online".encode("utf-8")
                self.assertIsNone(self.discovery._parse_presence(packet, "127.0.0.1"))
                self.discovery._accept_presence(packet, "127.0.0.1")
                self.assertEqual(self.discovery.get_peers(), {})
                self.assertTrue(self.events.empty())

    def test_valid_unicode_names_are_preserved(self):
        for name in ("Nguyễn Trí 🙂", "  Mari  ", "\u2003Mari\u3000", "🙂" * 16):
            with self.subTest(name=name):
                self.assertEqual(validate_name(name), name)
                packet = f"HELLO|{name}|5001|online".encode("utf-8")
                peer = self.discovery._parse_presence(packet, "127.0.0.1")
                self.assertIsNotNone(peer)
                self.assertEqual(peer["name"], name)

    def test_unicode_discovery_peers_chat_over_real_udp_and_tcp(self):
        from src.chat import ChatService

        def available_port(kind):
            with socket.socket(socket.AF_INET, kind) as reservation:
                reservation.bind(("127.0.0.1", 0))
                return reservation.getsockname()[1]

        def start_peer(name):
            port = available_port(socket.SOCK_STREAM)
            local = {**self.local, "peer_id": f"127.0.0.1:{port}",
                     "name": name, "tcp_port": port}
            events = queue.Queue()
            discovery = DiscoveryService(local, events, available_port(socket.SOCK_DGRAM))
            chat = ChatService(local, events, lambda pid: discovery.get_peers()[pid],
                               None, bind_host="127.0.0.1")
            self.addCleanup(chat.stop_listener)
            self.addCleanup(discovery.stop_discovery)
            chat.start_listener()
            discovery.start_discovery()
            return local, events, discovery, chat

        def wait_event(events, predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    event = events.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if event["type"] == "operation_failed":
                    self.fail(event["error"])
                if predicate(event):
                    return event
            self.fail("Expected UDP/TCP event did not arrive")

        left, le, ld, lc = start_peer("Nguyễn Trí 🙂")
        right, re, rd, rc = start_peer("\u2003Mari\u3000")
        for source, target_discovery, target_events in ((left, rd, re), (right, ld, le)):
            packet = "HELLO|{name}|{tcp_port}|{status}".format(**source).encode("utf-8")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.bind(("127.0.0.1", 0))
                sender.sendto(packet, ("127.0.0.1", target_discovery.udp_port))
            event = wait_event(target_events, lambda e: e["type"] == "peer_upsert"
                               and e["peer"]["peer_id"] == source["peer_id"])
            self.assertEqual(event["peer"]["name"], source["name"])

        for chat, target, receiver_events, text in (
                (lc, right, re, "Chào Mari 🙂"), (rc, left, le, "Đã nhận, chào Trí!")):
            chat.send_message(target["peer_id"], text)
            event = wait_event(receiver_events, lambda e: e["type"] == "conversation"
                               and any(m["text"] == text and not m.get("outgoing")
                                       for m in e["messages"]))
            self.assertEqual(event["messages"][-1]["text"], text)
