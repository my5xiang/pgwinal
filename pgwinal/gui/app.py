from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from ..dictstore.builder import build_dictionary_from_postgres
from ..dictstore.schema import DictStore
from ..parse.engine import ParseOptions, WalParseEngine
from ..resultstore.resultdb import ResultStore

# ── Light theme (MiMo-like) ───────────────────────────────────
COLORS = {
    "bg": "#FFFFFF",
    "bg_panel": "#F2F3F5",
    "bg_elevated": "#FFFFFF",
    "bg_input": "#FFFFFF",
    "border": "#E5E6EB",
    "border_soft": "#EBEDF0",
    "text": "#1F2329",
    "text_muted": "#646A73",
    "text_dim": "#8F959E",
    "accent": "#3370FF",
    "accent_hover": "#245BDB",
    "accent_soft": "#E8F1FF",
    "success": "#2EA121",
    "warning": "#DE7802",
    "danger": "#F54A45",
    "row_alt": "#FAFBFC",
    "row_sel": "#E8F1FF",
    "header_bg": "#F7F8FA",
    "toolbar_bg": "#FFFFFF",
    "btn_bg": "#FFFFFF",
    "btn_hover": "#F2F3F5",
    "btn_active": "#EBEDF0",
    "logo_fill": "#E8F1FF",
    "green_dot": "#2EA121",
    "blue_dot": "#3370FF",
    "amber_dot": "#DE7802",
}

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_UI_SM = ("Microsoft YaHei UI", 9)
FONT_UI_XS = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")
FONT_MONO = ("Consolas", 9)

