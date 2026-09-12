"""PostgreSQL WAL binary constants (aligned with PG 12–18 physical layout)."""

BLCKSZ = 8192
XLOG_PAGE_HDR_SIZE = 24
DEFAULT_WAL_SEG_SIZE = 16 * 1024 * 1024  # 16MB

# XLogPageHeader.xlp_info
XLP_FIRST_CONTINUE = 0x0001
XLP_LONG_HEADER = 0x0002

# XLogRecord.xl_info lower bits / heap opcodes (high nibble of xl_info for heap)
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

XLOG_HEAP2_MULTI_INSERT = 0x50

# Rmgr IDs (order from rmgrlist.h)
RMGR_NAMES = {
    0: "XLOG",
    1: "Transaction",
    2: "Storage",
    3: "CLOG",
    4: "Database",
    5: "Tablespace",
    6: "MultiXact",
    7: "RelMap",
    8: "Standby",
    9: "Heap2",
    10: "Heap",
    11: "Btree",
    12: "Hash",
    13: "Gin",
    14: "Gist",
    15: "Sequence",
    16: "SPGist",
    17: "BRIN",
    18: "CommitTs",
    19: "ReplicationOrigin",
    20: "Generic",
    21: "LogicalMessage",
}

RM_HEAP_ID = 10
RM_HEAP2_ID = 9
RM_XACT_ID = 1

# Transaction rmgr opcodes
XLOG_XACT_COMMIT = 0x00
XLOG_XACT_COMMIT_PREPARED = 0x10
XLOG_XACT_ABORT = 0x20
XLOG_XACT_ABORT_PREPARED = 0x30
XLOG_XACT_PREPARE = 0x40
XLOG_XACT_OPMASK = 0xE0

# xl_heap_insert flags
XLH_INSERT_CONTAINS_NEW_TUPLE = 0x08

# xl_heap_update flags
XLH_UPDATE_CONTAINS_OLD_TUPLE = 0x04
XLH_UPDATE_CONTAINS_OLD_KEY = 0x08
XLH_UPDATE_CONTAINS_NEW_TUPLE = 0x10
XLH_UPDATE_PREFIX_FROM_OLD = 0x20
XLH_UPDATE_SUFFIX_FROM_OLD = 0x40

# xl_heap_delete flags
XLH_DELETE_CONTAINS_OLD_TUPLE = 0x02
XLH_DELETE_CONTAINS_OLD_KEY = 0x04

# Heap tuple infomask
HEAP_HASNULL = 0x0001
HEAP_HASVARWIDTH = 0x0002
HEAP_HASEXTERNAL = 0x0004
HEAP_HASOID = 0x0008
HEAP_XMAX_KEYSHR_LOCK = 0x0010
HEAP_COMBOCID = 0x0020
HEAP_XMAX_EXCL_LOCK = 0x0040
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_XMIN_FROZEN = 0x1000
HEAP_KEYS_UPDATED = 0x2000
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000

VARATT_HEADER_SIZE = 1
VARATT_1B = 0x80  # 7-bit length
VARATT_1B_HUMAN_READABLE = 0x40  # 7-bit length
VARATT_1B_EXTERNAL = 0x40
VARATT_4B = 0x80  # high bit of first byte clear for 4B? Actually:
# On little-endian, varattrib_1b: va_header byte; if bit 7 set → 1-byte
# varattrib_4b: bit 7 clear → 4-byte header follows

# MaxHeapAttributeNumber
MAX_HEAP_ATTR_NUMBER = 1664
