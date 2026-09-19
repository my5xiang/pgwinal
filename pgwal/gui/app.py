"""pgwal GUI（PySide6）—— 界面实现参考 pgwinal（MiMo 风格浅色主题）。

布局与交互对齐旧项目 pgwinal/pgwal/gui/app.py：
  顶栏双行：品牌 + 统计 + 字典胶囊 + 主按钮「开始解析」
            添加文件/目录/清空 | 生成/切换/导入/导出字典 | 导出DO/UNDO/Excel
  左侧 300px 侧栏（卡片式）：当前字典卡片 / WAL 文件卡片 / 过滤条件卡片
  右侧：解析结果表（操作着色 + 隔行底色 + SQL 预览列）/ 活动日志 + 进度条
  底部状态栏：状态点 + 状态文本 | 右侧统计
  行为：动作 busy 锁、后台线程解析、双击详情、字典 JSON 往返
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

BASE_DIR = Path(__file__).resolve().parents[2]

# ── 浅色主题（对齐 pgwinal COLORS）────────────────────────────
COLORS = {
    "bg": "#FFFFFF", "bg_panel": "#F2F3F5", "bg_elevated": "#FFFFFF",
    "border": "#E5E6EB", "border_soft": "#EBEDF0",
    "text": "#1F2329", "text_muted": "#646A73", "text_dim": "#8F959E",
    "accent": "#3370FF", "accent_hover": "#245BDB", "accent_soft": "#E8F1FF",
    "success": "#2EA121", "warning": "#DE7802", "danger": "#F54A45",
    "row_alt": "#FAFBFC", "row_sel": "#E8F1FF", "header_bg": "#F7F8FA",
    "toolbar_bg": "#FFFFFF", "btn_hover": "#F2F3F5", "btn_active": "#EBEDF0",
}
OP_COLORS = {
    "INSERT": "#2EA121", "UPDATE": "#DE7802",
    "DELETE": "#F54A45", "MULTI_INSERT": "#3370FF", "ERROR": "#F54A45",
}
FONT = '"Microsoft YaHei UI"'
MONO = '"Consolas"'
PAGE_SIZE = 500

QSS = f"""
* {{ font-family: {FONT}, sans-serif; font-size: 10pt; color: {COLORS["text"]}; }}
QMainWindow, QDialog {{ background: {COLORS["bg"]}; }}
QFrame#toolbar {{ background: {COLORS["toolbar_bg"]}; border-bottom: 1px solid {COLORS["border"]}; }}
QFrame#sidebar {{ background: {COLORS["bg_panel"]}; }}
QFrame#sidebarGrip {{ background: {COLORS["bg_panel"]}; }}
QFrame#card {{ background: {COLORS["bg_elevated"]}; border: 1px solid {COLORS["border_soft"]}; border-radius: 6px; }}
QLabel#brand {{ font-size: 13pt; font-weight: bold; }}
QLabel#brandSub {{ color: {COLORS["text_dim"]}; font-size: 8pt; }}
QLabel#logo {{ background: {COLORS["accent_soft"]}; color: {COLORS["accent"]};
              border: 2px solid {COLORS["accent"]}; border-radius: 15px;
              font-size: 11pt; font-weight: bold; }}
QLabel#stat {{ color: {COLORS["text_muted"]}; font-size: 9pt; }}
QLabel#dictPill {{ background: {COLORS["accent_soft"]}; color: {COLORS["accent"]};
                  font-size: 9pt; padding: 3px 10px; border-radius: 4px; }}
QLabel#section {{ color: {COLORS["text_dim"]}; font-size: 8pt; font-weight: bold; }}
QLabel#dim {{ color: {COLORS["text_dim"]}; font-size: 8pt; }}
QLabel#muted {{ color: {COLORS["text_muted"]}; font-size: 9pt; }}
QLabel#title {{ font-size: 11pt; font-weight: bold; }}
QPushButton {{
    background: {COLORS["bg_elevated"]}; border: 1px solid {COLORS["border"]};
    border-radius: 4px; padding: 6px 12px;
}}
QPushButton:hover {{ background: {COLORS["btn_hover"]}; }}
QPushButton:pressed {{ background: {COLORS["btn_active"]}; }}
QPushButton:disabled {{ color: {COLORS["text_dim"]}; background: {COLORS["bg_panel"]}; }}
QPushButton#primary {{
    background: {COLORS["accent"]}; color: white; border: none;
    font-weight: bold; padding: 8px 18px; border-radius: 4px;
}}
QPushButton#primary:hover {{ background: {COLORS["accent_hover"]}; }}
QPushButton#primary:disabled {{ background: #A0BFFF; color: white; }}
QPushButton#ghost {{ border-color: {COLORS["border_soft"]}; color: {COLORS["text_muted"]}; }}
QLineEdit, QComboBox {{
    background: white; border: 1px solid {COLORS["border"]}; border-radius: 4px;
    padding: 5px 8px;
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {COLORS["accent"]}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: white; selection-background-color: {COLORS["row_sel"]};
                              selection-color: {COLORS["text"]}; }}