OP_COLORS = {
    "INSERT": "#2EA121",
    "UPDATE": "#DE7802",
    "DELETE": "#F54A45",
    "MULTI_INSERT": "#3370FF",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("pgwinal  ·  PostgreSQL WAL Recovery")
        self.geometry("1440x900")
        self.minsize(1280, 760)
        self.configure(bg=COLORS["bg"])

        self.base_dir = Path(__file__).resolve().parents[2]
        self.dict_path = self.base_dir / "dict" / "pgwinal_dict.sqlite"
        self.result_path = self.base_dir / "result" / "pgwinal_results.sqlite"
        self.wal_paths: list[Path] = []
        self._parsing = False

        self.dict_store = DictStore(self.dict_path)
        self.result_store = ResultStore(self.result_path)

        self._setup_style()
        self._build_ui()
        self._refresh_status()

    # ── Styles ────────────────────────────────────────────────
    def _setup_style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass

        c = COLORS
        s.configure(".", background=c["bg"], foreground=c["text"], font=FONT_UI)
        s.configure("TFrame", background=c["bg"])
        s.configure("Panel.TFrame", background=c["bg_panel"])
        s.configure("Toolbar.TFrame", background=c["toolbar_bg"])
        s.configure("Card.TFrame", background=c["bg_elevated"])

        s.configure("TLabel", background=c["bg_panel"], foreground=c["text"], font=FONT_UI)
        s.configure("Bg.TLabel", background=c["bg"], foreground=c["text"])
        s.configure("Muted.TLabel", background=c["bg_panel"], foreground=c["text_muted"], font=FONT_UI_SM)
        s.configure("Dim.TLabel", background=c["bg"], foreground=c["text_dim"], font=FONT_UI_XS)
        s.configure("Title.TLabel", background=c["bg_panel"], foreground=c["text"], font=FONT_TITLE)
        s.configure("Logo.TLabel", background=c["toolbar_bg"], foreground=c["accent"], font=("Microsoft YaHei UI", 14, "bold"))
        s.configure("Brand.TLabel", background=c["toolbar_bg"], foreground=c["text"], font=("Microsoft YaHei UI", 12, "bold"))
        s.configure("BrandSub.TLabel", background=c["toolbar_bg"], foreground=c["text_dim"], font=FONT_UI_XS)
        s.configure("Stat.TLabel", background=c["toolbar_bg"], foreground=c["text_muted"], font=FONT_UI_SM)
        s.configure("Section.TLabel", background=c["bg_panel"], foreground=c["text_dim"], font=("Microsoft YaHei UI", 8, "bold"))

        # Buttons
        s.configure(
            "TButton",
            background=c["bg_elevated"],
            foreground=c["text"],
            bordercolor=c["border"],
            lightcolor=c["bg_elevated"],
            darkcolor=c["bg_elevated"],
            borderwidth=1,
            focusthickness=1,
            focuscolor=c["accent"],
            padding=(12, 7),
            font=FONT_UI,
        )
        s.map(
            "TButton",
            background=[("active", c["btn_hover"]), ("pressed", c["btn_active"])],
            foreground=[("disabled", c["text_dim"])],
            bordercolor=[("focus", c["accent"]), ("active", c["border"])],
        )

        s.configure(
            "Primary.TButton",
            background=c["accent"],
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(16, 8),
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        s.map("Primary.TButton", background=[("active", c["accent_hover"]), ("pressed", "#1A4FCF")])

        s.configure(
            "Ghost.TButton",
            background=c["toolbar_bg"],
            foreground=c["text_muted"],
            bordercolor=c["border_soft"],
            padding=(11, 7),
            font=("Microsoft YaHei UI", 10),
        )
        s.map(
            "Ghost.TButton",
            background=[("active", c["btn_hover"])],
            foreground=[("active", c["text"])],
            bordercolor=[("active", c["border"])],
        )

        # Entry / Combobox
        s.configure(
            "TEntry",
            fieldbackground=c["bg_input"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
            lightcolor=c["border"],
            darkcolor=c["border"],
            padding=(8, 6),
        )
        s.map("TEntry", bordercolor=[("focus", c["accent"])], fieldbackground=[("focus", "#FFFFFF")])

        s.configure(
            "TCombobox",
            fieldbackground=c["bg_input"],
            foreground=c["text"],
            background=c["bg_elevated"],
            arrowcolor=c["text_muted"],
            bordercolor=c["border"],
            padding=(6, 5),
        )
        s.map(
            "TCombobox",
            fieldbackground=[("readonly", c["bg_input"])],
            foreground=[("readonly", c["text"])],
            bordercolor=[("focus", c["accent"])],
            arrowcolor=[("active", c["text"])],
        )

        # Listbox via tk options — styled in widget config

        # Treeview
        s.configure(
            "Treeview",
            background=c["bg_elevated"],
            fieldbackground=c["bg_elevated"],
            foreground=c["text"],
            borderwidth=0,
            font=FONT_UI_SM,
            rowheight=28,
        )
        s.map(
            "Treeview",
            background=[("selected", c["row_sel"])],
            foreground=[("selected", c["text"])],
        )
        s.configure(
            "Treeview.Heading",
            background=c["header_bg"],
            foreground=c["text_muted"],
            relief="flat",
            borderwidth=0,
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(8, 8),
        )
        s.map("Treeview.Heading", background=[("active", "#F0F1F3")])

        # Scrollbar
        s.configure("Vertical.TScrollbar", background=c["bg_elevated"], troughcolor=c["bg_panel"], bordercolor=c["bg_panel"], arrowcolor=c["text_muted"])
        s.map("Vertical.TScrollbar", background=[("active", c["border"])])

        # Separator
        s.configure("TSeparator", background=c["border_soft"])
        s.configure("V.TSeparator", background=c["border_soft"])

        # Checkbutton
        s.configure("TCheckbutton", background=c["bg_panel"], foreground=c["text"], font=FONT_UI)
        s.map("TCheckbutton", background=[("active", c["bg_panel"])])
        # indicator on clam
        s.configure("TCheckbutton indicatorbackground", background=c["bg_input"])
        s.configure("TCheckbutton indicatorforeground", background=c["accent"])

        # Progress
        s.configure(
            "Horizontal.TProgressbar",
            background=c["accent"],
            troughcolor=c["border_soft"],
            bordercolor=c["border_soft"],
            lightcolor=c["accent"],
            darkcolor=c["accent"],
            thickness=4,
        )

        # Panedwindow
        s.configure("TPanedwindow", background=c["border_soft"])
        s.configure("TPane", background=c["bg"])

    # ── Layout ────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

    def _build_toolbar(self) -> None:
        root = tk.Frame(self, bg=COLORS["toolbar_bg"])
        root.pack(fill=tk.X, side=tk.TOP)

        # ── Row 1: brand + primary ──
        row1 = tk.Frame(root, bg=COLORS["toolbar_bg"], height=48)
        row1.pack(fill=tk.X, padx=12, pady=(8, 2))
        row1.pack_propagate(False)

        brand = tk.Frame(row1, bg=COLORS["toolbar_bg"])
        brand.pack(side=tk.LEFT, fill=tk.Y)

        mark = tk.Canvas(brand, width=32, height=32, bg=COLORS["toolbar_bg"], highlightthickness=0)
        mark.pack(side=tk.LEFT, padx=(4, 8))
        mark.create_oval(1, 1, 31, 31, fill=COLORS["accent_soft"], outline=COLORS["accent"], width=2)
        mark.create_text(16, 16, text="W", fill=COLORS["accent"], font=("Consolas", 11, "bold"))

        texts = tk.Frame(brand, bg=COLORS["toolbar_bg"])
        texts.pack(side=tk.LEFT)
        tk.Label(texts, text="pgwinal", bg=COLORS["toolbar_bg"], fg=COLORS["text"], font=("Microsoft YaHei UI", 12, "bold")).pack(anchor=tk.W)
        tk.Label(texts, text="WAL · DO / UNDO SQL", bg=COLORS["toolbar_bg"], fg=COLORS["text_dim"], font=FONT_UI_XS).pack(anchor=tk.W)

        self.stat_label = tk.Label(row1, text="", bg=COLORS["toolbar_bg"], fg=COLORS["text_muted"], font=FONT_UI_XS)
        self.stat_label.pack(side=tk.RIGHT, padx=(8, 12))

        primary = ttk.Button(row1, text="开始解析", style="Primary.TButton", command=self.on_parse)
        primary.pack(side=tk.RIGHT, padx=4)

        # ── Row 2: all secondary actions ──
        row2 = tk.Frame(root, bg=COLORS["toolbar_bg"], height=40)
        row2.pack(fill=tk.X, padx=12, pady=(2, 6))
        row2.pack_propagate(False)

        actions = tk.Frame(row2, bg=COLORS["toolbar_bg"])
        actions.pack(side=tk.LEFT, fill=tk.Y)

        def add_btn(parent, text, cmd, primary=False):
            style = "Primary.TButton" if primary else "Ghost.TButton"
            b = ttk.Button(parent, text=text, style=style, command=cmd)
            b.pack(side=tk.LEFT, padx=2)

        add_btn(actions, "添加文件", self.on_add_wal_files)
        add_btn(actions, "添加目录", self.on_add_wal_dir)
        add_btn(actions, "清空", self.on_clear_wal)

        self._toolbar_sep(actions)

        add_btn(actions, "生成字典", self.on_build_dict)
        add_btn(actions, "导入字典", self.on_import_dict)
        add_btn(actions, "导出字典", self.on_export_dict)

        self._toolbar_sep(actions)

        add_btn(actions, "导出DO", lambda: self.on_export_sql("do"))
        add_btn(actions, "导出UNDO", lambda: self.on_export_sql("undo"))
        add_btn(actions, "导出Excel", self.on_export_excel, primary=True)

        tk.Frame(self, bg=COLORS["border"], height=1).pack(fill=tk.X, side=tk.TOP)

    def _toolbar_sep(self, parent: tk.Frame) -> None:
        sep = tk.Frame(parent, width=1, bg=COLORS["border"])
        sep.pack(side=tk.LEFT, padx=8, fill=tk.Y, pady=4)

    def _build_body(self) -> None:
        body = tk.Frame(self, bg=COLORS["bg"])
        body.pack(fill=tk.BOTH, expand=True)

        # Left sidebar
        side = tk.Frame(body, bg=COLORS["bg_panel"], width=300)
        side.pack(side=tk.LEFT, fill=tk.Y)
        side.pack_propagate(False)

        # sidebar header
        sh = tk.Frame(side, bg=COLORS["bg_panel"])
        sh.pack(fill=tk.X, padx=16, pady=(16, 8))
        tk.Label(sh, text="WORKSPACE", bg=COLORS["bg_panel"], fg=COLORS["text_dim"], font=("Microsoft YaHei UI", 8, "bold")).pack(anchor=tk.W)

        # WAL card
        wal_card = tk.Frame(side, bg=COLORS["bg_elevated"], highlightbackground=COLORS["border_soft"], highlightthickness=1)
        wal_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        wh = tk.Frame(wal_card, bg=COLORS["bg_elevated"])
        wh.pack(fill=tk.X, padx=10, pady=(10, 4))
        tk.Label(wh, text="WAL 文件", bg=COLORS["bg_elevated"], fg=COLORS["text"], font=FONT_UI, cursor="arrow").pack(side=tk.LEFT)
        self.wal_count = tk.Label(wh, text="0", bg=COLORS["bg_elevated"], fg=COLORS["accent"], font=FONT_UI_SM)
        self.wal_count.pack(side=tk.RIGHT)

        self.wal_list = tk.Listbox(
            wal_card,
            bg=COLORS["bg_input"],
            fg=COLORS["text"],
            selectbackground=COLORS["accent_soft"],
            selectforeground=COLORS["text"],
            highlightthickness=0,
            borderwidth=0,
            font=FONT_UI_XS,
            activestyle="none",
            exportselection=False,
        )
        self.wal_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 8))

        # filter card
        filt = tk.Frame(side, bg=COLORS["bg_elevated"], highlightbackground=COLORS["border_soft"], highlightthickness=1)
        filt.pack(fill=tk.X, padx=12, pady=6)

        tk.Label(filt, text="过滤条件", bg=COLORS["bg_elevated"], fg=COLORS["text_dim"], font=("Microsoft YaHei UI", 8, "bold")).pack(anchor=tk.W, padx=12, pady=(10, 6))

        row1 = tk.Frame(filt, bg=COLORS["bg_elevated"])
        row1.pack(fill=tk.X, padx=12, pady=2)
        tk.Label(row1, text="操作", bg=COLORS["bg_elevated"], fg=COLORS["text_muted"], font=FONT_UI_SM, width=4, anchor=tk.W).pack(side=tk.LEFT)
        self.op_var = tk.StringVar(value="")
        self.op_combo = ttk.Combobox(row1, textvariable=self.op_var, values=["全部", "INSERT", "UPDATE", "DELETE", "MULTI_INSERT"], state="readonly", width=16)
        self.op_combo.set("全部")
        self.op_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

        row2 = tk.Frame(filt, bg=COLORS["bg_elevated"])
        row2.pack(fill=tk.X, padx=12, pady=(4, 4))
        tk.Label(row2, text="表名", bg=COLORS["bg_elevated"], fg=COLORS["text_muted"], font=FONT_UI_SM, width=4, anchor=tk.W).pack(side=tk.LEFT)
        self.table_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.table_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

        btns = tk.Frame(filt, bg=COLORS["bg_elevated"])
        btns.pack(fill=tk.X, padx=12, pady=(6, 12))
        ttk.Button(btns, text="应用筛选", style="TButton", command=self.on_refresh_table).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        ttk.Button(btns, text="重置", style="Ghost.TButton", command=self._reset_filter).pack(side=tk.LEFT, expand=True, fill=tk.X)

        # Right content
        right = tk.Frame(body, bg=COLORS["bg"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # content header
        ch = tk.Frame(right, bg=COLORS["bg"])
        ch.pack(fill=tk.X, padx=16, pady=(14, 6))
        tk.Label(ch, text="解析结果", bg=COLORS["bg"], fg=COLORS["text"], font=FONT_TITLE).pack(side=tk.LEFT)
        self.row_count_label = tk.Label(ch, text="0 rows", bg=COLORS["bg"], fg=COLORS["text_dim"], font=FONT_UI_SM)
        self.row_count_label.pack(side=tk.RIGHT)

        # table card
        table_card = tk.Frame(right, bg=COLORS["bg_elevated"], highlightbackground=COLORS["border_soft"], highlightthickness=1)
        table_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        cols = ("id", "lsn", "xid", "commit_ts", "op", "table", "do_sql", "undo_sql")
        self.tree = ttk.Treeview(table_card, columns=cols, show="headings", selectmode="browse")
        headers = {
            "id": "ID",
            "lsn": "LSN",
            "xid": "XID",
            "commit_ts": "提交时间",
            "op": "操作",
            "table": "表",
            "do_sql": "DO SQL",
            "undo_sql": "UNDO SQL",
        }
        widths = {"id": 56, "lsn": 150, "xid": 80, "commit_ts": 150, "op": 90, "table": 180, "do_sql": 340, "undo_sql": 340}
        for c in cols:
            self.tree.heading(c, text=headers[c], anchor=tk.W)
            self.tree.column(c, width=widths[c], anchor=tk.W, stretch=(c in ("do_sql", "undo_sql", "table")))

        vsb = ttk.Scrollbar(table_card, orient=tk.VERTICAL, command=self.tree.yview, style="Vertical.TScrollbar")
        hsb = ttk.Scrollbar(table_card, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, pady=4, padx=(0, 4))
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.bind("<Double-1>", self.on_row_detail)
        self.tree.tag_configure("INSERT", foreground=COLORS["success"])
        self.tree.tag_configure("UPDATE", foreground=COLORS["warning"])
        self.tree.tag_configure("DELETE", foreground=COLORS["danger"])
        self.tree.tag_configure("MULTI_INSERT", foreground=COLORS["accent"])
        self.tree.tag_configure("odd", background=COLORS["row_alt"])

        # bottom log card
        log_card = tk.Frame(right, bg=COLORS["bg_elevated"], highlightbackground=COLORS["border_soft"], highlightthickness=1)
        log_card.pack(fill=tk.X, padx=12, pady=(0, 10))

        lh = tk.Frame(log_card, bg=COLORS["bg_elevated"])
        lh.pack(fill=tk.X, padx=10, pady=(8, 2))
        tk.Label(lh, text="活动日志", bg=COLORS["bg_elevated"], fg=COLORS["text_dim"], font=("Microsoft YaHei UI", 8, "bold")).pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(log_card, mode="indeterminate", length=120, style="Horizontal.TProgressbar")
        self.progress.pack(side=tk.RIGHT, padx=10)

        self.log = tk.Text(
            log_card,
            bg=COLORS["bg_input"],
            fg=COLORS["text_muted"],
            insertbackground=COLORS["text"],
            font=("Consolas", 9),
            height=7,
            wrap=tk.WORD,
            highlightthickness=0,
            borderwidth=0,
            padx=10,
            pady=8,
        )
        self.log.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.log.tag_configure("err", foreground=COLORS["danger"])
        self.log.tag_configure("ok", foreground=COLORS["success"])
        self.log.tag_configure("info", foreground=COLORS["text_muted"])
        self.log.tag_configure("dim", foreground=COLORS["text_dim"])

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=COLORS["toolbar_bg"], height=28)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        bar.pack_propagate(False)

        self.status_dot = tk.Canvas(bar, width=8, height=8, bg=COLORS["toolbar_bg"], highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(14, 6), pady=10)
        self._dot = self.status_dot.create_oval(1, 1, 7, 7, fill=COLORS["green_dot"], outline="")

        self.status = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.status, bg=COLORS["toolbar_bg"], fg=COLORS["text_muted"], font=FONT_UI_XS).pack(side=tk.LEFT)

        self.status_right = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.status_right, bg=COLORS["toolbar_bg"], fg=COLORS["text_dim"], font=FONT_UI_XS).pack(side=tk.RIGHT, padx=14)

    # ── Helpers ───────────────────────────────────────────────
    def _set_status(self, msg: str, kind: str = "info") -> None:
        self.status.set(msg)
        colors = {"info": COLORS["text_dim"], "ok": COLORS["success"], "err": COLORS["danger"], "warn": COLORS["warning"]}
        self.status_dot.itemconfigure(self._dot, fill=colors.get(kind, COLORS["text_dim"]))

    def _set_busy(self, busy: bool) -> None:
        self._parsing = busy
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def log_msg(self, msg: str, tag: str = "info") -> None:
        self.log.insert(tk.END, msg + "\n", tag)
        self.log.see(tk.END)
        self._set_status(msg[:80], "ok" if tag == "ok" else ("err" if tag == "err" else "info"))

    def _reset_filter(self) -> None:
        self.op_combo.set("全部")
        self.table_var.set("")
        self.on_refresh_table()

    def _update_stats(self) -> None:
        try:
            d = self.dict_store.load_dictionary()
            nrel = len(d.relations)
        except Exception:
            nrel = 0
        nwal = len(self.result_store.list_wal_files())
        nres = self.result_store.count()
        self.wal_count.configure(text=str(nwal))
        self.stat_label.configure(text=f"字典 {nrel} 表   ·   WAL {nwal}   ·   结果 {nres}")
        self.status_right.set(f"dict={nrel}  wal={nwal}  rows={nres}")
        self.row_count_label.configure(text=f"{nres} rows")

    def _refresh_status(self) -> None:
        try:
            d = self.dict_store.load_dictionary()
            self.log_msg(f"数据字典: {len(d.relations)} 张表 | 结果 {self.result_store.count()} 行 | WAL {len(self.wal_paths)}", "dim")
        except Exception as e:
            self.log_msg(f"状态: {e}", "err")
        self._refresh_wal_list()
        self.on_refresh_table()
        self._update_stats()

    def _refresh_wal_list(self) -> None:
        self.wal_list.delete(0, tk.END)
        for p in self.result_store.list_wal_files():
            name = Path(p).name
            self.wal_list.insert(tk.END, name)
            if Path(p) not in self.wal_paths:
                self.wal_paths.append(Path(p))
        self.wal_count.configure(text=str(len(self.wal_paths)))

    # ── Actions ───────────────────────────────────────────────
    def on_add_wal_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 WAL 文件（可多选）",
            filetypes=[
                ("WAL 段文件", "*.wal *.log *.partial"),
                ("WAL 段（无扩展名）", "*.*"),
                ("All", "*.*"),
            ],
        )
        if not paths:
            return
        for p in paths:
            self._add_wal_path(Path(p))

    def on_add_wal_dir(self) -> None:
        path = filedialog.askdirectory(title="选择 WAL 目录（如 $PGDATA/pg_wal）")
        if not path:
            return
        from ..core.xlog import collect_wal_files

        files = collect_wal_files(Path(path))
        if not files:
            self.log_msg(f"目录中未发现 WAL 文件: {path}", "warn")
            return
        for f in files:
            self._add_wal_path(f)
        self.log_msg(f"已从目录导入 {len(files)} 个文件", "ok")

    def _add_wal_path(self, path: Path) -> None:
        if path not in self.wal_paths:
            self.wal_paths.append(path)
        self.result_store.add_wal_file(str(path))
        self._refresh_wal_list()
        self.log_msg(f"已添加: {path.name}")

    def on_clear_wal(self) -> None:
        self.wal_paths.clear()
        for p in self.result_store.list_wal_files():
            self.result_store.remove_wal_file(p)
        self._refresh_wal_list()
        self.log_msg("WAL 列表已清空", "warn")

    def on_build_dict(self) -> None:
        dsn_win = tk.Toplevel(self)
        dsn_win.title("生成数据字典")
        dsn_win.geometry("620x280")
        dsn_win.configure(bg=COLORS["bg_panel"])
        dsn_win.transient(self)
        dsn_win.grab_set()

        wrap = tk.Frame(dsn_win, bg=COLORS["bg_panel"])
        wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=18)

        tk.Label(wrap, text="从 PostgreSQL 生成数据字典", bg=COLORS["bg_panel"], fg=COLORS["text"], font=FONT_TITLE).pack(anchor=tk.W, pady=(0, 4))
        tk.Label(wrap, text="连接源库读取 pg_class / pg_attribute 等目录信息", bg=COLORS["bg_panel"], fg=COLORS["text_muted"], font=FONT_UI_SM).pack(anchor=tk.W, pady=(0, 12))

        tk.Label(wrap, text="DSN", bg=COLORS["bg_panel"], fg=COLORS["text_muted"], font=FONT_UI_SM).pack(anchor=tk.W)
        dsn_var = tk.StringVar(value="postgresql://postgres:postgres@127.0.0.1:5432/postgres")
        ttk.Entry(wrap, textvariable=dsn_var).pack(fill=tk.X, pady=(2, 10))

        include_sys = tk.BooleanVar(value=False)
        ttk.Checkbutton(wrap, text="包含系统表（pg_catalog / information_schema，一般不需要）", variable=include_sys).pack(anchor=tk.W, pady=4)

        tk.Label(wrap, text="输出路径", bg=COLORS["bg_panel"], fg=COLORS["text_muted"], font=FONT_UI_SM).pack(anchor=tk.W, pady=(8, 0))
        of = tk.Frame(wrap, bg=COLORS["bg_panel"])
        of.pack(fill=tk.X, pady=2)
        out = tk.StringVar(value=str(self.dict_path))
        ttk.Entry(of, textvariable=out).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            of,
            text="浏览",
            style="Ghost.TButton",
            command=lambda: out.set(filedialog.asksaveasfilename(defaultextension=".sqlite", filetypes=[("SQLite", "*.sqlite")])),
        ).pack(side=tk.LEFT, padx=(6, 0))

        def run():
            try:
                d = build_dictionary_from_postgres(dsn_var.get(), include_system=include_sys.get())
                store = DictStore(Path(out.get()))
                store.save_dictionary(d)
                self.dict_path = Path(out.get())
                self.dict_store = DictStore(self.dict_path)
                self.after(0, lambda: self.log_msg(f"数据字典已生成: {len(d.relations)} 张表", "ok"))
                self.after(0, lambda: messagebox.showinfo("完成", f"已生成 {len(d.relations)} 张表的数据字典", parent=dsn_win))
            except Exception as e:
                self.after(0, lambda: self.log_msg(f"字典生成失败: {e}", "err"))
                self.after(0, lambda: messagebox.showerror("失败", str(e), parent=dsn_win))
            finally:
                self.after(0, dsn_win.destroy)

        btns = tk.Frame(wrap, bg=COLORS["bg_panel"])
        btns.pack(fill=tk.X, pady=(16, 0))
        ttk.Button(btns, text="取消", style="Ghost.TButton", command=dsn_win.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="生成字典", style="Primary.TButton", command=lambda: threading.Thread(target=run, daemon=True).start()).pack(side=tk.RIGHT)

    def on_import_dict(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("SQLite", "*.sqlite")])
        if not path:
            return
        try:
            store = DictStore(self.dict_path)
            if path.endswith(".json"):
                d = store.import_json(Path(path))
            else:
                other = DictStore(Path(path))
                d = other.load_dictionary()
                store.save_dictionary(d)
            self.dict_store = DictStore(self.dict_path)
            self.log_msg(f"字典导入成功: {len(d.relations)} 张表", "ok")
            self._update_stats()
        except Exception as e:
            messagebox.showerror("导入失败", str(e))
            self.log_msg(f"字典导入失败: {e}", "err")

    def on_export_dict(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="pgwinal_dict.json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            DictStore(self.dict_path).export_json(Path(path))
            self.log_msg(f"字典已导出: {path}", "ok")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def on_parse(self) -> None:
        if self._parsing:
            return
        if not self.wal_paths:
            messagebox.showwarning("提示", "请先添加 WAL 文件或目录")
            return
        try:
            d = self.dict_store.load_dictionary()
            if not d.relations:
                if not messagebox.askyesno(
                    "无数据字典",
                    "当前没有数据字典，将只能生成原始/占位 SQL，无法还原列值。\n是否仍继续解析？",
                ):
                    return
        except Exception as e:
            messagebox.showerror("字典错误", str(e))
            return

        engine = WalParseEngine(d, result_store=self.result_store)
        options = ParseOptions(only_committed=True, skip_catalog=True)
        self._set_busy(True)
        self.log.log_delete("1.0", tk.END)
        self.log_msg("开始解析 …", "info")

        def run():
            try:
                report = engine.parse_paths(
                    self.wal_paths,
                    options,
                    progress=lambda m: self.after(0, lambda: self.log_msg(m)),
                )
                def done():
                    self._set_busy(False)
                    self.log_msg(json.dumps({k: report[k] for k in report if k != "schema_report"}, ensure_ascii=False), "ok")
                    self.on_refresh_table()
                    self._update_stats()
                    messagebox.showinfo("解析完成", f"结果行数: {report['result_count']}")
                self.after(0, done)
            except Exception as e:
                def fail():
                    self._set_busy(False)
                    self.log_msg(f"解析失败: {e}", "err")
                    messagebox.showerror("解析失败", str(e))
                self.after(0, fail)

        threading.Thread(target=run, daemon=True).start()

    def on_refresh_table(self) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        op = self.op_var.get()
        if op in ("", "全部"):
            op = None
        try:
            rows = self.result_store.fetch(
                op=op,
                table_like=self.table_var.get() or None,
                limit=5000,
            )
        except Exception as e:
            self.log_msg(f"刷新失败: {e}", "err")
            return
        for idx, r in enumerate(rows):
            tags = [r["op"] or ""]
            if idx % 2 == 1:
                tags.append("odd")
            self.tree.insert(
                "",
                tk.END,
                tags=tuple(tags),
                values=(
                    r["id"],
                    r["lsn"],
                    r["xid"],
                    r["commit_ts"] or "",
                    r["op"],
                    f"{r['schema_name']}.{r['table_name']}",
                    (r["do_sql"] or "")[:180],
                    (r["undo_sql"] or "")[:180],
                ),
            )
        self.row_count_label.configure(text=f"显示 {len(rows)} / 总计 {self.result_store.count()}")
        self._set_status(f"显示 {len(rows)} 行", "info")

    def on_row_detail(self, _event) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        rid = int(vals[0])
        row = self.result_store.conn.execute("SELECT * FROM walminer_contents WHERE id=?", (rid,)).fetchone()
        if not row:
            return

        win = tk.Toplevel(self)
        win.title(f"记录详情  #{rid}   ·   {row['op']}")
        win.geometry("980x640")
        win.configure(bg=COLORS["bg"])
        win.transient(self)

        head = tk.Frame(win, bg=COLORS["bg_elevated"])
        head.pack(fill=tk.X)
        op = row["op"] or ""
        badge = tk.Label(
            head,
            text=f"  {op}  ",
            bg=OP_COLORS.get(op, COLORS["accent"]),
            fg="#fff",
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=6,
            pady=3,
        )
        badge.pack(side=tk.LEFT, padx=14, pady=12)
        tk.Label(
            head,
            text=f"{row['schema_name']}.{row['table_name']}   ·   LSN {row['lsn']}   ·   XID {row['xid']}",
            bg=COLORS["bg_elevated"],
            fg=COLORS["text"],
            font=FONT_UI,
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(head, text="关闭", style="Ghost.TButton", command=win.destroy).pack(side=tk.RIGHT, padx=12)

        body = tk.Frame(win, bg=COLORS["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        notebook_hosts = tk.Frame(body, bg=COLORS["bg"])
        notebook_hosts.pack(fill=tk.BOTH, expand=True)

        # simple tab-like panes
        panes = [
            ("DO SQL", row["do_sql"] or "--"),
            ("UNDO SQL", row["undo_sql"] or "--"),
            ("行数据", json.dumps(json.loads(row["row_data"] or "{}"), ensure_ascii=False, indent=2)),
            ("旧值", json.dumps(json.loads(row["old_row_data"] or "{}"), ensure_ascii=False, indent=2)),
            (
                "元信息",
                "\n".join(
                    [
                        f"ID            {row['id']}",
                        f"LSN           {row['lsn']}",
                        f"XID           {row['xid']}",
                        f"提交时间      {row['commit_ts']}",
                        f"操作          {row['op']}",
                        f"表            {row['schema_name']}.{row['table_name']}",
                        f"relfilenode   {row['relfilenode']}",
                        f"CTID          ({row['block_num']}, {row['offset_num']})",
                        f"NOTES         {row['notes']}",
                    ]
                ),
            ),
        ]

        # Use buttons to switch content
        left_tabs = tk.Frame(body, bg=COLORS["bg"], width=110)
        left_tabs.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        left_tabs.pack_propagate(False)

        text = tk.Text(
            body,
            bg=COLORS["bg_input"],
            fg=COLORS["text"],
            font=FONT_MONO,
            wrap=tk.NONE,
            highlightthickness=0,
            borderwidth=0,
            padx=12,
            pady=12,
        )
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def show(idx: int) -> None:
            text.delete("1.0", tk.END)
            text.insert(tk.END, panes[idx][1])

        for i, (label, _) in enumerate(panes):
            b = tk.Button(
                left_tabs,
                text=label,
                anchor=tk.W,
                relief=tk.FLAT,
                bg=COLORS["bg_elevated"],
                fg=COLORS["text_muted"],
                activebackground=COLORS["row_sel"],
                activeforeground=COLORS["text"],
                highlightthickness=1,
                highlightbackground=COLORS["border_soft"],
                font=FONT_UI_SM,
                padx=10,
                pady=8,
                cursor="hand2",
                command=lambda i=i: show(i),
            )
            b.pack(fill=tk.X, pady=2)
        show(0)

    def on_export_sql(self, kind: str) -> None:
        label = "DO" if kind == "do" else "UNDO"
        path = filedialog.asksaveasfilename(
            defaultextension=".sql",
            initialfile=f"recovery_{label}.sql",
            filetypes=[("SQL", "*.sql")],
        )
        if not path:
            return
        n = self.result_store.export_sql(Path(path), only_do=(kind == "do"), only_undo=(kind == "undo"))
        self.log_msg(f"已导出 {n} 条 {label} SQL → {Path(path).name}", "ok")
        messagebox.showinfo("完成", f"已导出 {n} 条 {label} 语句")

    def on_export_excel(self) -> None:
        if self.result_store.count() == 0:
            messagebox.showwarning("提示", "当前没有解析结果可导出")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            initialfile="pgwinal_results.xlsx",
            filetypes=[("Excel 工作簿", "*.xlsx")],
        )
        if not path:
            return
        op = self.op_var.get()
        if op in ("", "全部"):
            op = None
        table = self.table_var.get() or None
        try:
            n = self.result_store.export_excel(Path(path), op=op, table_like=table)
            self.log_msg(f"已导出 Excel: {n} 行 → {Path(path).name}", "ok")
            messagebox.showinfo("完成", f"已导出 {n} 行到 Excel")
        except Exception as e:
            self.log_msg(f"导出 Excel 失败: {e}", "err")
            messagebox.showerror("导出失败", str(e))


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
