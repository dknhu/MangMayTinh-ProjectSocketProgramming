"""Background file transfers over dedicated TCP connections.

The shared listener consumes OPEN only, then calls receive_file(sock, opening,
remote_ip). This manager owns the handed-off socket, including closing it.
All results are dictionaries posted to a queue; this module never imports Tk.
"""

import hashlib
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

from .protocol import (CHUNK_SIZE, MAX_FILE_BYTES, ProtocolError, peer_id,
                       read_header, recv_frame, send_frame, send_header)

DEFAULT_DOWNLOADS = Path(__file__).resolve().parents[1] / "downloads"


class TransferCancelled(Exception):
    pass


def safe_filename(name):
    """Reject remote paths and Windows device names on every platform."""
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240:
        raise ProtocolError("Tên file rỗng hoặc quá dài")
    if any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name):
        raise ProtocolError("Tên file chứa đường dẫn hoặc ký tự không hợp lệ")
    if name in {".", ".."} or name.endswith((".", " ")):
        raise ProtocolError("Tên file không hợp lệ")
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(
            r"(?:COM|LPT)[1-9¹²³]", stem):
        raise ProtocolError("Tên file dành riêng của hệ điều hành")
    return name


def _transfer_id(value):
    if not isinstance(value, str):
        raise ProtocolError("Thiếu transfer_id")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ProtocolError("transfer_id phải là UUID chuẩn") from exc
    return value


@dataclass
class _Job:
    transfer_id: str
    peer_id: str
    direction: str
    filename: str = ""
    total_bytes: int = 0
    bytes_done: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    sock: object = None
    thread: object = None
    last_progress: float = 0.0
    committed: bool = False


