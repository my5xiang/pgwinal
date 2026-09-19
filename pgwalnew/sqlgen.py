"""DO / UNDO SQL 生成（v1.3：主键精简 + 增量 UPDATE）。

策略（开发方案 §6.6）：
  行定位：主键优先（字典 pk_attnums）→ 全行 IS NOT DISTINCT FROM 回退
  UPDATE：仅变更列进 SET（DO 新值 / UNDO 旧值，同一组列）；无变更则跳过
  INSERT DO 全列；INSERT UNDO = 按主键删（无主键回退全行）
  DELETE DO = 按主键删（无主键回退全行）；DELETE UNDO = 全列插回
"""

from __future__ import annotations

from typing import Optional

from .decode import Change
from .sqlval import col_list, quote_ident, quote_qualified, sql_literal, where_clause


def _key_where(rel, values: dict, full_attrs) -> Optional[str]:
    """行定位 WHERE：主键优先，回退全行。返回 None 表示无法定位。"""
    pk = rel.pk_attrs
    if pk and all(values.get(a.name) is not None for a in pk):
        parts = [f"{quote_ident(a.name)} = {sql_literal(values[a.name], a.type_name)}"
                 for a in pk]
        return " AND ".join(parts)
    # 回退：全行匹配（NULL 安全）
    return where_clause(values, full_attrs)


def _changed_cols(ch: Change):
    """返回仅值发生变化的属性列表。

    DO 用新值作 SET；UNDO 用旧值作 SET（同一组列）。
    """
    changed = []
    for att in ch.rel.attrs:
        old_v = ch.old_values.get(att.name, _MISSING)
        new_v = ch.new_values.get(att.name, _MISSING)
        if old_v is _MISSING or new_v is _MISSING:
            continue
        if not _values_equal(old_v, new_v):
            changed.append(att)
    return changed


_MISSING = object()


def _values_equal(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return bool(a == b)
    except Exception:
        return False


def gen_do_sql(ch: Change) -> Optional[str]:
    rel = ch.rel
    if rel is None:
        return None
    q = quote_qualified(rel.schema_name, rel.rel_name)
    attrs = rel.attrs
    if ch.op in ("INSERT", "MULTI_INSERT"):
        if not ch.new_values:
            return None
        return (f"INSERT INTO {q} ({col_list(attrs)}) "
                f"VALUES ({', '.join(sql_literal(ch.new_values.get(a.name), a.type_name) for a in attrs)});")
    if ch.op == "UPDATE":
        if not ch.new_values or not ch.old_values:
            return None
        changed = _changed_cols(ch)
        if not changed:
            return None  # 无实际变更（如 SET x=x），重放语义安全
        where = _key_where(rel, ch.old_values, attrs)
        if not where:
            return None
        sets = ", ".join(
            f"{quote_ident(a.name)} = {sql_literal(ch.new_values[a.name], a.type_name)}"
            for a in changed)
        return f"UPDATE {q} SET {sets} WHERE {where};"
    if ch.op == "DELETE":
        if not ch.old_values:
            return None
        where = _key_where(rel, ch.old_values, attrs)
        if not where:
            return None
        return f"DELETE FROM {q} WHERE {where};"
    return None


def gen_undo_sql(ch: Change) -> Optional[str]:
    rel = ch.rel
    if rel is None:
        return None
    q = quote_qualified(rel.schema_name, rel.rel_name)
    attrs = rel.attrs
    if ch.op in ("INSERT", "MULTI_INSERT"):
        # 撤销插入 = 删除该行（主键优先定位）
        if not ch.new_values:
            return None
        where = _key_where(rel, ch.new_values, attrs)
        if not where:
            return None
        return f"DELETE FROM {q} WHERE {where};"
    if ch.op == "UPDATE":
        # 撤销更新 = 变更列回写旧值，按新行定位（主键优先）
        if not ch.old_values or not ch.new_values:
            return None
        changed = _changed_cols(ch)
        if not changed:
            return None
        where = _key_where(rel, ch.new_values, attrs)
        if not where:
            return None
        sets = ", ".join(
            f"{quote_ident(a.name)} = {sql_literal(ch.old_values[a.name], a.type_name)}"
            for a in changed)
        return f"UPDATE {q} SET {sets} WHERE {where};"
    if ch.op == "DELETE":
        # 撤销删除 = 完整插回旧行
        if not ch.old_values:
            return None
        return (f"INSERT INTO {q} ({col_list(attrs)}) "
                f"VALUES ({', '.join(sql_literal(ch.old_values.get(a.name), a.type_name) for a in attrs)});")
    return None
