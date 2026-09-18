from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..constants import (
    HEAP_XMAX_INVALID,
    RM_HEAP_ID,
    RM_HEAP2_ID,
    RM_XACT_ID,
    XLH_DELETE_CONTAINS_OLD_KEY,
    XLH_DELETE_CONTAINS_OLD_TUPLE,
    XLH_INSERT_CONTAINS_NEW_TUPLE,
    XLH_UPDATE_CONTAINS_NEW_TUPLE,
    XLH_UPDATE_CONTAINS_OLD_KEY,
    XLH_UPDATE_CONTAINS_OLD_TUPLE,
    XLOG_HEAP_CONFIRM,
    XLOG_HEAP_DELETE,
    XLOG_HEAP_HOT_UPDATE,
    XLOG_HEAP_INSERT,
    XLOG_HEAP_LOCK,
    XLOG_HEAP_OPMASK,
    XLOG_HEAP_UPDATE,
    XLOG_HEAP2_MULTI_INSERT,
    XLOG_XACT_COMMIT,
    XLOG_XACT_COMMIT_PREPARED,
)
from ..core.heaptuple import TupleData, decode_heap_header_fixed
from ..core.models import ChangeRecord, WalRecord
from ..core.xlog import WalScanner, collect_wal_files
from ..dictstore.schema import DataDictionary, DictStore, RelationDef
from ..reverse.sqlgen import SchemaChangeTracker, SqlGenerator
from ..resultstore.resultdb import ResultStore


@dataclass
class ParseOptions:
    only_committed: bool = True
    skip_catalog: bool = True
    include_ops: tuple[str, ...] = ("INSERT", "UPDATE", "DELETE", "MULTI_INSERT")
    max_records: int = 0  # 0 = unlimited


