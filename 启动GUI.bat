@echo off
rem ============================================================
rem  pgwalnew GUI launcher
rem  PostgreSQL WAL offline parser / DML recovery tool
rem ============================================================
title pgwalnew - WAL Parser
cd /d "%~dp0"

rem ---- find python ----
set PY=
where python >nul 2>nul && set PY=python
if not defined PY (
    where py >nul 2>nul && set PY=py
)
if not defined PY (
    echo [ERROR] Python not found. Please install Python 3.11+ and retry.
    pause
    exit /b 1
)

rem ---- check core dependency (crc32c) ----
%PY% -c "import crc32c" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing crc32c ...
    %PY% -m pip install crc32c
)

rem ---- check GUI dependency (PySide6) ----
%PY% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] PySide6 not found. GUI needs it ^(about 280MB^).
    choice /C YN /M "Install now"
    if errorlevel 2 (
        echo [TIP] CLI still works without PySide6:
        echo        %PY% -m pgwalnew parse WALDIR --dict dict.sqlite --out result.sqlite
        pause
        exit /b 1
    )
    %PY% -m pip install PySide6
)

echo Starting pgwalnew GUI ...
%PY% -X utf8 run_gui.py
if errorlevel 1 (
    echo.
    echo [ERROR] GUI exited with an error.
    pause
)
