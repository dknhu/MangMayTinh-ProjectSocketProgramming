import errno
import ipaddress
import socket
import threading
import time
import unicodedata

from .protocol import peer_id


def validate_name(name):
    if not isinstance(name, str):
        raise ValueError("Name must be a string")
    if not 1 <= len(name.encode("utf-8")) <= 64:
        raise ValueError("Name must contain 1 to 64 UTF-8 bytes")
    if "|" in name or any(unicodedata.category(c) == "Cc" for c in name):
        raise ValueError("Name cannot contain delimiters or control characters")
    return name


def validate_ip(ip):
    address = ipaddress.IPv4Address(ip)
    if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
        raise ValueError("A concrete IPv4 endpoint is required")
    return str(address)


class DiscoveryService:
    HEARTBEAT_INTERVAL = 3.0
    PEER_TIMEOUT = 10.0
    MAX_DATAGRAM = 512

    def __init__(self, local_peer, event_queue, udp_port=9999):
        self.local_peer = dict(local_peer)
        self.local_peer["name"] = validate_name(local_peer["name"])
        self.local_peer["ip"] = validate_ip(local_peer["ip"])
        expected = peer_id(self.local_peer["ip"], local_peer["tcp_port"])
        if local_peer.get("peer_id") != expected:
            raise ValueError("Local peer ID must match IPv4:TCP_port")
        if local_peer.get("status") not in ("online", "away"):
            raise ValueError("Status must be online or away")
        if type(udp_port) is not int or not 1 <= udp_port <= 65535:
            raise ValueError("UDP port must be between 1 and 65535")
        self.events = event_queue
        self.udp_port = udp_port
        self._peers = {}
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._receiver = None
        self._sender = None
        self._next_send = 0.0

    def _report_error(self, error):
        self.events.put({"type": "operation_failed", "error": f"Discovery: {error}"})

    def start_discovery(self):
        with self._lifecycle:
            if self._thread is not None:
                if self._thread.is_alive() and not self._stop.is_set():
                    return
                raise RuntimeError("Stop discovery before restarting a failed worker")
            try:
                self._stop.clear()
                self._receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._receiver.bind(("", self.udp_port))
                self._receiver.settimeout(0.1)
                self._sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                self._sender.bind((self.local_peer["ip"], 0))
                self._sender.settimeout(0.5)
                with self._lock:
                    self._send_presence()
                self._thread = threading.Thread(target=self._run, name="udp-discovery")
                self._thread.start()
            except Exception as error:
                self._thread = None
                self._stop.set()
                self._close_sockets()
                self._report_error(error)
                raise

    def _close_sockets(self):
        for attribute in ("_receiver", "_sender"):
            sock = getattr(self, attribute)
            if sock is not None:
                sock.close()
                setattr(self, attribute, None)

    def stop_discovery(self):
        with self._lifecycle:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=2.0)
                if self._thread.is_alive():
                    error = RuntimeError("Discovery worker did not stop within 2 seconds")
                    self._report_error(error)
                    raise error
                self._thread = None
            self._close_sockets()
            with self._lock:
                for pid in self._peers:
                    self.events.put({"type": "peer_removed", "peer_id": pid})
                self._peers.clear()

    def get_peers(self):
        with self._lock:
            return {pid: peer.copy() for pid, peer in self._peers.items()}

    def set_status(self, status):
        if status not in ("online", "away"):
            raise ValueError("Status must be online or away")
        with self._lifecycle, self._lock:
            self.local_peer["status"] = status
            self.events.put({"type": "status_changed", "status": status})
            if self._sender is not None and not self._stop.is_set():
                try:
                    self._send_presence()
                except OSError as error:
                    self._report_error(error)
                    raise

    def _send_presence(self):
        # Caller owns _lock; status changes and scheduled sends cannot interleave.
        message = "HELLO|{name}|{tcp_port}|{status}".format(**self.local_peer)
        self._sender.sendto(message.encode("utf-8"), ("255.255.255.255", self.udp_port))
        self._next_send = time.monotonic() + self.HEARTBEAT_INTERVAL

    def _parse_presence(self, data, source_ip):
        if len(data) > self.MAX_DATAGRAM:
            return None
        try:
            fields = data.decode("utf-8").split("|")
            if len(fields) != 4 or fields[0] != "HELLO":
                return None
            _, name, port_text, status = fields
            validate_name(name)
            ip = validate_ip(source_ip)
            if not port_text or not port_text.isascii() or not port_text.isdecimal():
                return None
            port = int(port_text)
            pid = peer_id(ip, port)
            if status not in ("online", "away"):
                return None
        except (ValueError, UnicodeError, OSError):
            return None
        return {"peer_id": pid, "name": name, "ip": ip, "tcp_port": port,
                "status": status, "last_seen": time.monotonic()}

    def _accept_presence(self, data, source_ip):
        peer = self._parse_presence(data, source_ip)
        if peer is None or peer["peer_id"] == self.local_peer["peer_id"]:
            return
        with self._lock:
            self._peers[peer["peer_id"]] = peer
            self.events.put({"type": "peer_upsert", "peer": peer.copy()})

    def _expire_peers(self):
        now = time.monotonic()
        with self._lock:
            expired = [pid for pid, peer in self._peers.items()
                       if now - peer["last_seen"] > self.PEER_TIMEOUT]
            for pid in expired:
                del self._peers[pid]
                self.events.put({"type": "peer_removed", "peer_id": pid})

    def _run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    if not self._stop.is_set() and time.monotonic() >= self._next_send:
                        self._send_presence()
                try:
                    # Extra byte detects oversized packets instead of accepting a prefix.
                    data, address = self._receiver.recvfrom(self.MAX_DATAGRAM + 1)
                except socket.timeout:
                    pass
                except OSError as error:
                    # Windows may reject oversized datagrams rather than truncate them.
                    if error.errno != errno.EMSGSIZE and getattr(error, "winerror", None) != 10040:
                        raise
                else:
                    self._accept_presence(data, address[0])
                self._expire_peers()
        except Exception as error:
            self._report_error(error)
        finally:
            self._stop.set()
