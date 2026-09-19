"""M0 帧级验证：在真实 WAL 上跑严格扫描器，输出 CRC/prev 链/直方图报告。

用法：
    python tools/validate_framing.py [wal_dir] [--dict path.sqlite] [--max-segs N]
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pgwalnew import profiles as P  # noqa: E402
from pgwalnew.xlogreader import WalStream, collect_wal_files  # noqa: E402


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("wal_dir", nargs="?", default=r"D:\mimo\pgwinal\testpg")
    ap.add_argument("--dict", default=r"D:\mimo\pgwinal\dict\pgwalnew_dict_aphx.sqlite")
    ap.add_argument("--max-segs", type=int, default=0)
    ap.add_argument("--rmgr-hist", action="store_true")
    args = ap.parse_args()

    files = collect_wal_files(Path(args.wal_dir))
    if args.max_segs:
        files = files[: args.max_segs]
    if not files:
        print(f"未找到 WAL 文件: {args.wal_dir}")
        return 2

    system_id = None
    pg_version = None
    if args.dict and Path(args.dict).exists():
        conn = sqlite3.connect(args.dict)
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        conn.close()
        system_id = int(meta.get("system_id", 0)) or None
        pg_version = meta.get("pg_version", "?")
        print(f"字典: {Path(args.dict).name}  PG={pg_version}  system_id={system_id}")

    print(f"段文件: {len(files)} 个，从 {files[0].name} 到 {files[-1].name}")

    notes = []
    stream = WalStream(files, profile=P.get_profile(12), system_id=system_id,
                        on_note=notes.append)

    rmgr_hist = Counter()
    heap_ops = Counter()
    heap2_ops = Counter()
    xact_ops = Counter()
    t0 = time.time()
    bytes_scanned = 0
    last_end = None
    gaps = 0

    try:
        for rec in stream.iter_records():
            rmgr_hist[rec.rmid] += 1
            if rec.rmid == P.RM_HEAP_ID:
                heap_ops[rec.info & P.XLOG_HEAP_OPMASK] += 1
            elif rec.rmid == P.RM_HEAP2_ID:
                heap2_ops[rec.info & P.XLOG_HEAP_OPMASK] += 1
            elif rec.rmid == P.RM_XACT_ID:
                xact_ops[rec.info & P.XLOG_XACT_OPMASK] += 1
            if last_end is not None and rec.start_lsn != last_end:
                # 记录间允许 MAXALIGN 填充
                if rec.start_lsn - last_end > 8:
                    gaps += 1
            last_end = rec.end_lsn
    except Exception as e:
        print(f"\n[失败] {type(e).__name__}: {e}")
        print(f"已扫描: {stream.stats['records']} 条记录后中止")
        return 1

    dt = time.time() - t0
    st = stream.stats
    total_mb = st["pages"] * 8192 / 1024 / 1024

    print("\n===== M0 帧级验证报告 =====")
    print(f"耗时 {dt:.1f}s  ({total_mb / dt:.1f} MB/s)")
    print(f"页: {st['pages']}  文件: {st['files']}  零页: {st['zero_pages']}")
    print(f"记录: {st['records']}  CRC通过: {st['crc_ok']}  prev链校验: {st['prev_checked']}")
    print(f"续记录: {st['continuations']}  孤儿续记录字节: {st['orphan_continuation_bytes']}")
    print(f"XLOG_SWITCH: {st['xlog_switch']}  最大记录: {st['max_reclen']}B")
    print(f"LSN 覆盖: {files[0].name} → 0x{last_end:016X}" if last_end else "")

    ok = (st["records"] == st["crc_ok"] == st["prev_checked"] + (1 if st["records"] else 0)
          if False else st["records"] > 0 and st["crc_ok"] == st["records"]
          and st["prev_checked"] == st["records"] - 1)
    print(f"\nCRC 100%: {'是' if st['crc_ok'] == st['records'] else '否'}")
    print(f"prev 链 100%: {'是' if st['prev_checked'] == st['records'] - 1 else '否'}"
          f"（首条无前驱，不计）")

    print("\nrmgr 直方图:")
    for rmid, n in rmgr_hist.most_common():
        print(f"  {rmid:2d} {P.RMGR_NAMES.get(rmid, '?'):18s} {n:>10}")
    print("\nHeap opcode:")
    names = {0x00: "INSERT", 0x10: "DELETE", 0x20: "UPDATE", 0x30: "TRUNCATE",
             0x40: "HOT_UPDATE", 0x50: "CONFIRM", 0x60: "LOCK", 0x70: "INPLACE"}
    for op, n in heap_ops.most_common():
        print(f"  {names.get(op, hex(op)):12s} {n:>10}")
    print("Heap2 opcode:")
    h2names = {0x00: "REWRITE", 0x10: "CLEAN", 0x20: "FREEZE_PAGE", 0x30: "CLEANUP_INFO",
               0x40: "VISIBLE", 0x50: "MULTI_INSERT", 0x60: "LOCK_UPDATED", 0x70: "NEW_CID"}
    for op, n in heap2_ops.most_common():
        print(f"  {h2names.get(op, hex(op)):16s} {n:>10}")
    print("XACT opcode:")
    xnames = {0x00: "COMMIT", 0x10: "PREPARE", 0x20: "ABORT", 0x30: "COMMIT_PREPARED",
              0x40: "ABORT_PREPARED", 0x50: "ASSIGNMENT"}
    for op, n in xact_ops.most_common():
        print(f"  {xnames.get(op, hex(op)):18s} {n:>10}")

    if notes:
        print(f"\n备注 ({len(notes)}):")
        for n in notes[:10]:
            print(f"  - {n}")

    print("\n结论:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
