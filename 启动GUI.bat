@echo off
chcp 936 >nul
title pgwinal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3 and add it to PATH.
  pause
  exit /b 1
)
echo Starting pgwinal GUI ...
python "%~dp0run_gui.py"
if errorlevel 1 (
  echo.
  echo [ERROR] pgwinal exited with an error.
  pause
)
