@echo off
REM Launch the Solid + OpenTUI frontend using the local Bun runtime.
cd /d "%~dp0"
set "BUN=node_modules\@oven\bun-windows-x64\bin\bun.exe"
if not exist "%BUN%" (
  echo OpenTUI requires Bun on Windows. Run: npm install bun
  exit /b 1
)
"%BUN%" src-open\index.tsx
