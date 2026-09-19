"""pgwal — PostgreSQL WAL 离线解析 / DML 恢复工具（独立程序，非插件）。

分层：xlogreader(帧) → decode(DML) → dictstore(字典) → sqlgen(DO/UNDO) → resultstore(GUI/CLI)
"""

__version__ = "0.1.0"
