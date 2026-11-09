"""Tkinter UI, independent from discovery/chat implementations.

Backend -> event_queue -> after() -> widgets, all on Tk's main thread.
UI -> ordered command worker -> callbacks supplied by main.py.
Conversation snapshots (history + unread + revision) belong to chat.py.
"""

from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


@dataclass
class UIActions:
    # These callbacks must enqueue network work and return promptly. The GUI
    # calls them on an ordered worker so select/mark-read commands cannot race.
    select_peer: object       # (peer_id | None) -> None; emit conversation snapshot
    send_message: object      # (peer_id, text) -> None; backend owns history/unread
    send_file: object         # (peer_id, path) -> transfer_id
    set_status: object        # ("online" | "away") -> None; emit status_changed
    leave_chat: object        # (peer_id) -> None; file transfers remain independent
    cancel_transfer: object   # (transfer_id) -> bool
    shutdown: object          # () -> None; stop listener/discovery/transfers


class ChatWindow:
    POLL_MS = 40

    def __init__(self, root, username, event_queue, actions, demo=False):
        self.root = root
        self.events = event_queue
        self.actions = actions
        self._ui_thread = threading.get_ident()
        self._commands = queue.Queue()
        self._closing = False
        self._destroyed = False
        self._poll_id = None
        self._peers = {}
        self._conversations = {}
        self._drafts = {}
        self._selected_peer = None
        self._pending_send = set()
        self._transfers = {}
        self._local_status = "online"
        self._worker = threading.Thread(target=self._command_loop, name="ui-commands")
        self._build(username, demo)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._worker.start()
        self._poll_id = self.root.after(self.POLL_MS, self._poll_events)

    def _build(self, username, demo):
        self.root.title(f"LAN Chat — {username}")
        self.root.geometry("1080x740")
        self.root.minsize(820, 620)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=28)
        top = ttk.Frame(self.root, padding=(14, 10))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text=f"LAN Chat  |  {username}", font=("Segoe UI", 14, "bold")).grid(
            row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="online")
        self.status_box = ttk.Combobox(top, textvariable=self.status_var,
                                       values=("online", "away"), state="readonly", width=9)
        self.status_box.grid(row=0, column=2, padx=8)
        self.status_box.bind("<<ComboboxSelected>>", self._on_status)
        ttk.Button(top, text="Trợ giúp", command=self.show_help).grid(row=0, column=3, padx=4)
        ttk.Button(top, text="Thoát", command=self.close).grid(row=0, column=4, padx=4)
        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", padx=14)
        left = ttk.Frame(body, padding=(0, 0, 10, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="NGƯỜI DÙNG", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.peer_tree = ttk.Treeview(left, columns=("name", "address", "status", "unread"),
                                      show="headings", selectmode="browse", height=12)
        for column, title, width in (("name", "Tên", 100), ("address", "IP : Port", 140),
                                     ("status", "Trạng thái", 65), ("unread", "Mới", 40)):
            self.peer_tree.heading(column, text=title)
            self.peer_tree.column(column, width=width, minwidth=35)
        self.peer_tree.grid(row=1, column=0, sticky="nsew")
        self.peer_tree.bind("<<TreeviewSelect>>", self._on_peer_selected)
        body.add(left, weight=2)
        right = ttk.Frame(body)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        heading = ttk.Frame(right)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        heading.columnconfigure(0, weight=1)
        self.conversation_title = tk.StringVar(value="Chọn một người để bắt đầu")
        ttk.Label(heading, textvariable=self.conversation_title,
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        self.leave_button = ttk.Button(heading, text="Rời hội thoại", command=self._on_leave)
        self.leave_button.grid(row=0, column=1)
        chat_frame = ttk.Frame(right)
        chat_frame.grid(row=1, column=0, sticky="nsew")
        chat_frame.columnconfigure(0, weight=1)
        chat_frame.rowconfigure(0, weight=1)
        self.chat_text = tk.Text(chat_frame, wrap="word", state="disabled", font=("Segoe UI", 11),
                                 padx=12, pady=12, background="#faf8fa", relief="flat")
        self.chat_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(chat_frame, command=self.chat_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.chat_text.configure(yscrollcommand=scroll.set)
        composer = ttk.Frame(right, padding=(0, 10, 0, 0))
        composer.grid(row=2, column=0, sticky="ew")
        composer.columnconfigure(0, weight=1)
        self.draft_var = tk.StringVar()
        self.message_entry = ttk.Entry(composer, textvariable=self.draft_var, font=("Segoe UI", 11))
        self.message_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.message_entry.bind("<Return>", self._on_send)
        self.send_button = ttk.Button(composer, text="Gửi", command=self._on_send)
        self.send_button.grid(row=0, column=1, padx=3)
        self.file_button = ttk.Button(composer, text="Chọn file", command=self._on_file)
        self.file_button.grid(row=0, column=2, padx=3)
        body.add(right, weight=3)
        bottom = ttk.Frame(self.root, padding=(14, 10))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, text="TRUYỀN FILE", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(bottom, text="Hủy lượt đang chọn", command=self._on_cancel)
        self.cancel_button.grid(row=0, column=1, sticky="e")
        self.transfer_tree = ttk.Treeview(bottom,
                                          columns=("file", "peer", "direction", "progress", "state"),
                                          show="headings", height=4, selectmode="browse")
        for column, label, width in (("file", "File", 220), ("peer", "Peer", 150),
                                     ("direction", "Chiều", 65), ("progress", "Tiến độ", 100),
                                     ("state", "Trạng thái", 260)):
            self.transfer_tree.heading(column, text=label)
            self.transfer_tree.column(column, width=width, minwidth=55)
        self.transfer_tree.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        self.transfer_tree.bind("<<TreeviewSelect>>", self._on_transfer_selected)
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(bottom, variable=self.progress_var, maximum=100).grid(
            row=2, column=0, columnspan=2, sticky="ew")
        self.notice_var = tk.StringVar(value=(
            "Bản thử member 3: gửi file TCP thật; peer/status/chat là dữ liệu mô phỏng."
            if demo else "Sẵn sàng"))
        ttk.Label(bottom, textvariable=self.notice_var, wraplength=980).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._update_controls()

    def _submit(self, name, *args, **context):
        if not self._closing:
            self._commands.put((name, args, context))

    def _command_loop(self):
        while True:
            name, args, context = self._commands.get()
            try:
                result = getattr(self.actions, name)(*args)
            except Exception as exc:
                self.events.put({"type": "ui_command_result", "command": name,
                                 "error": str(exc), **context})
                if name == "shutdown":
                    # Let the user retry shutdown rather than silently exiting
                    # with a worker/partial file still alive.
                    continue
            else:
                self.events.put({"type": "ui_command_result", "command": name,
                                 "result": result, **context})
                if name == "shutdown":
                    return

    def _poll_events(self):
        if self._destroyed:
            return
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self.process_event(event)
            except (KeyError, ValueError, TypeError, tk.TclError) as exc:
                self.notice_var.set(f"Sự kiện backend không hợp lệ: {exc}")
            if self._destroyed:
                return
        self._poll_id = self.root.after(self.POLL_MS, self._poll_events)

    def post_event(self, event):
        """Thread-safe entrypoint; does not call any Tk API."""
        self.events.put(event)

    def process_event(self, event):
        """Only main thread may call this; workers must use post_event/queue."""
        if threading.get_ident() != self._ui_thread:
            raise RuntimeError("Chỉ main thread được cập nhật GUI")
        kind = event["type"]
        if kind == "peer_upsert":
            peer = dict(event["peer"])
            pid = peer["peer_id"]
            new = pid not in self._peers or self._peers[pid].get("status") == "offline"
            self._peers[pid] = peer
            self._render_peer(pid)
            if new:
                self.notice_var.set(f"{peer['name']} vừa online ({peer['ip']})")
            if pid == self._selected_peer:
                self._render_conversation()
        elif kind == "peer_removed":
            pid = event["peer_id"]
            if pid in self._peers:
                self._peers[pid]["status"] = "offline"
                self.notice_var.set(f"{self._peers[pid]['name']} đã offline")
                if self.peer_tree.exists(pid):
                    self.peer_tree.delete(pid)
                if pid == self._selected_peer:
                    self._render_conversation()
                else:
                    self._update_controls()
        elif kind == "conversation":
            pid = event["peer_id"]
            old = self._conversations.get(pid, {})
            if event["revision"] <= old.get("revision", -1):
                return
            self._conversations[pid] = dict(event)
            self._render_peer(pid)
            if pid == self._selected_peer:
                self._render_conversation()
        elif kind == "status_changed":
            self._local_status = event["status"]
            self.status_var.set(self._local_status)
            self.notice_var.set(f"Trạng thái của bạn: {self._local_status}")
        elif kind.startswith("transfer_"):
            self._render_transfer(event)
        elif kind == "operation_failed":
            self.notice_var.set(f"Lỗi: {event['error']}")
        elif kind == "ui_command_result":
            self._command_result(event)

    def _command_result(self, event):
        name = event["command"]
        error = event.get("error")
        if name == "send_message":
            pid, text = event["peer_id"], event["text"]
            self._pending_send.discard(pid)
            if not error:
                if self._selected_peer == pid and self.draft_var.get() == text:
                    self.draft_var.set("")
                elif self._selected_peer != pid and self._drafts.get(pid) == text:
                    self._drafts[pid] = ""
            self._update_controls()
        if error:
            self.notice_var.set(f"Lỗi {name}: {error}")
            if name == "set_status":
                self.status_var.set(self._local_status)
            if name == "shutdown":
                self._closing = False
                self.status_var.set(self._local_status)
                self.status_box.configure(state="readonly")
                self._update_controls()
        elif name == "shutdown":
            self._destroyed = True
            self.root.destroy()

    def _render_peer(self, pid):
        peer = self._peers.get(pid)
        if not peer or peer.get("status") == "offline":
            return
        unread = self._conversations.get(pid, {}).get("unread", 0)
        values = (peer["name"], f"{peer['ip']}:{peer['tcp_port']}", peer["status"], unread or "")
        if self.peer_tree.exists(pid):
            self.peer_tree.item(pid, values=values)
        else:
            self.peer_tree.insert("", "end", iid=pid, values=values)

    def _render_conversation(self):
        pid = self._selected_peer
        peer = self._peers.get(pid, {})
        self.conversation_title.set(f"{peer.get('name', pid)}  ·  {peer.get('status', '')}"
                                    if pid else "Chọn một người để bắt đầu")
        # Never edit/focus the composer when incoming messages redraw history.
        old_position = self.chat_text.yview()
        at_end = old_position[1] >= 0.98
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        for message in self._conversations.get(pid, {}).get("messages", []):
            author = "Bạn" if message.get("outgoing") else message.get("sender", peer.get("name", "Peer"))
            state = message.get("state", "")
            suffix = " [gửi lỗi]" if state == "failed" else ""
            self.chat_text.insert("end", f"{author}{suffix}: {message['text']}\n\n")
        self.chat_text.configure(state="disabled")
        if at_end:
            self.chat_text.see("end")
        else:
            self.chat_text.yview_moveto(old_position[0])
        self._update_controls()

    def _update_controls(self):
        pid = self._selected_peer
        online = bool(pid and self._peers.get(pid, {}).get("status") in {"online", "away"})
        can_send = online and not self._closing
        self.message_entry.configure(state="normal" if can_send else "disabled")
        self.send_button.configure(state="normal" if can_send and pid not in self._pending_send else "disabled")
        self.file_button.configure(state="normal" if can_send else "disabled")
        self.leave_button.configure(state="normal" if pid and not self._closing else "disabled")
        self.cancel_button.configure(state="disabled" if self._closing else "normal")

    def select_peer(self, pid):
        """Main-thread selection API, also useful to integration tests."""
        if self._closing or pid == self._selected_peer:
            return
        if pid is not None and pid not in self._peers:
            return
        if self._selected_peer:
            self._drafts[self._selected_peer] = self.draft_var.get()
        self._selected_peer = pid
        self.draft_var.set(self._drafts.get(pid, ""))
        self._render_conversation()
        self.chat_text.see("end")
        self._submit("select_peer", pid)
        if pid and self._peers[pid].get("status") != "offline":
            self.message_entry.focus_set()

    def _on_peer_selected(self, _event=None):
        selected = self.peer_tree.selection()
        if selected:
            self.select_peer(selected[0])

    def _on_send(self, _event=None):
        pid, text = self._selected_peer, self.draft_var.get()
        if (not self._closing and pid and text.strip() and pid not in self._pending_send
                and self._peers.get(pid, {}).get("status") in {"online", "away"}):
            self._pending_send.add(pid)
            self._update_controls()
            self._submit("send_message", pid, text, peer_id=pid, text=text)
        return "break"

    def _on_file(self):
        pid = self._selected_peer
        if not pid or self._closing:
            return
        path = filedialog.askopenfilename(parent=self.root, title="Chọn file gửi")
        if path and not self._closing and self._peers.get(pid, {}).get("status") != "offline":
            self._submit("send_file", pid, path)

    def _on_status(self, _event=None):
        self._submit("set_status", self.status_var.get())

    def _on_leave(self):
        pid = self._selected_peer
        if pid:
            self._submit("leave_chat", pid)
            self.select_peer(None)
            self.peer_tree.selection_remove(*self.peer_tree.selection())
            self.notice_var.set("Đã rời hội thoại; truyền file và khám phá vẫn tiếp tục")

    def _render_transfer(self, event):
        tid = event["transfer_id"]
        previous = self._transfers.get(tid, {})
        terminal = {"transfer_completed", "transfer_failed", "transfer_cancelled"}
        if previous.get("type") in terminal:
            return
        current = {**previous, **event}
        self._transfers[tid] = current
        stage = {"preparing": "Đang tính SHA-256", "transferring": "Đang truyền",
                 "awaiting_confirmation": "Chờ bên nhận xác nhận"}.get(current.get("stage"), "Đang truyền")
        state = {"transfer_completed": "Hoàn tất", "transfer_failed": "Lỗi: " + current.get("error", ""),
                 "transfer_cancelled": "Đã hủy"}.get(current["type"], stage)
        percent = current.get("percent", 0)
        peer_name = self._peers.get(current["peer_id"], {}).get("name", current["peer_id"])
        values = (current["filename"], peer_name, "Gửi" if current["direction"] == "send" else "Nhận",
                  f"{percent:.1f}%", state)
        if self.transfer_tree.exists(tid):
            self.transfer_tree.item(tid, values=values)
        else:
            self.transfer_tree.insert("", "end", iid=tid, values=values)
            self.transfer_tree.selection_set(tid)
            self.transfer_tree.see(tid)
        if tid in self.transfer_tree.selection():
            self.progress_var.set(percent)
        if current["type"] in terminal:
            detail = current.get("warning") or current.get("path") or current.get("saved_as", "")
            self.notice_var.set(f"{current['filename']}: {state} {detail}")

    def _on_transfer_selected(self, _event=None):
        selected = self.transfer_tree.selection()
        if selected:
            self.progress_var.set(self._transfers[selected[0]].get("percent", 0))

    def _on_cancel(self):
        selected = self.transfer_tree.selection()
        if selected and self._transfers[selected[0]]["type"] not in {
                "transfer_completed", "transfer_failed", "transfer_cancelled"}:
            self._submit("cancel_transfer", selected[0])

    def show_help(self):
        messagebox.showinfo("Cách dùng LAN Chat", (
            "Chọn người ở danh sách để xem hội thoại và tin chưa đọc.\n"
            "Nhập tin rồi nhấn Enter hoặc Gửi.\n"
            "Chọn file để gửi cho người đang chat; vẫn có thể chat khi truyền.\n"
            "100% đang gửi chưa phải hoàn tất: cần bên nhận xác nhận lưu.\n"
            "File nhận nằm trong downloads/; file trùng được thêm số.\n"
            "Rời hội thoại giữ lịch sử, bản nháp và các lượt truyền.\n"
            "Online/Away đổi trạng thái hiện diện. Thoát dừng các module."
        ), parent=self.root)

    def close(self):
        if self._closing or self._destroyed:
            return
        self._closing = True
        self.status_box.configure(state="disabled")
        self._update_controls()
        self.notice_var.set("Đang đóng kết nối và dọn file nhận dở…")
        # Discard commands that have not started; wait for the current callback
        # and perform all blocking stop/join work on the command worker.
        while True:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                break
        self._pending_send.clear()
        self._commands.put(("shutdown", (), {}))


def run_ui(username, event_queue, actions, demo=False):
    root = tk.Tk()
    window = ChatWindow(root, username, event_queue, actions, demo=demo)
    root.mainloop()
    return window
