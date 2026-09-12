from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.make_sample_wal import build_sample_wal
from pgwinal.core.xlog import WalScanner

wal = Path(__file__).resolve().parent / "sample" / "000000010000000000000001"
build_sample_wal(wal)
for rec in WalScanner([wal]).scan():
    print("rmid", rec.rmid, "info", hex(rec.info), "xid", rec.xid, "lsn", hex(rec.rec.start_lsn))
    print(" blocks", len(rec.blocks), "main", rec.main_data.hex())
    for b in rec.blocks:
        print(
            "  ",
            b.block_id,
            b.has_data,
            b.rlocator,
            b.block_num,
            "payload",
            b.payload[:20].hex() if b.payload else None,
            "len",
            len(b.payload),
        )
    print(" body hex", rec.rec.data[:80].hex())
