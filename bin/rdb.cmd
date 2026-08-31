@echo off
REM Convenience shim: run the bridge CLI from anywhere without setting PYTHONPATH.
REM   rdb status
REM   rdb open terminal
REM
REM The exit code is captured before endlocal and re-raised after it. Without
REM that, endlocal resets ERRORLEVEL and every failure arrives at the caller as
REM 0: 1 for any BridgeError, 2 for a usage error, 130 for Ctrl-C. Since
REM cli.main collapses every BridgeError to 1, the process exit code is the only
REM machine-readable failure signal there is -- and a policy refusal reading as
REM success is exactly the failure this bridge must not have.
setlocal
set "RDB_REPO=%~dp0.."
set "PYTHONPATH=%RDB_REPO%\src;%PYTHONPATH%"
python -m rdbridge %*
set "RDB_RC=%ERRORLEVEL%"
endlocal & exit /b %RDB_RC%
