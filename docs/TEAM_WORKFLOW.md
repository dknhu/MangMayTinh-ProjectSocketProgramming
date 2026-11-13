# Cách member 1–2 code tiếp trên phần member 3

Áp dụng cho repo có remote `dknhu/MangMayTinh-ProjectSocketProgramming`, thư mục
`LAN_Chat_Simplified/LAN_Chat_Simplified`. Không code trong bản `LAN-Chat-Project`
khác ở cùng workspace rồi chép đè qua. Mỗi người clone **cùng repository**.

## 1. Chia quyền sửa file

| Người | File sửa chính | Trách nhiệm tích hợp |
|---|---|---|
| Member 1 | src/discovery.py, src/main.py, tests/test_discovery.py, docs/MEMBER_1.md | Khởi tạo các service, nối callbacks và shutdown |
| Member 2 | src/chat.py, tests/test_chat.py, docs/MEMBER_2.md | Một TCP listener, chat/history/unread, OPEN handoff cho file |
| Member 3 | src/gui.py, src/file_transfer.py, src/member3_demo.py, tests/test_gui.py, tests/test_file_transfer.py, docs/MEMBER_3*.md | GUI và file; sửa khi tích hợp phát hiện lỗi |
| Dùng chung, member 2 điều phối | src/protocol.py, tests/test_protocol.py, docs/PROTOCOL.md | **Giữ v1 hiện có**, chỉ thay đổi trong PR riêng có review ảnh hưởng file/GUI |
| Member 1 tổng hợp | README.md, docs/ARCHITECTURE.md, docs/TEAM_WORKFLOW.md | Hai người còn lại góp ý, tránh cùng sửa khi đang phát triển module |

Member 1–2 **không cần sửa GUI/file** để nối backend: dùng UIActions và queue.
Member 2 import hàm protocol hiện có, không viết một parser MSG|... khác.
Nếu phát hiện API thiếu, trao đổi và cập nhật hợp đồng trước khi sửa phần dùng chung.

## 2. Đưa bản member 3 làm nền chung trước

Nhánh bàn giao member 3 là **nhukei**, theo tên người phụ trách đã chọn.
Main có thể tiếp tục thay đổi trong lúc mỗi người code; kiểm tra git status/log
trước khi thao tác. Các branch feature/udp và feature/chat dành cho member 1–2.
GitHub chưa có code mới chỉ vì file đã có ở máy người viết.

Người phụ trách member 3 kiểm tra diff, đưa phần này vào nhánh của mình:

```powershell
git status --short
git diff
git switch nhukei
git add src/file_transfer.py src/gui.py src/protocol.py src/member3_demo.py
git add tests/test_file_transfer.py tests/test_gui.py tests/test_protocol.py
git add README.md docs
git diff --cached
python -X utf8 -m unittest discover -s tests -v
git commit -m "Implement member 3 GUI, TCP file transfer and integration contracts"
git push -u origin nhukei
```

Mở PR nhukei → main. Một thành viên khác review, chạy kiểm thử rồi merge.
Sau đó cả hai cập nhật nhánh đang có; không cần tạo lại nhánh:

```powershell
# Member 1, sau khi lưu/commit công việc đang làm của mình
git fetch origin
git switch feature/udp
git merge origin/main

# Member 2 chạy trên máy của mình
git fetch origin
git switch feature/chat
git merge origin/main
```

Chỉ chạy chuỗi của thành viên tương ứng. Mỗi lần cập nhật main, lưu/commit phần
đang làm trước. Không giải quyết conflict bằng chép đè toàn bộ thư mục hoặc chọn
nguyên ours/theirs. Không force-push main hoặc branch người khác.

Các lệnh trên dùng để cập nhật nhánh đã tồn tại; nếu chưa có, tạo nhánh bằng
`git switch -c nhukei` trước lần commit đầu tiên. Không merge main khi chưa review.

## 3. Hợp đồng member 1: DiscoveryService

Đây là **API cần triển khai**, chưa có class trong discovery.py. Chữ ký cố định
để main và chat dùng được mà không sửa file của nhau:

```python
class DiscoveryService:
    def __init__(self, local_peer, event_queue, udp_port=9999): ...
    def start_discovery(self): ...
    def stop_discovery(self): ...
    def get_peers(self): ...   # dict[peer_id, peer_snapshot], bản sao, có lock
    def set_status(self, status): ...
```

Peer theo PROTOCOL: peer_id = IPv4:TCP_port, name, ip, tcp_port, status, last_seen.
Không dùng tên làm ID. start/stop quản lý worker UDP; stop gọi lại được an toàn.
Start UDP sau khi TCP bind/listen thành công; local_peer có IP/port thực tế.

Phát vào event_queue theo đúng tên mà GUI đang xử lý:

```python
{"type": "peer_upsert", "peer": peer_snapshot}
{"type": "peer_removed", "peer_id": pid}
{"type": "status_changed", "status": "away"}
{"type": "operation_failed", "error": "Nội dung lỗi"}
```

Member 1 quản lý peer list, member 2 chỉ gọi get_peers/resolve_peer để lấy bản sao.
Không để chat/GUI sửa dictionary nội bộ của discovery.

## 4. Hợp đồng member 2: ChatService

Đây là **API cần triển khai** trong chat.py:

