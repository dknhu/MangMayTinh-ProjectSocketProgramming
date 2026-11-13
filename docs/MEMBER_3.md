# MEMBER 3 — File Transfer & GUI

**File chính:** `src/file_transfer.py`, `src/gui.py`  
**Mục tiêu:** Giao diện dễ dùng, gửi file được, hiện %, chat vẫn chạy khi gửi.

> **Cập nhật 09/11/2026:** Code member 3 và hợp đồng tích hợp đã có.
> Chạy `python -m src.member3_demo`; xem [bàn giao](MEMBER_3_HANDOFF.md)
> và [kết quả kiểm thử](MEMBER_3_VALIDATION.md). Checkbox dưới đây phân biệt
> kiểm thử component với nghiệm thu ứng dụng LAN đầy đủ.

## A. GUI — làm khung trước

- [x] Tạo cửa sổ Tkinter có: tên user, status Online/Away, danh sách peer, vùng chat, ô nhập, nút gửi, nút chọn file.
- [ ] Hiển thị `name`, IP, online/away và thông báo peer vào/ra từ Member 1.
- [ ] Chọn peer để chat; hiển thị lịch sử, unread và tin mới từ Member 2.
- [x] Khi tin đến, giữ nguyên nội dung user đang gõ (đã kiểm tra với event backend).
- [x] Có thao tác Rời hội thoại, Trợ giúp, Thoát.
- [x] Nhận sự kiện mạng qua `queue`; **không cập nhật Tkinter trực tiếp từ luồng nền**.

## B. File Transfer — đã triển khai theo Protocol v1

- [x] Cho phép chọn file và gửi cho người đang chat.
- [x] Đọc tên file, kích thước và gửi **bytes thật** qua TCP.
- [x] Nhận **đúng số bytes** đã khai báo rồi lưu trong `downloads/`.
- [x] Hiển thị % theo `bytes_sent / total_bytes × 100` (xử lý riêng file 0 byte).
- [x] Gửi file trong **thread nền**, GUI vẫn xử lý tin và lệnh gửi (chat TCP thật chờ member 2).
- [x] Xử lý file không tồn tại, mất kết nối, gửi dở; **xóa file nhận chưa hoàn tất**.
- [x] Kiểm tra file nhận có nội dung giống file nguồn (đếm byte và SHA-256).

## Phải thống nhất với ai?

- **Member 1:** Dữ liệu online list/status và cách `main.py` khởi động GUI.
- **Member 2:** Format `FILE`, cách TCP listener phân loại/đưa connection file cho bộ nhận, sự kiện chat/unread.
- **Cả nhóm:** Thống nhất [PROTOCOL](PROTOCOL.md) trước khi viết file transfer.

## Hoàn thành khi

- [ ] GUI hoạt động với Discovery và Chat thật, không chỉ hiển thị dữ liệu mẫu.
- [ ] Gửi file giữa **2 máy cùng LAN**, lưu vào `downloads/`, progress hiển thị.
- [ ] Trong khi truyền file vẫn gửi và nhận chat được, cửa sổ không đứng.
- [x] Ngắt truyền giữa chừng → báo lỗi, không giữ file nhận dở (đã test TCP local; LAN cần chạy lại).

**Lưu ý phân công:** Member 1 và 2 phải hỗ trợ kết nối module của họ vào GUI; Member 3 không chịu trách nhiệm tự sửa mọi phần mạng.
