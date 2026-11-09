"""Run member 3 alone: real TCP files, simulated peers/chat/status.

python -m src.member3_demo
python -m src.member3_demo --name Quan --port 5000 --peer 192.168.1.20:5001
This adapter is a test harness, not the member 1/2 implementation.
"""

import argparse
from pathlib import Path
import queue
import socket
import threading

from .file_transfer import FileTransferManager
from .gui import UIActions, run_ui
from .protocol import peer_id, recv_frame


class FileDemoListener:
    """Minimal FILE-only listener exercising the documented OPEN handoff."""
    def __init__(self, manager, host="0.0.0.0", port=0):
        self.manager = manager
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._pending = set()
        self._handlers = set()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind((host, port))
            self.sock.listen(16)
            self.sock.settimeout(0.2)
        except Exception:
            self.sock.close()
            raise
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, name="demo-file-listener")
        self.thread.start()

    def _accept(self):
        while not self._stopping.is_set():
            try:
                conn, address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(3)
            with self._lock:
                if self._stopping.is_set() or len(self._handlers) >= 16:
                    conn.close()
                    continue
                worker = threading.Thread(target=self._handle, args=(conn, address),
                                          name="demo-open-handshake")
                self._pending.add(conn)
                self._handlers.add(worker)
                worker.start()

    def _handle(self, conn, address):
        transferred = False
        try:
            opening, _ = recv_frame(conn)
            if self._stopping.is_set():
                return
            self.manager.receive_file(conn, opening, address[0])
            transferred = True
        except Exception as exc:
            if not self._stopping.is_set():
                self.manager.events.put({"type": "operation_failed", "error": str(exc)})
        finally:
            with self._lock:
                self._pending.discard(conn)
                self._handlers.discard(threading.current_thread())
            if not transferred:
                conn.close()

    def stop(self):
        self._stopping.set()
        self.sock.close()
        self.thread.join(2)
        with self._lock:
            pending, handlers = list(self._pending), list(self._handlers)
        for conn in pending:
            FileTransferManager._close(conn)
        for worker in handlers:
            worker.join(4)
        if self.thread.is_alive() or any(worker.is_alive() for worker in handlers):
            raise TimeoutError("Listener thử nghiệm chưa dừng")


class DemoBackend:
    def __init__(self, local, events, target=None, downloads=None, host="0.0.0.0"):
        self.local = local
        self.events = events
        self._lock = threading.RLock()
        self._active = None
        self._conversations = {}
        self.peers = {}
        self.transfers = FileTransferManager(local, events, **(
            {"downloads_dir": downloads} if downloads is not None else {}))
        self.listener = FileDemoListener(self.transfers, host, local["tcp_port"])
        if target:
            self.add_peer(target)
        else:
            for number, name in ((5001, "Minh (mô phỏng)"), (5002, "Lan (mô phỏng)")):
                if number == local["tcp_port"]:
                    number += 10
                self.add_peer({"name": name, "ip": "127.0.0.1", "tcp_port": number,
                               "peer_id": peer_id("127.0.0.1", number), "status": "online"})

    def add_peer(self, peer):
        self.peers[peer["peer_id"]] = dict(peer)
        self.events.put({"type": "peer_upsert", "peer": dict(peer)})

    def _state(self, pid):
        return self._conversations.setdefault(pid, {"messages": [], "unread": 0, "revision": 0})

    def _publish(self, pid):
        state = self._state(pid)
        state["revision"] += 1
        self.events.put({"type": "conversation", "peer_id": pid,
                         "messages": [dict(message) for message in state["messages"]],
                         "unread": state["unread"], "revision": state["revision"]})

    def select_peer(self, pid):
        with self._lock:
            self._active = pid
            if pid is not None:
                self._state(pid)["unread"] = 0
                self._publish(pid)

    def send_message(self, pid, text):
        with self._lock:
            self._state(pid)["messages"].append({"text": text, "outgoing": True})
            self._publish(pid)
        # Explicitly simulated; no claim that this message crossed the LAN.
        self.inject_message(pid, f"[Mô phỏng] Đã nhận: {text}")

    def inject_message(self, pid, text):
        with self._lock:
            state = self._state(pid)
            state["messages"].append({"text": text, "sender": self.peers[pid]["name"]})
            if self._active != pid:
                state["unread"] += 1
            self._publish(pid)

    def send_file(self, pid, path):
        return self.transfers.send_file(self.peers[pid], path)

    def set_status(self, status):
        if status not in {"online", "away"}:
            raise ValueError("Trạng thái không hợp lệ")
        self.events.put({"type": "status_changed", "status": status})

    def leave_chat(self, pid):
        with self._lock:
            if self._active == pid:
                self._active = None

    def shutdown(self):
        errors = []
        for stop in (self.listener.stop, self.transfers.stop_transfers):
            try:
                stop()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))

    def actions(self):
        return UIActions(self.select_peer, self.send_message, self.send_file, self.set_status,
                         self.leave_chat, self.transfers.cancel_transfer, self.shutdown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Quan")
    parser.add_argument("--ip", default="127.0.0.1", help="IPv4 cục bộ dùng trong OPEN")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--peer", help="IP:TCP_PORT đích (thêm thủ công, không phải UDP discovery)")
    parser.add_argument("--downloads", type=Path)
    args = parser.parse_args()
    backend = None
    try:
        local = {"name": args.name, "ip": args.ip, "tcp_port": args.port,
                 "peer_id": peer_id(args.ip, args.port), "status": "online"}
        target = None
        if args.peer:
            ip, port_text = args.peer.rsplit(":", 1)
            port = int(port_text)
            target = {"name": f"Peer {ip}:{port}", "ip": ip, "tcp_port": port,
                      "peer_id": peer_id(ip, port), "status": "online"}
        events = queue.Queue()
        backend = DemoBackend(local, events, target, args.downloads, args.bind)
        run_ui(args.name, events, backend.actions(), demo=True)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Không khởi động được bản thử: {exc}\n")
    finally:
        if backend is not None:
            backend.shutdown()


if __name__ == "__main__":
    main()