QListWidget {{
    background: white; border: none; font-size: 8pt; color: {COLORS["text"]};
    outline: none;
}}
QListWidget::item {{ padding: 4px 6px; border-radius: 3px; }}
QListWidget::item:selected {{ background: {COLORS["accent_soft"]}; color: {COLORS["text"]}; }}
QTableWidget {{
    background: white; border: none; gridline-color: {COLORS["border_soft"]};
    alternate-background-color: {COLORS["row_alt"]}; outline: none;
}}
QHeaderView::section {{
    background: {COLORS["header_bg"]}; color: {COLORS["text_muted"]};
    border: none; border-bottom: 1px solid {COLORS["border_soft"]};
    padding: 7px 8px; font-size: 9pt; font-weight: bold;
}}
QTableWidget::item:selected {{ background: {COLORS["row_sel"]}; }}
QPlainTextEdit {{
    background: white; border: none; color: {COLORS["text_muted"]};
    font-family: {MONO}; font-size: 9pt;
}}
QProgressBar {{ background: {COLORS["border_soft"]}; border: none; border-radius: 2px;
               max-height: 4px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {COLORS["accent"]}; border-radius: 2px; }}
QStatusBar {{ background: {COLORS["toolbar_bg"]}; border-top: 1px solid {COLORS["border_soft"]}; color: {COLORS["text_muted"]}; }}
QSplitter::handle {{ background: {COLORS["bg_panel"]}; }}
QCheckBox {{ color: {COLORS["text"]}; }}
"""


# ── 后台线程 ─────────────────────────────────────────────────
class ParseWorker(QThread):
    log = Signal(str)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, wal_files, dict_path, out_path, parent=None):
        super().__init__(parent)
        self.wal_files, self.dict_path, self.out_path = wal_files, dict_path, out_path

    def run(self):
        try:
            from ..dictstore import DataDictionary
            from ..engine import Engine
            from ..resultstore import ResultStore
            d = DataDictionary.load_sqlite(self.dict_path)
            result = ResultStore(self.out_path)
            eng = Engine(d, result, only_committed=True, progress=self.log.emit)
            stats = eng.parse(self.wal_files)
            result.commit()
            result.close()
            self.done.emit(stats)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class DictBuildWorker(QThread):
    log = Signal(str)
    done = Signal(str, int)     # (path, n_relations)
    failed = Signal(str)

    def __init__(self, dsn, out_path, parent=None):
        super().__init__(parent)
        self.dsn, self.out_path = dsn, out_path

    def run(self):
        try:
            from ..dictbuilder import build_dictionary
            self.log.emit("正在连接数据库并生成字典 …")
            build_dictionary(self.dsn, self.out_path)
            from ..dictstore import DataDictionary
            d = DataDictionary.load_sqlite(self.out_path)
            self.done.emit(self.out_path, d.relation_count)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


# ── 详情对话框 ─────────────────────────────────────────────────
class DetailDialog(QDialog):
    def __init__(self, row: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"变更详情 · lsn={row['start_lsn']}")
        self.resize(1000, 640)
        lay = QVBoxLayout(self)

        head = QLabel(
            f"op = {row['op']}    xid = {row['xid']}    "
            f"表 = {row['schema_name']}.{row['table_name']}\n"
            f"commit = {row['commit_ts'] or '—'}    undo来源 = {row['undo_source']}    "
            f"可执行 = {'是' if row['executable'] else '否'}\n"
            f"notes = {row['notes'] or '—'}")
        head.setStyleSheet(f"color: {COLORS['text_muted']};")
        head.setWordWrap(True)
        lay.addWidget(head)

        def fmt(js):
            if not js:
                return "（无）"
            try:
                d = json.loads(js)
                return "\n".join(f"  {k} = {v!r}" for k, v in d.items())
            except Exception:
                return js

        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(
            "===== 新值 =====\n" + fmt(row["row_data"]) +
            "\n\n===== 旧值 =====\n" + fmt(row["old_row_data"]) +
            "\n\n===== DO SQL =====\n" + (row["do_sql"] or "（无）") +
            "\n\n===== UNDO SQL =====\n" + (row["undo_sql"] or "（无）"))
        lay.addWidget(txt, 1)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        lay.addWidget(btn)


# ── 生成字典对话框（对齐旧项目 DSN 弹窗）──────────────────────
class BuildDictDialog(QDialog):
    def __init__(self, default_out: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("生成数据字典")
        self.resize(680, 420)
        self.worker = None
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("从 PostgreSQL 生成数据字典"))
        sub = QLabel("连接源库读取 pg_class / pg_attribute 等目录信息")
        sub.setStyleSheet(f"color: {COLORS['text_muted']};")
        lay.addWidget(sub)

        lay.addWidget(QLabel("DSN"))
        self.dsn = QLineEdit("postgresql://user:pass@host:port/dbname")
        lay.addWidget(self.dsn)
        tip = QLabel("示例: postgresql://user:pass@host:port/dbname · 密码中的 @ 请写成 %40")
        tip.setStyleSheet(f"color: {COLORS['text_dim']}; font-size: 8pt;")
        lay.addWidget(tip)

        lay.addWidget(QLabel("输出路径"))
        path_row = QHBoxLayout()
        self.out = QLineEdit(default_out)
        path_row.addWidget(self.out, 1)
        browse = QPushButton("浏览")
        browse.clicked.connect(self.on_browse)
        path_row.addWidget(browse)
        lay.addLayout(path_row)

        self.gen_btn = QPushButton("生成字典")
        self.gen_btn.setObjectName("primary")
        self.gen_btn.clicked.connect(self.on_gen)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(cancel)
        row.addWidget(self.gen_btn)
        lay.addLayout(row)

    def on_browse(self):
        f, _ = QFileDialog.getSaveFileName(self, "字典输出", self.out.text(),
                                            "SQLite (*.sqlite)")
        if f:
            self.out.setText(f)

    def on_gen(self):
        if self.worker and self.worker.isRunning():
            return
        self.gen_btn.setEnabled(False)
        self.gen_btn.setText("生成中…")
        self.worker = DictBuildWorker(self.dsn.text(), self.out.text())
        self.worker.done.connect(lambda p, n: (self.accept(),))
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def on_fail(self, msg):
        self.gen_btn.setEnabled(True)
        self.gen_btn.setText("生成字典")
        QMessageBox.critical(self, "生成字典失败", msg)


# ── 主窗口 ─────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pgwal · PostgreSQL WAL Recovery")
        self.resize(1440, 900)
        self.setMinimumSize(1280, 760)
        self.setStyleSheet(QSS)

        self.dict_path = BASE_DIR / "dict" / "pgwal_dict.sqlite"
        self.result_path = BASE_DIR / "result" / "pgwal_results.sqlite"
        self.wal_paths: list[Path] = []
        self.worker = None
        self.dict_worker = None
        self._page = 0
        self._buttons: list[QPushButton] = []

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._log("新会话已就绪 | 双击结果行可查看 DO/UNDO 详情", "dim")
        self._auto_load_dict()

    def _auto_load_dict(self):
        """启动时自动加载字典：默认路径 → dict/ 下最新的 sqlite。"""
        default = BASE_DIR / "dict" / "pgwal_dict.sqlite"
        candidates = []
        if default.exists():
            candidates.append(default)
        dict_dir = BASE_DIR / "dict"
        if dict_dir.is_dir():
            candidates += sorted(
                (p for p in dict_dir.glob("*.sqlite") if p != default),
                key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            self.dict_path = candidates[0]
            d = self._load_dict_info()
            if d is not None:
                self._log(f"已自动加载字典: {candidates[0].name}（{d.relation_count} 表）", "ok")
                self._update_stats()

    # ── 顶栏 ──────────────────────────────────────────────────
    def _build_toolbar(self):
        tb = QFrame()
        tb.setObjectName("toolbar")
        self.addToolBar = None
        tb_l = QVBoxLayout(tb)
        tb_l.setContentsMargins(12, 8, 12, 6)
        tb_l.setSpacing(4)

        # Row 1
        r1 = QHBoxLayout()
        logo = QLabel("W")
        logo.setObjectName("logo")
        logo.setFixedSize(32, 32)
        logo.setAlignment(Qt.AlignCenter)
        r1.addWidget(logo)
        brandbox = QVBoxLayout()
        brandbox.setSpacing(0)
        brand = QLabel("pgwal")
        brand.setObjectName("brand")
        sub = QLabel("WAL · DO / UNDO SQL")
        sub.setObjectName("brandSub")
        brandbox.addWidget(brand)
        brandbox.addWidget(sub)
        r1.addLayout(brandbox)
        r1.addSpacing(16)
        self.stat_label = QLabel("")
        self.stat_label.setObjectName("stat")
        r1.addWidget(self.stat_label)
        r1.addStretch(1)
        self.dict_pill = QLabel("字典: —")
        self.dict_pill.setObjectName("dictPill")
        r1.addWidget(self.dict_pill)
        r1.addSpacing(8)
        self.btn_parse = QPushButton("开始解析")
        self.btn_parse.setObjectName("primary")
        self.btn_parse.clicked.connect(self.on_parse)
        r1.addWidget(self.btn_parse)
        tb_l.addLayout(r1)

        # Row 2
        r2 = QHBoxLayout()
        r2.setSpacing(4)

        def add_btn(text, cb, ghost=True):
            b = QPushButton(text)
            if ghost:
                b.setObjectName("ghost")
            b.clicked.connect(cb)
            r2.addWidget(b)
            self._buttons.append(b)
            return b

        def add_sep():
            sep = QFrame()
            sep.setFixedSize(1, 22)
            sep.setStyleSheet(f"background: {COLORS['border']};")
            r2.addWidget(sep)
            r2.addSpacing(4)

        add_btn("添加文件", self.on_add_files)
        add_btn("添加目录", self.on_add_dir)
        add_btn("清空", self.on_clear_wal)
        add_sep()
        add_btn("生成字典", self.on_build_dict)
        add_btn("切换字典", self.on_switch_dict)
        add_btn("导入字典", self.on_import_dict)
        add_btn("导出字典", self.on_export_dict)
        add_sep()
        add_btn("导出DO", lambda: self.on_export_sql("do"))
        add_btn("导出UNDO", lambda: self.on_export_sql("undo"))
        add_btn("导出Excel", self.on_export_excel, ghost=False)
        r2.addStretch(1)
        tb_l.addLayout(r2)

        # 顶部工具栏区（不可移动）
        wrap = QWidget()
        wrap.setLayout(QVBoxLayout())
        wrap.layout().setContentsMargins(0, 0, 0, 0)
        wrap.layout().addWidget(tb)
        self.setMenuWidget(wrap)

    # ── 主体 ──────────────────────────────────────────────────
    def _build_body(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        root.addWidget(splitter, 1)

        # ---- 左侧 300px 侧栏 ----
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(300)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(12, 14, 12, 12)
        sv.setSpacing(8)

        ws = QLabel("WORKSPACE")
        ws.setObjectName("section")
        sv.addWidget(ws)

        # 当前字典卡片
        dict_card = QFrame()
        dict_card.setObjectName("card")
        dv = QVBoxLayout(dict_card)
        dv.setContentsMargins(10, 10, 10, 10)
        dv.setSpacing(2)
        dh = QLabel("当前字典")
        dv.addWidget(dh)
        self.dict_path_label = QLabel("—")
        self.dict_path_label.setObjectName("dictPill")
        self.dict_path_label.setWordWrap(True)
        dv.addWidget(self.dict_path_label)
        self.dict_meta_label = QLabel("—")
        self.dict_meta_label.setObjectName("dim")
        self.dict_meta_label.setWordWrap(True)
        dv.addWidget(self.dict_meta_label)
        sv.addWidget(dict_card)

        # WAL 卡片
        wal_card = QFrame()
        wal_card.setObjectName("card")
        wv = QVBoxLayout(wal_card)
        wv.setContentsMargins(10, 10, 10, 10)
        wv.setSpacing(4)
        wh = QHBoxLayout()
        wh.addWidget(QLabel("WAL 文件"))
        self.wal_count = QLabel("0")
        self.wal_count.setStyleSheet(f"color: {COLORS['accent']};")
        wh.addStretch(1)
        wh.addWidget(self.wal_count)
        wv.addLayout(wh)
        self.wal_list = QListWidget()
        wv.addWidget(self.wal_list, 1)
        sv.addWidget(wal_card, 1)

        # 过滤卡片
        filt = QFrame()
        filt.setObjectName("card")
        fv = QVBoxLayout(filt)
        fv.setContentsMargins(12, 10, 12, 12)
        fv.setSpacing(6)
        fs = QLabel("过滤条件")
        fs.setObjectName("section")
        fv.addWidget(fs)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("操作"))
        self.op_combo = QComboBox()
        self.op_combo.addItems(["全部", "INSERT", "UPDATE", "DELETE", "MULTI_INSERT"])
        r1.addWidget(self.op_combo, 1)
        fv.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("表名"))
        self.table_edit = QLineEdit()
        r2.addWidget(self.table_edit, 1)
        fv.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("可执行"))
        self.exec_combo = QComboBox()
        self.exec_combo.addItems(["全部", "是", "否"])
        r3.addWidget(self.exec_combo, 1)
        fv.addLayout(r3)
        btns = QHBoxLayout()
        b_apply = QPushButton("应用筛选")
        b_apply.clicked.connect(self.refresh_results)
        btns.addWidget(b_apply, 1)
        b_reset = QPushButton("重置")
        b_reset.setObjectName("ghost")
        b_reset.clicked.connect(self.reset_filter)
        btns.addWidget(b_reset, 1)
        fv.addLayout(btns)
        # 翻页
        nav = QHBoxLayout()
        self.btn_prev = QPushButton("◀ 上一页")
        self.btn_prev.setObjectName("ghost")
        self.btn_prev.clicked.connect(lambda: self.turn_page(-1))
        self.btn_next = QPushButton("下一页 ▶")
        self.btn_next.setObjectName("ghost")
        self.btn_next.clicked.connect(lambda: self.turn_page(1))
        nav.addWidget(self.btn_prev, 1)
        nav.addWidget(self.btn_next, 1)
        fv.addLayout(nav)
        sv.addWidget(filt)

        splitter.addWidget(side)

        # ---- 右侧 ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(12, 12, 12, 10)
        rv.setSpacing(6)

        ch = QHBoxLayout()
        t = QLabel("解析结果")
        t.setObjectName("title")
        ch.addWidget(t)
        ch.addStretch(1)
        self.row_count_label = QLabel("0 rows")
        self.row_count_label.setObjectName("muted")
        ch.addWidget(self.row_count_label)
        rv.addLayout(ch)

        table_card = QFrame()
        table_card.setObjectName("card")
        tv = QVBoxLayout(table_card)
        tv.setContentsMargins(4, 4, 4, 4)
        cols = ["ID", "LSN", "XID", "提交时间", "操作", "表", "DO SQL", "UNDO SQL"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for i in range(1, 6):
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        for i in (6, 7):
            hdr.setSectionResizeMode(i, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(5, 170)
        self.table.doubleClicked.connect(self.on_detail)
        tv.addWidget(self.table)
        rv.addWidget(table_card, 1)

        # 日志卡片
        log_card = QFrame()
        log_card.setObjectName("card")
        lv = QVBoxLayout(log_card)
        lv.setContentsMargins(10, 8, 10, 10)
        lv.setSpacing(4)
        lh = QHBoxLayout()
        ls = QLabel("活动日志")
        ls.setObjectName("section")
        lh.addWidget(ls)
        lh.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setFixedWidth(120)
        lh.addWidget(self.progress)
        lv.addLayout(lh)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        lv.addWidget(self.log)
        rv.addWidget(log_card)

        splitter.addWidget(right)
        splitter.setSizes([300, 1120])
        splitter.setCollapsible(1, False)

    # ── 状态栏 ────────────────────────────────────────────────
    def _build_statusbar(self):
        sb = self.statusBar()
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(6)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {COLORS['success']}; font-size: 8pt;")
        lay.addWidget(self.status_dot)
        self.status_label = QLabel("就绪")
        lay.addWidget(self.status_label)
        lay.addStretch(1)
        self.status_right = QLabel("")
        self.status_right.setObjectName("dim")
        lay.addWidget(self.status_right)
        sb.addWidget(w, 1)

    # ── 通用 ──────────────────────────────────────────────────
    def _log(self, msg: str, kind: str = "info"):
        color = {"ok": COLORS["success"], "err": COLORS["danger"],
                 "warn": COLORS["warning"], "dim": COLORS["text_dim"],
                 "info": COLORS["text_muted"]}.get(kind, COLORS["text_muted"])
        self.log.appendHtml(
            f'<span style="color:{color}">{msg.replace("<", "&lt;")}</span>')
        sb = self.statusBar()
        self.status_label.setText(msg[:80])
        self.status_dot.setStyleSheet(
            f"color: {COLORS['success'] if kind == 'ok' else COLORS['warning'] if kind in ('warn',) else COLORS['danger'] if kind == 'err' else COLORS['text_dim']}; font-size: 8pt;")

    def _set_busy(self, busy: bool, label: str = ""):
        for b in self._buttons + [self.btn_parse]:
            b.setEnabled(not busy)
        self.progress.setRange(0, 0) if busy else self.progress.setRange(0, 1)
        if busy:
            self._log(f"▶ 开始: {label}", "info")

    # ── WAL 管理 ──────────────────────────────────────────────
    def on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 WAL 文件（可多选）", "",
            "WAL 段文件 (*.wal *.log *.partial);;WAL 段（无扩展名）(*.*);;All (*.*)")
        for f in files:
            p = Path(f)
            if p not in self.wal_paths:
                self.wal_paths.append(p)
        self._refresh_wal_list()
        if files:
            self._log(f"已添加 {len(files)} 个文件", "ok")

    def on_add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择 WAL 目录（如 $PGDATA/pg_wal）")
        if not d:
            return
        from ..xlogreader import collect_wal_files
        files = collect_wal_files(Path(d))
        if not files:
            self._log(f"目录中未发现 WAL 文件: {d}", "warn")
            return
        for f in files:
            if f not in self.wal_paths:
                self.wal_paths.append(f)
        self._refresh_wal_list()
        self._log(f"已从目录导入 {len(files)} 个文件", "ok")

    def on_clear_wal(self):
        self.wal_paths.clear()
        self._refresh_wal_list()
        self._log("WAL 列表已清空", "warn")

    def _refresh_wal_list(self):
        self.wal_list.clear()
        for p in sorted(self.wal_paths):
            self.wal_list.addItem(p.name)
        self.wal_count.setText(str(len(self.wal_paths)))
        self._update_stats()

    # ── 字典 ──────────────────────────────────────────────────
    def _load_dict_info(self):
        try:
            from ..dictstore import DataDictionary
            d = DataDictionary.load_sqlite(self.dict_path)
            self.dict_pill.setText(
                f"字典: {Path(self.dict_path).name} · {d.relation_count} 表"
                + (f" · PG {d.pg_version}" if d.pg_version else ""))
            self.dict_path_label.setText(str(self.dict_path))
            meta = f"{d.relation_count} 张表"
            if d.pg_version:
                meta += f"\nPostgreSQL {d.pg_version}"
            if d.meta.get("created_at"):
                meta += f"\n创建: {d.meta['created_at']}"
            self.dict_meta_label.setText(meta)
            # 主键覆盖提示（决定 DELETE/UPDATE 是否精简）
            n_pk = sum(1 for r in d._by_filenode.values() if r.pk_attnums)
            if n_pk == 0:
                self._log("警告: 该字典无主键信息——DELETE/UNDO 将使用全字段匹配（冗长）。"
                          "建议用「生成字典」重新生成（自动采集主键）", "warn")
            else:
                meta += f"\n主键: {n_pk} 表（DELETE 按主键 / UPDATE 仅变更列）"
                self.dict_meta_label.setText(meta)
            return d
        except Exception:
            self.dict_pill.setText("字典: —（未加载）")
            self.dict_path_label.setText(str(self.dict_path))
            self.dict_meta_label.setText("—")
            return None

    def on_build_dict(self):
        dlg = BuildDictDialog(str(self.dict_path), self)
        if dlg.exec() != QDialog.Accepted:
            return
        # 生成成功（worker 已完成）
        self.dict_path = Path(dlg.out.text())
        self._load_dict_info()
        self._update_stats()
        self._log(f"数据字典已生成并切换: {self.dict_path.name}", "ok")

    def on_switch_dict(self):
        init = str(BASE_DIR / "dict")
        f, _ = QFileDialog.getOpenFileName(
            self, "选择数据字典（SQLite）",
            init if Path(init).is_dir() else str(Path(self.dict_path).parent),
            "SQLite 字典 (*.sqlite);;All (*.*)")
        if not f:
            return
        self.dict_path = Path(f)
        d = self._load_dict_info()
        self._update_stats()
        if d:
            self._log(f"已切换字典 → {self.dict_path.name} · {d.relation_count} 表", "ok")

    def on_import_dict(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "导入字典", str(BASE_DIR / "dict"), "JSON (*.json);;SQLite (*.sqlite)")
        if not f:
            return
        if f.endswith(".json"):
            try:
                self._import_dict_json(Path(f))
            except Exception as e:
                QMessageBox.critical(self, "导入失败", str(e))
        else:
            self.dict_path = Path(f)
            self._load_dict_info()
            self._log(f"已切换字典 → {Path(f).name}", "ok")
        self._update_stats()

    def _import_dict_json(self, js: Path):
        """JSON 字典 → 当前 sqlite 字典（对齐旧项目导入导出）。"""
        data = json.loads(js.read_text(encoding="utf-8"))
        out = self.dict_path
        out.parent.mkdir(parents=True, exist_ok=True)
        s = sqlite3.connect(out)
        s.executescript(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS relations (rel_oid INTEGER, schema_name TEXT,"
            " rel_name TEXT, relfilenode INTEGER, reltablespace INTEGER, db_oid INTEGER,"
            " relkind TEXT, pk_attnums TEXT);"
            "CREATE TABLE IF NOT EXISTS attributes (rel_oid INTEGER, attnum INTEGER,"
            " attname TEXT, type_oid INTEGER, type_name TEXT, typmod INTEGER,"
            " attnotnull INTEGER, is_dropped INTEGER, attndims INTEGER, collation INTEGER);")
        s.execute("DELETE FROM relations")
        s.execute("DELETE FROM attributes")
        s.execute("DELETE FROM meta")
        for k, v in data.get("meta", {}).items():
            s.execute("INSERT INTO meta VALUES (?,?)", (k, str(v)))
        s.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
                      [tuple(r) for r in data.get("relations", [])])
        s.executemany("INSERT INTO attributes VALUES (?,?,?,?,?,?,?,?,?,?)",
                      [tuple(a) for a in data.get("attributes", [])])
        s.commit()
        s.close()
        self._log(f"字典 JSON 已导入: {js.name}", "ok")

    def on_export_dict(self):
        f, _ = QFileDialog.getSaveFileName(
            self, "导出字典", "pgwal_dict.json", "JSON (*.json)")
        if not f:
            return
        try:
            conn = sqlite3.connect(f"file:{Path(self.dict_path).as_posix()}?mode=ro", uri=True)
            data = {
                "meta": {k: v for k, v in conn.execute("SELECT key, value FROM meta")},
                "relations": [list(r) for r in conn.execute("SELECT * FROM relations")],
                "attributes": [list(a) for a in conn.execute("SELECT * FROM attributes")],
            }
            conn.close()
            Path(f).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self._log(f"字典已导出: {f}", "ok")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ── 解析 ──────────────────────────────────────────────────
    def on_parse(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.wal_paths:
            QMessageBox.warning(self, "提示", "请先添加 WAL 文件或目录")
            return
        self.wal_paths = [p for p in self.wal_paths if p.exists()]
        if not self.wal_paths:
            QMessageBox.warning(self, "提示", "WAL 路径均不存在")
            return
        try:
            from ..dictstore import DataDictionary
            d = DataDictionary.load_sqlite(self.dict_path)
            if d.relation_count == 0:
                if not QMessageBox.question(
                        self, "无数据字典",
                        f"当前字典: {self.dict_path}\n没有表结构，解析将无法映射表名。\n仍继续？"):
                    return
        except Exception as e:
            QMessageBox.critical(self, "字典错误", str(e))
            return

        self._set_busy(True, "解析")
        self.log.clear()
        self._log(f"开始解析 · 字典 {Path(self.dict_path).name} · {len(self.wal_paths)} 个 WAL 段", "info")
        self.result_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker = ParseWorker([str(p) for p in self.wal_paths],
                                  str(self.dict_path), str(self.result_path))
        self.worker.log.connect(lambda m: self._log(m))
        self.worker.done.connect(self.on_parse_done)
        self.worker.failed.connect(self.on_parse_fail)
        self.worker.start()

    def on_parse_done(self, stats):
        self._set_busy(False)
        n = stats.get("changes", 0)
        self._log(
            f"解析成功 · 结果 {n} 条 · 提交 {stats.get('commits', 0)} · "
            f"耗时 {stats.get('elapsed', '?')}s", "ok")
        self._page = 0
        self.refresh_results()
        self._update_stats()
        self.status_label.setText(f"解析成功 · {n} 条")

    def on_parse_fail(self, msg):
        self._set_busy(False)
        self._log(f"解析失败: {msg}", "err")
        QMessageBox.critical(self, "解析失败", msg)

    # ── 结果表 ────────────────────────────────────────────────
    def _where(self):
        conds, args = ["1=1"], []
        if self.op_combo.currentText() != "全部":
            conds.append("op=?")
            args.append(self.op_combo.currentText())
        t = self.table_edit.text().strip()
        if t:
            conds.append("(table_name LIKE ? OR schema_name LIKE ?)")
            args += [f"%{t}%", f"%{t}%"]
        ex = self.exec_combo.currentText()
        if ex == "是":
            conds.append("executable=1")
        elif ex == "否":
            conds.append("executable=0")
        return " AND ".join(conds), args

    def refresh_results(self):
        self._page = 0
        self._load_page()

    def reset_filter(self):
        self.op_combo.setCurrentIndex(0)
        self.table_edit.clear()
        self.exec_combo.setCurrentIndex(0)
        self.refresh_results()

    def turn_page(self, d):
        self._page = max(0, self._page + d)
        self._load_page()

    def _load_page(self):
        if not Path(self.result_path).exists():
            self.row_count_label.setText("0 rows")
            return
        try:
            conn = sqlite3.connect(f"file:{Path(self.result_path).as_posix()}?mode=ro", uri=True)
        except Exception:
            return
        where, args = self._where()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM walminer_contents WHERE {where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT id,start_lsn,xid,commit_ts,op,schema_name,table_name,"
                f"do_sql,undo_sql FROM walminer_contents WHERE {where} "
                f"ORDER BY id LIMIT {PAGE_SIZE} OFFSET {self._page * PAGE_SIZE}", args).fetchall()
        except Exception as e:
            conn.close()
            self._log(f"刷新失败: {e}", "err")
            return
        conn.close()

        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            items = [str(r[0]), r[1], str(r[2]), r[3] or "", r[4],
                     f"{r[5]}.{r[6]}" if r[5] else "",
                     (r[7] or "")[:180], (r[8] or "")[:180]]
            for j, v in enumerate(items):
                it = QTableWidgetItem(v)
                if j == 4 and v in OP_COLORS:
                    it.setForeground(QColor(OP_COLORS[v]))
                if j >= 6:
                    it.setToolTip(r[7] if j == 6 else r[8])
                self.table.setItem(i, j, it)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.row_count_label.setText(f"显示 {len(rows)} / 总计 {total} · 第 {self._page + 1}/{pages} 页")

    def on_detail(self, index):
        item = self.table.item(index.row(), 0)
        if item is None:
            return
        rid = item.text()
        try:
            conn = sqlite3.connect(f"file:{Path(self.result_path).as_posix()}?mode=ro", uri=True)
        except Exception:
            return
        cur = conn.execute(
            "SELECT sqlno,xid,op,commit_ts,start_lsn,schema_name,table_name,"
            "undo_source,executable,row_data,old_row_data,do_sql,undo_sql,notes "
            "FROM walminer_contents WHERE id=?", (rid,))
        r = cur.fetchone()
        conn.close()
        if r:
            cols = ("sqlno xid op commit_ts start_lsn schema_name table_name "
                    "undo_source executable row_data old_row_data do_sql undo_sql notes").split()
            DetailDialog(dict(zip(cols, r)), self).exec()

    def _update_stats(self):
        nwal = len(self.wal_paths)
        try:
            from ..dictstore import DataDictionary
            nrel = DataDictionary.load_sqlite(self.dict_path).relation_count
        except Exception:
            nrel = 0
        nres = 0
        if Path(self.result_path).exists():
            try:
                conn = sqlite3.connect(f"file:{Path(self.result_path).as_posix()}?mode=ro", uri=True)
                nres = conn.execute("SELECT COUNT(*) FROM walminer_contents").fetchone()[0]
                conn.close()
            except Exception:
                pass
        self.stat_label.setText(f"字典 {nrel} 表 · WAL {nwal} · 结果 {nres}")
        self.status_right.setText(
            f"字典={Path(self.dict_path).name}  表={nrel}  wal={nwal}  rows={nres}")

    # ── 导出 ──────────────────────────────────────────────────
    def on_export_sql(self, mode):
        if not Path(self.result_path).exists():
            QMessageBox.information(self, "提示", "尚无解析结果")
            return
        default = f"{'do' if mode == 'do' else 'undo'}_{Path(self.result_path).stem}.sql"
        f, _ = QFileDialog.getSaveFileName(self, f"导出 {mode.upper()} SQL", default,
                                           "SQL (*.sql)")
        if not f:
            return
        try:
            from ..resultstore import ResultStore
            store = ResultStore(str(self.result_path))
            n = store.export_sql(f, mode)
            store.close()
            self._log(f"{mode.upper()} SQL 导出: {f}（{n} 条）", "ok")
            QMessageBox.information(self, "导出完成", f"{mode.upper()} SQL {n} 条\n{f}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def on_export_excel(self):
        if not Path(self.result_path).exists():
            QMessageBox.information(self, "提示", "尚无解析结果")
            return
        f, _ = QFileDialog.getSaveFileName(self, "导出 Excel", "pgwal_results.xlsx",
                                           "Excel (*.xlsx)")
        if not f:
            return
        try:
            n = self._export_xlsx(f)
            self._log(f"Excel 导出: {f}（{n} 行）", "ok")
            QMessageBox.information(self, "导出完成", f"已导出 {n} 行\n{f}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _export_xlsx(self, path) -> int:
        """标准 OOXML xlsx 导出（pgwal.xlsx 模块）。"""
        from ..xlsx import write_xlsx
        conn = sqlite3.connect(f"file:{Path(self.result_path).as_posix()}?mode=ro", uri=True)

        def rows():
            for r in conn.execute(
                    "SELECT sqlno,op,commit_ts,start_lsn,xid,schema_name,table_name,"
                    "undo_source,executable,do_sql,undo_sql FROM walminer_contents "
                    "ORDER BY id LIMIT 200000"):
                yield r

        headers = ["sqlno", "op", "commit_ts", "lsn", "xid", "schema", "table",
                   "undo_source", "executable", "do_sql", "undo_sql"]
        try:
            return write_xlsx(path, headers, rows())
        finally:
            conn.close()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
