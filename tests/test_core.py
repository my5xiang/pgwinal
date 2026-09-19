"""单元测试：crc32c / 帧扫描 / SQL 字面量 / 元组 deform。

运行：python -m unittest discover tests -v
真实数据测试（test_framing_real）在 testpg 数据存在时自动执行。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pgwal.crc32c import crc32c, record_crc_valid  # noqa: E402
from pgwal.sqlval import sql_literal  # noqa: E402
from pgwal.typereg import RawValue  # noqa: E402
from pgwal import profiles as P  # noqa: E402

WAL_DIR = Path(r"D:\mimo\pgwinal\testpg")
DICT = Path(r"D:\mimo\pgwinal\dict\pgwal_dict_aphx.sqlite")


class TestCrc32c(unittest.TestCase):
    def test_vector(self):
        # RFC 3720 / iSCSI 标准测试向量
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)
        self.assertEqual(crc32c(b""), 0x00000000)
        self.assertEqual(crc32c(b"a"), 0xC1D04330)

    def test_record_crc(self):
        # 构造一条合法记录（自校验）
        import struct
        header = struct.pack("<IIQBBHI", 24, 0, 0, 0, 0, 0, 0)
        body = b""
        crc = crc32c(body + header[:20])
        rec = struct.pack("<IIQBBHI", 24, 0, 0, 0, 0, 0, crc)
        self.assertTrue(record_crc_valid(rec))


class TestSqlLiteral(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(sql_literal("it's"), "'it''s'")
        self.assertEqual(sql_literal("a\\b"), "E'a\\\\b'")
        self.assertEqual(sql_literal(42), "42")
        self.assertEqual(sql_literal(None), "NULL")
        self.assertEqual(sql_literal(True), "TRUE")
        self.assertEqual(sql_literal(b"\x01\x02"), "'\\x0102'")

    def test_raw_not_executable(self):
        v = sql_literal(RawValue(b"x", "测试原因", "raw"))
        self.assertIn("不可执行", v)


class TestProfiles(unittest.TestCase):
    def test_pg12_heap2(self):
        p = P.get_profile(12)
        self.assertEqual(p.heap2["MULTI_INSERT"], 0x50)
        self.assertEqual(p.heap2["CLEAN"], 0x10)
        self.assertFalse(p.has_toplevel_xid_block)

    def test_pg17_heap2(self):
        p = P.get_profile(17)
        self.assertEqual(p.heap2["PRUNE_ON_ACCESS"], 0x10)
        self.assertEqual(p.heap2["MULTI_INSERT"], 0x50)

    def test_bimg_flags_versioned(self):
        p12 = P.get_profile(12)
        p15 = P.get_profile(15)
        # PG12: 0x02=IS_COMPRESSED；PG15: 0x04=PGLZ
        self.assertTrue(p12.image_is_compressed(0x02))
        self.assertFalse(p12.image_is_compressed(0x04))  # PG12 的 0x04 是 APPLY
        self.assertTrue(p15.image_is_compressed(0x04))
        self.assertTrue(p15.image_is_compressed(0x08))


class TestFramingReal(unittest.TestCase):
    """真实数据帧级验证（testpg 存在时执行）。"""

    def test_first_segments(self):
        if not WAL_DIR.exists():
            self.skipTest("testpg 数据不存在")
        from pgwal.xlogreader import WalStream, collect_wal_files
        files = collect_wal_files(WAL_DIR)[:2]
        stream = WalStream(files, profile=P.get_profile(12),
                           system_id=7579057487605995718)
        n = 0
        for rec in stream.iter_records():
            n += 1
            if n > 50000:
                break
        self.assertEqual(stream.stats["crc_ok"], n)
        self.assertEqual(stream.stats["prev_checked"], n - 1)


class TestXlsx(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile
        import zipfile
        from pgwal.xlsx import write_xlsx
        headers = ["id", "op", "备注"]
        rows = [(1, "INSERT", "中文内容&<tag>"), (2, "DELETE", None),
                (3, "UPDATE", "a" * 40000)]  # 超长截断 + 非法控制字符
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            path = f.name
        try:
            n = write_xlsx(path, headers, rows)
            self.assertEqual(n, 3)
            # 结构完整性：OOXML 必需部件
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                for part in ("[Content_Types].xml", "_rels/.rels",
                             "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                             "xl/worksheets/sheet1.xml"):
                    self.assertIn(part, names)
            # openpyxl 读取（Excel 兼容性事实标准）
            try:
                from openpyxl import load_workbook
                wb = load_workbook(path)
                ws = wb.active
                self.assertEqual([c.value for c in ws[1]], headers)
                self.assertEqual(ws.cell(row=2, column=2).value, "INSERT")
                self.assertEqual(ws.cell(row=2, column=3).value, "中文内容&<tag>")
                self.assertLessEqual(len(ws.cell(row=4, column=3).value), 32767)
            except ImportError:
                pass
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
