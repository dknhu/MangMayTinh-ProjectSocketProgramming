# MEMBER 1 — UDP Discovery & Integration

**File chính:** `src/discovery.py`, `src/main.py`  
**Mục tiêu:** Mở hai app trên hai máy cùng Wi-Fi, máy nào cũng thấy máy kia và biết trạng thái.

## Việc cần làm (theo thứ tự)

- [ ] Thử UDP `sendto()` / `recvfrom()` giữa 2 chương trình.
- [ ] Tạo UDP listener ở discovery port chung (đề xuất `9999`).
- [ ] Broadcast presence `HELLO|name|tcp_port|status` mỗi **3 giây**.
- [ ] Nhận presence, lấy IP nguồn, cập nhật `name/IP/TCP port/status/last_seen`.
- [ ] Hiển thị/thông báo khi peer online hoặc offline; xóa peer nếu **10 giây** không nhận được.
- [ ] Cho user đổi `online` / `away` và phát status mới.
- [ ] Không hiển thị chính mình như một peer khác; không crash khi gói UDP sai.
- [ ] Dùng `main.py` để khởi động TCP listener, UDP và GUI; đóng app gọn gàng.
- [ ] Kết nối dữ liệu Discovery vào online list của GUI cùng Member 3.

## Phải thống nhất với ai?

- **Member 2:** Cổng TCP thực tế quảng bá và cấu trúc peer để `connect_peer()` dùng được.
- **Member 3:** Dữ liệu sự kiện thêm/xóa peer, đổi trạng thái để GUI hiển thị.
- **Cả nhóm:** Mục [Cần chốt trong PROTOCOL](PROTOCOL.md#3-cần-chốt-trước-khi-code-điền-vào-bảng-này).

## Hoàn thành khi

- [ ] Trên **2 máy cùng LAN**, hai app thấy nhau; trạng thái online/away cập nhật.
- [ ] Tắt một app: app còn lại xóa peer sau khoảng 10 giây.
- [ ] App không crash khi nhận gói sai hoặc peer tắt.
- [ ] Có ảnh chụp Wireshark UDP broadcast kèm giải thích cho report.
