from functools import partial
from pathlib import Path
import queue
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from src.chat import ChatService
from src.file_transfer import FileTransferManager
from src.main import Application, main, off_ui_thread


def available_port(ip, kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as reservation:
        reservation.bind((ip, 0))
        return reservation.getsockname()[1]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.local = {"peer_id": "127.0.0.1:5000", "name": "Local",
                      "ip": "127.0.0.1", "tcp_port": 5000, "status": "online"}

    def test_listener_failure_does_not_advertise_and_cleans_all_services(self):
        chat, files = Mock(), Mock()
        app = Application(self.local, Mock(return_value=chat), Mock(return_value=files))
        chat.start_listener.side_effect = OSError("Port occupied")
        with patch.object(app.discovery, "start_discovery") as advertise:
            with self.assertRaisesRegex(OSError, "Port occupied"):
                app.start()
        advertise.assert_not_called()
        chat.stop_listener.assert_called_once()
        files.stop_transfers.assert_called_once()

    def test_discovery_failure_stops_listener_and_file_workers(self):
        chat, files = Mock(), Mock()
        app = Application(self.local, Mock(return_value=chat), Mock(return_value=files))
        with patch.object(app.discovery, "start_discovery", side_effect=OSError("UDP unavailable")):
            with self.assertRaisesRegex(OSError, "UDP unavailable"):
                app.start()
        chat.stop_listener.assert_called_once()
        files.stop_transfers.assert_called_once()

    def test_constructor_failure_cleans_file_manager(self):
        files = Mock()
        with self.assertRaisesRegex(ValueError, "Bad chat configuration"):
            Application(self.local, Mock(side_effect=ValueError("Bad chat configuration")),
                        Mock(return_value=files))
        files.stop_transfers.assert_called_once()

    def test_shutdown_continues_after_error(self):
        chat, files = Mock(), Mock()
        app = Application(self.local, Mock(return_value=chat), Mock(return_value=files))
        with patch.object(app.discovery, "stop_discovery", side_effect=OSError("UDP cleanup")):
            with self.assertRaisesRegex(RuntimeError, "discovery: UDP cleanup"):
                app.shutdown()
        chat.stop_listener.assert_called_once()
        files.stop_transfers.assert_called_once()

    def test_lifecycle_worker_propagates_errors(self):
        with self.assertRaisesRegex(ValueError, "Lifecycle failure"):
            off_ui_thread(Mock(side_effect=ValueError("Lifecycle failure")))

    def test_invalid_cli_rejected_before_startup(self):
        with patch("src.main.Application") as app, patch("sys.stderr"):
            for args in (("--name", "Bad|name", "--ip", "127.0.0.1"),
                         ("--name", "Valid", "--ip", "0.0.0.0"),
                         ("--name", "Valid", "--ip", "127.0.0.1", "--tcp-port", "0")):
                with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                    main(args)
                self.assertEqual(error.exception.code, 2)
        app.assert_not_called()


class IntegratedNetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.alice = self.start_app("Alice", "127.0.0.2")
        self.bob = self.start_app("Bob", "127.0.0.1")
        self.announce(self.alice, self.bob)
        self.announce(self.bob, self.alice)

    def start_app(self, name, ip):
        port = available_port(ip)
        local = {"peer_id": f"{ip}:{port}", "name": name, "ip": ip,
                 "tcp_port": port, "status": "online"}
        app = Application(local, ChatService,
                          partial(FileTransferManager, downloads_dir=self.root / name))
        self.addCleanup(app.shutdown)
        app.discovery.udp_port = available_port(ip, socket.SOCK_DGRAM)
        app.start()
        return app

    def wait_event(self, events, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if predicate(event):
                return event
        self.fail("Expected network event did not arrive")

    def announce(self, source, target):
        # Use a real UDP socket with separate test ports, independent of LAN routing.
        peer = source.local_peer
        data = f"HELLO|{peer['name']}|{peer['tcp_port']}|{source.discovery.local_peer['status']}"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.bind((peer["ip"], 0))
            sender.sendto(data.encode("utf-8"), (target.local_peer["ip"], target.discovery.udp_port))
        return self.wait_event(target.events, lambda event: event["type"] == "peer_upsert"
                               and event["peer"]["peer_id"] == peer["peer_id"])

    def test_discovered_peers_chat_with_configured_source_ip_and_status(self):
        alice, bob = self.alice, self.bob
        aid, bid = alice.local_peer["peer_id"], bob.local_peer["peer_id"]
        alice.actions().send_message(bid, "Chào Bob 🙂")
        received = self.wait_event(bob.events, lambda event: event["type"] in
                                   ("conversation", "operation_failed"))
        self.assertEqual(received["type"], "conversation", received)
        self.assertEqual(received["peer_id"], aid)
        self.assertEqual(received["messages"][-1]["text"], "Chào Bob 🙂")
        self.assertEqual(received["unread"], 1)
        bob.actions().select_peer(aid)
        opened = self.wait_event(bob.events, lambda event: event["type"] == "conversation")
        self.assertEqual(opened["unread"], 0)
        bob.actions().send_message(aid, "Đã nhận")
        reply = self.wait_event(alice.events, lambda event: event["type"] == "conversation"
                                and any(not message.get("outgoing") for message in event["messages"]))
        self.assertEqual(reply["messages"][-1]["text"], "Đã nhận")
        alice.actions().set_status("away")
        changed = self.announce(alice, bob)
        self.assertEqual(changed["peer"]["status"], "away")

    def test_discovered_peers_transfer_file_with_configured_source_ip(self):
        source = self.root / "binary.bin"
        body = bytes(range(256)) * 1024
        source.write_bytes(body)
        self.alice.actions().send_file(self.bob.local_peer["peer_id"], source)
        result = self.wait_event(self.alice.events, lambda event: event["type"] in
                                 ("transfer_completed", "transfer_failed"))
        self.assertEqual(result["type"], "transfer_completed", result)
        self.assertEqual((self.root / "Bob" / source.name).read_bytes(), body)
