@echo off
setlocal

set "MWBC_ROOT=%~dp0.."
set "PYTHON_EXE=%LOCALAPPDATA%\mwbc-python\python-3.12.10\python.exe"
set "MWBC_EXE=%LOCALAPPDATA%\mwbc-python\python-3.12.10\Scripts\mwbc.exe"

if exist "%MWBC_EXE%" (
  "%MWBC_EXE%" host --backend pynput
  exit /b %ERRORLEVEL%
)

if not exist "%PYTHON_EXE%" (
  echo Windows Python not found: %PYTHON_EXE%
  exit /b 1
)

set "PYTHONPATH=%MWBC_ROOT%\src"
"%PYTHON_EXE%" -u -m mwbc host --backend pynput
