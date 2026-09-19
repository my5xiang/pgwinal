"""解析引擎：两遍扫描编排（Pass1 事务收集 / Pass2 DML 解码）。"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from . import profiles as P
from .decode import Change, HeapDecoder, commit_ts_from_record, parse_xact_assignment
from .dictstore import DataDictionary
from .resultstore import ResultStore
from .xlogreader import WalStream, collect_wal_files


class Engine:
    def __init__(self, dictionary: DataDictionary, result: ResultStore,
                 only_committed=True, progress: Optional[Callable] = None,
                 max_pages: int = 60000):
        self.dict = dictionary
        self.result = result
        self.only_committed = only_committed
        self.progress = progress or (lambda msg: None)
        self.max_pages = max_pages
        self.stats: Counter = Counter()
        self.profile = P.get_profile(dictionary.major or 12)

    # ------------------------------------------------------------------

    def parse(self, wal_paths, max_records=0) -> dict:
        files = []
        for p in wal_paths:
            files.extend(collect_wal_files(Path(p)))
        files = sorted(set(files), key=lambda x: x.name.lower())
        if not files:
            raise FileNotFoundError("未找到可解析的 WAL 文件")
        self.stats["files"] = len(files)
        self.progress(f"共 {len(files)} 个 WAL 段（{files[0].name} → {files[-1].name}）")

        t0 = time.time()
        commit_ts, aborts, sub2top = self._pass1(files, max_records)
        self.progress(f"Pass1 完成: commits={len(commit_ts)} aborts={len(aborts)} "
                      f"耗时 {time.time()-t0:.1f}s")
        self.stats["commits"] = len(commit_ts)
        self.stats["aborts"] = len(aborts)

        t1 = time.time()
        n = self._pass2(files, commit_ts, aborts, sub2top, max_records)
        self.result.commit()
        self.progress(f"Pass2 完成: 产出 {n} 条变更，耗时 {time.time()-t1:.1f}s")
        self.stats["changes"] = n
        self.stats["elapsed"] = round(time.time() - t0, 1)
        return dict(self.stats)

    # ------------------------------------------------------------------

    def _stream(self, files) -> WalStream:
        return WalStream(files, profile=self.profile,
                        system_id=self.dict.system_id,
                        on_note=lambda m: self.progress(f"  [帧] {m}"))

    def _pass1(self, files, max_records=0):
        commit_ts: dict[int, object] = {}
        aborts: set[int] = set()
        sub2top: dict[int, int] = {}
        n = 0
        for rec in self._stream(files).iter_records():
            n += 1
            if max_records and n > max_records:
                break
            # PG13+：记录内 TOPLEVEL_XID 块直接建立 sub→top 映射
            if rec.toplevel_xid and rec.xid != rec.toplevel_xid:
                sub2top[rec.xid] = rec.toplevel_xid
            if rec.rmid == P.RM_XACT_ID:
                op = rec.info & P.XLOG_XACT_OPMASK
                if op in (P.XLOG_XACT_COMMIT, P.XLOG_XACT_COMMIT_PREPARED):
                    commit_ts[rec.xid] = commit_ts_from_record(rec)
                elif op in (P.XLOG_XACT_ABORT, P.XLOG_XACT_ABORT_PREPARED):
                    aborts.add(rec.xid)
                elif op == P.XLOG_XACT_ASSIGNMENT:
                    for sub, top in parse_xact_assignment(rec):
                        sub2top[sub] = top
        self.stats["pass1_records"] = n
        return commit_ts, aborts, sub2top

    def _pass2(self, files, commit_ts, aborts, sub2top, max_records=0):
        from .decode import HeapDecoder
        decoder = HeapDecoder(self.dict, profile=self.profile, max_pages=self.max_pages)
        self.decoder = decoder
        # 打开的事务缓冲：topxid → [Change]
        open_txns: dict[int, list] = {}
        n = 0
        emitted = 0

        def toplevel(xid: int, rec=None) -> int:
            # PG13+：记录内 TOPLEVEL_XID 块优先；PG12：XACT_ASSIGNMENT 映射
            if rec is not None and getattr(rec, "toplevel_xid", 0):
                return rec.toplevel_xid
            return sub2top.get(xid, xid)

        def flush(top: int, ts):
            nonlocal emitted
            changes = open_txns.pop(top, None)
            if not changes:
                return
            self.result.insert_changes(changes, commit_ts=ts, topxid=top)
            emitted += len(changes)

        for rec in self._stream(files).iter_records():
            n += 1
            if max_records and n > max_records:
                break
            if rec.rmid == P.RM_XACT_ID:
                op = rec.info & P.XLOG_XACT_OPMASK
                if op in (P.XLOG_XACT_COMMIT, P.XLOG_XACT_COMMIT_PREPARED):
                    top = toplevel(rec.xid)
                    flush(top, commit_ts.get(rec.xid))
                    for sub, t in list(sub2top.items()):
                        if t == top:
                            flush(sub, commit_ts.get(rec.xid))
                elif op in (P.XLOG_XACT_ABORT, P.XLOG_XACT_ABORT_PREPARED):
                    top = toplevel(rec.xid)
                    open_txns.pop(top, None)
                    for sub, t in list(sub2top.items()):
                        if t == top:
                            open_txns.pop(sub, None)
                continue

            if rec.rmid == P.RM_XLOG_ID:
                # XLOG_FPI / FPI_FOR_HINT：为已知关系播种页像
                op = rec.info & ~P.XLR_INFO_MASK
                if op in (P.XLOG_FPI, P.XLOG_FPI_FOR_HINT):
                    decoder.on_record(rec)
                continue

            if rec.rmid not in (P.RM_HEAP_ID, P.RM_HEAP2_ID):
                continue

            # 物理重放（FPI 播种 + prune）对【所有】记录执行——页是物理的
            seeded = decoder.on_record(rec)

            if rec.xid == 0:
                continue
            top = toplevel(rec.xid, rec)
            if self.only_committed:
                if top in aborts or rec.xid in aborts:
                    decoder.redo_physical(rec, seeded)
                    continue
                if top not in commit_ts and rec.xid not in commit_ts:
                    # 未提交：物理重放但不收集变更（页像必须包含未提交元组）
                    self.stats["uncommitted_skipped"] += 1
                    decoder.redo_physical(rec, seeded)
                    continue

            changes = decoder.decode(rec, seeded)
            if changes:
                open_txns.setdefault(top, []).extend(changes)
                if len(open_txns[top]) > 50000:
                    self.stats["huge_txn_flush"] += 1
                    flush(top, commit_ts.get(top))
            if n % 200000 == 0:
                self.progress(f"  ... 已扫 {n} 条记录，产出 {emitted} 条变更")

        # 流结束时未决事务（无 commit 记录）：按配置丢弃或输出
        leftover = sum(len(v) for v in open_txns.values())
        self.stats["open_txn_changes_left"] = leftover
        if not self.only_committed:
            for top, changes in open_txns.items():
                self.result.insert_changes(changes, topxid=top)
                emitted += len(changes)
        self.stats["pass2_records"] = n
        self.stats["page_store_size"] = len(decoder.pages)
        for k, v in decoder.pages.stats.items():
            self.stats[f"pg_{k}"] = v
        for k, v in decoder.stats.items():
            self.stats[f"dec_{k}"] = v
        return emitted
