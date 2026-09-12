from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RelFileLocator:
    spc_oid: int
    db_oid: int
    rel_number: int

    def key(self) -> tuple[int, int, int]:
        return (self.spc_oid, self.db_oid, self.rel_number)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Rel({self.spc_oid}/{self.db_oid}/{self.rel_number})"


@dataclass
class XLogRecord:
    total_len: int
    xid: int
    prev: int
    info: int
    rmid: int
    crc: int
    start_lsn: int
    end_lsn: int
    data: bytes = b""  # payload after fixed header (block headers + main data)


@dataclass
class BlockRef:
    block_id: int
    fork_flags: int
    data_length: int
    has_image: bool
    has_data: bool
    same_rel: bool
    rlocator: Optional[RelFileLocator]
    block_num: int
    image_length: int = 0
    hole_offset: int = 0
    hole_length: int = 0
    bimg_info: int = 0
    payload: bytes = b""


@dataclass
class WalRecord:
    rec: XLogRecord
    blocks: list[BlockRef] = field(default_factory=list)
    main_data: bytes = b""

    @property
    def rmid(self) -> int:
        return self.rec.rmid

    @property
    def info(self) -> int:
        return self.rec.info

    @property
    def xid(self) -> int:
        return self.rec.xid


@dataclass
class ChangeRecord:
    """Logical row change ready for SQL generation / persistence."""

    lsn: int
    xid: int
    op: str  # INSERT | UPDATE | DELETE | MULTI_INSERT | OTHER
    spc_oid: int
    db_oid: int
    rel_number: int
    block_num: int = 0
    offset_num: int = 0
    do_sql: str = ""
    undo_sql: str = ""
    table_name: str = ""
    schema_name: str = ""
    row_data: dict = field(default_factory=dict)
    old_row_data: dict = field(default_factory=dict)
    is_catalog: bool = False
    commit_ts: Optional[str] = None
    notes: str = ""
