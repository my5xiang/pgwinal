"""数据字典：aphx SQLite 字典加载 + relfilenode 解析。

字典 schema（与旧项目 pgwinal 兼容）：
  relations(rel_oid, schema_name, rel_name, relfilenode, reltablespace, db_oid, relkind, pk_attnums)
  attributes(rel_oid, attnum, attname, type_oid, type_name, typmod, attnotnull, is_dropped, attndims, collation)
  meta(key, value)  -- version / pg_version / system_id / created_at
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class AttrDef:
    attnum: int
    name: str
    type_oid: int
    type_name: str
    typmod: int
    is_dropped: bool


@dataclass
class RelationDef:
    rel_oid: int
    schema_name: str
    rel_name: str
    relfilenode: int
    db_oid: int
    relkind: str
    attrs: list = field(default_factory=list)   # 按 attnum 升序
    pk_attnums: str = ""                        # 逗号分隔的主键 attnum（如 "1,3"）

    @property
    def qualified(self) -> str:
        return f'{self.schema_name}.{self.rel_name}'

    @property
    def pk_attrs(self) -> list:
        """主键列（按 attnum 升序）。无主键返回 []。"""
        if not self.pk_attnums:
            return []
        try:
            pks = {int(x) for x in str(self.pk_attnums).split(",") if x.strip()}
        except ValueError:
            return []
        return [a for a in self.attrs if a.attnum in pks]


class DataDictionary:
    def __init__(self):
        self.meta: dict[str, str] = {}
        self.system_id: Optional[int] = None
        self.pg_version: str = ""
        self.major: int = 0
        self._by_filenode: dict[tuple[int, int], RelationDef] = {}
        self._by_oid: dict[int, RelationDef] = {}
        self.relation_count = 0

    # ------------------------------------------------------------------

    @classmethod
    def load_sqlite(cls, path) -> "DataDictionary":
        self = cls()
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            self.meta = {k: v for k, v in cur.execute("SELECT key, value FROM meta")}
            self.system_id = int(self.meta.get("system_id", 0)) or None
            self.pg_version = self.meta.get("pg_version", "")
            self.major = int(self.pg_version.split(".")[0]) if self.pg_version else 0

            attrs_by_oid: dict[int, list] = {}
            for row in cur.execute(
                    "SELECT rel_oid, attnum, attname, type_oid, type_name, typmod, is_dropped "
                    "FROM attributes ORDER BY rel_oid, attnum"):
                rel_oid, attnum, attname, type_oid, type_name, typmod, is_dropped = row
                attrs_by_oid.setdefault(rel_oid, []).append(
                    AttrDef(attnum, attname, type_oid, type_name, typmod or 0, bool(is_dropped)))

            for row in cur.execute(
                    "SELECT rel_oid, schema_name, rel_name, relfilenode, db_oid, relkind, pk_attnums "
                    "FROM relations"):
                rel_oid, schema, name, relfilenode, db_oid, relkind, pk_attnums = row
                if not relfilenode:
                    continue
                rel = RelationDef(rel_oid, schema, name, relfilenode, db_oid, relkind or "r",
                                  attrs_by_oid.get(rel_oid, []), pk_attnums or "")
                self._by_filenode[(db_oid, relfilenode)] = rel
                self._by_oid[rel_oid] = rel
            self.relation_count = len(self._by_filenode)
        finally:
            conn.close()
        return self

    # ------------------------------------------------------------------

    def find_by_relfilenode(self, db_oid: int, relfilenode: int) -> Optional[RelationDef]:
        rel = self._by_filenode.get((db_oid, relfilenode))
        if rel is not None:
            return rel
        # 共享表（db_oid=0）或跨库容错
        for db in (db_oid, 0):
            rel = self._by_filenode.get((db, relfilenode))
            if rel is not None:
                return rel
        return None

    def find_by_oid(self, rel_oid: int) -> Optional[RelationDef]:
        return self._by_oid.get(rel_oid)
