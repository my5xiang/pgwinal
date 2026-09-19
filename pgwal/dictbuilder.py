"""数据字典构建：从在线 PostgreSQL 生成（需 psycopg2，可选依赖）。

生成与 aphx 字典兼容的 SQLite schema（见 dictstore.py 文档）。
用法：python -m pgwal dict --dsn postgresql://user:pass@host:5432/db --out dict.sqlite
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def build_dictionary(dsn: str, out_path: str) -> None:
    try:
        import psycopg2
    except ImportError:
        raise SystemExit("需要 psycopg2：pip install psycopg2-binary")

    conn = psycopg2.connect(dsn)
    conn.set_client_encoding("UTF8")
    cur = conn.cursor()

    # 元信息
    cur.execute("SELECT current_setting('server_version')")
    pg_version = cur.fetchone()[0]
    cur.execute("SELECT system_identifier FROM pg_control_system()")
    row = cur.fetchone()
    system_id = row[0] if row else 0
    cur.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
    db_oid = cur.fetchone()[0]

    out = Path(out_path)
    if out.exists():
        out.unlink()
    s = sqlite3.connect(out)
    s.executescript("""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE relations (rel_oid INTEGER, schema_name TEXT, rel_name TEXT,
  relfilenode INTEGER, reltablespace INTEGER, db_oid INTEGER, relkind TEXT, pk_attnums TEXT);
CREATE TABLE attributes (rel_oid INTEGER, attnum INTEGER, attname TEXT,
  type_oid INTEGER, type_name TEXT, typmod INTEGER, attnotnull INTEGER,
  is_dropped INTEGER, attndims INTEGER, collation INTEGER);
CREATE INDEX idx_rel_fil ON relations(db_oid, relfilenode);
CREATE INDEX idx_att_rel ON attributes(rel_oid);
""")

    from datetime import datetime
    s.executemany("INSERT INTO meta VALUES (?,?)", [
        ("version", "1"),
        ("pg_version", pg_version),
        ("system_id", str(system_id)),
        ("db_oid", str(db_oid)),
        ("created_at", datetime.now().isoformat(sep=" ")),
    ])

    # 关系（含 toast 表，供 TOAST 重组映射）
    cur.execute("""
        SELECT c.oid, n.nspname, c.relname, pg_relation_filenode(c.oid),
               c.reltablespace, c.relkind
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','t','p')
    """)
    rels = cur.fetchall()
    s.executemany(
        "INSERT INTO relations VALUES (?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3] or 0, r[4] or 0, db_oid, r[5], "") for r in rels])

    # 属性
    cur.execute("""
        SELECT a.attrelid, a.attnum, a.attname, a.atttypid, t.typname,
               a.atttypmod, a.attnotnull::int, a.attisdropped::int,
               a.attndims, a.attcollation
        FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
        WHERE a.attnum > 0
    """)
    attrs = cur.fetchall()
    s.executemany(
        "INSERT INTO attributes VALUES (?,?,?,?,?,?,?,?,?,?)", attrs)

    # 主键信息（供 UNDO WHERE 优化）
    cur.execute("""
        SELECT con.conrelid, array_agg(a.attnum ORDER BY a.attnum)
        FROM pg_constraint con
        JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        WHERE con.contype = 'p'
        GROUP BY con.conrelid
    """)
    for rel_oid, attnums in cur.fetchall():
        s.execute("UPDATE relations SET pk_attnums=? WHERE rel_oid=?",
                  (",".join(str(x) for x in attnums), rel_oid))

    s.commit()
    s.close()
    conn.close()
    print(f"字典已生成: {out}（{len(rels)} 关系 / {len(attrs)} 属性 / PG {pg_version}）")
