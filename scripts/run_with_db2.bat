@echo off
setlocal

set "PYTHON_CMD=%~1"
set "MODULE=%~2"
set "VENV_PATH=%~3"
set "ARGS=%~4"

set "DB2_PATH=%VENV_PATH%\Lib\site-packages\clidriver"
set "DB2_BIN_PATH=%DB2_PATH%\bin"
set "DB2_VC14_PATH=%DB2_BIN_PATH%\amd64.VC14.CRT"

if not exist "%DB2_PATH%" (
    echo ERROR: DB2 clidriver directory not found at %DB2_PATH%
    exit /b 1
)

if not exist "%DB2_BIN_PATH%" (
    echo ERROR: DB2 bin directory not found at %DB2_BIN_PATH%
    exit /b 1
)

rem Prepend clidriver paths so the Python process can locate native DLLs
set "PATH=%DB2_PATH%;%DB2_BIN_PATH%;%DB2_VC14_PATH%;%PATH%"
set "IBM_DB_HOME=%DB2_PATH%"
set "VIRTUAL_ENV=%VENV_PATH%"

"%PYTHON_CMD%" -m scripts.preload_and_run %MODULE% %ARGS%