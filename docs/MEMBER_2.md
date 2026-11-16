# MEMBER 2 — TCP Chat & Protocol

**File chính:** `src/chat.py`, `src/protocol.py` (file protocol là **dùng chung**)  
**Mục tiêu:** Hai máy chat hai chiều, chuyển hội thoại được và có unread.

> Trước khi code: lấy bản member 3 đã merge vào main, đọc
> [TEAM_WORKFLOW](TEAM_WORKFLOW.md) và triển khai đúng API `ChatService`.
> Framing v1 đã có trong `protocol.py`: import và dùng lại. Bạn sửa `chat.py`
> và test chat; member 1 sửa main.py, member 3 sửa GUI/file khi cần.

## Việc cần làm (theo thứ tự)

- [ ] Thử TCP echo client/server: `socket` → `bind/listen/accept` → `connect` → gửi/nhận.
- [x] Đọc và dùng framing v1, OPEN phân loại chat/file trong [PROTOCOL](PROTOCOL.md). Mọi thay đổi phần dùng chung cần PR riêng và review ảnh hưởng file transfer.
- [x] TCP listener chấp nhận kết nối từ các peer, không khóa GUI.
- [x] Kết nối đến peer bằng **IP + TCP port từ Discovery**.
- [x] Gửi và nhận chat hai chiều; đọc đủ bytes cho mỗi tin nhắn, kể cả message dài hoặc nhiều message liên tiếp.
- [x] Hỗ trợ nhiều peer; lưu lịch sử trong phiên chạy ứng dụng.
- [x] Đếm tin chưa đọc từ peer không đang được chọn; xóa unread khi mở chat đó.
- [x] Gửi sự kiện tin đến/lỗi để GUI hiển thị ngay, không mất nội dung đang gõ.
- [x] Xử lý `/leave` hoặc nút Rời hội thoại, peer offline, mất kết nối và đóng app mà không crash.
- [x] Phối hợp với Member 3 để TCP listener chuyển kết nối file cho `file_transfer.py` theo quy ước thống nhất.

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

## Kiểm thử và bàn giao

Chạy từ thư mục gốc của repo.

```powershell
python -X utf8 -m unittest discover -s tests -p test_chat.py -v
python -X utf8 -m unittest discover -s tests -v
```

Test chat dùng TCP loopback và bảng peer mô phỏng, bao gồm tin Unicode dài,
nhiều tin liên tiếp, tin đến từ peer thứ ba, gửi tới peer offline, OPEN sai,
BYE, shutdown gọi lại và truyền file thật qua listener chung.

Member 1 khởi tạo ChatService với resolve_peer và files.receive_file,
sau đó chạy TCP listener trước UDP discovery theo TEAM_WORKFLOW. Khi hai peer
cùng mở chat, service chọn cùng một kết nối theo quy tắc trong PROTOCOL.
Mỗi socket có một luồng đọc và ghi frame tuần tự. Rời chat đóng các kết nối
chat của peer đó, kể cả socket đang đọc nốt dữ liệu trước khi đóng.

Khi ghép GUI, kiểm tra draft, unread và chat đồng thời với gửi file trên LAN.
Để lấy ảnh báo cáo, bắt gói trên card Wi-Fi/Ethernet trước khi mở một kết nối
chat mới. Dùng bộ lọc Wireshark tcp.port == 5000 hoặc thay bằng cổng TCP đã cấu
hình, rồi chọn cùng một TCP stream để chụp ba gói SYN, SYN-ACK và ACK.

## Bổ sung issue #4 — 16/11/2026

Đã cập nhật feature/chat từ main tại 73e9715 trước khi sửa.

- [x] History trong RAM và snapshot giữ 500 tin gần nhất mỗi peer bằng deque.
  Unread vẫn tính cả tin đã bị loại; chọn peer reset, peer đang mở nhận tin
  giữ unread=0. Kết quả gửi muộn không đưa tin đã bị loại trở lại history.
- [x] OPEN dùng deadline chung 3 giây từ accept. MAX_HANDSHAKES=16 trong chat.py
  giới hạn worker handshake; hết slot đóng kết nối mới ngay. Kết thúc OPEN hoặc
  lỗi/timeout trả slot. File manager tiếp nhận socket và đặt timeout riêng.
- [x] Hai peer cùng connect chọn hạng nhỏ nhất của (peer_id bên khởi tạo,
  TCP source port), theo PROTOCOL. Kết nối dư gửi nốt frame đang ghi, half-close
  rồi đọc tới EOF để giữ các tin đang truyền. Cleanup của session cũ không xóa
  session mới. Lệnh gửi mới chuyển sang kết nối được giữ.
- [x] Regression test cho giới hạn history/unread, kết quả gửi muộn, OPEN phân
  mảnh và nhiều MSG liền nhau, gửi nhỏ giọt, hết slot/giải phóng slot, connect
  đồng thời, chuyển socket khi đang gửi frame Unicode dở và reconnect.

Nội dung để Member 1 tổng hợp TEAM_WORKFLOW nằm trong các quy tắc trên và
PROTOCOL mục 3/6. API ChatService và schema conversation vẫn theo hợp đồng v1.
Các kết quả local và bài test bổ sung không đánh dấu hoàn tất checklist LAN
ở trên. Nghiệm thu hai máy, Wireshark và báo cáo tiếp tục theo issue #10.

Kết quả ngày 16/11/2026 trên Windows, Python runtime của Codex: **72 test pass**,
gồm **11 test chat** (7 test mới), toàn bộ test discovery/GUI/file/protocol và
tích hợp main. Lệnh unittest discover như trên; git diff --check đạt.
Tkinter có in cảnh báo ThemeChanged sau khi một cửa sổ test bị hủy; test đó
và toàn bộ suite vẫn trả OK. Kiểm thử mạng ở đây dùng loopback.
