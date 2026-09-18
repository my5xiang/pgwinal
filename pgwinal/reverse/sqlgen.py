from __future__ import annotations

import json
from typing import Any, Optional

from ..core.models import ChangeRecord
from ..dictstore.schema import DataDictionary, RelationDef
from ..core.heaptuple import decode_tuple_values, TupleData


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (dict, list)):
        return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'"
    if isinstance(value, (bytes, bytearray)):
        return f"E'\\\\x{bytes(value).hex()}'"
    s = str(value)
    # already-decoded timestamps etc.
    return "'" + s.replace("'", "''") + "'"


class SchemaChangeTracker:
    """
    Track DDL-related changes and schema drift between dictionary snapshot
    and WAL-era physical relfilenodes.

    Strategy (research goal #8):
    1. Dictionary is a snapshot at build time (like walminer).
    2. If a relfilenode is missing → mark unknown (schema changed / vacuum full / truncate).
    3. Keep attribute maps from dictionary; if attnums exceed dictionary, emit raw decode warnings.
    4. Support multi-snapshot dictionaries: load additional dict and merge newer relfilenodes.
    """

    def __init__(self, dictionary: DataDictionary):
        self.dictionary = dictionary
        self.unknown_relations: dict[tuple[int, int, int], int] = {}
        self.partial_decodes: list[str] = []
        self.notes: list[str] = []
        self.last_resolve_how: str = "none"

    def resolve(self, spc: int, db: int, rel: int) -> Optional[RelationDef]:
        found, how = self.dictionary.find_relation(rel, db)
        self.last_resolve_how = how
        if found is not None:
            if how == "rel_oid":
                self.notes.append(
                    f"relfilenode={rel} 按 oid 回退匹配到 {found.qualified_name()}"
                    f"（当前 relfilenode={found.relfilenode}，表可能被重写）"
                )
            return found
        key = (spc, db, rel)
        self.unknown_relations[key] = self.unknown_relations.get(key, 0) + 1
        return None

    def merge_dictionary(self, other: DataDictionary) -> None:
        """Merge another dictionary (e.g. older/newer snapshot) by rel_oid+relfilenode."""
        existing = {(r.rel_oid, r.relfilenode) for r in self.dictionary.relations}
        for r in other.relations:
            key = (r.rel_oid, r.relfilenode)
            if key not in existing:
                self.dictionary.relations.append(r)
                existing.add(key)
        self.notes.append(f"merged {len(other.relations)} relations from another dictionary snapshot")

    def report(self) -> dict:
        return {
            "unknown_relations": len(self.unknown_relations),
            "unknown_detail": [
                {"spc": k[0], "db": k[1], "rel": k[2], "hits": v}
                for k, v in list(self.unknown_relations.items())[:200]
            ],
            "notes": self.notes,
            "partial_decodes": self.partial_decodes[:100],
        }


