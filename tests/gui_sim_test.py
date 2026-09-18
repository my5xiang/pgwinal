from __future__ import annotations

import sys
import time
import traceback
import tkinter as tk
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

errors: list[str] = []
ok: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        ok.append(name)
        print(f"[OK] {name}" + (f" — {detail}" if detail else ""))
    else:
        errors.append(f"{name}: {detail}")
        print(f"[FAIL] {name} — {detail}")


def walk(w, acc=None):
    if acc is None:
        acc = []
    acc.append(w)
    for c in w.winfo_children():
        walk(c, acc)
    return acc


def main() -> int:
    app = None
    try:
        from pgwinal.core.xlog import collect_wal_files
        from pgwinal.dictstore.builder import build_dictionary_from_postgres
        from pgwinal.dictstore.schema import DictStore
        from pgwinal.gui.app import App
        from pgwinal.resultstore.resultdb import ResultStore
        from pgwinal.gui import app as app_mod

        # avoid modal dialogs blocking simulation
        app_mod.messagebox.showinfo = mock.MagicMock()
        app_mod.messagebox.showerror = mock.MagicMock()
        app_mod.messagebox.showwarning = mock.MagicMock()
        app_mod.messagebox.askyesno = mock.MagicMock(return_value=True)
        app_mod.filedialog.askopenfilename = mock.MagicMock(return_value="")
        app_mod.filedialog.askdirectory = mock.MagicMock(return_value="")
        app_mod.filedialog.asksaveasfilename = mock.MagicMock(return_value="")

        app = App()
        app.update()
        check(
            "GUI 启动",
            True,
            f"title={app.title()} {app.winfo_width()}x{app.winfo_height()}",
        )

        # 1) dictionary dialog layout
        dialogs: list = []
        orig_toplevel = tk.Toplevel

        class SpyToplevel(orig_toplevel):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                dialogs.append(self)

        tk.Toplevel = SpyToplevel
        try:
            app.on_build_dict()
            app.update()
            check("打开生成字典弹窗", len(dialogs) == 1)
            win = dialogs[0]
            win.update_idletasks()
            app.update()
            w, h = win.winfo_width(), win.winfo_height()
            check("弹窗尺寸 >= 640x420", w >= 640 and h >= 420, f"{w}x{h}")
            labels = []
            for wdg in walk(win):
                try:
                    t = wdg.cget("text")
                    if t:
                        labels.append((str(t), wdg.winfo_y(), wdg.winfo_height()))
                except Exception:
                    pass
            texts = [x[0] for x in labels]
            check(
                "弹窗含生成字典/取消",
                any("生成字典" in x for x in texts)
                and any("取消" in x for x in texts),
                str(texts),
            )
            gen = [x for x in labels if "生成字典" in x[0]]
            if gen:
                check("生成按钮在窗口内", gen[0][1] < h, f"y={gen[0][1]} h={h}")
            entries = [
                wdg for wdg in walk(win) if wdg.winfo_class() in ("TEntry", "Entry")
            ]
            check("弹窗有输入框", len(entries) >= 2, f"entries={len(entries)}")
            win.destroy()
            app.update()
        finally:
            tk.Toplevel = orig_toplevel

        # 2) dictionary generation via dialog worker + UI queue
        dsn = "postgresql://user:pass@host:port/dbname"
        d = build_dictionary_from_postgres(dsn, include_system=False)
        DictStore(app.dict_path).save_dictionary(d)
        app.dict_store = DictStore(app.dict_path)
        check(
            "生成字典",
            len(d.relations) > 0,
            f"tables={len(d.relations)} pg={d.pg_version}",
        )
        app._update_stats()
        app.update()

        # 3) add WAL
        wal_dir = Path(r"D:\mimo\pgwinal\testpg")
        files = collect_wal_files(wal_dir)
        check(
            "收集WAL文件",
            len(files) >= 6,
            f"n={len(files)} {[p.name for p in files]}",
        )
        app.wal_paths.clear()
        for p in list(app.result_store.list_wal_files()):
            try:
                app.result_store.remove_wal_file(p)
            except Exception:
                pass
        for f in files:
            app._add_wal_path(f)
        app.update()
        check(
            "GUI WAL列表",
            len(app.wal_paths) == len(files),
            f"paths={len(app.wal_paths)} label={app.wal_count.cget('text')}",
        )

        # 4) parse via on_parse; pump UI queue on main thread (like mainloop)
        app.on_parse()
        check("on_parse 启动 busy", app._parsing is True)

        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                app._drain_ui_queue()
                app.update()
            except Exception:
                break
            if not app._parsing and app._ui_q.empty():
                break
            time.sleep(0.03)
        try:
            app._drain_ui_queue()
            app.update()
        except Exception:
            pass
        check(
            "解析结束 busy=False",
            app._parsing is False,
            f"elapsed={time.time() - t0:.2f}s",
        )

        rs = ResultStore(app.result_path)
        n = rs.count()
        rows = rs.conn.execute(
            "SELECT op, schema_name, table_name, do_sql FROM walminer_contents"
        ).fetchall()
        sample = [(r[0], f"{r[1]}.{r[2]}") for r in rows[:5]]
        check("结果行数 > 0", n > 0, f"count={n} sample={sample}")
        if rows:
            check(
                "识别 bdsy.sys_log",
                any(r[1] == "bdsy" and r[2] == "sys_log" for r in rows),
                str({(r[1], r[2]) for r in rows}),
            )
            check("DO SQL 非空", all(bool(r[3]) for r in rows))

        tree_n = len(app.tree.get_children())
        check("结果表格有行", tree_n > 0, f"tree_rows={tree_n}")
        log_text = app.log.get("1.0", tk.END)
        check(
            "日志含解析结果",
            ("解析完成" in log_text)
            or ("result_count" in log_text)
            or ("Pass" in log_text)
            or (str(n) in log_text),
            log_text[-300:].replace("\n", " | "),
        )

        # 5) export
        out_do = Path(r"D:\mimo\pgwinal\result\gui_sim_do.sql")
        out_undo = Path(r"D:\mimo\pgwinal\result\gui_sim_undo.sql")
        out_xlsx = Path(r"D:\mimo\pgwinal\result\gui_sim.xlsx")
        n_do = app.result_store.export_sql(out_do, only_do=True)
        n_undo = app.result_store.export_sql(out_undo, only_undo=True)
        try:
            n_x = app.result_store.export_excel(out_xlsx, op=None, table_like=None)
        except TypeError:
            n_x = app.result_store.export_excel(out_xlsx)
        check("导出DO", n_do > 0 and out_do.exists(), f"n={n_do}")
        check("导出UNDO", out_undo.exists(), f"n={n_undo}")
        check(
            "导出Excel",
            out_xlsx.exists() and out_xlsx.stat().st_size > 0,
            f"n={n_x} size={out_xlsx.stat().st_size if out_xlsx.exists() else 0}",
        )

        # 6) dictionary export/import
        jpath = Path(r"D:\mimo\pgwinal\result\gui_sim_dict.json")
        DictStore(app.dict_path).export_json(jpath)
        check(
            "字典导出JSON",
            jpath.exists() and jpath.stat().st_size > 1000,
            f"size={jpath.stat().st_size}",
        )
        d2 = DictStore(app.dict_path).import_json(jpath)
        check(
            "字典导入JSON",
            len(d2.relations) == len(d.relations),
            f"{len(d2.relations)} vs {len(d.relations)}",
        )

        # 7) dialog worker path for build dict (thread + queue)
        tk.Toplevel = SpyToplevel
        dialogs.clear()
        try:
            app.on_build_dict()
            app.update()
            check("再次打开字典弹窗", len(dialogs) == 1)
            if dialogs:
                # click generate via command
                gen_btn = None
                for wdg in walk(dialogs[0]):
                    try:
                        if wdg.cget("text") == "生成字典":
                            gen_btn = wdg
                            break
                    except Exception:
                        pass
                if gen_btn:
                    gen_btn.invoke()
                    t1 = time.time()
                    while time.time() - t1 < 40:
                        app._drain_ui_queue()
                        app.update()
                        if not dialogs or not dialogs[0].winfo_exists():
                            break
                        time.sleep(0.03)
                    app._drain_ui_queue()
                    app.update()
                    check(
                        "字典弹窗生成线程完成并关闭",
                        (not dialogs) or (not dialogs[0].winfo_exists()),
                        f"elapsed={time.time()-t1:.1f}s",
                    )
                else:
                    check("找到生成字典按钮", False, "not found")
            if dialogs:
                try:
                    dialogs[0].destroy()
                except Exception:
                    pass
            app.update()
        finally:
            tk.Toplevel = orig_toplevel

        try:
            app.destroy()
        except Exception:
            pass
        app = None
    except Exception:
        traceback.print_exc()
        errors.append(traceback.format_exc())
        try:
            if app:
                app.destroy()
        except Exception:
            pass

    print("\n==== SUMMARY ====")
    print("PASS", len(ok), "FAIL", len(errors))
    for e in errors:
        print(" -", e[:400])
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
