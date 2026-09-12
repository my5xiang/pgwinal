# pgwinal 开发方案说明

> 目的：下次打开本仓库时，读完本文档即可继续对 pgwinal 做优化/排错，无需重新翻聊天记录。  
> 项目根目录：`D:\mimo\pgwinal`  
> 最后更新：2026-09-12

---

## 1. 项目定位

pgwinal 是一个 **Windows 友好的 PostgreSQL WAL 离线解析 / DML 恢复工具**（纯 Python、独立程序、不装 PG 插件）。

对齐 Linux 上 walminer 的使用心智：

1. 从源库生成 **数据字典**
2. 添加 **WAL 文件/目录**
3. **解析** → 生成 **DO SQL / UNDO SQL**
4. 结果写入解析表（类比 `walminer_contents`），可导出 SQL / Excel

### 1.1 需求对照（已完成情况）

| # | 需求 | 状态 | 说明 |
|---|------|------|------|
| 1 | 解析 WAL，得到 DO/UNDO SQL | 部分完成 | Heap INSERT/UPDATE/DELETE/MULTI_INSERT 主路径可用 |
| 2 | 支持 PG 12–18 | 设计上支持 | 按官方 `XLogRecord`/`Heap` 物理布局解析；未在 12–18 全版本真库矩阵验证 |
| 3 | Python 实现 | 完成 | 标准库为主 |
| 4 | 学习 walminer：结果入解析表 | 完成 | SQLite 表 `walminer_contents` |
| 5 | 非插件独立程序 | 完成 | 只读 WAL 文件 |
| 6 | Windows | 完成 | `启动GUI.bat` + tkinter |
| 7 | GUI：加 WAL、建字典 | 完成 | 浅色主题 GUI |
| 8 | 结构变化下能否生成恢复 SQL | 部分完成 | 字典缺失/TOAST 截断/多快照合并有策略，细节可加强 |

---

## 2. 快速恢复上下文（下次必读）

### 2.1 怎么跑起来

```powershell
cd D:\mimo\pgwinal
# 依赖（可选，仅连库建字典时需要驱动）
py -m pip install -r requirements.txt

# GUI
python run_gui.py
# 或
启动GUI.bat

# CLI
python -m pgwinal dict --dsn "postgresql://user:pass@host:5432/db" --out dict\pgwinal_dict.sqlite
python -m pgwinal parse D:\waldir --dict dict\pgwinal_dict.sqlite --out result\pgwinal_results.sqlite --export-do do.sql --export-undo undo.sql

# 测试
set PYTHONPATH=D:\mimo\pgwinal
python -m unittest tests.test_core -v
```

### 2.2 当前测试基线（自动化已通过）

- `tests/test_core.py`：5 个用例（SQL 字面量/生成、WAL 扫描、端到端解析、字典往返）
- 合成 WAL：`tests/make_sample_wal.py` 生成 Heap INSERT + XACT COMMIT
- 已验证：解析出 1 条 INSERT、可导出合法 xlsx（stdlib zip 写 xlsx）

**未验证：** 真实 `pg_wal`、PG 12–18 全版本、复杂类型（数组/范围/几何）、大事务、TOAST 重列、DDL 后旧 WAL。

### 2.3 用户可见功能地图（GUI）

| 区域 | 功能 |
|------|------|
| 顶栏第一行 | 品牌、统计摘要、主按钮「开始解析」 |
| 顶栏第二行 | 添加文件/目录、清空、生成/导入/导出字典、导出DO/UNDO/Excel |
| 左侧 | WAL 列表、操作过滤、表名过滤 |
| 中部 | 结果表（按操作着色）、双击看详情 |
| 底部 | 活动日志、进度条、状态栏 |

布局约定：工具栏为**双行**，默认窗口 `1440×900`，保证全部按钮可见；勿再改回单行长工具栏（窄屏会截断）。

数据目录默认：

- 字典：`dict/pgwinal_dict.sqlite`
- 结果：`result/pgwinal_results.sqlite`

---

## 3. 架构

