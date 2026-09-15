@echo off
title Qinling Grab Console (Python)
setlocal

set "PYEXE="
if exist "D:\Anaconda3\python.exe" set "PYEXE=D:\Anaconda3\python.exe"
if "%PYEXE%"=="" for %%P in (python.exe) do set "PYEXE=%%~$PATH:P"
if "%PYEXE%"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python39\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
if "%PYEXE%"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if "%PYEXE%"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if "%PYEXE%"=="" (
  echo [ERROR] Python not found. Please install Python 3.7+ or edit PYEXE in this file.
  pause
  exit /b 1
)

REM Anaconda needs Library\bin in PATH, otherwise _ssl DLL fails to load
set "PATH=D:\Anaconda3;D:\Anaconda3\Library\mingw-w64\bin;D:\Anaconda3\Library\usr\bin;D:\Anaconda3\Library\bin;D:\Anaconda3\Scripts;D:\Anaconda3\condabin;%PATH%"

cd /d "%~dp0"
echo Using Python: %PYEXE%
echo Starting grab console ^(Python^) ...
echo URL: https://127.0.0.1:8787/
echo 首次访问有证书安全警告, 选择继续访问即可
echo 访问令牌见上方控制台输出 (或 data\token.txt)
echo Press Ctrl+C to stop.
REM 默认仅本机访问; 需要远程时在命令行加: --host 0.0.0.0
"%PYEXE%" grab_server.py --host 127.0.0.1 %*
echo.
pause