# Quy ước giao tiếp v1

Chốt ngày **09/11/2026** theo yêu cầu của người phụ trách member 3. Đây là các
quyết định triển khai, không phải khẳng định member 1–2 đã review hoặc đã làm xong.
`src/protocol.py` và phần FILE đã có code. UDP/chat là hợp đồng để member 1–2 nối vào.

## 1. Peer và UDP (member 1)

Peer là bản chụp dictionary, không chia sẻ dictionary đang bị worker sửa:

```python
{"peer_id": "192.168.1.10:5000", "name": "Quan", "ip": "192.168.1.10",
 "tcp_port": 5000, "status": "online", "last_seen": 12345.0}
```

- Chỉ IPv4; `peer_id = IP:TCP_port`, không dùng tên hay TCP source port tạm.
- Discovery sở hữu peer list; `last_seen` là `time.monotonic()` tại máy nhận.
- UDP port **9999**, presence UTF-8 `HELLO|name|tcp_port|status`.
- Tên 1–64 byte UTF-8, không chứa `|`, ký tự điều khiển hoặc xuống dòng.
- Datagram tối đa 512 byte; port số nguyên 1–65535; status `online` hoặc `away`.
- Broadcast mỗi **3 giây**, gửi ngay lúc khởi động/đổi status; xóa sau **10 giây** im lặng.
- IP lấy từ nguồn `recvfrom`, TCP port lấy từ presence, không lấy UDP source port.
- TCP mặc định **5000**, cấu hình được; nhiều instance cùng máy dùng port khác.
- TCP listener chạy trước khi quảng bá. Hai máy khác nhau có thể cùng port 5000.
- Bỏ qua endpoint của mình; tên trùng được phép. Away vẫn đang hiện diện.
- Timeout UDP không tự đóng mọi TCP/file còn tốt. Restart cùng endpoint giữ
  cùng khóa lịch sử trong phiên chạy của máy bên kia.

## 2. Framing TCP (đã triển khai)

```text
[4 byte unsigned big-endian: H][H byte JSON UTF-8][payload_length byte payload]
```

`H` đếm byte JSON đã mã hóa UTF-8, không phải số ký tự; 1–16384 byte.
Mỗi header có `version:1`, `type`, `payload_length` (số nguyên, không nhận bool).
Từ chối khóa trùng, UTF-8/JSON/type/version/length sai. MSG tối đa 65536 byte.
FILE tối đa **1 GiB**, máy nhận có thể cấu hình thấp hơn. Frame điều khiển
OPEN/FILE_OK/ERROR/BYE có payload_length=0.

`read_header()`/`recv_exact()` luôn đọc đúng ranh giới, không đọc dư. Không bọc
socket bằng `makefile()` hoặc bộ đọc buffer rồi chỉ bàn giao socket.
Không giả định một recv là một message. `Hello Minh` dài 10 byte; `Chào` dài 5 byte:

```python
send_frame(sock, {"type": "MSG"}, "Chào".encode("utf-8"))
```

## 3. OPEN và sở hữu socket

Một TCP listening port chung cho chat/file. Mỗi lượt FILE mở **một kết nối mới**.
Frame đầu luôn là OPEN:

```json
{"version":1,"type":"OPEN","payload_length":0,"purpose":"file","peer_id":"192.168.1.10:5000","name":"Quan","tcp_port":5000,"transfer_id":"8f9c4303-7f9e-4ed5-84ea-09b5d354fb59"}
```

Chat dùng purpose="chat", không cần transfer_id. FILE dùng UUID chuẩn, duy nhất
trong lượt đang chạy. Receiver kiểm tra peer_id khớp IP nguồn kết nối và tcp_port
trong OPEN. Endpoint identity không phải xác thực người dùng.

Member 2 accept nhanh rồi đọc OPEN trong worker, timeout 3 giây, giới hạn worker.
Dùng recv_frame lấy OPEN/payload rỗng và phân loại purpose:

- chat: chat.py độc quyền đọc/đóng socket, tuần tự hóa ghi frame bằng lock.
- file: gọi `files.receive_file(conn, opening, source_ip)`. Sau lời gọi, manager
  sở hữu/đóng socket, **kể cả khi từ chối/báo lỗi**. Chat không recv/close lần nữa.
- OPEN sai: listener báo lỗi cục bộ và đóng riêng connection.

Không đặt `with conn:` quanh lời gọi bàn giao bất đồng bộ. Không giữ socket FILE
trong danh sách socket chat để rời chat vô tình đóng lượt truyền.

## 4. FILE và hoàn tất

Sau OPEN, gửi header FILE rồi stream dữ liệu nhị phân thô:

