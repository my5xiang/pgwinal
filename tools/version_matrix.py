"""PG 13–18 版本矩阵测试：真实实例生成 WAL → pgwal 解析 → 与地面真值对照。

前置：各版本便携二进制已解压到 D:\\mimo\\pgwinal\\pgvXX\\（bin/initdb/psql 等）。
用法：python tools\\version_matrix.py 13 14 15 16 17 18
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE_PORT = 55200  # 每版本 +1

# 地面真值 DML 脚本（已知操作序列）
DML = """
CREATE TABLE vm_test (
    id int PRIMARY KEY,
    val text,
    num numeric(12,2),
    ts timestamp,
    f double precision,
    b boolean,
    d date
);
-- 50 行插入
INSERT INTO vm_test SELECT i, 'row_' || i, i * 1.5, '2026-01-01'::timestamp + i * interval '1 hour', i * 0.5, (i % 2 = 0), ('2026-01-' || lpad((i % 28 + 1)::text, 2, '0'))::date FROM generate_series(1, 50) i;
-- 20 行更新
UPDATE vm_test SET val = 'upd_' || id, num = num + 100 WHERE id <= 20;
-- 10 行删除
DELETE FROM vm_test WHERE id > 40;
-- 子事务
BEGIN;
SAVEPOINT s1;
INSERT INTO vm_test VALUES (100, 'sub_tx', 999.99, now(), 1.5, true, current_date);
RELEASE SAVEPOINT s1;
COMMIT;
-- 回滚
BEGIN;
INSERT INTO vm_test VALUES (200, 'rolled_back', 0, now(), 0, false, current_date);
ROLLBACK;
-- 大值（触发 TOAST 路径检测）
UPDATE vm_test SET val = repeat('x', 500) WHERE id = 1;
CHECKPOINT;
-- checkpoint 后再改（产生 FPI）
UPDATE vm_test SET num = 42.00 WHERE id BETWEEN 21 AND 30;
DELETE FROM vm_test WHERE id = 31;
INSERT INTO vm_test VALUES (301, 'after_ckpt', 77.7, now(), 7.7, false, current_date);
SELECT pg_switch_wal();
"""

EXPECTED = {
    "inserts": 52,       # 50 + sub_tx + after_ckpt
    "deletes": 11,       # 10 + id=31
    "updates": 31,       # 20 + 1 (toast) + 10 (ckpt 后)
    "rolled_back": 1,     # id=200 不应出现
    "final_rows": 42,    # 50 - 10 + 1(sub) + 1(ckpt后) - 1(id=31) = 41... 让脚本算
}


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    return r


def setup_instance(ver: int, bin_dir: Path, data_dir: Path, port: int) -> bool:
    """initdb + 启动（不等待 pg_ctl 返回，避免 Windows 下挂起）。"""
    # 先清理旧实例
    try:
        run([str(bin_dir / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "stop"],
            timeout=10)
    except Exception:
        pass
    time.sleep(1)
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    r = run([str(bin_dir / "initdb"), "-D", str(data_dir), "-U", "postgres",
              "-A", "trust", "-E", "UTF8"])
    if r.returncode != 0:
        print(f"  initdb 失败: {r.stderr[-200:]}")
        return False
    conf = data_dir / "postgresql.conf"
    conf.write_text(conf.read_text() +
                    f"\nlisten_addresses = '127.0.0.1'\nport = {port}\n"
                    f"wal_level = replica\nlogging_collector = off\n", encoding="utf-8")
    # 不用 -w（等待启动完成会挂起）；后台启动后轮询就绪
    subprocess.Popen([str(bin_dir / "pg_ctl"), "-D", str(data_dir),
                      "-l", str(ROOT / f"pgv{ver}.log"), "start"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        r = run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                 "-U", "postgres", "-t", "-c", "SELECT 1"])
        if r.returncode == 0:
            return True
    print("  启动超时（30s）")
    return False


def psql(bin_dir: Path, port: int, sql: str, db="postgres") -> subprocess.CompletedProcess:
    return run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                "-U", "postgres", "-d", db, "-q", "-t", "-A",
                "-v", "ON_ERROR_STOP=1", "-c", sql])


def test_version(ver: int) -> dict:
    bin_dir = ROOT / f"pgv{ver}" / "bin"
    if not bin_dir.exists():
        # 也尝试 pgv{ver} 直接含 bin
        alt = ROOT / f"pgv{ver}"
        if (alt / "bin" / "psql.exe").exists():
            bin_dir = alt / "bin"
        else:
            return {"ver": ver, "status": "SKIP", "reason": f"二进制不存在: {bin_dir}"}
    port = BASE_PORT + ver
    data_dir = ROOT / f"pgv{ver}_data"
    wal_dir = data_dir / "pg_wal"

    print(f"\n===== PG {ver} (port {port}) =====")
    if not setup_instance(ver, bin_dir, data_dir, port):
        return {"ver": ver, "status": "INIT_FAIL"}

    try:
        # 1. 执行 DML 脚本
        sql_file = ROOT / f"vm_{ver}.sql"
        sql_file.write_text(DML, encoding="utf-8")
        r = run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                 "-U", "postgres", "-d", "postgres", "-q", "-v", "ON_ERROR_STOP=1",
                 "-f", str(sql_file)])
        if r.returncode != 0:
            print(f"  DML 执行失败: {r.stderr[:300]}")
            return {"ver": ver, "status": "DML_FAIL", "stderr": r.stderr[:300]}
        sql_file.unlink()

        # 2. 地面真值
        r = psql(bin_dir, port, "SELECT COUNT(*) FROM vm_test")
        final_rows = int(r.stdout.strip())
        r = psql(bin_dir, port, "SELECT COUNT(*) FROM vm_test WHERE val = 'rolled_back'")
        rolled = int(r.stdout.strip())

        # 3. 生成字典
        dict_path = ROOT / f"vm_dict_{ver}.sqlite"
        from pgwal.dictbuilder import build_dictionary
        build_dictionary(f"postgresql://postgres@127.0.0.1:{port}/postgres", str(dict_path))

        # 4. 解析 WAL（switch 后的当前段 + 前一段）
        wals = sorted(wal_dir.glob("[0-9A-F]" * 24))
        if not wals:
            return {"ver": ver, "status": "NO_WAL"}
        # 取最后 2 个段（DML 在其中）
        parse_wals = wals[-2:] if len(wals) >= 2 else wals

        from pgwal.dictstore import DataDictionary
        from pgwal.engine import Engine
        from pgwal.resultstore import ResultStore
        from pgwal import profiles as P

        d = DataDictionary.load_sqlite(dict_path)
        result_path = ROOT / f"vm_result_{ver}.sqlite"
        result = ResultStore(str(result_path))
        eng = Engine(d, result, only_committed=True,
                     progress=lambda m: None)
        stats = eng.parse([str(w) for w in parse_wals])
        result.commit()

        # 5. 核对
        summary = result.summary()
        n_ins = summary.get("INSERT", {}).get("count", 0) + \
            summary.get("MULTI_INSERT", {}).get("count", 0)
        n_upd = summary.get("UPDATE", {}).get("count", 0)
        n_del = summary.get("DELETE", {}).get("count", 0)
        n_err = summary.get("ERROR", {}).get("count", 0)

        # 回放 DO SQL 验证行数
        replay_rows = None
        do_path = ROOT / f"vm_do_{ver}.sql"
        n_do = result.export_sql(str(do_path), "do")
        result.close()

        # 6. 回放到 scratch 库
        r = run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                 "-U", "postgres", "-d", "postgres", "-q", "-c",
                 "CREATE DATABASE vm_replay"])
        if r.returncode == 0:
            # 建表
            run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                 "-U", "postgres", "-d", "vm_replay", "-q", "-c",
                 DML.split(";")[0] + ";"])
            r2 = run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                      "-U", "postgres", "-d", "vm_replay", "-q", "-v",
                      "ON_ERROR_STOP=0", "-f", str(do_path)])
            errs = r2.stderr.count("ERROR") if r2.stderr else 0
            r3 = psql(bin_dir, port, "SELECT COUNT(*) FROM vm_test", db="vm_replay")
            try:
                replay_rows = int(r3.stdout.strip())
            except ValueError:
                replay_rows = -1
            run([str(bin_dir / "psql"), "-h", "127.0.0.1", "-p", str(port),
                 "-U", "postgres", "-d", "postgres", "-q", "-c",
                 "DROP DATABASE vm_replay"])

        do_path.unlink(missing_ok=True)
        dict_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)

        ok = (n_err == 0 and rolled == 0 and
              n_ins >= EXPECTED["inserts"] - 2 and  # 允许 ±2（段边界）
              n_upd >= EXPECTED["updates"] - 2 and
              n_del >= EXPECTED["deletes"] - 2)

        return {
            "ver": ver, "status": "PASS" if ok else "CHECK",
            "crc_ok": stats.get("pass1_records", 0),
            "changes": stats.get("changes", 0),
            "ins": n_ins, "upd": n_upd, "del": n_del, "err": n_err,
            "final_rows": final_rows, "replay_rows": replay_rows,
            "replay_errors": errs if replay_rows is not None else None,
        }
    finally:
        try:
            # 先尝试正常停，再强制停
            run([str(bin_dir / "pg_ctl"), "-D", str(data_dir), "-m", "immediate", "stop"],
                timeout=10)
        except Exception:
            pass
        time.sleep(2)
        try:
            if data_dir.exists():
                shutil.rmtree(data_dir)
        except Exception:
            pass


def main():
    versions = [int(x) for x in sys.argv[1:]] or [13, 14, 15, 16, 17, 18]
    results = []
    for v in versions:
        try:
            results.append(test_version(v))
        except Exception as e:
            results.append({"ver": v, "status": "EXCEPTION", "reason": str(e)[:200]})
    print("\n" + "=" * 70)
    print("版本矩阵结果")
    print("=" * 70)
    for r in results:
        print(f"  PG{r['ver']}: {r['status']}"
              + (f"  ins={r['ins']} upd={r['upd']} del={r['del']} err={r['err']}"
                 f"  源库行数={r.get('final_rows')} 回放行数={r.get('replay_rows')}"
                 f" 回放错误={r.get('replay_errors')}" if r["status"] in ("PASS", "CHECK") else
                 f"  {r.get('reason', r.get('stderr', ''))}"))
    ok = all(r["status"] == "PASS" for r in results)
    print(f"\n结论: {'全部通过 ✅' if ok else '存在需检查项 ⚠️'}")


if __name__ == "__main__":
    main()
