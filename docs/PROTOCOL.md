# Quy ước giao tiếp của nhóm

> **RẤT QUAN TRỌNG:** Các dòng `HELLO`, `MSG`, `FILE`, `BYE` dưới đây là **ví dụ trong đề bài**. Chưa phải protocol framing có thể code ngay. Cả 3 người phải điền mục **Cần chốt** bên dưới trước khi triển khai.

## 1. UDP — Peer Discovery và Status

**Ví dụ trong assignment:**

```text
HELLO|<name>|<tcp_port>|<status>
HELLO|Quan|5000|online
```

- Broadcast trên cùng UDP port, **đề xuất `9999`**.
- Broadcast mỗi **3 giây**.
- Bên nhận lấy **IP của người gửi từ gói UDP**, không lấy từ chuỗi `HELLO`.
- `tcp_port` là **cổng TCP của người gửi** để máy khác chat đến.
- `status` chỉ gồm `online` / `away`.
- Sau **10 giây** không nhận presence thì xóa peer khỏi online list.

Ví dụ lưu peer:

```text
name=Quan, ip=192.168.1.10, tcp_port=5000, status=online, last_seen=...
```

## 2. TCP — Chat và file

**Ví dụ trong assignment:**

```text
MSG|<length>|<utf-8 text>
FILE|<filename>|<size_in_bytes>|<raw file bytes>
BYE|
```

| Loại | Ý nghĩa |
|---|---|
| `MSG` | Tin nhắn chat |
| `FILE` | Thông tin file và dữ liệu file |
| `BYE` | Thông báo đóng/rời phiên |

**Lưu ý kỹ thuật tối thiểu:** TCP là luồng byte. Một lần `recv()` **không đảm bảo nhận đúng một message**. Cần quy tắc header và độ dài thật rõ, rồi đọc cho đủ số byte. File chứa byte nhị phân bất kỳ, nên **không được** dùng cách `split('|')` cho cả file. Với UTF-8, số ký tự có thể khác số byte.

## 3. Cần chốt trước khi code (điền vào bảng này)

| Vấn đề | Quyết định của nhóm |
|---|---|
| UDP discovery port | `9999` (đề xuất, xác nhận: **[ ]**) |
| Cách cấu hình TCP port mỗi máy | **CHƯA CHỐT** |
| Định danh peer (kể cả trùng tên) | **CHƯA CHỐT** |
| Cách xác định giới hạn header / frame chat | **CHƯA CHỐT** |
| Nội dung `MSG` được mã hóa & tính `length` thế nào | Đề xuất UTF-8, **CHƯA CHỐT framing** |
| Kết nối chat và gửi file | Đề xuất **TCP connection riêng cho file**, **CHƯA CHỐT** |
| TCP listener nhận biết kết nối chat/file | **CHƯA CHỐT** |
| Header file, độ dài byte và cách đọc chính xác | **CHƯA CHỐT** |
| Trùng tên file trong `downloads/` | **CHƯA CHỐT** |
| Báo nhận file thành công/lỗi | **CHƯA CHỐT** |
| Ai đọc/đóng từng socket, xử lý lỗi | **CHƯA CHỐT** |

**Cách chốt nhanh:** Member 2 đề xuất định dạng cụ thể (có 1 ví dụ chat, 1 ví dụ file), Member 3 kiểm tra phần file, Member 1 kiểm tra discovery và nối các module. Cả nhóm đồng ý rồi mới sửa `src/protocol.py`.

## 4. Giao diện giữa các module (tên hàm gợi ý, chưa implement)

- Discovery: `start_discovery`, `stop_discovery`, `set_status`, `get_peers`.
- Chat: `start_listener`, `connect_peer`, `send_message`, `get_history`, `mark_read`, `leave_chat`.
- File: `send_file` và callback/sự kiện tiến độ, thành công/lỗi.
- GUI: nhận sự kiện qua queue, gọi các hàm trên khi người dùng thao tác.

**Trước khi mỗi người code:** Chốt đối số, dữ liệu trả về, ai sở hữu trạng thái và callback/sự kiện lỗi; ghi ngay tại đây để tránh hai người đặt hàm khác nhau.
