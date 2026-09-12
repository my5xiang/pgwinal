from pathlib import Path

# Pure ASCII bat body — avoids GBK/UTF-8 mojibake in cmd.exe
bat = "\n".join(
    [
        "@echo off",
        "chcp 936 >nul",
        'cd /d "%~dp0"',
        'set "PYTHONPATH=%~dp0"',
        "where python >nul 2>nul",
        "if errorlevel 1 (",
        "  echo [ERROR] Python not found. Install Python 3 and add it to PATH.",
        "  pause",
        "  exit /b 1",
        ")",
        "echo Starting pgwinal GUI ...",
        'python "%~dp0run_gui.py"',
        "if errorlevel 1 (",
        "  echo.",
        "  echo [ERROR] pgwinal exited with an error.",
        "  pause",
        ")",
        "",
    ]
)

root = Path(r"D:\mimo\pgwinal")
(root / "start_gui.bat").write_bytes(bat.encode("ascii"))
(root / "启动GUI.bat").write_bytes(bat.encode("ascii"))
print("ok")
