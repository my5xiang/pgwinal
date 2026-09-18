from __future__ import annotations

from datetime import datetime
from typing import Optional


def build_dictionary_from_postgres(
    dsn: str,
    include_system: bool = False,
    limit_relfilenode: Optional[set[int]] = None,
):
    """
    Build DataDictionary by connecting to a live PostgreSQL instance.
    Requires optional dependency: psycopg2-binary or psycopg.
    """
    try:
        import psycopg  # type: ignore
        use_psycopg3 = True
    except ImportError:
        try:
            import psycopg2  # type: ignore
            use_psycopg3 = False
        except ImportError as e:
            raise RuntimeError(
                "未安装数据库驱动。请在运行本程序的 Python 环境中安装：\n"
                "  py -m pip install psycopg[binary]\n"
                "  或 py -m pip install psycopg2-binary\n"
                "当前解释器: " + __import__("sys").executable
            ) from e

    from .schema import AttributeDef, DataDictionary, RelationDef

    def _connect():
        if use_psycopg3:
            return psycopg.connect(dsn)
        import psycopg2

        return psycopg2.connect(dsn)

    sql_rel = """
    SELECT c.oid, n.nspname, c.relname, c.relfilenode, c.reltablespace, c.relkind,
           current_database() AS db,
           (SELECT oid FROM pg_database WHERE datname = current_database()) AS db_oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r','p','m','t')
      AND n.nspname <> 'pg_catalog'
      AND n.nspname <> 'information_schema'
    """
    if not include_system:
        sql_rel += " AND n.nspname NOT LIKE 'pg_toast%'"

    sql_attr = """
    SELECT a.attnum, a.attname, a.atttypid, t.typname, a.atttypmod,
           a.attnotnull, a.attisdropped, a.attndims, a.attcollation
    FROM pg_attribute a
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE a.attrelid = %s AND a.attnum > 0
    ORDER BY a.attnum
    """

    d = DataDictionary(
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    with _connect() as conn:
        with conn.cursor() as cur:

            def _safe_exec(sql: str, params=None):
                """Run SQL; on failure rollback so later statements are not aborted."""
                try:
                    if params is not None:
                        cur.execute(sql, params)
                    else:
                        cur.execute(sql)
                    return True
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    return False

            if _safe_exec("SHOW server_version"):
                d.pg_version = str(cur.fetchone()[0])

            d.system_id = ""
            if _safe_exec("SHOW system_identifier"):
                d.system_id = str(cur.fetchone()[0])
            elif _safe_exec("SELECT system_identifier FROM pg_control_system()"):
                d.system_id = str(cur.fetchone()[0])

            # single snapshot of relations, then attributes per relation
            if not _safe_exec(sql_rel):
                raise RuntimeError("读取 pg_class/pg_namespace 失败")
            rel_rows = cur.fetchall()
            for row in rel_rows:
                rel_oid, nsp, name, relfn, relts, relkind, _db, db_oid = row
                if limit_relfilenode is not None and relfn not in limit_relfilenode:
                    continue
                rel = RelationDef(
                    rel_oid=rel_oid,
                    schema_name=nsp,
                    rel_name=name,
                    relfilenode=relfn,
                    reltablespace=relts or 0,
                    db_oid=db_oid,
                    relkind=relkind,
                )
                if not _safe_exec(sql_attr, (rel_oid,)):
                    continue
                for a in cur.fetchall():
                    rel.attributes.append(
                        AttributeDef(
                            attnum=a[0],
                            attname=a[1],
                            type_oid=a[2],
                            type_name=a[3],
                            typmod=a[4],
                            attnotnull=bool(a[5]),
                            is_dropped=bool(a[6]),
                            attndims=a[7],
                            collation=a[8],
                        )
                    )
                d.relations.append(rel)
    return d
