"""TCP chat, conversation state, and OPEN routing for the shared listener."""

from collections import deque
from dataclasses import dataclass, field
import socket
import threading
import time

from .protocol import MAX_CONTROL_BYTES, ProtocolError, peer_id, recv_frame, send_frame


@dataclass
class _Conversation:
    messages: list = field(default_factory=list)
    unread: int = 0
    revision: int = 0


@dataclass(eq=False)
class _Session:
    sock: socket.socket
    name: str
    write_lock: threading.Lock = field(default_factory=threading.Lock)


class ChatService:
    """Expose quick GUI callbacks and run socket I/O on background workers."""

    def __init__(self, local_peer, event_queue, resolve_peer, file_receiver,
                 bind_host="0.0.0.0"):
        self._local = dict(local_peer)
        if self._local["peer_id"] != peer_id(self._local["ip"], self._local["tcp_port"]):
            raise ValueError("local_peer.peer_id phải khớp IP:TCP port")
        self._events = event_queue
        self._resolve_peer = resolve_peer
        self._file_receiver = file_receiver
        self._bind_host = bind_host
        self._lock = threading.Lock()
        self._listener = None
        self._stopped = False
        self._workers = set()
        self._sockets = set()
        self._handshakes = threading.BoundedSemaphore(16)
        self._sessions = {}
        self._outboxes = {}
        self._conversations = {}
        self._active = None

    def start_listener(self):
        with self._lock:
            if self._stopped:
                raise RuntimeError("ChatService đã dừng")
            if self._listener is not None:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((self._bind_host, self._local["tcp_port"]))
                listener.listen()
                listener.settimeout(0.25)
                self._listener = listener
                self._start_worker(self._accept, listener)
            except BaseException:
                self._listener = None
                listener.close()
                raise

    def stop_listener(self):
        with self._lock:
            self._stopped = True
            listener, self._listener = self._listener, None
            sockets = tuple(self._sockets)
            workers = tuple(self._workers)
        self._close(listener)
        for sock in sockets:
            self._close(sock)
        deadline = time.monotonic() + 5
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise RuntimeError("Luồng chat chưa dừng đúng hạn")

    def select_peer(self, peer_id):
        with self._lock:
            self._require_running()
            self._active = peer_id
            if peer_id is not None:
                self._conversation(peer_id).unread = 0
                self._publish(peer_id)
                if peer_id not in self._sessions and peer_id not in self._outboxes:
                    self._enqueue(peer_id, (None, b""))

    def send_message(self, peer_id, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Tin nhắn không được để trống")
        payload = text.encode("utf-8")
        if len(payload) > MAX_CONTROL_BYTES:
            raise ValueError("Tin nhắn vượt quá 64 KiB UTF-8")
        with self._lock:
            self._require_running()
            message = {"text": text, "outgoing": True, "state": "pending"}
            self._enqueue(peer_id, (message, payload))
            self._conversation(peer_id).messages.append(message)
            self._publish(peer_id)

    def leave_chat(self, peer_id):
        with self._lock:
            self._require_running()
            if self._active == peer_id:
                self._active = None
            outbox = self._outboxes.pop(peer_id, ())
            cancelled = [message for message, _ in outbox if message is not None]
            for message in cancelled:
                message["state"] = "failed"
            if cancelled:
                self._publish(peer_id)
                self._error(f"Đã hủy lệnh gửi đang chờ tới {peer_id}")
            sessions = self._sessions.pop(peer_id, ())
            if sessions:
                self._start_worker(self._leave, sessions)

    def _require_running(self):
        if self._listener is None:
            raise RuntimeError("TCP listener chưa chạy")

    def _conversation(self, pid):
        return self._conversations.setdefault(pid, _Conversation())

    def _publish(self, pid):
        # Caller holds the state lock so revision and snapshot stay in order.
        conversation = self._conversation(pid)
        conversation.revision += 1
        self._events.put_nowait({
            "type": "conversation", "peer_id": pid, "revision": conversation.revision,
            "unread": conversation.unread,
            "messages": [dict(message) for message in conversation.messages],
        })

    def _error(self, error):
        self._events.put_nowait({"type": "operation_failed", "error": str(error)})

    def _start_worker(self, target, *args):
        # Register and start under the state lock before stop can take a snapshot.
        def run():
            try:
                target(*args)
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=run, name=f"chat-{target.__name__[1:]}", daemon=True)
        self._workers.add(worker)
        try:
            worker.start()
        except RuntimeError:
            self._workers.discard(worker)
            raise

    @staticmethod
    def _close(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _release(self, sock):
        with self._lock:
            self._sockets.discard(sock)
        self._close(sock)

    def _accept(self, listener):
        while True:
            try:
                conn, (source_ip, _) = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                with self._lock:
                    if not self._stopped:
                        self._error(exc)
                return
            with self._lock:
                if self._stopped or not self._handshakes.acquire(blocking=False):
                    conn.close()
                    continue
                self._sockets.add(conn)
                try:
                    self._start_worker(self._incoming, conn, source_ip)
                except RuntimeError as exc:
                    self._handshakes.release()
                    self._sockets.remove(conn)
                    conn.close()
                    self._error(exc)

    def _incoming(self, conn, source_ip):
        handed_off = False
        session = None
        try:
            conn.settimeout(3)
            opening, _ = recv_frame(conn)
            purpose = opening.get("purpose")
            if opening["type"] != "OPEN" or purpose not in ("chat", "file"):
                raise ProtocolError("Frame đầu phải là OPEN chat hoặc file")
            with self._lock:
                if self._stopped:
                    return
                if purpose == "file":
                    self._sockets.remove(conn)
                    handed_off = True
                else:
                    pid = peer_id(source_ip, opening.get("tcp_port"))
                    name = opening.get("name")
                    if (opening.get("peer_id") != pid or pid == self._local["peer_id"]
                            or not isinstance(name, str) or not name.strip()):
                        raise ProtocolError("Danh tính peer trong OPEN không hợp lệ")
                    conn.settimeout(None)
                    session = _Session(conn, name)
                    self._sessions.setdefault(pid, []).append(session)
        except (OSError, ValueError) as exc:
            with self._lock:
                if not self._stopped:
                    self._error(exc)
        finally:
            self._handshakes.release()
            if not handed_off and session is None:
                self._release(conn)
        if handed_off:
            try:
                self._file_receiver(conn, opening, source_ip)
            except Exception as exc:
                self._error(exc)
        elif session is not None:
            self._receive(pid, session)

    def _enqueue(self, pid, item):
        outbox = self._outboxes.get(pid)
        if outbox is None:
            outbox = deque()
            self._outboxes[pid] = outbox
            try:
                self._start_worker(self._send, pid, outbox)
            except RuntimeError:
                del self._outboxes[pid]
                raise
        outbox.append(item)

    def _send(self, pid, outbox):
        while True:
            with self._lock:
                if self._outboxes.get(pid) is not outbox:
                    return
                if not outbox:
                    del self._outboxes[pid]
                    return
                message, payload = outbox.popleft()
                sessions = self._sessions.get(pid)
                session = sessions[0] if sessions else None
            try:
                if session is None:
                    session = self._connect(pid, outbox)
                if message is not None:
                    with session.write_lock:
                        with self._lock:
                            if self._stopped or self._outboxes.get(pid) is not outbox:
                                raise ConnectionAbortedError("Phiên chat đã đóng")
                        send_frame(session.sock, {"type": "MSG"}, payload)
            except (OSError, ValueError, LookupError, RuntimeError) as exc:
                if session is not None:
                    self._drop(pid, session)
                with self._lock:
                    if not self._stopped and self._outboxes.get(pid) is outbox:
                        self._error(f"Chat với {pid}: {exc}")
                state = "failed"
            else:
                state = "sent"
            if message is not None:
                with self._lock:
                    message["state"] = state
                    self._publish(pid)

    def _connect(self, pid, outbox):
        peer = self._resolve_peer(pid)
        if peer is None or peer.get("status") == "offline":
            raise ConnectionError("Peer đã offline")
        address = (peer["ip"], peer["tcp_port"])
        if peer_id(*address) != pid or pid == self._local["peer_id"]:
            raise ValueError("Địa chỉ peer không hợp lệ")
        name = peer["name"]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        session = _Session(sock, name)
        try:
            with self._lock:
                if self._stopped or self._outboxes.get(pid) is not outbox:
                    raise ConnectionError("Phiên chat đã đóng")
                self._sockets.add(sock)
            sock.settimeout(3)
            sock.connect(address)
            send_frame(sock, {"type": "OPEN", "purpose": "chat",
                              "peer_id": self._local["peer_id"], "name": self._local["name"],
                              "tcp_port": self._local["tcp_port"]})
            sock.settimeout(None)
            with self._lock:
                if self._stopped or self._outboxes.get(pid) is not outbox:
                    raise ConnectionError("Phiên chat đã đóng")
                sessions = self._sessions.setdefault(pid, [])
                sessions.append(session)
                try:
                    self._start_worker(self._receive, pid, session)
                except RuntimeError:
                    sessions.remove(session)
                    raise
        except BaseException:
            self._release(sock)
            raise
        return session

    def _receive(self, pid, session):
        try:
            while True:
                header, payload = recv_frame(session.sock)
                if header["type"] == "BYE":
                    with self._lock:
                        if not self._stopped and session in self._sessions.get(pid, ()):
                            self._error(f"{session.name} đã rời phiên chat")
                    return
                if header["type"] != "MSG":
                    raise ProtocolError("Phiên chat chỉ nhận MSG hoặc BYE")
                text = payload.decode("utf-8")
                with self._lock:
                    if self._stopped or session not in self._sessions.get(pid, ()):
                        return
                    conversation = self._conversation(pid)
                    conversation.messages.append({
                        "text": text, "sender": session.name, "outgoing": False,
                    })
                    if self._active != pid:
                        conversation.unread += 1
                    self._publish(pid)
        except (OSError, ValueError) as exc:
            with self._lock:
                if not self._stopped and session in self._sessions.get(pid, ()):
                    self._error(f"Chat với {session.name}: {exc}")
        finally:
            self._drop(pid, session)

    def _drop(self, pid, session):
        with self._lock:
            sessions = self._sessions.get(pid)
            if sessions and session in sessions:
                sessions.remove(session)
                if not sessions:
                    del self._sessions[pid]
        self._release(session.sock)

    def _leave(self, sessions):
        for session in sessions:
            try:
                with session.write_lock:
                    send_frame(session.sock, {"type": "BYE"})
            except OSError:
                pass
            finally:
                self._release(session.sock)
