"""PG 版本档案：WAL 二进制布局常量。

数据来源：postgres/postgres 官方仓库 REL_12~REL_18_STABLE 的
  src/include/access/xlogrecord.h / heapam_xlog.h / xlog_internal.h / xact.h
以及 REL_12_STABLE 的 src/backend/access/transam/xlogreader.c（DecodeXLogRecord /
ValidXLogRecord 的逐行对照）。已逐项核实，见 开发方案.md §7.2 / 附录 A。

【纪律】禁止凭记忆修改本文件数值；任何改动必须重新对照官方头文件并在
tests 中更新验收基准。
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 帧级（12–18 全系一致，已核实）

BLCKSZ = 8192
XLOG_BLCKSZ = 8192
SIZEOF_XLOG_RECORD = 24          # offsetof(xl_crc)+4
SIZEOF_XLOG_SHORT_PHD = 24       # MAXALIGN(20)
SIZEOF_XLOG_LONG_PHD = 40        # MAXALIGN(36)
SIZEOF_HEAP_HEADER = 5           # xl_heap_header: infomask2/infomask/hoff
SIZEOF_HEAP_TUPLE_HEADER = 23   # HeapTupleHeaderData 固定部分
DEFAULT_WAL_SEG_SIZE = 16 * 1024 * 1024

# WAL 页 magic（各版本不同！来源：REL_12~18_STABLE xlog_internal.h）
XLOG_PAGE_MAGIC_BY_VERSION = {
    12: 0xD101, 13: 0xD106, 14: 0xD10D, 15: 0xD110,
    16: 0xD113, 17: 0xD116, 18: 0xD118,
}
# 兼容旧引用（PG12）
XLOG_PAGE_MAGIC = 0xD101

XLP_FIRST_IS_CONTRECORD = 0x0001
XLP_LONG_HEADER = 0x0002
XLP_BKP_REMOVABLE = 0x0004       # PG12；PG13+ 改为 XLP_FIRST_IS_OVERWRITE_CONTRECORD
XLP_ALL_FLAGS = 0x0007

# XLogRecordBlockHeader.fork_flags 高 4 位
BKPBLOCK_FORK_MASK = 0x0F
BKPBLOCK_HAS_IMAGE = 0x10
BKPBLOCK_HAS_DATA = 0x20
BKPBLOCK_WILL_INIT = 0x40
BKPBLOCK_SAME_REL = 0x80

# bimg_info —— 分版本（已对照官方头文件 + 真实数据实测）：
#   PG12–14: HAS_HOLE=0x01, IS_COMPRESSED=0x02, APPLY=0x04（单一压缩标志，仅 pglz）
#   PG15+  : HAS_HOLE=0x01, APPLY=0x02, COMPRESS_PGLZ=0x04, LZ4=0x08, ZSTD=0x10
# 注意：0x02/0x04 的语义在 PG15 前后互换，这是实测踩过的坑（Btree 压缩 FPI 记录）。
BKPIMAGE_HAS_HOLE = 0x01
BKPIMAGE_IS_COMPRESSED_LEGACY = 0x02   # PG12–14
BKPIMAGE_APPLY_LEGACY = 0x04            # PG12–14
BKPIMAGE_APPLY = 0x02                    # PG15+
BKPIMAGE_COMPRESS_PGLZ = 0x04            # PG15+
BKPIMAGE_COMPRESS_LZ4 = 0x08             # PG15+
BKPIMAGE_COMPRESS_ZSTD = 0x10            # PG15+

XLR_BLOCK_ID_DATA_SHORT = 255
XLR_BLOCK_ID_DATA_LONG = 254
XLR_BLOCK_ID_ORIGIN = 253
XLR_BLOCK_ID_TOPLEVEL_XID = 252  # PG13+ 才进入 xlogreader 解码；PG12 无此块
XLR_MAX_BLOCK_ID = 32

XLR_INFO_MASK = 0x0F

# ---------------------------------------------------------------- rmgr id（12–18 稳定）

RM_XLOG_ID = 0
RM_XACT_ID = 1
RM_SMGR_ID = 2
RM_CLOG_ID = 3
RM_DBASE_ID = 4
RM_TBLSPC_ID = 5
RM_MULTIXACT_ID = 6
RM_RELMAP_ID = 7
RM_STANDBY_ID = 8
RM_HEAP2_ID = 9
RM_HEAP_ID = 10
RM_MAX_ID = 22  # PG12: RM_LOGICALMSG_ID=21, RM_NEXT_ID=22

RMGR_NAMES = {
    0: "XLOG", 1: "Transaction", 2: "Storage", 3: "CLOG", 4: "Database",
    5: "Tablespace", 6: "MultiXact", 7: "RelMap", 8: "Standby", 9: "Heap2",
    10: "Heap", 11: "Btree", 12: "Hash", 13: "Gin", 14: "Gist", 15: "Sequence",
    16: "SPGist", 17: "BRIN", 18: "CommitTs", 19: "ReplicationOrigin",
    20: "Generic", 21: "LogicalMessage",
}

# ---------------------------------------------------------------- Heap rmgr opcode（12–18 一致）

XLOG_HEAP_OPMASK = 0x70
XLOG_HEAP_INIT_PAGE = 0x80
XLOG_HEAP_INSERT = 0x00
XLOG_HEAP_DELETE = 0x10
XLOG_HEAP_UPDATE = 0x20
XLOG_HEAP_TRUNCATE = 0x30
XLOG_HEAP_HOT_UPDATE = 0x40
XLOG_HEAP_CONFIRM = 0x50
XLOG_HEAP_LOCK = 0x60
XLOG_HEAP_INPLACE = 0x70

# xl_heap_insert flags
XLH_INSERT_IS_SPECULATIVE = 1 << 2

# xl_heap_update flags（12–18 一致）
XLH_UPDATE_CONTAINS_OLD_TUPLE = 1 << 2
XLH_UPDATE_CONTAINS_OLD_KEY = 1 << 3
XLH_UPDATE_CONTAINS_NEW_TUPLE = 1 << 4
XLH_UPDATE_PREFIX_FROM_OLD = 1 << 5
XLH_UPDATE_SUFFIX_FROM_OLD = 1 << 6
XLH_UPDATE_CONTAINS_OLD = XLH_UPDATE_CONTAINS_OLD_TUPLE | XLH_UPDATE_CONTAINS_OLD_KEY

# xl_heap_delete flags
XLH_DELETE_CONTAINS_OLD_TUPLE = 1 << 1
XLH_DELETE_CONTAINS_OLD_KEY = 1 << 2
XLH_DELETE_CONTAINS_OLD = XLH_DELETE_CONTAINS_OLD_TUPLE | XLH_DELETE_CONTAINS_OLD_KEY

# 结构体尺寸（offsetof 公式对照头文件）
SIZEOF_HEAP_INSERT = 3    # offnum u16 + flags u8
SIZEOF_HEAP_DELETE = 8    # xmax u32 + offnum u16 + infobits u8 + flags u8
SIZEOF_HEAP_UPDATE = 14   # old_xmax u32 + old_offnum u16 + old_infobits u8 + flags u8 + new_xmax u32 + new_offnum u16

# ---------------------------------------------------------------- XACT rmgr（xact.h，12–18 一致）

XLOG_XACT_OPMASK = 0x70
XLOG_XACT_COMMIT = 0x00
XLOG_XACT_PREPARE = 0x10
XLOG_XACT_ABORT = 0x20
XLOG_XACT_COMMIT_PREPARED = 0x30
XLOG_XACT_ABORT_PREPARED = 0x40
XLOG_XACT_ASSIGNMENT = 0x50
XLOG_XACT_HAS_INFO = 0x80  # info 高位：main data 含 xl_xact_xinfo

# XLOG rmgr 操作码（catalog/pg_control.h，使用完整 info 字节，非 0x70 掩码）
XLOG_CHECKPOINT_SHUTDOWN = 0x00
XLOG_CHECKPOINT_ONLINE = 0x10
XLOG_NOOP = 0x20
XLOG_NEXTOID = 0x30
XLOG_SWITCH = 0x40
XLOG_BACKUP_END = 0x50
XLOG_PARAMETER_CHANGE = 0x60
XLOG_RESTORE_POINT = 0x70
XLOG_FPW_CHANGE = 0x80
XLOG_END_OF_RECOVERY = 0x90
XLOG_FPI_FOR_HINT = 0xA0
XLOG_FPI = 0xB0

# ---------------------------------------------------------------- Heap2 opcode（分版本，已对照头文件）

HEAP2_COMMON = {
    "REWRITE": 0x00,
    "VISIBLE": 0x40,
    "MULTI_INSERT": 0x50,
    "LOCK_UPDATED": 0x60,
    "NEW_CID": 0x70,
}

HEAP2_BY_VERSION = {
    12: {**HEAP2_COMMON, "CLEAN": 0x10, "FREEZE_PAGE": 0x20, "CLEANUP_INFO": 0x30},
    13: {**HEAP2_COMMON, "CLEAN": 0x10, "FREEZE_PAGE": 0x20, "CLEANUP_INFO": 0x30},
    14: {**HEAP2_COMMON, "PRUNE": 0x10, "VACUUM": 0x20, "FREEZE_PAGE": 0x30},
    15: {**HEAP2_COMMON, "PRUNE": 0x10, "VACUUM": 0x20, "FREEZE_PAGE": 0x30},
    16: {**HEAP2_COMMON, "PRUNE": 0x10, "VACUUM": 0x20, "FREEZE_PAGE": 0x30},
    17: {**HEAP2_COMMON, "PRUNE_ON_ACCESS": 0x10, "PRUNE_VACUUM_SCAN": 0x20, "PRUNE_VACUUM_CLEANUP": 0x30},
    18: {**HEAP2_COMMON, "PRUNE_ON_ACCESS": 0x10, "PRUNE_VACUUM_SCAN": 0x20, "PRUNE_VACUUM_CLEANUP": 0x30},
}


@dataclass
class VersionProfile:
    major: int
    heap2: dict = field(default_factory=dict)
    # PG13+ 记录体可含 XLR_BLOCK_ID_TOPLEVEL_XID 块；PG12 用 XACT_ASSIGNMENT
    has_toplevel_xid_block: bool = False
    # FPI 压缩：PG15 起有方法位（pglz/lz4/zstd）；12–14 为单一 IS_COMPRESSED 位（仅 pglz）
    fpi_compression_methods: bool = False

    @property
    def page_magic(self) -> int:
        """WAL 页 magic（各版本不同）。"""
        return XLOG_PAGE_MAGIC_BY_VERSION.get(self.major, XLOG_PAGE_MAGIC)

    def image_is_compressed(self, bimg_info: int) -> bool:
        if self.fpi_compression_methods:
            return bool(bimg_info & (BKPIMAGE_COMPRESS_PGLZ | BKPIMAGE_COMPRESS_LZ4 | BKPIMAGE_COMPRESS_ZSTD))
        return bool(bimg_info & BKPIMAGE_IS_COMPRESSED_LEGACY)

    @property
    def heap2_multi_insert(self) -> int:
        return self.heap2["MULTI_INSERT"]


def get_profile(major: int) -> VersionProfile:
    if major not in HEAP2_BY_VERSION:
        raise ValueError(f"不支持的 PG 版本: {major}（支持 12–18）")
    return VersionProfile(
        major=major,
        heap2=dict(HEAP2_BY_VERSION[major]),
        has_toplevel_xid_block=major >= 13,
        fpi_compression_methods=major >= 15,
    )
