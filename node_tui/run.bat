@echo off
setlocal
REM Launch the Solid + OpenTUI frontend using the local Bun runtime.
REM Preserve the caller's directory as the workspace before switching to node_tui.
if not defined HARNESS_WORKSPACE set "HARNESS_WORKSPACE=%CD%"
cd /d "%~dp0"
set "BUN=node_modules\@oven\bun-windows-x64\bin\bun.exe"
if not exist "%BUN%" (
  echo OpenTUI requires Bun on Windows. Run: npm install bun
  exit /b 1
)
"%BUN%" src-open\index.tsx %*
set "PROOFTASK_EXIT=%ERRORLEVEL%"
endlocal & exit /b %PROOFTASK_EXIT%
