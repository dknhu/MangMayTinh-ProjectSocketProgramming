# MEMBER 3 — File Transfer & GUI

**File chính:** `src/file_transfer.py`, `src/gui.py`  
**Mục tiêu:** Giao diện dễ dùng, gửi file được, hiện %, chat vẫn chạy khi gửi.

## A. GUI — làm khung trước

- [ ] Tạo cửa sổ Tkinter có: tên user, status Online/Away, danh sách peer, vùng chat, ô nhập, nút gửi, nút chọn file.
- [ ] Hiển thị `name`, IP, online/away và thông báo peer vào/ra từ Member 1.
- [ ] Chọn peer để chat; hiển thị lịch sử, unread và tin mới từ Member 2.
- [ ] Khi tin đến, giữ nguyên nội dung user đang gõ.
- [ ] Có thao tác Rời hội thoại, Trợ giúp, Thoát.
- [ ] Nhận sự kiện mạng qua `queue`; **không cập nhật Tkinter trực tiếp từ luồng nền**.

## B. File Transfer — làm sau khi chốt Protocol với Member 2

- [ ] Cho phép chọn file và gửi cho người đang chat.
- [ ] Đọc tên file, kích thước và gửi **bytes thật** qua TCP.
- [ ] Nhận **đúng số bytes** đã khai báo rồi lưu trong `downloads/`.
- [ ] Hiển thị % theo `bytes_sent / total_bytes × 100` (xử lý riêng file 0 byte).
- [ ] Gửi file trong **thread nền**, vẫn nhắn tin được khi đang gửi.
- [ ] Xử lý file không tồn tại, mất kết nối, gửi dở; **xóa file nhận chưa hoàn tất**.
- [ ] Kiểm tra file nhận có nội dung giống file nguồn.

## Phải thống nhất với ai?

- **Member 1:** Dữ liệu online list/status và cách `main.py` khởi động GUI.
- **Member 2:** Format `FILE`, cách TCP listener phân loại/đưa connection file cho bộ nhận, sự kiện chat/unread.
- **Cả nhóm:** Thống nhất [PROTOCOL](PROTOCOL.md) trước khi viết file transfer.

## Hoàn thành khi

- [ ] GUI hoạt động với Discovery và Chat thật, không chỉ hiển thị dữ liệu mẫu.
- [ ] Gửi file giữa **2 máy cùng LAN**, lưu vào `downloads/`, progress hiển thị.
- [ ] Trong khi truyền file vẫn gửi và nhận chat được, cửa sổ không đứng.
- [ ] Ngắt truyền giữa chừng → báo lỗi, không giữ file nhận dở.

**Lưu ý phân công:** Member 1 và 2 phải hỗ trợ kết nối module của họ vào GUI; Member 3 không chịu trách nhiệm tự sửa mọi phần mạng.
