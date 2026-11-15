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
    if not name.strip():
        raise ValueError("Name must contain a non-whitespace character")
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
    RETRY_INITIAL = 1.0
    RETRY_MAX = 8.0

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
        self._state = "stopped"
        self._retry_delay = self.RETRY_INITIAL

    def _report_error(self, error):
        self.events.put({"type": "operation_failed", "error": f"Discovery: {error}"})

    def _open_sockets(self):
        # Caller owns _lock; cleanup also covers a partially opened pair.
        self._receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._receiver.bind(("", self.udp_port))
        self._receiver.settimeout(0.1)
        self._sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sender.bind((self.local_peer["ip"], 0))
        self._sender.settimeout(0.5)

    def start_discovery(self):
        with self._lifecycle:
            if self._thread is not None:
                if self._thread.is_alive():
                    if not self._stop.is_set():
                        return  # Includes a worker currently waiting to retry.
                    raise RuntimeError("Discovery is still stopping; try again after stop")
                self._thread.join()
                self._thread = None
            with self._lock:
                try:
                    self._stop.clear()
                    self._retry_delay = self.RETRY_INITIAL
                    self._open_sockets()
                    self._send_presence()
                    self._state = "running"
                    self._thread = threading.Thread(target=self._run, name="udp-discovery")
                    self._thread.start()
                except Exception as error:
                    self._thread = None
                    self._stop.set()
                    self._state = "stopped"
                    self._close_sockets()
                    self._clear_peers()
                    self._report_error(error)
                    raise

    def _close_sockets(self):
        # Detach both references and attempt both closes even if one close fails.
        for attribute in ("_receiver", "_sender"):
            sock = getattr(self, attribute)
            setattr(self, attribute, None)
            if sock is not None:
                try:
                    sock.close()
                except OSError as error:
                    self._report_error(f"Socket cleanup failed: {error}")

    def _clear_peers(self):
        # Caller owns _lock. Only presence is invalidated, never TCP or file state.
        for pid in self._peers:
            self.events.put({"type": "peer_removed", "peer_id": pid})
        self._peers.clear()

    def _begin_retry(self, error, delay=None):
        # Caller owns _lock; the same worker recreates sockets after backoff.
        self._state = "retrying"
        self._retry_delay = self.RETRY_INITIAL if delay is None else delay
        self._close_sockets()
        self._clear_peers()
        self._report_error(
            f"Presence unavailable: {error}. UDP retry in {self._retry_delay:g}s. "
            "TCP chat and file transfers are not stopped."
        )

    def stop_discovery(self):
        with self._lifecycle:
            self._stop.set()  # Interrupts backoff immediately, including the 8s cap.
            if self._thread is not None:
                self._thread.join(timeout=2.0)
                if self._thread.is_alive():
                    error = RuntimeError("Discovery worker did not stop within 2 seconds")
                    self._report_error(error)
                    raise error
                self._thread = None
            with self._lock:
                self._state = "stopped"
                self._close_sockets()
                self._clear_peers()

    def get_peers(self):
        with self._lock:
            return {pid: peer.copy() for pid, peer in self._peers.items()}

    def set_status(self, status):
        if status not in ("online", "away"):
            raise ValueError("Status must be online or away")
        with self._lifecycle, self._lock:
            if self._state != "running" or self._stop.is_set() or self._sender is None:
                error = RuntimeError(
                    "Discovery is stopped or recovering; status was not changed or advertised"
                )
                self._report_error(error)
                raise error
            try:
                self._send_presence(status)
            except OSError as error:
                self._begin_retry(error)
                raise OSError(f"Status was not changed or advertised: {error}") from error
            # Commit the local choice and notify GUI only after sendto succeeds.
            # UDP send success does not guarantee delivery to another machine.
            self.local_peer["status"] = status
            self.events.put({"type": "status_changed", "status": status})

    def _send_presence(self, status=None):
        # Caller owns _lock; never mutate local status before a successful send.
        presence = dict(self.local_peer)
        if status is not None:
            presence["status"] = status
        message = "HELLO|{name}|{tcp_port}|{status}".format(**presence)
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

    def _retry_once(self):
        delay = self._retry_delay
        if self._stop.wait(delay):
            return
        with self._lock:
            if self._stop.is_set():
                return
            try:
                self._open_sockets()
                if self._stop.is_set():
                    return
                self._send_presence()
            except OSError as error:
                self._begin_retry(error, min(delay * 2, self.RETRY_MAX))
            else:
                self._state = "running"
                self._retry_delay = self.RETRY_INITIAL
                self.events.put({"type": "status_changed", "status": self.local_peer["status"]})

    def _receive_once(self, receiver):
        try:
            # Extra byte detects oversized packets instead of accepting a prefix.
            data, address = receiver.recvfrom(self.MAX_DATAGRAM + 1)
        except socket.timeout:
            return
        except OSError as error:
            # Windows may reject oversized datagrams rather than truncate them.
            if error.errno == errno.EMSGSIZE or getattr(error, "winerror", None) == 10040:
                return
            with self._lock:
                if receiver is self._receiver and not self._stop.is_set():
                    raise
        else:
            with self._lock:
                # set_status may have invalidated this socket while recv was pending.
                if receiver is self._receiver and self._state == "running" and not self._stop.is_set():
                    self._accept_presence(data, address[0])

    def _run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    state = self._state
                if state == "retrying":
                    self._retry_once()
                    continue
                if state != "running":
                    break
                try:
                    with self._lock:
                        if self._stop.is_set() or self._state != "running":
                            continue
                        if time.monotonic() >= self._next_send:
                            self._send_presence()
                        receiver = self._receiver
                    self._receive_once(receiver)
                    self._expire_peers()
                except OSError as error:
                    with self._lock:
                        if not self._stop.is_set() and self._state == "running":
                            self._begin_retry(error)
        except Exception as error:
            self._report_error(f"Worker stopped: {error}. Call start_discovery to restart.")
        finally:
            with self._lock:
                self._stop.set()
                self._state = "stopped"
                self._close_sockets()
                self._clear_peers()