class FileTransferManager:
    def __init__(self, local_peer, event_queue, downloads_dir=DEFAULT_DOWNLOADS,
                 timeout=15.0, max_file_bytes=MAX_FILE_BYTES, max_transfers=8):
        self.local_peer = dict(local_peer)
        expected = peer_id(local_peer["ip"], local_peer["tcp_port"])
        if local_peer.get("peer_id") != expected:
            raise ValueError("local_peer.peer_id phải khớp IP:TCP port")
        if not 0 <= max_file_bytes <= MAX_FILE_BYTES or timeout <= 0 or max_transfers < 1:
            raise ValueError("Cấu hình truyền file không hợp lệ")
        self.events = event_queue
        self.downloads_dir = Path(downloads_dir).resolve()
        self.timeout = timeout
        self.max_file_bytes = max_file_bytes
        self.max_transfers = max_transfers
        self._lock = threading.RLock()
        self._jobs = {}
        self._stopped = False

    def _emit(self, job, kind, **extra):
        event = {"type": kind, "transfer_id": job.transfer_id,
                 "peer_id": job.peer_id, "direction": job.direction,
                 "filename": job.filename, "total_bytes": job.total_bytes,
                 "bytes_done": job.bytes_done,
                 "percent": 100.0 * job.bytes_done / job.total_bytes
                 if job.total_bytes else 0.0, **extra}
        # Contract: an unbounded queue avoids blocking a network worker on UI.
        self.events.put_nowait(event)

    def _progress(self, job, force=False, **extra):
        now = time.monotonic()
        if force or now - job.last_progress >= 0.1:
            job.last_progress = now
            self._emit(job, "transfer_progress", **extra)

    def _check(self, job):
        if job.cancel.is_set():
            raise TransferCancelled("Đã hủy truyền file")

    def _attach(self, job, sock):
        with self._lock:
            job.sock = sock
            self._check(job)

    @staticmethod
    def _close(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _start(self, job, operation):
        with self._lock:
            if self._stopped:
                raise RuntimeError("Bộ truyền file đã dừng")
            if job.transfer_id in self._jobs:
                raise ProtocolError("transfer_id đang được sử dụng")
            if len(self._jobs) >= self.max_transfers:
                raise RuntimeError("Đã đạt giới hạn lượt truyền đồng thời")
            self._jobs[job.transfer_id] = job
            job.thread = threading.Thread(target=self._run, args=(job, operation),
                                          name=f"file-{job.direction}-{job.transfer_id[:8]}")
            try:
                job.thread.start()
            except Exception:
                self._jobs.pop(job.transfer_id)
                raise
        return job.transfer_id

    def _run(self, job, operation):
        try:
            operation(job)
        except Exception as exc:
            cancelled = job.cancel.is_set() and not job.committed
            self._emit(job, "transfer_cancelled" if cancelled else "transfer_failed",
                       error=str(exc), saved=job.committed)
        finally:
            self._close(job.sock)
            with self._lock:
                self._jobs.pop(job.transfer_id, None)

    def send_file(self, peer, file_path):
        """Return a transfer UUID immediately; preparation and I/O run in a worker."""
        target = dict(peer)
        target_id = peer_id(target["ip"], target["tcp_port"])
        if target.get("peer_id") != target_id:
            raise ValueError("Peer đích không khớp IP:TCP port")
        job = _Job(str(uuid.uuid4()), target_id, "send")
        return self._start(job, lambda current: self._send(current, target, Path(file_path)))

    def _send(self, job, peer, path):
        self._check(job)
        job.filename = safe_filename(path.name)
        with path.open("rb") as source:
            initial = os.fstat(source.fileno())
            if not path.is_file() or not 0 <= initial.st_size <= self.max_file_bytes:
                raise ValueError("Nguồn phải là file thường, tối đa kích thước cho phép")
            job.total_bytes = initial.st_size
            self._emit(job, "transfer_started", stage="preparing")
            digest = hashlib.sha256()
            hashed = 0
            while True:
                self._check(job)
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                hashed += len(chunk)
                if hashed > job.total_bytes:
                    raise OSError("File nguồn thay đổi khi chuẩn bị")
            if hashed != job.total_bytes:
                raise OSError("File nguồn thay đổi khi chuẩn bị")
            source.seek(0)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._attach(job, sock)
            sock.settimeout(self.timeout)
            self._check(job)
            sock.connect((peer["ip"], peer["tcp_port"]))
            send_frame(sock, {"type": "OPEN", "purpose": "file",
                              "peer_id": self.local_peer["peer_id"],
                              "name": self.local_peer["name"],
                              "tcp_port": self.local_peer["tcp_port"],
                              "transfer_id": job.transfer_id})
            send_header(sock, {"type": "FILE", "transfer_id": job.transfer_id,
                               "filename": job.filename, "payload_length": job.total_bytes,
                               "sha256": digest.hexdigest()})
            self._progress(job, force=True, stage="transferring")
            while job.bytes_done < job.total_bytes:
                self._check(job)
                chunk = source.read(min(CHUNK_SIZE, job.total_bytes - job.bytes_done))
                if not chunk:
                    raise OSError("File nguồn bị rút ngắn khi đang gửi")
                sock.sendall(chunk)
                job.bytes_done += len(chunk)
                self._progress(job, stage="transferring")
            final_stat = os.fstat(source.fileno())
            if source.read(1) or (final_stat.st_size, final_stat.st_mtime_ns) != (
                    initial.st_size, initial.st_mtime_ns):
                raise OSError("File nguồn thay đổi khi đang gửi; chưa xác nhận thành công")
            # FIN marks end of this dedicated body. Receiver rejects excess bytes.
            sock.shutdown(socket.SHUT_WR)
            self._progress(job, force=True, stage="awaiting_confirmation")
            reply, _ = recv_frame(sock)
            if reply.get("transfer_id") != job.transfer_id:
                raise ProtocolError("Xác nhận sai transfer_id")
            if reply["type"] == "ERROR":
                raise OSError(str(reply.get("message", "Bên nhận báo lỗi")))
            if (reply["type"] != "FILE_OK" or reply.get("sha256") != digest.hexdigest()
                    or type(reply.get("size")) is not int or reply["size"] != job.total_bytes):
                raise ProtocolError("Xác nhận FILE_OK không hợp lệ")
            self._emit(job, "transfer_completed", percent=100.0,
                       sha256=digest.hexdigest(), saved_as=reply.get("saved_as", job.filename))

    def receive_file(self, sock, opening, remote_ip):
        """Own and close sock, even if opening validation or worker start fails.

        The listener must use read_header/recv_frame to consume OPEN exactly,
        without buffered over-read; no other thread may read this socket again.
        """
        try:
            if opening.get("type") != "OPEN" or opening.get("purpose") != "file":
                raise ProtocolError("Cần OPEN purpose=file")
            if opening.get("payload_length") != 0 or opening.get("version") != 1:
                raise ProtocolError("OPEN không hợp lệ")
            sender_id = peer_id(remote_ip, opening.get("tcp_port"))
            if opening.get("peer_id") != sender_id:
                raise ProtocolError("Định danh nguồn không khớp địa chỉ kết nối")
            job = _Job(_transfer_id(opening.get("transfer_id")), sender_id, "receive", sock=sock)
            sock.settimeout(self.timeout)
            return self._start(job, self._receive)
        except Exception:
            self._close(sock)
            raise

    def _receive(self, job):
        temporary = None
        try:
            self._check(job)
            header = read_header(job.sock)
            if header["type"] != "FILE" or header.get("transfer_id") != job.transfer_id:
                raise ProtocolError("Thiếu FILE hoặc sai transfer_id")
            job.filename = safe_filename(header.get("filename"))
            job.total_bytes = header["payload_length"]
            if job.total_bytes > self.max_file_bytes:
                raise ProtocolError("File vượt giới hạn cấu hình máy nhận")
            checksum = header.get("sha256")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ProtocolError("Thiếu SHA-256 hợp lệ")
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".lan-", suffix=".part", dir=self.downloads_dir)
            temporary = Path(temp_name)
            digest = hashlib.sha256()
            self._emit(job, "transfer_started", stage="transferring")
            with os.fdopen(fd, "wb") as output:
                while job.bytes_done < job.total_bytes:
                    self._check(job)
                    chunk = job.sock.recv(min(CHUNK_SIZE, job.total_bytes - job.bytes_done))
                    if not chunk:
                        raise ConnectionError(f"Ngắt truyền ở {job.bytes_done}/{job.total_bytes} byte")
                    if output.write(chunk) != len(chunk):
                        raise OSError("Không ghi đủ dữ liệu xuống đĩa")
                    digest.update(chunk)
                    job.bytes_done += len(chunk)
                    self._progress(job, stage="transferring")
                # Sender half-closes after the body; extra bytes are an error.
                if job.sock.recv(1):
                    raise ProtocolError("Dữ liệu vượt kích thước đã khai báo")
                if digest.hexdigest() != checksum:
                    raise ProtocolError("SHA-256 không khớp: file hỏng hoặc nguồn đã thay đổi")
                self._check(job)
                output.flush()
                os.fsync(output.fileno())
            # A successful cancellation must win before the final publication.
            # Once publication wins, cancellation cannot retract a saved file.
            with self._lock:
                self._check(job)
                saved = self._publish(temporary, job.filename)
                job.committed = True
            temporary.unlink()
            temporary = None
            try:
                send_frame(job.sock, {"type": "FILE_OK", "transfer_id": job.transfer_id,
                                     "size": job.total_bytes, "sha256": checksum,
                                     "saved_as": saved.name})
            except OSError as exc:
                self._emit(job, "transfer_completed", percent=100.0, path=str(saved),
                           sha256=checksum, warning=f"File đã lưu; không gửi được xác nhận: {exc}")
            else:
                self._emit(job, "transfer_completed", percent=100.0,
                           path=str(saved), sha256=checksum)
        except Exception as exc:
            cleanup_error = None
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup:
                    cleanup_error = cleanup
            try:
                send_frame(job.sock, {"type": "ERROR", "transfer_id": job.transfer_id,
                                     "message": str(exc)[:500]})
            except (OSError, ProtocolError):
                pass
            if cleanup_error:
                raise OSError(f"{exc}; không xóa được file nhận dở {temporary}: {cleanup_error}") from exc
            raise

    def _publish(self, temporary, name):
        # A hard link atomically publishes a complete file without overwriting
        # an existing path, even when two receivers finish simultaneously.
        original = Path(name)
        for number in range(10000):
            candidate = self.downloads_dir / (name if number == 0 else
                                              f"{original.stem} ({number}){original.suffix}")
            try:
                os.link(temporary, candidate)
                return candidate
            except FileExistsError:
                continue
        raise OSError("Không tìm được tên file đích còn trống")

    def cancel_transfer(self, transfer_id):
        with self._lock:
            job = self._jobs.get(transfer_id)
            if job is None or job.committed:
                return False
            job.cancel.set()
            sock = job.sock
        self._close(sock)
        return True

    def stop_transfers(self, timeout=5.0):
        """Call outside Tk's thread. Cancel, unblock I/O and join workers."""
        with self._lock:
            self._stopped = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if not job.committed:
                    job.cancel.set()
        for job in jobs:
            self._close(job.sock)
        deadline = time.monotonic() + timeout
        for job in jobs:
            if job.thread is not threading.current_thread():
                job.thread.join(max(0, deadline - time.monotonic()))
        alive = [job.transfer_id for job in jobs if job.thread.is_alive()]
        if alive:
            raise TimeoutError(f"Chưa dừng được lượt truyền: {', '.join(alive)}")

    @property
    def active_transfer_ids(self):
        with self._lock:
            return tuple(self._jobs)
