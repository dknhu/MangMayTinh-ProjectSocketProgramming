# MEMBER 3 — File Transfer & GUI

**File chính:** `src/file_transfer.py`, `src/gui.py`  
**Mục tiêu:** Giao diện dễ dùng, gửi file được, hiện %, chat vẫn chạy khi gửi.

> **Cập nhật 09/11/2026:** Code member 3 và hợp đồng tích hợp đã có.
> Chạy `python -m src.member3_demo`; xem [bàn giao](MEMBER_3_HANDOFF.md)
> và [kết quả kiểm thử](MEMBER_3_VALIDATION.md). Checkbox dưới đây phân biệt
> kiểm thử component với nghiệm thu ứng dụng LAN đầy đủ.

> **Giao diện 13/11/2026:** Đã áp dụng tông hồng pastel, icon pixel và bố cục
> sidebar/chat/truyền tệp/ô nhập. Xem [thiết kế và ảnh preview](MEMBER_3_UI.md).
> API và event tích hợp giữ nguyên; toàn bộ 48 test local đạt.

> Thử hai cửa sổ Như ↔ Minh bằng `python -m src.member3_demo --pair`.
> Chat nội bộ mô phỏng, file TCP thật; hướng dẫn ở MEMBER_3_HANDOFF.

> **Kiểm thử tích hợp 15/11/2026 — #9:** Đã thêm
> `tests/test_gui_integration.py` với GUI và backend production qua UDP/TCP
> loopback. Toàn bộ **67 test đạt**; chat hai chiều, unread, giữ draft và
> truyền file 8 MiB đồng thời chat đã được kiểm tra. Hai máy LAN vẫn chờ #10.

## A. GUI — làm khung trước

- [x] Tạo cửa sổ Tkinter có: tên user, status Online/Away, danh sách peer, vùng chat, ô nhập, nút gửi, nút chọn file.
- [x] Hiển thị `name`, IP, online/away và thông báo peer vào/ra từ Member 1 (đã có test component và tích hợp production local; LAN chờ #10).
- [x] Chọn peer để chat; hiển thị lịch sử, unread và tin mới từ Member 2 (đã kiểm tra GUI với ChatService thật trên loopback).
- [x] Khi tin đến, giữ nguyên nội dung user đang gõ (đã kiểm tra với event backend).
- [x] Có thao tác Rời hội thoại, Trợ giúp, Thoát.
- [x] Nhận sự kiện mạng qua `queue`; **không cập nhật Tkinter trực tiếp từ luồng nền**.

## B. File Transfer — đã triển khai theo Protocol v1

- [x] Cho phép chọn file và gửi cho người đang chat.
- [x] Đọc tên file, kích thước và gửi **bytes thật** qua TCP.
- [x] Nhận **đúng số bytes** đã khai báo rồi lưu trong `downloads/`.
- [x] Hiển thị % theo `bytes_sent / total_bytes × 100` (xử lý riêng file 0 byte).
- [x] Gửi file trong **thread nền**, GUI vẫn xử lý tin và lệnh gửi (đã kiểm tra chat TCP thật khi file đang truyền trên loopback).
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
