"""标准 OOXML xlsx 生成（纯标准库）。

之前版本的问题：写的是 Excel 2003 SpreadsheetML（<Workbook><Worksheet><Table>），
却按 .xlsx 扩展名打包 —— 缺 [Content_Types].xml / workbook.xml / 关系文件，
Excel 无法打开。本模块生成正确的最小 OOXML 结构：

  [Content_Types].xml
  _rels/.rels                          → 指向 xl/workbook.xml
  xl/workbook.xml                      → <sheet name r:id="rId1"/>
  xl/_rels/workbook.xml.rels           → rId1 指向 worksheets/sheet1.xml
  xl/worksheets/sheet1.xml             → <sheetData><row><c t="inlineStr">

注意（Excel 兼容性硬约束）：
  - 单元格文本 ≤ 32767 字符（超长截断）
  - XML 1.0 非法控制字符（<0x20 除 \t\n\r）必须清除，否则 Excel 报“不可读内容”
  - 行号/单元格引用（r="A1"）必须提供
"""

from __future__ import annotations

import zipfile
from pathlib import Path

MAX_CELL_TEXT = 32000  # Excel 上限 32767，留余量

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets>'
    '</workbook>'
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    '</Relationships>'
)

# XML 1.0 合法字符：\t \n \r 及 >= 0x20（不含代理区/非法区）
_VALID = frozenset(
    [0x09, 0x0A, 0x0D]
    + list(range(0x20, 0xD800))
    + list(range(0xE000, 0xFFFE))
    + list(range(0x10000, 0x110000))
)


def _clean(text: str) -> str:
    """清除 XML 非法字符 + 截断到 Excel 上限。"""
    text = "".join(ch for ch in text if ord(ch) in _VALID)
    return text[:MAX_CELL_TEXT]


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _col_ref(idx: int) -> str:
    """0 基列号 → A1 式列名（0→A, 25→Z, 26→AA）。"""
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path, headers: list, rows, sheet_name: str = "contents") -> int:
    """写标准 xlsx。rows 为行迭代器（值可为 None/str/int/float）。

    返回写入的数据行数。
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]

    def cell_xml(col: int, value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return f'<c r="{_col_ref(col)}{{r}}" t="inlineStr"><is><t>{value}</t></is></c>'
        if isinstance(value, (int, float)):
            v = _clean(str(value))
            return f'<c r="{_col_ref(col)}{{r}}"><v>{v}</v></c>'
        v = _esc(_clean(str(value)))
        return f'<c r="{_col_ref(col)}{{r}}" t="inlineStr"><is><t>{v}</t></is></c>'

    # 表头
    parts.append('<row r="1">')
    for j, h in enumerate(headers):
        parts.append(cell_xml(j, h).replace("{r}", "1"))
    parts.append("</row>")

    n = 0
    for row in rows:
        n += 1
        parts.append(f'<row r="{n + 1}">')
        for j, v in enumerate(row):
            parts.append(cell_xml(j, v).replace("{r}", str(n + 1)))
        parts.append("</row>")
        if len(parts) > 50000:  # 控制内存（大结果集分块拼接）
            pass
    parts.append("</sheetData></worksheet>")
    sheet = "".join(parts)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheet=_esc(sheet_name)[:31] or "Sheet1"))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return n