```json
{"version":1,"type":"FILE","payload_length":3,"transfer_id":"8f9c4303-7f9e-4ed5-84ea-09b5d354fb59","filename":"hello.txt","sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}
```

Payload ví dụ là 3 byte `abc`, không JSON/base64 hay split theo dấu |.
Sender tính hash trong worker trước khi connect rồi đọc lại để stream, kiểm tra
thay đổi size/mtime nguồn; receiver tự tính SHA-256 khi ghi.

1. Receiver kiểm tra header/tên/size/hash, tạo `.lan-*.part` trong downloads/.
2. Nhận/ghi khối tối đa 64 KiB, giới hạn recv theo số byte còn lại.
3. Sender gửi body rồi `shutdown(SHUT_WR)` (half-close), vẫn đọc được xác nhận.
4. Receiver đợi EOF phía ghi; byte dư, thiếu byte, timeout, hash sai đều là lỗi.
5. Receiver flush/fsync/đóng file, xuất bản tên cuối, trả FILE_OK:

```json
{"version":1,"type":"FILE_OK","payload_length":0,"transfer_id":"8f9c4303-7f9e-4ed5-84ea-09b5d354fb59","size":3,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","saved_as":"hello.txt"}
```

Sender chỉ hoàn tất khi FILE_OK khớp transfer_id/size/hash. 100% byte gửi là
**chờ xác nhận**. ACK mất sau khi file đã lưu: receiver giữ file/báo cảnh báo;
sender báo lỗi/chưa xác nhận, không tự gửi lại.

Receiver báo lỗi bằng ERROR rồi đóng connection:

```json
{"version":1,"type":"ERROR","payload_length":0,"transfer_id":"8f9c4303-7f9e-4ed5-84ea-09b5d354fb59","message":"SHA-256 không khớp"}
```

Timeout mặc định 15 giây cho mỗi thao tác socket; tối đa 8 lượt đồng thời (cấu hình
được). File 0 byte vẫn OPEN/FILE, half-close, kiểm tra hash rỗng và FILE_OK;
progress 0% tới khi hoàn tất thì 100%, không chia 0. Progress tối đa khoảng
10 event/giây/lượt cộng các mốc bắt đầu/kết thúc.

## 5. Lưu file, trùng tên, hủy, dừng

- Mặc định `downloads/` trong repo, không phụ thuộc cwd.
- Tên tối đa 240 byte UTF-8; từ chối tên rỗng/đường dẫn, .., ký tự điều khiển,
  tên thiết bị Windows, tên kết thúc bằng dấu chấm/khoảng trắng.
- Trùng tên thành `report (1).pdf`…; không ghi đè file cũ.
- Xuất bản file hoàn chỉnh bằng hard link cùng thư mục (NTFS/ext4 hỗ trợ), sau
  đó xóa `.part`; không ghi đè kể cả hai lượt kết thúc đồng thời.
- Lỗi trước khi lưu hoàn chỉnh: đóng/xóa `.part`; báo thêm lỗi nếu xóa thất bại.
- Hủy đặt tín hiệu dừng/đóng socket; không xóa file đã xuất bản hoàn chỉnh.
- Rời chat không hủy file. Stop ngừng việc mới/hủy/đóng socket/chờ worker tối đa
  5 giây; timeout báo rõ. Gọi stop ngoài luồng Tkinter.

## 6. Chat và lịch sử (hợp đồng member 2)

- MSG payload UTF-8, nhận đủ byte rồi decode. BYE chỉ rời một phiên chat.
- Chat sở hữu history/unread trong bộ nhớ; GUI không đếm bản riêng.
- `select_peer(id)`/`select_peer(None)` đổi active peer. Cùng lock với xử lý tin
  đến, đặt active và reset unread của peer được chọn.
- Mọi thay đổi history/unread phát snapshot `conversation`, revision tăng theo
  từng peer (gửi/nhận/đánh dấu đọc dùng cùng bộ đếm). Không phát snapshot cũ với revision mới.
- Tin của active peer unread=0, peer khác tăng unread; lịch sử theo phiên chạy.
- Lỗi gửi cập nhật message state="failed", phát operation_failed.
- Kết nối/send/recv chạy nền; callback UI nhận lệnh và trả nhanh.

## 7. GUI và sự kiện

Main tạo **queue.Queue() không giới hạn**, GUI là consumer duy nhất. Worker chỉ
put dictionary mới/bản chụp; main thread Tkinter poll bằng after(40ms), tối đa
200 event/lần để không giữ vòng lặp UI quá lâu. Progress không đẩy từng byte.
Callback/schema/ví dụ listener xem [MEMBER_3_HANDOFF.md](MEMBER_3_HANDOFF.md).
