# Member 3 — Kết quả kiểm tra ngày 09/11/2026

Môi trường: Windows, Python **3.14.6**, Tk **8.6.15**. Socket TCP loopback thật và
Tkinter thật; cửa sổ kiểm thử được ẩn. Test dùng thư mục tạm và dọn worker/socket
sau mỗi lượt, không ghi file thử vào downloads/ của người dùng.

## Lệnh và kết quả

```text
python -X utf8 -m unittest discover -s tests -v
Ran 42 tests in 5.430s
OK
```

| Nhóm | Số test | Nội dung |
|---|---:|---|
| test_protocol.py | 8 | TCP phân mảnh/gộp frame, Unicode, EOF, header quá lớn/sai JSON/khóa trùng, không đọc dư FILE body, endpoint ID |
| test_file_transfer.py | 21 | gửi/nhận hai chiều, binary/Unicode, file rỗng, hash, trùng tên/đồng thời, lỗi đường dẫn/size/đĩa, ngắt/hủy/timeout/dừng, nguồn thay đổi, mất FILE_OK |
| test_gui.py | 13 | draft/cursor/focus, chuyển chat, unread/revision, lệnh chạy ngoài Tk, lỗi gửi/hủy chọn file, offline, thread ownership, progress, rời chat, thoát không chặn và TCP file thật |

Kiểm tra bổ sung đã đạt:

```text
python -m compileall -q src tests
python -X utf8 -m src.member3_demo --help
git diff --check
```

Đã chạy thêm smoke test khởi tạo đầy đủ DemoBackend + Tk GUI, hai listener TCP
thật, gửi file 128 KiB qua callback GUI, chat mô phỏng và shutdown; file khớp,
không còn background thread sau khi đóng. Kiểm tra này vẫn trên cùng một máy.

## Điều kiện có bằng chứng trực tiếp

- File binary chứa mọi byte 0–255, dấu |, null, xuống dòng và UTF-8 không hợp lệ
  truyền thành công, nội dung/hash khớp.
- File rỗng hoàn tất 100% sau xác nhận; không chia cho 0.
- File trùng tên không ghi đè; ba lượt cùng tên đồng thời tạo ba tên riêng.
- Thiếu byte, dư byte, hash sai, tên vượt thư mục, timeout và hủy không tạo file
  hoàn chỉnh giả; .part được xóa. Lỗi xóa .part được báo rõ.
- Nguồn bị thay đổi sau khi tính hash không báo thành công.
- FILE_OK bị mất: receiver giữ file thật và cảnh báo, sender báo lỗi/chưa xác nhận.
- Test tích hợp truyền **8 MiB TCP** với receiver làm chậm có kiểm soát; trong
  lúc truyền, Tk xử lý event tin đến và callback gửi, giữ draft; file cuối khớp.
- Shutdown có callback dừng đang chờ vẫn cho Tk xử lý timer; destroy sau khi dừng.

## Giới hạn của bằng chứng

- Chưa có code discovery/chat/main của member 1–2 để tích hợp end-to-end.
- Tin chat trong test được đưa qua event/callback; không phải chat TCP production.
- So sánh focus trong test chạy cửa sổ ẩn; cần kiểm tra lại focus/hiển thị trực quan
  trong cửa sổ mở trên máy demo. Trợ giúp và các nút cần một lượt rà thủ công.
- Chưa chạy trên hai máy vật lý, chưa kiểm tra firewall/Wi-Fi timeout thực tế,
  chưa có capture Wireshark hay diễn tập demo.
- Chưa kiểm tra Python/OS khác; lưu file hoàn chỉnh yêu cầu filesystem hỗ trợ hard link.

## Checklist nhóm trước khi nộp

- [ ] Member 1 nối event peer/status, vòng đời và TCP port/IP đúng.
- [ ] Member 2 dùng framing v1, OPEN handoff, history/unread/revision và chat worker.
- [ ] Hai máy vật lý gửi file hai chiều; kiểm tra SHA-256 nguồn/đích.
- [ ] Vừa truyền file lớn vừa chat TCP thật với Minh, nhận tin/unread từ Lan.
- [ ] Rà trực quan GUI: tên/IP/status, Enter, chọn file, progress, help, leave, quit.
- [ ] Ngắt Wi-Fi/tắt peer lúc đang nhận: lỗi rõ, .part bị xóa, chat khác vẫn chạy.
- [ ] Đóng cửa sổ lúc truyền, mở lại app và kiểm tra port đã được giải phóng.
- [ ] Chụp Wireshark/ảnh tiến độ, cập nhật phần báo cáo và diễn tập hai máy.

## Cập nhật giao diện ngày 13/11/2026

Đã triển khai giao diện pastel/pixel theo ảnh tham khảo. Thiết kế, lệnh chạy
và ảnh chụp cửa sổ thật nằm trong [MEMBER_3_UI](MEMBER_3_UI.md).

Toàn bộ suite đạt **43 test trong 5.242 giây**: 8 protocol, 21 file transfer,
14 GUI. Test mới kiểm tra lịch sử 500 tin tiếng Việt/nhiều dòng, định dạng
tin gửi/nhận và giữ draft. Test harness giải phóng wrapper Tk trên main thread;
icon cũng được giải phóng trên Tk thread khi GUI shutdown thành công.

Đã rà ảnh cửa sổ 1140 × 820 và 920 × 700; giữ nguyên UIActions và event schema.
Không thêm thư viện runtime. Ảnh preview dùng dữ liệu minh họa, không thay cho
nghiệm thu chat TCP/UDP và file giữa hai máy vật lý.

## Demo cặp Như ↔ Minh — 13/11/2026

`python -m src.member3_demo --pair` mở hai cửa sổ đã chọn nhau, có cùng đoạn chat
mẫu. Chat được relay nội bộ để kiểm GUI; listener file vẫn dùng TCP loopback thật,
cổng được OS chọn và thư mục nhận tách riêng từng bên.

Toàn bộ suite đạt **48 test trong 5.061 giây**: 8 protocol, 21 file transfer,
15 GUI, 4 paired-demo. Các test mới xác nhận tin xuất hiện ở cửa sổ đối phương,
reply hai chiều, không tự echo, unread/status/history, partner đóng và file TCP
hai chiều có nội dung/hash đúng. Test GUI bấm gửi tin ở hai cửa sổ và chọn file
qua callback, chờ cả hai phía Hoàn tất.

Đây là kiểm thử demo local; TCP ChatService, UDP discovery và hai máy LAN vẫn
cần Member 1–2 triển khai/tích hợp/nghiệm thu.