```python
class ChatService:
    def __init__(self, local_peer, event_queue, resolve_peer,
                 file_receiver, bind_host="0.0.0.0"): ...
    def start_listener(self): ...
    def stop_listener(self): ...
    def select_peer(self, peer_id): ...  # None = không xem hội thoại nào
    def send_message(self, peer_id, text): ...
    def leave_chat(self, peer_id): ...
```

- resolve_peer(pid) trả peer snapshot hoặc raise lỗi nếu không còn tìm thấy.
- start_listener bind local_peer['tcp_port']; báo lỗi ngay nếu bind thất bại,
  rồi nhận kết nối nền. Không chiếm Tk thread.
- file_receiver là callback `FileTransferManager.receive_file`, chữ ký
  `(socket, opening_header, source_ip)`. Chat đọc OPEN rồi bàn giao độc quyền.
- select_peer đổi active/reset unread nguyên tử, phát conversation kể cả rỗng;
  có thể lên lịch connect nền khi chọn peer mà chưa có kết nối.
- send_message ghi nhận yêu cầu vào history/hàng đợi và trả nhanh; connect/send
  chạy trên worker. Tin gửi lỗi có state='failed', không được im lặng bỏ tin.
- leave_chat chỉ đóng chat của peer đó; không dừng UDP/file. Giữ history.
- stop_listener dừng listener/handshake/chat workers, không đóng socket đã bàn giao.
- Chat sở hữu history/unread/revision; lựa chọn quản lý session/kết nối đồng thời
  nằm trong chat.py, không đẩy trách nhiệm đó sang GUI hoặc main.

Event gửi GUI (snapshot mới, revision tăng theo từng peer):

```python
{"type": "conversation", "peer_id": pid, "revision": 1, "unread": 0,
 "messages": [{"text": "Xin chào", "outgoing": True, "state": "sent"}]}
```

Nhận tin của active peer: unread=0; peer khác: tăng unread. GUI chỉ hiển thị.
Không tự đổi tên event thành message_received/unread_changed: GUI hiện dùng
conversation snapshot. Ví dụ OPEN và framing theo PROTOCOL, không tự đổi format.

## 5. Main nối các API; member 1 là người sửa main.py

Mẫu sau thể hiện cách ghép sau khi hai service được triển khai. Member 1 bổ sung
CLI cấu hình tên/IP/port và xử lý lỗi khởi động, không hardcode IP trong app thật.

```python
events = queue.Queue()
discovery = DiscoveryService(local_peer, events)
files = FileTransferManager(local_peer, events)

def resolve_peer(pid):
    return discovery.get_peers()[pid]

chat = ChatService(local_peer, events, resolve_peer, files.receive_file)

def shutdown():
    errors = []
    for stop in (discovery.stop_discovery, chat.stop_listener, files.stop_transfers):
        try:
            stop()
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))

actions = UIActions(
    select_peer=chat.select_peer,
    send_message=chat.send_message,
    send_file=lambda pid, path: files.send_file(resolve_peer(pid), path),
    set_status=discovery.set_status,
    leave_chat=chat.leave_chat,
    cancel_transfer=files.cancel_transfer,
    shutdown=shutdown,
)
```

Khởi động: TCP listener → discovery → run_ui trên main thread. Lỗi bất kỳ bước
nào phải dừng phần đã khởi động và báo lỗi. Shutdown được GUI gọi ở command worker,
đợi dọn file/worker xong mới destroy cửa sổ. Tất cả stop phải gọi lại an toàn.

## 6. Quy tắc giúp ghép đúng, ngoài chuyện tránh conflict Git

1. Một queue.Queue không giới hạn cho event; **GUI là consumer duy nhất**. Main
   không có thread khác lấy mất event. Module cần dữ liệu thì gọi API lấy snapshot.
2. Worker không đụng widget Tkinter; UIActions không chặn bằng I/O lâu.
3. Chỉ một TCP listener; phân OPEN purpose chat/file. File dùng TCP connection
   riêng, half-close đúng quy ước; chỉ một bên đọc/đóng mỗi socket.
4. Cả ba dùng protocol.py hiện có và PROTOCOL v1. Không thay tên key, type,
   callback hoặc đơn vị độ dài một phía. Thay đổi hợp đồng phải sửa docs + test
   + nơi gọi cùng PR, có người phụ trách module bị ảnh hưởng review.
5. Mỗi người thêm test riêng; giữ các test member 3 đã có. Trước PR chạy toàn bộ:

```powershell
python -X utf8 -m unittest discover -s tests -v
git diff --check
git diff --name-only origin/main
```

## 7. Thứ tự merge và ghép thử

1. Member 3/framing/contracts → main trước, để hai người cùng một nền.
2. Member 1 làm discovery và test; member 2 làm chat và test song song trên file riêng.
3. Merge discovery và chat qua PR nhỏ. Nếu main.py được viết trước chat có thể
   để trên feature/udp cho tới lúc cả hai API đã chạy, tránh main import class chưa có.
4. Ghép: peer/status lên GUI → chat/history/unread → FILE handoff và vừa chat vừa gửi.
5. Chạy lại trên hai máy vật lý, kiểm tra lỗi/thoát và thu Wireshark/báo cáo.

Member 2 báo API sẵn sàng cho member 1; không tự sửa main.py trên feature/chat.
Nếu cần thay wiring, member 1 làm một PR nhỏ sau khi chat merge. Review sớm từng
mốc thay vì chờ cả ba làm xong hết mới ghép.
