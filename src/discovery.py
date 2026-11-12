import queue
import socket
import threading
import time
import uuid


class DiscoveryService:
    def __init__(
        self,
        name,
        tcp_port,
        discovery_port=4000,
        broadcast_ip="255.255.255.255",
        heartbeat_interval=3.0,
        peer_timeout=10.0,
    ):
        if not name.strip() or "|" in name:
            raise ValueError("Name must be non-empty and cannot contain '|'")

        if not 1 <= tcp_port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535")

        if not 1 <= discovery_port <= 65535:
            raise ValueError("Discovery port must be between 1 and 65535")

        if heartbeat_interval <= 0 or peer_timeout <= 0:
            raise ValueError("Time intervals must be positive")

        self.name = name
        self.tcp_port = tcp_port
        self.discovery_port = discovery_port
        self.broadcast_ip = broadcast_ip
        self.heartbeat_interval = heartbeat_interval
        self.peer_timeout = peer_timeout

        self.peer_id = str(uuid.uuid4())
        self.status = "online"
        self.peers = {}
        self.events = queue.Queue()

        self.udp_socket = None
        self._worker = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def start(self):
        if self._worker is not None:
            raise RuntimeError("Stop the current service before starting again")

        self._stop_event.clear()

        try:
            self.udp_socket = socket.socket(
                socket.AF_INET,
                socket.SOCK_DGRAM,
            )

            self.udp_socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BROADCAST,
                1,
            )

            self.udp_socket.bind(("", self.discovery_port))
            self.udp_socket.settimeout(0.2)

            self._worker = threading.Thread(
                target=self._run,
                name="discovery-worker",
            )
            self._worker.start()

        except Exception:
            self._worker = None

            if self.udp_socket is not None:
                self.udp_socket.close()
                self.udp_socket = None

            raise

    def stop(self):
        self._stop_event.set()

        if self._worker is not None:
            self._worker.join()
            self._worker = None

        if self.udp_socket is not None:
            self.udp_socket.close()
            self.udp_socket = None

        with self._lock:
            self.peers.clear()

    def set_status(self, status):
        if status not in ("online", "away"):
            raise ValueError("Status must be 'online' or 'away'")

        with self._lock:
            self.status = status

    def get_peers(self):
        with self._lock:
            return [peer.copy() for peer in self.peers.values()]

    def send_presence(self):
        with self._lock:
            status = self.status

        message = (
            f"{self.peer_id}|{self.name}|"
            f"{self.tcp_port}|{status}"
        )

        self.udp_socket.sendto(
            message.encode("utf-8"),
            (self.broadcast_ip, self.discovery_port),
        )

    def parse_presence(self, data, sender_ip):
        try:
            message = data.decode("utf-8")
            fields = message.split("|")

            if len(fields) != 4:
                return None

            peer_id, name, port_text, status = fields
            peer_id = str(uuid.UUID(peer_id))
            tcp_port = int(port_text)

        except (UnicodeDecodeError, ValueError):
            return None

        if not name.strip():
            return None

        if not 1 <= tcp_port <= 65535:
            return None

        if status not in ("online", "away"):
            return None

        return {
            "peer_id": peer_id,
            "name": name,
            "ip": sender_ip,
            "tcp_port": tcp_port,
            "status": status,
        }

    def update_peer(self, peer):
        if peer["peer_id"] == self.peer_id:
            return

        peer_key = (peer["ip"], peer["tcp_port"])

        with self._lock:
            previous = self.peers.get(peer_key)

            changed = previous is None or any(
                previous[field] != peer[field]
                for field in ("peer_id", "name", "status")
            )

            peer["last_seen"] = time.monotonic()
            self.peers[peer_key] = peer

            if changed:
                event_type = (
                    "PEER_ADDED"
                    if previous is None
                    else "PEER_UPDATED"
                )

                self.events.put({
                    "type": event_type,
                    "peer": peer.copy(),
                })

    def remove_expired_peers(self):
        current_time = time.monotonic()

        with self._lock:
            expired_keys = [
                peer_key
                for peer_key, peer in self.peers.items()
                if current_time - peer["last_seen"] > self.peer_timeout
            ]

            for peer_key in expired_keys:
                peer = self.peers.pop(peer_key)

                self.events.put({
                    "type": "PEER_REMOVED",
                    "peer": peer.copy(),
                })

    def _run(self):
        next_heartbeat = time.monotonic()

        try:
            while not self._stop_event.is_set():
                if time.monotonic() >= next_heartbeat:
                    self.send_presence()
                    next_heartbeat = (
                        time.monotonic() + self.heartbeat_interval
                    )

                try:
                    data, address = self.udp_socket.recvfrom(4096)
                except socket.timeout:
                    pass
                else:
                    sender_ip, _ = address
                    peer = self.parse_presence(data, sender_ip)

                    if peer is not None:
                        self.update_peer(peer)

                self.remove_expired_peers()

        except Exception as error:
            self.events.put({
                "type": "DISCOVERY_ERROR",
                "message": f"{type(error).__name__}: {error}",
            })
        finally:
            self._stop_event.set()