```
WAL 文件
   │
   ▼
core/xlog.py          页/记录扫描 → WalRecord（blocks + main_data）
   │
   ▼
parse/engine.py       Pass1 收集 commit xid
                      Pass2 解码 Heap 变更
   │
   ├─ dictstore       relfilenode → RelationDef/AttributeDef
   ├─ core/heaptuple  元组字段按 type OID 解码
   └─ reverse/sqlgen  DO / UNDO SQL + SchemaChangeTracker
   │
   ▼
resultstore           SQLite walminer_contents
   │
   ├─ export_sql
   └─ export_excel (stdlib xlsx)
   │
   ▼
gui/app.py            tkinter 操作与展示
```

### 3.1 模块职责

| 路径 | 职责 | 关键点 |
|------|------|--------|
| `pgwinal/constants.py` | Rmgr / Heap opcode / infomask 常量 | 与 PG 头文件对齐 |
| `pgwinal/core/models.py` | RelFileLocator / XLogRecord / ChangeRecord | 跨层数据模型 |
| `pgwinal/core/xlog.py` | 读 8K 页、拼接跨页记录、拆 block+main | **块引用头 4 字节**（曾修过 6→4 bug） |
| `pgwinal/core/heaptuple.py` | null bitmap、常用类型 varlena 解码 | 复杂类型可 hex 回退 |
| `pgwinal/dictstore/schema.py` | 字典模型 + SQLite 存取 + JSON 导入导出 | 按 `relfilenode+db` 查找 |
| `pgwinal/dictstore/builder.py` | 连在线 PG 拉 catalog | 可选 psycopg2 |
| `pgwinal/reverse/sqlgen.py` | INSERT/UPDATE/DELETE SQL；结构变化 notes | 引号/字面量安全 |
| `pgwinal/parse/engine.py` | 两阶段解析编排 | 默认 `only_committed=True` |
| `pgwinal/resultstore/resultdb.py` | 结果表、过滤查询、SQL/Excel 导出 | |
| `pgwinal/resultstore/xlsx.py` | 无第三方依赖写 xlsx | zip+xml |
| `pgwinal/gui/app.py` | 全部 UI | 浅色主题 tokens |

### 3.2 关键二进制常量（实现依据）

来自 PostgreSQL 官方头文件（PG12–16 稳定布局；17/18 需再核对）：

- `XLogRecord` 固定头 **24 字节**：`tot_len, xid, prev, info, rmid, pad, crc`
- `XLogPageHeader` **24 字节**：`magic, info, tli, pageaddr, blckseg, xrecoff`
- `XLogRecordBlockHeader` **4 字节**：`id, fork_flags, data_length`  
  - 随后可选 ImageHeader / RelFileLocator(12) / BlockNumber(4) / payload
- Main data：`0xFF + len`（short）或 `0xFE + uint32 len`（long）
- Heap rmgr id=10；opcode 在 `xl_info & 0x70`  
  - INSERT=0x00 DELETE=0x10 UPDATE=0x20 HOT_UPDATE=0x40  
- Heap2 rmgr id=9；MULTI_INSERT=0x50
- Transaction rmgr id=1；COMMIT=0x00（info 高 3 位）

**历史坑：** `_split_body` 里块头曾写成 `pos += 6`，导致 relfilenode 错位；已改为 `pos += 4`。

---

## 4. 数据流与结果模型

### 4.1 解析流程

1. 收集 WAL 文件（目录或文件；识别 `0000000X` 24 位段名，或 `.wal/.log/.partial`）
2. Pass1：扫全部记录，收集 `commit` 的 xid → `commit_xids` / `commit_ts`
3. Pass2：对 HEAP/HEAP2 解码；若 `only_committed` 且 xid 不在集合中则跳过
4. 用字典把 `(spc, db, relfilenode)` 映射到表名列；生成 DO/UNDO
5. 批量插入 `walminer_contents`

### 4.2 结果表字段

`walminer_contents`：`lsn, xid, commit_ts, op, schema_name, table_name, relfilenode, block/offset, row_data, old_row_data, do_sql, undo_sql, is_catalog, notes`

Excel 导出列与上述业务字段对应（见 `export_excel`）。

### 4.3 字典结构

