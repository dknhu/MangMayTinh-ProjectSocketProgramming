# LAN Chat & File Sharing (TCP/UDP)

> **Trạng thái:** Bộ khung tài liệu và mã nguồn rỗng. Chưa có chức năng nào chạy được.

## 1. Project này làm gì?

Một ứng dụng chat **ngang hàng (P2P)** cho các máy cùng LAN/Wi-Fi. Mỗi người chạy cùng chương trình, tự phát hiện người dùng, chọn người để chat và gửi file, không cần chat server trung tâm.

| Chức năng bắt buộc | Cách hoạt động |
|---|---|
| Peer Discovery | UDP broadcast thông báo hiện diện mỗi **3 giây**, xóa peer sau **10 giây** không nhận được |
| Online Status | Trạng thái `online` / `away` gửi qua UDP |
| Chat | TCP hai chiều, nhiều peer, tin đến hiển thị ngay, lịch sử và thông báo chưa đọc |
| File Transfer | TCP, % tiến độ, lưu vào `downloads/`, chat vẫn dùng được khi truyền |
| GUI | Tkinter; danh sách peer, chọn chat, nhập tin, chọn file, trạng thái và thông báo |
| Độ ổn định | Không crash khi peer tắt, kết nối lỗi hoặc file gửi dở |

## 2. Chia việc

| Ai | Chính | File đảm nhận |
|---|---|---|
| Member 1 | UDP Discovery, Status, tích hợp ứng dụng | `src/discovery.py`, `src/main.py` |
| Member 2 | TCP Chat, framing và TCP listener | `src/chat.py`, `src/protocol.py` (dùng chung) |
| Member 3 | TCP File Transfer và Tkinter GUI | `src/file_transfer.py`, `src/gui.py` |

**Cả nhóm** hỗ trợ tích hợp, test hai máy, chụp Wireshark, viết báo cáo và demo. Member 1–2 phải giúp Member 3 kết nối các chức năng vào GUI, không để Member 3 tự xử lý toàn bộ tích hợp.

## 3. Cấu trúc thư mục

```text
LAN_Chat_Simplified/
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── PROTOCOL.md
│   ├── MEMBER_1.md
│   ├── MEMBER_2.md
│   └── MEMBER_3.md
├── src/
│   ├── __init__.py
│   ├── main.py
│   ├── discovery.py
│   ├── chat.py
│   ├── protocol.py
│   ├── file_transfer.py
│   └── gui.py
├── tests/
└── downloads/
```

## 4. Đọc tài liệu theo thứ tự nào?

1. **README** (file này): hiểu phạm vi và ai làm gì.
2. [ARCHITECTURE](docs/ARCHITECTURE.md): hiểu luồng UDP → TCP → GUI.
3. [PROTOCOL](docs/PROTOCOL.md): cả nhóm thống nhất định dạng truyền dữ liệu trước khi code.
4. [MEMBER_1](docs/MEMBER_1.md), [MEMBER_2](docs/MEMBER_2.md), [MEMBER_3](docs/MEMBER_3.md): ai đọc file của người đó.

## 5. Cách làm nhóm (Git)

- Tạo các branch `feature/udp`, `feature/chat`, `feature/file-gui` từ `main`.
- Chỉ sửa file mình sở hữu; thay đổi `protocol.py` cần cả nhóm thống nhất.
- Mỗi phần làm xong, tạo pull request; một người khác xem rồi mới merge.
- **Ghép thử sớm**: UDP+GUI trước; chat+GUI tiếp theo; file+chat cuối cùng.

## 6. Các mốc đơn giản (không cố định ngày)

- [ ] **Mốc 1:** Chốt [PROTOCOL](docs/PROTOCOL.md), thử UDP send/receive và TCP echo.
- [ ] **Mốc 2:** Hai máy nhìn thấy nhau; online/away, timeout hiện trên GUI.
- [ ] **Mốc 3:** Chat hai chiều qua GUI; nhiều peer, unread hoạt động.
- [ ] **Mốc 4:** Gửi file có progress, lưu đúng, vẫn chat được; xử lý lỗi.
- [ ] **Mốc 5:** Test trên ít nhất 2 máy thật, ảnh Wireshark, báo cáo và diễn tập demo.

## 7. Cách chạy (sau khi đã code)

Dự kiến dùng **Python 3 + thư viện chuẩn** (`socket`, `threading`, `tkinter`). Khi đã triển khai xong:

```bash
python -m src.main
```

> Hiện file `.py` đang rỗng, lệnh này **chưa chạy ra ứng dụng**. Cách truyền username/port sẽ được bổ sung khi nhóm chốt và làm xong phần khởi động.

## 8. Yêu cầu nộp theo đề

Source code + README, **report 5–10 trang**, ảnh Wireshark **UDP broadcast** và **TCP three-way handshake**, demo **10 phút trên ít nhất 2 máy tính**.
