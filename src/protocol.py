"""Shared TCP framing. Never read beyond the current header/payload boundary."""

import json
import socket
import struct

VERSION = 1
MAX_HEADER_BYTES = 16 * 1024
MAX_CONTROL_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MESSAGE_TYPES = {"OPEN", "MSG", "FILE", "FILE_OK", "ERROR", "BYE"}


class ProtocolError(ValueError):
    """Malformed, unsupported or oversized frame."""


class ConnectionClosed(ConnectionError):
    """EOF before all required bytes have arrived."""


def peer_id(ip, tcp_port):
    """LAN IPv4 endpoint identity; display names are never identity keys."""
    socket.inet_pton(socket.AF_INET, ip)
    if type(tcp_port) is not int or not 1 <= tcp_port <= 65535:
        raise ValueError("TCP port phải từ 1 đến 65535")
    return f"{ip}:{tcp_port}"


def recv_exact(sock, count):
    if type(count) is not int or count < 0:
        raise ProtocolError("Số byte không hợp lệ")
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(min(count - len(data), CHUNK_SIZE))
        if not chunk:
            raise ConnectionClosed(f"Kết nối đóng: nhận {len(data)}/{count} byte")
        data.extend(chunk)
    return bytes(data)


def validate_header(header):
    if not isinstance(header, dict):
        raise ProtocolError("Header phải là JSON object")
    if type(header.get("version")) is not int or header["version"] != VERSION:
        raise ProtocolError("Phiên bản protocol không được hỗ trợ")
    if not isinstance(header.get("type"), str) or header["type"] not in MESSAGE_TYPES:
        raise ProtocolError("Loại frame không hợp lệ")
    size = header.get("payload_length")
    limit = MAX_FILE_BYTES if header["type"] == "FILE" else MAX_CONTROL_BYTES
    if type(size) is not int or not 0 <= size <= limit:
        raise ProtocolError("payload_length vượt giới hạn hoặc không hợp lệ")
    if header["type"] not in {"MSG", "FILE"} and size != 0:
        raise ProtocolError("Frame điều khiển phải có payload_length=0")
    return header


def send_header(sock, header):
    header = {"version": VERSION, "payload_length": 0, **header}
    validate_header(header)
    try:
        raw = json.dumps(header, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError("Không mã hóa được header") from exc
    if not 0 < len(raw) <= MAX_HEADER_BYTES:
        raise ProtocolError("Header quá lớn")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Header chứa khóa JSON trùng")
        result[key] = value
    return result


def read_header(sock):
    length = struct.unpack("!I", recv_exact(sock, 4))[0]
    if not 0 < length <= MAX_HEADER_BYTES:
        raise ProtocolError("Độ dài header không hợp lệ")
    try:
        header = json.loads(recv_exact(sock, length).decode("utf-8"),
                            object_pairs_hook=_unique_keys,
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                ProtocolError("JSON constant không hợp lệ")))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("Header JSON/UTF-8 không hợp lệ") from exc
    return validate_header(header)


def send_frame(sock, header, payload=b""):
    """For small messages; FILE data must be streamed by file_transfer.py."""
    if len(payload) > MAX_CONTROL_BYTES:
        raise ProtocolError("Dùng streaming cho nội dung lớn")
    send_header(sock, {**header, "payload_length": len(payload)})
    if payload:
        sock.sendall(payload)


def recv_frame(sock):
    header = read_header(sock)
    if header["type"] == "FILE":
        raise ProtocolError("Bàn giao FILE cho bộ nhận streaming, không recv_frame")
    return header, recv_exact(sock, header["payload_length"])
