# pgwinalnew

Windows 平台 **PostgreSQL WAL 离线解析 / DML 数据恢复**独立程序（非插件、带 GUI），目标对齐 Linux 上的 walminer。

> **开发总纲：[开发方案.md](开发方案.md)** —— 架构、失败根因、版本策略、验证体系、里程碑。
> **当前状态：核心已实现并在 736MB 真实 WAL 上端到端验证通过（见下）。**

## 实现状态（2026-09-19）

在 aphx 真实数据（46 段 × 16MB，PG 12.22，system_id 已配对验证）上的实测结果：

| 指标 | 结果 |
|------|------|
| 帧级验证 | **1,722,307 条记录，CRC32C 100%、prev 链 100%**（46 段全量） |
| 解析产出 | **146,613 条变更**（86 秒，约 8.5 MB/s 全解码） |
| INSERT 可执行 | **40,699 / 40,699（100%）** |
| DELETE 可执行 | **25,579 / 25,651（99.7%）** |
| UPDATE 可执行 | **45,425 / 80,263（56.5%）**；其中 30,668 条为真实无变更 UPDATE（`SET x=x` 空转，正确跳过），其余为窗口盲区 |
| **SQL 精简化（v1.3）** | DELETE/INSERT-UNDO **按主键定位**（字典 pk_attnums）；UPDATE **仅变更列**（平均 2 列）；文件从 416MB 降至 **65MB（-84%）** |
| DO / UNDO 导出 | **各 111,703 条**可执行 SQL（do.sql 65MB / undo.sql 52MB） |
| 解码错误 | 0 |
| **真实回放测试** | **DO/UNDO 各 111,703 条在 PG12.22 空库上全部执行 0 错误；UNDO 完整逆转并恢复 35 行被删数据**（详见 [回放测试报告.md](回放测试报告.md)） |

> UPDATE 剩余 7.5% 不可执行为固有盲区：WAL 窗口从中途开始，首个 checkpoint 前的页无 FPI 可播种（与 walminer 同款物理限制；精确解析模式可消除）。

## 快速开始

**双击 `启动GUI.bat`**（自动检测/安装依赖后启动图形界面）。

```powershell
cd D:\mimo\pgwinal
py -m pip install PySide6 crc32c        # GUI 与 CRC 加速（核心解析仅需 crc32c）

# CLI：解析真实数据（字典用补丁版：含主键信息，DELETE/UPDATE 按主键精简）
python -m pgwalnew parse D:\mimo\pgwinal\testpg ^
    --dict D:\mimo\pgwinal\dict\pgwalnew_dict_aphx.sqlite ^
    --out result\pgwalnew_results.sqlite --export-do result\do.sql --export-undo result\undo.sql

# GUI（布局继承旧项目：双行工具栏/左右分栏/结果着色/底部日志）
python run_gui.py

# 单元测试（含真实数据帧级验证）
python -m unittest discover tests -v

# M0 帧级验证报告
python tools\validate_framing.py D:\mimo\pgwinal\testpg --dict D:\mimo\pgwinal\dict\pgwalnew_dict_aphx.sqlite
```

从在线库生成新字典（需 psycopg2）：

```powershell
python -m pgwalnew dict --dsn "postgresql://user:pass@host:5432/db" --out dict.sqlite
```

## 结果表（对齐 walminer_contents）

```sql
SELECT sqlno, xid, op, commit_ts, schema_name, table_name,
       do_sql, undo_sql, undo_source, executable, notes
FROM walminer_contents
WHERE op = 'DELETE' AND executable = 1;
```

- `undo_source`：`record`（WAL 记录内）/ `history`（流内历史）/ `fpi`（页像重放）/ `incomplete`
- `executable`：五条件全满足（CRC/字典映射/类型完整/UNDO 来源/SQL 合法）

## 架构（实现于 `pgwalnew/`）

```
xlogreader.py   严格帧扫描：CRC32C(body‖header[0:20]) + prev 链 + 跨页/跨段续记录
                + 记录头跨页分裂 + XLOG_SWITCH + system_id 预检
decode.py       Heap DML 解码：INSERT/UPDATE(含 prefix-suffix 差量)/DELETE/MULTI_INSERT
pageredo.py     页像重放引擎：FPI 播种 + PageAddItem redo + prune 应用（UNDO 地基）
heaptuple.py    元组 deform：null bitmap + 对齐（相对 hoff 基点）+ varlena 标签
typereg.py      类型注册表：numeric（长/短格式位打包）等 40+ 类型
dictstore.py    aphx 兼容字典加载 + relfilenode 解析
sqlgen.py       DO/UNDO SQL（IS NOT DISTINCT FROM，NULL 安全）
resultstore.py  SQLite 结果表 + SQL 导出（UNDO 逆 LSN 序）
engine.py       两遍扫描：Pass1 事务收集（含 XACT_ASSIGNMENT 子事务映射）
gui/app.py      PySide6 GUI（QThread 后台解析，分页浏览，详情对话框）
```

**版本差异已核实的坑**（详见开发方案 §7.2）：`bimg_info` 压缩位 PG12–14 与 PG15+ 语义互换（0x02/0x04）；XLOG rmgr 的 `XLOG_SWITCH=0x40`（0x10 是在线检查点）；varlena 标签小端位序（00=4B未压缩/10=4B压缩/奇数=1B/0x01=外部）。

## 参考实现

| | 说明 |
|---|---|
| [walminer 3.x](https://gitee.com/movead/XLogMiner)（开源，MIT） | 算法参考（页像重放引擎/字典/SQL 生成）；源码在 `reference/XLogMiner/` |
| [walminer.cn](http://walminer.cn/)（闭源 4.x） | PG10–18、仅 Linux——Windows+开源是空缺 |

## 与旧项目的关系

本项目即原 pgwinal 的**重新实现**（v1 完整重写）：旧实现的失败根因分析与四条纪律（CRC 严格校验、常量对照官方头文件、真值对照、不确定即标记）见开发方案 §2。
git 仓库沿用原 pgwinal 的历史；WAL 段（`testpg/`）、字典（`dict/`）、解析产物（`result/`）、测试实例（`pg12/`、`pgdata/`）与参考源码（`reference/`）均不进版本管理（见 `.gitignore`）。
