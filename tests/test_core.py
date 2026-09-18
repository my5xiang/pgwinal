from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.make_sample_wal import build_sample_wal  # noqa: E402
from pgwinal.core.xlog import WalScanner  # noqa: E402
from pgwinal.dictstore.schema import AttributeDef, DataDictionary, DictStore, RelationDef  # noqa: E402
from pgwinal.parse.engine import ParseOptions, WalParseEngine  # noqa: E402
from pgwinal.resultstore.resultdb import ResultStore  # noqa: E402
from pgwinal.reverse.sqlgen import SqlGenerator, SchemaChangeTracker, literal, quote_ident  # noqa: E402


class TestSql(unittest.TestCase):
    def test_literal(self):
        self.assertEqual(literal(None), "NULL")
        self.assertEqual(literal(1), "1")
        self.assertEqual(literal("a'b"), "'a''b'")
        self.assertEqual(quote_ident('a"b'), '"a""b"')

    def test_generate_insert(self):
        rel = RelationDef(
            rel_oid=1,
            schema_name="public",
            rel_name="t1",
            relfilenode=16400,
            reltablespace=1663,
            db_oid=16384,
            attributes=[
                AttributeDef(1, "id", 23, "int4"),
                AttributeDef(2, "name", 25, "text"),
            ],
        )
        gen = SqlGenerator(SchemaChangeTracker(DataDictionary()))
        do, undo, notes = gen.generate("INSERT", rel, {"id": 1, "name": "hello"})
        self.assertIn("INSERT INTO", do)
        self.assertIn("public", do)
        self.assertIn("DELETE FROM", undo)

    def test_generate_delete_ctid_without_old_tuple(self):
        rel = RelationDef(
            rel_oid=40484,
            schema_name="bdsy",
            rel_name="sys_log",
            relfilenode=47194,
            reltablespace=0,
            db_oid=32085,
            attributes=[AttributeDef(1, "id", 1043, "varchar")],
        )
        gen = SqlGenerator(SchemaChangeTracker(DataDictionary(relations=[rel])))
        do, undo, notes = gen.generate("DELETE", rel, {}, None, 0, 32085, 40484, ctid=(598, 13))
        self.assertIn("bdsy", do)
        self.assertIn("ctid", do)
        self.assertIn("(598,13)", do)
        self.assertIn("undo DELETE missing", undo)


class TestDictResolve(unittest.TestCase):
    def test_find_relation_oid_fallback_after_rewrite(self):
        d = DataDictionary(
            relations=[
                RelationDef(
                    rel_oid=40484,
                    schema_name="bdsy",
                    rel_name="sys_log",
                    relfilenode=47194,  # rewritten
                    reltablespace=0,
                    db_oid=32085,
                    attributes=[],
                )
            ]
        )
        rel, how = d.find_relation(40484, 32085)
        self.assertIsNotNone(rel)
        self.assertEqual(how, "rel_oid")
        self.assertEqual(rel.rel_name, "sys_log")

        # current relfilenode still wins
        rel2, how2 = d.find_relation(47194, 32085)
        self.assertEqual(how2, "relfilenode")
        self.assertEqual(rel2.rel_name, "sys_log")


class TestScan(unittest.TestCase):
    def test_scan_synthetic(self):
        wal = ROOT / "tests" / "sample" / "000000010000000000000001"
        build_sample_wal(wal)
        scanner = WalScanner([wal])
        recs = list(scanner.scan())
        self.assertGreaterEqual(len(recs), 2)
        rmids = {r.rmid for r in recs}
        self.assertIn(10, rmids)  # Heap
        self.assertIn(1, rmids)  # Transaction


class TestEngine(unittest.TestCase):
    def test_parse_end_to_end(self):
        wal = ROOT / "tests" / "sample" / "000000010000000000000001"
        build_sample_wal(wal)
        dictionary = DataDictionary(
            relations=[
                RelationDef(
                    rel_oid=1,
                    schema_name="public",
                    rel_name="t1",
                    relfilenode=16400,
                    reltablespace=1663,
                    db_oid=16384,
                    attributes=[
                        AttributeDef(1, "id", 23, "int4"),
                        AttributeDef(2, "name", 25, "text"),
                    ],
                )
            ]
        )
        out = ROOT / "tests" / "out_results.sqlite"
        if out.exists():
            out.unlink()
        engine = WalParseEngine(dictionary, ResultStore(out))
        report = engine.parse_paths([wal], ParseOptions(only_committed=True, skip_catalog=True))
        self.assertGreaterEqual(report["result_count"], 1)
        rows = engine.results.fetch(limit=10)
        self.assertTrue(any(r["op"] == "INSERT" for r in rows))
        insert_row = next(r for r in rows if r["op"] == "INSERT")
        self.assertIn("INSERT INTO", insert_row["do_sql"])


class TestDictStore(unittest.TestCase):
    def test_roundtrip(self):
        path = ROOT / "tests" / "out_dict.sqlite"
        if path.exists():
            path.unlink()
        store = DictStore(path)
        d = DataDictionary(pg_version="16.2")
        d.relations.append(
            RelationDef(
                rel_oid=10,
                schema_name="public",
                rel_name="a",
                relfilenode=11,
                reltablespace=1663,
                db_oid=16384,
                attributes=[AttributeDef(1, "c", 23, "int4")],
            )
        )
        store.save_dictionary(d)
        d2 = store.load_dictionary()
        self.assertEqual(len(d2.relations), 1)
        self.assertEqual(d2.relations[0].attributes[0].attname, "c")


if __name__ == "__main__":
    unittest.main()
