"""展示 DO/UNDO SQL 配对样本（UTF-8 输出）。"""
import sqlite3
import sys

conn = sqlite3.connect('result_final.sqlite')
cur = conn.cursor()


def show(title, sql, maxlen=1500):
    print(f'  {title}:')
    if sql is None:
        print('    （无）')
        return
    if len(sql) > maxlen:
        # 截断超长语句的中间部分
        sql = sql[:maxlen // 2] + '\n      ......(截断)......\n      ' + sql[-maxlen // 2:]
    for line in sql.split('\n'):
        print('    ' + line)
    print()


def pick(op, table=None, where_extra=''):
    w = f"op='{op}' AND executable=1 {where_extra}"
    if table:
        w += f" AND table_name='{table}'"
    return cur.execute(
        f"SELECT do_sql, undo_sql, start_lsn, commit_ts, schema_name, table_name, "
        f"undo_source FROM walminer_contents WHERE {w} LIMIT 1").fetchone()


print('=' * 78)
print('样本 1：INSERT —— DO 是插入，UNDO 是删除该行')
print('=' * 78)
r = pick('INSERT', 'x_actlog')
print(f'  表: {r[4]}.{r[5]}   lsn: {r[2]}   commit: {r[3]}')
show('DO  SQL', r[0])
show('UNDO SQL', r[1])

print('=' * 78)
print('样本 2：UPDATE —— DO 用新值 SET/旧值 WHERE，UNDO 反过来（旧值 SET/新值 WHERE）')
print('=' * 78)
r = pick('UPDATE', 'x_sysnote')
print(f'  表: {r[4]}.{r[5]}   lsn: {r[2]}   commit: {r[3]}   undo来源: {r[6]}')
show('DO  SQL', r[0])
show('UNDO SQL', r[1])

print('=' * 78)
print('样本 3：DELETE —— DO 是删除，UNDO 是把被删的行插回来（数据恢复！）')
print('=' * 78)
r = cur.execute(
    "SELECT do_sql, undo_sql, start_lsn, commit_ts, schema_name, table_name, undo_source "
    "FROM walminer_contents WHERE op='DELETE' AND table_name='e_cw_ysyf' "
    "AND old_row_data LIKE '%HX030126092614%' AND executable=1 LIMIT 1").fetchone()
print(f'  表: {r[4]}.{r[5]}   lsn: {r[2]}   commit: {r[3]}   undo来源: {r[6]}')
print('  （此行正是 UNDO 回放后"复活"的 18 行之一，已与回放库交叉验证一致）')
show('DO  SQL', r[0], 800)
show('UNDO SQL', r[1])

conn.close()