- `relations`: rel_oid, schema_name, rel_name, **relfilenode**, reltablespace, db_oid, relkind  
- `attributes`: attnum, attname, type_oid, type_name, typmod, attnotnull, is_dropped...

查找逻辑：`find_by_relfilenode(rel_number, db_oid)`（同库优先精确 db）。

---

## 5. 已知限制与根因

| 限制 | 根因 | 优化方向 |
|------|------|----------|
| 无字典时 SQL 只能占位 | WAL 无列名/类型 | 强制先建字典；或从 toast/column 镜像推断（难） |
| UPDATE 可能缺旧值 | WAL 可能只带 key/new image | 提示人工核对；结合 full page image 提取（进阶） |
| DELETE 可能无行像 | `xl_heap_delete` 常只有 offnum | 有 `CONTAINS_OLD_*` 才解码 |
| DDL 不还原 | 工具只做 DML | 可选：解析 `xl_heap_truncate` / catalog heap 变更提示 |
| 复杂类型回退 hex | 解码器未覆盖全类型 | 扩 `heaptuple` 类型表 |
| 默认 16MB 段 | `DEFAULT_WAL_SEG_SIZE` | GUI/CLI 暴露 seg_size；自动从段文件名推断 |
| TOAST 外链值不完整 | 仅识别 external | 需同时解析 toast 表 WAL 并重拼 |
| 真库兼容未矩阵测 | 仅合成样例 | 建 PG12–18 docker/本机对照脚本 |
| GUI 无项目会话文件 | 每次固定 dict/result 路径 | 增加 project.json / 最近打开 |

---

## 6. 优化路线图（建议优先级）

### P0 — 正确性（优先做）

1. **真实 WAL 对照测试**  
   - 固定：`wal_level=replica`，小表 INSERT/UPDATE/DELETE 后 `pg_switch_wal()`  
   - 对比 pg_waldump 输出与 pgwinal 的 xid/表/行值  
   - 建 `tests/test_real_wal.py`（有 DSN/目录时才跑）
2. **UPDATE/DELETE 旧元组路径加固**  
   - 明确 `XLH_UPDATE_CONTAINS_OLD_TUPLE|KEY` 与 new tuple 的切分（现 main 消费策略偏启发式）  
   - 对 block data + main 双份 tuple 的优先级写死并单测
3. **跨页长记录**  
   - 补更多单页拼接、XLOG_FIRST_CONTINUE 样例
4. **多 segment 连续扫描**  
   - 按文件名排序 + 段间 LSN 连续性校验日志

### P1 — 功能完整度

5. 类型系统：numeric 精确、timestamp 时区、常用数组、bytea 文本、enum（按 typlen）  
6. TOAST：识别 external 值 + 可选“带 toast 关系字典/双 WAL”重拼  
7. `wal_segment_size` 配置；从控制文件/CLI 读取  
8. 多字典快照 GUI 合并界面（底层 `merge_dictionary` 已有）  
9. 只解析某表 / 时间窗 / xid 窗（对齐 walminer 参数）

### P2 — 体验

10. 解析进度百分比（按文件字节）  
11. 结果分页/虚拟列表（大数据量）  
12. 右键：复制 SQL、按表导出、只看 DO  
13. 主题记忆、深浅色切换  
14. 打包：`PyInstaller` 出单文件 exe（注意 tkinter）

### P3 — 研究项（需求 #8）

15. 从 catalog 变更 WAL 推断 DROP COLUMN / ALTER TYPE，生成字典差异报告  
16. 字典版本时间线：按 LSN 选择“当时”的 relfilenode 映射

---

## 7. 测试与验收

### 7.1 已有

```text
tests/test_core.py
  TestSql.test_literal / test_generate_insert
  TestScan.test_scan_synthetic
  TestEngine.test_parse_end_to_end
  TestDictStore.test_roundtrip
```

### 7.2 建议新增用例清单

| 用例 | 目的 |
|------|------|
| multi_insert 合成 WAL | heap2 路径 |
| update with old+new | DO/UNDO 成对 |
| delete with old key | undo INSERT |
| 无 commit 记录 | only_committed 过滤 |
| 无字典 | 占位 SQL + notes |
| xlsx 含中文列名 | Excel 兼容 |
| 跨页 record | scanner |
| 真库 mini E2E | 对照 pg_waldump |

