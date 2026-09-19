"""SQL 字面量与标识符渲染。"""

from __future__ import annotations

import datetime
from decimal import Decimal

from .typereg import ExternalValue, RawValue


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_qualified(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def sql_literal(v, col_type: str = "") -> str:
    """Python 值 → SQL 字面量。RawValue/ExternalValue → 不可执行占位。"""
    if v is None:
        return "NULL"
    if v is True:
        return "TRUE"
    if v is False:
        return "FALSE"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "'NaN'"
        if v == float("inf"):
            return "'Infinity'"
        if v == float("-inf"):
            return "'-Infinity'"
        return repr(v)
    if isinstance(v, str):
        # 特殊文本值
        if col_type in ("timestamp", "timestamptz", "date", "time", "timetz", "interval"):
            return "'" + v.replace("'", "''") + "'"
        if v in ("infinity", "-infinity") and col_type in ("date", "timestamp", "timestamptz"):
            return "'" + v + "'"
        return _quote_str(v)
    if isinstance(v, datetime.datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
    if isinstance(v, datetime.date):
        return "'" + v.isoformat() + "'"
    if isinstance(v, (bytes, bytearray)):
        return "'\\x" + bytes(v).hex() + "'"
    if isinstance(v, (RawValue, ExternalValue)):
        return "/* 不可执行: " + repr(v).replace("*/", "* /") + " */ NULL"
    if isinstance(v, list):
        return "ARRAY[" + ", ".join(sql_literal(x, col_type) for x in v) + "]"
    return _quote_str(str(v))


def _quote_str(s: str) -> str:
    if "\\" in s:
        return "E'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return "'" + s.replace("'", "''") + "'"


def where_clause(values: dict, attrs: list) -> str:
    """全列 IS NOT DISTINCT FROM 匹配（NULL 安全，可执行）。"""
    parts = []
    for att in attrs:
        v = values.get(att.name)
        lit = sql_literal(v, att.type_name)
        if v is None:
            parts.append(f"{quote_ident(att.name)} IS NULL")
        else:
            parts.append(f"{quote_ident(att.name)} IS NOT DISTINCT FROM {lit}")
    return " AND ".join(parts) if parts else "TRUE"


def col_list(attrs: list) -> str:
    return ", ".join(quote_ident(a.name) for a in attrs)