class WalParseEngine:
    """End-to-end parse pipeline: WAL files + dictionary → results DB."""

    def __init__(
        self,
        dictionary: DataDictionary,
        result_store: Optional[ResultStore] = None,
        seg_size: int = 16 * 1024 * 1024,
    ):
        self.dictionary = dictionary
        self.results = result_store or ResultStore()
        self.tracker = SchemaChangeTracker(dictionary)
        self.sqlgen = SqlGenerator(self.tracker)
        self.seg_size = seg_size
        self.stats = {
            "records": 0,
            "heap_changes": 0,
            "committed_xids": 0,
            "commits": 0,
            "errors": 0,
        }

    def parse_paths(
        self,
        paths: list[Path | str],
        options: Optional[ParseOptions] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict:
        options = options or ParseOptions()
        files: list[Path] = []
        missing: list[str] = []
        for p in paths:
            path = Path(p)
            found = collect_wal_files(path)
            if not found and not path.exists():
                missing.append(str(path))
                continue
            files.extend(found)
        files = sorted(set(files), key=lambda x: x.name.lower())
        if missing:
            log_partial = f"跳过不存在的路径 {len(missing)} 个: " + ", ".join(Path(m).name for m in missing[:8])
            if progress:
                progress(log_partial)
        if not files:
            raise FileNotFoundError(
                "未找到可解析的 WAL 文件"
                + (f"（以下路径不存在: {', '.join(missing[:5])}）" if missing else "")
            )
        for f in files:
            self.results.add_wal_file(str(f))

        def log(msg: str):
            if progress:
                progress(msg)

        log(f"开始解析，共 {len(files)} 个文件")
        try:
            self.results.clear_contents()
        except Exception:
            pass

        # Pass 1: collect commit xids
        commit_xids: set[int] = set()
        commit_ts: dict[int, str] = {}
        filter_uncommitted = options.only_committed
        if options.only_committed:
            log("Pass1: 收集事务提交信息 ...")
            scanner = WalScanner(files, seg_size=self.seg_size)
            for rec in scanner.scan(progress_cb=log):
                if rec.rmid == RM_XACT_ID:
                    op = rec.info & 0xE0
                    if op in (XLOG_XACT_COMMIT, XLOG_XACT_COMMIT_PREPARED):
                        commit_xids.add(rec.xid)
                        commit_ts[rec.xid] = self._parse_commit_ts(rec)
                        self.stats["commits"] += 1
                elif rec.rmid in (RM_HEAP_ID, RM_HEAP2_ID):
                    self.stats["heap_records_pass1"] = self.stats.get("heap_records_pass1", 0) + 1
            self.stats["committed_xids"] = len(commit_xids)
            log(f"Pass1 完成: commits={len(commit_xids)}")
            if not commit_xids and self.stats.get("heap_records_pass1", 0) > 0:
                filter_uncommitted = False
                msg = (
                    "提供的 WAL 中未找到 COMMIT 记录（可能段文件不完整），"
                    "将解析全部堆变更（含可能未提交事务）。"
                )
                self.stats["commit_filter_disabled"] = True
                self.tracker.notes.append(msg)
                log("警告: " + msg)

        # Pass 2: decode heap changes
        log("Pass2: 解析堆变更并生成 SQL ...")
        scanner = WalScanner(files, seg_size=self.seg_size)
        pending: list[ChangeRecord] = []
        total = 0
        for rec in scanner.scan():
            if options.max_records and total >= options.max_records:
                break
            total += 1
            self.stats["records"] = total
            if total % 20000 == 0:
                log(f"... 已扫描 {total} 条记录，已生成 {self.stats['heap_changes']} 条变更")
            if filter_uncommitted and rec.xid not in commit_xids:
                # still allow if xid==0
                if rec.xid != 0:
                    continue
            try:
                ch = self._decode_record(rec, options, commit_ts.get(rec.xid))
            except Exception as e:
                self.stats["errors"] += 1
                self.tracker.notes.append(f"decode error xid={rec.xid} rmid={rec.rmid}: {e}")
                continue
            if ch is None:
                continue
            if isinstance(ch, list):
                pending.extend(ch)
            else:
                pending.append(ch)
            if len(pending) >= 500:
                self.results.insert_changes(pending)
                pending = []
        if pending:
            self.results.insert_changes(pending)

        report = self.tracker.report()
        summary = self.results.summary()
        self.results.set_meta("stats", json.dumps({**self.stats, **summary, "schema": report}, ensure_ascii=False))
        log(
            f"解析完成: changes={self.stats['heap_changes']} commits={self.stats['commits']} "
            f"results={self.results.count()} errors={self.stats['errors']}"
        )
        return {
            "stats": self.stats,
            "summary": summary,
            "schema_report": report,
            "result_count": self.results.count(),
            "files": [str(f) for f in files],
        }

    def _parse_commit_ts(self, rec: WalRecord) -> Optional[str]:
        data = rec.main_data
        if len(data) < 8:
            return None
        # xact commit main data: xl_xact_commit_cutoff? Actually:
        # typedef struct xl_xact_commit { XLogRecPtr xact_time; ... } PG10+
        import struct
        from datetime import datetime, timedelta

        try:
            xact_time = struct.unpack_from("<Q", data, 0)[0]
            # microseconds since PG epoch
            return (datetime(2000, 1, 1) + timedelta(microseconds=xact_time)).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )
        except Exception:
            return None

    def _decode_record(
        self, rec: WalRecord, options: ParseOptions, cts: Optional[str]
    ) -> ChangeRecord | list[ChangeRecord] | None:
        if rec.rmid == RM_HEAP_ID:
            op = rec.info & XLOG_HEAP_OPMASK
            if op == XLOG_HEAP_INSERT:
                return self._decode_insert(rec, options, cts)
            if op in (XLOG_HEAP_UPDATE, XLOG_HEAP_HOT_UPDATE):
                return self._decode_update(rec, options, cts, hot=(op == XLOG_HEAP_HOT_UPDATE))
            if op == XLOG_HEAP_DELETE:
                return self._decode_delete(rec, options, cts)
            return None
        if rec.rmid == RM_HEAP2_ID:
            op = rec.info & XLOG_HEAP_OPMASK
            if op == XLOG_HEAP2_MULTI_INSERT:
                return self._decode_multi_insert(rec, options, cts)
        return None

    def _relation(self, rec: WalRecord) -> tuple[int, int, int, Optional[RelationDef]]:
        for b in rec.blocks:
            if b.rlocator:
                r = b.rlocator
                rel = self.tracker.resolve(r.spc_oid, r.db_oid, r.rel_number)
                return r.spc_oid, r.db_oid, r.rel_number, rel
        return 0, 0, 0, None

    def _decode_insert(
        self, rec: WalRecord, options: ParseOptions, cts: Optional[str]
    ) -> Optional[ChangeRecord]:
        spc, db, relno, rel = self._relation(rec)
        if options.skip_catalog and rel and (
            rel.schema_name.startswith("pg_") or rel.schema_name == "information_schema"
        ):
            return None
        if rel is None and not rec.blocks:
            return None

        main = rec.main_data
        if len(main) < 3:
            return None
        # xl_heap_insert: OffsetNumber offnum; uint8 flags; (uint16 xl_heap_header in block)
        import struct

        offnum = struct.unpack_from("<H", main, 0)[0]
        flags = main[2]
        # find tuple payload: block 0 data is xl_heap_header + tuple
        payload = b""
        block_num = 0
        for b in rec.blocks:
            if b.has_data and b.payload:
                payload = b.payload
                block_num = b.block_num
                break
        if len(payload) < 5:
            return None
        td = decode_heap_header_fixed(payload)
        # values from user data after xl_heap_header
        from ..core.heaptuple import decode_tuple_values

        values = decode_tuple_values(td, rel, payload[5:])
        do, undo, notes = self.sqlgen.generate(
            "INSERT",
            rel,
            values,
            None,
            spc,
            db,
            relno,
            where_keys=None,
            ctid=(block_num, offnum),
            resolve_how=getattr(self.tracker, "last_resolve_how", "none"),
        )
        # refine undo/delete by non-null key candidates
        if rel and values:
            key_where = {
                k: v
                for k, v in values.items()
                if v is not None and not (isinstance(v, dict))
            }
            if key_where:
                from ..reverse.sqlgen import literal, quote_ident

                where = "WHERE " + " AND ".join(
                    f"{quote_ident(k)} = {literal(v)}" for k, v in key_where.items()
                )
                undo = f"DELETE FROM {rel.qualified_name()} {where};"
        self.stats["heap_changes"] += 1
        return ChangeRecord(
            lsn=rec.rec.start_lsn,
            xid=rec.xid,
            op="INSERT",
            spc_oid=spc,
            db_oid=db,
            rel_number=relno,
            block_num=block_num,
            offset_num=offnum,
            do_sql=do,
            undo_sql=undo,
            table_name=rel.rel_name if rel else "",
            schema_name=rel.schema_name if rel else "",
            row_data=values,
            is_catalog=bool(rel and rel.schema_name.startswith("pg_")),
            commit_ts=cts,
            notes=notes,
        )

    def _decode_update(
        self, rec: WalRecord, options: ParseOptions, cts: Optional[str], hot: bool
    ) -> Optional[ChangeRecord]:
        spc, db, relno, rel = self._relation(rec)
        if options.skip_catalog and rel and rel.schema_name.startswith("pg_"):
            return None
        main = rec.main_data
        if len(main) < 14:
            return None
        import struct

        old_xmax, old_off, old_infobits, flags, new_xmax, new_off = struct.unpack_from(
            "<IHBBIH", main, 0
        )
        pos = 14
        old_values: dict = {}
        new_values: dict = {}
        from ..core.heaptuple import decode_tuple_values

        # optional prefix/suffix uint16s
        prefix = suffix = 0
        if flags & 0x20 and pos + 2 <= len(main):
            prefix = struct.unpack_from("<H", main, pos)[0]
            pos += 2
        if flags & 0x40 and pos + 2 <= len(main):
            suffix = struct.unpack_from("<H", main, pos)[0]
            pos += 2

        if flags & XLH_UPDATE_CONTAINS_OLD_TUPLE and pos + 5 <= len(main):
            td_old = decode_heap_header_fixed(main[pos:])
            # estimate length: remaining until we try new tuple — ambiguous; use rest split heuristically
            # Prefer: old tuple payload length is not explicit; if CONTAINS_NEW_TUPLE, try common split
            old_payload = main[pos + 5 :]
            old_values = decode_tuple_values(td_old, rel, old_payload)
            pos = len(main)  # consumed
        elif flags & XLH_UPDATE_CONTAINS_OLD_KEY and pos + 5 <= len(main):
            td_old = decode_heap_header_fixed(main[pos:])
            old_values = decode_tuple_values(td_old, rel, main[pos + 5 :])
            pos = len(main)

        # new tuple from block data
        for b in rec.blocks:
            if b.has_data and b.payload and len(b.payload) >= 5:
                # If both old and new are in main, new may be in block
                td_new = decode_heap_header_fixed(b.payload)
                new_values = decode_tuple_values(td_new, rel, b.payload[5:])
                break

        if not new_values and flags & XLH_UPDATE_CONTAINS_NEW_TUPLE and pos < len(main):
            td_new = decode_heap_header_fixed(main[pos:])
            new_values = decode_tuple_values(td_new, rel, main[pos + 5 :])

        do, undo, notes = self.sqlgen.generate(
            "UPDATE",
            rel,
            new_values,
            old_values or None,
            spc,
            db,
            relno,
            where_keys=None,
            ctid=(rec.blocks[0].block_num if rec.blocks else 0, new_off or old_off),
            resolve_how=getattr(self.tracker, "last_resolve_how", "none"),
        )
        if hot:
            notes = (notes + "; " if notes else "") + "HOT_UPDATE"
        self.stats["heap_changes"] += 1
        return ChangeRecord(
            lsn=rec.rec.start_lsn,
            xid=rec.xid,
            op="UPDATE",
            spc_oid=spc,
            db_oid=db,
            rel_number=relno,
            block_num=rec.blocks[0].block_num if rec.blocks else 0,
            offset_num=new_off or old_off,
            do_sql=do,
            undo_sql=undo,
            table_name=rel.rel_name if rel else "",
            schema_name=rel.schema_name if rel else "",
            row_data=new_values,
            old_row_data=old_values,
            is_catalog=bool(rel and rel.schema_name.startswith("pg_")),
            commit_ts=cts,
            notes=notes,
        )

    def _decode_delete(
        self, rec: WalRecord, options: ParseOptions, cts: Optional[str]
    ) -> Optional[ChangeRecord]:
        spc, db, relno, rel = self._relation(rec)
        if options.skip_catalog and rel and rel.schema_name.startswith("pg_"):
            return None
        main = rec.main_data
        if len(main) < 8:
            return None
        import struct

        xmax, offnum, infobits, flags = struct.unpack_from("<IHBB", main, 0)
        old_values: dict = {}
        from ..core.heaptuple import decode_tuple_values

        if flags & (XLH_DELETE_CONTAINS_OLD_TUPLE | XLH_DELETE_CONTAINS_OLD_KEY):
            if len(main) >= 8 + 5:
                td = decode_heap_header_fixed(main[8:])
                old_values = decode_tuple_values(td, rel, main[13:])

        do, undo, notes = self.sqlgen.generate(
            "DELETE",
            rel,
            {},
            old_values or None,
            spc,
            db,
            relno,
            where_keys=None,
            ctid=(rec.blocks[0].block_num if rec.blocks else 0, offnum),
            resolve_how=getattr(self.tracker, "last_resolve_how", "none"),
        )
        self.stats["heap_changes"] += 1
        return ChangeRecord(
            lsn=rec.rec.start_lsn,
            xid=rec.xid,
            op="DELETE",
            spc_oid=spc,
            db_oid=db,
            rel_number=relno,
            block_num=rec.blocks[0].block_num if rec.blocks else 0,
            offset_num=offnum,
            do_sql=do,
            undo_sql=undo,
            table_name=rel.rel_name if rel else "",
            schema_name=rel.schema_name if rel else "",
            row_data={},
            old_row_data=old_values,
            is_catalog=bool(rel and rel.schema_name.startswith("pg_")),
            commit_ts=cts,
            notes=notes,
        )

    def _decode_multi_insert(
        self, rec: WalRecord, options: ParseOptions, cts: Optional[str]
    ) -> list[ChangeRecord]:
        out: list[ChangeRecord] = []
        spc, db, relno, rel = self._relation(rec)
        if options.skip_catalog and rel and rel.schema_name.startswith("pg_"):
            return []
        main = rec.main_data
        if len(main) < 3:
            return []
        import struct

        flags = main[0]
        ntuples = struct.unpack_from("<H", main, 1)[0]
        # block data: sequence of xl_multi_insert_tuple + data
        payload = b""
        block_num = 0
        for b in rec.blocks:
            if b.has_data and b.payload:
                payload = b.payload
                block_num = b.block_num
                break
        pos = 0
        from ..core.heaptuple import decode_tuple_values

        for i in range(ntuples):
            # SizeOfMultiInsertTuple = 7 (2+2+2+1), then tuple data, then pad to MAXALIGN
            if pos + 7 > len(payload):
                break
            datalen, infomask2, infomask, hoff = struct.unpack_from("<HHHB", payload, pos)
            # actually layout: datalen, t_infomask2, t_infomask, t_hoff
            # struct is uint16 datalen; uint16 infomask2; uint16 infomask; uint8 hoff → 7 bytes
            pos += 7
            # pad to MAXALIGN(7)=8
            pos = (pos + 7) // 8 * 8 if False else pos  # packing is tight in WAL
            # In PostgreSQL, xl_multi_insert_tuple is MAXALIGNed in the stream
            # After reading struct, next tuple data of `datalen` bytes follows immediately
            # then padding to MAXALIGN for next struct.
            if pos + datalen > len(payload):
                chunk = payload[pos:]
                pos = len(payload)
            else:
                chunk = payload[pos : pos + datalen]
                pos += datalen
            # pad
            while pos % 8 != 0 and pos < len(payload):
                pos += 1
            td = TupleData(
                t_xmin=0,
                t_xmax=0,
                t_cid=0,
                t_ctid_block=block_num,
                t_ctid_offset=0,
                t_infomask2=infomask2,
                t_infomask=infomask,
                t_hoff=hoff,
                user_data=chunk,
                attnum=infomask2 & 0x07FF,
            )
            values = decode_tuple_values(td, rel, chunk)
            do, undo, notes = self.sqlgen.generate(
                "MULTI_INSERT",
                rel,
                values,
                None,
                spc,
                db,
                relno,
                ctid=(block_num, i),
                resolve_how=getattr(self.tracker, "last_resolve_how", "none"),
            )
            self.stats["heap_changes"] += 1
            out.append(
                ChangeRecord(
                    lsn=rec.rec.start_lsn + i,
                    xid=rec.xid,
                    op="MULTI_INSERT",
                    spc_oid=spc,
                    db_oid=db,
                    rel_number=relno,
                    block_num=block_num,
                    offset_num=i,
                    do_sql=do,
                    undo_sql=undo,
                    table_name=rel.rel_name if rel else "",
                    schema_name=rel.schema_name if rel else "",
                    row_data=values,
                    is_catalog=bool(rel and rel.schema_name.startswith("pg_")),
                    commit_ts=cts,
                    notes=notes,
                )
            )
        return out
