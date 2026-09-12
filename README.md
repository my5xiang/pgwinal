# pgwinal

Windows 友好的 **PostgreSQL WAL 离线解析 / 数据恢复** 独立程序（Python），目标能力对齐 Linux 上的 walminer：

> **继续开发请先读：[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)**（架构、已知限制、优化路线图、排错手册）。

- 解析 WAL 二进制，生成 **DO SQL / UNDO SQL**
- 支持 PostgreSQL **12–18** 物理页格式（按版本无关的记录头/Heap 记录布局解析）
- **不依赖 PG 插件**，纯用户态独立程序，跨平台（重点 Windows）
- 数据字典：从在线库生成 / JSON 导出导入；解析结果写入 SQLite 结果表（类比 `walminer_contents`）
- GUI：添加 WAL 文件、生成字典、解析、过滤查看、导出 SQL
- 研究并处理 **结构变化** 场景：字典缺失 relfilenode / TOAST 截断 / 多快照字典合并

> 设计思路参考 [walminer](https://github.com/universe2000/walminer)：`add wal → build/load dictionary → start → 查看 contents`。

## 目录结构

```
pgwinal/
  pgwinal/
    core/          # WAL 页/记录扫描、heap 元组解码
    dictstore/     # 数据字典生成与 SQLite/JSON 存取
    reverse/       # DO/UNDO SQL 生成、结构变化跟踪
    resultstore/   # 解析结果入库与 SQL 导出
    parse/         # 解析引擎（Pass1 commit / Pass2 heap）
    gui/           # tkinter GUI
  tests/           # 单元测试 + 合成 WAL 样例
  run_gui.py
```

## 安装

```powershell
cd D:\mimo\pgwinal
py -m pip install -r requirements.txt
```

生成数据字典需要能连到 PostgreSQL，安装 `psycopg2-binary` 即可。

## 启动 GUI

```powershell
python run_gui.py
# 或
python -m pgwinal gui
```

## 典型流程（对齐 walminer）

### 1. 在源库生成数据字典

GUI：**生成数据字典(数据库)** → 填 DSN。

或命令行：

```powershell
python -m pgwinal dict --dsn "postgresql://user:pass@127.0.0.1:5432/dbname" --out dict\pgwinal_dict.sqlite
```

字典也可 **导出 JSON** 交给隔离环境 **导入 JSON**（非同库解析场景）。

> 注意：`relfilenode` 与物理文件对应。若在字典生成后执行了 `VACUUM FULL` / `TRUNCATE` / `ALTER TABLESPACE` / 删表，此前 WAL 对该表的变更将无法映射到当前字典（与 walminer 限制一致）。

### 2. 准备 WAL 文件

从 `$PGDATA/pg_wal/`（PG10+）或归档目录拷贝 `0000000X...` 段文件。要求 `wal_level >= replica`（原 `archive`）。

### 3. 解析

GUI：**添加 WAL 文件/目录** → **开始解析**。结果进入 `result/pgwinal_results.sqlite` 的 `walminer_contents` 表。

命令行：

```powershell
python -m pgwinal parse D:\waldir --dict dict\pgwinal_dict.sqlite --out result\pgwinal_results.sqlite --export-do do.sql --export-undo undo.sql
```

### 4. 导出恢复脚本

GUI 导出 **DO SQL**（正放）或 **UNDO SQL**（回滚补偿）。

## 结构变化（DDL）应对策略

| 场景 | 行为 |
|------|------|
| 字典缺少 relfilenode | 标记 unknown，生成占位/原始 SQL，notes 提示 |
| 字典多快照 | 可合并另一份字典（`merge_dictionary`）按 oid+relfilenode 补齐 |
| DROP 列 / 自定义类型 | 列解码回退 hex，notes 标注 |
| TOAST 外部值 | 识别 external/截断，notes 提示 |
| UPDATE 无旧元组镜像 | UNDO 可能不完整（同 walminer） |
| 未提交事务 | 默认过滤（仅 committed xid） |

**重要**：WAL 只含物理行变更；DDL 不会还原出 `ALTER TABLE`。本工具聚焦 **DML 恢复**。若结构已变，请使用 **变更前生成的数据字典** 解析，或合并历史字典快照。

## 结果表（类比 walminer_contents）

```sql
-- 在 result/pgwinal_results.sqlite
SELECT lsn, xid, commit_ts, op, schema_name, table_name, do_sql, undo_sql, notes
FROM walminer_contents
WHERE op = 'DELETE';
```

## 测试

```powershell
python -m unittest tests.test_core -v
```

## 限制说明

1. 仅物理解析 Heap / Heap2 MULTI_INSERT 相关 DML，不还原 DDL。
2. 依赖完整 WAL 文件与匹配时间线的数据字典。
3. 默认 16MB 段大小；若使用自定义 `wal_segment_size` 请扩展扫描器。
4. 列类型覆盖常用类型；复杂数组/范围/几何等回退 hex/占位。
5. 合成 WAL 单测覆盖“扫描→入库→SQL”链路；真实库请用生产 `pg_wal` 验证。
6. 解析大事务（大批 COPY）时内存与耗时会明显增加。

## 许可

MIT（实现独立编写；WAL 布局依据 PostgreSQL 头文件公开定义与 walminer 行为参考）。
