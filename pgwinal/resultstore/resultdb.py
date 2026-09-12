from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from ..core.models import ChangeRecord


class ResultStore:
    """Persist parse results into tables (SQLite), similar to walminer_contents."""

    def __init__(self, path: Path | str = "result/pgwinal_results.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        self.conn.close()

    def clear_contents(self) -> None:
        self.conn.execute("DELETE FROM walminer_contents")
        self.conn.execute("DELETE FROM parse_meta")
        self.conn.commit()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS parse_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS wal_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS walminer_contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lsn TEXT,
                start_lsn INTEGER,
                end_lsn INTEGER,
                xid INTEGER,
                commit_ts TEXT,
                op TEXT,
                schema_name TEXT,
                table_name TEXT,
                relfilenode INTEGER,
                block_num INTEGER,
                offset_num INTEGER,
                spc_oid INTEGER,
                db_oid INTEGER,
                row_data TEXT,
                old_row_data TEXT,
                do_sql TEXT,
                undo_sql TEXT,
                is_catalog INTEGER DEFAULT 0,
                notes TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_contents_rel ON walminer_contents(relfilenode);
            CREATE INDEX IF NOT EXISTS idx_contents_xid ON walminer_contents(xid);
            CREATE INDEX IF NOT EXISTS idx_contents_op ON walminer_contents(op);
            """
        )
        self.conn.commit()

    def add_wal_file(self, path: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO wal_files(path) VALUES(?)",
            (str(path),),
        )
        self.conn.commit()

    def list_wal_files(self) -> list[str]:
        return [r["path"] for r in self.conn.execute("SELECT path FROM wal_files ORDER BY id")]

    def remove_wal_file(self, path: str) -> None:
        self.conn.execute("DELETE FROM wal_files WHERE path=?", (str(path),))
        self.conn.commit()

    def insert_changes(self, changes: Iterable[ChangeRecord], batch: int = 200) -> int:
        import json
        from datetime import datetime

        rows = []
        for ch in changes:
            rows.append(
                (
                    f"{ch.lsn:016X}",
                    ch.lsn,
                    ch.lsn,  # end filled by caller optionally
                    ch.xid,
                    ch.commit_ts,
                    ch.op,
                    ch.schema_name,
                    ch.table_name,
                    ch.rel_number,
                    ch.block_num,
                    ch.offset_num,
                    ch.spc_oid,
                    ch.db_oid,
                    json.dumps(ch.row_data, ensure_ascii=False, default=str),
                    json.dumps(ch.old_row_data, ensure_ascii=False, default=str),
                    ch.do_sql,
                    ch.undo_sql,
                    1 if ch.is_catalog else 0,
                    ch.notes,
                )
            )
            if len(rows) >= batch:
                self._flush(rows)
                rows = []
        if rows:
            self._flush(rows)
        self.conn.commit()
        return len(list(self._last_inserted_count()))

    def _last_inserted_count(self):
        return iter(())

    def _flush(self, rows: list) -> None:
        self.conn.executemany(
            """INSERT INTO walminer_contents(
                lsn, start_lsn, end_lsn, xid, commit_ts, op, schema_name, table_name,
                relfilenode, block_num, offset_num, spc_oid, db_oid,
                row_data, old_row_data, do_sql, undo_sql, is_catalog, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO parse_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM walminer_contents").fetchone()["c"]

    def fetch(
        self,
        op: Optional[str] = None,
        table_like: Optional[str] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM walminer_contents WHERE 1=1"
        args: list = []
        if op:
            sql += " AND op=?"
            args.append(op)
        if table_like:
            sql += " AND (table_name LIKE ? OR schema_name LIKE ?)"
            args.extend([f"%{table_like}%", f"%{table_like}%"])
        sql += " ORDER BY id LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        return list(self.conn.execute(sql, args))

    def export_excel(self, path: Path | str, op: Optional[str] = None, table_like: Optional[str] = None) -> int:
        """Export walminer_contents to Excel (.xlsx). Returns data row count."""
        from .xlsx import write_xlsx

        headers = [
            "ID",
            "LSN",
            "XID",
            "提交时间",
            "操作",
            "Schema",
            "表名",
            "relfilenode",
            "block",
            "offset",
            "DO SQL",
            "UNDO SQL",
            "行数据JSON",
            "旧值JSON",
            "备注",
        ]

        sql = "SELECT * FROM walminer_contents WHERE 1=1"
        args: list = []
        if op:
            sql += " AND op=?"
            args.append(op)
        if table_like:
            sql += " AND (table_name LIKE ? OR schema_name LIKE ?)"
            args.extend([f"%{table_like}%", f"%{table_like}%"])
        sql += " ORDER BY id"

        rows = []
        for r in self.conn.execute(sql, args):
            rows.append(
                [
                    r["id"],
                    r["lsn"] or "",
                    r["xid"],
                    r["commit_ts"] or "",
                    r["op"] or "",
                    r["schema_name"] or "",
                    r["table_name"] or "",
                    r["relfilenode"],
                    r["block_num"],
                    r["offset_num"],
                    r["do_sql"] or "",
                    r["undo_sql"] or "",
                    r["row_data"] or "",
                    r["old_row_data"] or "",
                    r["notes"] or "",
                ]
            )
        return write_xlsx(path, headers, rows, sheet_name="解析结果")

    def export_sql(self, path: Path, only_do: bool = False, only_undo: bool = False) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            f.write("-- generated by pgwinal\nBEGIN;\n")
            for r in self.conn.execute("SELECT * FROM walminer_contents ORDER BY id"):
                if only_do and r["do_sql"]:
                    f.write(r["do_sql"] + "\n")
                    n += 1
                elif only_undo and r["undo_sql"]:
                    f.write(r["undo_sql"] + "\n")
                    n += 1
                elif not only_do and not only_undo:
                    if r["do_sql"]:
                        f.write(f"-- DO xid={r['xid']} lsn={r['lsn']}\n")
                        f.write(r["do_sql"] + "\n")
                    n += 1
            f.write("COMMIT;\n")
        return n

    def summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT op, COUNT(*) c FROM walminer_contents GROUP BY op"
        ).fetchall()
        return {r["op"]: r["c"] for r in rows}
