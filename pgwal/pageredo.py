"""页像重放引擎：FPI 播种 + DML/CLEAN redo —— UNDO 旧值的地基。

对齐 walminer 3.x imagemanage.c 的机制（见 开发方案.md §6.3.4）：
  1. FPI 播种：记录带块镜像 → 重建 8K 页（洞回填/解压）存入页库（FPI 为变更后状态）
  2. DML redo：无 FPI 的 INSERT/UPDATE/MULTI_INSERT 向页库 PageAddItem（新元组）
  3. CLEAN redo：行指针 redirect/dead/unused + PageRepairFragmentation
     （否则页像与真实页漂移，后续取的旧元组就是错的——walminer 同款关键点）
  4. 读元组：带 FPI 无 data 的记录直接从镜像读；UPDATE/DELETE 旧值查页库

页格式（bufpage.h，12–18 稳定）：
  PageHeader 24B：pd_lsn(8) pd_checksum(2) pd_flags(2) pd_lower(2) pd_upper(2)
                  pd_special(2) pd_pagesize_version(2) pd_prune_xid(4)
  ItemId 4B：lp_off=bit0-14, lp_flags=bit15-16, lp_len=bit17-31
    LP_UNUSED=0 LP_NORMAL=1 LP_REDIRECT=2 LP_DEAD=3
"""

from __future__ import annotations

import struct
from collections import OrderedDict
from typing import Optional

from . import profiles as P
from .pglz import pglz_decompress
from .xlogreader import BlockRef, Record, restore_block_image

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

PAGE_HEADER_SIZE = 24
LP_UNUSED, LP_NORMAL, LP_REDIRECT, LP_DEAD = 0, 1, 2, 3


def MAXALIGN(x: int) -> int:
    return (x + 7) & ~7


def _lp_get(page: bytes, offnum: int) -> tuple[int, int, int]:
    """返回 (lp_off, lp_flags, lp_len)。offnum 从 1 起。"""
    v = _U32.unpack_from(page, PAGE_HEADER_SIZE + 4 * (offnum - 1))[0]
    return v & 0x7FFF, (v >> 15) & 0x3, (v >> 17) & 0x7FFF


def _lp_set(page: bytearray, offnum: int, value: int):
    struct.pack_into("<I", page, PAGE_HEADER_SIZE + 4 * (offnum - 1), value)


def _lp_normal(off: int, ln: int) -> int:
    return (off & 0x7FFF) | (LP_NORMAL << 15) | ((ln & 0x7FFF) << 17)


def _lp_redirect(target: int) -> int:
    return (target & 0x7FFF) | (LP_REDIRECT << 15)


def _pd(page: bytes) -> tuple[int, int, int]:
    """(pd_lower, pd_upper, pd_special)"""
    lower, upper, special = struct.unpack_from("<HHH", page, 12)
    return lower, upper, special


def page_add_item(page: bytearray, item: bytes, offnum: int):
    """PageAddItem 等价：元组自 pd_upper 向下分配，行指针指向之。"""
    itemsz = MAXALIGN(len(item))
    lower, upper, _special = _pd(page)
    new_upper = upper - itemsz
    if new_upper < lower:
        raise ValueError("页空间不足（redo 异常）")
    lp_pos = PAGE_HEADER_SIZE + 4 * (offnum - 1)
    if lp_pos + 4 > lower:
        # 新增行指针：扩展 pd_lower（中间空隙清零）
        for i in range(lower, lp_pos + 4):
            page[i] = 0
        struct.pack_into("<H", page, 12, lp_pos + 4)
    _lp_set(page, offnum, _lp_normal(new_upper, itemsz))
    page[new_upper:new_upper + len(item)] = item
    struct.pack_into("<H", page, 14, new_upper)


