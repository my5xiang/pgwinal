"""pgwal 命令行入口。

用法：
  python -m pgwal parse <wal目录或文件>... --dict <字典.sqlite> --out <结果.sqlite>
      [--all-tx] [--max-records N] [--export-do do.sql] [--export-undo undo.sql]
  python -m pgwal dict --dsn <postgresql://...> --out <字典.sqlite>
  python -m pgwal gui
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_parse(args) -> int:
    from .dictstore import DataDictionary
    from .engine import Engine
    from .resultstore import ResultStore

    d = DataDictionary.load_sqlite(args.dict)
    print(f"字典: {Path(args.dict).name}  PG={d.pg_version}  "
          f"关系={d.relation_count}  system_id={d.system_id}")
    if d.major not in range(12, 19):
        print(f"警告: 字典 PG 版本 {d.pg_version} 不在 12–18 支持范围")
    # 主键覆盖率（决定 DELETE/UPDATE 是否精简）
    n_pk = sum(1 for r in d._by_filenode.values() if r.pk_attnums)
    if n_pk == 0:
        print("警告: 字典无主键信息——DELETE/INSERT-UNDO 将使用全字段匹配（冗长）。"
              "建议用「生成字典」重新生成（自动采集主键），或使用含主键的字典。")
    else:
        print(f"主键覆盖: {n_pk}/{d.relation_count} 张表"
              f"（DELETE 按主键定位，UPDATE 仅变更列）")

    result = ResultStore(args.out)
    eng = Engine(d, result, only_committed=not args.all_tx,
                 progress=lambda m: print(m, flush=True),
                 max_pages=args.max_pages)
    stats = eng.parse(args.wal, max_records=args.max_records)

    print("\n===== 解析结果 =====")
    for k in ("files", "commits", "aborts", "changes", "uncommitted_skipped",
              "pass1_records", "pass2_records", "elapsed"):
        if k in stats:
            print(f"  {k}: {stats[k]}")
    for k, v in sorted(stats.items()):
        if k.startswith("dec_"):
            print(f"  {k[4:]}: {v}")
    print("\n结果汇总:")
    for op, s in result.summary().items():
        print(f"  {op}: {s['count']} 条（可执行 {s['executable']}）")
    print(f"\n结果库: {args.out}")

    if args.export_do:
        n = result.export_sql(args.export_do, "do")
        print(f"DO SQL 导出: {args.export_do}（{n} 条）")
    if args.export_undo:
        n = result.export_sql(args.export_undo, "undo")
        print(f"UNDO SQL 导出: {args.export_undo}（{n} 条）")
    result.close()
    return 0


def cmd_dict(args) -> int:
    from .dictbuilder import build_dictionary
    build_dictionary(args.dsn, args.out)
    return 0


def cmd_gui(args) -> int:
    from .gui.app import main
    main()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pgwal")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="解析 WAL 生成 DO/UNDO SQL")
    p.add_argument("wal", nargs="+", help="WAL 段文件或目录")
    p.add_argument("--dict", required=True, help="数据字典 sqlite")
    p.add_argument("--out", default="result.sqlite", help="结果 sqlite")
    p.add_argument("--all-tx", action="store_true", help="包含未提交事务")
    p.add_argument("--max-records", type=int, default=0)
    p.add_argument("--max-pages", type=int, default=60000,
                   help="页像重放引擎的页库上限（LRU），增大可减少 incomplete")
    p.add_argument("--export-do", help="导出 DO SQL 文件")
    p.add_argument("--export-undo", help="导出 UNDO SQL 文件")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("dict", help="从在线库生成数据字典")
    p.add_argument("--dsn", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_dict)

    p = sub.add_parser("gui", help="启动图形界面")
    p.set_defaults(func=cmd_gui)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
