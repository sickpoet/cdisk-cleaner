@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set "SCRIPT=%~dp0cdisk_cleaner.py"
set "PY="

rem 在 PATH 里挨个试，挑一个带 tkinter 的 Python
for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined PY (
        "%%I" -c "import tkinter" >nul 2>nul
        if not errorlevel 1 set "PY=%%I"
    )
)

if not defined PY (
    echo.
    echo [错误] 没有找到带 tkinter 的 Python 3。
    echo 请安装 Python 3，安装时勾选 "tcl/tk and IDLE" 组件，
    echo 或者直接下载打包好的 exe。
    echo.
    pause
    exit /b 1
)

set "PYW=!PY:python.exe=pythonw.exe!"
if exist "!PYW!" (
    start "" "!PYW!" "%SCRIPT%"
) else (
    "!PY%" "%SCRIPT%"
)
