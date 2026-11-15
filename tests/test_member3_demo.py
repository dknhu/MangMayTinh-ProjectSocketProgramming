import hashlib
from pathlib import Path
import queue
import tempfile
import time
import unittest

from src.member3_pair_demo import create_demo_pair


class PairedDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.first, self.second = create_demo_pair(self.temp.name)

    def tearDown(self):
        self.first.shutdown()
        self.second.shutdown()
        self.temp.cleanup()

    def wait_transfer(self, backend, tid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event = backend.events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event.get("transfer_id") == tid and event["type"] in {
                    "transfer_completed", "transfer_failed", "transfer_cancelled"}:
                self.assertEqual(event["type"], "transfer_completed", event)
                return event
        self.fail("Không nhận được kết quả truyền tệp")

    def test_bidirectional_messages_and_unread_without_fake_echo(self):
        left, right = self.first, self.second
        left_id, right_id = left.local["peer_id"], right.local["peer_id"]
        left.select_peer(right_id)
        left.send_message(right_id, "Chào Minh ♡\nDòng thứ hai")
        self.assertEqual(len(left._state(right_id)["messages"]), 1)
        self.assertEqual(right._state(left_id)["messages"][0]["text"],
                         "Chào Minh ♡\nDòng thứ hai")
        self.assertEqual(right._state(left_id)["unread"], 1)
        right.select_peer(left_id)
        right.send_message(left_id, "Như ơi, nhận được rồi!")
        self.assertEqual(len(left._state(right_id)["messages"]), 2)
        self.assertEqual(len(right._state(left_id)["messages"]), 2)
        self.assertEqual(right._state(left_id)["unread"], 0)
        self.assertEqual(left._state(right_id)["unread"], 0)
        right.set_status("away")
        self.assertEqual(left.peers[right_id]["status"], "away")

    def test_real_tcp_files_both_directions_and_separate_downloads(self):
        for sender, receiver, name in ((self.first, self.second, "gửi-từ-nhu.bin"),
                                       (self.second, self.first, "gửi-từ-minh.bin")):
            data = bytes(range(256)) * 1024
            source = Path(self.temp.name) / name
            source.write_bytes(data)
            tid = sender.send_file(receiver.local["peer_id"], source)
            sent = self.wait_transfer(sender, tid)
            received = self.wait_transfer(receiver, tid)
            self.assertEqual(Path(received["path"]).read_bytes(), data)
            self.assertEqual(sent["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(Path(received["path"]).parent, receiver.transfers.downloads_dir)
            self.assertEqual(list(receiver.transfers.downloads_dir.glob("*.part")), [])
        self.assertNotEqual(self.first.transfers.downloads_dir, self.second.transfers.downloads_dir)

    def test_closing_partner_disables_delivery_instead_of_echo(self):
        self.second.shutdown()
        with self.assertRaises(ConnectionError):
            self.first.send_message(self.second.local["peer_id"], "Không được báo gửi thành công")
        seen = []
        while True:
            try:
                seen.append(self.first.events.get_nowait())
            except queue.Empty:
                break
        self.assertTrue(any(event.get("type") == "peer_removed" and
                            event.get("peer_id") == self.second.local["peer_id"]
                            for event in seen))

    def test_demo_history_keeps_last_500_messages(self):
        pid = self.second.local["peer_id"]
        for number in range(501):
            self.first.send_message(pid, f"Tin {number}")
        history = self.first._state(pid)["messages"]
        received = self.second._state(self.first.local["peer_id"])["messages"]
        self.assertEqual(len(history), 500)
        self.assertEqual(len(received), 500)
        self.assertEqual(history[0]["text"], "Tin 1")
        self.assertEqual(received[-1]["text"], "Tin 500")


if __name__ == "__main__":
    unittest.main()
