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


# Drawn in Tk itself: no downloaded font, raster asset or extra dependency.
PALETTE = {
    "background": "#FCE8EE", "paper": "#FFFAF5", "panel": "#FFF1F3",
    "pink": "#F5C0D2", "pink_light": "#FBDCE6", "accent": "#A6476C",
    "border": "#784253", "ink": "#64374B", "muted": "#986F7F",
    "line": "#E7B4C7", "white": "#FFFFFF", "green": "#527862",
    "incoming": "#F9DFE8", "outgoing": "#F3BDCF", "error": "#A23250",
}

def _pixel_image(master, kind, scale=3):
    drawings = {
        "heart": (
            "............", ".##..##.....", "#pp##pp#....", "#pppppp#....",
            "#pppppp#....", ".#pppp#.....", "..#pp#......", "...##.......",
        ),
        "chat": (
            ".#########..", "#wwwwwwwww#.", "#wwwwwwwww#.", "#ww#ww#www#.",
            "#wwwwwwwww#.", "#ww####www#.", ".##ww#####..", "..#w#.......",
            "..##........",
        ),
        "flower": (
            "....###.....", "...#ppp#....", ".###ppp###..", "#pppyyyppp#.",
            "#pppyyyppp#.", ".###ppp###..", "...#ppp#....", "....###.....",
        ),
        "file": (
            "..######....", "..#wwww##...", "..#wwww#w#..", "..#wwww###..",
            "..#wwwwww#..", "..#wppppw#..", "..#wwwwww#..", "..########..",
        ),
    }
    rows = drawings[kind]
    image = tk.PhotoImage(master=master, width=12 * scale, height=len(rows) * scale)
    colors = {"#": PALETTE["border"], "p": PALETTE["pink"],
              "w": PALETTE["paper"], "y": "#F1CF87"}
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            if value in colors:
                image.put(colors[value], to=(x * scale, y * scale,
                                             (x + 1) * scale, (y + 1) * scale))
    return image


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
        c = PALETTE
        self.root.title(f"chat.exe — {username}")
        self.root.geometry("1140x820")
        self.root.minsize(920, 700)
        self.root.configure(background=c["background"])
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._icons = {kind: _pixel_image(self.root, kind) for kind in
                       ("heart", "chat", "flower", "file")}
        self.root.iconphoto(True, self._icons["chat"])
        self._style()
        self._username = username
        self._demo = demo

        # Window-like title strip; actual window management remains native.
        top = tk.Frame(self.root, background=c["pink"], highlightthickness=2,
                       highlightbackground=c["border"], padx=16, pady=10)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 12))
        top.columnconfigure(1, weight=1)
        tk.Label(top, image=self._icons["chat"], background=c["pink"]).grid(
            row=0, column=0, rowspan=2, padx=(0, 12))
        tk.Label(top, text="chat.exe", background=c["pink"], foreground=c["ink"],
                 font=("Consolas", 25, "bold")).grid(row=0, column=1, sticky="w")
        tk.Label(top, text="MỘT GÓC NHỎ, KẾT NỐI CÙNG NHAU",
                 background=c["pink"], foreground=c["ink"],
                 font=("Segoe UI", 8, "bold")).grid(row=1, column=1, sticky="w")
        tk.Label(top, text="✧  ♡  ✧", background=c["pink"], foreground=c["accent"],
                 font=("Segoe UI", 18)).grid(row=0, column=2, rowspan=2, padx=18)
        ttk.Button(top, text="Trợ giúp", style="Rose.TButton",
                   command=self.show_help).grid(row=0, column=3, rowspan=2, padx=4)
        ttk.Button(top, text="Thoát", style="Rose.TButton",
                   command=self.close).grid(row=0, column=4, rowspan=2, padx=(4, 0))

        body = tk.Frame(self.root, background=c["background"])
        body.grid(row=1, column=0, sticky="nsew", padx=20)
        body.columnconfigure(0, minsize=260)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = self._panel(body, c["paper"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        profile = tk.Frame(left, background=c["pink_light"], padx=16, pady=16)
        profile.grid(row=0, column=0, sticky="ew")
        profile.columnconfigure(1, weight=1)
        tk.Label(profile, image=self._icons["flower"], background=c["paper"],
                 padx=7, pady=7, highlightthickness=1,
                 highlightbackground=c["line"]).grid(row=0, column=0, rowspan=2, padx=(0, 12))
        tk.Label(profile, text=username, background=c["pink_light"], foreground=c["ink"],
                 font=("Segoe UI", 13, "bold"), anchor="w", width=12).grid(
                     row=0, column=1, sticky="ew")
        tk.Label(profile, text="Góc nhỏ của bạn", background=c["pink_light"],
                 foreground=c["muted"], font=("Segoe UI", 9)).grid(row=1, column=1, sticky="w")
        self.status_var = tk.StringVar(value="online")
        self.status_box = ttk.Combobox(profile, textvariable=self.status_var,
                                       values=("online", "away"), state="readonly",
                                       style="Rose.TCombobox", width=12)
        self.status_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.status_box.bind("<<ComboboxSelected>>", self._on_status)

        peer_heading = tk.Frame(left, background=c["paper"], padx=16, pady=14)
        peer_heading.grid(row=1, column=0, sticky="ew")
        peer_heading.columnconfigure(0, weight=1)
        tk.Label(peer_heading, text="BẠN BÈ CÙNG LAN", background=c["paper"],
                 foreground=c["ink"], font=("Segoe UI", 9, "bold")).grid(
                     row=0, column=0, sticky="w")
        self.peer_count_var = tk.StringVar(value="0")
        tk.Label(peer_heading, textvariable=self.peer_count_var,
                 background=c["pink_light"], foreground=c["accent"],
                 font=("Consolas", 10, "bold"), padx=7, pady=2).grid(row=0, column=1)

        peer_frame = tk.Frame(left, background=c["paper"])
        peer_frame.grid(row=2, column=0, sticky="nsew", padx=8)
        peer_frame.columnconfigure(0, weight=1)
        peer_frame.rowconfigure(0, weight=1)
        self.peer_tree = ttk.Treeview(
            peer_frame, columns=("name", "address", "status", "unread"),
            displaycolumns=("name", "unread"), show="headings",
            selectmode="browse", height=6, style="Rose.Peer.Treeview")
        for column, title, width in (("name", "Tên · địa chỉ", 195),
                                     ("address", "IP : Port", 140),
                                     ("status", "Trạng thái", 65), ("unread", "Mới", 38)):
            self.peer_tree.heading(column, text=title)
            self.peer_tree.column(column, width=width, minwidth=32,
                                  stretch=column == "name",
                                  anchor="center" if column == "unread" else "w")
        self.peer_tree.grid(row=0, column=0, sticky="nsew")
        self.peer_tree.tag_configure("away", foreground=c["muted"])
        self.peer_tree.bind("<<TreeviewSelect>>", self._on_peer_selected)
        peer_scroll = ttk.Scrollbar(peer_frame, command=self.peer_tree.yview,
                                    style="Rose.Vertical.TScrollbar")
        peer_scroll.grid(row=0, column=1, sticky="ns")
        self.peer_tree.configure(yscrollcommand=peer_scroll.set)
        tk.Label(left, text="✧  Cùng mạng, gần nhau hơn  ✧", background=c["paper"],
                 foreground=c["muted"], font=("Segoe UI", 9), pady=16).grid(
                     row=3, column=0, sticky="ew")

        right = self._panel(body, c["paper"])
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        heading = tk.Frame(right, background=c["panel"], padx=18, pady=13)
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(1, weight=1)
        tk.Label(heading, image=self._icons["heart"], background=c["panel"]).grid(
            row=0, column=0, rowspan=2, padx=(0, 8))
        self.conversation_title = tk.StringVar(value="Một cuộc trò chuyện nhỏ")
        self.conversation_detail = tk.StringVar(value="Chọn một người bạn để bắt đầu")
        ttk.Label(heading, textvariable=self.conversation_title, style="Rose.Title.TLabel").grid(
            row=0, column=1, sticky="w")
        ttk.Label(heading, textvariable=self.conversation_detail,
                  style="Rose.Detail.TLabel").grid(row=1, column=1, sticky="w", pady=(3, 0))
        self.leave_button = ttk.Button(heading, text="Rời hội thoại",
                                      style="Rose.TButton", command=self._on_leave)
        self.leave_button.grid(row=0, column=2, rowspan=2, padx=(8, 0))

        chat_frame = tk.Frame(right, background=c["paper"])
        chat_frame.grid(row=1, column=0, sticky="nsew")
        chat_frame.columnconfigure(0, weight=1)
        chat_frame.rowconfigure(0, weight=1)
        self.chat_text = tk.Text(
            chat_frame, wrap="word", state="disabled", font=("Segoe UI", 11),
            padx=22, pady=18, background=c["paper"], foreground=c["ink"],
            relief="flat", borderwidth=0, highlightthickness=0, cursor="arrow",
            width=40, height=8, spacing1=4, spacing3=6,
            selectbackground=c["pink"], selectforeground=c["ink"])
        self.chat_text.grid(row=0, column=0, sticky="nsew")
        for tag, color, align in (("incoming", c["incoming"], "left"),
                                  ("outgoing", c["outgoing"], "right")):
            self.chat_text.tag_configure(tag, background=color, foreground=c["ink"],
                                         justify=align, lmargin1=16, lmargin2=16,
                                         rmargin=16, spacing1=7, spacing3=7,
                                         lmargincolor=c["paper"], rmargincolor=c["paper"],
                                         borderwidth=1, relief="solid")
        self.chat_text.tag_configure("author_in", foreground=c["muted"],
                                     font=("Segoe UI", 9, "bold"), spacing1=12)
        self.chat_text.tag_configure("author_out", foreground=c["accent"],
                                     font=("Segoe UI", 9, "bold"), justify="right", spacing1=12)
        self.chat_text.tag_configure("failed", foreground=c["error"])
        self.chat_text.tag_configure("gap", spacing1=0, spacing3=0,
                                     font=("Segoe UI", 4))
        self._bubble_layout = []
        self.chat_text.bind("<Configure>", self._resize_bubbles)
        scroll = ttk.Scrollbar(chat_frame, command=self.chat_text.yview,
                               style="Rose.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.chat_text.configure(yscrollcommand=scroll.set)
        self.empty_state = tk.Frame(chat_frame, background=c["paper"])
        tk.Label(self.empty_state, image=self._icons["chat"], background=c["paper"]).pack(pady=(0, 14))
        tk.Label(self.empty_state, text="Xin chào, người bạn cùng LAN!",
                 background=c["paper"], foreground=c["ink"],
                 font=("Segoe UI", 15, "bold")).pack()
        self.empty_hint = tk.Label(
            self.empty_state, text="Chọn một người ở bên trái.\nMột lời chào cũng đủ để bắt đầu ♡",
            background=c["paper"], foreground=c["muted"],
            font=("Segoe UI", 10), justify="center")
        self.empty_hint.pack(pady=(8, 0))
        self.empty_state.place(relx=0.5, rely=0.46, anchor="center")

        # File activity belongs to the conversation column, above the composer.
        transfer = tk.Frame(right, background=c["panel"], padx=14, pady=10,
                            highlightthickness=1, highlightbackground=c["line"])
        transfer.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        transfer.columnconfigure(0, weight=1)
        tk.Label(transfer, text="TỆP ĐÍNH KÈM", background=c["panel"],
                 foreground=c["ink"], font=("Segoe UI", 9, "bold")).grid(
                     row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(transfer, text="Hủy lượt chọn",
                                        style="Small.Rose.TButton", command=self._on_cancel)
        self.cancel_button.grid(row=0, column=1, sticky="e", pady=(0, 6))
        self.transfer_tree = ttk.Treeview(
            transfer, columns=("file", "peer", "direction", "progress", "state"),
            show="headings", height=2, selectmode="browse", style="Rose.Transfer.Treeview")
        for column, label, width in (("file", "Tên tệp", 160), ("peer", "Bạn bè", 90),
                                     ("direction", "Chiều", 45), ("progress", "Tiến độ", 65),
                                     ("state", "Trạng thái", 180)):
            self.transfer_tree.heading(column, text=label)
            self.transfer_tree.column(column, width=width, minwidth=35,
                                      stretch=column in {"file", "state"})
        self.transfer_tree.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.transfer_tree.bind("<<TreeviewSelect>>", self._on_transfer_selected)
        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(transfer, variable=self.progress_var, maximum=100,
                        style="Rose.Horizontal.TProgressbar").grid(
                            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        composer = tk.Frame(right, background=c["pink_light"], padx=14, pady=12)
        composer.grid(row=3, column=0, sticky="ew")
        composer.columnconfigure(1, weight=1)
        self.file_button = ttk.Button(
            composer, text="Tệp", image=self._icons["file"], compound="left",
            style="Rose.TButton", command=self._on_file)
        self.file_button.grid(row=0, column=0, padx=(0, 10))
        self.draft_var = tk.StringVar()
        self.message_entry = ttk.Entry(composer, textvariable=self.draft_var,
                                        font=("Segoe UI", 11), style="Rose.TEntry")
        self.message_entry.grid(row=0, column=1, sticky="ew", ipady=7)
        self.message_entry.bind("<Return>", self._on_send)
        self.send_button = ttk.Button(composer, text="Gửi  ♡", style="Send.Rose.TButton",
                                      command=self._on_send)
        self.send_button.grid(row=0, column=2, padx=(10, 0))
        tk.Label(composer, text="Một tin nhắn nhỏ, một ngày vui hơn.   ·   Enter để gửi",
                 background=c["pink_light"], foreground=c["ink"],
                 font=("Segoe UI", 8)).grid(
                     row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        footer = tk.Frame(self.root, background=c["background"], padx=2, pady=9)
        footer.grid(row=2, column=0, sticky="ew", padx=20)
        footer.columnconfigure(0, weight=1)
        self.notice_var = tk.StringVar(value="Sẵn sàng cho một lời chào.")
        self.notice_label = tk.Label(
            footer, textvariable=self.notice_var, background=c["background"],
            foreground=c["ink"], font=("Segoe UI", 9), anchor="w", justify="left",
            wraplength=1000)
        self.notice_label.grid(row=0, column=0, sticky="ew")
        self.notice_label.bind("<Configure>",
                               lambda event: self.notice_label.configure(wraplength=max(100, event.width)))
        tk.Label(footer, text="✧ ♡ ✧", background=c["background"], foreground=c["accent"],
                 font=("Segoe UI", 12)).grid(row=0, column=1, padx=(12, 0))
        if demo:
            tk.Label(
                footer, text=(demo if isinstance(demo, str) else
                    "BẢN THỬ  ·  Peer, trạng thái và chat mô phỏng. Truyền tệp dùng TCP thật."),
                background=c["background"], foreground=c["muted"],
                font=("Segoe UI", 8), anchor="w").grid(
                    row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self._update_controls()

    def _style(self):
        c = PALETTE
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Rose.TButton", background=c["paper"], foreground=c["ink"],
                        bordercolor=c["border"], lightcolor=c["paper"], darkcolor=c["line"],
                        borderwidth=2, padding=(11, 7), font=("Segoe UI", 9, "bold"),
                        focuscolor=c["accent"])
        style.map("Rose.TButton", background=[("disabled", c["panel"]),
                  ("pressed", c["pink"]), ("active", c["pink_light"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("Send.Rose.TButton", background=c["accent"], foreground=c["white"],
                        bordercolor=c["border"], lightcolor=c["accent"], darkcolor=c["border"],
                        padding=(18, 10))
        style.map("Send.Rose.TButton", background=[("disabled", c["pink_light"]),
                  ("pressed", c["border"]), ("active", "#94375E")],
                  foreground=[("disabled", c["muted"]), ("!disabled", c["white"])])
        style.configure("Small.Rose.TButton", padding=(8, 3), font=("Segoe UI", 8))
        style.configure("Rose.Title.TLabel", background=c["panel"], foreground=c["ink"],
                        font=("Segoe UI", 13, "bold"))
        style.configure("Rose.Detail.TLabel", background=c["panel"], foreground=c["muted"],
                        font=("Segoe UI", 9))
        style.configure("Rose.TEntry", fieldbackground=c["paper"], foreground=c["ink"],
                        bordercolor=c["border"], lightcolor=c["paper"], darkcolor=c["line"],
                        insertcolor=c["ink"], padding=7)
        style.map("Rose.TEntry", fieldbackground=[("disabled", c["panel"])],
                  bordercolor=[("focus", c["accent"])])
        style.configure("Rose.TCombobox", fieldbackground=c["paper"], background=c["paper"],
                        foreground=c["ink"], arrowcolor=c["accent"], bordercolor=c["line"],
                        padding=5, font=("Segoe UI", 10))
        style.map("Rose.TCombobox", fieldbackground=[("readonly", c["paper"])],
                  selectbackground=[("readonly", c["paper"])],
                  selectforeground=[("readonly", c["ink"])])
        self.root.option_add("*TCombobox*Listbox.background", c["paper"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["pink"])
        for name, height in (("Rose.Peer.Treeview", 62), ("Rose.Transfer.Treeview", 27)):
            style.configure(name, background=c["paper"], fieldbackground=c["paper"],
                            foreground=c["ink"], rowheight=height, borderwidth=0,
                            font=("Segoe UI", 9), relief="flat",
                            bordercolor=c["line"], lightcolor=c["paper"], darkcolor=c["paper"])
            style.map(name, background=[("selected", c["pink_light"])],
                      foreground=[("selected", c["ink"])])
            style.configure(name + ".Heading", background=c["panel"], foreground=c["muted"],
                            font=("Segoe UI", 8, "bold"), padding=(6, 5),
                            borderwidth=0, relief="flat")
            style.map(name + ".Heading", background=[("active", c["pink_light"])])
        style.configure("Rose.Vertical.TScrollbar", background=c["pink_light"],
                        troughcolor=c["paper"], arrowcolor=c["accent"], borderwidth=0,
                        width=11)
        style.configure("Rose.Horizontal.TProgressbar", background=c["accent"],
                        troughcolor=c["pink_light"], bordercolor=c["line"],
                        lightcolor=c["accent"], darkcolor=c["accent"], thickness=7)

    @staticmethod
    def _panel(parent, background):
        return tk.Frame(parent, background=background, highlightthickness=2,
                        highlightbackground=PALETTE["border"], borderwidth=0)

    def _resize_bubbles(self, event=None):
        width = max(100, self.chat_text.winfo_width() - 44)
        font = self.chat_text.cget("font")
        for tag, text, outgoing in self._bubble_layout:
            longest = max((int(self.chat_text.tk.call("font", "measure", font, line))
                           for line in text.splitlines()), default=0)
            bubble_width = min(int(width * 0.78), max(110, longest + 36))
            opposite = max(12, width - bubble_width)
            left, right = (opposite, 12) if outgoing else (12, opposite)
            self.chat_text.tag_configure(tag, lmargin1=left, lmargin2=left, rmargin=right)

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
                self.peer_count_var.set(str(len(self.peer_tree.get_children())))
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
            # PhotoImage finalizers must execute on the Tk thread.
            self._icons.clear()
            self.root.destroy()

    def _render_peer(self, pid):
        peer = self._peers.get(pid)
        if not peer or peer.get("status") == "offline":
            return
        unread = self._conversations.get(pid, {}).get("unread", 0)
        label = "online" if peer["status"] == "online" else "tạm vắng"
        values = (f"{peer['name']} · {label}\n{peer['ip']}:{peer['tcp_port']}",
                  f"{peer['ip']}:{peer['tcp_port']}", peer["status"], unread or "")
        if self.peer_tree.exists(pid):
            self.peer_tree.item(pid, values=values, tags=(peer["status"],))
        else:
            self.peer_tree.insert("", "end", iid=pid, values=values, tags=(peer["status"],))
        self.peer_count_var.set(str(len(self.peer_tree.get_children())))

    def _render_conversation(self):
        pid = self._selected_peer
        peer = self._peers.get(pid, {})
        status = {"online": "Đang online", "away": "Tạm vắng", "offline": "Đã offline"}
        self.conversation_title.set(peer.get("name", pid) if pid else "Một cuộc trò chuyện nhỏ")
        self.conversation_detail.set(
            f"{status.get(peer.get('status'), '')}  ·  {peer.get('ip', '')}:{peer.get('tcp_port', '')}"
            if pid else "Chọn một người bạn để bắt đầu")
        # Never edit/focus the composer when incoming messages redraw history.
        old_position = self.chat_text.yview()
        at_end = old_position[1] >= 0.98
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        messages = self._conversations.get(pid, {}).get("messages", [])
        self._bubble_layout = []
        # Reuse at most one geometry tag per retained message.
        for tag in self.chat_text.tag_names():
            if tag.startswith("bubble_"):
                self.chat_text.tag_delete(tag)
        for index, message in enumerate(messages):
            outgoing = message.get("outgoing", False)
            author = "Bạn" if outgoing else message.get("sender", peer.get("name", "Peer"))
            state = {"failed": " · Gửi lỗi", "pending": " · Đang gửi"}.get(message.get("state"), "")
            self.chat_text.insert("end", f"{author}{state}\n",
                                  ("author_out" if outgoing else "author_in",))
            bubble_tag = f"bubble_{index}"
            self.chat_text.tag_configure(bubble_tag)
            self._bubble_layout.append((bubble_tag, message["text"], outgoing))
            tags = ("outgoing" if outgoing else "incoming", bubble_tag)
            if message.get("state") == "failed":
                tags += ("failed",)
            self.chat_text.insert("end", f"  {message['text']}  \n", tags)
            self.chat_text.insert("end", "\n", ("gap",))
        self._resize_bubbles()
        self.chat_text.configure(state="disabled")
        if messages:
            self.empty_state.place_forget()
        else:
            self.empty_hint.configure(text=(
                "Gửi một lời chào để bắt đầu hội thoại ♡" if pid else
                "Chọn một người ở bên trái.\nMột lời chào cũng đủ để bắt đầu ♡"))
            self.empty_state.place(relx=0.5, rely=0.46, anchor="center")
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
        if pid and self.peer_tree.exists(pid):
            self.peer_tree.selection_set(pid)
        else:
            self.peer_tree.selection_remove(*self.peer_tree.selection())
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
        stage = {"preparing": "Đang chuẩn bị", "transferring": "Đang truyền",
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