### 7.3 验收标准（真库）

1. 字典含目标表  
2. 小规模 DML 后拿到连续 WAL  
3. pgwinal 结果中 DO 能在干净库重放，行与源一致  
4. 对目标行做反向 UNDO 后回到原状态（在可精确定位时）

---

## 8. 排错手册（已踩过的坑）

| 症状 | 原因 | 处理 |
|------|------|------|
| relfilenode=0 / db 乱码 | 块头长度错误 | 确认 `SizeOfXLogRecordBlockHeader=4` |
| GUI 一开就 AttributeError | `StringVar.configure` | 用 `.set()` |
| bat 中文乱码 | UTF-8 bat + GBK 控制台 | bat 保持纯 ASCII |
| 无字典 SQL 全是 UNKNOWN | 未建/未加载字典 | GUI「生成/导入字典」 |
| 解析 0 条 | 非 commit；或 rmgr 非 Heap；或 only_committed | `--all-tx` 或检查 xl_info |
| INSERT 有表名但列解码怪 | type OID 不在映射 | 扩 `heaptuple._decode_attribute` |
| Excel 打不开 | 手写 xlsx 结构问题 | 检查 `resultstore/xlsx.py` zip 部件 |

---

## 9. 代码约定与改代码时注意

1. **改 WAL 解析必须补合成样例 + 单测**，不要只改数字。  
2. 字典查找键是 **relfilenode**，不是 pg_class.oid。  
3. SQL 生成必须走 `quote_ident` / `literal`，禁止拼接裸用户列名。  
4. GUI 只调用 store/engine 公共方法；耗时操作用 `threading` + `self.after` 回 UI。  
5. 保持 Windows 中文路径可用；文件 IO 显式 `encoding="utf-8"`。  
6. 不要引入必须联网的依赖；Excel 已用 stdlib。  
7. 主题色集中在 `app.py` 顶部 `COLORS`。

---

## 10. 推荐工作流（下次继续开发）

1. 读本文档 §2、§5、§6  
2. `python -m unittest tests.test_core -v` 确认基线  
3. 从 P0 选一项 → 改代码 → 加测试 → 再跑测试  
4. 若用户有真实 WAL：先 `python -m pgwinal parse ...` 看 notes / unknown 表  
5. 更新本文件的「已完成/未验证」表和路线图勾选  

### 建议的下一个具体任务（二选一）

**任务 A（正确性）：** 对照 `pg_waldump` 写 `tests/test_xlog_vs_waldump.md` 说明 + 至少 3 个真实字段对比断言。  

**任务 B（产品）：** GUI 支持「同时加载第二份历史字典并合并」，按钮调用已有 `SchemaChangeTracker.merge_dictionary`。

---

## 11. 文件树（核心）

```
D:\mimo\pgwinal\
  README.md
  本方案: docs\DEVELOPMENT.md
  requirements.txt
  run_gui.py
  启动GUI.bat
  pgwinal\
    __init__.py
    __main__.py
    constants.py
    core\{models,xlog,heaptuple}.py
    dictstore\{schema,builder}.py
    reverse\sqlgen.py
    parse\engine.py
    resultstore\{resultdb,xlsx}.py
    gui\app.py
  tests\
    test_core.py
    make_sample_wal.py
    sample\000000010000000000000001
```

---

## 12. 参考资料

- walminer 行为与限制：`https://github.com/universe2000/walminer`  
- PG16 `xlogrecord.h` / `heapam_xlog.h` / `rmgrlist.h` / `relfilelocator.h`  
- `pg_waldump`：解析真值对照  
- PostgreSQL 逻辑解码：可借鉴 toast/relation 映射，但本工具是**物理 WAL** 路径

---

**一句话交接：**  
pgwinal 已具备「字典 + 物理 WAL → DO/UNDO SQLite/Excel + 浅色 GUI」的可运行骨架；下一步优先用**真实 pg_waldump 对照**加固 INSERT/UPDATE/DELETE 正确性，再扩类型与 TOAST。
