from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pgwinal", description="PostgreSQL WAL DO/UNDO SQL 解析工具")
    sub = parser.add_subparsers(dest="cmd")

    p_gui = sub.add_parser("gui", help="启动图形界面")
    p_dict = sub.add_parser("dict", help="从数据库生成数据字典")
    p_dict.add_argument("--dsn", required=True)
    p_dict.add_argument("--out", default="dict/pgwinal_dict.sqlite")
    p_dict.add_argument("--include-system", action="store_true")

    p_parse = sub.add_parser("parse", help="解析 WAL")
    p_parse.add_argument("wal", nargs="+", help="WAL 文件或目录")
    p_parse.add_argument("--dict", dest="dict_path", default="dict/pgwinal_dict.sqlite")
    p_parse.add_argument("--out", default="result/pgwinal_results.sqlite")
    p_parse.add_argument("--export-do", default="")
    p_parse.add_argument("--export-undo", default="")
    p_parse.add_argument("--all-tx", action="store_true", help="包含未提交事务")

    args = parser.parse_args(argv)
    if args.cmd is None or args.cmd == "gui":
        from .gui.app import main as gui_main

        gui_main()
        return 0

    if args.cmd == "dict":
        from .dictstore.builder import build_dictionary_from_postgres
        from .dictstore.schema import DictStore

        d = build_dictionary_from_postgres(args.dsn, include_system=args.include_system)
        DictStore(Path(args.out)).save_dictionary(d)
        print(f"OK: {len(d.relations)} relations -> {args.out}")
        return 0

    if args.cmd == "parse":
        from .dictstore.schema import DictStore
        from .parse.engine import ParseOptions, WalParseEngine
        from .resultstore.resultdb import ResultStore

        dict_path = Path(args.dict_path)
        if dict_path.exists():
            dictionary = DictStore(dict_path).load_dictionary()
            print(f"Loaded dictionary: {len(dictionary.relations)} relations")
        else:
            print("WARN: dictionary not found, SQL may be incomplete")
            from .dictstore.schema import DataDictionary

            dictionary = DataDictionary()
        engine = WalParseEngine(dictionary, ResultStore(Path(args.out)))
        options = ParseOptions(only_committed=not args.all_tx)
        report = engine.parse_paths(args.wal, options, progress=lambda m: print(m))
        print("REPORT:", report)
        rs = engine.results
        if args.export_do:
            print("export do", rs.export_sql(Path(args.export_do), only_do=True))
        if args.export_undo:
            print("export undo", rs.export_sql(Path(args.export_undo), only_undo=True))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