def page_apply_prune(page: bytearray, redirected: list, nowdead: list, nowunused: list):
    """heap_page_prune_execute 等价（PG12）。"""
    lower, _upper, _special = _pd(page)
    max_lp = (lower - PAGE_HEADER_SIZE) // 4  # 现有行指针数（防御）
    for i in range(0, len(redirected), 2):
        off, target = redirected[i], redirected[i + 1]
        if 1 <= off <= max_lp and 1 <= target <= 0x7FFF:
            _lp_set(page, off, _lp_redirect(target))
    for off in nowdead:
        if 1 <= off <= max_lp:
            _lp_set(page, off, (LP_DEAD << 15))
    for off in nowunused:
        if 1 <= off <= max_lp:
            _lp_set(page, off, 0)
    _page_repair_fragmentation(page)


def _page_repair_fragmentation(page: bytearray):
    """PageRepairFragmentation 等价：LP_NORMAL 元组按行指针序压到页尾。"""
    lower, upper, special = _pd(page)
    nlps = (lower - PAGE_HEADER_SIZE) // 4
    items = []  # (offnum, off, len)
    for n in range(1, nlps + 1):
        off, flags, ln = _lp_get(page, n)
        if flags == LP_NORMAL and ln > 0:
            items.append((n, off, ln))
    if not items:
        struct.pack_into("<H", page, 14, special)
        return
    pos = special
    for n, off, ln in items:
        aln = MAXALIGN(ln)
        pos -= aln
        # 移动元组（含对齐填充）
        page[pos:pos + ln] = page[off:off + ln]
        if aln > ln:
            page[pos + ln:pos + aln] = b"\x00" * (aln - ln)
        _lp_set(page, n, _lp_normal(pos, ln))
    struct.pack_into("<H", page, 14, pos)


def read_tuple_from_page(page: bytes, offnum: int, follow_redirect=True) -> Optional[bytes]:
    """按行指针读元组（跟随 LP_REDIRECT）。返回 None 表示槽位无元组。"""
    hops = 0
    while True:
        if offnum < 1 or PAGE_HEADER_SIZE + 4 * offnum > len(page):
            return None
        off, flags, ln = _lp_get(page, offnum)
        if flags == LP_NORMAL:
            if ln == 0 or off + ln > len(page):
                return None
            return bytes(page[off:off + ln])
        if flags == LP_REDIRECT and follow_redirect:
            offnum = off
            hops += 1
            if hops > 32:
                return None
            continue
        return None


class PageStore:
    """(db, relfilenode, blkno) → 页像，LRU 上限控制内存。"""

    def __init__(self, max_pages: int = 60000):
        self.max_pages = max_pages
        self._pages: OrderedDict = OrderedDict()
        self.stats = {"seeded": 0, "evicted": 0, "hits": 0, "misses": 0}

    def put(self, key, page: bytes):
        self._pages[key] = bytearray(page)
        self._pages.move_to_end(key)
        self.stats["seeded"] += 1
        while len(self._pages) > self.max_pages:
            self._pages.popitem(last=False)
            self.stats["evicted"] += 1

    def get(self, key) -> Optional[bytearray]:
        pg = self._pages.get(key)
        if pg is not None:
            self._pages.move_to_end(key)
            self.stats["hits"] += 1
        else:
            self.stats["misses"] += 1
        return pg

    def __len__(self):
        return len(self._pages)


def restore_image(blk: BlockRef, profile: P.VersionProfile) -> Optional[bytes]:
    """块镜像 → 8K 页（含洞回填与 pglz 解压）。"""
    try:
        if profile.image_is_compressed(blk.bimg_info):
            if blk.hole_length == 0:
                raw = pglz_decompress(blk.image, P.BLCKSZ)
                return raw
            # 压缩且有洞：解压后回填
            decompressed = pglz_decompress(blk.image, P.BLCKSZ - blk.hole_length)
            page = bytearray(P.BLCKSZ)
            page[:blk.hole_offset] = decompressed[:blk.hole_offset]
            page[blk.hole_offset + blk.hole_length:] = decompressed[blk.hole_offset:]
            return bytes(page)
        return restore_block_image(blk)
    except Exception:
        return None