class SqlGenerator:
    def __init__(self, tracker: SchemaChangeTracker):
        self.tracker = tracker

    def _table(self, rel: Optional[RelationDef], spc, db, rel_number) -> str:
        if rel:
            return rel.qualified_name()
        return f'/* unknown relfilenode={rel_number} db={db} */ UNKNOWN_TABLE'

    def generate(
        self,
        op: str,
        rel: Optional[RelationDef],
        new_values: dict[str, Any],
        old_values: Optional[dict[str, Any]] = None,
        spc: int = 1663,
        db: int = 0,
        rel_number: int = 0,
        where_keys: Optional[dict[str, Any]] = None,
        ctid: Optional[tuple[int, int]] = None,
        resolve_how: str = "relfilenode",
    ) -> tuple[str, str, str]:
        """Return (do_sql, undo_sql, notes)."""
        notes: list[str] = []
        table = self._table(rel, spc, db, rel_number)
        if rel is None:
            notes.append(
                f"数据字典中缺少 relfilenode={rel_number}（结构变更/未导出）。"
                f"将生成占位 SQL，请检查字典时间线。"
            )
            # Still try to emit structure-agnostic SQL using raw keys
            fallback = self._fallback_sql(op, table, new_values, old_values)
            if ctid:
                fallback += f"\n-- physical ctid=({ctid[0]},{ctid[1]}) db_oid={db} relfilenode={rel_number}\n"
            return fallback, (
                "-- undo unavailable without data dictionary"
            ), "; ".join(notes)
        if resolve_how == "rel_oid":
            notes.append(
                f"字典按 oid 回退解析：WAL relfilenode={rel_number} → "
                f"{table} (当前 relfilenode={rel.relfilenode})"
            )

        cols = [a for a in rel.attributes if not a.is_dropped]
        if new_values and any(
            isinstance(v, dict) and v.get("status") == "truncated_payload" for v in new_values.values()
        ):
            notes.append("存在 TOAST/截断列，SQL 中该列可能不完整")
            self.tracker.partial_decodes.append(f"{table}: truncated")

        if op == "INSERT":
            col_names = [quote_ident(c.attname) for c in cols if c.attname in new_values]
            val_list = [literal(new_values[c.attname]) for c in cols if c.attname in new_values]
            if not col_names:
                return "-- empty insert", "-- empty undo", "; ".join(notes)
            do = f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(val_list)});"
            # undo: DELETE by all non-null new values (best effort) or where_keys
            w = where_keys or {c.attname: new_values.get(c.attname) for c in cols if c.attname in new_values and new_values.get(c.attname) is not None}
            undo = f"DELETE FROM {table} {self._where(w)};"
            return do, undo, "; ".join(notes)

        if op == "UPDATE":
            sets = []
            where = []
            for c in cols:
                if c.attname in new_values and c.attname in (old_values or {}):
                    if new_values[c.attname] != (old_values or {}).get(c.attname):
                        sets.append(f"{quote_ident(c.attname)} = {literal(new_values[c.attname])}")
                    where.append(f"{quote_ident(c.attname)} = {literal((old_values or {}).get(c.attname))}")
                elif c.attname in new_values:
                    sets.append(f"{quote_ident(c.attname)} = {literal(new_values[c.attname])}")
            if not sets:
                sets = ["/* no column change detected */"]
            w = where_keys or where
            if not w and ctid:
                w_sql = f"WHERE ctid = '({ctid[0]},{ctid[1]})'"
            else:
                w_sql = self._where(w)
            do = f"UPDATE {table} SET {', '.join(sets)} {w_sql};"
            # undo: reverse set
            if old_values:
                undo_sets = [
                    f"{quote_ident(c.attname)} = {literal(old_values.get(c.attname))}"
                    for c in cols
                    if c.attname in old_values
                ]
                new_where = [
                    f"{quote_ident(c.attname)} = {literal(new_values.get(c.attname))}"
                    for c in cols
                    if c.attname in new_values
                ]
                undo = f"UPDATE {table} SET {', '.join(undo_sets)} {self._where(where_keys or new_where)};"
            else:
                undo = f"-- undo UPDATE missing old tuple (only key/new image logged); check ctid-based recovery on table {table}"
                notes.append("UPDATE 未携带旧元组镜像，UNDO 可能不完整（walminer 同类限制）")
            return do, undo, "; ".join(notes)

        if op == "DELETE":
            where = where_keys or {
                c.attname: old_values.get(c.attname)
                for c in cols
                if old_values and c.attname in old_values and old_values.get(c.attname) is not None
            }
            if where:
                do = f"DELETE FROM {table} {self._where(where)};"
            elif ctid:
                do = f"DELETE FROM {table} WHERE ctid = '({ctid[0]},{ctid[1]})';"
                notes.append("DELETE 未携带业务键/旧元组，DO SQL 使用 ctid 定位")
            else:
                do = f"DELETE FROM {table} {self._where(where)};"
            if old_values:
                col_names = [quote_ident(c.attname) for c in cols if c.attname in old_values]
                val_list = [literal(old_values.get(c.attname)) for c in cols if c.attname in old_values]
                undo = f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(val_list)});"
            else:
                undo = (
                    f"-- undo DELETE missing old key/tuple image on {table}\n"
                    + (f"-- physical ctid=({ctid[0]},{ctid[1]})\n" if ctid else "")
                    + "-- 需完整 page image / 业务备份才能还原 INSERT UNDO"
                )
                notes.append("DELETE 未包含旧元组（仅 offnum/infobits），无法生成精确 UNDO")
            return do, undo, "; ".join(notes)

        if op == "MULTI_INSERT":
            # treat first row; caller expands multi
            col_names = [quote_ident(c.attname) for c in cols if c.attname in new_values]
            val_list = [literal(new_values.get(c.attname)) for c in cols if c.attname in new_values]
            do = f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(val_list)});"
            w = {
                c.attname: new_values.get(c.attname)
                for c in cols
                if c.attname in new_values and new_values.get(c.attname) is not None
            }
            undo = f"DELETE FROM {table} {self._where(w)};"
            return do, undo, "; ".join(notes)

        return f"-- unsupported op {op}", "", "; ".join(notes)

    def _fallback_sql(self, op: str, table: str, new_values, old_values) -> str:
        return (
            f"-- schema-less {op} (no dictionary)\n"
            f"-- table filenode path: {table}\n"
            f"-- new: {json.dumps(new_values, ensure_ascii=False, default=str)[:500]}\n"
            f"-- old: {json.dumps(old_values or {}, ensure_ascii=False, default=str)[:500]}\n"
        )

    @staticmethod
    def _where(pairs: Optional[dict[str, Any]]) -> str:
        if not pairs:
            return "-- WHERE (无法定位行，建议人工核对 ctid/业务键)"
        parts = [f"{quote_ident(k)} = {literal(v)}" for k, v in pairs.items()]
        return "WHERE " + " AND ".join(parts)


def decode_row_from_tuple_header(
    td: TupleData,
    rel: Optional[RelationDef],
    payload: bytes,
) -> dict[str, Any]:
    return decode_tuple_values(td, rel, payload)
