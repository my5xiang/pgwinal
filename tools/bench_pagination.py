# -*- coding: utf-8 -*-
"""对比旧分页模式（每页新建连接 + COUNT + OFFSET）与新模式（长连接 + keyset）。

每页成本分开计量；另测 executable 筛选（新索引）与深翻页场景。
"""
import sqlite3
import time
from pathlib import Path

DB = Path(r"D:\mimo\pgwinal\result\pgwal_results.sqlite")
PAGE = 500
N_PAGES = 20
DEEP = 200  # 深翻页：第 200 页（跳过 10 万行）

COLS = ("id,start_lsn,xid,commit_ts,op,schema_name,table_name,"
        "substr(do_sql,1,400),substr(undo_sql,1,400)")
OLD_COLS = ("id,start_lsn,xid,commit_ts,op,schema_name,table_name,"
            "do_sql,undo_sql")


def old_page(conn, page, where="1=1"):
    """旧：每页 COUNT + OFFSET（全文取 do/undo sql）。"""
    conn.execute(f"SELECT COUNT(*) FROM walminer_contents WHERE {where}").fetchone()
    return conn.execute(
        f"SELECT {OLD_COLS} FROM walminer_contents WHERE {where} "
        f"ORDER BY id LIMIT {PAGE} OFFSET {page * PAGE}").fetchall()


def new_page(conn, last_id, where="1=1"):
    """新：keyset（id > 游标），substr 截断 SQL 文本。"""
    return conn.execute(
        f"SELECT {COLS} FROM walminer_contents WHERE ({where}) AND id>? "
        f"ORDER BY id LIMIT {PAGE}", [last_id]).fetchall()


def bench(title, one_page, n=N_PAGES):
    """one_page: 每次调用翻一页。"""
    t0 = time.perf_counter()
    for _ in range(n):
        one_page()
    dt = time.perf_counter() - t0
    print(f"{title:<46} {dt*1000:7.0f} ms / {n} 页 → {dt/n*1000:6.1f} ms/页")
    return dt


if __name__ == "__main__":
    from itertools import count
    ro = lambda: sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    total = ro().execute("SELECT COUNT(*) FROM walminer_contents").fetchone()[0]
    print(f"结果库 {DB.stat().st_size/1e6:.0f} MB · {total:,} 行 · 每页 {PAGE}\n")

    # 预热 OS 文件缓存
    for p in range(DEEP, DEEP + 3):
        old_page(ro(), p)

    print("── 场景A：深翻页（第 200 页起，连续 20 页）──")
    pages = count(DEEP)

    def old_one():
        conn = ro()                      # 旧模式：每页新建连接
        old_page(conn, next(pages))
        conn.close()
    t_old = bench("旧: 新建连接+COUNT+OFFSET(全文)", old_one)

    # 新模式：长连接 + keyset（一次性 seek 到第 200 页，不计入每页成本）
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA mmap_size=268435456")
    t0 = time.perf_counter()
    seek = conn.execute(
        f"SELECT id FROM walminer_contents WHERE 1=1 ORDER BY id "
        f"LIMIT 1 OFFSET {DEEP * PAGE}").fetchone()
    print(f"   （一次性 seek 到第 {DEEP} 页: {(time.perf_counter()-t0)*1000:.0f} ms，"
          f"之后每页不再付出）")
    last = seek[0]

    def new_one():
        global last
        last = new_page(conn, last)[-1][0]
    t_new = bench("新: 长连接+keyset(substr 400)", new_one)
    print(f"   → 每页提速 {t_old/t_new:.1f}x\n")

    print("── 场景B：executable=1 筛选（新加索引）──")
    conn2 = sqlite3.connect(str(DB))
    conn2.execute("PRAGMA query_only=1")
    conn2.execute("PRAGMA cache_size=-65536")
    t0 = time.perf_counter()
    conn2.execute("SELECT COUNT(*) FROM walminer_contents WHERE executable=1").fetchone()
    n_exec = conn2.execute("SELECT COUNT(*) FROM walminer_contents WHERE executable=1").fetchone()[0]
    print(f"   executable=1 共 {n_exec:,} 行，COUNT 耗时 {(time.perf_counter()-t0)*1000:.0f} ms（旧模式每页重复付，新模式仅筛选时付一次）")
    conn2.close()
    conn.close()
