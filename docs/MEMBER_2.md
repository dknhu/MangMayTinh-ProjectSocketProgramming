# MEMBER 2 — TCP Chat & Protocol

**File chính:** `src/chat.py`, `src/protocol.py` (file protocol là **dùng chung**)  
**Mục tiêu:** Hai máy chat hai chiều, chuyển hội thoại được và có unread.

## Việc cần làm (theo thứ tự)

- [ ] Thử TCP echo client/server: `socket` → `bind/listen/accept` → `connect` → gửi/nhận.
- [ ] Cùng nhóm **chốt framing** và cách phân biệt kết nối chat/file trong [PROTOCOL](PROTOCOL.md).
- [ ] TCP listener chấp nhận kết nối từ các peer, không khóa GUI.
- [ ] Kết nối đến peer bằng **IP + TCP port từ Discovery**.
- [ ] Gửi và nhận chat hai chiều; đọc đủ bytes cho mỗi tin nhắn, kể cả message dài hoặc nhiều message liên tiếp.
- [ ] Hỗ trợ nhiều peer; lưu lịch sử trong phiên chạy ứng dụng.
- [ ] Đếm tin chưa đọc từ peer không đang được chọn; xóa unread khi mở chat đó.
- [ ] Gửi sự kiện tin đến/lỗi để GUI hiển thị ngay, không mất nội dung đang gõ.
- [ ] Xử lý `/leave` hoặc nút Rời hội thoại, peer offline, mất kết nối và đóng app mà không crash.
- [ ] Phối hợp với Member 3 để TCP listener chuyển kết nối file cho `file_transfer.py` theo quy ước thống nhất.

## Phải thống nhất với ai?

- **Member 1:** Cấu trúc peer, TCP port và thứ tự khởi động listener.
- **Member 3:** Định dạng `MSG`/`FILE`, sự kiện chat/unread và cách chuyển kết nối file.
- **Cả nhóm:** Chỉ một thành phần đọc từng TCP socket; không tự sửa protocol chung.

## Hoàn thành khi

- [ ] Hai máy gửi/nhận tin hai chiều qua GUI.
- [ ] Peer thứ ba gửi tin khi đang chat người khác → có unread và xem lại được.
- [ ] Nhận tin không làm mất chữ đang gõ.
- [ ] TCP connection lỗi/đóng không làm sập ứng dụng.
- [ ] Có ảnh Wireshark TCP **SYN → SYN-ACK → ACK** kèm giải thích.
