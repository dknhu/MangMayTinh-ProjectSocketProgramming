import queue
import threading
import tempfile
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
        self.discovery._state = "running"
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


    def test_send_and_recv_errors_remove_stale_peers_and_close_sockets(self):
        for operation in ("send", "recv"):
            with self.subTest(operation=operation):
                service = DiscoveryService(self.local, queue.Queue())
                self.addCleanup(service.stop_discovery)
                service._accept_presence(b"HELLO|Remote|5001|online", "127.0.0.1")
                service.events.get_nowait()
                receiver, sender = Mock(), Mock()
                service._receiver, service._sender = receiver, sender
                service._state = "running"
                if operation == "send":
                    sender.sendto.side_effect = OSError(10051, "Network is unreachable")
                else:
                    service._next_send = float("inf")
                    receiver.recvfrom.side_effect = OSError("Receive failed")

                def stop_during_wait(delay):
                    self.assertEqual(service.get_peers(), {})
                    service._stop.set()
                    return True

                with patch.object(service._stop, "wait", side_effect=stop_during_wait):
                    service._run()
                self.assertEqual(service.get_peers(), {})
                self.assertEqual(service.events.get_nowait(),
                                 {"type": "peer_removed", "peer_id": "127.0.0.1:5001"})
                self.assertEqual(service.events.get_nowait()["type"], "operation_failed")
                receiver.close.assert_called_once()
                sender.close.assert_called_once()
                self.assertIsNone(service._receiver)
                self.assertIsNone(service._sender)
                service.stop_discovery()
                service.stop_discovery()

    def test_retry_reopens_sockets_and_accepts_fresh_presence(self):
        service = self.discovery
        old_receiver, old_sender = Mock(), Mock()
        old_receiver.recvfrom.side_effect = OSError("Network lost")
        service._receiver, service._sender = old_receiver, old_sender
        service._state = "running"
        service._next_send = float("inf")
        new_receiver, new_sender = Mock(), Mock()
        calls = []

        def receive(_):
            if not calls:
                calls.append(True)
                return b"HELLO|Returned|5001|away", ("127.0.0.1", 45000)
            service._stop.set()
            raise socket.timeout()

        new_receiver.recvfrom.side_effect = receive

        def reopen():
            service._receiver, service._sender = new_receiver, new_sender

        with patch.object(service._stop, "wait", return_value=False) as wait, \
                patch.object(service, "_open_sockets", side_effect=reopen) as opened:
            service._run()
        wait.assert_called_once_with(service.RETRY_INITIAL)
        opened.assert_called_once()
        new_sender.sendto.assert_called_once_with(
            b"HELLO|Local|5000|online", ("255.255.255.255", 9999))
        events = []
        while not service.events.empty():
            events.append(service.events.get_nowait())
        self.assertTrue(any(e["type"] == "status_changed" for e in events))
        fresh = next(e["peer"] for e in events if e["type"] == "peer_upsert")
        self.assertEqual(fresh["peer_id"], "127.0.0.1:5001")
        self.assertEqual(fresh["name"], "Returned")
        for sock in (old_receiver, old_sender, new_receiver, new_sender):
            sock.close.assert_called_once()

    def test_repeated_retry_failures_use_capped_backoff_and_close_partial_sockets(self):
        service = self.discovery
        service._state = "retrying"
        sockets = []

        def failing_open():
            sock = Mock()
            sockets.append(sock)
            service._receiver = sock
            raise OSError("Interface unavailable")

        with patch.object(service._stop, "wait", return_value=False) as wait, \
                patch.object(service, "_open_sockets", side_effect=failing_open):
            for _ in range(6):
                service._retry_once()
        self.assertEqual([c.args[0] for c in wait.call_args_list], [1, 2, 4, 8, 8, 8])
        for sock in sockets:
            sock.close.assert_called_once()
        self.assertIsNone(service._receiver)
        self.assertEqual(service._state, "retrying")

    def test_status_failure_does_not_commit_local_choice_or_publish_success(self):
        service = self.discovery
        service._state = "running"
        service._sender, service._receiver = Mock(), Mock()
        service._sender.sendto.side_effect = OSError("Send failed")
        with self.assertRaisesRegex(OSError, "not changed or advertised"):
            service.set_status("away")
        self.assertEqual(service.local_peer["status"], "online")
        self.assertEqual(service._state, "retrying")
        with self.assertRaisesRegex(RuntimeError, "stopped or recovering"):
            service.set_status("away")
        while not service.events.empty():
            self.assertNotEqual(service.events.get_nowait()["type"], "status_changed")
        service.stop_discovery()
        with self.assertRaises(RuntimeError):
            service.set_status("away")

    def test_stop_interrupts_backoff_without_duplicate_worker_and_allows_restart(self):
        service = self.discovery
        service.RETRY_INITIAL = 8.0
        recovering = threading.Event()
        original_begin = service._begin_retry
        opened_pairs = []

        def opened():
            receiver, sender = Mock(), Mock()
            receiver.recvfrom.side_effect = OSError("Network down")
            service._receiver, service._sender = receiver, sender
            opened_pairs.append((receiver, sender))

        def begin(error, delay=None):
            original_begin(error, delay)
            recovering.set()

        with patch.object(service, "_open_sockets", side_effect=opened), \
                patch.object(service, "_begin_retry", side_effect=begin):
            for _ in range(2):
                recovering.clear()
                service.start_discovery()
                worker = service._thread
                self.assertTrue(recovering.wait(2), "Worker did not enter retry")
                service.start_discovery()
                self.assertIs(service._thread, worker)
                before = time.monotonic()
                service.stop_discovery()
                self.assertLess(time.monotonic() - before, 1.0)
                self.assertFalse(worker.is_alive())
                service.stop_discovery()
        self.assertEqual(len(opened_pairs), 2)
        for pair in opened_pairs:
            for sock in pair:
                sock.close.assert_called_once()

    def test_terminal_worker_failure_can_restart_without_an_extra_stop(self):
        service = self.discovery
        service._state = "running"
        service._sender, service._receiver = Mock(), Mock()
        service._next_send = float("inf")
        service._receiver.recvfrom.side_effect = ValueError("Unexpected failure")
        worker = threading.Thread(target=service._run)
        service._thread = worker
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(service._stop.is_set())
        self.assertEqual(service.get_peers(), {})

        def open_sockets():
            service._sender, service._receiver = Mock(), Mock()

        with patch.object(service, "_open_sockets", side_effect=open_sockets), \
                patch.object(service, "_receive_once", side_effect=lambda sock: service._stop.set()):
            service.start_discovery()
            service._thread.join(2)
        self.assertFalse(service._thread.is_alive())
        service.stop_discovery()

    def test_discovery_failure_preserves_established_chat_and_active_file(self):
        from src.chat import ChatService
        from src.file_transfer import FileTransferManager

        def port():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                return sock.getsockname()[1]

        def wait_event(events, predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                event = events.get(timeout=max(0.01, deadline - time.monotonic()))
                if event["type"] in ("operation_failed", "transfer_failed"):
                    self.fail(str(event))
                if predicate(event):
                    return event
            self.fail("Expected TCP event did not arrive")

        with tempfile.TemporaryDirectory() as root:
            ap, bp = port(), port()
            alice = {**self.local, "peer_id": f"127.0.0.1:{ap}", "tcp_port": ap}
            bob = {**self.local, "peer_id": f"127.0.0.1:{bp}", "tcp_port": bp, "name": "Bob"}
            ae, be = queue.Queue(), queue.Queue()
            ad, bd = DiscoveryService(alice, ae), DiscoveryService(bob, be)
            # Separate discovery events from transport events for this lifecycle check.
            ad.events, bd.events = queue.Queue(), queue.Queue()
            ad._accept_presence(f"HELLO|Bob|{bp}|online".encode(), "127.0.0.1")
            bd._accept_presence(f"HELLO|Local|{ap}|online".encode(), "127.0.0.1")
            af = FileTransferManager(alice, ae)
            bf = FileTransferManager(bob, be, downloads_dir=Path(root) / "received")
            entered, release = threading.Event(), threading.Event()

            def receive_file(*args):
                entered.set()
                if not release.wait(5):
                    args[0].close()
                    return
                bf.receive_file(*args)

            ac = ChatService(alice, ae, lambda pid: ad.get_peers()[pid], af.receive_file,
                             bind_host="127.0.0.1")
            bc = ChatService(bob, be, lambda pid: bd.get_peers()[pid], receive_file,
                             bind_host="127.0.0.1")
            try:
                ac.start_listener()
                bc.start_listener()
                ac.send_message(bob["peer_id"], "Before UDP failure")
                wait_event(be, lambda e: e["type"] == "conversation"
                           and any(m["text"] == "Before UDP failure" for m in e["messages"]))
                source = Path(root) / "payload.bin"
                source.write_bytes(bytes(range(256)) * 1024)
                af.send_file(bob, source)
                self.assertTrue(entered.wait(3), "File connection did not reach handoff")
                for discovery in (ad, bd):
                    with discovery._lock:
                        discovery._begin_retry(OSError("UDP network failure"))
                    self.assertEqual(discovery.get_peers(), {})
                ac.send_message(bob["peer_id"], "After UDP failure")
                wait_event(be, lambda e: e["type"] == "conversation"
                           and any(m["text"] == "After UDP failure" for m in e["messages"]))
                release.set()
                wait_event(ae, lambda e: e["type"] == "transfer_completed")
                wait_event(be, lambda e: e["type"] == "transfer_completed")
                self.assertEqual((Path(root) / "received" / source.name).read_bytes(),
                                 source.read_bytes())
            finally:
                release.set()
                ad.stop_discovery()
                bd.stop_discovery()
                ac.stop_listener()
                bc.stop_listener()
                af.stop_transfers()
                bf.stop_transfers()


    def test_real_udp_recovers_after_receiver_socket_failure(self):
        service = self.discovery
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            service.udp_port = reservation.getsockname()[1]
        service.RETRY_INITIAL = 0.02
        service.start_discovery()
        worker = service._thread

        def wait_event(kind):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
                if event["type"] == kind:
                    return event
            self.fail(f"Expected {kind} did not arrive")

        def announce():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(b"HELLO|Remote|5001|online", ("127.0.0.1", service.udp_port))

        announce()
        wait_event("peer_upsert")
        receiver, sender = service._receiver, service._sender
        receiver.close()  # Fault injection into a real socket; no physical Wi-Fi change.
        self.assertEqual(wait_event("peer_removed")["peer_id"], "127.0.0.1:5001")
        wait_event("status_changed")
        self.assertEqual(receiver.fileno(), -1)
        self.assertEqual(sender.fileno(), -1)
        self.assertIs(service._thread, worker)
        self.assertTrue(worker.is_alive())
        self.assertIsNot(service._receiver, receiver)
        announce()
        self.assertEqual(wait_event("peer_upsert")["peer"]["name"], "Remote")
        service.set_status("away")
        self.assertEqual(wait_event("status_changed")["status"], "away")
        service.stop_discovery()
        service.stop_discovery()
        self.assertFalse(worker.is_alive())

    def test_cleanup_attempts_both_socket_closes_when_one_fails(self):
        receiver, sender = Mock(), Mock()
        self.discovery._receiver, self.discovery._sender = receiver, sender
        receiver.close.side_effect = OSError("Close failed")
        self.discovery.stop_discovery()
        receiver.close.assert_called_once()
        sender.close.assert_called_once()
        self.assertIsNone(self.discovery._receiver)
        self.assertIsNone(self.discovery._sender)
        self.assertIn("cleanup", self.events.get_nowait()["error"])
        self.discovery.stop_discovery()
