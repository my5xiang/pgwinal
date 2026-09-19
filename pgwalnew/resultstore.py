"""结果存储：SQLite（对齐 walminer_contents 并增强）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .decode import Change
from .sqlgen import gen_do_sql, gen_undo_sql
from .heaptuple import is_clean
from .typereg import ExternalValue, RawValue

_SCHEMA = """
CREATE TABLE IF NOT EXISTS walminer_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sqlno INTEGER,
    xid INTEGER,
    topxid INTEGER,
    op TEXT,
    commit_ts TEXT,
    start_lsn TEXT,
    schema_name TEXT,
    table_name TEXT,
    relfilenode INTEGER,
    block_num INTEGER,
    offset_num INTEGER,
    row_data TEXT,
    old_row_data TEXT,
    do_sql TEXT,
    undo_sql TEXT,
    undo_source TEXT,
    executable INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_wc_xid ON walminer_contents(xid);
CREATE INDEX IF NOT EXISTS idx_wc_op ON walminer_contents(op);
CREATE INDEX IF NOT EXISTS idx_wc_table ON walminer_contents(schema_name, table_name);
CREATE TABLE IF NOT EXISTS parse_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, wal_files TEXT, stats TEXT
);
"""


def _json_default(o):
    if isinstance(o, (RawValue, ExternalValue)):
        return {"__raw__": True, "kind": o.kind if hasattr(o, "kind") else "ext",
                "reason": getattr(o, "reason", "")}
    return str(o)


class ResultStore:
    def __init__(self, path=":memory:"):
        self.path = str(path)
        fresh = not Path(self.path).exists() if self.path != ":memory:" else True
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_SCHEMA)
        self.conn.execute("DELETE FROM walminer_contents")
        self._seq = 0

    def insert_changes(self, changes: list, commit_ts=None, topxid=0):
        rows = []
        for ch in changes:
            self._seq += 1
            do_sql = gen_do_sql(ch)
            undo_sql = gen_undo_sql(ch)
            executable = 1 if (ch.executable and do_sql) else 0
            rows.append((
                self._seq, ch.xid, topxid or ch.topxid, ch.op,
                commit_ts.isoformat(sep=" ") if commit_ts else None,
                f"{ch.lsn >> 32:X}/{ch.lsn & 0xFFFFFFFF:X}",
                ch.rel.schema_name if ch.rel else None,
                ch.rel.rel_name if ch.rel else None,
                ch.relfilenode, ch.block, ch.offset,
                json.dumps(ch.new_values, ensure_ascii=False, default=_json_default)
                if ch.new_values else None,
                json.dumps(ch.old_values, ensure_ascii=False, default=_json_default)
                if ch.old_values else None,
                do_sql, undo_sql, ch.undo_source, executable,
                "; ".join(ch.notes) if ch.notes else None,
            ))
        self.conn.executemany(
            "INSERT INTO walminer_contents (sqlno,xid,topxid,op,commit_ts,start_lsn,"
            "schema_name,table_name,relfilenode,block_num,offset_num,row_data,"
            "old_row_data,do_sql,undo_sql,undo_source,executable,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def commit(self):
        self.conn.commit()

    def count(self, where="1=1") -> int:
        return self.conn.execute(
            f"SELECT COUNT(*) FROM walminer_contents WHERE {where}").fetchone()[0]

    def summary(self) -> dict:
        out = {}
        for row in self.conn.execute(
                "SELECT op, COUNT(*), SUM(executable) FROM walminer_contents GROUP BY op"):
            out[row[0]] = {"count": row[1], "executable": row[2] or 0}
        return out

    def export_sql(self, path, mode="do", executable_only=True) -> int:
        """导出 DO（LSN 升序）或 UNDO（LSN 降序）。"""
        order = "ASC" if mode == "do" else "DESC"
        cond = " AND executable=1" if executable_only else ""
        rows = self.conn.execute(
            f"SELECT start_lsn, xid, sqlno, do_sql, undo_sql, op, schema_name, table_name "
            f"FROM walminer_contents WHERE (do_sql IS NOT NULL OR undo_sql IS NOT NULL)"
            f"{cond} ORDER BY id {order}").fetchall()
        col = 3 if mode == "do" else 4
        n = 0
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"-- pgwalnew {mode.upper()} SQL\n")
            f.write(f"-- 共 {len(rows)} 条{'（仅可执行）' if executable_only else ''}\n\n")
            cur_xid = None
            for r in rows:
                sql = r[col]
                if not sql:
                    continue
                if mode == "undo" and r[5] == "INSERT" and r[col] and "不可执行" in r[col]:
                    continue
                if r[1] != cur_xid:
                    if cur_xid is not None:
                        f.write("COMMIT;\n\n")
                    f.write(f"-- xid={r[1]} lsn={r[0]} {r[6]}.{r[7]} {r[5]}\n")
                    f.write("BEGIN;\n")
                    cur_xid = r[1]
                f.write(sql + "\n")
                n += 1
            if cur_xid is not None:
                f.write("COMMIT;\n")
        return n

    def close(self):
        self.conn.close()
